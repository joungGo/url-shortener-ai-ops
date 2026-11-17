"""Explicitly enabled TLS ingress / reply relay; no worker or recovery launcher.

Only check-config and probes are safe without connection approval. Ingress owns
only the signing secret; relay owns bot + signing credentials. No credentials
are read, store created or socket opened at import or during config checking.
"""
import argparse
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import ssl
import stat
import threading
import time

from ai_ops.b_contracts import load_b_settings
from ai_ops.job_queue import JobQueue
from ai_ops.path_policy import cli_paths
from ai_ops.runtime_settings import runbook_settings_kwargs
from ai_ops.slack_adapter import SlackAdapter, SlackPolicy
from ai_ops.slack_ingress import SlackEventsApp
from ai_ops.slack_process import check_process, process_lock
from ai_ops.slack_runtime import SlackRuntime, SlackWebConfig
from ai_ops.slack_web import read_private_secret
from ai_ops.tasks import TaskService
from ai_ops.workflow_config import read_config

POLL_SECONDS = 1
RETENTION_INTERVAL = 60


def load_settings(config):
    policy_args = runbook_settings_kwargs(config)
    if not policy_args:
        raise ValueError('slack_runbook_policy_required')
    settings = load_b_settings(config / 'bindings.json', config / 'runtime.json',
                               config / 'metric-queries.json', **policy_args)
    if settings.runtime.llm_enabled or any(b.target.environment != 'practice' for b in settings.bindings.bindings):
        raise ValueError('slack_practice_rules_only')
    policy = SlackPolicy.model_validate(read_config(config / 'slack-policy.json'))
    aliases = {b.alias for b in settings.bindings.bindings}
    if any(not set(grant.targets) <= aliases for grant in policy.grants):
        raise ValueError('slack_policy_target_rejected')
    return settings, policy


def make_ingress(tasks, policy, config):
    if not config.enabled:
        raise ValueError('slack_web_disabled')
    # Deliberately never construct SlackRuntime / transport or read bot_token_file.
    secret = read_private_secret(config.signing_secret_file)
    return SlackEventsApp(SlackAdapter(tasks, policy, secret, transport_mode='slack_web_api'))


class ManagedIngress:
    """Health follows the serving event loop, not an unrelated heartbeat thread."""
    def __init__(self, app, progress):
        self.app, self.progress = app, progress
        self.heartbeat = None

    async def _heartbeat(self):
        while True:
            # PVC fsync must not block request ACKs on the ASGI event loop.
            flush = asyncio.create_task(asyncio.to_thread(self.progress.tick))
            try:
                await asyncio.shield(flush)
            except asyncio.CancelledError:
                # Finish the real write before shutdown removes health. Cancelling
                # a to_thread wrapper alone does not stop its filesystem thread.
                await asyncio.shield(flush)
                raise
            await asyncio.sleep(5)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'lifespan':
            await self.app(scope, receive, send)
            return
        async def lifecycle_send(message):
            if message['type'] == 'lifespan.startup.complete':
                self.progress.tick()
                self.heartbeat = asyncio.create_task(self._heartbeat())
            elif message['type'] == 'lifespan.shutdown.complete':
                await self._stop_heartbeat()
            await send(message)
        async def lifecycle_receive():
            message = await receive()
            if message['type'] == 'lifespan.shutdown':
                await self._stop_heartbeat()
                self.progress.path.unlink(missing_ok=True)
            return message
        try:
            await self.app(scope, lifecycle_receive, lifecycle_send)
        finally:
            await self._stop_heartbeat()

    async def _stop_heartbeat(self):
        if self.heartbeat is not None:
            self.heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await self.heartbeat
            self.heartbeat = None


def server_config(app, certificate, key):
    import uvicorn
    for path in (certificate, key):
        path = Path(path)
        info = path.lstat()
        if not path.is_absolute() or not stat.S_ISREG(info.st_mode):
            raise ValueError('slack_tls_file_rejected')
    key_info = Path(key).stat()
    if key_info.st_uid != os.geteuid() or key_info.st_mode & 0o077 or key_info.st_nlink != 1:
        raise ValueError('slack_tls_key_rejected')
    # Validate certificate/key before acquiring the listener, using TLS >= 1.2.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, key)
    return uvicorn.Config(app, host='0.0.0.0', port=9444, workers=1,
        loop='asyncio', http='h11', ws='none', lifespan='on', interface='asgi3',
        ssl_context_factory=lambda *_: context, proxy_headers=False, forwarded_allow_ips=[],
        access_log=False, server_header=False, timeout_keep_alive=2,
        timeout_graceful_shutdown=10, limit_concurrency=16, backlog=32,
        h11_max_incomplete_event_size=16384,
        log_config={'version':1, 'disable_existing_loggers':False,
                    'handlers':{'null':{'class':'logging.NullHandler'}},
                    'loggers':{name:{'handlers':['null'],'propagate':False} for name in
                               ('uvicorn','uvicorn.error','uvicorn.access','uvicorn.asgi')}})


def relay_loop(runtime, stopped, progress, *, monotonic=time.monotonic):
    next_prune = 0.0
    while not stopped.is_set():
        # Complete the current tick/journal on SIGTERM; no later tick or retry.
        runtime.relay_once(stopped=stopped.is_set)
        if stopped.is_set():
            break
        now = monotonic()
        if now >= next_prune:
            runtime.adapter.prune()
            runtime.tasks.prune()
            next_prune = now + RETENTION_INTERVAL
        progress.tick()
        stopped.wait(POLL_SECONDS)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Slack TLS ingress / existing worker result relay (explicit opt-in)')
    parser.add_argument('action', choices=('check-config','ingress','relay','probe-ingress','probe-relay'))
    parser.add_argument('--config', type=Path, default=Path('/etc/ai-ops'))
    parser.add_argument('--store', type=Path, default=Path('/data/incidents'))
    parser.add_argument('--enable-web', action='store_true')
    parser.add_argument('--tls-cert', default='/run/ai-slack-tls/tls.crt')
    parser.add_argument('--tls-key', default='/run/ai-slack-tls/tls.key')
    args = parser.parse_args(argv)
    try:
        # Probe does not initialize a queue, load settings or touch any credential.
        if args.action.startswith('probe-'):
            args = cli_paths(parser, args, {'config':'directory', 'store':'store'})
            check_process(args.store, args.action.removeprefix('probe-'))
            return 0
        if args.action != 'check-config' and not args.enable_web:
            raise ValueError('slack_explicit_enable_required')
        args = cli_paths(parser, args, {'config':'directory', 'store':'store'})
        web = SlackWebConfig.load(args.config / 'slack-web.json')
        if args.action != 'check-config' and not web.enabled:
            raise ValueError('slack_web_disabled')
        settings, policy = load_settings(args.config)
        if args.action == 'check-config':
            print(json.dumps({'status':'valid', 'enabled':web.enabled,
                'config_hash':settings.config_hash, 'runbook_policy_hash':settings.runbooks.policy_hash,
                'execution_allowed':False, 'connected':False}))
            return 0
        os.umask(0o077)
        # Create private root via the same existing queue implementation after gate.
        queue = JobQueue(args.store)
        with process_lock(args.store, args.action) as progress:
            tasks = TaskService(queue, settings)
            if args.action == 'ingress':
                import uvicorn
                app = ManagedIngress(make_ingress(tasks, policy, web), progress)
                server = uvicorn.Server(server_config(app, args.tls_cert, args.tls_key))
                server.run()
                return 0 if server.started else 1
            stopped = threading.Event()
            previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
            try:
                for sig in previous:
                    signal.signal(sig, lambda *_: stopped.set())
                runtime = SlackRuntime(tasks, policy, web)
                relay_loop(runtime, stopped, progress)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        return 0
    except Exception:
        # No path, request, credential, API response or exception text in logs.
        print('{"status":"slack_process_rejected"}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
