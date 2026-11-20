"""WF-07 local bridge from an approved WF-05 plan to existing image-PR contracts.

All readers are explicitly injected trusted read-only ports; there is no default
cloud client, Slack sender, token issuer, merge or sync executor. PR publication is
available only through a local fixture writer. Production transport is not wired.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import json

from ai_ops.contracts import sha256_json, _utc_datetime
from ai_ops.c_recovery import (ISOLATED_REPOSITORY, IMAGE_REPOSITORY, VALUES_PATH,
    RecoveryRequest, RecoveryProposal, read_incident_projection, draft_verified_proposal,
    _encoded_values)
from ai_ops.c_recovery_proof import assess_incident_request, assess_proposal_recheck
from ai_ops.c_approval import prepare_pr_link, _pr, _verify_diff
from ai_ops.c_pr_link import RecoveryPRLink
from ai_ops.c_execution_preflight import verify_execution_preflight, _check_live, MAX_PREFLIGHT_DURATION
from ai_ops.c_merge_guard import plan_exact_merge_request, verify_merged_commit
from ai_ops.c_sync_guard import plan_exact_sync, verify_sync_result
from ai_ops.gitops_drift_lock import assert_unlocked


def body(model):
    return model.model_dump(mode='json')


class ImageRecoveryPRBridge:
    """Durable one-plan/one-PR binding. Preparation is not merge authorization.

    `read_proof` must re-read/verify normal CI, ECR and current resources, then return
    assess_recovery_proof's result plus canonical values bytes. No user JSON input.
    `principals` is trusted configuration mapping Slack IDs to verified GitHub IDs.
    `reader` is the existing GitHub read contract (or local Git fixture equivalent).
    """
    def __init__(self, approvals, drafts, *, principals, reader, read_proof,
                 author_id, prepare_seconds):
        if (not isinstance(principals, dict) or not principals
                or any(not isinstance(k,str) or type(v) is not int or v<=0 for k,v in principals.items())
                or type(author_id) is not int or author_id<=0 or author_id in principals.values()
                or type(prepare_seconds) is not int or not 1<=prepare_seconds<=15):
            raise ValueError('pr_bridge_configuration_rejected')
        self.approvals, self.drafts, self.queue = approvals, drafts, approvals.queue
        self.principals = dict(principals)
        self.reader, self.read_proof = reader, read_proof
        self.author_id, self.prepare_seconds = author_id, prepare_seconds
        self.config_hash=sha256_json([principals,author_id,prepare_seconds,ISOLATED_REPOSITORY])
        with self.queue.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS recovery_pr_links (plan_hash TEXT PRIMARY KEY, document TEXT NOT NULL)')

    def now(self):
        return datetime.fromtimestamp(self.queue.now(),timezone.utc)

    def _principal(self, actor):
        if sha256_json([self.principals,self.author_id,self.prepare_seconds,ISOLATED_REPOSITORY])!=self.config_hash:
            raise ValueError('pr_bridge_configuration_changed')
        if actor not in self.principals:
            raise ValueError('recovery_pr_unavailable')
        return self.principals[actor]

    def _approval(self, key, actor, *, fresh=False):
        self._principal(actor)
        record=self.approvals.show(key,actor=actor)
        if record['state']!='proposal_ready' or record['approval_scope']!='prepare_proposal':
            raise ValueError('recovery_preparation_approval_inactive')
        if fresh and self.queue.now()>=record['fresh_until']:
            raise ValueError('recovery_preparation_observation_expired')
        return record

    @staticmethod
    def _same(first, second):
        if any(first[k]!=second[k] for k in ('plan_hash','approval_hash','generation','policy_hash')):
            raise ValueError('recovery_preparation_approval_changed')

    def _get(self, db, key):
        row=db.execute('SELECT document FROM recovery_pr_links WHERE plan_hash=?',(key,)).fetchone()
        return json.loads(row['document']) if row else None

    def _save(self, db, record):
        db.execute('INSERT OR REPLACE INTO recovery_pr_links VALUES(?,?)',
                   (record['plan_hash'],json.dumps(record,sort_keys=True)))

    def _source(self, key, actor):
        self._principal(actor)
        return self.approvals._plan(key,actor)

    def _record(self, key, actor):
        plan=self._source(key,actor)
        with self.queue.transaction() as db:record=self._get(db,key)
        if record is None or record['config_hash']!=self.config_hash:
            raise ValueError('recovery_pr_unavailable')
        return plan,record

    def _view(self, record):
        return {k:record[k] for k in ('plan_hash','event_id','job_id','state','reason','generation',
                                     'pull_number','head_sha','url','payload_hash')} | {
            'execution_allowed':False,'service_resolved':False}

    def show(self,key,*,actor):
        _,record=self._record(key,actor)
        return self._view(record)

    def prepare(self,key,*,actor):
        approval=self._approval(key,actor,fresh=True)
        plan=self._source(key,actor)
        with self.queue.transaction() as db:
            self._ready_in_db(db,key,approval)
            old=self._get(db,key)
            if old:
                if old['config_hash']!=self.config_hash:raise ValueError('recovery_pr_configuration_changed')
                return self._view(old)
            if db.execute('SELECT COUNT(*) FROM recovery_pr_links').fetchone()[0]>=10000:
                raise ValueError('recovery_pr_ledger_full')
            record=dict(plan_hash=key,event_id=plan.event_id,job_id=plan.job_id,
                config_hash=self.config_hash,approval_hash=approval['approval_hash'],
                approval_generation=approval['generation'],approval_policy_hash=approval['policy_hash'],state='preparing',reason='reading_proof',
                generation=0,cancel_requested=False,pull_number=None,head_sha=None,url=None,payload_hash=None,
                proposal=None,link=None,proof=None,payload=None,retained_until=plan.retained_until.timestamp())
            self._save(db,record)
        start=self.queue.now()
        deadline=min(start+self.prepare_seconds,approval['fresh_until'],approval['approved_until'])
        try:
            projection=read_incident_projection(self.queue.root,plan.evidence.incident_id,plan.evidence.run_id)
            if (projection.bundle_hash!=plan.evidence.bundle_hash or projection.report_hash!=plan.evidence.report_hash
                    or projection.image_failure!=plan.evidence.image_failure):
                raise ValueError('recovery_pr_projection_changed')
            # Deterministic source request key: crash/retry cannot mint another proposal lineage.
            request=RecoveryRequest(request_id='creq-'+key[:32],requester_github_id=self._principal(actor),
                incident_id=projection.incident_id,run_id=projection.run_id,bundle_hash=projection.bundle_hash,
                report_hash=projection.report_hash,failure_binding_hash=sha256_json(body(projection.image_failure)),
                target=plan.target,requested_at=plan.created_at)
            initial,values=self.read_proof(projection,request,deadline=deadline)
            first=assess_incident_request(initial,projection,request,now=self.now())
            latest,new_values=self.read_proof(projection,request,deadline=deadline)
            second=assess_incident_request(latest,projection,request,now=self.now())
            if values!=new_values:raise ValueError('recovery_pr_values_changed')
            proof=assess_proposal_recheck(first,second,now=self.now())
            identity=approval['identity']
            if (proof.get('source_repository')!=ISOLATED_REPOSITORY
                    or proof.get('deployment_uid_hash')!=identity['deployment_uid_hash']
                    or sha256_json(proof.get('base_sha'))!=identity['revision_hash']
                    or proof.get('image_hash')!=identity['image_hash']):
                raise ValueError('recovery_pr_approval_target_changed')
            proposal=draft_verified_proposal(request,projection,proof,new_values,now=self.now())
            result,link=prepare_pr_link(proposal,proof,proof,reader=self.reader,clock=self.now)
            final=self._approval(key,actor,fresh=True);self._same(approval,final)
            if not start<=self.queue.now()<deadline:raise ValueError('recovery_pr_prepare_expired')
            if link is None:
                record.update(state='no_pr',reason=result['status'])
            else:
                if link.repository!=ISOLATED_REPOSITORY:raise ValueError('recovery_pr_scope_rejected')
                payload=dict(repository=ISOLATED_REPOSITORY,base='main',base_sha=link.pr_base_sha,
                    branch='recovery/wf07-'+key,values_path=VALUES_PATH,
                    before=_encoded_values(proposal.current_digest),after=_encoded_values(proposal.desired_digest),
                    diff=proposal.diff,commit_message='격리 앱 정상 이미지 복구\n\nRecovery-Proposal-SHA256: '+link.link_hash,
                    title='격리 앱 이미지 복구 계획',
                    description={'event_id':plan.event_id,'job_id':plan.job_id,'plan_hash':key,
                        'proposal_hash':proposal.proposal_hash,'link_hash':link.link_hash,
                        'impact':proposal.impact_scope,'verification':proposal.verification_checks,
                        'failure_action':proposal.failure_action},write_enabled=False)
                self.drafts.save(request,proposal)
                record.update(state='prepared',reason='exact_diff_prepared',proposal=body(proposal),link=body(link),
                              proof=proof,payload=payload,payload_hash=sha256_json(payload))
            with self.queue.transaction() as db:
                current=self._get(db,key)
                if current['state']!='preparing' or current['generation']!=record['generation']:
                    return self._view(current)
                if not start<=self.queue.now()<deadline:raise ValueError('recovery_pr_prepare_expired')
                self._ready_in_db(db,key,approval)
                record['generation']+=1;self._save(db,record)
            return self._view(record)
        except Exception:
            self._fail(key,'preparing','preparation_failed')
            raise ValueError('recovery_pr_preparation_failed') from None

    def _fail(self,key,expected,reason):
        with self.queue.transaction() as db:
            record=self._get(db,key)
            if record and record['state']==expected:
                record.update(state='needs_review',reason=reason,generation=record['generation']+1)
                self._save(db,record)

    def interrupt(self,key,*,actor):
        """Explicit cancellation/restart recovery; never retries an uncertain write."""
        self._record(key,actor)
        with self.queue.transaction() as db:
            record=self._get(db,key)
            if record['state'] not in ('cancelled','no_pr'):
                uncertain=record['state'] in ('publishing','needs_review')
                record.update(cancel_requested=True,state='needs_review' if uncertain else 'cancelled',
                    reason='publication_may_exist' if uncertain else 'cancelled',generation=record['generation']+1)
                self._save(db,record)
            return self._view(record)

    def publish_local(self,key,*,actor,writer):
        """Exercise write ordering with a local Git fixture only; no production writer."""
        if getattr(writer,'mode',None)!='local_fixture':raise ValueError('live_pr_writer_not_enabled')
        _,record=self._record(key,actor)
        if record['state']=='awaiting_review':return self._view(record)
        approval=self._approval(key,actor,fresh=True)
        if record['state']!='prepared':raise ValueError('recovery_pr_not_publishable')
        if record['approval_hash']!=approval['approval_hash'] or record['approval_generation']!=approval['generation']:
            raise ValueError('recovery_pr_approval_changed')
        proposal,link=self._objects(record,actor)
        # Fresh independent proof and Git recheck immediately before fixture publication.
        request=self.drafts.read_request(proposal.request_id)
        plan=self._source(key,actor)
        projection=read_incident_projection(self.queue.root,plan.evidence.incident_id,plan.evidence.run_id)
        deadline=min(approval['fresh_until'],self.queue.now()+self.prepare_seconds)
        first,values=self.read_proof(projection,request,deadline=deadline)
        second,new_values=self.read_proof(projection,request,deadline=deadline)
        proof=assess_proposal_recheck(assess_incident_request(first,projection,request,now=self.now()),
                                    assess_incident_request(second,projection,request,now=self.now()),now=self.now())
        result,fresh_link=prepare_pr_link(proposal,record['proof'],proof,reader=self.reader,clock=self.now)
        if (values!=new_values or new_values.decode()!=record['payload']['before'] or fresh_link is None
                or fresh_link.pr_base_sha!=link.pr_base_sha or fresh_link.lab_tree_sha!=link.lab_tree_sha):
            raise ValueError('recovery_pr_prepublication_changed')
        self._same(approval,self._approval(key,actor,fresh=True))
        if self.queue.now()>=deadline:raise ValueError('recovery_pr_publication_expired')
        with self.queue.transaction() as db:
            current=self._get(db,key)
            if current['state']!='prepared' or current['generation']!=record['generation']:
                raise ValueError('recovery_pr_changed')
            self._ready_in_db(db,key,approval)
            if self.queue.now()>=deadline:raise ValueError('recovery_pr_publication_expired')
            record.update(state='publishing',reason='response_pending',generation=record['generation']+1)
            self._save(db,record)
        try:
            number=writer.create(key,json.loads(json.dumps(record['payload'])))
            return self._accept(key,actor,number,expected='publishing')
        except Exception:
            self._fail(key,'publishing','publication_outcome_unknown')
            raise ValueError('recovery_pr_publication_uncertain') from None

    def _objects(self,record,actor):
        proposal=RecoveryProposal.model_validate(record['proposal'])
        link=RecoveryPRLink.model_validate(record['link'])
        stored=self.drafts.read(proposal.proposal_id,actor_github_id=self._principal(actor),now=self.now())
        if (stored!=proposal or not link.matches_draft(proposal)
                or proposal.repository!=ISOLATED_REPOSITORY
                or sha256_json(record['payload'])!=record['payload_hash']):
            raise ValueError('recovery_pr_record_changed')
        return proposal,link

    def _accept(self,key,actor,number,*,expected):
        _,record=self._record(key,actor)
        proposal,link=self._objects(record,actor)
        if type(number) is not int or number<=0:raise ValueError('recovery_pr_number_invalid')
        endpoint=f'repos/{ISOLATED_REPOSITORY}/pulls/{number}'
        pull=self.reader.get(endpoint)
        head,created=_pr(pull,number,link.pr_base_sha,self.author_id,ISOLATED_REPOSITORY)
        if not link.created_at<=created<link.expires_at:raise ValueError('recovery_pr_time_invalid')
        if pull['head']['ref']!=record['payload']['branch']:raise ValueError('recovery_pr_branch_mismatch')
        _verify_diff(self.reader,proposal,link.pr_base_sha,head,link.link_hash)
        if self.reader.get(endpoint)!=pull:raise ValueError('recovery_pr_changed_during_read')
        with self.queue.transaction() as db:
            current=self._get(db,key)
            if current['state']!=expected or current['generation']!=record['generation']:
                return self._view(current)
            consent=self.approvals._get(db,key)
            if consent['state'] in ('cancelled','rejected','superseded','already_recovered'):
                current['cancel_requested']=True
            current.update(state='cancelled' if current['cancel_requested'] else 'awaiting_review',
                reason='cancelled_with_pr_recorded' if current['cancel_requested'] else 'exact_head_human_review_and_ci_required',
                generation=current['generation']+1,pull_number=number,head_sha=head,
                url=f'https://github.com/{ISOLATED_REPOSITORY}/pull/{number}')
            self._save(db,current)
            return self._view(current)

    def reconcile(self,key,*,actor,writer):
        if getattr(writer,'mode',None)!='local_fixture':raise ValueError('live_pr_writer_not_enabled')
        _,record=self._record(key,actor)
        if record['state'] not in ('publishing','needs_review'):return self._view(record)
        number=writer.lookup(key)  # Read-only; a missing/ambiguous outcome is never retried.
        if number is None:return self._view(record)
        return self._accept(key,actor,number,expected=record['state'])

    def preflight(self,key,*,actor,live_reader,executor_identity,expected_lease=None):
        # Human wait after PR creation follows the existing proposal/review contracts,
        # never extends WF06 approval or substitutes it for GitHub's exact-head review.
        started=self.now()
        _,record=self._record(key,actor)
        self._prepared_authority(key,actor,record)
        if record['state']!='awaiting_review':raise ValueError('recovery_pr_not_awaiting_review')
        proposal,link=self._objects(record,actor)
        latest=[]
        def observed_read(*,expected_lease=None):
            observation=live_reader.read(expected_lease=expected_lease)
            latest[:]=[deepcopy(observation)]
            return observation
        result=verify_execution_preflight(drafts=self.drafts,proposal_id=proposal.proposal_id,
            pull_number=record['pull_number'],allowed_github_id=self._principal(actor),pr_author_id=self.author_id,
            executor_identity=executor_identity,original_recheck=record['proof'],pr_link=link,
            live_reader=SimpleNamespace(read=observed_read),reader=self.reader,clock=self.now,expected_lease=expected_lease)
        self._prepared_authority(key,actor,record)
        if result['head_sha']!=record['head_sha']:raise ValueError('recovery_pr_head_changed')
        with self.queue.transaction() as db:
            current=self._get(db,key)
            if current['state']!=record['state'] or current['generation']!=record['generation']:
                raise ValueError('recovery_pr_cancelled_during_preflight')
            # WF06 cancellation can change its own ledger without changing this row.
            # Recheck it atomically, including retention after DB/source lookup waits.
            self._check_prepared_authority(self.approvals._get(db,key),actor,current);assert_unlocked(db)
            moment=self.now()
            if (not started<=moment or moment-started>MAX_PREFLIGHT_DURATION
                    or not proposal.created_at<=moment<min(proposal.expires_at,link.expires_at)):
                raise ValueError('recovery_pr_preflight_expired')
            _check_live(latest[0],proposal,link,moment,expected_lease)
        return dict(result,event_id=record['event_id'],job_id=record['job_id'],plan_hash=key,
                    execution_allowed=False,merge_request_created=False,sync_allowed=False)

    def _ready_in_db(self,db,key,original):
        assert_unlocked(db)  # a baseline-pinned rollback leaves main ahead: no new PR on it
        current=self.approvals._get(db,key)
        self._same(original,current)
        if current['state']!='proposal_ready':raise ValueError('recovery_pr_approval_inactive')

    def _prepared_authority(self,key,actor,record):
        with self.queue.transaction() as db:assert_unlocked(db)
        current=self.approvals.show(key,actor=actor)
        return self._check_prepared_authority(current,actor,record)

    def _check_prepared_authority(self,current,actor,record):
        if (self.queue.now()>=min(current['retained_until'],record['retained_until'])
                or current['approval_hash']!=record['approval_hash']
                or current['policy_hash']!=self.approvals.policy_hash
                or current['policy_hash']!=record['approval_policy_hash']
                or current['approved_by_hash']!=sha256_json(actor)
                or current['state'] in ('cancelled','rejected','superseded','already_recovered')
                or current['reason'] not in ('fresh_proposal_preparation_only','observation_expired','approval_or_plan_expired')
                or record['cancel_requested']):
            raise ValueError('recovery_pr_prepared_authority_revoked')
        return current

    def plan_merge(self,key,*,actor,locked,live_reader,executor_identity):
        """Read-only exact-head merge request. Caller must already own the verified lease."""
        checked=self.preflight(key,actor=actor,live_reader=live_reader,
                               executor_identity=executor_identity,expected_lease=locked.get('lease'))
        for name in ('proposal_id','pr_link_hash','head_sha','base_sha','pull_number','review_id',
                     'required_check_run_id','executor_actor_id'):
            if checked[name]!=locked.get(name):raise ValueError('recovery_pr_locked_binding_changed')
        _,record=self._record(key,actor);proposal,link=self._objects(record,actor)
        with self.queue.transaction() as db:
            current=self._get(db,key)
            if current['generation']!=record['generation'] or current['cancel_requested']:
                raise ValueError('recovery_pr_changed')
            self._check_prepared_authority(self.approvals._get(db,key),actor,current);assert_unlocked(db)
            request=plan_exact_merge_request(locked=locked,proposal=proposal,pr_link=link,now=self.now())
            current['locked']=json.loads(json.dumps(locked,default=lambda x:x.isoformat()))
            self._save(db,current)
        return request

    def plan_sync(self,key,*,actor,merge_response,live_reader):
        """Verify actual merge tree/actor/main before describing pinned sync; never runs it."""
        _,record=self._record(key,actor);self._prepared_authority(key,actor,record)
        if record['state']!='awaiting_review' or 'locked' not in record:
            raise ValueError('recovery_pr_locked_plan_required')
        proposal,link=self._objects(record,actor);locked=record['locked']
        merged=verify_merged_commit(reader=self.reader,locked=locked,proposal=proposal,
                                   pr_link=link,merge_response=merge_response)
        # The legacy guard accepts a fixed `now`, not a clock. Capture both fresh
        # reads before choosing that instant so neither observation is future-dated.
        # Its unchanged checks still validate both ages, equality, lease and Git.
        held=locked['lease']
        observations=[live_reader.read(expected_lease=held),live_reader.read(expected_lease=held)]
        snapshots=iter(observations)
        def captured_read(*,expected_lease):
            if expected_lease!=held:raise ValueError('recovery_pr_lease_changed')
            return next(snapshots)
        request=plan_exact_sync(reader=self.reader,live_reader=SimpleNamespace(read=captured_read),
                               locked=locked,proposal=proposal,pr_link=link,
                               merge_response=merge_response,now=self.now())
        self._prepared_authority(key,actor,record)
        with self.queue.transaction() as db:
            current=self._get(db,key)
            if current['generation']!=record['generation'] or current['cancel_requested']:
                raise ValueError('recovery_pr_changed')
            self._check_prepared_authority(self.approvals._get(db,key),actor,current);assert_unlocked(db)
            for observation in observations:
                _check_live(observation,proposal,link,self.now(),held)
            current.update(state='merge_verified',reason='pinned_sync_plan_only',merge_response=merge_response,
                           merged=merged,generation=current['generation']+1)
            self._save(db,current)
        return request

    def record_sync(self,key,*,actor,snapshot):
        _,record=self._record(key,actor);self._prepared_authority(key,actor,record)
        if record['state']!='merge_verified':raise ValueError('recovery_pr_merge_not_verified')
        proposal,link=self._objects(record,actor)
        result=verify_sync_result(reader=self.reader,snapshot=snapshot,locked=record['locked'],proposal=proposal,
                                 pr_link=link,merge_response=record['merge_response'],now=self.now())
        self._prepared_authority(key,actor,record)
        with self.queue.transaction() as db:
            current=self._get(db,key)
            if current['generation']!=record['generation'] or current['cancel_requested']:
                raise ValueError('recovery_pr_changed')
            self._check_prepared_authority(self.approvals._get(db,key),actor,current);assert_unlocked(db)
            observed=_utc_datetime(snapshot['checked_at'])
            if not 0<=(self.now()-observed).total_seconds()<=15:
                raise ValueError('recovery_pr_sync_observation_expired')
            current.update(state='sync_verified',reason='service_verification_still_required',
                           sync_result=result,sync_verified_at=observed.isoformat(),generation=current['generation']+1)
            self._save(db,current)
        return {**result,'event_id':record['event_id'],'execution_allowed':False}

    def result_text(self,key,*,actor):
        value=self.show(key,actor=actor)
        detail={
            'merge_verified':'병합 커밋을 검증했습니다. 동기화 실행이나 서비스 회복을 뜻하지 않습니다.',
            'sync_verified':'동기화 관측 자료를 검증했습니다. 서비스 회복은 별도 효과 검증이 필요합니다.',
            'cancelled':'취소됐습니다. 이미 생성된 PR의 자동 병합·재실행은 하지 않습니다.',
            'needs_review':'처리 결과를 확정할 수 없어 중지했습니다. 조회로 확인하며 자동 재생성하지 않습니다.',
        }.get(value['state'],'정확한 head의 GitHub 사람 리뷰·CI와 실행 직전 검증이 별도로 필요합니다. '
                            '병합·배포·서비스 회복을 실행하거나 확인한 결과가 아닙니다.')
        return (f"복구 PR 준비: {value['state']}\n사건: {value['event_id']}\n조사: {value['job_id']}\n"
                + (f"PR: {value['url']}\n검토할 head: {value['head_sha']}\n" if value['url'] else '')
                + detail)

    def prune(self):
        with self.queue.transaction() as db:
            for row in db.execute('SELECT document FROM recovery_pr_links').fetchall():
                record=json.loads(row['document'])
                if self.queue.now()>=record['retained_until']:
                    record.update(state='expired',reason='source_expired',proposal=None,link=None,proof=None,
                                  payload=None,url=None,head_sha=None,event_id=None,job_id=None,locked=None,merged=None,
                                  merge_response=None,sync_result=None)
                    self._save(db,record)
