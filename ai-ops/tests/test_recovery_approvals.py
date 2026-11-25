"""WF-06 fake-clock lifecycle over real WF-05 Slack/queue/worker fixtures. No IO."""
import json
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import test_recovery_plans as fixtures
from ai_ops.contracts import sha256_json
from ai_ops.recovery_approvals import (ApprovalPolicy, CurrentRecoveryState,
                                      RecoveryApprovalLedger, slack_plan_source)


class RecoveryApprovalsTest(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RecoveryPlansTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        receipt, _ = self.f.plan(self.f.investigate())
        self.request = receipt['request_id']
        self.plan = self.f.saved(self.request)
        self.key = self.plan.plan_hash
        self.actor = 'UALICE01'
        self.policy = ApprovalPolicy(version=1,approvers=[self.actor],wait_seconds=7200,
                                     approval_seconds=300,observation_seconds=30,revalidation_seconds=3)
        self.source = slack_plan_source(self.f.adapter, self.request)
        self.probe_count = 0
        self.overrides = {}
        self.ledger = self.make_ledger()

    def make_ledger(self):
        return RecoveryApprovalLedger(self.f.queue,self.policy,load_plan=self.source,observe=self.observe)

    def observe(self, plan, *, deadline=None):
        self.probe_count += 1
        now = datetime.fromtimestamp(self.f.fixture.time,timezone.utc)
        failure = plan.evidence.image_failure.model_copy(update={'observed_at':now})
        body = dict(plan_hash=plan.plan_hash,event_id=plan.event_id,target=plan.target,
                    deployment_uid_hash=failure.deployment_uid_hash,revision_hash='a'*64,
                    image_hash=failure.image_hash,proof_hash=sha256_json([self.probe_count,self.f.fixture.time]),
                    observed_at=now,status='image_failure',failure=failure)
        body.update(self.overrides)
        return CurrentRecoveryState(**body)

    def opened(self):
        return self.ledger.open(self.key,actor=self.actor)

    def decision(self, record, decision='approve', request='approval-1', **changes):
        params=dict(actor=self.actor,approval_hash=record['approval_hash'],generation=record['generation'],
                    request_id=request,decision=decision)
        params.update(changes)
        return self.ledger.decide(self.key,**params)

    def approved(self):
        return self.decision(self.opened())

    def revalidate(self, record):
        return self.ledger.revalidate(self.key,actor=self.actor,generation=record['generation'])

    def test_hour_wait_reopens_without_credentials_locks_jobs_or_network(self):
        with patch('socket.socket.connect',side_effect=AssertionError('network')), \
             patch('subprocess.Popen',side_effect=AssertionError('executor')), \
             patch.object(self.f.queue,'worker_lock',side_effect=AssertionError('worker lock')):
            opened=self.opened()
            self.f.fixture.time += 3601
            self.ledger=self.make_ledger()
            again=self.opened()
            self.assertEqual((again['wait_until'],again['approval_hash']),
                             (opened['wait_until'],opened['approval_hash']))
            approved=self.decision(again)
            self.assertEqual(approved['state'],'approved')
            final=self.revalidate(approved)
            self.assertEqual(final['state'],'proposal_ready')
            self.assertFalse(final['execution_allowed'])
            self.assertEqual(final['approval_scope'],'prepare_proposal')
        self.assertEqual(self.f.count('jobs'),1)
        self.assertEqual(self.probe_count,2)
        self.assertEqual(self.f.transport.calls,0)

    def test_late_approval_at_exact_wait_boundary_is_expired(self):
        opened=self.opened();self.f.fixture.time=opened['wait_until']
        current=self.ledger.show(self.key,actor=self.actor)
        self.assertEqual(current['state'],'expired')
        self.assertEqual(self.decision(current)['state'],'expired')
        self.assertFalse(self.opened()['proposal_ready'])

    def test_approval_expires_independently_of_wait_window(self):
        approved=self.approved();self.f.fixture.time=approved['approved_until']
        current=self.ledger.show(self.key,actor=self.actor)
        self.assertLess(self.f.fixture.time,current['wait_until'])
        self.assertEqual(current['state'],'expired')
        with self.assertRaises(ValueError):self.revalidate(current)

    def test_approval_replay_is_idempotent_and_does_not_extend_ttl(self):
        opened=self.opened();first=self.decision(opened)
        self.f.fixture.time+=10
        again=self.decision(opened)
        self.assertEqual((again['generation'],again['approved_until']),
                         (first['generation'],first['approved_until']))

    def test_reused_receipt_with_changed_decision_is_rejected(self):
        opened=self.opened();self.decision(opened)
        with self.assertRaisesRegex(ValueError,'replay_conflict'):self.decision(opened,'reject')

    def test_second_approval_with_new_id_does_not_renew(self):
        current=self.approved()
        with self.assertRaisesRegex(ValueError,'already_decided'):self.decision(current,request='another')

    def test_exact_subject_and_generation_required(self):
        opened=self.opened()
        for changes in ({'approval_hash':'b'*64},{'generation':9},{'generation':True}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.decision(opened,**changes)
        self.assertEqual(self.ledger.show(self.key,actor=self.actor)['state'],'awaiting_approval')

    def test_unknown_or_other_actor_cannot_read_or_approve(self):
        opened=self.opened()
        for actor in ('UBOB0001','UALICE01 approve'):
            with self.assertRaises(ValueError):self.ledger.show(self.key,actor=actor)
            with self.assertRaises(ValueError):self.decision(opened,actor=actor)
        with self.assertRaises(ValueError):self.ledger.open('f'*64,actor=self.actor)

    def test_source_rechecks_slack_policy_before_approval(self):
        opened=self.opened();self.f.adapter.policy.grants.clear()
        with self.assertRaises(ValueError):self.decision(opened)

    def test_source_rechecks_permission_after_probe(self):
        approved=self.approved()
        def probe(plan, *, deadline):
            value=self.observe(plan);self.f.adapter.policy.grants.clear();return value
        self.ledger.observe=probe
        self.assertEqual(self.revalidate(approved)['state'],'needs_review')

    def test_natural_health_is_verified_without_change(self):
        approved=self.approved()
        self.overrides=dict(status='healthy',failure=None,healthy_checks=self.plan.verification)
        final=self.revalidate(approved)
        self.assertEqual(final['state'],'already_recovered');self.assertFalse(final['proposal_ready'])

    def test_resolved_alert_alone_cannot_mean_recovered(self):
        approved=self.approved();self.overrides=dict(status='healthy',failure=None)
        self.assertEqual(self.revalidate(approved)['state'],'needs_review')

    def test_uid_revision_image_and_event_change_require_new_plan(self):
        for field in ('deployment_uid_hash','revision_hash','image_hash','event_id'):
            # Independent lifecycle per subcase; fixture has immutable retained plan.
            with self.subTest(field=field):
                other=self.make_ledger()
                self.ledger=other
                with self.f.queue.transaction() as db:
                    db.execute('DELETE FROM recovery_approvals');db.execute('DELETE FROM recovery_approval_receipts')
                approved=self.approved()
                self.overrides={field:('evt-' if field=='event_id' else '')+'d'*64}
                final=self.revalidate(approved)
                self.assertIn(final['state'],('superseded','needs_review'))
                self.assertFalse(final['proposal_ready'])
                self.overrides={}

    def test_plan_changed_in_source_is_not_old_approval(self):
        approved=self.approved()
        def probe(plan, *, deadline):
            observation=self.observe(plan)
            self.ledger.load_plan=lambda *args: None
            return observation
        self.ledger.observe=probe
        self.assertEqual(self.revalidate(approved)['state'],'needs_review')

    def test_cancellation_during_probe_fences_success(self):
        approved=self.approved()
        def probe(plan, *, deadline):
            # This transaction succeeds because observation holds no ledger/cluster lock.
            current=self.ledger.show(self.key,actor=self.actor)
            self.decision(current,'cancel','cancel-1')
            return self.observe(plan)
        self.ledger.observe=probe
        final=self.revalidate(approved)
        self.assertEqual(final['state'],'cancelled');self.assertFalse(final['execution_allowed'])

    def test_cancel_after_ready_cannot_be_replayed_into_ready(self):
        approved=self.approved();ready=self.revalidate(approved)
        final=self.decision(ready,'cancel','cancel-1')
        self.assertEqual(final['state'],'cancelled')
        with self.assertRaises(ValueError):self.revalidate(approved)

    def test_reject_is_terminal(self):
        rejected=self.decision(self.opened(),'reject')
        self.assertEqual(rejected['state'],'rejected')
        self.assertEqual(self.decision(rejected,request='late')['state'],'rejected')

    def test_read_exception_is_sanitized_and_never_retried(self):
        approved=self.approved()
        self.ledger.observe=lambda plan, **kwargs: (_ for _ in ()).throw(OSError('SECRET-TOKEN-url'))
        final=self.revalidate(approved)
        self.assertEqual(final['state'],'needs_review')
        self.assertNotIn('SECRET',json.dumps(final))
        with self.assertRaises(ValueError):self.revalidate(final)

    def test_slow_probe_cannot_extend_budget(self):
        approved=self.approved()
        def probe(plan, *, deadline):
            self.f.fixture.time+=3
            return self.observe(plan)
        self.ledger.observe=probe
        final=self.revalidate(approved)
        self.assertEqual((final['state'],final['reason']),('needs_review','revalidation_timed_out'))

    def test_process_interrupt_is_persisted_and_times_out_after_reopen(self):
        approved=self.approved()
        self.ledger.observe=lambda plan, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):self.revalidate(approved)
        self.f.fixture.time+=3;self.ledger=self.make_ledger()
        self.assertEqual(self.ledger.show(self.key,actor=self.actor)['reason'],'revalidation_timed_out')

    def test_stale_future_and_pre_probe_observations_are_rejected(self):
        for offset in (-1,1):
            with self.subTest(offset=offset):
                self.overrides={'observed_at':datetime.fromtimestamp(self.f.fixture.time+offset,timezone.utc)}
                with self.assertRaises(ValueError):self.opened()

    def test_ready_observation_has_its_own_short_expiry(self):
        ready=self.revalidate(self.approved())
        self.f.fixture.time=ready['fresh_until']
        final=self.ledger.show(self.key,actor=self.actor)
        self.assertEqual((final['state'],final['reason']),('needs_review','observation_expired'))
        self.assertFalse(final['proposal_ready'])

    def test_clock_regression_fails_closed(self):
        self.opened();self.f.fixture.time-=1
        self.assertEqual(self.ledger.show(self.key,actor=self.actor)['reason'],'clock_regressed')

    def test_explicit_policy_transition_invalidates_waiting_and_old_instances(self):
        self.approved();old=self.make_ledger()
        newer=self.policy.model_copy(update={'version':2,'wait_seconds':10800})
        with self.assertRaises(ValueError):
            RecoveryApprovalLedger(self.f.queue,newer,load_plan=self.source,observe=self.observe)
        self.ledger.replace_policy(newer)
        self.assertEqual(self.ledger.show(self.key,actor=self.actor)['state'],'superseded')
        with self.assertRaisesRegex(ValueError,'policy_changed'):old.show(self.key,actor=self.actor)

    def test_policy_mutation_cannot_change_limits_in_place(self):
        self.opened();self.ledger.policy.wait_seconds=9999
        with self.assertRaisesRegex(ValueError,'policy_changed'):self.ledger.show(self.key,actor=self.actor)

    def test_prune_erases_identity_and_keeps_hashed_tombstone(self):
        opened=self.opened();self.f.fixture.time=opened['retained_until']
        self.ledger.prune()
        with self.f.queue.transaction() as db:
            body=json.loads(db.execute('SELECT document FROM recovery_approvals').fetchone()[0])
        self.assertEqual(body['state'],'expired');self.assertIsNone(body['identity'])
        self.assertIsNone(body['proof_hash']);self.assertIsNone(body['approved_by_hash'])
        with self.assertRaises(ValueError):self.opened()

    def test_real_wf05_plan_and_saved_approval_never_change_original_reply(self):
        self.revalidate(self.approved())
        self.assertEqual(self.f.saved(self.request),self.plan)
        self.assertEqual(self.plan.approval,'not_requested')
        self.assertFalse(self.plan.execution_allowed)

    def test_no_implicit_production_timeouts_or_execution_scope(self):
        with self.assertRaises(ValueError):ApprovalPolicy(version=1,approvers=[self.actor])
        with self.assertRaises(ValueError):ApprovalPolicy(**{**self.policy.model_dump(),'scope':'execute'})
        with self.assertRaises(ValueError):ApprovalPolicy(**{**self.policy.model_dump(),'approvers':[self.actor,self.actor]})

    def test_observation_expiring_during_source_check_cannot_be_ready(self):
        approved=self.approved()
        self.ledger.replace_policy(self.policy.model_copy(update={'version':2,'observation_seconds':1}))
        # Fresh policy is independently installed before a new lifecycle subject.
        with self.f.queue.transaction() as db:
            db.execute('DELETE FROM recovery_approvals');db.execute('DELETE FROM recovery_approval_receipts')
        approved=self.approved()
        original=self.ledger.load_plan
        calls=0
        def source(*args):
            nonlocal calls
            calls+=1
            if calls==2:self.f.fixture.time+=1
            return original(*args)
        self.ledger.load_plan=source
        final=self.revalidate(approved)
        self.assertEqual(final['state'],'needs_review')
        self.assertFalse(final['proposal_ready'])

    def test_concurrent_approve_cancel_only_one_exact_generation_wins(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        opened=self.opened();barrier=Barrier(2)
        def decide(decision):
            barrier.wait()
            try:return self.decision(opened,decision,decision)['state']
            except ValueError:return 'stale_generation'
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(decide,('approve','cancel')))
        self.assertEqual(outcomes.count('stale_generation'),1)
        self.assertIn(self.ledger.show(self.key,actor=self.actor)['state'],('approved','cancelled'))

    def test_policy_change_during_probe_fences_completion(self):
        approved=self.approved()
        def probe(plan, *, deadline):
            self.ledger.replace_policy(self.policy.model_copy(update={'version':2}))
            return self.observe(plan)
        self.ledger.observe=probe
        self.assertEqual(self.revalidate(approved)['state'],'superseded')

    def test_observer_gets_original_bounded_deadline(self):
        approved=self.approved();now=self.f.fixture.time
        def probe(plan, *, deadline):
            self.assertEqual(deadline,now+3)
            return self.observe(plan)
        self.ledger.observe=probe
        self.assertEqual(self.revalidate(approved)['state'],'proposal_ready')

    def test_unknown_observation_and_command_candidate_cannot_advance(self):
        approved=self.approved();self.overrides={'status':'unknown','failure':None}
        self.assertEqual(self.revalidate(approved)['state'],'needs_review')
        receipt,_=self.f.plan(self.plan.job_id,'deployment-restart')
        candidate=self.f.saved(receipt['request_id'])
        other=RecoveryApprovalLedger(self.f.queue,self.policy,
            load_plan=slack_plan_source(self.f.adapter,receipt['request_id']),observe=self.observe)
        with self.assertRaises(ValueError):other.open(candidate.plan_hash,actor=self.actor)

    def test_changed_initial_target_requires_new_draft(self):
        # Healthy now (2026-11-25 decision): never opens. Same workload/image -> the
        # dedicated "already healthy" refusal; another image -> a new plan is required.
        self.overrides={'revision_hash':'d'*64,'status':'healthy','healthy_checks':self.plan.verification}
        with self.assertRaisesRegex(ValueError,'current_state_already_healthy'):self.opened()
        self.overrides={'status':'healthy','healthy_checks':self.plan.verification,'image_hash':'e'*64,'failure':None}
        with self.assertRaisesRegex(ValueError,'new_recovery_plan_required'):self.opened()
        self.assertEqual(self.ledger.page()[0],[])

    def test_source_rejects_even_allowlisted_other_requester(self):
        other=self.policy.model_copy(update={'version':2,'approvers':[self.actor,'UBOB0001']})
        self.ledger.replace_policy(other)
        with self.assertRaisesRegex(ValueError,'unavailable'):self.ledger.open(self.key,actor='UBOB0001')

    def test_review_open_rechecks_budget_after_database_wait(self):
        original=self.ledger._active
        checks=0
        def active(db):
            nonlocal checks
            checks+=1
            if checks==2:self.f.fixture.time+=3
            return original(db)
        with patch.object(self.ledger,'_active',side_effect=active):
            with self.assertRaisesRegex(ValueError,'revalidation_timed_out'):self.opened()
        with self.f.queue.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM recovery_approvals').fetchone()[0],0)

    def test_review_terminal_cancel_survives_clock_regression(self):
        cancelled=self.decision(self.opened(),'cancel')
        self.f.fixture.time-=1
        current=self.ledger.show(self.key,actor=self.actor)
        self.assertEqual((current['state'],current['generation']),
                         (cancelled['state'],cancelled['generation']))

    def test_review_unavailable_source_result_cannot_authorize_preparation(self):
        opened=self.opened()
        with patch.object(self.f.tasks,'result',return_value={'state':'unavailable'}):
            with self.assertRaisesRegex(ValueError,'approval_unavailable'):self.decision(opened)
        self.assertEqual(self.ledger.show(self.key,actor=self.actor)['state'],'awaiting_approval')

    def test_review_source_bundle_changed_is_rejected(self):
        opened=self.opened()
        result=self.f.tasks.result(self.plan.job_id)
        with patch.object(self.f.tasks,'result',return_value={**result,'bundle_hash':'f'*64}):
            with self.assertRaisesRegex(ValueError,'approval_unavailable'):self.decision(opened)
