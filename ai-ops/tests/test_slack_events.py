"""WF-04 signed synthetic requests, real local SQLite/queue/worker, fake Slack only."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from ai_ops.contracts import sha256_json
from ai_ops.event_store import EventStore
from ai_ops.job_queue import JobQueue, QueueFull, RETENTION_SECONDS
from ai_ops.slack_adapter import SlackAdapter
from ai_ops.slack_events import UNAVAILABLE
from ai_ops.slack_runtime import SlackWebConfig
from ai_ops.tasks import TaskService
from test_slack_adapter import SlackFixture, FakeSlack, SECRET


class SlackEventsTest(SlackFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.sources={'lab-seoul':{'source_id':'am-lab','cluster_id':self.fixture.target.target.cluster_id}}
        self.reopen()
        self.alert={'labels':{**self.fixture.target.target.model_dump(),'alertname':'CacheHitRateLow','ai_ops':'enabled'},
                    'status':'firing','fingerprint':'0123456789abcdef','startsAt':'1970-01-01T00:16:40Z'}
        self.eid=self.adapter.events.ingest('lab-seoul',self.alert)
        self.ref='al1/lab-seoul/0123456789abcdef/1970-01-01T00:16:40Z'
        self.serial=0

    def reopen(self):
        self.queue=JobQueue(self.fixture.store,clock=lambda:self.fixture.time)
        self.tasks=TaskService(self.queue,self.fixture.settings)
        self.adapter=SlackAdapter(self.tasks,self.policy,SECRET,event_sources=self.sources)

    def command(self, action='investigate-event', reference=None, **fields):
        self.serial+=1
        event=deepcopy(self.event)
        event['event_id']=f'EvWF04{self.serial:06}'
        event['event'].update(text=f'<@{self.policy.bot_user_id}> {action} {reference or self.eid}',**fields)
        return event

    def count(self, table):
        with self.queue.transaction() as db:
            return db.execute('SELECT count(*) FROM '+table).fetchone()[0]

    def run_request(self, event=None, *, complete=False):
        receipt=self.receive(event or self.command())
        admitted=self.adapter.drain_one()
        if complete:self.fixture.worker().run_once()
        return receipt,admitted

    def test_manual_default_request_ack_queue_worker_same_event_and_thread(self):
        self.assertEqual(self.count('jobs'),0)
        event=self.command(reference=self.ref)
        with patch.object(self.adapter.events,'request',side_effect=AssertionError('ACK must not queue')):
            receipt=self.receive(event)
        self.assertFalse(receipt['queue_accepted']);self.assertEqual(self.count('jobs'),0)
        queued=self.adapter.drain_one();self.fixture.worker().run_once()
        self.assertEqual(queued['event_id'],self.eid)
        result=self.adapter.reply(receipt['request_id'],self.transport)
        self.assertEqual(result['state'],'delivered');self.assertFalse(result['execution_allowed'])
        payload=self.transport.last_payload
        self.assertEqual((payload['channel'],payload['thread_ts']),('CLOCAL01','1000.000001'))
        for text in (self.eid,'경보 발생:','실제 자료 범위:','자료 수집 시각:','당시 UID','변경·복구를 실행하지 않았습니다'):
            self.assertIn(text,payload['text'])
        self.assertEqual(self.count('jobs'),1)
        self.assertEqual(self.count('event_requests'),1)

    def test_status_is_snapshot_without_collecting(self):
        receipt,queued=self.run_request(complete=True)
        status,_=self.run_request(self.command('status-event'))
        self.adapter.reply(status['request_id'],self.transport)
        text=self.transport.last_payload['text']
        self.assertIn(queued['task_id'],text)
        self.assertIn('새 조사를 시작하지 않았습니다',text)
        self.assertEqual(self.count('jobs'),1)
        # An alert transition after status preparation cannot mutate a retry payload.
        self.adapter.events.ingest('lab-seoul',{**self.alert,'status':'resolved','endsAt':'1970-01-01T00:16:41Z'})
        self.adapter.reply(status['request_id'],self.transport)
        self.assertEqual(self.transport.calls,1)

    def test_status_before_any_investigation_creates_no_job(self):
        receipt,_=self.run_request(self.command('status-event'))
        self.adapter.reply(receipt['request_id'],self.transport)
        self.assertIn('연결된 조사 작업 없음',self.transport.last_payload['text'])
        self.assertEqual(self.count('jobs'),0)

    def test_unknown_unbound_and_unauthorized_have_identical_non_disclosing_reply(self):
        other=deepcopy(self.alert);other['fingerprint']='1123456789abcdef'
        other['labels']={'alertname':'NodeDown'}
        unbound=self.adapter.events.ingest('lab-seoul',other)
        second=self.fixture.target.model_copy(deep=True)
        second.alias='other-target';second.target.workload='other-api';second.target.service='other-api'
        self.fixture.settings.bindings.bindings.append(second)
        other['fingerprint']='2123456789abcdef';other['labels']={**second.target.model_dump(),'alertname':'HighErrorRate'}
        denied=self.adapter.events.ingest('lab-seoul',other)
        # Policy fixture may allow several bindings; explicitly restrict current target.
        self.policy.grants[0].targets=[self.fixture.target.alias];self.reopen()
        replies=[]
        for reference in ('evt-'+'a'*64,unbound,denied):
            receipt,_=self.run_request(self.command(reference=reference))
            self.adapter.reply(receipt['request_id'],self.transport)
            replies.append(self.transport.last_payload['text'])
            self.assertNotIn(reference,replies[-1])
        self.assertEqual(len(set(replies)),1);self.assertIn(UNAVAILABLE,replies[0])
        self.assertEqual(self.count('jobs'),0)

    def test_notice_arrives_before_registration_requires_new_message(self):
        ref='al1/lab-seoul/3123456789abcdef/1970-01-01T00:16:40Z'
        event=self.command(reference=ref)
        receipt,_=self.run_request(event)
        self.adapter.reply(receipt['request_id'],self.transport)
        self.adapter.events.ingest('lab-seoul',{**self.alert,'fingerprint':'3123456789abcdef'})
        self.assertTrue(self.receive(event)['duplicate'])
        self.assertEqual(self.adapter.drain_one()['state'],'idle')
        self.assertEqual(self.count('jobs'),0)
        _,admitted=self.run_request(self.command(reference=ref))
        self.assertEqual(admitted['state'],'queued');self.assertEqual(self.count('jobs'),1)

    def test_unsigned_foreign_user_channel_team_and_reference_rejected(self):
        for field,value in [('user','UFOREIGN'),('channel','CALERT01')]:
            with self.subTest(field=field),self.assertRaises(ValueError):self.receive(self.command(**{field:value}))
        event=self.command();event['team_id']='TFOREIGN'
        with self.assertRaises(ValueError):self.receive(event)
        headers,body=self.signed(self.command());headers['X-Slack-Signature']='v0='+'0'*64
        with self.assertRaises(ValueError):self.adapter.receive(headers,body)
        for ref in ('https://example.invalid/path','al1/foreign/0123456789abcdef/1970-01-01T00:16:40Z','evt-invalid'):
            with self.assertRaises(ValueError):self.receive(self.command(reference=ref))
        self.assertEqual(self.count('jobs'),0)
        self.assertEqual(self.rows('slack_requests'),[])

    def test_opt_in_disabled_preserves_ordinary_investigate(self):
        self.adapter=SlackAdapter(self.tasks,self.policy,SECRET)
        with self.assertRaises(ValueError):self.receive(self.command())
        receipt,_=self.run_request(self.event,complete=True)
        self.adapter.reply(receipt['request_id'],self.transport)
        self.assertIn('사건 미지정',self.transport.last_payload['text'])
        self.assertEqual(self.count('event_requests'),0)

    def test_duplicate_reopen_and_response_loss_do_not_repost(self):
        event=self.command();receipt,queued=self.run_request(event,complete=True)
        self.reopen();self.assertTrue(self.receive(event)['duplicate'])
        self.assertEqual(self.adapter.drain_one()['state'],'idle')
        self.transport.lost_response=True
        self.assertEqual(self.adapter.reply(receipt['request_id'],self.transport)['state'],'needs_review')
        self.reopen()
        self.assertEqual(self.adapter.reply(receipt['request_id'],self.transport)['state'],'needs_review')
        self.assertEqual(self.adapter.reconcile_reply(receipt['request_id'],self.transport)['state'],'delivered')
        self.assertEqual(self.transport.calls,1);self.assertEqual(self.count('jobs'),1)

    def test_crash_after_queue_commit_reconnects_same_job(self):
        receipt=self.receive(self.command())
        with patch.object(self.adapter.events,'request_receipt',side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):self.adapter.drain_one()
        self.assertEqual(self.count('jobs'),1)
        self.reopen();queued=self.adapter.drain_one()
        self.assertEqual(self.count('jobs'),1)
        self.fixture.worker().run_once();self.adapter.reply(receipt['request_id'],self.transport)
        self.assertEqual(self.transport.calls,1)

    def test_crash_after_event_reservation_does_not_lose_or_duplicate(self):
        self.receive(self.command())
        with patch.object(self.adapter.events,'drain',side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):self.adapter.drain_one()
        self.assertEqual(self.count('event_requests'),1);self.assertEqual(self.count('jobs'),0)
        self.reopen();self.adapter.drain_one()
        self.assertEqual(self.count('event_requests'),1);self.assertEqual(self.count('jobs'),1)

    def test_queue_capacity_retains_same_reservation(self):
        self.receive(self.command())
        with patch.object(self.queue,'_submit',side_effect=QueueFull('fixture')):
            with self.assertRaises(QueueFull):self.adapter.drain_one()
        self.assertEqual(self.count('event_requests'),1)
        self.adapter.drain_one();self.assertEqual(self.count('event_requests'),1)

    def test_new_request_always_gets_fresh_job_and_own_thread(self):
        first,one=self.run_request(complete=True)
        second,two=self.run_request(self.command(thread_ts='1000.000099'),complete=True)
        self.assertNotEqual(one['task_id'],two['task_id'])
        for receipt,thread in ((first,'1000.000001'),(second,'1000.000099')):
            self.adapter.reply(receipt['request_id'],self.transport)
            self.assertEqual(self.transport.last_payload['thread_ts'],thread)

    def test_unique_thread_this_and_ambiguous_thread(self):
        self.run_request()
        _,queued=self.run_request(self.command(reference='this'))
        self.assertEqual(queued['event_id'],self.eid)
        eid2=self.adapter.events.ingest('lab-seoul',{**self.alert,'fingerprint':'4123456789abcdef'})
        self.run_request(self.command(reference=eid2))
        receipt,result=self.run_request(self.command(reference='this'))
        self.assertEqual(result['state'],'ready')
        self.adapter.reply(receipt['request_id'],self.transport)
        self.assertIn('하나로 정할 수 없습니다',self.transport.last_payload['text'])
        self.assertEqual(self.count('jobs'),3)

    def test_other_thread_cannot_infer_this(self):
        self.run_request()
        receipt,_=self.run_request(self.command(reference='this',thread_ts='1000.000099'))
        self.adapter.reply(receipt['request_id'],self.transport)
        self.assertIn('하나로 정할 수 없습니다',self.transport.last_payload['text'])
        self.assertEqual(self.count('jobs'),1)

    def test_status_does_not_expose_other_users_jobs(self):
        self.policy.grants.append(self.policy.grants[0].model_copy(update={'user_id':'UBOB0001'}));self.reopen()
        _,queued=self.run_request()
        receipt,_=self.run_request(self.command('status-event',user='UBOB0001'))
        self.adapter.reply(receipt['request_id'],self.transport)
        self.assertNotIn(queued['task_id'],self.transport.last_payload['text'])

    def test_policy_change_between_ack_and_relay_blocks_old_request(self):
        self.receive(self.command())
        self.policy.grants[0].user_id='UBOB0001';self.reopen()
        self.assertEqual(self.adapter.drain_one()['state'],'rejected')
        self.assertEqual(self.count('jobs'),0)

    def test_policy_change_after_result_blocks_reply(self):
        receipt,_=self.run_request(complete=True)
        self.policy.grants[0].user_id='UBOB0001';self.reopen()
        with self.assertRaises(ValueError):self.adapter.reply(receipt['request_id'],self.transport)
        self.assertEqual(self.transport.calls,0)

    def test_resolved_old_event_uses_current_collection_without_historic_claim(self):
        self.adapter.events.ingest('lab-seoul',{**self.alert,'status':'resolved','endsAt':'1970-01-01T00:16:41Z'})
        self.fixture.time+=3600
        receipt,_=self.run_request(complete=True)
        self.adapter.reply(receipt['request_id'],self.transport)
        text=self.transport.last_payload['text']
        self.assertIn('경보 상태: resolved',text);self.assertIn('현재 관측은 같다고 가정하지 않습니다',text)
        self.assertIn('실제 자료 범위:',text)

    def test_expiry_maintenance_erases_event_context_and_blocks_collection(self):
        receipt,_=self.run_request()
        self.fixture.time+=RETENTION_SECONDS
        self.adapter.prune()
        with self.queue.transaction() as db:
            self.assertIsNone(db.execute('SELECT metadata FROM events WHERE event_id=?',(self.eid,)).fetchone()[0])
        entry=self.adapter._event_bridge().entry(receipt['request_id'])
        self.assertEqual(entry['event_id'],'');self.assertIsNone(entry['context'])
        self.assertIsNone(self.queue.claim())
        with self.assertRaises(ValueError):self.adapter.reply(receipt['request_id'],self.transport)

    def test_expired_known_target_gets_expiry_response_before_tombstone(self):
        self.fixture.time+=RETENTION_SECONDS
        receipt,result=self.run_request(self.command('status-event'))
        self.assertEqual(result['state'],'ready')
        self.adapter.reply(receipt['request_id'],self.transport)
        self.assertIn('만료',self.transport.last_payload['text'])
        self.assertEqual(self.count('jobs'),0)

    def test_scope_mutation_or_config_change_never_creates_new_target_authority(self):
        receipt,_=self.run_request(complete=True)
        self.fixture.settings.bindings.bindings[0].target.workload='different-app'
        with self.assertRaises(ValueError):self.adapter.reply(receipt['request_id'],self.transport)
        self.assertEqual(self.transport.calls,0)

    def test_recovery_words_remain_read_only_and_secrets_are_redacted(self):
        event=self.command();event['event']['text']+=' 복구해줘 token=private-secret-value'
        receipt,_=self.run_request(event,complete=True)
        self.adapter.reply(receipt['request_id'],self.transport)
        self.assertNotIn('private-secret-value',self.transport.last_payload['text'])
        for command in ('recover-event','approve-event','run-event'):
            with self.assertRaises(ValueError):self.receive(self.command(command))
        self.assertEqual(self.count('jobs'),1)

    def test_scoped_drain_does_not_start_unrelated_automatic_job(self):
        self.adapter.events.set_policy(expected_revision=0,delivery_ready=True,automatic_rules=[
            {'source_alias':'lab-seoul','target':self.fixture.target.alias,'alertname':'CacheHitRateLow',
             'channel_id':'CLOCAL001','audience':'fixture'}])
        self.adapter.events.ingest('lab-seoul',{**self.alert,'fingerprint':'5123456789abcdef'})
        self.assertEqual(self.count('event_requests'),1)
        self.run_request()
        self.assertEqual(self.count('jobs'),1)
        with self.queue.transaction() as db:
            self.assertEqual(db.execute("SELECT state FROM event_requests WHERE kind='automatic'").fetchone()[0],'pending')

    def test_disabled_runtime_config_and_registry_validation_are_offline(self):
        config=SlackWebConfig(bot_token_file='/unused',signing_secret_file='/unused',event_sources=self.sources)
        self.assertFalse(config.enabled);self.assertEqual(config.sources(),self.sources)
        for sources in ({'bad/alias':self.sources['lab-seoul']},
                        {'lab-seoul':self.sources['lab-seoul'],'copy':self.sources['lab-seoul']}):
            with self.assertRaises(ValueError):SlackWebConfig(bot_token_file='/unused',signing_secret_file='/unused',event_sources=sources)

    def test_revoked_reserved_request_cannot_escape_through_general_outbox_drain(self):
        self.receive(self.command())
        with patch.object(self.adapter.events,'drain',side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):self.adapter.drain_one()
        self.policy.grants[0].user_id='UBOB0001';self.reopen()
        self.assertEqual(self.adapter.events.drain(),[])
        self.assertEqual(self.count('jobs'),0)

    def test_policy_revocation_blocks_worker_claim_after_admission(self):
        self.run_request()
        self.policy.grants[0].user_id='UBOB0001';self.reopen()
        self.assertIsNone(self.queue.claim())
        with self.queue.transaction() as db:
            self.assertEqual(db.execute('SELECT reason FROM jobs').fetchone()[0],'event_request_authorization_changed')

    def test_disabling_event_connection_fences_already_queued_work(self):
        self.run_request()
        SlackAdapter(self.tasks,self.policy,SECRET)
        self.assertIsNone(self.queue.claim())

    def test_failed_foreign_installation_does_not_revoke_existing_authority(self):
        self.run_request()
        wrong=self.policy.model_copy(deep=True);wrong.app_id='AFOREIGN'
        with self.assertRaisesRegex(ValueError,'installation_mismatch'):
            SlackAdapter(self.tasks,wrong,SECRET,event_sources=self.sources)
        self.assertIsNotNone(self.queue.claim())

    def test_event_expiry_erases_younger_slack_copy(self):
        self.fixture.time+=3600
        receipt,_=self.run_request(complete=True)
        self.fixture.time+=RETENTION_SECONDS-3600
        self.adapter.prune()
        row=self.adapter._row(receipt['request_id'])
        self.assertEqual(row['state'],'expired');self.assertEqual(row['target'],'')
        self.assertIsNone(self.adapter._event_bridge().entry(receipt['request_id'])['context'])

    def test_schema_upgrade_fences_old_worker_and_keeps_existing_jobs(self):
        self.run_request()
        with self.queue.transaction() as db:
            self.assertEqual(db.execute('SELECT version FROM event_meta').fetchone()[0],2)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],3)
        self.reopen();self.assertEqual(self.count('jobs'),1)
        with self.queue.transaction() as db:
            db.execute('DROP TABLE event_request_scopes')
        with self.assertRaisesRegex(ValueError,'authorization schema missing'):
            self.queue.claim()
        with self.assertRaisesRegex(ValueError,'authorization schema missing'):
            self.reopen()

    def test_changed_same_slack_event_conflicts_instead_of_rebinding(self):
        event=self.command();self.receive(event)
        event['event']['text']+=' changed question'
        with self.assertRaisesRegex(ValueError,'conflict'):
            self.receive(event)
        self.assertEqual(len(self.rows('slack_requests')),1)

    def test_legacy_reply_payload_and_task_result_remain_compatible(self):
        receipt,queued=self.run_request(self.event,complete=True)
        with self.adapter._db() as db:
            db.execute('DELETE FROM slack_request_formats WHERE request_id=?',(receipt['request_id'],))
        old=self.adapter._payload(self.adapter._row(receipt['request_id']))
        self.assertNotIn('사건 미지정',old['text'])
        self.assertNotIn('observation',self.tasks.result(queued['task_id']))
        self.adapter.reply(receipt['request_id'],self.transport)
        self.reopen()
        self.assertEqual(self.adapter._payload(self.adapter._row(receipt['request_id'])),old)
        self.adapter.reply(receipt['request_id'],self.transport)
        self.assertEqual(self.transport.calls,1)

    def test_authorization_revocation_after_claim_prevents_collection(self):
        self.run_request()
        original=self.queue.claim
        def revoked():
            job=original()
            self.policy.grants[0].user_id='UBOB0001'
            self.reopen()
            return job
        worker=self.fixture.worker(collector=lambda *a,**k: self.fail('revoked collection'))
        with patch.object(worker.queue,'claim',side_effect=revoked):
            result=worker.run_once()
        self.assertEqual(result['state'],'cancelled')

    def test_review_policy_crash_cannot_keep_old_queue_authority(self):
     self.run_request()
     self.policy.grants[0].user_id='UBOB0001'
     with patch.object(EventStore,'set_request_authority',side_effect=KeyboardInterrupt()):
      with self.assertRaises(KeyboardInterrupt): self.reopen()
     self.assertIsNone(self.queue.claim())
    def test_review_disabled_adapter_still_expires_event_copy(self):
     self.fixture.time+=3600
     receipt,_=self.run_request(complete=True)
     self.adapter=SlackAdapter(self.tasks,self.policy,SECRET)
     self.fixture.time+=RETENTION_SECONDS-3600
     self.adapter.prune()
     self.assertIsNone(self.adapter._event_bridge().entry(receipt['request_id'])['context'])


if __name__=='__main__':unittest.main()
