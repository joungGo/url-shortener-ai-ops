from copy import deepcopy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import test_recovery_commands as command_fixture
import test_recovery_pr as pr_fixture
from ai_ops.contracts import sha256_json
from ai_ops.recovery_effects import (ExecutionReceipt,EffectPolicy,EffectBatch,EffectCoordinator,
    CleanupItem,RollbackScope,command_effect_source,pr_effect_source,verify_effect,body)

ACTOR=command_fixture.ACTOR

def policy_for(e,start=None):
    start=start or e.applied_at+timedelta(seconds=1)
    return EffectPolicy(window_start=start,window_end=start+timedelta(seconds=120),
        minimum_span_seconds=120,maximum_gap_seconds=45,minimum_samples=4,maximum_report_delay_seconds=120,
        read_seconds=15,http_paths=['/readyz','/items'],
        metrics=[dict(name='error_rate',definition_hash='f'*64,comparison='lte',threshold=.05)],
        alerts=[dict(name='WorkloadUnavailable',definition_hash='a'*64)])


def batch_for(e,p):
    runtime=dict(kind='runtime',revision=e.revision,application_uid=e.application_uid,digest=e.digest,
        generation=e.generation or 3,observed_generation=e.generation or 3,replicas=1,ready_replicas=1,
        pod_uid_hashes=['c'*64],ready_pods=1,marker=e.marker,argo_synced_healthy=True if e.method=='pr' else None)
    data=[]
    for kind,name,reading,definition in [('runtime','lab-api',runtime,None),
        ('http','/readyz',dict(kind='http',status_code=200,response_contract_valid=True),None),
        ('http','/items',dict(kind='http',status_code=200,response_contract_valid=True),None),
        ('metric','error_rate',dict(kind='metric',value=.01),'f'*64),
        ('alert','WorkloadUnavailable',dict(kind='alert',rule_present=True,evaluation='ok',active_count=0),'a'*64)]:
        samples=[]
        for n in range(0,121,30):
            at=p.window_start+timedelta(seconds=n);value=deepcopy(reading)
            if kind=='alert':value['evaluated_at']=at.isoformat()
            samples.append(dict(at=at.isoformat(),reading=value))
        data.append(dict(kind=kind,name=name,target=body(e.target),deployment_uid=e.deployment_uid,
            execution_hash=e.digest_key(),definition_hash=definition,source_hash=sha256_json(samples),status='available',samples=samples))
    return EffectBatch.model_validate(dict(series=data))


class CleanupPort:
    mode='local_fixture'
    def __init__(self,items):self.items={i.item_hash:i.owner_hash for i in items};self.writes=0;self.lost=False;self.hook=None
    def inspect(self,item,*,deadline):
        return {'present':True,'owner_hash':self.items[item.item_hash]} if item.item_hash in self.items else {'present':False}
    def dispose(self,item,*,deadline):
        self.writes+=1;self.items.pop(item.item_hash,None)
        if self.hook:self.hook()
        if self.lost:raise OSError('secret-token-response')


class EffectTest(unittest.TestCase):
    def setUp(self):
        self.f=command_fixture.RestartCommandTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.consent();self.f.execute()
        self.source=command_effect_source(self.f.coordinator);self.e=self.source(self.f.key,ACTOR)
        self.policy=policy_for(self.e);self.batch=batch_for(self.e,self.policy)
        self.f.f.fixture.time=(self.policy.window_end+timedelta(seconds=1)).timestamp()
        self.c=EffectCoordinator(self.f.f.queue,source=self.source,policy=self.policy)
        self.key=self.f.key;self.item=CleanupItem(kind='credential',item_hash='1'*64,owner_hash='2'*64,action='revoke_credential')
        self.c.open(self.key,actor=ACTOR,cleanup_items=[self.item]);self.port=CleanupPort([self.item])
        self.reader=SimpleNamespace(mode='local_fixture',read=lambda e,p,deadline:self.batch)
    def evaluate(self,batch=None,e=None,policy=None):return verify_effect(e or self.e,policy or self.policy,batch or self.batch,now=self.c.now())
    def run_verify(self):return self.c.verify_local(self.key,actor=ACTOR,reader=self.reader)
    def test_complete_command_effect_is_separate_from_application_and_cleanup(self):
        self.assertFalse(self.c.show(self.key,actor=ACTOR)['service_resolved'])
        with patch('socket.socket.connect',side_effect=AssertionError('network')),patch('subprocess.Popen',side_effect=AssertionError('shell')):
            r=self.run_verify()
        self.assertEqual(r['state'],'succeeded');self.assertTrue(r['service_resolved']);self.assertFalse(r['cleanup_complete'])
        self.assertFalse(r['execution_allowed']);self.assertFalse(r['slack_delivered']);self.assertEqual(self.f.port.writes,1)
    def test_partial_metrics_and_empty_alert_are_unknown(self):
        for index,change in [(3,{'status':'missing','samples':[]}),(4,{'samples':[]})]:
            d=body(self.batch);d['series'][index].update(change)
            self.assertEqual(self.evaluate(EffectBatch.model_validate(d))['status'],'verification_unknown')
    def test_http_failure_with_missing_metrics_remains_unresolved(self):
        d=body(self.batch);d['series'][1]['samples'][0]['reading']['status_code']=503;d['series'].pop(3)
        self.assertEqual(self.evaluate(EffectBatch.model_validate(d))['status'],'unresolved')
    def test_same_pod_or_wrong_marker_revision_image_generation_are_not_recovery(self):
        for changes in [{'pod_uid_hashes':[self.e.old_pod_uid_hash]},{'marker':'0'*64},{'revision':'0'*64},
                        {'digest':'sha256:'+'0'*64},{'generation':99},{'ready_pods':0}]:
            d=body(self.batch);d['series'][0]['samples'][-1]['reading'].update(changes)
            self.assertEqual(self.evaluate(EffectBatch.model_validate(d))['status'],'unresolved')
    def test_wrong_identity_and_query_are_not_failure_evidence(self):
        for i,field,value in [(0,'deployment_uid','bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'),(3,'definition_hash','0'*64),(1,'execution_hash','0'*64)]:
            d=body(self.batch);d['series'][i][field]=value
            self.assertEqual(self.evaluate(EffectBatch.model_validate(d))['status'],'verification_unknown')
    def test_old_duplicate_gapped_and_short_samples_are_unknown(self):
        for mode in ['old','duplicate','gap','short']:
            d=body(self.batch);s=d['series'][1]['samples']
            if mode=='old':s[0]['at']=(self.policy.window_start-timedelta(seconds=1)).isoformat()
            elif mode=='duplicate':s[1]['at']=s[0]['at']
            elif mode=='gap':s.pop(2)
            else:s.pop()
            self.assertEqual(self.evaluate(EffectBatch.model_validate(d))['status'],'verification_unknown')
    def test_nonfinite_metrics_and_unknown_rules_not_zero(self):
        for value in [None,float('nan'),float('inf')]:
            d=body(self.batch);d['series'][3]['samples'][0]['reading']['value']=value
            if value is None:self.assertEqual(self.evaluate(EffectBatch.model_validate(d))['status'],'verification_unknown')
            else:
                with self.assertRaises(ValueError):EffectBatch.model_validate(d)
        d=body(self.batch);d['series'][4]['samples'][0]['reading']['evaluation']='no_data'
        self.assertEqual(self.evaluate(EffectBatch.model_validate(d))['status'],'verification_unknown')
    def test_no_applied_receipt_cannot_be_success(self):
        e=self.e.model_copy(update={'applied':False,'applied_at':None})
        self.assertEqual(self.evaluate(e=e)['status'],'verification_unknown')
    def test_report_stale_or_future_window_is_unknown(self):
        for now in [self.policy.window_start,self.policy.window_end+timedelta(seconds=121)]:
            self.assertEqual(verify_effect(self.e,self.policy,self.batch,now=now)['status'],'verification_unknown')
    def test_old_c04_bounds_not_relaxed(self):
        for changes in [{'minimum_span_seconds':119},{'read_seconds':16},{'maximum_gap_seconds':46}]:
            with self.assertRaises(ValueError):EffectPolicy.model_validate(body(self.policy)|changes)
    def test_resolved_alert_still_allows_effect_read_not_restart_execution(self):
        self.f.f.adapter.events.ingest('lab-seoul',{**self.f.f.alert,'status':'resolved','endsAt':self.c.now().isoformat()})
        self.assertTrue(self.run_verify()['service_resolved'])
        with self.assertRaises(ValueError):self.f.coordinator.load(self.key,ACTOR)
    def test_other_actor_or_missing_original_source_denied(self):
        with self.assertRaises(ValueError):self.c.show(self.key,actor='UBOB0001')
        with patch.object(self.f.f.tasks,'result',return_value={'state':'unavailable'}):
            with self.assertRaises(ValueError):self.run_verify()
    def test_idempotent_reopen_does_not_extend_window_or_duplicate_read(self):
        self.run_verify();self.c=EffectCoordinator(self.f.f.queue,source=self.source,policy=self.policy)
        self.c.open(self.key,actor=ACTOR,cleanup_items=[self.item])
        self.reader.read=lambda *a,**k:(_ for _ in ()).throw(AssertionError('second read'))
        self.assertTrue(self.run_verify()['service_resolved'])
        with self.assertRaises(ValueError):self.c.open(self.key,actor=ACTOR,cleanup_items=[])
    def test_policy_change_does_not_reuse_result(self):
        changed=self.policy.model_copy(update={'minimum_samples':5})
        other=EffectCoordinator(self.f.f.queue,source=self.source,policy=changed)
        with self.assertRaises(ValueError):other.show(self.key,actor=ACTOR)
    def test_timeout_or_exception_sanitized_unknown(self):
        def slow(*a,**k):self.f.f.fixture.time+=16;return self.batch
        self.reader.read=slow;r=self.run_verify();self.assertEqual(r['state'],'verification_unknown')
        self.assertFalse(r['service_resolved'])
    def test_cancellation_during_read_cannot_be_overwritten(self):
        def cancel(*a,**k):self.c.cancel(self.key,actor=ACTOR);return self.batch
        self.reader.read=cancel;r=self.run_verify();self.assertEqual(r['state'],'cancelled');self.assertFalse(r['service_resolved'])
    def test_read_only_followup_never_restarts_command(self):
        d=body(self.batch);d['series'][1]['samples'][0]['reading']['status_code']=500;self.batch=EffectBatch.model_validate(d)
        self.run_verify();result=self.c.follow_up(self.key,actor=ACTOR)
        self.assertEqual(result['decision'],'manual_handoff');self.assertFalse(result['execution_allowed']);self.assertEqual(self.f.port.writes,1)
    def test_cleanup_records_only_confirmed_owned_absence(self):
        r=self.c.cleanup_local(self.key,actor=ACTOR,port=self.port)
        self.assertTrue(r['cleanup_complete']);self.assertEqual(self.port.writes,1)
        self.c.cleanup_local(self.key,actor=ACTOR,port=self.port);self.assertEqual(self.port.writes,1)
    def test_wrong_owner_never_disposed(self):
        self.port.items[self.item.item_hash]='9'*64
        r=self.c.cleanup_local(self.key,actor=ACTOR,port=self.port)
        self.assertFalse(r['cleanup_complete']);self.assertEqual(self.port.writes,0);self.assertEqual(r['cleanup'][0]['state'],'not_owned')
    def test_unknown_cleanup_write_is_read_only_reconciled(self):
        self.port.lost=True;r=self.c.cleanup_local(self.key,actor=ACTOR,port=self.port)
        self.assertFalse(r['cleanup_complete']);self.assertNotIn('secret',str(r))
        r=self.c.cleanup_local(self.key,actor=ACTOR,port=self.port,read_only=True)
        self.assertTrue(r['cleanup_complete']);self.assertEqual(self.port.writes,1)
    def test_cleanup_process_interrupt_is_not_replayed(self):
        self.port.hook=lambda:(_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):self.c.cleanup_local(self.key,actor=ACTOR,port=self.port)
        r=self.c.cleanup_local(self.key,actor=ACTOR,port=self.port)
        self.assertTrue(r['cleanup_complete']);self.assertEqual(self.port.writes,1)
    def test_live_reader_and_cleanup_not_enabled(self):
        self.reader.mode='live';self.port.mode='live'
        with self.assertRaises(ValueError):self.run_verify()
        with self.assertRaises(ValueError):self.c.cleanup_local(self.key,actor=ACTOR,port=self.port)
    def test_retention_prunes_records_not_original_source_or_other_data(self):
        self.run_verify();self.f.f.fixture.time=self.e.retained_until.timestamp();self.c.prune()
        with self.f.f.queue.transaction() as db:
            r=self.c._get(db,self.key);self.assertTrue(r['expired']);self.assertIsNone(r['result']);self.assertEqual(r['cleanup'],[])
            self.assertEqual(db.execute('SELECT count(*) FROM restart_plans').fetchone()[0],1)
        with self.assertRaises(ValueError):self.c.show(self.key,actor=ACTOR)
    def test_original_approval_cancellation_is_preserved_in_effects_and_followup(self):
        # Cancel just after application, before the observation-window time jump.
        observation_time=self.f.f.fixture.time
        self.f.f.fixture.time=self.e.applied_at.timestamp()
        a=self.f.ledger.show(self.key,actor=ACTOR)
        self.f.ledger.decide(self.key,actor=ACTOR,approval_hash=a['approval_hash'],generation=a['generation'],
            request_id='wf09-cancel',decision='cancel')
        self.f.f.fixture.time=observation_time
        r=self.run_verify();self.assertEqual(r['state'],'cancelled');self.assertTrue(r['cancel_requested'])
        r=self.c.follow_up(self.key,actor=ACTOR)
        self.assertEqual(r['decision'],'manual_handoff');self.assertEqual(r['reason'],'cancelled_no_follow_up')

    def test_concurrent_cleanup_disposes_at_most_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        barrier=Barrier(2)
        def run(_):
            barrier.wait()
            return self.c.cleanup_local(self.key,actor=ACTOR,port=self.port)
        # Keep the WAL open while racing coordinators, so this test exercises
        # disposal reservation rather than SQLite sidecar teardown/file checks.
        import sqlite3
        keeper=sqlite3.connect(self.f.f.queue.path)
        try:
            keeper.execute('SELECT count(*) FROM recovery_effects').fetchone()
            with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(run,range(2)))
        finally:keeper.close()
        self.assertEqual(self.port.writes,1)
        self.assertTrue(self.c.show(self.key,actor=ACTOR)['cleanup_complete'])

    def test_cleanup_clock_regression_stops_before_disposal(self):
        original=self.port.inspect
        def backwards(item,*,deadline):
            self.f.f.fixture.time-=1
            return original(item,deadline=deadline)
        self.port.inspect=backwards
        self.assertFalse(self.c.cleanup_local(self.key,actor=ACTOR,port=self.port)['cleanup_complete'])
        self.assertEqual(self.port.writes,0)

    def test_result_text_is_same_event_no_delivery_claim(self):
        self.run_verify();text=self.c.result_text(self.key,actor=ACTOR)
        self.assertIn(self.e.event_id,text);self.assertIn(self.e.job_id,text);self.assertIn('로컬 합성',text)


class PREffectTest(unittest.TestCase):
    def setUp(self):
        self.f=pr_fixture.RecoveryPRTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
        locked,request,response=self.f.merged_local()
        self.f.bridge.plan_sync(self.f.key,actor=ACTOR,merge_response=response,live_reader=self.f.live)
        snapshot=dict(checked_at=self.f.now().isoformat(),application=deepcopy(self.f.raw.app),
            deployment=deepcopy(self.f.raw.deployment),lease=deepcopy(self.f.raw.lease))
        app=snapshot['application'];app['spec']['source']['targetRevision']=response['sha']
        app['status'].update(sync={'status':'Synced','revision':response['sha']},operationState={'phase':'Succeeded','syncResult':{'revision':response['sha']}})
        snapshot['deployment']['spec']['template']['spec']['containers'][0]['image']=self.f.raw.binding.app.image
        self.f.bridge.record_sync(self.f.key,actor=ACTOR,snapshot=snapshot)
        self.source=pr_effect_source(self.f.bridge);self.e=self.source(self.f.key,ACTOR)
        self.policy=policy_for(self.e);self.f.f.fixture.time=(self.policy.window_end+timedelta(seconds=1)).timestamp()
        self.c=EffectCoordinator(self.f.f.queue,source=self.source,policy=self.policy);self.key=self.f.key
        item=CleanupItem(kind='lease',item_hash=self.e.lease_hash,owner_hash='2'*64,action='release_lease')
        self.c.open(self.key,actor=ACTOR,cleanup_items=[item]);self.batch=batch_for(self.e,self.policy)
        self.reader=SimpleNamespace(mode='local_fixture',read=lambda *a,**k:self.batch)
    def failure(self):
        d=body(self.batch);d['series'][1]['samples'][0]['reading']['status_code']=503;self.batch=EffectBatch.model_validate(d)
        return self.c.verify_local(self.key,actor=ACTOR,reader=self.reader)
    def scope(self):return RollbackScope(execution_hash=self.e.digest_key(),approval_hash='1'*64,baseline_proof_hash='2'*64,
        target=self.e.target,deployment_uid=self.e.deployment_uid,application_uid=self.e.application_uid,
        revision='9'*40,digest='sha256:'+'8'*64,issued_at=self.e.applied_at-timedelta(seconds=1),
        expires_at=self.e.applied_at+timedelta(seconds=900),revoked=False,attempts_used=0)
    def test_real_pr_guard_receipt_then_full_effect(self):
        r=self.c.verify_local(self.key,actor=ACTOR,reader=self.reader)
        self.assertEqual(r['state'],'succeeded');self.assertFalse(r['cleanup_complete']);self.assertEqual(self.f.writer.calls,1)
    def test_required_owned_lease_cannot_be_omitted(self):
        with self.assertRaises(ValueError):self.c.open(self.key,actor=ACTOR,cleanup_items=[])
    def test_no_prior_scope_requires_new_approval(self):
        self.failure();r=self.c.follow_up(self.key,actor=ACTOR);self.assertEqual(r['decision'],'request_new_approval')
    def test_prior_exact_scope_is_candidate_never_execution(self):
        self.failure();r=self.c.follow_up(self.key,actor=ACTOR,scope_reader=lambda e:self.scope())
        self.assertEqual(r['decision'],'rollback_candidate');self.assertTrue(r['requires_execution_revalidation']);self.assertFalse(r['execution_allowed'])
    def test_wrong_expired_revoked_late_or_consumed_scope_not_candidate(self):
        self.failure()
        for changes in [{'execution_hash':'0'*64},{'revoked':True},{'expires_at':self.e.applied_at+timedelta(seconds=1)},
                        {'issued_at':self.e.applied_at+timedelta(seconds=1)},{'attempts_used':1}]:
            scope=self.scope().model_copy(update=changes)
            r=self.c.follow_up(self.key,actor=ACTOR,scope_reader=lambda e:scope)
            self.assertNotEqual(r['decision'],'rollback_candidate');self.assertFalse(r['execution_allowed'])
    def test_rollback_effect_preserves_first_failure(self):
        self.failure();scope=self.scope();self.c.follow_up(self.key,actor=ACTOR,scope_reader=lambda e:scope)
        applied=self.c.now();restored=self.e.model_copy(update={'revision':scope.revision,'digest':scope.digest,'applied_at':applied,'source_hash':'7'*64})
        policy=policy_for(restored);self.f.f.fixture.time=(policy.window_end+timedelta(seconds=1)).timestamp()
        reader=SimpleNamespace(mode='local_fixture',applied=lambda *a,**k:restored,read=lambda *a,**k:batch_for(restored,policy))
        r=self.c.record_rollback_local(self.key,actor=ACTOR,scope_reader=lambda e:scope,reader=reader,policy=policy)
        self.assertEqual(r['state'],'rollback_verified');self.assertTrue(r['service_resolved'])
        self.assertEqual(r['first_result']['status'],'unresolved');self.assertEqual(r['result']['status'],'unresolved')
        with self.assertRaises(ValueError):self.c.record_rollback_local(self.key,actor=ACTOR,scope_reader=lambda e:scope,reader=reader,policy=policy)
    def test_missing_evidence_never_rolls_back(self):
        self.batch=EffectBatch(series=[]);self.c.verify_local(self.key,actor=ACTOR,reader=self.reader)
        self.assertEqual(self.c.follow_up(self.key,actor=ACTOR,scope_reader=lambda e:self.scope())['decision'],'manual_handoff')

    def test_lease_cleanup_calls_keep_three_second_limit(self):
        r=self.c.show(self.key,actor=ACTOR);item=CleanupItem.model_validate(r['cleanup'][0]['item'])
        port=CleanupPort([item]);original=port.inspect
        def bounded(item,*,deadline):
            self.assertLessEqual(deadline,self.f.f.fixture.time+3)
            return original(item,deadline=deadline)
        port.inspect=bounded
        self.assertTrue(self.c.cleanup_local(self.key,actor=ACTOR,port=port)['cleanup_complete'])

    def test_source_delay_cannot_admit_stale_rollback_candidate(self):
        self.failure();original=self.c.source;calls=[]
        def slow(key,actor):
            value=original(key,actor);calls.append(True)
            if len(calls)==2:self.f.f.fixture.time+=121
            return value
        self.c.source=slow
        r=self.c.follow_up(self.key,actor=ACTOR,scope_reader=lambda e:self.scope())
        self.assertEqual(r['decision'],'manual_handoff');self.assertEqual(r['reason'],'effect_result_stale')

    def test_followup_after_unknown_rollback_never_offers_second_candidate(self):
        self.failure();scope=self.scope()
        self.c.follow_up(self.key,actor=ACTOR,scope_reader=lambda e:scope)
        reader=SimpleNamespace(mode='local_fixture',applied=lambda *a,**k:(_ for _ in ()).throw(OSError('lost receipt')))
        self.c.record_rollback_local(self.key,actor=ACTOR,scope_reader=lambda e:scope,reader=reader,policy=self.policy)
        result=self.c.follow_up(self.key,actor=ACTOR,scope_reader=lambda e:scope)
        self.assertEqual(result['decision'],'manual_handoff')
        self.assertEqual(result['reason'],'rollback_attempt_consumed')

    def test_followup_after_verified_rollback_reports_preserved_result(self):
        self.test_rollback_effect_preserves_first_failure()
        result=self.c.follow_up(self.key,actor=ACTOR,scope_reader=lambda e:self.scope())
        self.assertEqual(result['decision'],'report_result')
        self.assertEqual(result['reason'],'rollback_effect_verified')
        self.assertEqual(self.c.show(self.key,actor=ACTOR)['first_result']['status'],'unresolved')

    def test_cancel_during_scope_read_wins_over_expired_scope(self):
        self.failure()
        def cancelled(e):
            self.c.cancel(self.key,actor=ACTOR)
            return self.scope().model_copy(update={'expires_at':self.e.applied_at+timedelta(seconds=1)})
        result=self.c.follow_up(self.key,actor=ACTOR,scope_reader=cancelled)
        self.assertEqual(result['decision'],'manual_handoff')
        self.assertEqual(result['reason'],'cancelled_no_follow_up')
