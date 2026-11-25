"""Explicitly enabled TLS ingress / reply relay; no worker or recovery launcher.

With an enabled ``recovery-approval.json`` the ingress also serves signed
approval buttons and the relay handles ``request-approval`` and approval notices
(record/notify only; see ``recovery_approval_wiring``). With its optional
``restart`` section the same command opens the restart (command) approval, which
stops at command_ready: execution stays disabled (``recovery_restart_wiring``).

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
from ai_ops.recovery_approval_wiring import (CONFIG_NAME, RecoveryApprovalConfig, RecoveryApprovalService,
                                             relay_retention)
from ai_ops.recovery_proof_live import live_pr_bridge
from ai_ops.recovery_retention import INTERVAL_SECONDS as RECOVERY_RETENTION_INTERVAL
from ai_ops.slack_ingress import SlackEventsApp, SlackInteractivityApp, SlackRouter
from ai_ops.slack_process import check_process, process_lock
from ai_ops.slack_runtime import SlackRuntime, SlackWebConfig
from ai_ops.slack_web import read_private_secret
from ai_ops.tasks import TaskService
from ai_ops.workflow_config import read_config

POLL_SECONDS = 1
RETENTION_INTERVAL = 60
APPROVAL_SYNC_INTERVAL = 5


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


def make_ingress(tasks, policy, config, recovery=None):
    if not config.enabled:
        raise ValueError('slack_web_disabled')
    # Deliberately never construct SlackRuntime / transport or read bot_token_file.
    secret = read_private_secret(config.signing_secret_file)
    adapter = SlackAdapter(tasks, policy, secret, transport_mode='slack_web_api',event_sources=config.sources())
    events = SlackEventsApp(adapter)
    if recovery is None or not recovery.enabled:
        return events
    # Button presses only record a ledger decision; same signing secret, no bot token.
    service = RecoveryApprovalService(adapter, recovery)
    return SlackRouter(events, SlackInteractivityApp(service.receiver(secret)))


def load_recovery(config_dir):
    """Optional approval wiring config; None (off) when absent or disabled."""
    recovery = RecoveryApprovalConfig.load_optional(config_dir / CONFIG_NAME)
    return recovery if recovery is not None and recovery.enabled else None


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


def recovery_tick(recovery, transport, stopped, state, now):
    """Approval flow: periodic one-step advance (revalidate / WF07 prepare, read-only)
    and ledger->outbox sync, then at most one delivery.

    Failures are swallowed by design (no exception text in logs) and retried on a
    later tick; an ambiguous send is never retried by the outbox itself.
    """
    if now >= state['next_sync']:
        state['next_sync'] = now + APPROVAL_SYNC_INTERVAL
        if not stopped.is_set():
            with suppress(Exception):
                recovery.advance_once()  # before sync: its result is announced this cycle
        with suppress(Exception):
            recovery.sync_once()
    if not stopped.is_set():
        with suppress(Exception):
            recovery.deliver_once(transport)


def retention_tick(retention, state, now):
    """A7-5 recovery retention on its own (10 minute) interval; failures are retried later."""
    if retention is None or now < state['next_retention']:
        return
    state['next_retention'] = now + RECOVERY_RETENTION_INTERVAL
    with suppress(Exception):
        retention.tick()


def relay_loop(runtime, stopped, progress, *, monotonic=time.monotonic, recovery=None, retention=None):
    next_prune = 0.0
    approvals = {'next_sync': 0.0, 'next_retention': 0.0}
    while not stopped.is_set():
        # Complete the current tick/journal on SIGTERM; no later tick or retry.
        runtime.relay_once(stopped=stopped.is_set)
        if stopped.is_set():
            break
        if recovery is not None:
            recovery_tick(recovery, runtime.transport, stopped, approvals, monotonic())
            if stopped.is_set():
                break
        now = monotonic()
        if now >= next_prune:
            runtime.adapter.prune()
            runtime.tasks.prune()
            next_prune = now + RETENTION_INTERVAL
        # After the request prune, so messages of just-expired requests go in this pass.
        if retention is not None and not stopped.is_set():
            retention_tick(retention, approvals, now)
        progress.tick()
        stopped.wait(POLL_SECONDS)


def transition_approval_policy(store, config_dir, previous_path):
    """Explicit operator transition of the approval policies (PR preparation and restart command).

    A ledger refuses to start when the configured policy differs from the one it
    recorded (WF06: no silent rule change for approvals already in flight). This is
    the supported way through: relay and ingress must be stopped (their process
    locks are taken here). For every scope whose ledger exists and whose configured
    policy changed, the previous recovery-approval.json must reproduce the recorded
    policy hash exactly and the new policy version must increase; every scope is
    checked before any is replaced. Open approvals of a replaced scope become
    ``superseded``; the restarted relay announces them. Without an existing ledger
    nothing is created. Output carries hashes and counts only.
    """
    from contextlib import ExitStack
    from ai_ops.recovery_approvals import RecoveryApprovalLedger, _body
    from ai_ops.recovery_commands import RestartApprovalLedger
    from ai_ops.contracts import sha256_json
    new = load_recovery(config_dir)
    previous = RecoveryApprovalConfig.load_optional(previous_path)
    if new is None or previous is None:
        raise ValueError('approval_transition_config_required')
    restart = lambda config: config.restart.approval if config.restart is not None else None
    scopes = (('prepare_proposal', RecoveryApprovalLedger, previous.prepare_proposal, new.prepare_proposal),
              ('execute_command', RestartApprovalLedger, restart(previous), restart(new)))
    queue = JobQueue(store)

    def unused(*args, **kwargs):
        raise ValueError('approval_transition_reads_nothing')
    with ExitStack() as stack:
        for role in ('ingress', 'relay'):
            try:
                stack.enter_context(process_lock(store, role))
            except BlockingIOError:
                raise ValueError('approval_transition_requires_stopped_processes') from None
        planned, report = [], {}
        for scope, kind, old_policy, new_policy in scopes:
            table = kind.TABLE_PREFIX
            with queue.transaction() as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                              (table + '_meta',)).fetchone() is None or new_policy is None:
                    continue  # never created, or this scope is off (its ledger is not opened)
                recorded = db.execute(f'SELECT policy_hash FROM {table}_meta WHERE singleton=1').fetchone()
            if recorded is not None and recorded['policy_hash'] == sha256_json(_body(new_policy)):
                report[scope] = {'status': 'unchanged', 'policy_hash': recorded['policy_hash']}
                continue
            old_hash = sha256_json(_body(old_policy)) if old_policy is not None else None
            if recorded is None or recorded['policy_hash'] != old_hash:
                raise ValueError('approval_transition_previous_policy_mismatch')
            if new_policy.version <= old_policy.version:
                raise ValueError('approval_policy_version_must_increase')
            planned.append((scope, kind(queue, old_policy, load_plan=unused, observe=unused), new_policy, old_hash))
        if not planned:
            return {'status': 'nothing_to_transition', 'scopes': report, 'execution_allowed': False}
        superseded = 0
        for scope, ledger, new_policy, old_hash in planned:
            with queue.transaction() as db:
                active = sum(1 for row in db.execute(f'SELECT document FROM {ledger.TABLE_PREFIX}s').fetchall()
                             if json.loads(row['document'])['state'] in
                             ('awaiting_approval', 'approved', 'revalidating', 'proposal_ready'))
            ledger.replace_policy(new_policy)
            superseded += active
            report[scope] = {'status': 'transitioned', 'superseded': active,
                             'previous_policy_hash': old_hash, 'policy_hash': ledger.policy_hash}
        return {'status': 'transitioned', 'superseded': superseded, 'scopes': report, 'execution_allowed': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Slack TLS ingress / existing worker result relay (explicit opt-in)')
    parser.add_argument('action', choices=('check-config','ingress','relay','probe-ingress','probe-relay',
                                           'transition-approval-policy'))
    parser.add_argument('--previous', type=Path, help='previous recovery-approval.json (policy transition only)')
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
        if args.action == 'transition-approval-policy':
            args = cli_paths(parser, args, {'config':'directory', 'store':'store', 'previous':'input'})
            os.umask(0o077)
            print(json.dumps(transition_approval_policy(args.store, args.config, args.previous)))
            return 0
        if args.action != 'check-config' and not args.enable_web:
            raise ValueError('slack_explicit_enable_required')
        args = cli_paths(parser, args, {'config':'directory', 'store':'store'})
        web = SlackWebConfig.load(args.config / 'slack-web.json')
        if args.action != 'check-config' and not web.enabled:
            raise ValueError('slack_web_disabled')
        settings, policy = load_settings(args.config)
        recovery = load_recovery(args.config)
        granted = {g.user_id for g in policy.grants}
        if recovery is not None and (not set(recovery.prepare_proposal.approvers) <= granted or (
                recovery.restart is not None and not set(recovery.restart.approval.approvers) <= granted)):
            raise ValueError('recovery_approvers_not_granted')
        if args.action == 'check-config':
            print(json.dumps({'status':'valid', 'enabled':web.enabled,
                'config_hash':settings.config_hash, 'runbook_policy_hash':settings.runbooks.policy_hash,
                'recovery_approval':recovery is not None,
                'recovery_observe':recovery is not None and recovery.observe is not None,
                'recovery_prepare_configured':recovery is not None and recovery.prepare is not None,
                'recovery_restart_configured':recovery is not None and recovery.restart is not None,
                'execution_allowed':False, 'connected':False}))
            return 0
        os.umask(0o077)
        # Create private root via the same existing queue implementation after gate.
        queue = JobQueue(args.store)
        with process_lock(args.store, args.action) as progress:
            tasks = TaskService(queue, settings)
            if args.action == 'ingress':
                import uvicorn
                app = ManagedIngress(make_ingress(tasks, policy, web, recovery), progress)
                server = uvicorn.Server(server_config(app, args.tls_cert, args.tls_key))
                server.run()
                return 0 if server.started else 1
            stopped = threading.Event()
            previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
            try:
                for sig in previous:
                    signal.signal(sig, lambda *_: stopped.set())
                runtime = SlackRuntime(tasks, policy, web)
                service = None
                if recovery is not None:
                    # WF07 live preparation only in the relay and only with a `prepare` section.
                    bridge = live_pr_bridge(recovery, queue) if recovery.prepare is not None else None
                    service = RecoveryApprovalService(runtime.adapter, recovery, pr_bridge=bridge)
                    runtime.adapter.recovery_approvals = service
                relay_loop(runtime, stopped, progress, recovery=service,
                           retention=relay_retention(runtime.adapter, service))
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
