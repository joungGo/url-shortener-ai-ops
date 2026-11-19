"""WF-07 real queue/worker, real local Git/Helm, synthetic API/proof; no cloud."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_recovery_plans as plans
import test_c_recovery_proof as proof_fixture
import test_c_approval as git_fixture
from ai_ops.c_recovery import (ISOLATED_REPOSITORY, IMAGE_REPOSITORY, RecoveryDraftStore, _encoded_values)
from ai_ops.c_recovery_proof import assess_recovery_proof, assess_incident_request, assess_proposal_recheck
from ai_ops.c_execution_preflight import REQUIRED_CHECK
from ai_ops.contracts import sha256_json
from ai_ops.recovery_approvals import ApprovalPolicy, RecoveryApprovalLedger, slack_plan_source
from ai_ops.recovery_pr import ImageRecoveryPRBridge

REPO=ISOLATED_REPOSITORY
API='repos/'+REPO
UID='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
APP_UID='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
BAD='sha256:'+'a'*64
ACTOR='UALICE01'


class Reader(git_fixture.LocalGitReader):
    def get(self,endpoint):
        if endpoint in self.responses:return deepcopy(self.responses[endpoint])
        return super().get(endpoint.replace(API,git_fixture.REPO_API))


class Writer:
    mode='local_fixture'
    def __init__(self,test):self.t=test;self.calls=0;self.number=None;self.lose_response=False;self.hook=None
    def create(self,key,payload):
        self.calls+=1;t=self.t;r=t.reader
        r.git('checkout','-b',payload['branch'],payload['base_sha'])
        (r.root/payload['values_path']).write_text(payload['after'])
        head=r.commit(payload['commit_message'])
        self.number=123
        r.responses[API+'/pulls/123']=dict(number=123,state='open',draft=False,merged=False,
            created_at=t.now().isoformat(),user={'id':22,'type':'Bot'},mergeable=True,mergeable_state='clean',
            base={'ref':'main','sha':payload['base_sha'],'repo':{'full_name':REPO}},
            head={'ref':payload['branch'],'sha':head,'repo':{'full_name':REPO}})
        if self.hook:self.hook()
        if self.lose_response:raise OSError('secret-upstream-body')
        return self.number
    def lookup(self,key):return self.number


class RecoveryPRTest(unittest.TestCase):
    def setUp(self):
        self.f=plans.RecoveryPlansTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.snapshot(deployment_uid=UID,pod_deployment_uid=UID)
        receipt,_=self.f.plan(self.f.investigate())
        self.plan=self.f.saved(receipt['request_id']);self.key=self.plan.plan_hash
        self.root=self.f.queue.root.parent/'pr-local';self.root.mkdir()
        git=self.root/'git';git.mkdir();self.reader=Reader(git);self.reader.git('init','-b','main')
        shutil.copytree(Path(__file__).resolve().parents[1]/'lab',git/'ai-ops/lab')
        (git/'ai-ops/lab/values.yaml').write_text(_encoded_values(BAD))
        self.base=self.reader.commit('합성 장애 이미지 기준')
        self.policy=ApprovalPolicy(version=1,approvers=[ACTOR],wait_seconds=7200,
            approval_seconds=300,observation_seconds=30,revalidation_seconds=3)
        self.approvals=RecoveryApprovalLedger(self.f.queue,self.policy,
            load_plan=slack_plan_source(self.f.adapter,receipt['request_id']),observe=self.observation)
        self.drafts=RecoveryDraftStore(self.root/'drafts')
        self.make_bridge()
        self.writer=Writer(self)
        p=proof_fixture.RecoveryProofTest();p.setUp();self.raw=p
        p.binding.app.deployment_uid=UID
        normal_receipt=p.history.receipt.model_copy(update={
            'binding_hash':sha256_json(p.binding.model_dump(mode='json')),
            'finished_at':plans.AT-timedelta(seconds=30)})
        p.history=replace(p.history,receipt=normal_receipt)
        p.app['metadata']['uid']=APP_UID
        p.app['spec']['source'].update(repoURL='https://github.com/'+REPO+'.git',targetRevision=self.base)
        p.app['status']['sync']['revision']=self.base
        p.app['status']['operationState'].update(startedAt=(plans.AT-timedelta(seconds=20)).isoformat(),syncResult={'revision':self.base})
        p.deployment['metadata']['uid']=UID
        p.deployment['spec']['template']['spec']['containers'][0]['image']=IMAGE_REPOSITORY+'@'+BAD
        p.rs['metadata']['ownerReferences'][0]['uid']=UID
        p.rs['spec']['template']['spec']['containers'][0]['image']=IMAGE_REPOSITORY+'@'+BAD
        p.pods[0]['metadata']['creationTimestamp']=(plans.AT-timedelta(seconds=20)).isoformat()
        p.pods[0]['spec']['containers'][0]['image']=IMAGE_REPOSITORY+'@'+BAD
        p.pods[0]['status']['containerStatuses'][0]['image']=IMAGE_REPOSITORY+'@'+BAD
        p.values=_encoded_values(BAD).encode()
        p.lease.update(apiVersion='coordination.k8s.io/v1',kind='Lease')
        from ai_ops.b6_coordination import PURPOSE
        p.lease['metadata'].update(uid='lease-uid',resourceVersion='1',labels=dict(PURPOSE))
        self.proof_calls=0;self.proof_hook=None

    def now(self):return datetime.fromtimestamp(self.f.fixture.time,timezone.utc)

    def observation(self,plan,*,deadline):
        binding=plan.evidence.image_failure.model_copy(update={'observed_at':self.now()})
        from ai_ops.recovery_approvals import CurrentRecoveryState
        return CurrentRecoveryState(plan_hash=self.key,event_id=plan.event_id,target=plan.target,
            deployment_uid_hash=binding.deployment_uid_hash,revision_hash=sha256_json(self.base),
            image_hash=binding.image_hash,proof_hash='b'*64,observed_at=self.now(),status='image_failure',failure=binding)

    def make_bridge(self):
        self.bridge=ImageRecoveryPRBridge(self.approvals,self.drafts,principals={ACTOR:21},
            reader=self.reader,read_proof=self.proof,author_id=22,prepare_seconds=15)

    def proof(self,projection,request,*,deadline):
        self.f.fixture.time+=0.1;self.proof_calls+=1;p=self.raw
        if self.proof_hook:self.proof_hook()
        result=assess_recovery_proof(application=p.app,deployment=p.deployment,lease=p.lease,
            values_bytes=p.values,replica_set=p.rs,pods=p.pods,binding=p.binding,history=p.history,
            ecr_response=p.ecr,bad_digest_absent=True,now=self.now(),repository=REPO,expected_lease_holder=getattr(self,'expected_holder',None))
        return result,p.values

    def consent(self):
        record=self.approvals.open(self.key,actor=ACTOR)
        accepted=self.approvals.decide(self.key,actor=ACTOR,approval_hash=record['approval_hash'],
            generation=record['generation'],request_id='consent',decision='approve')
        return self.approvals.revalidate(self.key,actor=ACTOR,generation=accepted['generation'])

    def prepare(self):self.consent();return self.bridge.prepare(self.key,actor=ACTOR)
    def publish(self):self.prepare();return self.bridge.publish_local(self.key,actor=ACTOR,writer=self.writer)

    def check_setup(self):
        view=self.publish();self.f.fixture.time+=1
        self.reader.responses['user']={'id':21,'type':'User'}
        self.reader.responses[API+'/pulls/123/reviews?per_page=100']=[dict(id=9,user={'id':21,'type':'User'},
            state='APPROVED',commit_id=view['head_sha'],submitted_at=self.now().isoformat())]
        self.reader.responses[API+'/branches/main/protection']={
            'required_pull_request_reviews':{'required_approving_review_count':1,'dismiss_stale_reviews':True},
            'required_status_checks':{'strict':True,'contexts':[REQUIRED_CHECK]},
            'enforce_admins':{'enabled':True},'allow_force_pushes':{'enabled':False},'allow_deletions':{'enabled':False}}
        self.reader.responses[API+'/commits/'+view['head_sha']+'/check-runs?per_page=100']={
            'total_count':1,'check_runs':[dict(id=55,name=REQUIRED_CHECK,head_sha=view['head_sha'],
                status='completed',conclusion='success',app={'slug':'github-actions'})]}
        self.executor=dict(kind='github_app_installation',repository=REPO,actor_id=33,app_id=34,
            installation_id=35,permissions={'contents':'write','pull_requests':'read'})
        self.live=SimpleNamespace(read=self.live_read)
        return view

    def live_read(self,*,expected_lease=None):
        self.expected_holder=expected_lease['holder'] if expected_lease else None
        _,record=self.bridge._record(self.key,ACTOR)
        request=self.drafts.read_request(record['proposal']['request_id'])
        from ai_ops.c_recovery import read_incident_projection
        projection=read_incident_projection(self.f.queue.root,self.plan.evidence.incident_id,self.plan.evidence.run_id)
        a,_=self.proof(projection,request,deadline=self.f.fixture.time+15)
        a=assess_incident_request(a,projection,request,now=self.now())
        b,_=self.proof(projection,request,deadline=self.f.fixture.time+15)
        b=assess_incident_request(b,projection,request,now=self.now())
        proof=assess_proposal_recheck(a,b,now=self.now())
        return dict(application=deepcopy(self.raw.app),deployment=deepcopy(self.raw.deployment),
                    lease=deepcopy(self.raw.lease),values_bytes=self.raw.values,proof=proof,checked_at=self.now().isoformat())

    def preflight(self):
        return self.bridge.preflight(self.key,actor=ACTOR,live_reader=self.live,executor_identity=self.executor)

    def test_existing_plan_approval_real_git_and_render_prepare_one_digest(self):
        view=self.prepare();self.assertEqual(view['state'],'prepared');self.assertFalse(view['execution_allowed'])
        _,record=self.bridge._record(self.key,ACTOR)
        self.assertEqual(record['proposal']['schema_version'],2)
        self.assertEqual(record['proposal']['repository'],REPO)
        self.assertEqual(record['proposal']['current_digest'],BAD)
        self.assertEqual(record['proposal']['desired_digest'],self.raw.binding.app.image.split('@')[1])
        self.assertEqual(self.f.count('jobs'),1);self.assertEqual(self.writer.calls,0)
        self.assertEqual(self.bridge.prepare(self.key,actor=ACTOR),view)

    def test_missing_preparation_approval_blocks_before_proof(self):
        with self.assertRaises(ValueError):self.bridge.prepare(self.key,actor=ACTOR)
        self.assertEqual(self.proof_calls,0)

    def test_local_publication_returns_same_event_and_exact_review_head(self):
        view=self.publish();self.assertEqual(view['state'],'awaiting_review')
        self.assertEqual(view['url'],'https://github.com/'+REPO+'/pull/123')
        self.assertEqual(self.writer.calls,1)
        again=self.bridge.publish_local(self.key,actor=ACTOR,writer=self.writer)
        self.assertEqual(again,view);self.assertEqual(self.writer.calls,1)
        self.assertIn(self.plan.event_id,self.bridge.result_text(self.key,actor=ACTOR))
        self.assertEqual(self.f.transport.calls,0)

    def test_exact_review_ci_and_runtime_use_existing_preflight(self):
        self.check_setup();checked=self.preflight()
        self.assertTrue(checked['preflight_ready']);self.assertFalse(checked['execution_allowed'])
        self.assertFalse(checked['sync_allowed']);self.assertFalse(checked['merge_request_created'])
        self.assertEqual(checked['event_id'],self.plan.event_id)

    def test_uncertain_response_reconciles_readonly_without_duplicate_pr(self):
        self.prepare();self.writer.lose_response=True
        with self.assertRaisesRegex(ValueError,'uncertain'):
            self.bridge.publish_local(self.key,actor=ACTOR,writer=self.writer)
        self.assertEqual(self.bridge.show(self.key,actor=ACTOR)['state'],'needs_review')
        self.make_bridge()
        with self.assertRaises(ValueError):self.bridge.publish_local(self.key,actor=ACTOR,writer=self.writer)
        view=self.bridge.reconcile(self.key,actor=ACTOR,writer=self.writer)
        self.assertEqual(view['state'],'awaiting_review');self.assertEqual(self.writer.calls,1)

    def test_production_writer_is_disabled(self):
        self.prepare();self.writer.mode='live'
        with self.assertRaisesRegex(ValueError,'not_enabled'):
            self.bridge.publish_local(self.key,actor=ACTOR,writer=self.writer)
        self.assertEqual(self.writer.calls,0)

    def test_preparation_consent_does_not_expire_human_pr_review_at_30_seconds(self):
        self.check_setup();self.f.fixture.time+=120
        self.assertTrue(self.preflight()['preflight_ready'])

    def test_wrong_or_dismissed_head_review_blocks_preflight(self):
        self.check_setup();review=self.reader.responses[API+'/pulls/123/reviews?per_page=100'][0]
        review['commit_id']='f'*40
        with self.assertRaises(ValueError):self.preflight()
        review['commit_id']=self.bridge.show(self.key,actor=ACTOR)['head_sha'];review['state']='DISMISSED'
        with self.assertRaises(ValueError):self.preflight()

    def test_failed_ci_and_missing_review_protection_are_rejected(self):
        view=self.check_setup();check=self.reader.responses[API+'/commits/'+view['head_sha']+'/check-runs?per_page=100']['check_runs'][0]
        check['conclusion']='failure'
        with self.assertRaises(ValueError):self.preflight()
        check['conclusion']='success'
        self.reader.responses[API+'/branches/main/protection']['required_pull_request_reviews']['required_approving_review_count']=0
        with self.assertRaises(ValueError):self.preflight()

    def test_author_is_not_reviewer_or_merge_actor(self):
        self.check_setup();self.executor['actor_id']=22
        with self.assertRaises(ValueError):self.preflight()
        self.executor['actor_id']=21
        with self.assertRaises(ValueError):self.preflight()

    def test_normal_image_missing_from_ecr_and_changed_uid_stop_preparation(self):
        self.consent();self.raw.ecr={'imageDetails':[]}
        with self.assertRaises(ValueError):self.bridge.prepare(self.key,actor=ACTOR)
        self.assertEqual(self.writer.calls,0)
        self.assertEqual(self.bridge.show(self.key,actor=ACTOR)['state'],'needs_review')

    def test_main_diverged_before_publication_does_not_write(self):
        self.prepare();self.reader.git('checkout','main')
        (self.reader.root/'README.md').write_text('unrelated change')
        self.reader.commit('main advanced')
        with self.assertRaises(ValueError):self.bridge.publish_local(self.key,actor=ACTOR,writer=self.writer)
        self.assertEqual(self.writer.calls,0)

    def test_main_already_desired_has_no_pr_and_is_not_service_recovery(self):
        self.consent();(self.reader.root/'ai-ops/lab/values.yaml').write_text(_encoded_values(self.raw.binding.app.image.split('@')[1]))
        self.reader.commit('already desired')
        view=self.bridge.prepare(self.key,actor=ACTOR)
        self.assertEqual(view['state'],'no_pr');self.assertFalse(view['service_resolved'])
        self.assertEqual(self.writer.calls,0)

    def test_cancel_during_prepare_discards_completed_candidate(self):
        self.consent()
        self.proof_hook=lambda:self.bridge.interrupt(self.key,actor=ACTOR)
        view=self.bridge.prepare(self.key,actor=ACTOR)
        self.assertEqual(view['state'],'cancelled');self.assertEqual(self.writer.calls,0)

    def test_revoke_consent_during_proof_stops_before_publication(self):
        self.prepare()
        def revoke():
            self.proof_hook=None
            current=self.approvals.show(self.key,actor=ACTOR)
            self.approvals.decide(self.key,actor=ACTOR,approval_hash=current['approval_hash'],
                generation=current['generation'],request_id='cancel',decision='cancel')
        self.proof_hook=revoke
        with self.assertRaises(ValueError):self.bridge.publish_local(self.key,actor=ACTOR,writer=self.writer)
        self.assertEqual(self.writer.calls,0)

    def test_cancel_during_write_records_uncertain_then_reconciles_without_resume(self):
        self.prepare();self.writer.hook=lambda:self.bridge.interrupt(self.key,actor=ACTOR)
        first=self.bridge.publish_local(self.key,actor=ACTOR,writer=self.writer)
        self.assertEqual(first['state'],'needs_review')
        found=self.bridge.reconcile(self.key,actor=ACTOR,writer=self.writer)
        self.assertEqual(found['state'],'cancelled');self.assertEqual(found['pull_number'],123)
        self.assertEqual(self.writer.calls,1)

    def test_process_interrupt_in_write_requires_readonly_reconciliation(self):
        self.prepare();self.writer.hook=lambda:(_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):self.bridge.publish_local(self.key,actor=ACTOR,writer=self.writer)
        self.make_bridge()
        self.assertEqual(self.bridge.show(self.key,actor=ACTOR)['state'],'publishing')
        with self.assertRaises(ValueError):self.bridge.publish_local(self.key,actor=ACTOR,writer=self.writer)
        self.assertEqual(self.bridge.reconcile(self.key,actor=ACTOR,writer=self.writer)['state'],'awaiting_review')
        self.assertEqual(self.writer.calls,1)

    def test_ambiguous_or_missing_publication_never_retries(self):
        self.prepare()
        self.writer.create=lambda *args:(_ for _ in ()).throw(OSError('secret'))
        with self.assertRaises(ValueError):self.bridge.publish_local(self.key,actor=ACTOR,writer=self.writer)
        self.assertEqual(self.bridge.reconcile(self.key,actor=ACTOR,writer=self.writer)['state'],'needs_review')
        self.assertNotIn('secret',self.bridge.result_text(self.key,actor=ACTOR))

    def test_different_actor_and_policy_transition_invalidate_link(self):
        self.check_setup()
        with self.assertRaises(ValueError):self.bridge.show(self.key,actor='UBOB01')
        self.approvals.replace_policy(self.policy.model_copy(update={'version':2}))
        with self.assertRaises(ValueError):self.preflight()

    def test_preparation_deadline_remains_bounded(self):
        self.consent();self.proof_hook=lambda:setattr(self.f.fixture,'time',self.f.fixture.time+16)
        with self.assertRaises(ValueError):self.bridge.prepare(self.key,actor=ACTOR)
        self.assertEqual(self.writer.calls,0)

    def test_readonly_failure_retains_initial_failed_proposal_lineage(self):
        self.consent();self.raw.deployment['metadata']['uid']='cccccccc-cccc-cccc-cccc-cccccccccccc'
        with self.assertRaises(ValueError):self.bridge.prepare(self.key,actor=ACTOR)
        self.make_bridge()
        self.assertEqual(self.bridge.prepare(self.key,actor=ACTOR)['state'],'needs_review')
        self.assertEqual(self.writer.calls,0)

    def test_expired_source_prunes_copied_identity(self):
        self.publish();self.f.fixture.time=self.plan.retained_until.timestamp()
        self.bridge.prune()
        with self.f.queue.transaction() as db:record=self.bridge._get(db,self.key)
        for field in ('proposal','link','proof','payload','url','event_id','job_id'):
            self.assertIsNone(record[field])
        with self.assertRaises(ValueError):self.bridge.show(self.key,actor=ACTOR)

    def test_empty_lock_cannot_create_merge_request(self):
        self.check_setup()
        with self.assertRaises(ValueError):self.bridge.plan_merge(self.key,actor=ACTOR,locked={},
            live_reader=self.live,executor_identity=self.executor)

    def locked_plan(self):
        checked=self.preflight();holder='c3-'+'e'*32
        self.raw.lease['spec'].update(holderIdentity=holder,acquireTime=self.now().isoformat(),renewTime=self.now().isoformat())
        locked=dict(checked,locked_preflight_ready=True,checked_at=self.now(),
            lease={'name':'b6-scenario-lock','uid':'lease-uid','holder':holder})
        request=self.bridge.plan_merge(self.key,actor=ACTOR,locked=locked,live_reader=self.live,executor_identity=self.executor)
        return locked,request

    def merged_local(self):
        self.check_setup();locked,request=self.locked_plan()
        _,record=self.bridge._record(self.key,ACTOR)
        self.reader.git('checkout','main')
        (self.reader.root/'ai-ops/lab/values.yaml').write_text(record['payload']['after'])
        merged=self.reader.commit('local simulated squash')
        self.reader.responses[API+'/pulls/123'].update(state='closed',merged=True,merge_commit_sha=merged,merged_by={'id':33,'type':'Bot'})
        return locked,request,{'merged':True,'sha':merged}

    def test_exact_merge_and_sync_plans_reuse_guards_and_never_execute(self):
        locked,request,response=self.merged_local()
        self.assertFalse(request['write_enabled']);self.assertFalse(request['body']['bypass_rules'])
        self.assertEqual(request['body']['sha'],locked['head_sha'])
        sync=self.bridge.plan_sync(self.key,actor=ACTOR,merge_response=response,live_reader=self.live)
        self.assertFalse(sync['write_enabled']);self.assertEqual(sync['merge_sha'],response['sha'])
        snapshot=self.live_read(expected_lease=locked['lease'])
        snapshot['application']['spec']['source']['targetRevision']=response['sha']
        snapshot['application']['status']['sync'].update(status='Synced',revision=response['sha'])
        snapshot['application']['status']['operationState'].update(phase='Succeeded',syncResult={'revision':response['sha']})
        _,record=self.bridge._record(self.key,ACTOR)
        snapshot['deployment']['spec']['template']['spec']['containers'][0]['image']=IMAGE_REPOSITORY+'@'+record['proposal']['desired_digest']
        snapshot['checked_at']=datetime.fromisoformat(snapshot['checked_at'])
        result=self.bridge.record_sync(self.key,actor=ACTOR,snapshot=snapshot)
        self.assertTrue(result['sync_verified']);self.assertFalse(result['service_resolved'])
        self.assertEqual(result['event_id'],self.plan.event_id)
        self.assertEqual(self.writer.calls,1)

    def test_wrong_merge_revision_and_lost_lease_block_sync(self):
        locked,request,response=self.merged_local()
        with self.assertRaises(ValueError):self.bridge.plan_sync(self.key,actor=ACTOR,merge_response={**response,'sha':'f'*40},live_reader=self.live)
        self.raw.lease['spec']['holderIdentity']='c3-'+'f'*32
        with self.assertRaises(ValueError):self.bridge.plan_sync(self.key,actor=ACTOR,merge_response=response,live_reader=self.live)

    def test_preflight_cancellation_wins_over_read_success(self):
        self.check_setup();called=False
        def read(*,expected_lease=None):
            nonlocal called
            value=self.live_read(expected_lease=expected_lease)
            if not called:self.bridge.interrupt(self.key,actor=ACTOR);called=True
            return value
        with self.assertRaises(ValueError):self.bridge.preflight(self.key,actor=ACTOR,
            live_reader=SimpleNamespace(read=read),executor_identity=self.executor)

    def test_sync_rejects_first_observation_aged_during_second_read(self):
        locked,request,response=self.merged_local();calls=0
        def read(*,expected_lease):
            nonlocal calls
            calls+=1
            if calls==2:self.f.fixture.time+=16
            return self.live_read(expected_lease=expected_lease)
        with self.assertRaises(ValueError):self.bridge.plan_sync(self.key,actor=ACTOR,
            merge_response=response,live_reader=SimpleNamespace(read=read))
        self.assertEqual(self.bridge.show(self.key,actor=ACTOR)['state'],'awaiting_review')

    def test_approval_cancel_during_publication_keeps_receipt_but_blocks_resume(self):
        self.prepare()
        def cancel():
            current=self.approvals.show(self.key,actor=ACTOR)
            self.approvals.decide(self.key,actor=ACTOR,approval_hash=current['approval_hash'],
                generation=current['generation'],request_id='cancel-writing',decision='cancel')
        self.writer.hook=cancel
        result=self.bridge.publish_local(self.key,actor=ACTOR,writer=self.writer)
        self.assertEqual(result['state'],'cancelled');self.assertEqual(result['pull_number'],123)
        self.assertEqual(self.writer.calls,1)
        self.assertIn('취소',self.bridge.result_text(self.key,actor=ACTOR))

    def final_authority_hook(self, effect):
        original=self.bridge._prepared_authority
        calls=0
        def check(*args):
            nonlocal calls
            value=original(*args);calls+=1
            if calls==2:effect()
            return value
        return patch.object(self.bridge,'_prepared_authority',side_effect=check)

    def test_review_approval_cancel_before_final_db_fence_blocks_ready(self):
        self.check_setup()
        def cancel():
            current=self.approvals.show(self.key,actor=ACTOR)
            self.approvals.decide(self.key,actor=ACTOR,approval_hash=current['approval_hash'],
                generation=current['generation'],request_id='final-cancel',decision='cancel')
        with self.final_authority_hook(cancel):
            with self.assertRaises(ValueError):self.preflight()
        self.assertEqual(self.writer.calls,1)

    def test_review_source_retention_ends_before_final_db_fence(self):
        self.check_setup()
        def expire():self.f.fixture.time=self.plan.retained_until.timestamp()
        with self.final_authority_hook(expire):
            with self.assertRaises(ValueError):self.preflight()

    def test_review_final_source_lookup_cannot_return_stale_live_proof(self):
        self.check_setup()
        def delay():self.f.fixture.time+=16
        with self.final_authority_hook(delay):
            with self.assertRaises(ValueError):self.preflight()
