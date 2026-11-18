"""WF-02 actual SQLite/queue/worker tests, no external services."""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import test_job_queue as fixtures
from ai_ops.alerts import AlertInbox
from ai_ops.event_store import EventStore
from ai_ops.job_queue import JobQueue, QueueFull, RETENTION_SECONDS


class EventStoreTest(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.JobQueueTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.q, self.settings = self.f.queue, self.f.settings
        self.sources = {'lab-seoul':{'source_id':'am-lab','cluster_id':self.f.target.target.cluster_id}}
        self.store = EventStore(self.q,self.settings,sources=self.sources)

    def alert(self, **changes):
        item = {'labels':{**self.f.target.target.model_dump(),'alertname':'CacheHitRateLow','ai_ops':'enabled'},
                'status':'firing','fingerprint':'0123456789abcdef','startsAt':'1970-01-01T00:16:40Z',
                'annotations':{'summary':'token=do-not-store-secret'},'generatorURL':'https://untrusted.invalid/'}
        item.update(changes)
        return item

    def ingest(self, item=None):
        return self.store.ingest('lab-seoul',item or self.alert())

    def request(self, event, request_id='req-1', **changes):
        args = dict(request_id=request_id,question='현재 상태 token=question-secret',subject='user-1',allowed_targets=[self.f.target.alias])
        args.update(changes)
        return self.store.request(event,**args)

    def enable(self):
        return self.store.set_policy(expected_revision=0,delivery_ready=True,automatic_rules=[self.rule()])

    def rule(self):
        return {'source_alias':'lab-seoul','target':self.f.target.alias,'alertname':'CacheHitRateLow','channel_id':'CLOCAL001','audience':'lab-operators'}

    def count(self, table):
        with self.q.transaction() as db:
            return db.execute('SELECT count(*) FROM '+table).fetchone()[0]

    def test_manual_default_and_private_metadata(self):
        event = self.ingest()
        self.assertRegex(event,r'^evt-[0-9a-f]{64}$')
        self.assertEqual(self.count('jobs'),0)
        self.assertEqual(self.count('event_requests'),0)
        self.assertEqual(self.store.show(event)['notification'],'unknown')
        with self.q.transaction() as db:
            metadata=db.execute('SELECT metadata FROM events').fetchone()[0]
            self.assertNotIn('do-not-store-secret',metadata)
            self.assertNotIn('untrusted.invalid',metadata)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],3)
        self.assertEqual(self.q.path.stat().st_mode & 0o777,0o600)

    def test_offset_equivalence_nanos_and_reference(self):
        first=self.ingest()
        self.assertEqual(first,self.ingest(self.alert(startsAt='1970-01-01T09:16:40+09:00')))
        self.assertNotEqual(first,self.ingest(self.alert(startsAt='1970-01-01T00:16:40.000000001Z')))
        self.assertEqual(first,self.store.resolve_reference('al1/lab-seoul/0123456789abcdef/1970-01-01T09:16:40+09:00'))
        with self.assertRaises(ValueError): self.store.resolve_reference('https://untrusted.invalid/')

    def test_identity_conflict_does_not_overwrite(self):
        event=self.ingest()
        item=self.alert(); item['labels']['severity']='critical'
        with self.assertRaisesRegex(ValueError,'conflict'): self.ingest(item)
        self.assertEqual(self.store.show(event)['status'],'firing')

    def test_auth_source_cluster_heartbeat_validation(self):
        with self.assertRaises(ValueError): self.store.ingest('foreign',self.alert())
        for key,value in [('cluster_id','foreign'),('alertname','Watchdog')]:
            item=self.alert(); item['labels'][key]=value
            with self.assertRaises(ValueError): self.ingest(item)
        self.assertEqual(self.count('events'),0)

    def test_unbound_events_no_investigation(self):
        item=self.alert(); item['labels']={'alertname':'NodeDown'}
        event=self.ingest(item)
        self.assertIsNone(self.store.show(event)['target'])
        with self.assertRaises(ValueError): self.request(event)
        self.assertEqual(self.count('jobs'),0)

    def test_resolved_first_and_late_firing_suppressed(self):
        self.enable()
        event=self.ingest(self.alert(status='resolved',endsAt='1970-01-01T00:16:41Z'))
        self.ingest()
        self.assertEqual(self.store.show(event)['status'],'resolved')
        self.assertEqual(self.count('event_requests'),0)

    def test_auto_exact_rules_and_once_after_reopen(self):
        self.enable(); event=self.ingest()
        self.ingest()
        reopened=EventStore(JobQueue(self.f.store,clock=lambda:self.f.time),self.settings,sources=self.sources)
        reopened.ingest('lab-seoul',self.alert())
        jobs=reopened.drain()
        self.assertEqual(len(jobs),1)
        self.assertEqual(reopened.drain(),[])
        self.assertEqual(self.store.show(event)['requests'][0]['job_id'],jobs[0])
        item=self.alert(fingerprint='1123456789abcdef'); item['labels']['alertname']='HighErrorRate'
        self.ingest(item)
        self.assertEqual(self.count('event_requests'),1)

    def test_auto_needs_label_and_no_retroactive_work(self):
        self.ingest()
        self.f.time+=1; self.enable()
        self.ingest()
        item=self.alert(fingerprint='2123456789abcdef',startsAt='1970-01-01T00:16:41Z')
        del item['labels']['ai_ops']
        self.ingest(item)
        self.assertEqual(self.count('event_requests'),0)
        # First receipt of an old occurrence is not activation of a new episode.
        self.ingest(self.alert(fingerprint='3123456789abcdef'))
        self.assertEqual(self.count('event_requests'),0)

    def test_policy_validation_and_cas(self):
        with self.assertRaises(ValueError): self.store.set_policy(expected_revision=0,automatic_rules=[self.rule()])
        for key,value in [('target','*'),('alertname','NodeDown'),('channel_id','*'),('source_alias','*')]:
            rule=self.rule(); rule[key]=value
            with self.assertRaises(ValueError): self.store.set_policy(expected_revision=0,automatic_rules=[rule],delivery_ready=True)
        self.enable()
        with self.assertRaisesRegex(ValueError,'stale'): self.store.set_policy(expected_revision=0)

    def test_policy_change_before_delivery_cancels_pending(self):
        self.enable(); event=self.ingest()
        self.store.set_policy(expected_revision=1)
        self.assertEqual(self.store.drain(),[])
        self.assertEqual(self.store.show(event)['requests'][0]['state'],'cancelled')

    def test_policy_and_resolution_guard_before_worker_collection(self):
        self.enable(); event=self.ingest(); job=self.store.drain()[0]
        self.store.set_policy(expected_revision=1)
        with patch.object(self.f.worker(),'collector',side_effect=AssertionError('must not collect')) as collector:
            self.assertIsNone(self.f.worker(collector=collector).run_once())
        self.assertEqual(self.q.show(job)['reason'],'event_policy_changed')
        self.assertEqual(self.q.show(job)['attempts'],0)

    def test_resolved_between_claim_and_collection_fenced(self):
        self.enable(); self.ingest(); job=self.store.drain()[0]
        original=self.q.claim
        def claim_then_resolve():
            result=original()
            self.ingest(self.alert(status='resolved',endsAt='1970-01-01T00:16:41Z'))
            return result
        with patch.object(self.q,'claim',side_effect=claim_then_resolve):
            result=self.f.worker(collector=lambda *a,**kw:self.fail('must not collect')).run_once()
        self.assertEqual(result['state'],'cancelled')
        self.assertEqual(self.q.show(job)['reason'],'event_resolved')

    def test_manual_unknown_alert_resolved_hour_later_runs_real_worker(self):
        item=self.alert(status='resolved',endsAt='1970-01-01T00:16:41Z'); item['labels']['alertname']='CustomServiceAlert'
        event=self.ingest(item); self.f.time+=3600
        self.request(event); job=self.store.drain()[0]
        result=self.f.worker().run_once()
        self.assertEqual(result['job_id'],job)
        self.assertEqual(result['state'],'partial')
        self.assertIsNotNone(result['incident_id'])
        self.assertEqual(self.store.show(event)['status'],'resolved')
        self.assertEqual(self.store.show(event)['starts_at'],'1970-01-01T00:16:40.000000000Z')

    def test_requests_dedup_conflict_and_fresh_report(self):
        event=self.ingest(); key=self.request(event)
        self.assertEqual(key,self.request(event))
        for changes in ({'question':'different'},{'subject':'user-2'},{'allowed_targets':[]}):
            with self.assertRaises(ValueError): self.request(event,**changes)
        job=self.store.drain()[0]; self.f.worker().run_once()
        self.assertEqual(self.request(event),key)
        self.request(event,request_id='req-2')
        new=self.store.drain()[0]
        self.assertNotEqual(job,new)
        with self.q.transaction() as db:
            self.assertTrue(all(r[0] is None for r in db.execute('SELECT payload FROM event_requests')))
            self.assertNotIn('question-secret',db.execute('SELECT payload FROM jobs LIMIT 1').fetchone()[0])

    def test_missing_expired_and_registry_changes(self):
        with self.assertRaisesRegex(ValueError,'not registered'): self.store.resolve_reference('evt-'+'0'*64)
        event=self.ingest(); self.f.time+=RETENTION_SECONDS
        with self.assertRaisesRegex(ValueError,'expired'): self.request(event)
        changed=copy.deepcopy(self.sources); changed['lab-seoul']['source_id']='changed'
        with self.assertRaises(ValueError): EventStore(self.q,self.settings,sources=changed)

    def test_transaction_failure_after_queue_insert_rolls_back_then_retry(self):
        event=self.ingest(); self.request(event)
        original=self.q._submit
        def fail_after_insert(*a,**kw):
            original(*a,**kw)
            raise RuntimeError('simulated crash before commit')
        with patch.object(self.q,'_submit',side_effect=fail_after_insert):
            with self.assertRaises(RuntimeError): self.store.drain()
        self.assertEqual(self.count('jobs'),0)
        self.assertEqual(self.store.show(event)['requests'][0]['state'],'pending')
        job=self.store.drain()[0]
        reopened=EventStore(JobQueue(self.f.store,clock=lambda:self.f.time),self.settings,sources=self.sources)
        self.assertEqual(reopened.drain(),[])
        self.assertEqual(reopened.show(event)['requests'][0]['job_id'],job)

    def test_reservation_failure_rolls_back_event(self):
        self.enable()
        with patch.object(self.store,'_reserve',side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError): self.ingest()
        self.assertEqual(self.count('events'),0)

    def test_concurrent_receipts_and_drains(self):
        self.enable(); errors=[]
        def run():
            try:
                other=EventStore(JobQueue(self.f.store,clock=lambda:self.f.time),self.settings,sources=self.sources)
                other.ingest('lab-seoul',self.alert()); other.drain()
            except Exception as error: errors.append(error)
        threads=[threading.Thread(target=run) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors,[])
        self.assertEqual(self.count('events'),1); self.assertEqual(self.count('jobs'),1)

    def test_capacity_rollback_leaves_pending_for_retry(self):
        event=self.ingest(); self.request(event)
        with patch('ai_ops.job_queue.MAX_WAITING',0):
            with self.assertRaises(QueueFull): self.store.drain()
        self.assertEqual(self.store.show(event)['requests'][0]['state'],'pending')
        self.assertEqual(len(self.store.drain()),1)
        with patch('ai_ops.event_store.MAX_EVENTS',1):
            with self.assertRaises(QueueFull): self.ingest(self.alert(fingerprint='a123456789abcdef'))

    def test_retention_preview_tombstone_and_active_defer(self):
        event=self.ingest(); self.request(event); job=self.store.drain()[0]
        claimed=self.q.claim()
        self.f.time+=RETENTION_SECONDS
        self.assertEqual(self.store.expire(apply=True)['deferred'],[event])
        self.assertEqual(self.q.show(job)['state'],'collecting')
        self.q.cancel(job)
        self.assertEqual(self.store.expire()['expired'],[event])
        with self.q.transaction() as db: self.assertIsNotNone(db.execute('SELECT metadata FROM events').fetchone()[0])
        self.store.expire(apply=True)
        with self.q.transaction() as db:
            self.assertIsNone(db.execute('SELECT metadata FROM events').fetchone()[0])
            self.assertIsNone(db.execute('SELECT fingerprint FROM event_requests').fetchone()[0])
        with self.assertRaisesRegex(ValueError,'expired'): self.ingest()
        self.assertEqual(self.count('jobs'),1)  # Existing Retention owns evidence/queue deletion.

    def test_expired_queued_work_is_cancelled_before_collect(self):
        event=self.ingest(); self.request(event); job=self.store.drain()[0]
        self.f.time+=RETENTION_SECONDS
        self.store.expire(apply=True)
        self.assertEqual(self.q.show(job)['reason'],'event_expired')
        self.assertIsNone(self.q.claim())

    def legacy(self, item=None):
        item=item or self.alert()
        inbox=AlertInbox(self.q,self.settings,cluster_id=self.f.target.target.cluster_id)
        body=json.dumps({'version':'4','receiver':'ai-investigation','status':item['status'],'alerts':[item]}).encode()
        inbox.accept(body)
        return inbox,body

    def test_verified_legacy_migration_preserves_job_and_duplicate_key(self):
        inbox,body=self.legacy()
        with self.q.transaction() as db: before=dict(db.execute('SELECT * FROM jobs').fetchone())
        with self.assertRaisesRegex(ValueError,'migration'): self.ingest()
        event=self.store.migrate_legacy('lab-seoul',self.alert())
        self.assertEqual(event,self.store.migrate_legacy('lab-seoul',self.alert()))
        self.ingest(); inbox.accept(body)
        with self.q.transaction() as db: self.assertEqual(dict(db.execute('SELECT * FROM jobs').fetchone()),before)
        self.assertEqual(self.count('jobs'),1)
        self.assertEqual(self.store.show(event)['requests'][0]['job_id'],before['job_id'])
        # Legacy work is preserved, but cannot start under an implicit new policy.
        self.assertIsNone(self.q.claim())
        self.assertEqual(self.q.show(before['job_id'])['reason'],'legacy_requires_manual_request')

    def test_legacy_conflict_and_missing_replay_leave_original_untouched(self):
        self.legacy()
        item=self.alert(); item['labels']['severity']='critical'
        with self.assertRaises(ValueError): self.store.migrate_legacy('lab-seoul',item)
        self.assertEqual(self.count('events'),0); self.assertEqual(self.count('jobs'),1)
        self.assertEqual(self.count('event_legacy'),0)

    def test_legacy_resolved_first_has_no_job_and_cannot_reactivate(self):
        item=self.alert(status='resolved',endsAt='1970-01-01T00:16:41Z')
        self.legacy(item)
        event=self.store.migrate_legacy('lab-seoul',self.alert())
        self.enable(); self.ingest()
        self.assertEqual(self.store.show(event)['status'],'resolved')
        self.assertEqual(self.count('jobs'),0)

    def test_original_queue_path_remains_usable_after_extension(self):
        job,_=self.f.submit()
        self.assertEqual(self.f.worker().run_once()['job_id'],job)
        reopened=JobQueue(self.f.store)
        with reopened.transaction() as db: self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],3)

    def test_envelope_resolved_first_and_atomic_validation(self):
        self.enable()
        body={'version':'4','receiver':'ai-events','status':'firing','alerts':[
            self.alert(),self.alert(status='resolved',endsAt='1970-01-01T00:16:41Z')]}
        ids=self.store.accept('lab-seoul',json.dumps(body).encode())
        self.assertEqual(ids[0],ids[1]); self.assertEqual(self.count('jobs'),0)
        self.assertEqual(self.count('event_requests'),0)
        for value in (1,True):
            body['truncatedAlerts']=value
            with self.assertRaises(ValueError): self.store.accept('lab-seoul',json.dumps(body).encode())
        body.pop('truncatedAlerts')
        body['alerts']=[self.alert(fingerprint='a123456789abcdef'),self.alert(fingerprint='invalid')]
        with self.assertRaises(ValueError): self.store.accept('lab-seoul',json.dumps(body).encode())
        self.assertEqual(self.count('events'),1)

    def test_stale_config_and_unknown_schema_fail_closed(self):
        event=self.ingest(); self.request(event)
        with self.q.transaction() as db:
            db.execute("UPDATE event_policy SET config_hash='changed'")
        self.assertEqual(self.store.drain(),[])
        with self.q.transaction() as db: db.execute('UPDATE event_meta SET version=99')
        self.f.submit()
        with self.assertRaisesRegex(ValueError,'schema'): self.q.claim()

    def test_abrupt_process_exit_rolls_back_partial_queue_delivery(self):
        event=self.ingest(); self.request(event)
        root=Path(__file__).resolve().parents[1]
        script = """
import os
from pathlib import Path
from ai_ops.job_queue import JobQueue
from ai_ops.event_store import EventStore
from ai_ops.b_contracts import load_b_settings
root=Path(os.environ['WF02_REPO'])
settings=load_b_settings(root/'config/targets.practice.example.json',root/'config/b-runtime.example.json',root/'config/metric-queries.json')
q=JobQueue(Path(os.environ['AI_OPS_FILES_ROOT'])/'store',clock=lambda:1000.0)
s=EventStore(q,settings,sources={'lab-seoul':{'source_id':'am-lab','cluster_id':settings.bindings.bindings[0].target.cluster_id}})
original=q._submit
def crash(*a,**kw):
    original(*a,**kw)
    os._exit(77)
q._submit=crash
s.drain()
"""
        env={**os.environ,'WF02_REPO':str(root)}
        result=subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,77,result.stderr.decode())
        self.assertEqual(self.count('jobs'),0)
        self.assertEqual(len(self.store.drain()),1)

    def test_retention_remains_compatible_with_queue_purge(self):
        from ai_ops.retention import QueueRetention
        event=self.ingest(); self.request(event); job=self.store.drain()[0]
        self.f.worker().run_once()
        self.f.time+=RETENTION_SECONDS+1
        self.store.expire(apply=True)
        QueueRetention(self.q).run(apply=True,job_id=job)
        self.assertEqual(self.q.show(job)['state'],'purged')
        with self.assertRaisesRegex(ValueError,'expired'): self.ingest()
        self.assertEqual(self.count('event_requests'),1)

    def test_partial_queue_capacity_delivers_prefix_and_retries_remainder(self):
        event=self.ingest()
        self.request(event,request_id='first')
        self.request(event,request_id='second')
        blocker,_=self.f.submit()
        with patch('ai_ops.job_queue.MAX_WAITING',2):
            delivered=self.store.drain()
            self.assertEqual(len(delivered),1)
            states=[r['state'] for r in self.store.show(event)['requests']]
            self.assertCountEqual(states,['delivered','pending'])
            self.q.cancel(blocker)
            remaining=self.store.drain()
            self.assertEqual(len(remaining),1)
            self.assertNotEqual(delivered,remaining)
            self.assertEqual(self.store.drain(),[])

    def test_allowed_targets_rejects_string_substring_and_malformed_acl(self):
        event=self.ingest()
        alias=self.f.target.alias
        for acl in ('prefix-'+alias+'-suffix',alias,{alias:True},None,[alias,1]):
            with self.subTest(acl=acl), self.assertRaises(ValueError):
                self.request(event,allowed_targets=acl)
        self.assertEqual(self.count('event_requests'),0)
        with self.assertRaises(ValueError): self.request(event,allowed_targets=['prefix-'+alias+'-suffix'])
        self.request(event,allowed_targets=frozenset([alias]))
        self.assertEqual(len(self.store.drain()),1)

    def test_repeat_migration_merges_new_resolution_without_replacing_job(self):
        self.legacy()
        event=self.store.migrate_legacy('lab-seoul',self.alert())
        before=self.store.show(event)['requests']
        self.store.migrate_legacy('lab-seoul',self.alert(status='resolved',endsAt='1970-01-01T00:16:42Z'))
        self.store.migrate_legacy('lab-seoul',self.alert())
        self.assertEqual(self.store.show(event)['status'],'resolved')
        self.assertEqual(self.store.show(event)['ends_at'],'1970-01-01T00:16:42.000000000Z')
        self.assertEqual(self.store.show(event)['requests'],before)
        self.assertEqual(self.count('jobs'),1)
        self.f.time+=RETENTION_SECONDS
        self.store.expire(apply=True)
        with self.assertRaisesRegex(ValueError,'expired'):
            self.store.migrate_legacy('lab-seoul',self.alert())


if __name__=='__main__': unittest.main()
