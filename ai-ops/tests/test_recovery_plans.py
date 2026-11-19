"""WF-05: signed local requests, stored real reports, mock delivery, no external IO."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_ops.c_recovery import TARGET, IMAGE_REPOSITORY
from ai_ops.contracts import sha256_json
from ai_ops.recovery_plans import RecoveryPlan, action_registry, parse_plan_arguments
from ai_ops.slack_adapter import SlackAdapter
from ai_ops.slack_events import UNAVAILABLE
from ai_ops.job_queue import RETENTION_SECONDS
from test_slack_adapter import SlackFixture, SECRET
import test_slack_events as event_fixture
import test_captured_rules_v3 as captured
from test_live_collectors import AT, stamp


class RecoveryPlansTest(SlackFixture, unittest.TestCase):
    reopen = event_fixture.SlackEventsTest.reopen
    command = event_fixture.SlackEventsTest.command
    count = event_fixture.SlackEventsTest.count
    run_request = event_fixture.SlackEventsTest.run_request

    def setUp(self):
        super().setUp()
        self.fixture.time = AT.timestamp()+1
        self.fixture.target.target = TARGET.model_copy(deep=True)
        self.sources={'lab-seoul':{'source_id':'am-lab','cluster_id':TARGET.cluster_id}}
        self.reopen()
        self.alert={'labels':{**TARGET.model_dump(),'alertname':'HighErrorRate'},
                    'status':'firing','fingerprint':'0123456789abcdef','startsAt':stamp(AT-timedelta(seconds=30))}
        self.eid=self.adapter.events.ingest('lab-seoul',self.alert)
        self.serial=0
        self.snapshot()

    def snapshot(self, **changes):
        helper=SimpleNamespace(settings=self.fixture.settings,target=TARGET)
        image=IMAGE_REPOSITORY+'@sha256:'+'a'*64
        options=dict(deployment_image=image,pod_image=image)
        options.update(changes)
        self.fixture.snapshot=captured.CapturedRulesV3Test.snapshot(helper,**options)

    def investigate(self, *, complete=True):
        return self.run_request(complete=complete)[1]['task_id']

    def plan(self, job, action='image-rollback', **fields):
        event=self.command('plan-recovery', **fields)
        event['event']['text']+=' '+job+' '+action
        return self.run_request(event)

    def saved(self, request_id):
        context=json.loads(self.adapter._event_bridge().entry(request_id)['context'])
        return RecoveryPlan.model_validate(context['recovery_plan'])

    def test_signed_plan_reuses_job_and_preserves_partial_without_any_execution(self):
        job=self.investigate()
        with patch('socket.socket.connect',side_effect=AssertionError('network forbidden')), \
             patch('subprocess.Popen',side_effect=AssertionError('executor forbidden')), \
             patch.object(self.queue.engine,'load_api_key',side_effect=AssertionError('model forbidden')):
            receipt,reply=self.plan(job)
            plan=self.saved(receipt['request_id'])
            self.adapter.reply(receipt['request_id'],self.transport)
        self.assertEqual(plan.assessment,'supported')
        self.assertFalse(plan.proposal_ready); self.assertFalse(plan.execution_allowed)
        self.assertEqual(plan.approval,'not_requested');self.assertEqual(plan.state,'draft')
        self.assertEqual(self.tasks.show(job)['investigation']['state'],'partial')
        self.assertEqual(self.count('jobs'),1);self.assertEqual(self.count('event_requests'),1)
        self.assertEqual(self.transport.last_payload['thread_ts'],'1000.000001')
        self.assertEqual(plan.repository,'joungGo/c03-execution-lab')
        for text in ('실행 준비 아님','정확한 diff','승인되지 않은','계획 해시:',self.eid,job):
            self.assertIn(text,self.transport.last_payload['text'])
        self.assertIsNotNone(plan.evidence.image_failure)
        self.assertNotIn('pod-1',plan.model_dump_json())
        self.assertNotIn('desired_digest',plan.model_dump())

    def test_missing_owner_blocks_supported_path(self):
        self.snapshot(missing_owner=True)
        receipt,_=self.plan(self.investigate())
        self.assertEqual(self.saved(receipt['request_id']).assessment,'insufficient_evidence')

    def test_non_image_reason_does_not_infer_rollback(self):
        self.snapshot(reason='Unhealthy',message='probe timeout')
        receipt,_=self.plan(self.investigate())
        self.assertEqual(self.saved(receipt['request_id']).reason,'image_failure_not_proven')

    def test_unregistered_image_does_not_match(self):
        self.snapshot(pod_image='other.invalid/a:bad',deployment_image='other.invalid/a:bad')
        receipt,_=self.plan(self.investigate())
        self.assertEqual(self.saved(receipt['request_id']).assessment,'insufficient_evidence')

    def test_resolved_is_neither_recovery_success_nor_permission(self):
        job=self.investigate()
        self.adapter.events.ingest('lab-seoul',{**self.alert,'status':'resolved','endsAt':stamp(AT)})
        receipt,_=self.plan(job)
        self.assertEqual(self.saved(receipt['request_id']).reason,'event_resolved')

    def test_pending_investigation_has_no_fabricated_evidence(self):
        receipt,_=self.plan(self.investigate(complete=False))
        plan=self.saved(receipt['request_id'])
        self.assertEqual(plan.reason,'result_not_available');self.assertIsNone(plan.evidence)
        self.assertEqual(self.count('jobs'),1)

    def test_command_candidate_and_unknown_action_are_manual_handoff(self):
        job=self.investigate()
        for action,method,reason in [('deployment-restart','command','action_not_implemented'),
                                     ('database-restore','manual','action_not_registered')]:
            receipt,_=self.plan(job,action)
            plan=self.saved(receipt['request_id'])
            self.assertEqual((plan.assessment,plan.method,plan.reason),('unsupported',method,reason))
            self.assertIsNone(plan.repository);self.assertFalse(plan.execution_allowed)
        self.assertEqual(self.count('jobs'),1)

    def test_other_target_does_not_enable_image_recovery(self):
        from ai_ops.recovery_plans import prepare_plan
        job=self.investigate()
        # Change a binding before using it: EventStore must reject even before planning.
        self.fixture.target.target.workload='other-api'
        with self.assertRaises(ValueError):
            prepare_plan(self.tasks,self.adapter.events,
                view={'registration':'registered','event_id':self.eid,'target':self.fixture.target.alias},
                job_id=job,action_id='image-rollback',request_id='slack-'+'a'*64,
                authorization_hash='b'*64,audience_hash='c'*64)

    def test_arguments_do_not_accept_shell_digest_approval_or_url(self):
        for text in ('','job-'+'a'*32+' image-rollback approve',
                     'job-'+'a'*32+'; kubectl delete pod', 'https://example.invalid',
                     'job-'+'a'*32+' sha256:'+64*'a'):
            with self.subTest(text=text),self.assertRaises(ValueError):parse_plan_arguments(text)
        for command in ('recover','approve','execute'):
            with self.assertRaises(ValueError):self.receive(self.command(command))

    def test_request_text_never_changes_investigation_intent(self):
        event=self.command();event['event']['text']+=' 복구해줘 approve kubectl rollout restart'
        _,queued=self.run_request(event,complete=True)
        self.assertEqual(queued['state'],'queued')
        self.assertTrue(all('recovery_plan' not in (r['context'] or '') for r in self.rows('slack_event_requests')))

    def test_unknown_job_and_other_event_have_same_non_disclosing_response(self):
        job=self.investigate()
        other=self.adapter.events.ingest('lab-seoul',{**self.alert,'fingerprint':'1123456789abcdef'})
        responses=[]
        for selected,eid in [('job-'+'a'*32,self.eid),(job,other)]:
            receipt,_=self.plan(selected,reference=eid)
            self.adapter.reply(receipt['request_id'],self.transport)
            responses.append(self.transport.last_payload['text'])
        self.assertEqual(responses[0],responses[1]);self.assertIn(UNAVAILABLE,responses[0])

    def test_another_authorized_user_cannot_select_someone_elses_job(self):
        # A second allowed principal still does not acquire Alice's result scope.
        grant=self.policy.grants[0].model_copy(deep=True);grant.user_id='UBOB0001'
        self.policy.grants.append(grant);self.reopen()
        job=self.investigate()
        receipt,_=self.plan(job,user='UBOB0001')
        self.adapter.reply(receipt['request_id'],self.transport)
        self.assertIn(UNAVAILABLE,self.transport.last_payload['text'])
        self.assertNotIn(job,self.transport.last_payload['text'])

    def test_duplicate_restart_and_lost_reply_preserve_plan_hash(self):
        job=self.investigate()
        event=self.command('plan-recovery');event['event']['text']+=' '+job
        receipt,_=self.run_request(event)
        plan=self.saved(receipt['request_id'])
        self.reopen();self.assertTrue(self.receive(event)['duplicate'])
        self.assertEqual(self.adapter.drain_one()['state'],'idle')
        self.transport.lost_response=True
        self.assertEqual(self.adapter.reply(receipt['request_id'],self.transport)['state'],'needs_review')
        self.reopen();self.adapter.reply(receipt['request_id'],self.transport)
        self.assertEqual(self.transport.calls,1);self.assertEqual(self.saved(receipt['request_id']),plan)

    def test_plan_is_frozen_when_event_later_resolves(self):
        receipt,_=self.plan(self.investigate())
        plan=self.saved(receipt['request_id'])
        self.adapter.events.ingest('lab-seoul',{**self.alert,'status':'resolved','endsAt':stamp(AT)})
        self.adapter.reply(receipt['request_id'],self.transport)
        self.assertEqual(self.saved(receipt['request_id']),plan)
        self.assertIn('저장된 관측 기준',self.transport.last_payload['text'])

    def test_changed_policy_prevents_prepared_reply(self):
        receipt,_=self.plan(self.investigate())
        self.adapter.policy.grants[0].targets=['other-target']
        with self.assertRaises(ValueError):self.adapter.reply(receipt['request_id'],self.transport)
        self.assertEqual(self.transport.calls,0)

    def test_expiry_erases_copied_plan_even_when_event_commands_disabled(self):
        receipt,_=self.plan(self.investigate())
        self.fixture.time+=RETENTION_SECONDS
        self.adapter=SlackAdapter(self.tasks,self.policy,SECRET)
        self.adapter.prune()
        row=self.adapter._event_bridge().entry(receipt['request_id'])
        self.assertIsNone(row['context']);self.assertIsNone(row['reply_text'])

    def test_forged_hash_or_execution_state_cannot_be_accepted(self):
        receipt,_=self.plan(self.investigate())
        plan=self.saved(receipt['request_id'])
        for field,value in [('execution_allowed',True),('state','approved'),('repository','foreign/repo'),
                            ('verification',[]),('plan_hash','0'*64)]:
            body=plan.model_dump(mode='json');body[field]=value
            if field!='plan_hash':body['plan_hash']=sha256_json({k:v for k,v in body.items() if k!='plan_hash'})
            with self.subTest(field=field),self.assertRaises(ValueError):RecoveryPlan.model_validate(body)

    def test_registry_copy_cannot_register_or_enable_command(self):
        actions=action_registry();actions[1]['planning_supported']=True
        self.assertFalse(action_registry()[1]['planning_supported'])

    def test_retention_is_not_an_approval_deadline_and_no_write_tokens_are_created(self):
        receipt,_=self.plan(self.investigate())
        plan=self.saved(receipt['request_id'])
        self.assertEqual(plan.retained_until.timestamp(),AT.timestamp()-30+RETENTION_SECONDS)
        self.assertNotIn('token',plan.model_dump_json());self.assertNotIn('approved',plan.approval)
        self.assertEqual(list(self.queue.root.rglob('recovery-drafts.sqlite3')),[])

    def test_reply_and_prune_honor_job_deadline_shorter_than_request_retention(self):
        receipt,_=self.plan(self.investigate())
        plan=self.saved(receipt['request_id'])
        self.fixture.time=plan.retained_until.timestamp()+1
        # Maintenance works without an enabled event bridge.
        self.adapter=SlackAdapter(self.tasks,self.policy,SECRET)
        self.adapter.prune()
        self.assertIsNone(self.adapter._event_bridge().entry(receipt['request_id'])['context'])

    def test_expired_source_result_blocks_reply_and_erases_plan(self):
        job=self.investigate();receipt,_=self.plan(job)
        with patch.object(self.tasks,'show',return_value={'result':{'state':'expired'}}):
            with self.assertRaises(ValueError):self.adapter.reply(receipt['request_id'],self.transport)
            self.adapter.prune()
        self.assertIsNone(self.adapter._event_bridge().entry(receipt['request_id'])['context'])

    def test_scope_change_after_read_cannot_commit_draft(self):
        job=self.investigate()
        import ai_ops.recovery_plans as plans
        original=plans.prepare_plan
        def change(*args,**kwargs):
            result=original(*args,**kwargs)
            self.adapter.policy.grants[0].targets=['other-target']
            return result
        with patch.object(plans,'prepare_plan',side_effect=change):
            receipt,result=self.plan(job)
        self.assertEqual(result['state'],'rejected')
        self.assertIsNone(self.adapter._event_bridge().entry(receipt['request_id'])['context'])

    def test_plan_timestamp_includes_fractional_event_start(self):
        other=self.adapter.events.ingest('lab-seoul',{**self.alert,'fingerprint':'3123456789abcdef',
            'startsAt':'2026-10-16T00:59:30.123456789Z'})
        self.eid=other
        receipt,_=self.plan(self.investigate())
        self.assertEqual(self.saved(receipt['request_id']).event_id,other)

    def test_future_or_pre_event_observations_do_not_match_action(self):
        job=self.investigate()
        self.fixture.time=AT.timestamp()-1
        receipt,_=self.plan(job)
        self.assertEqual(self.saved(receipt['request_id']).reason,'observation_outside_event')

    def test_ordinary_job_cannot_be_substituted_for_event_investigation(self):
        receipt,job=self.complete()
        planned,_=self.plan(job)
        self.adapter.reply(planned['request_id'],self.transport)
        self.assertIn(UNAVAILABLE,self.transport.last_payload['text'])

    def test_plan_ack_does_not_read_evidence_or_queue_job(self):
        job=self.investigate()
        event=self.command('plan-recovery');event['event']['text']+=' '+job
        with patch.object(self.tasks,'result',side_effect=AssertionError('no ACK evidence read')), \
             patch.object(self.adapter.events,'request',side_effect=AssertionError('no queue')):
            receipt=self.receive(event)
        self.assertFalse(receipt['queue_accepted'])
        self.assertEqual(self.count('jobs'),1)

    def test_plan_commit_crash_does_not_create_second_job_on_retry(self):
        job=self.investigate()
        event=self.command('plan-recovery');event['event']['text']+=' '+job
        receipt=self.receive(event)
        bridge=self.adapter._event_bridge()
        with patch.object(type(bridge),'_ready',side_effect=OSError('simulated commit crash')):
            with self.assertRaises(OSError):self.adapter.drain_one()
        self.assertEqual(self.adapter._row(receipt['request_id'])['state'],'accepted')
        self.reopen();self.adapter.drain_one()
        self.assertEqual(self.saved(receipt['request_id']).job_id,job)
        self.assertEqual(self.count('jobs'),1)

    def test_review_nanosecond_event_boundary_rejects_earlier_observation(self):
        started='2026-10-16T01:00:00.000000001Z'
        self.eid=self.adapter.events.ingest('lab-seoul',{
            **self.alert,'fingerprint':'5123456789abcdef','startsAt':started})
        receipt,_=self.plan(self.investigate())
        plan=self.saved(receipt['request_id'])
        self.assertEqual(plan.assessment,'insufficient_evidence')
        self.assertEqual(plan.reason,'observation_outside_event')
        self.assertEqual(plan.event_started_at,started)
        self.assertEqual(RecoveryPlan.model_validate_json(plan.model_dump_json()),plan)

    def test_review_rehashed_plan_cannot_claim_pre_event_evidence_supported(self):
        receipt,_=self.plan(self.investigate())
        body=self.saved(receipt['request_id']).model_dump(mode='json')
        body['event_started_at']='2026-10-16T01:00:00.000000001Z'
        body['plan_hash']=sha256_json({k:v for k,v in body.items() if k!='plan_hash'})
        with self.assertRaisesRegex(ValueError,'support_rejected'):
            RecoveryPlan.model_validate(body)
