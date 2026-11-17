"""Only injected HTTP doubles and synthetic ASGI traffic; network/model/process denied."""
import asyncio
from copy import deepcopy
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import ssl
import threading
import time
import unittest
from unittest.mock import patch

from ai_ops.slack_adapter import SlackAdapter
from ai_ops.slack_ingress import SlackEventsApp
from ai_ops.slack_runtime import SlackRuntime, SlackWebConfig
from ai_ops.slack_web import SlackWebTransport, FLAGS, read_private_secret
from test_slack_adapter import SlackFixture, FakeSlack, SECRET


TOKEN = b'xoxb-fake-local-token-never-a-real-credential'
SIGNING = b'1234567890abcdef1234567890abcdef'


class Response:
    def __init__(self, status=200, body=None, headers=None, raw=None):
        self.status = status
        self.raw = json.dumps(body).encode() if raw is None else raw
        self.headers = {'Content-Length':str(len(self.raw)),**(headers or {})}
    def read(self, limit): return self.raw[:limit]
    def getheader(self, key, default=None): return self.headers.get(key,default)


class Wire:
    def __init__(self, policy):
        self.policy = policy
        self.calls, self.connections, self.outcomes = [], [], []
        self.closed = 0
    def connect(self, host, **kwargs):
        self.connections.append((host, kwargs))
        wire = self
        class Connection:
            def request(self, method, path, body=None, headers=None):
                self.call = {'method':method,'path':path,'body':json.loads(body) if body else None,'headers':headers}
                wire.calls.append(self.call)
            def getresponse(self):
                call = self.call
                if call['path'] == '/api/auth.test':
                    return Response(body={'ok':True,'team_id':wire.policy.team_id,
                        'user_id':wire.policy.bot_user_id,'bot_id':'BBOT001'})
                if call['path'].startswith('/api/bots.info?'):
                    return Response(body={'ok':True,'bot':{'id':'BBOT001','app_id':wire.policy.app_id,
                        'user_id':wire.policy.bot_user_id,'deleted':False}})
                if wire.outcomes:
                    outcome = wire.outcomes.pop(0)
                    if isinstance(outcome,Exception): raise outcome
                    return outcome
                p = call['body']
                return Response(body={'ok':True,'channel':p['channel'],'ts':'1001.000001','message':{
                    'user':wire.policy.bot_user_id,'bot_id':'BBOT001','ts':'1001.000001',
                    'thread_ts':p['thread_ts'],'text':p['text']}})
            def close(self): wire.closed += 1
        return Connection()
    def posts(self): return [c for c in self.calls if c['path']=='/api/chat.postMessage']


async def invoke(app, headers, body, *, scheme='https', chunks=None):
    events = list(chunks or [{'type':'http.request','body':body}])
    sent = []
    async def receive(): return events.pop(0)
    async def send(event): sent.append(event)
    scope = {'type':'http','scheme':scheme,'method':'POST','path':'/slack/events',
        'headers':[(k.encode(),v.encode()) for k,v in headers.items()]+[(b'content-length',str(len(body)).encode())]}
    await app(scope,receive,send)
    return sent[0]['status'],json.loads(sent[1]['body'])


class WebFixture(SlackFixture):
    def setUp(self):
        super().setUp()
        self.addCleanup(patch.stopall)
        patch.object(socket.socket,'connect',side_effect=AssertionError('external_network_forbidden')).start()
        patch('subprocess.Popen',side_effect=AssertionError('external_process_forbidden')).start()
        patch.object(self.queue.engine,'load_api_key',side_effect=AssertionError('model_forbidden')).start()
        self.token_file = self.queue.root/'fake-bot-token'
        self.secret_file = self.queue.root/'fake-signing-secret'
        for p,data in ((self.token_file,TOKEN),(self.secret_file,SIGNING)):
            p.write_bytes(data);p.chmod(0o600)
        self.now = 10.0
        self.wire = Wire(self.policy)
        self.web = self.make_web()
        self.adapter = SlackAdapter(self.tasks,self.policy,SECRET,transport_mode='slack_web_api')
    def make_web(self):
        return SlackWebTransport(self.policy,self.token_file,enabled=True,
            connection_factory=self.wire.connect,monotonic=lambda:self.now)
    def verified(self):
        self.web.verify_identity()
        return self.web
    def payload(self):
        return {'channel':'CLOCAL01','thread_ts':'1000.000001','text':'로컬 대역 결과',**FLAGS}


class WebTransportTest(WebFixture,unittest.TestCase):
    def test_disabled_does_not_read_secret_or_connect(self):
        with patch('ai_ops.slack_web.read_private_secret',side_effect=AssertionError('read')):
            with self.assertRaisesRegex(ValueError,'disabled'):
                SlackWebTransport(self.policy,'/nonexistent',enabled=False)
        self.assertEqual(self.wire.calls,[])

    def test_private_secret_rejects_permissions_links_oversize_and_control_characters(self):
        self.assertEqual(read_private_secret(self.token_file,token=True),TOKEN)
        self.token_file.chmod(0o640)
        with self.assertRaisesRegex(ValueError,'credential_rejected'): read_private_secret(self.token_file,token=True)
        self.token_file.chmod(0o600)
        link=self.queue.root/'link';link.symlink_to(self.token_file)
        with self.assertRaises(ValueError):read_private_secret(link,token=True)
        hard=self.queue.root/'hard';os.link(self.token_file,hard)
        with self.assertRaises(ValueError):read_private_secret(self.token_file,token=True)
        hard.unlink()
        for raw in (TOKEN+b'\r\n',TOKEN+b'\nInjected: value',b'a'*4097,b'xoxp-user-token'):
            self.token_file.write_bytes(raw)
            with self.assertRaises(ValueError) as error:read_private_secret(self.token_file,token=True)
            self.assertNotIn('Injected',str(error.exception))
        with self.assertRaises(ValueError):read_private_secret(self.queue.root,token=True)

    def test_explicit_identity_verification_fixed_tls_endpoint_and_close(self):
        self.assertFalse(self.web.verified_for(self.policy));self.assertEqual(self.wire.calls,[])
        self.verified()
        self.assertTrue(self.web.verified_for(self.policy))
        self.assertEqual([c['path'] for c in self.wire.calls],['/api/auth.test','/api/bots.info?bot=BBOT001'])
        for host,opts in self.wire.connections:
            self.assertEqual((host,opts['port'],opts['timeout']),('slack.com',443,3.0))
            self.assertEqual(opts['context'].verify_mode,ssl.CERT_REQUIRED)
            self.assertTrue(opts['context'].check_hostname)
        self.assertEqual(self.wire.closed,2)
        self.assertNotIn(TOKEN.decode(),repr(self.web))

    def test_identity_mismatch_and_expired_verification_prevent_send(self):
        self.verified();different=self.policy.model_copy(update={'app_id':'AOTHER01'})
        self.assertFalse(self.web.verified_for(different))
        for now in (9,310):
            self.now=now
            with self.assertRaises(ValueError):self.web.post_message(self.payload())
        self.assertEqual(self.wire.posts(),[])
        with patch.object(self.web,'_request',return_value={'status':200,'body':{'ok':True,'team_id':'TOTHER'}}):
            with self.assertRaises(ValueError):self.web.verify_identity()
        self.assertFalse(self.web.verified_for(self.policy))

    def test_wrong_app_and_deleted_bot_rejected(self):
        for bot in ({'app_id':'AOTHER01'}, {'deleted':True}, {'user_id':'UOTHER01'}):
            responses=[{'status':200,'body':{'ok':True,'team_id':self.policy.team_id,
                'user_id':self.policy.bot_user_id,'bot_id':'BBOT001'}},
                {'status':200,'body':{'ok':True,'bot':{'id':'BBOT001','app_id':self.policy.app_id,
                'user_id':self.policy.bot_user_id,'deleted':False,**bot}}}]
            with patch.object(self.web,'_request',side_effect=responses),self.assertRaises(ValueError):
                self.web.verify_identity()

    def test_payload_destination_flags_and_extra_urls_rejected_before_post(self):
        self.verified()
        for delta in ({'channel':'COTHER01'},{'thread_ts':'abc'},{'text':'x'*4001},
                {'unfurl_links':True},{'mrkdwn':0},{'response_url':'https://example.com'}):
            with self.subTest(delta=delta),self.assertRaises(ValueError):self.web.post_message({**self.payload(),**delta})
        self.assertEqual(self.wire.posts(),[])

    def test_success_returns_only_verified_receipt_fields(self):
        self.verified();result=self.web.post_message(self.payload())
        self.assertTrue(result['body']['ok']);self.assertEqual(len(self.wire.posts()),1)
        self.assertEqual(result['body']['message']['bot_id'],'BBOT001')
        self.assertEqual(self.wire.closed,3)

    def test_official_bot_id_only_receipt_is_supported_without_invented_user(self):
        self.verified();p=self.payload()
        body={'ok':True,'channel':p['channel'],'ts':'1001.000001','message':{
            'bot_id':'BBOT001','ts':'1001.000001','thread_ts':p['thread_ts'],'text':p['text']}}
        self.wire.outcomes=[Response(body=body)]
        result=self.web.post_message(p)
        self.assertEqual(result['status'],200)
        self.assertNotIn('user',result['body']['message'])
        del body['message']['bot_id'];self.wire.outcomes=[Response(body=body)]
        self.assertEqual(self.web.post_message(p),{'status':503})

    def test_mismatched_reply_bot_and_content_are_ambiguous(self):
        self.verified()
        for delta in ({'user':'UOTHER01'},{'thread_ts':'1000.000099'},{'text':'wrong'},{'bot_id':'BOTHER01'}):
            p=self.payload()
            self.wire.outcomes=[Response(body={'ok':True,'channel':p['channel'],'ts':'1001.000001',
                'message':{'ts':'1001.000001','thread_ts':p['thread_ts'],'text':p['text'],
                'user':self.policy.bot_user_id,'bot_id':'BBOT001',**delta}})]
            self.assertEqual(self.web.post_message(p),{'status':503})

    def test_slack_partial_unknown_failures_not_terminal_or_retried(self):
        self.verified()
        for error in ('internal_error','fatal_error','new_unknown_error'):
            self.wire.outcomes=[Response(body={'ok':False,'error':error,'secret':'private-value'})]
            self.assertEqual(self.web.post_message(self.payload()),{'status':503})
        self.wire.outcomes=[Response(body={'ok':False,'error':'not_in_channel'})]
        self.assertEqual(self.web.post_message(self.payload()),{'status':200,'body':{'ok':False}})

    def test_redirect_timeout_malformed_truncated_large_responses_are_unknown(self):
        self.verified()
        for response in (Response(302,headers={'Location':'https://attacker.invalid'}),Response(503),
                TimeoutError('private-token'),Response(raw=b'{'),Response(raw=b'x'*65537),
                Response(raw=b'{"ok":false}',headers={'Content-Length':'100'}),Response(raw=b'{"ok":true,"ok":false}')):
            self.wire.outcomes=[response]
            before=len(self.wire.calls)
            self.assertEqual(self.web.post_message(self.payload()),{'status':503})
            self.assertEqual(len(self.wire.calls),before+1)
        self.assertEqual(self.wire.closed,len(self.wire.calls))

    def test_rate_limit_requires_valid_delay_without_inline_sleep(self):
        self.verified()
        for delay in ('0','301','abc','1, 2','9999'):
            self.wire.outcomes=[Response(429,headers={'Retry-After':delay})]
            self.assertEqual(self.web.post_message(self.payload()),{'status':503})
        self.wire.outcomes=[Response(429,headers={'Retry-After':'60'})]
        self.assertEqual(self.web.post_message(self.payload()),{'status':429,'retry_after':60})

    def test_unverified_or_fixture_transport_cannot_send_web_ledger(self):
        request_id,_=self.complete()
        for transport in (self.web,FakeSlack(self.policy),None):
            with self.assertRaises(ValueError):self.adapter.reply(request_id,transport)
        self.assertEqual(self.wire.posts(),[])

    def test_ambiguous_result_never_reposts_even_after_restart_and_reconcile(self):
        request_id,_=self.complete();self.verified()
        self.wire.outcomes=[TimeoutError('private-response')]
        self.assertEqual(self.adapter.reply(request_id,self.web)['state'],'needs_review')
        restarted=SlackAdapter(self.tasks,self.policy,SECRET,transport_mode='slack_web_api')
        self.assertEqual(restarted.reply(request_id,self.web)['state'],'needs_review')
        self.assertEqual(restarted.reconcile_reply(request_id,self.web)['state'],'needs_review')
        self.assertEqual(len(self.wire.posts()),1)

    def test_fixture_and_web_request_ids_jobs_delivery_ledgers_are_separate(self):
        local=SlackAdapter(self.tasks,self.policy,SECRET)
        local_id=local.receive(*self.signed())['request_id'];local_job=local.drain_one()['task_id']
        self.fixture.worker().run_once();local.reply(local_id,FakeSlack(self.policy))
        web_id,web_job=self.complete()
        self.assertNotEqual(local_id,web_id);self.assertNotEqual(local_job,web_job)
        self.assertNotEqual(local.directory,self.adapter.directory)
        self.assertEqual(self.adapter.reply(web_id,self.verified())['state'],'delivered')
        self.assertEqual(len(self.wire.posts()),1)

    def test_persisted_429_retry_uses_original_adapter_cap_and_due_time(self):
        request_id,_=self.complete();self.verified()
        self.wire.outcomes=[Response(429,headers={'Retry-After':'10'})]*3
        for attempt in range(1,4):
            result=self.adapter.reply(request_id,self.web)
            self.assertEqual(result['attempts'],attempt)
            self.assertEqual(self.adapter.reply(request_id,self.web)['attempts'],attempt)
            self.fixture.time+=10
        self.assertEqual(result['state'],'failed');self.assertEqual(len(self.wire.posts()),3)


class IngressRuntimeTest(WebFixture,unittest.TestCase):
    def test_asgi_ack_persists_only_then_existing_worker_and_web_reply(self):
        config=SlackWebConfig(enabled=True,bot_token_file=str(self.token_file),signing_secret_file=str(self.secret_file))
        runtime=SlackRuntime(self.tasks,self.policy,config,connection_factory=self.wire.connect,monotonic=lambda:self.now)
        headers,body=self.signed()
        headers['X-Slack-Signature']='v0='+hmac.new(SIGNING,b'v0:'+headers['X-Slack-Request-Timestamp'].encode()+b':'+body,hashlib.sha256).hexdigest()
        started=time.monotonic();status,receipt=asyncio.run(invoke(runtime.app,headers,body))
        self.assertEqual(status,200);self.assertLess(time.monotonic()-started,2.5)
        self.assertFalse(receipt['queue_accepted']);self.assertEqual(self.wire.calls,[])
        runtime.prepare()
        self.assertEqual(runtime.relay_once()['reply']['state'],'idle')
        self.fixture.worker().run_once()
        result=runtime.relay_once()['reply']
        self.assertEqual(result['state'],'delivered');self.assertEqual(result['transport'],'slack_web_api')
        self.assertFalse(result['execution_allowed']);self.assertFalse(result['human_read_confirmed'])
        runtime=SlackRuntime(self.tasks,self.policy,config,connection_factory=self.wire.connect,monotonic=lambda:self.now)
        status,duplicate=asyncio.run(invoke(runtime.app,headers,body))
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(runtime.relay_once()['reply']['state'],'idle')
        self.assertEqual(len(self.wire.posts()),1)
        self.assertNotIn('private-secret-value',self.wire.posts()[0]['body']['text'])
        self.assertIs(runtime.tasks,self.tasks)

    def test_existing_runbook_citations_survive_actual_api_payload_format(self):
        from ai_ops.workflow_config import load_local_workflow
        from ai_ops.tasks import TaskService
        from test_job_queue import ROOT, Clock, API, collect_snapshot
        settings=load_local_workflow(ROOT/'config/request-workflow.local.example.json').settings
        self.fixture.settings=settings
        clock=Clock();api=API(settings,clock);api.docs['deployment']['spec']['replicas']=1
        self.fixture.snapshot=collect_snapshot(settings,self.fixture.target.target,transport=api,clock=clock)
        tasks=TaskService(self.queue,settings)
        adapter=SlackAdapter(tasks,self.policy,SECRET,transport_mode='slack_web_api')
        receipt=adapter.receive(*self.signed());job=adapter.drain_one()['task_id']
        self.fixture.worker().run_once()
        result=tasks.result(job)
        self.assertTrue(result['runbook_context']['citations'])
        self.assertEqual(adapter.reply(receipt['request_id'],self.verified())['state'],'delivered')
        text=self.wire.posts()[0]['body']['text']
        for citation in result['runbook_context']['citations']:
            self.assertIn(citation['content_sha256'],text)
            self.assertIn(citation['document_id'],text)
            self.assertIn(citation['section']['id'],text)
        self.assertNotIn('private-secret-value',text)

    def test_relay_pending_work_does_not_block_later_completed_result(self):
        config=SlackWebConfig(enabled=True,bot_token_file=str(self.token_file),signing_secret_file=str(self.secret_file))
        runtime=SlackRuntime(self.tasks,self.policy,config,connection_factory=self.wire.connect)
        runtime.prepare()
        # Same adapter infrastructure, with test signing key for synthetic requests.
        runtime.adapter=SlackAdapter(self.tasks,self.policy,SECRET,transport_mode='slack_web_api')
        first=runtime.adapter.receive(*self.signed())['request_id']
        runtime.adapter.drain_one()
        event=deepcopy(self.event);event['event_id']='EvSECOND001'
        second=runtime.adapter.receive(*self.signed(event))['request_id']
        runtime.adapter.drain_one()
        original=self.tasks.show
        seen=[]
        def show(job):
            seen.append(job)
            return {'result':{'state':'pending' if job==runtime.adapter._row(first)['job_id'] else 'available'}}
        with patch.object(self.tasks,'show',side_effect=show), patch.object(runtime.adapter,'reply',return_value={'state':'delivered'}) as reply:
            self.assertEqual(runtime.relay_once()['reply']['state'],'delivered')
            self.assertEqual(reply.call_args.args[0],second)
        self.assertLessEqual(len(seen),2)

    def test_queue_backpressure_does_not_block_completed_reply(self):
        from ai_ops.job_queue import QueueFull
        config=SlackWebConfig(enabled=True,bot_token_file=str(self.token_file),signing_secret_file=str(self.secret_file))
        runtime=SlackRuntime(self.tasks,self.policy,config,connection_factory=self.wire.connect)
        runtime.adapter=SlackAdapter(self.tasks,self.policy,SECRET,transport_mode='slack_web_api')
        runtime.adapter.receive(*self.signed());runtime.adapter.drain_one()
        self.fixture.worker().run_once()
        with patch.object(runtime.adapter,'drain_one',side_effect=QueueFull('full')):
            result=runtime.relay_once()
        self.assertEqual(result['admission']['state'],'backpressure')
        self.assertEqual(result['reply']['state'],'delivered')

    def test_asgi_cancelled_wait_does_not_release_thread_slot_early(self):
        app=SlackEventsApp(self.adapter);headers,body=self.signed()
        started=threading.Event();release=threading.Event()
        original=self.adapter.receive
        def delayed(*args):
            started.set();release.wait(2);return original(*args)
        async def scenario():
            with patch.object(self.adapter,'receive',side_effect=delayed):
                task=asyncio.create_task(invoke(app,headers,body))
                while not started.is_set():await asyncio.sleep(.001)
                task.cancel()
                try:await task
                except asyncio.CancelledError:pass
                self.assertEqual(app._inflight,1)
                release.set()
                await asyncio.gather(*app._tasks)
                await asyncio.sleep(0)
                self.assertEqual(app._inflight,0)
        try:asyncio.run(scenario())
        finally:release.set()

    def test_disabled_runtime_does_not_read_keys_or_change_queue(self):
        config=SlackWebConfig(bot_token_file='/missing',signing_secret_file='/missing')
        with patch('ai_ops.slack_runtime.read_private_secret',side_effect=AssertionError('read')):
            with self.assertRaisesRegex(ValueError,'disabled'):SlackRuntime(self.tasks,self.policy,config)
        self.assertEqual(self.wire.calls,[])

    def test_config_is_strict_and_example_cannot_activate(self):
        from test_job_queue import ROOT
        config=SlackWebConfig.load(ROOT/'config/slack-web.disabled.example.json')
        self.assertFalse(config.enabled)
        path=self.queue.root/'config.json'
        for data in ({'enabled':'false'},{'schema_version':1,'enabled':False,'bot_token_file':'secret',
                'signing_secret_file':'secret'},{'enabled':True,'bot_token_file':'/x','signing_secret_file':'/y','endpoint':'https://attacker'}):
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError,'slack_web_config_rejected'):SlackWebConfig.load(path)

    def test_asgi_rejects_plaintext_bad_signature_commands_and_foreign_users(self):
        app=SlackEventsApp(self.adapter);headers,body=self.signed()
        self.assertEqual(asyncio.run(invoke(app,headers,body,scheme='http'))[0],403)
        self.assertEqual(asyncio.run(invoke(app,{**headers,'X-Slack-Signature':'invalid'},body))[0],403)
        for field,value in [('user','UOTHER01'),('text',f'<@{self.policy.bot_user_id}> approve trial')]:
            event=deepcopy(self.event);event['event'][field]=value
            self.assertEqual(asyncio.run(invoke(app,*self.signed(event)))[0],403)
        self.assertEqual(self.rows('slack_requests'),[]);self.assertEqual(self.wire.calls,[])

    def test_signed_url_challenge_and_chunked_asgi_body(self):
        app=SlackEventsApp(self.adapter)
        status,result=asyncio.run(invoke(app,*self.signed({'type':'url_verification','challenge':'challenge-value'})))
        self.assertEqual((status,result),(200,{'challenge':'challenge-value'}))
        headers,body=self.signed();half=len(body)//2
        chunks=[{'type':'http.request','body':body[:half],'more_body':True},
                {'type':'http.request','body':body[half:]}]
        self.assertEqual(asyncio.run(invoke(app,headers,body,chunks=chunks))[0],200)
        self.assertEqual(self.wire.calls,[])

    def test_asgi_content_length_headers_limits_and_partial_body(self):
        app=SlackEventsApp(self.adapter);headers,body=self.signed()
        for extra in ({'Content-Length':str(len(body))},{'Transfer-Encoding':'chunked'},
                      {'x-slack-signature':headers['X-Slack-Signature']}):
            self.assertEqual(asyncio.run(invoke(app,{**headers,**extra},body))[0],403)
        self.assertEqual(asyncio.run(invoke(app,headers,b'x'*65537))[0],403)
        self.assertEqual(asyncio.run(invoke(app,headers,body,chunks=[{'type':'http.request','body':body[:-1]}]))[0],403)

    def test_timeout_keeps_inflight_slot_until_thread_finishes_and_deduplicates(self):
        app=SlackEventsApp(self.adapter);app.ACK_SECONDS=.03;app.MAX_INFLIGHT=1
        headers,body=self.signed();release=threading.Event();original=self.adapter.receive
        def delayed(*args):
            release.wait(2)
            return original(*args)
        async def scenario():
            with patch.object(self.adapter,'receive',side_effect=delayed):
                try:
                    self.assertEqual((await invoke(app,headers,body))[0],503)
                    self.assertEqual(app._inflight,1)
                    self.assertEqual((await invoke(app,headers,body))[0],503)
                finally:release.set()
                await asyncio.gather(*app._tasks)
                await asyncio.sleep(0)
            self.assertEqual(app._inflight,0)
            app.ACK_SECONDS=2.5
            status,result=await invoke(app,headers,body)
            self.assertEqual(status,200);self.assertTrue(result['duplicate'])
        asyncio.run(scenario())
        self.assertEqual(len(self.rows('slack_requests')),1)
        self.assertEqual(self.wire.calls,[])

    def test_shutdown_cancellation_preserves_running_intake_until_actual_completion(self):
        app=SlackEventsApp(self.adapter);app.ACK_SECONDS=.01
        headers,body=self.signed();release=threading.Event()
        original=self.adapter.receive
        def delayed(*args):
            release.wait(2);return original(*args)
        async def scenario():
            with patch.object(self.adapter,'receive',side_effect=delayed):
                try:
                    self.assertEqual((await invoke(app,headers,body))[0],503)
                    async def receive():return {'type':'lifespan.shutdown'}
                    async def send(event):pass
                    shutdown=asyncio.create_task(app({'type':'lifespan'},receive,send))
                    await asyncio.sleep(.01)
                    shutdown.cancel()
                    try:await shutdown
                    except asyncio.CancelledError:pass
                    await asyncio.sleep(0)
                    self.assertEqual(app._inflight,1)
                    self.assertEqual(len(app._tasks),1)
                finally:
                    release.set()
                    if app._tasks:await asyncio.gather(*app._tasks,return_exceptions=True)
                    await asyncio.sleep(0)
            self.assertEqual(app._inflight,0)
        asyncio.run(scenario())
        self.assertEqual(len(self.rows('slack_requests')),1)

    def test_slow_body_is_bounded_and_releases_capacity(self):
        app=SlackEventsApp(self.adapter);app.ACK_SECONDS=.01
        async def scenario():
            sent=[]
            async def receive():await asyncio.sleep(.1)
            async def send(e):sent.append(e)
            await app({'type':'http','scheme':'https','method':'POST','path':'/slack/events',
                       'headers':[(b'content-length',b'100')]},receive,send)
            self.assertEqual(sent[0]['status'],503);self.assertEqual(app._inflight,0)
        asyncio.run(scenario())


if __name__=='__main__':unittest.main()
