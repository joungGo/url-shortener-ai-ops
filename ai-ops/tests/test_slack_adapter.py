"""실제 Slack 자격/연결 없이 서명·인가·영속 접수·큐·회신 경계를 검증한다."""

from copy import deepcopy
import hashlib
import hmac
import http.client
import json
import socket
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from ai_ops.contracts import canonical_json_bytes
from ai_ops.job_queue import RETENTION_SECONDS
from ai_ops.slack_adapter import SlackAdapter, SlackAdmissionBusy, SlackNotSent, SlackPolicy
from ai_ops.slack_local import SlackLocalServer
from ai_ops.tasks import TaskService
import test_job_queue as fixtures


SECRET=b'local-only-fixture-signing-secret-0001'


class FakeSlack:
    mode='local_fixture'
    def __init__(self,policy):
        self.team_id=policy.team_id;self.app_id=policy.app_id;self.bot_user_id=policy.bot_user_id
        self.calls=0;self.find_calls=0;self.messages=[];self.outcomes=[];self.lost_response=False
        self.last_payload=None

    def post_message(self,payload):
        self.calls+=1;self.last_payload=deepcopy(payload)
        if self.outcomes:
            outcome=self.outcomes.pop(0)
            if isinstance(outcome,BaseException):raise outcome
            return outcome
        stamp=f'1001.{self.calls:06d}'
        body={'ok':True,'channel':payload['channel'],'ts':stamp,'message':{
            'user':self.bot_user_id,'ts':stamp,'thread_ts':payload['thread_ts'],'text':payload['text']}}
        self.messages.append(body)
        if self.lost_response:raise OSError('private-upstream-response-lost')
        return {'status':200,'body':deepcopy(body)}

    def find_message(self,payload):
        self.find_calls+=1
        matches=[m for m in self.messages if m['channel']==payload['channel'] and m['message']['text']==payload['text']]
        return deepcopy(matches[0]) if len(matches)==1 else None


class SlackFixture:
    def setUp(self):
        self.fixture=fixtures.JobQueueTest();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.queue=self.fixture.queue;self.tasks=TaskService(self.queue,self.fixture.settings)
        self.policy=SlackPolicy.model_validate_json((fixtures.ROOT/'config/slack-local.example.json').read_bytes())
        self.adapter=SlackAdapter(self.tasks,self.policy,SECRET)
        self.transport=FakeSlack(self.policy)
        self.event={'type':'event_callback','team_id':self.policy.team_id,'api_app_id':self.policy.app_id,
            'event_id':'EvLOCAL0001','event':{'type':'app_mention','user':'UALICE01','channel':'CLOCAL01',
            'ts':'1000.000002','thread_ts':'1000.000001',
            'text':f'<@{self.policy.bot_user_id}> investigate {self.fixture.target.alias} 상태 token=private-secret-value'}}

    def signed(self,event=None,*,body=None,timestamp=None):
        raw=canonical_json_bytes(self.event if event is None else event) if body is None else body
        stamp=str(int(self.fixture.time)) if timestamp is None else str(timestamp)
        signature='v0='+hmac.new(SECRET,b'v0:'+stamp.encode()+b':'+raw,hashlib.sha256).hexdigest()
        return {'Content-Type':'application/json; charset=utf-8','X-Slack-Request-Timestamp':stamp,'X-Slack-Signature':signature},raw

    def receive(self,event=None):
        return self.adapter.receive(*self.signed(event))

    def complete(self,event=None):
        receipt=self.receive(event);queued=self.adapter.drain_one()
        self.fixture.worker().run_once()
        return receipt['request_id'],queued['task_id']

    def rows(self,table):
        with self.adapter._db() as db:return [dict(r) for r in db.execute('SELECT * FROM '+table)]


class SlackAdapterTest(SlackFixture,unittest.TestCase):
    def test_signature_raw_bytes_timestamp_and_case_insensitive_headers(self):
        headers,body=self.signed()
        for changes,raw in [({'X-Slack-Signature':'v0='+'0'*64},body),({},body+b' '),({'X-Slack-Request-Timestamp':'0001'},body)]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.adapter.receive({**headers,**changes},raw)
        for stamp in (self.fixture.time-301,self.fixture.time+301):
            with self.assertRaises(ValueError):self.adapter.receive(*self.signed(timestamp=int(stamp)))
        self.assertTrue(self.adapter.receive({k.lower():v for k,v in headers.items()},body)['accepted'])

    def test_duplicate_signature_headers_unsupported_content_and_invalid_json(self):
        headers,body=self.signed()
        with self.assertRaises(ValueError):self.adapter.receive([*headers.items(),('x-slack-signature',headers['X-Slack-Signature'])],body)
        with self.assertRaises(ValueError):self.adapter.receive({**headers,'Content-Type':'text/plain'},body)
        for body in (b'{"type":"event_callback","type":"url_verification"}',b'\xff',b'['*1200+b'0'+b']'*1200):
            with self.assertRaises((ValueError,RecursionError)):self.adapter.receive(*self.signed(body=body))
        self.assertEqual(self.rows('slack_requests'),[])

    def test_workspace_app_actor_channel_target_and_shared_context_deny_by_default(self):
        for field,value in [('team_id','TFOREIGN'),('api_app_id','AFOREIGN'),('is_ext_shared_channel',True),('context_team_id','TFOREIGN')]:
            event=deepcopy(self.event);event[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):self.receive(event)
        for field,value in [('user','UBOB0001'),('channel','CFOREIGN'),('user_team','TFOREIGN'),('team','TFOREIGN')]:
            event=deepcopy(self.event);event['event'][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):self.receive(event)
        event=deepcopy(self.event);event['event']['text']=f'<@{self.policy.bot_user_id}> investigate unregistered-target'
        with self.assertRaises(ValueError):self.receive(event)
        self.assertEqual(self.rows('slack_requests'),[])

    def test_bot_events_and_approval_recovery_commands_are_not_requests(self):
        for field,value in [('bot_id','BLOCAL01'),('subtype','bot_message'),('hidden',True),('user',self.policy.bot_user_id)]:
            event=deepcopy(self.event);event['event'][field]=value
            with self.assertRaises(ValueError):self.receive(event)
        for command in ('approve 17','recover lab-api','cancel all','investigate'):
            event=deepcopy(self.event);event['event']['text']=f'<@{self.policy.bot_user_id}> {command}'
            with self.assertRaises(ValueError):self.receive(event)

    def test_fast_ack_is_durable_inbox_only_and_never_calls_queue_or_worker(self):
        with patch.object(self.tasks,'submit',side_effect=AssertionError('ACK 중 큐 실행 금지')):
            receipt=self.receive()
        self.assertTrue(receipt['accepted']);self.assertFalse(receipt['queue_accepted']);self.assertFalse(receipt['execution_allowed'])
        reopened=SlackAdapter(self.tasks,self.policy,SECRET)
        self.assertTrue(reopened.receive(*self.signed())['duplicate'])
        with self.queue.transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
        self.assertEqual(len(self.rows('slack_requests')),1)

    def test_duplicate_fresh_signature_reuses_job_and_modified_event_conflicts(self):
        receipt=self.receive();job=self.adapter.drain_one()['task_id'];self.fixture.time+=1
        again=self.receive();self.assertEqual(receipt['request_id'],again['request_id']);self.assertTrue(again['duplicate'])
        self.assertTrue(again['queue_accepted']);self.assertEqual(self.adapter.drain_one()['state'],'idle')
        event=deepcopy(self.event);event['event']['text']+=' changed'
        with self.assertRaises(ValueError):self.receive(event)
        with self.queue.transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
        self.assertEqual(self.rows('slack_requests')[0]['job_id'],job)

    def test_queue_commit_then_relay_crash_recovers_same_job(self):
        self.receive();original=self.tasks.submit
        def crash(raw):original(raw);raise OSError('lost-queue-reply')
        with patch.object(self.tasks,'submit',side_effect=crash),self.assertRaises(OSError):self.adapter.drain_one()
        self.assertEqual(self.rows('slack_requests')[0]['state'],'accepted')
        self.adapter.drain_one()
        with self.queue.transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
        self.assertIsNone(self.rows('slack_requests')[0]['question'])

    def test_queue_busy_retains_sanitized_inbox_for_retry(self):
        self.receive()
        with patch.object(self.tasks,'submit',side_effect=OSError('queue-full')),self.assertRaises(OSError):self.adapter.drain_one()
        self.assertNotIn('private-secret-value',str(self.rows('slack_requests')))
        self.assertEqual(self.adapter.drain_one()['state'],'queued')

    def test_permanent_queue_rejection_does_not_block_other_accepted_requests(self):
        receipt=self.receive()
        with patch.object(self.tasks,'submit',side_effect=ValueError('private-invalid-input')):
            self.assertEqual(self.adapter.drain_one()['state'],'rejected')
        self.assertEqual(self.receive()['intake_state'],'failed')
        self.assertIsNone(self.adapter._row(receipt['request_id'])['question'])
        event=deepcopy(self.event);event['event_id']='EvNEXT0002';self.receive(event)
        self.assertEqual(self.adapter.drain_one()['state'],'queued')

    def test_existing_queue_worker_verified_partial_result_replies_only_in_source_thread(self):
        request_id,job=self.complete()
        with patch('socket.create_connection',side_effect=AssertionError('실제 Slack 연결 금지')):
            result=self.adapter.reply(request_id,self.transport)
        self.assertEqual(result['state'],'delivered');self.assertFalse(result['human_read_confirmed']);self.assertFalse(result['execution_allowed'])
        payload=self.transport.last_payload
        self.assertEqual(payload['channel'],'CLOCAL01');self.assertEqual(payload['thread_ts'],'1000.000001')
        self.assertIn('partial',payload['text']);self.assertIn(job,payload['text'])
        self.assertNotIn('private-secret-value',json.dumps(payload));self.assertFalse(payload['reply_broadcast'])
        self.assertEqual(self.adapter.reply(request_id,self.transport)['attempts'],1);self.assertEqual(self.transport.calls,1)
        self.assertEqual(self.queue.show(job)['attempts'],1)

    def test_top_level_mention_uses_own_message_as_reply_parent(self):
        event=deepcopy(self.event);event['event'].pop('thread_ts')
        request_id,_=self.complete(event);self.adapter.reply(request_id,self.transport)
        self.assertEqual(self.transport.last_payload['thread_ts'],event['event']['ts'])

    def test_wrong_or_live_transport_and_pending_report_never_send(self):
        receipt=self.receive();self.adapter.drain_one()
        with self.assertRaises(ValueError):self.adapter.reply(receipt['request_id'],self.transport)
        self.fixture.worker().run_once()
        with self.assertRaises(ValueError):self.adapter.reply(receipt['request_id'])
        for field,value in [('mode','live'),('team_id','TFOREIGN'),('app_id','AFOREIGN'),('bot_user_id','UFOREIGN')]:
            fake=FakeSlack(self.policy);setattr(fake,field,value)
            with self.assertRaises(ValueError):self.adapter.reply(receipt['request_id'],fake)
            self.assertEqual(fake.calls,0)

    def test_revoked_policy_prevents_queue_and_reply(self):
        receipt=self.receive()
        policy=self.policy.model_dump();policy['grants'][0]['user_id']='UBOB0001'
        changed=SlackAdapter(self.tasks,policy,SECRET)
        with self.assertRaises(ValueError):self.receive()
        self.assertEqual(changed.drain_one()['state'],'rejected')
        self.assertIsNone(self.rows('slack_requests')[0]['question'])
        self.adapter=SlackAdapter(self.tasks,self.policy,SECRET)
        self.event['event_id']='EvLOCAL0002';request_id,_=self.complete()
        changed=SlackAdapter(self.tasks,policy,SECRET)
        with self.assertRaises(ValueError):changed.reply(request_id,self.transport)
        with self.assertRaises(ValueError):self.adapter.reply(request_id,self.transport)
        self.assertEqual(self.transport.calls,0)

    def test_other_installation_cannot_replace_this_store_identity(self):
        self.receive()
        for field,value in [('team_id','TOTHER01'),('app_id','AOTHER01'),('bot_user_id','UOTHER01')]:
            policy=self.policy.model_dump();policy[field]=value
            with self.assertRaises(ValueError):SlackAdapter(self.tasks,policy,SECRET)
        self.assertTrue(self.receive()['duplicate'])

    def test_inactive_instance_cannot_reject_or_prune_new_policy_requests(self):
        policy=self.policy.model_dump();policy['grants'].append({'user_id':'UBOB0001','channel_id':'CLOCAL01','targets':[self.fixture.target.alias]})
        current=SlackAdapter(self.tasks,policy,SECRET)
        receipt=current.receive(*self.signed())
        with self.assertRaises(ValueError):self.adapter.drain_one()
        with self.assertRaises(ValueError):self.adapter.prune()
        self.assertEqual(current._row(receipt['request_id'])['state'],'accepted')
        self.assertEqual(current.drain_one()['state'],'queued')

    def test_signing_key_rotation_disables_old_instance_without_persisting_keys(self):
        new_key=b'new-local-fixture-signing-secret-002'
        rotated=SlackAdapter(self.tasks,self.policy,new_key)
        with self.assertRaises(ValueError):self.receive()
        headers,body=self.signed()
        headers['X-Slack-Signature']='v0='+hmac.new(new_key,b'v0:'+headers['X-Slack-Request-Timestamp'].encode()+b':'+body,hashlib.sha256).hexdigest()
        self.assertTrue(rotated.receive(headers,body)['accepted'])
        with rotated._db() as db:
            stored=str([tuple(r) for r in db.execute('SELECT * FROM slack_installation')])
        self.assertNotIn(SECRET.decode(),stored);self.assertNotIn(new_key.decode(),stored)

    def test_config_change_and_in_place_policy_change_fail_closed(self):
        request_id,_=self.complete()
        self.adapter.policy.grants[0].targets.append('unregistered-extra')
        with self.assertRaises(ValueError):self.adapter.reply(request_id,self.transport)
        with self.assertRaises(ValueError):self.receive()
        self.adapter.policy.grants[0].targets.pop()
        settings=self.tasks.settings
        self.tasks.settings=settings.__class__(settings.bindings,settings.runtime,settings.metrics,'0'*64)
        with self.assertRaises(ValueError):self.adapter.reply(request_id,self.transport)
        with self.assertRaises(ValueError):self.receive()
        self.assertEqual(self.transport.calls,0)

    def test_even_other_authorized_actor_or_channel_cannot_rebind_an_event(self):
        policy=self.policy.model_dump()
        policy['grants'] += [{'user_id':'UBOB0001','channel_id':'CLOCAL01','targets':[self.fixture.target.alias]},
                             {'user_id':'UALICE01','channel_id':'COTHER01','targets':[self.fixture.target.alias]}]
        self.adapter=SlackAdapter(self.tasks,policy,SECRET);self.receive()
        for field,value in [('user','UBOB0001'),('channel','COTHER01'),('thread_ts','1000.000003')]:
            event=deepcopy(self.event);event['event'][field]=value
            with self.assertRaises(ValueError):self.receive(event)

    def test_request_supplied_callback_url_is_never_a_reply_destination(self):
        for where in ('envelope','event'):
            event=deepcopy(self.event)
            (event if where=='envelope' else event['event'])['response_url']='http://169.254.169.254/private'
            with self.assertRaises(ValueError):self.receive(event)
        self.assertEqual(self.transport.calls,0)

    def test_global_pending_limit_is_bounded_across_authorized_users(self):
        policy=self.policy.model_dump();policy['grants']=[{'user_id':f'UUSER{i:04d}',
            'channel_id':'CLOCAL01','targets':[self.fixture.target.alias]} for i in range(21)]
        self.adapter=SlackAdapter(self.tasks,policy,SECRET)
        for i in range(100):
            event=deepcopy(self.event);event['event_id']=f'EvGLOBAL{i:04d}';event['event']['user']=f'UUSER{i//5:04d}'
            self.receive(event)
        event=deepcopy(self.event);event['event_id']='EvGLOBAL0100';event['event']['user']='UUSER0020'
        with self.assertRaises(SlackAdmissionBusy):self.receive(event)
        self.assertEqual(len(self.rows('slack_requests')),100)

    def test_429_retry_after_uses_response_time_and_never_reexecutes_analysis(self):
        request_id,job=self.complete()
        original=self.transport.post_message
        def throttled(payload):
            self.transport.calls+=1;self.fixture.time+=20
            return {'status':429,'retry_after':30,'body':{'ok':False}}
        with patch.object(self.transport,'post_message',side_effect=throttled):
            self.assertEqual(self.adapter.reply(request_id,self.transport)['retry_at'],self.fixture.time+30)
        self.fixture.time+=29
        self.assertEqual(self.adapter.reply(request_id,self.transport)['state'],'pending');self.assertEqual(self.transport.calls,1)
        self.fixture.time+=1
        self.assertEqual(self.adapter.reply(request_id,self.transport)['state'],'delivered');self.assertEqual(self.transport.calls,2)
        self.assertEqual(self.queue.show(job)['attempts'],1)

    def test_definite_not_sent_retries_at_most_three_times(self):
        request_id,_=self.complete();self.transport.outcomes=[SlackNotSent(),SlackNotSent(),SlackNotSent()]
        for state in ('pending','pending','failed'):
            self.assertEqual(self.adapter.reply(request_id,self.transport)['state'],state);self.fixture.time+=5
        self.assertEqual(self.adapter.reply(request_id,self.transport)['state'],'failed');self.assertEqual(self.transport.calls,3)

    def test_success_http_with_slack_error_and_server_errors_are_not_success(self):
        cases=[({'status':200,'body':{'ok':False,'error':'private-error'}},'failed'),
               ({'status':503,'body':{'ok':False}},'needs_review'),({'status':429,'retry_after':-1},'needs_review'),
               ({'status':200,'body':{'ok':True,'channel':'CFOREIGN','ts':'1000.000001'}},'needs_review')]
        for i,(response,state) in enumerate(cases):
            event=deepcopy(self.event);event['event_id']=f'EvCASE000{i}'
            request_id,_=self.complete(event);fake=FakeSlack(self.policy);fake.outcomes=[response]
            with self.subTest(i=i):self.assertEqual(self.adapter.reply(request_id,fake)['state'],state)
            self.adapter.reply(request_id,fake);self.assertEqual(fake.calls,1)

    def test_lost_response_reconciles_own_message_without_reposting(self):
        request_id,_=self.complete();self.transport.lost_response=True
        self.assertEqual(self.adapter.reply(request_id,self.transport)['state'],'needs_review')
        self.assertEqual(self.adapter.reply(request_id,self.transport)['state'],'needs_review')
        self.assertEqual(self.transport.calls,1)
        self.assertEqual(self.adapter.reconcile_reply(request_id,self.transport)['state'],'delivered')
        self.assertEqual(self.transport.calls,1)

    def test_reconcile_missing_or_foreign_author_never_forges_success(self):
        request_id,_=self.complete();self.transport.outcomes=[OSError('private-timeout')]
        self.assertEqual(self.adapter.reply(request_id,self.transport)['state'],'needs_review')
        self.assertEqual(self.adapter.reconcile_reply(request_id,self.transport)['state'],'needs_review')
        self.transport.outcomes=[];payload=self.adapter._payload(self.adapter._row(request_id));self.transport.post_message(payload)
        self.transport.messages[0]['message']['user']='UFOREIGN'
        self.assertEqual(self.adapter.reconcile_reply(request_id,self.transport)['state'],'needs_review')

    def test_reconcile_keeps_known_message_id_after_a_failed_lookup(self):
        request_id,_=self.complete()
        first=self.adapter.reply(request_id,self.transport)
        original=self.transport.messages.pop()
        missing=self.adapter.reconcile_reply(request_id,self.transport)
        self.assertEqual(missing['state'],'needs_review')
        self.assertEqual(missing['message_ts'],first['message_ts'])
        other=deepcopy(original);other['ts']='1001.999999';other['message']['ts']=other['ts']
        self.transport.messages=[other]
        self.assertEqual(self.adapter.reconcile_reply(request_id,self.transport)['state'],'needs_review')
        self.transport.messages=[original]
        self.assertEqual(self.adapter.reconcile_reply(request_id,self.transport)['state'],'delivered')
        self.assertEqual(self.transport.calls,1)

    def test_reconcile_rechecks_expiry_after_message_lookup(self):
        request_id,_=self.complete();self.transport.lost_response=True
        self.adapter.reply(request_id,self.transport)
        original=self.transport.find_message
        def expired(payload):
            result=original(payload);self.fixture.time+=RETENTION_SECONDS
            return result
        with patch.object(self.transport,'find_message',side_effect=expired):
            self.assertEqual(self.adapter.reconcile_reply(request_id,self.transport)['state'],'needs_review')
        self.assertEqual(self.transport.calls,1)

    def test_reconcile_rechecks_policy_after_message_lookup(self):
        request_id,_=self.complete();self.transport.lost_response=True
        self.adapter.reply(request_id,self.transport)
        original=self.transport.find_message
        def changed(payload):
            result=original(payload);self.adapter.policy.grants[0].targets.append('changed')
            return result
        with patch.object(self.transport,'find_message',side_effect=changed):
            self.assertEqual(self.adapter.reconcile_reply(request_id,self.transport)['state'],'needs_review')
        self.assertEqual(self.transport.calls,1)

    def test_crash_during_post_keeps_sending_and_never_posts_again(self):
        request_id,_=self.complete();self.transport.outcomes=[KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):self.adapter.reply(request_id,self.transport)
        self.assertEqual(self.adapter.reply(request_id,self.transport)['state'],'sending');self.assertEqual(self.transport.calls,1)

    def test_per_user_limit_does_not_block_same_event_retry(self):
        first=None
        for i in range(5):
            event=deepcopy(self.event);event['event_id']=f'EvLIMIT00{i}'
            self.receive(event)
            if first is None:first=event
        event=deepcopy(self.event);event['event_id']='EvLIMIT006'
        with self.assertRaises(SlackAdmissionBusy):self.receive(event)
        self.assertTrue(self.receive(first)['duplicate'])

    def test_ack_deadline_failure_retries_durable_event_without_duplicate(self):
        with patch.object(self.adapter,'monotonic',side_effect=[0,3]),self.assertRaises(TimeoutError):self.receive()
        self.assertTrue(self.receive()['duplicate']);self.assertEqual(len(self.rows('slack_requests')),1)

    def test_inbox_contention_is_bounded_and_never_claims_acceptance(self):
        started=time.monotonic()
        with self.adapter._db():
            with self.assertRaises(sqlite3.OperationalError):self.receive()
        self.assertLess(time.monotonic()-started,3)
        self.assertEqual(self.rows('slack_requests'),[])

    def test_expiry_clears_question_route_and_receipt_but_preserves_dedup_tombstone(self):
        request_id,_=self.complete();self.adapter.reply(request_id,self.transport)
        self.fixture.time+=RETENTION_SECONDS
        with self.assertRaises(ValueError):self.adapter.reply(request_id,self.transport)
        self.assertEqual(self.adapter.prune()['expired_requests'],1)
        row=self.rows('slack_requests')[0]
        self.assertEqual(row['channel_id'],'');self.assertEqual(row['user_id'],'');self.assertIsNone(row['question'])
        self.assertIsNone(self.rows('slack_replies')[0]['message_ts'])
        with self.assertRaises(ValueError):self.receive()
        self.assertEqual(self.adapter.prune()['expired_requests'],0)

    def test_processing_lock_does_not_block_fast_ack(self):
        with self.adapter._lock():
            self.assertTrue(self.receive()['accepted'])
            with self.assertRaises(ValueError):self.adapter.drain_one()


class SlackHTTPTest(SlackFixture,unittest.TestCase):
    def server(self):
        server=SlackLocalServer(self.adapter);thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
        def close():server.shutdown();thread.join(timeout=3);server.server_close()
        self.addCleanup(close);return server

    def post(self,server,headers,body,path='/slack/events',*,return_headers=False):
        client=http.client.HTTPConnection(*server.server_address,timeout=3)
        try:
            client.request('POST',path,body=body,headers=headers);response=client.getresponse()
            value=json.loads(response.read())
            return (response.status,value,dict(response.getheaders())) if return_headers else (response.status,value)
        finally:client.close()

    def test_actual_loopback_signed_request_queue_worker_and_fixture_thread_reply(self):
        server=self.server();start=time.monotonic()
        status,receipt=self.post(server,*self.signed());elapsed=time.monotonic()-start
        self.assertEqual(status,200);self.assertLess(elapsed,3);self.assertFalse(receipt['queue_accepted'])
        queued=self.adapter.drain_one();self.fixture.worker().run_once()
        self.assertEqual(self.adapter.reply(receipt['request_id'],self.transport)['state'],'delivered')
        self.assertEqual(self.transport.last_payload['thread_ts'],'1000.000001');self.assertFalse(queued['execution_allowed'])

    def test_http_auth_errors_and_unsupported_path_do_not_reflect_body_or_secrets(self):
        server=self.server();headers,body=self.signed();headers['X-Slack-Signature']='v0='+'0'*64
        status,value=self.post(server,headers,body)
        self.assertEqual((status,value),(403,{'status':'rejected'}))
        self.assertNotIn('private-secret-value',json.dumps(value))
        self.assertEqual(self.post(server,*self.signed(),path='/slack/events?override=true')[0],403)

    def test_http_permanent_rejection_disables_retry_without_granting_access(self):
        server=self.server();headers,body=self.signed();headers['X-Slack-Signature']='v0='+'0'*64
        status,value,response_headers=self.post(server,headers,body,return_headers=True)
        self.assertEqual(status,403);self.assertEqual(value,{'status':'rejected'})
        self.assertEqual(response_headers.get('X-Slack-No-Retry'),'1')
        self.assertEqual(self.rows('slack_requests'),[])

    def test_http_temporary_admission_limit_allows_retry_and_never_accepts_overflow(self):
        server=self.server()
        for i in range(5):
            event=deepcopy(self.event);event['event_id']=f'EvRATE000{i}';self.receive(event)
        status,value,response_headers=self.post(server,*self.signed(),return_headers=True)
        self.assertEqual((status,value),(503,{'status':'unavailable'}))
        self.assertNotIn('X-Slack-No-Retry',response_headers)
        self.assertEqual(len(self.rows('slack_requests')),5)

    def test_signed_url_challenge_is_not_a_job_or_permission_grant(self):
        server=self.server();status,value=self.post(server,*self.signed({'type':'url_verification','challenge':'LOCAL_fixture_only'}))
        self.assertEqual((status,value),(200,{'challenge':'LOCAL_fixture_only'}));self.assertEqual(self.rows('slack_requests'),[])

    def test_http_late_ack_returns_unavailable_and_retry_recovers_same_receipt(self):
        server=self.server()
        with patch.object(self.adapter,'monotonic',side_effect=[0,3]):
            self.assertEqual(self.post(server,*self.signed())[0],503)
        status,receipt=self.post(server,*self.signed());self.assertEqual(status,200);self.assertTrue(receipt['duplicate'])


if __name__=='__main__':unittest.main()
