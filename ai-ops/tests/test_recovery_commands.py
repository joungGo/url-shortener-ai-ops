from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_recovery_plans as plans
from ai_ops.contracts import sha256_json
from ai_ops.c_recovery import TARGET, IMAGE_REPOSITORY
from ai_ops.recovery_approvals import ApprovalPolicy, RecoveryApprovalLedger
from ai_ops.recovery_commands import (RestartCoordinator,RestartApprovalLedger,RestartSnapshot,RestartPlan,
    slack_restart_source,ANNOTATION,ENDPOINT)
from ai_ops.restart_gates import REQUIRED_EVIDENCE

ACTOR='UALICE01'
UID='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'

class LocalExecutor:
    mode='local_fixture'
    def __init__(self,t):
        self.t=t;self.reads=0;self.writes=0;self.acquires=0;self.releases=0;self.held=None
        self.write_hook=None;self.read_hook=None;self.lose_response=False;self.lost_lease=False;self.release_ok=True
        self.captures=0;self.evidence_override=None
        self.identity={'executor_id':'local-restart-test','namespace':'ai-ops-lab','resource':'deployments/lab-api','verbs':['get','patch']}
    def read(self,*,deadline):
        self.reads+=1
        if self.read_hook:self.read_hook()
        return self.t.observe(None,deadline=deadline)
    def acquire(self,*,owner,deadline):
        self.acquires+=1
        self.held={'name':'b6-scenario-lock','namespace':'ai-ops-lab','uid':'lease-local','holder':owner,'verified_until':deadline+30}
        return deepcopy(self.held)
    def inspect_lease(self,held,*,deadline):
        return {**self.held,'uid':'different'} if self.lost_lease else deepcopy(self.held)
    def capture_evidence(self,*,deadline):
        self.captures+=1
        if self.evidence_override is not None:
            if isinstance(self.evidence_override,Exception):raise self.evidence_override
            return self.evidence_override
        return {'bundle_sha256':'e'*64,'items':list(REQUIRED_EVIDENCE),'captured_at':self.t.f.fixture.time}
    def patch(self,request,*,deadline):
        self.writes+=1
        self.t.assertEqual(request,self.t.plan.request)
        if (request['body'][0]['value']!=self.t.state.deployment_uid
                or request['body'][1]['value']!=self.t.state.resource_version):raise ValueError('json_patch_test_conflict')
        self.t.state=self.t.state.model_copy(update={'resource_version':'2','generation':2,'restart_marker':self.t.plan.marker})
        if self.write_hook:self.write_hook()
        if self.lose_response:raise OSError('secret-upstream-content')
        return {'accepted':True}
    def release(self,held,*,deadline):
        self.releases+=1
        return self.release_ok

class RestartCommandTest(unittest.TestCase):
    def setUp(self):
        self.f=plans.RecoveryPlansTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.snapshot(reason='Unhealthy',message='Readiness probe failed: timeout',phase='Running',
            deployment_uid=UID,pod_deployment_uid=UID)
        self.job=self.f.investigate();receipt,_=self.f.plan(self.job,'deployment-restart')
        self.candidate=self.f.saved(receipt['request_id']);self.source=slack_restart_source(self.f.adapter,receipt['request_id'])
        self.state=RestartSnapshot(target=TARGET,deployment_uid=UID,resource_version='1',revision_hash='b'*64,
            image=IMAGE_REPOSITORY+'@sha256:'+'a'*64,pod_uid_hash=sha256_json('pod-1'),observed_at=self.now(),
            generation=1,observed_generation=1,ready_replicas=0,strategy='Recreate',container_running=True,restart_count=0,
            pod_started_at=self.now()-timedelta(hours=1),readiness_failing_since=self.now()-timedelta(minutes=10),
            readiness_window_seconds=30,prior_ready='observed',dependencies='pass',
            rollout_in_progress=False,autoscaling=False,gitops_auto_sync=False,annotations_exist=True)
        self.coordinator=self.create_coordinator()
        self.plan=self.coordinator.prepare(self.candidate.plan_hash,actor=ACTOR)
        self.key=self.plan.plan_hash
        self.policy=ApprovalPolicy(version=1,approvers=[ACTOR],wait_seconds=7200,approval_seconds=300,
            observation_seconds=30,revalidation_seconds=3,scope='execute_command')
        self.ledger=RestartApprovalLedger(self.f.queue,self.policy,load_plan=self.coordinator.load,observe=self.observe)
        self.port=LocalExecutor(self)
    def now(self):return datetime.fromtimestamp(self.f.fixture.time,timezone.utc)
    def observe(self,plan,*,deadline):return self.state.model_copy(update={'observed_at':self.now()})
    def create_coordinator(self):return RestartCoordinator(self.f.queue,source=self.source,read_current=self.observe,
        executor_id='local-restart-test',execution_seconds=15)
    def consent(self):
        r=self.ledger.open(self.key,actor=ACTOR)
        r=self.ledger.decide(self.key,actor=ACTOR,approval_hash=r['approval_hash'],generation=r['generation'],
            request_id='consent',decision='approve')
        return self.ledger.revalidate(self.key,actor=ACTOR,generation=r['generation'])
    def execute(self):return self.coordinator.execute_local(self.key,actor=ACTOR,ledger=self.ledger,port=self.port)
    def test_exact_plan_reuses_real_queue_source_without_writes(self):
        with patch('socket.socket.connect',side_effect=AssertionError('network')),patch('subprocess.Popen',side_effect=AssertionError('shell')):
            again=self.coordinator.prepare(self.candidate.plan_hash,actor=ACTOR)
        self.assertEqual(again,self.plan);self.assertFalse(self.plan.execution_allowed)
        self.assertEqual(self.f.count('jobs'),1);self.assertEqual(self.plan.request['endpoint'],ENDPOINT)
        self.assertEqual(len(self.plan.request['body']),3)
        self.assertEqual(self.plan.request['body'][-1]['value'],self.plan.marker)
        self.assertEqual((self.port.writes,self.port.acquires), (0,0))
    def test_explicit_command_approval_then_single_local_patch_is_not_recovery(self):
        ready=self.consent();self.assertTrue(ready['command_ready']);self.assertFalse(ready['proposal_ready'])
        self.assertEqual(ready['approval_scope'],'execute_command')
        result=self.execute()
        self.assertEqual(result['state'],'applied_unverified');self.assertTrue(result['applied'])
        self.assertFalse(result['service_resolved']);self.assertFalse(result['execution_allowed'])
        self.assertTrue(result['lease_released']);self.assertEqual(self.port.writes,1)
        self.assertIn(self.plan.event_id,self.coordinator.result_text(self.key,actor=ACTOR))
    def test_no_approval_never_acquires_or_writes(self):
        with self.assertRaises(ValueError):self.execute()
        self.assertEqual((self.port.writes,self.port.acquires),(0,0))
    def test_pr_approval_scope_cannot_authorize_command(self):
        bad=self.policy.model_copy(update={'scope':'prepare_proposal'})
        with self.assertRaises(ValueError):RestartApprovalLedger(self.f.queue,bad,load_plan=self.coordinator.load,observe=self.observe)
        with self.assertRaises(ValueError):RecoveryApprovalLedger(self.f.queue,self.policy,load_plan=self.coordinator.load,observe=self.observe)
        legacy=RecoveryApprovalLedger(self.f.queue,bad,load_plan=lambda k,a:None,observe=lambda p,deadline:None)
        self.consent()
        with self.assertRaises(ValueError):self.coordinator.execute_local(self.key,actor=ACTOR,ledger=legacy,port=self.port)
        self.assertEqual(self.port.writes,0)
    def test_one_hour_human_wait_then_fresh_check_without_held_lock(self):
        r=self.ledger.open(self.key,actor=ACTOR);self.f.fixture.time+=3601
        self.assertEqual(self.port.acquires,0)
        r=self.ledger.decide(self.key,actor=ACTOR,approval_hash=r['approval_hash'],generation=r['generation'],request_id='late',decision='approve')
        r=self.ledger.revalidate(self.key,actor=ACTOR,generation=r['generation'])
        self.assertTrue(r['command_ready']);self.assertEqual(self.execute()['state'],'applied_unverified')
    def test_cancel_before_execution_is_terminal(self):
        self.consent();self.coordinator.cancel(self.key,actor=ACTOR)
        with self.assertRaises(ValueError):self.execute()
        self.assertEqual(self.port.writes,0)
    def test_cancel_after_lock_before_write_wins(self):
        self.consent()
        def cancel():
            if self.port.reads==2:self.coordinator.cancel(self.key,actor=ACTOR)
        self.port.read_hook=cancel
        self.assertEqual(self.execute()['state'],'cancelled');self.assertEqual(self.port.writes,0)
    def test_cancel_after_write_keeps_observed_application_and_no_false_success(self):
        self.consent();self.port.write_hook=lambda:self.coordinator.cancel(self.key,actor=ACTOR)
        r=self.execute();self.assertEqual(r['state'],'cancelled');self.assertTrue(r['applied']);self.assertFalse(r['service_resolved'])
    def test_unknown_write_outcome_only_reconciles_readonly(self):
        self.consent();self.port.lose_response=True
        r=self.execute();self.assertEqual(r['state'],'needs_review');self.assertFalse(r['applied'])
        self.assertNotIn('secret',json.dumps(r));self.assertFalse(r['lease_released'])
        with self.assertRaises(ValueError):self.execute()
        self.coordinator=self.create_coordinator()
        r=self.coordinator.reconcile_local(self.key,actor=ACTOR,port=self.port)
        self.assertTrue(r['applied']);self.assertEqual(self.port.writes,1);self.assertFalse(r['lease_released'])
    def test_process_interrupt_never_repeats_write(self):
        self.consent();self.port.write_hook=lambda:(_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):self.execute()
        self.coordinator=self.create_coordinator()
        self.assertEqual(self.coordinator.show(self.key,actor=ACTOR)['state'],'executing')
        with self.assertRaises(ValueError):self.execute()
        self.assertTrue(self.coordinator.reconcile_local(self.key,actor=ACTOR,port=self.port)['applied'])
        self.assertEqual(self.port.writes,1)
    def test_unconfirmed_marker_is_not_noop_success_or_retry(self):
        self.consent();self.port.patch=lambda request,deadline: {'accepted':True}
        self.assertEqual(self.execute()['state'],'needs_review')
        self.assertEqual(self.coordinator.reconcile_local(self.key,actor=ACTOR,port=self.port)['state'],'needs_review')
        with self.assertRaises(ValueError):self.execute()
    def test_changed_uid_revision_image_and_resource_version_deny_write(self):
        for field,value in [('deployment_uid','cccccccc-cccc-cccc-cccc-cccccccccccc'),('resource_version','3'),
            ('revision_hash','e'*64),('image',IMAGE_REPOSITORY+'@sha256:'+'b'*64)]:
            with self.subTest(field=field):
                original=self.state;self.state=original.model_copy(update={field:value})
                with self.assertRaises(ValueError):self.ledger.open(self.key,actor=ACTOR)
                self.state=original
        self.assertEqual(self.port.writes,0)
    def test_hpa_gitops_rollout_dependency_and_healthy_state_fail_closed(self):
        recent=self.now()-timedelta(seconds=30)
        for changes in ({'autoscaling':True},{'gitops_auto_sync':True},{'rollout_in_progress':True},
            {'ready_replicas':1},{'observed_generation':0},{'restart_marker':'already'},
            {'strategy':'RollingUpdate'},{'readiness_failing_since':None},{'readiness_failing_since':recent},
            {'pod_started_at':recent},{'container_running':False},{'restart_count':2},
            {'prior_ready':'absent'},{'prior_ready':'unknown'},{'dependencies':'fail'},
            {'readiness_failing_since':self.now()-timedelta(hours=2)}):
            with self.subTest(changes=changes):
                original=self.state;self.state=original.model_copy(update=changes)
                with self.assertRaises(ValueError):self.ledger.open(self.key,actor=ACTOR)
                self.state=original
    def test_unknown_dependencies_are_display_only(self):
        self.state=self.state.model_copy(update={'dependencies':'unknown','image_id_hash':'f'*64,
                                                 'scheduling_signals':['failed_scheduling']})
        self.consent()
        self.assertEqual(self.execute()['state'],'applied_unverified')
        self.assertEqual(self.port.writes,1)
    def test_evidence_capture_is_a_hard_gate_before_any_write(self):
        bad=[OSError('capture failed'),
             {'bundle_sha256':'e'*64,'items':[i for i in REQUIRED_EVIDENCE if i!='logs_previous'],'captured_at':0},
             {'bundle_sha256':'not-a-hash','items':list(REQUIRED_EVIDENCE),'captured_at':0},
             {'bundle_sha256':'e'*64,'items':list(REQUIRED_EVIDENCE),'captured_at':-1},
             {'bundle_sha256':'e'*64,'items':list(REQUIRED_EVIDENCE),'captured_at':0,'raw_log':'x'}]
        for i,override in enumerate(bad):
            with self.subTest(case=i):
                if i:self.setUp()
                if isinstance(override,dict) and override['captured_at']==0:
                    override={**override,'captured_at':self.f.fixture.time}
                self.port.evidence_override=override
                self.consent()
                result=self.execute()
                self.assertEqual((result['state'],result['reason']),('needs_review','inspect_without_retry'))
                self.assertEqual(self.port.writes,0)
                self.assertNotIn('evidence_bundle',result)
    def test_evidence_is_captured_before_patch_and_recorded(self):
        order=[]
        original=self.port.capture_evidence
        def capture(*,deadline):
            order.append(('capture',self.port.writes));return original(deadline=deadline)
        self.port.capture_evidence=capture
        self.consent()
        result=self.execute()
        self.assertEqual(result['state'],'applied_unverified')
        self.assertEqual(order,[('capture',0)])
        self.assertEqual(result['evidence_bundle'],{'bundle_sha256':'e'*64,'items':list(REQUIRED_EVIDENCE),
                                                    'captured_at':result['evidence_bundle']['captured_at']})
    def test_gate_rejection_after_approval_is_recorded_for_manual_handoff(self):
        self.consent()
        self.state=self.state.model_copy(update={'prior_ready':'absent','restart_count':1})
        result=self.execute()
        self.assertEqual((result['state'],result['reason']),('needs_review','gate_rejected'))
        verdicts={g['name']:g['verdict'] for g in result['gate_results']}
        self.assertEqual(verdicts['prior_ready_evidence'],'fail')
        self.assertEqual(verdicts['liveness_stable'],'fail')
        self.assertEqual(verdicts['persistent_readiness_failure'],'pass')
        self.assertTrue(all(set(g)=={'name','verdict','blocking','rules_version'} for g in result['gate_results']))
        self.assertEqual((self.port.writes,self.port.captures),(0,0))
        with self.assertRaises(ValueError):self.execute()  # one attempt per event, no retry
    def test_persistent_failure_threshold_boundary(self):
        from ai_ops.restart_gates import evaluate
        now=self.f.fixture.time
        def verdict(failing_for):
            s=self.state.model_copy(update={'readiness_failing_since':datetime.fromtimestamp(now-failing_for,timezone.utc)})
            return {g.name:g.verdict for g in evaluate(self.plan,s,now)}['persistent_readiness_failure']
        self.assertEqual(verdict(60),'pass')   # 2 x readiness window (30s)
        self.assertEqual(verdict(59),'fail')
    def test_expired_approval_or_observation_cannot_execute(self):
        ready=self.consent();self.f.fixture.time=ready['fresh_until']
        with self.assertRaises(ValueError):self.execute()
        self.assertEqual(self.port.writes,0)
    def test_timeout_after_lock_does_not_patch(self):
        self.consent()
        def slow():
            if self.port.reads==2:self.f.fixture.time+=16
        self.port.read_hook=slow
        self.assertEqual(self.execute()['state'],'needs_review');self.assertEqual(self.port.writes,0)
    def test_timeout_after_patch_does_not_claim_success(self):
        self.consent();self.port.write_hook=lambda:setattr(self.f.fixture,'time',self.f.fixture.time+16)
        self.assertEqual(self.execute()['state'],'needs_review');self.assertEqual(self.port.writes,1)
    def test_lost_lock_never_patches(self):
        self.consent();self.port.lost_lease=True
        self.assertEqual(self.execute()['state'],'needs_review');self.assertEqual(self.port.writes,0)
    def test_failed_release_is_reported_not_assumed_clean(self):
        self.consent();self.port.release_ok=False
        r=self.execute();self.assertEqual(r['state'],'needs_review');self.assertTrue(r['applied']);self.assertFalse(r['lease_released'])
    def test_live_or_broad_executor_rejected(self):
        self.consent();self.port.mode='live'
        with self.assertRaises(ValueError):self.execute()
        self.port.mode='local_fixture';self.port.identity['verbs']=['*']
        with self.assertRaises(ValueError):self.execute()
        self.assertEqual(self.port.acquires,0)
    def test_plan_hash_and_fixed_operation_reject_injected_command(self):
        for changes in ({'request':{'method':'DELETE','endpoint':ENDPOINT}}, {'target':{'cluster_id':'other','namespace':'prod','workload':'db'}}):
            data=self.plan.model_dump(mode='json');data.update(changes);data['plan_hash']=sha256_json({k:v for k,v in data.items() if k!='plan_hash'})
            with self.assertRaises(ValueError):RestartPlan.model_validate(data)
    def test_wrong_actor_policy_and_source_loss_deny(self):
        with self.assertRaises(ValueError):self.coordinator.load(self.key,'UBOB0001')
        self.consent();self.ledger.replace_policy(self.policy.model_copy(update={'version':2}))
        with self.assertRaises(ValueError):self.execute()
        with patch.object(self.f.tasks,'result',return_value={'state':'unavailable'}):
            with self.assertRaises(ValueError):self.coordinator.load(self.key,ACTOR)
    def test_source_expiry_clears_plans_retains_consumption(self):
        self.consent();self.execute();self.f.fixture.time=self.plan.retained_until.timestamp()
        self.coordinator.prune();self.ledger.prune()
        with self.f.queue.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM restart_plans').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM restart_consumed').fetchone()[0],1)
        with self.assertRaises(ValueError):self.coordinator.load(self.key,ACTOR)

    def test_wf06_cancel_during_initial_read_does_not_acquire(self):
        self.consent()
        def cancel():
            a=self.ledger.show(self.key,actor=ACTOR)
            self.ledger.decide(self.key,actor=ACTOR,approval_hash=a['approval_hash'],generation=a['generation'],
                request_id='cancel-command',decision='cancel')
        self.port.read_hook=cancel
        self.assertEqual(self.execute()['state'],'needs_review')
        self.assertEqual((self.port.acquires,self.port.writes),(0,0))

    def test_wf06_cancel_after_patch_records_applied_but_cancelled(self):
        self.consent()
        def cancel():
            a=self.ledger.show(self.key,actor=ACTOR)
            self.ledger.decide(self.key,actor=ACTOR,approval_hash=a['approval_hash'],generation=a['generation'],
                request_id='cancel-command',decision='cancel')
        self.port.write_hook=cancel
        r=self.execute();self.assertTrue(r['applied']);self.assertEqual(r['state'],'cancelled')

    def test_wrong_lock_namespace_or_nonfinite_proof_never_writes(self):
        self.consent()
        original=self.port.acquire
        def bad(**kwargs):return {**original(**kwargs),'namespace':'production','verified_until':float('inf')}
        self.port.acquire=bad
        self.assertEqual(self.execute()['state'],'needs_review');self.assertEqual(self.port.writes,0)

    def test_resolved_event_revokes_original_candidate(self):
        self.f.adapter.events.ingest('lab-seoul',{**self.f.alert,'status':'resolved','endsAt':self.now().isoformat()})
        with self.assertRaises(ValueError):self.coordinator.load(self.key,ACTOR)

    def test_incomplete_or_old_source_evidence_is_not_restart_candidate(self):
        for changes in ({'phase':'Pending'},{'event_age':40},{'missing_owner':True},{'event_missing':True},
                        {'reason':'ImagePullBackOff'}):
            with self.subTest(changes=changes):
                # Build a separate genuine worker record, not caller-supplied flags.
                f=plans.RecoveryPlansTest();f.setUp()
                try:
                    opts=dict(reason='Unhealthy',message='Readiness probe failed: timeout',phase='Running',
                        deployment_uid=UID,pod_deployment_uid=UID);opts.update(changes);f.snapshot(**opts)
                    receipt,_=f.plan(f.investigate(),'deployment-restart');candidate=f.saved(receipt['request_id'])
                    with self.assertRaises(ValueError):slack_restart_source(f.adapter,receipt['request_id'])(candidate.plan_hash,ACTOR)
                finally:f.doCleanups()

    def test_compare_and_swap_conflict_does_not_retry_or_claim_application(self):
        self.consent();original=self.port.patch
        def conflict(request,*,deadline):
            self.state=self.state.model_copy(update={'resource_version':'999'})
            return original(request,deadline=deadline)
        self.port.patch=conflict
        r=self.execute();self.assertEqual(r['state'],'needs_review');self.assertFalse(r['applied'])
        self.assertEqual(self.port.writes,1)
        with self.assertRaises(ValueError):self.execute()

    def test_same_event_second_plan_cannot_restart_again(self):
        receipt,_=self.f.plan(self.job,'deployment-restart');candidate=self.f.saved(receipt['request_id'])
        other=RestartCoordinator(self.f.queue,source=slack_restart_source(self.f.adapter,receipt['request_id']),
            read_current=self.observe,executor_id='local-restart-test',execution_seconds=15)
        other_plan=other.prepare(candidate.plan_hash,actor=ACTOR)
        other_ledger=RestartApprovalLedger(self.f.queue,self.policy,load_plan=other.load,observe=self.observe)
        a=other_ledger.open(other_plan.plan_hash,actor=ACTOR)
        a=other_ledger.decide(other_plan.plan_hash,actor=ACTOR,approval_hash=a['approval_hash'],generation=a['generation'],request_id='other',decision='approve')
        other_ledger.revalidate(other_plan.plan_hash,actor=ACTOR,generation=a['generation'])
        self.consent();self.execute()
        with self.assertRaises(ValueError):other.execute_local(other_plan.plan_hash,actor=ACTOR,ledger=other_ledger,port=self.port)
        self.assertEqual(self.port.writes,1)

    def test_two_concurrent_executors_submit_at_most_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        self.consent();barrier=Barrier(2)
        def run():
            barrier.wait()
            try:return self.execute()['state']
            except ValueError:return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:run(),range(2)))
        self.assertEqual(self.port.writes,1);self.assertIn('applied_unverified',results);self.assertIn('rejected',results)

    def cancel_approval(self):
        a=self.ledger.show(self.key,actor=ACTOR)
        self.ledger.decide(self.key,actor=ACTOR,approval_hash=a['approval_hash'],generation=a['generation'],
            request_id='cancel-reconcile',decision='cancel')

    def test_reconcile_preserves_approval_cancel_after_lost_response(self):
        self.consent();self.port.lose_response=True;self.execute();self.cancel_approval()
        r=self.coordinator.reconcile_local(self.key,actor=ACTOR,port=self.port)
        self.assertTrue(r['applied']);self.assertTrue(r['cancel_requested']);self.assertEqual(r['state'],'cancelled')
        self.assertEqual(self.port.writes,1)

    def test_cancel_during_reconcile_read_still_records_observed_application(self):
        self.consent();self.port.lose_response=True;self.execute()
        self.port.read_hook=lambda:self.coordinator.cancel(self.key,actor=ACTOR)
        r=self.coordinator.reconcile_local(self.key,actor=ACTOR,port=self.port)
        self.assertTrue(r['applied']);self.assertEqual(r['state'],'cancelled');self.assertEqual(self.port.writes,1)

    def test_failed_reconcile_read_does_not_overwrite_cancelled_state(self):
        self.consent();self.port.lose_response=True;self.execute()
        # Another reader can complete the observation and a cancellation while this read fails.
        original=self.port.read
        def fail(*,deadline):
            self.port.read=original
            self.coordinator.reconcile_local(self.key,actor=ACTOR,port=self.port)
            self.coordinator.cancel(self.key,actor=ACTOR)
            raise OSError('redacted transport failure')
        self.port.read=fail
        r=self.coordinator.reconcile_local(self.key,actor=ACTOR,port=self.port)
        self.assertTrue(r['applied']);self.assertEqual(r['state'],'cancelled');self.assertEqual(self.port.writes,1)

    def test_failed_reconcile_does_not_overwrite_another_completed_observation(self):
        self.consent();self.port.lose_response=True;self.execute()
        original=self.port.read
        def fail(*,deadline):
            self.port.read=original
            self.coordinator.reconcile_local(self.key,actor=ACTOR,port=self.port)
            raise OSError('redacted transport failure')
        self.port.read=fail
        r=self.coordinator.reconcile_local(self.key,actor=ACTOR,port=self.port)
        self.assertTrue(r['applied']);self.assertEqual(r['state'],'applied_unverified');self.assertEqual(self.port.writes,1)
