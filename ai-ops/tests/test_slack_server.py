"""Local TLS + synthetic Slack only; never loads user credentials or contacts Slack."""
import asyncio
from contextlib import redirect_stdout
import hashlib
import hmac
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ai_ops import slack_server
from ai_ops.b_contracts import load_b_settings
from ai_ops.contracts import canonical_json_bytes
from ai_ops.runtime_settings import runbook_settings_kwargs
from ai_ops.slack_process import check_process, process_lock
from ai_ops.slack_runtime import SlackRuntime, SlackWebConfig
from ai_ops.tasks import TaskService
from test_slack_adapter import SlackFixture
from test_slack_web import Wire, TOKEN, SIGNING, invoke
from test_job_queue import ROOT, API, Clock, collect_snapshot


class ServerTest(SlackFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config = self.fixture.root / 'config'
        self.config.mkdir()
        for source, name in [('targets.practice.example.json','bindings.json'),
                             ('b-runtime.example.json','runtime.json'),
                             ('metric-queries.json','metric-queries.json'),
                             ('runbook-policy.local.example.json','runbook-policy.json')]:
            shutil.copyfile(ROOT / 'config' / source, self.config / name)
        self.write('slack-policy.json', self.policy.model_dump())
        self.secret = self.fixture.root / 'signing'
        self.token = self.fixture.root / 'token'
        for path, raw in ((self.secret,SIGNING),(self.token,TOKEN)):
            path.write_bytes(raw); path.chmod(0o600)
        self.web = SlackWebConfig(enabled=True, bot_token_file=str(self.token), signing_secret_file=str(self.secret))
        self.write('slack-web.json', self.web.model_dump())
        self.settings, _ = slack_server.load_settings(self.config)
        self.fixture.settings = self.settings
        api = API(self.settings, Clock()); api.docs['deployment']['spec']['replicas'] = 1
        self.fixture.snapshot = collect_snapshot(self.settings, self.fixture.target.target, transport=api, clock=Clock())
        self.tasks = TaskService(self.queue, self.settings)
        self.wire = Wire(self.policy)

    def write(self, name, value):
        (self.config / name).write_text(json.dumps(value))

    def args(self, action, *extra):
        return [action, '--config', str(self.config), '--store', str(self.queue.root), *extra]

    def signed_web(self, event=None):
        raw = canonical_json_bytes(event or self.event)
        stamp = str(int(self.fixture.time))
        sig = 'v0=' + hmac.new(SIGNING,b'v0:'+stamp.encode()+b':'+raw,hashlib.sha256).hexdigest()
        return {'Content-Type':'application/json','X-Slack-Request-Timestamp':stamp,'X-Slack-Signature':sig}, raw

    def runtime(self):
        return SlackRuntime(self.tasks,self.policy,self.web,connection_factory=self.wire.connect)

    def test_default_gate_and_disabled_config_read_no_secret_or_store(self):
        with (patch('ai_ops.slack_server.JobQueue',side_effect=AssertionError('queue')),
             patch('ai_ops.slack_web.read_private_secret',side_effect=AssertionError('secret')),
             patch('socket.create_connection',side_effect=AssertionError('network')),
             redirect_stdout(io.StringIO())):
            self.assertEqual(slack_server.main(self.args('ingress')),1)
            self.write('slack-web.json',{**self.web.model_dump(),'enabled':False})
            self.assertEqual(slack_server.main(self.args('relay','--enable-web')),1)

    def test_check_config_has_shared_hash_and_no_side_effects(self):
        output=io.StringIO()
        with (patch('ai_ops.slack_server.JobQueue',side_effect=AssertionError('queue')),
             patch('ai_ops.slack_server.read_private_secret',side_effect=AssertionError('secret')),
             patch('socket.create_connection',side_effect=AssertionError('network')), redirect_stdout(output)):
            self.assertEqual(slack_server.main(self.args('check-config')),0)
        value=json.loads(output.getvalue())
        self.assertEqual(value['config_hash'],self.settings.config_hash)
        self.assertFalse(value['connected']); self.assertFalse(value['execution_allowed'])
        self.assertFalse((self.queue.root/'slack-process').exists())

    def test_missing_or_invalid_runbook_policy_rejected_without_fallback(self):
        path=self.config/'runbook-policy.json';path.unlink()
        with self.assertRaisesRegex(ValueError,'required'):slack_server.load_settings(self.config)
        self.assertEqual(runbook_settings_kwargs(self.config),{})
        path.symlink_to(self.config/'missing.json')
        self.assertIn('runbook_policy',runbook_settings_kwargs(self.config))
        with self.assertRaises(OSError):slack_server.load_settings(self.config)

    def test_ingress_never_reads_bot_token_or_calls_network(self):
        self.token.unlink()
        with patch('socket.create_connection',side_effect=AssertionError('network')):
            app=slack_server.make_ingress(self.tasks,self.policy,self.web)
            status,receipt=asyncio.run(invoke(app,*self.signed_web()))
        self.assertEqual(status,200); self.assertTrue(receipt['accepted'])

    def test_signed_request_existing_worker_citations_reply_and_restart(self):
        with (patch.object(socket.socket,'connect',side_effect=AssertionError('external')),
             patch.object(self.queue.engine,'load_api_key',side_effect=AssertionError('model'))):
            app=slack_server.make_ingress(self.tasks,self.policy,self.web)
            status,receipt=asyncio.run(invoke(app,*self.signed_web()))
            self.assertEqual(status,200)
            runtime=self.runtime();runtime.relay_once()
            self.assertEqual(self.fixture.worker().run_once()['state'],'partial')
            runtime.relay_once()
            self.assertEqual(len(self.wire.posts()),1)
            message=self.wire.posts()[0]['body']
            self.assertEqual(message['channel'],'CLOCAL01');self.assertEqual(message['thread_ts'],'1000.000001')
            self.assertIn('런북',message['text'])
            with runtime.adapter._db() as db:
                row=db.execute('SELECT job_id FROM slack_requests WHERE request_id=?',(receipt['request_id'],)).fetchone()
            context=self.tasks.result(row['job_id'])['runbook_context']
            self.assertEqual(context['status'],'available')
            for citation in context['citations']:
                self.assertIn('document_id',citation);self.assertIn('content_sha256',citation)
                self.assertIn('document_version',citation);self.assertIn('section',citation)
            self.assertFalse(context['execution_allowed'])
            restarted=self.runtime();restarted.relay_once()
            asyncio.run(invoke(app,*self.signed_web()))
            restarted.relay_once()
            self.assertEqual(len(self.wire.posts()),1)

    def test_ambiguous_post_is_not_resent_after_restart(self):
        app=slack_server.make_ingress(self.tasks,self.policy,self.web)
        asyncio.run(invoke(app,*self.signed_web()))
        runtime=self.runtime();runtime.relay_once();self.fixture.worker().run_once()
        self.wire.outcomes.append(OSError('synthetic lost reply'))
        self.assertEqual(runtime.relay_once()['reply']['state'],'needs_review')
        self.runtime().relay_once()
        self.assertEqual(len(self.wire.posts()),1)

    def test_stop_during_identity_verification_does_not_admit_or_send(self):
        runtime=self.runtime();stopped=threading.Event()
        with patch.object(runtime,'prepare',side_effect=stopped.set):
            self.assertEqual(runtime.relay_once(stopped=stopped.is_set)['state'],'stopped')
        self.assertEqual(self.wire.posts(),[])

    def test_relay_loop_retention_and_wait_and_stop(self):
        runtime=Mock();stopped=Mock();stopped.is_set.side_effect=[False,False,True]
        progress=Mock()
        slack_server.relay_loop(runtime,stopped,progress,monotonic=lambda:100)
        runtime.relay_once.assert_called_once();runtime.adapter.prune.assert_called_once()
        runtime.tasks.prune.assert_called_once();progress.tick.assert_called_once()
        stopped.wait.assert_called_once_with(1)

    def test_relay_failure_never_publishes_fresh_health(self):
        runtime=Mock();runtime.relay_once.side_effect=OSError('private-value')
        progress=Mock()
        with self.assertRaises(OSError):slack_server.relay_loop(runtime,threading.Event(),progress)
        progress.tick.assert_not_called()

    def test_progress_probe_requires_lock_freshness_and_private_files(self):
        with process_lock(self.queue.root,'relay') as progress:
            with self.assertRaises(OSError):check_process(self.queue.root,'relay')
            progress.tick();check_process(self.queue.root,'relay')
            with self.assertRaises(BlockingIOError):
                with process_lock(self.queue.root,'relay'):pass
            check_process(self.queue.root,'relay')  # contending start did not remove health
            state=json.loads(progress.path.read_text());state['monotonic']-=31
            progress.path.write_text(json.dumps(state))
            with self.assertRaises(ValueError):check_process(self.queue.root,'relay')
            progress.tick();progress.path.chmod(0o644)
            with self.assertRaises(ValueError):check_process(self.queue.root,'relay')
        with self.assertRaises(OSError):check_process(self.queue.root,'relay')

    def test_probe_creates_nothing_and_reads_no_config_or_secret(self):
        missing=self.fixture.root/'absent'
        with (patch('ai_ops.slack_server.load_settings',side_effect=AssertionError('settings')),
             redirect_stdout(io.StringIO())):
            self.assertEqual(slack_server.main(['probe-relay','--config',str(self.config),'--store',str(missing)]),1)
        self.assertFalse(missing.exists())

    def test_worker_and_ci_relay_load_shared_policy_keep_maintenance(self):
        from ai_ops import worker_runtime, ci_relay
        (self.config/'normal-ci-binding.json').write_text('{}')
        worker=Mock();worker.serve.side_effect=lambda **kwargs:(kwargs['progress'](),kwargs['maintenance']())
        inbox=Mock();retention=Mock()
        with (patch.object(worker_runtime,'check_local'), patch.object(worker_runtime,'Worker',return_value=worker) as factory,
             patch.object(worker_runtime,'WorkerTelemetry'), patch.object(worker_runtime,'RetentionSchedule',return_value=retention),
             patch.object(worker_runtime.NormalCIBinding,'model_validate_json',return_value=Mock()),
             patch.object(worker_runtime,'NormalCIInbox',return_value=inbox)):
            self.assertEqual(worker_runtime.main(['--config',str(self.config),'--store',str(self.queue.root),'--retention']),0)
            self.assertEqual(factory.call_args.args[1].config_hash,self.settings.config_hash)
            inbox.prune.assert_called_once();inbox.process_one.assert_called_once()
            retention.tick.assert_called_once()
        relay=Mock();relay.scan_once.return_value={'rejected':0}
        with patch.object(ci_relay,'CIRelay',return_value=relay) as factory,redirect_stdout(io.StringIO()):
            self.assertEqual(ci_relay.main(['--config',str(self.config),'--store',str(self.queue.root)]),0)
            self.assertEqual(factory.call_args.args[1].config_hash,self.settings.config_hash)

    def test_errors_do_not_expose_secrets_or_exception_text(self):
        output=io.StringIO()
        with (patch('ai_ops.slack_server.load_settings',side_effect=RuntimeError('private-value')),
             redirect_stdout(output)):
            self.assertEqual(slack_server.main(self.args('relay','--enable-web')),1)
        self.assertEqual(output.getvalue(),'{"status":"slack_process_rejected"}\n')

    def test_slow_progress_flush_does_not_block_request_event_loop(self):
        started=threading.Event();release=threading.Event();finished=threading.Event()
        def tick():
            started.set();release.wait(2);finished.set()
        progress=Mock();progress.tick.side_effect=tick
        managed=slack_server.ManagedIngress(None,progress)
        async def check():
            managed.heartbeat=asyncio.create_task(managed._heartbeat())
            try:
                while not started.is_set():await asyncio.sleep(.01)
                self.assertFalse(finished.is_set())
            finally:
                release.set()
                await managed._stop_heartbeat()
        asyncio.run(check())
        self.assertTrue(finished.is_set())

    def test_shutdown_waits_for_actual_progress_write_before_removing_health(self):
        from ai_ops.slack_process import Progress
        started=threading.Event();release=threading.Event()
        directory=self.fixture.root/'progress';directory.mkdir()
        progress=Progress(directory,'ingress')
        write=progress.tick
        def delayed():
            started.set();release.wait(2);write()
        progress.tick=delayed
        managed=slack_server.ManagedIngress(None,progress)
        async def check():
            managed.heartbeat=asyncio.create_task(managed._heartbeat())
            stop=None
            try:
                while not started.is_set():await asyncio.sleep(.01)
                stop=asyncio.create_task(managed._stop_heartbeat())
                await asyncio.sleep(.01)
                self.assertFalse(stop.done())
            finally:
                release.set()
                if stop is not None:await stop
                else:await managed._stop_heartbeat()
            self.assertTrue(progress.path.exists())
            progress.path.unlink()
            await asyncio.sleep(.02)
            self.assertFalse(progress.path.exists())
        asyncio.run(check())

    def test_real_relay_entrypoint_sigterm_preserves_journal(self):
        code = """
import sys
from unittest.mock import patch
from ai_ops.slack_runtime import SlackRuntime
from ai_ops.slack_server import main
from test_slack_web import Wire

def factory(tasks, policy, config):
    wire=Wire(policy)
    return SlackRuntime(tasks,policy,config,connection_factory=wire.connect)
with patch('ai_ops.slack_server.SlackRuntime',factory), patch('socket.create_connection',side_effect=AssertionError('network forbidden')):
    raise SystemExit(main(sys.argv[1:]))
"""
        # unittest discovery adjusts only the parent sys.path. Explicitly pass
        # the fixture directory to the child; CI PYTHONPATH contains only src.
        environment = {**os.environ, 'PYTHONPATH': os.pathsep.join(
            [str(ROOT / 'tests'), os.environ.get('PYTHONPATH', '')])}
        with subprocess.Popen([sys.executable,'-c',code,*self.args('relay','--enable-web')],
                              env=environment, stdout=subprocess.PIPE,stderr=subprocess.PIPE) as child:
            try:
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    if child.poll() is not None:self.fail('relay exited: '+child.communicate()[1].decode())
                    try:check_process(self.queue.root,'relay');break
                    except (OSError,ValueError):time.sleep(.05)
                else:self.fail('relay did not start')
                child.send_signal(signal.SIGTERM)
                stdout,stderr=child.communicate(timeout=5)
                self.assertEqual(child.returncode,0,stderr.decode())
                self.assertEqual(stdout,b'');self.assertNotIn(b'Traceback',stderr)
            finally:
                if child.poll() is None:child.kill();child.wait()
        self.assertTrue((self.queue.root/'slack-web/inbox.sqlite3').is_file())
        with self.assertRaises(OSError):check_process(self.queue.root,'relay')

    def test_target_allowlist_and_rules_only_fail_closed(self):
        from copy import deepcopy
        for change in ('target','model','production'):
            with self.subTest(change=change):
                policy=self.policy.model_dump()
                runtime=json.loads((ROOT/'config/b-runtime.example.json').read_text())
                bindings=json.loads((ROOT/'config/targets.practice.example.json').read_text())
                if change=='target':policy['grants'][0]['targets']=['foreign']
                if change=='model':runtime['llm_enabled']=True
                if change=='production':bindings['bindings'][0]['target']['environment']='production'
                self.write('slack-policy.json',policy);self.write('runtime.json',runtime);self.write('bindings.json',bindings)
                with self.assertRaises(ValueError):slack_server.load_settings(self.config)

    def test_real_tls_ingress_challenge_signature_and_sigterm(self):
        # Independent child process, synthetic key/cert and ONLY loopback HTTP.
        certificate=self.fixture.root/'cert.pem';key=self.fixture.root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1',
            '-keyout',str(key),'-out',str(certificate)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        key.chmod(0o600)
        app=slack_server.make_ingress(self.tasks,self.policy,self.web)
        config=slack_server.server_config(app,str(certificate),str(key))
        self.assertFalse(config.proxy_headers);self.assertFalse(config.access_log)
        self.assertEqual(config.workers,1);self.assertEqual(config.http,'h11')
        self.assertEqual(config.timeout_graceful_shutdown,10)
        # Let the OS assign the port before passing socket ownership to child.
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));listener.listen(16)
            port=listener.getsockname()[1]
            code='''
import socket,sys,uvicorn
from pathlib import Path
from ai_ops.slack_server import *
config_dir=Path(sys.argv[1]);store=Path(sys.argv[2])
settings,policy=load_settings(config_dir)
web=SlackWebConfig.load(config_dir/'slack-web.json')
tasks=TaskService(JobQueue(store),settings)
with process_lock(store,'ingress') as progress:
    app=ManagedIngress(make_ingress(tasks,policy,web),progress)
    config=server_config(app,sys.argv[3],sys.argv[4])
    uvicorn.Server(config).run(sockets=[socket.socket(fileno=int(sys.argv[5]))])
'''
            with (subprocess.Popen([sys.executable,'-c',code,str(self.config),str(self.queue.root),str(certificate),str(key),str(listener.fileno())],
                    pass_fds=(listener.fileno(),),stdout=subprocess.PIPE,stderr=subprocess.PIPE) as child):
                try:
                    deadline=time.monotonic()+10
                    while time.monotonic()<deadline:
                        if child.poll() is not None:
                            self.fail('TLS child exited: '+child.communicate()[1].decode())
                        try:check_process(self.queue.root,'ingress');break
                        except (OSError,ValueError):time.sleep(.05)
                    else:self.fail('TLS ingress did not start')
                    context=ssl.create_default_context(cafile=str(certificate))
                    self.fixture.time=time.time()
                    event={'type':'url_verification','challenge':'local-only-challenge'}
                    headers,body=self.signed_web(event)
                    client=http.client.HTTPSConnection('127.0.0.1',port,context=context,timeout=4)
                    try:
                        before=time.monotonic();client.request('POST','/slack/events',body,headers)
                        response=client.getresponse();raw=response.read()
                        self.assertEqual(response.status,200,raw)
                        self.assertLess(time.monotonic()-before,2.5)
                        self.assertEqual(json.loads(raw)['challenge'],'local-only-challenge')
                        event_headers,event_body=self.signed_web()
                        client.request('POST','/slack/events',event_body,event_headers)
                        response=client.getresponse();receipt=json.loads(response.read())
                        self.assertEqual(response.status,200);self.assertTrue(receipt['accepted'])
                        headers['X-Slack-Signature']='v0'+'0'*64
                        client.request('POST','/slack/events',body,headers)
                        response=client.getresponse();response.read();self.assertEqual(response.status,403)
                    finally:client.close()
                    child.send_signal(signal.SIGTERM)
                    stdout,stderr=child.communicate(timeout=10)
                    self.assertEqual(child.returncode,-signal.SIGTERM,stderr.decode())
                    self.assertNotIn(b'Traceback',stderr);self.assertEqual(stdout,b'')
                    self.assertNotIn(SIGNING,stderr);self.assertNotIn(TOKEN,stderr)
                finally:
                    if child.poll() is None:child.kill();child.wait()
        with self.assertRaises(OSError):check_process(self.queue.root,'ingress')
        runtime=self.runtime();runtime.relay_once();self.fixture.worker().run_once();runtime.relay_once()
        self.assertEqual(len(self.wire.posts()),1)
        self.assertIn('런북',self.wire.posts()[0]['body']['text'])


if __name__=='__main__':unittest.main()
