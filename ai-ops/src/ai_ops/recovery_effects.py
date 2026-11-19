"""WF09 local effect verification and bounded follow-up/cleanup contracts.

All sources and ports are trusted internal adapters, never Slack/user JSON. No
cloud client, shell, sender or production writer is installed. Hashes bind data;
they do not authenticate it. Live collectors/approval/cleanup wiring is deferred.
"""
from __future__ import annotations
from datetime import datetime, timezone
import json
from typing import Annotated, Literal
from pydantic import Field, model_validator
from ai_ops.contracts import StrictModel, Target, Sha256, UTCDateTime, sha256_json
from ai_ops.c_recovery import TARGET, RecoveryProposal
from ai_ops.c_verification import (VerificationPolicy, HTTPReading, MetricReading,
    AlertReading, _reading_result)
from ai_ops.recovery_commands import RestartPlan, RestartSource, body, UUID


class ExecutionReceipt(StrictModel):
    method: Literal['pr','command']
    plan_hash: Sha256
    event_id: str = Field(pattern=r'^evt-[a-f0-9]{64}$')
    job_id: str = Field(pattern=r'^job-[a-f0-9]{32}$')
    target: Target
    deployment_uid: str = Field(pattern=UUID)
    application_uid: str | None
    revision: str  # PR commit SHA; command's sealed semantic revision hash.
    digest: str = Field(pattern=r'^sha256:[a-f0-9]{64}$')
    marker: Sha256 | None
    old_pod_uid_hash: Sha256 | None
    generation: int | None = Field(ge=1)
    applied_at: UTCDateTime | None
    applied: bool
    cancelled: bool
    source_hash: Sha256
    lease_hash: Sha256 | None
    lease_released: bool
    retained_until: UTCDateTime

    @model_validator(mode='after')
    def valid(self):
        import re
        if self.target!=TARGET or (self.applied_at is not None and not self.applied):raise ValueError('effect_receipt_invalid')
        if self.method=='pr':
            if not re.fullmatch(r'[a-f0-9]{40}',self.revision) or not re.fullmatch(UUID,self.application_uid or '') or self.marker is not None:raise ValueError('effect_pr_binding_invalid')
        elif (not re.fullmatch(r'[a-f0-9]{64}',self.revision) or self.application_uid is not None
                or self.marker is None or self.old_pod_uid_hash is None or self.generation is None):raise ValueError('effect_command_binding_invalid')
        return self

    def digest_key(self):
        # Cancellation/cleanup can advance without changing the applied operation.
        return sha256_json(self.model_dump(mode='json',exclude={'cancelled','lease_released'}))


def command_effect_source(coordinator):
    def load(key,actor):
        with coordinator.queue.transaction() as db:
            row=db.execute('SELECT document FROM restart_plans WHERE plan_hash=?',(key,)).fetchone()
            run=coordinator._get(db,key)
        if row is None:raise ValueError('effect_source_unavailable')
        p=RestartPlan.model_validate_json(row[0])
        original=RestartSource.model_validate(body(coordinator.source.for_effects(p.candidate_hash,actor)))
        if (sha256_json(body(original))!=p.source_hash or coordinator.queue.now()>=p.retained_until.timestamp()
                or (p.executor_id,p.execution_seconds)!=(coordinator.executor_id,coordinator.execution_seconds)):
            raise ValueError('effect_source_changed')
        lease=run.get('lease')
        with coordinator.queue.transaction() as db:
            a=db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='restart_approvals'").fetchone()
            a=db.execute('SELECT document FROM restart_approvals WHERE plan_hash=?',(key,)).fetchone() if a else None
        cancelled=run['cancel_requested'] or bool(a and json.loads(a[0])['state']=='cancelled')
        return ExecutionReceipt(method='command',plan_hash=key,event_id=p.event_id,job_id=p.job_id,target=p.target,
            deployment_uid=p.deployment_uid,application_uid=None,revision=p.revision_hash,
            digest=original_image(original,coordinator,key,actor),
            marker=p.marker,old_pod_uid_hash=p.pod_uid_hash,generation=p.generation+1,
            applied=run['applied'],applied_at=run.get('applied_at'),cancelled=cancelled,
            source_hash=sha256_json([body(p),run.get('applied_at'),run['applied']]),
            lease_hash=sha256_json(lease) if lease else None,lease_released=run['lease_released'],retained_until=p.retained_until)
    return load


def original_image(source,coordinator,key,actor):
    # The sealed exact request does not carry the image plaintext; its original
    # audited source supplies the pinned image through a separate trusted accessor.
    image=coordinator.source.image_for_effects(source.candidate.plan_hash,actor)
    if sha256_json(image)!=source.image_hash:raise ValueError('effect_image_changed')
    return image.split('@',1)[1]


def pr_effect_source(bridge):
    def load(key,actor):
        plan,r=bridge._record(key,actor)
        if not r.get('proposal'):raise ValueError('effect_source_unavailable')
        p=RecoveryProposal.model_validate(r['proposal'])
        result=r.get('sync_result') or {}
        # No network/re-merge here: the stored WF07 record was committed only after
        # exact head/review/CI/merge/sync validation. Future readers authenticate it.
        applied=result.get('sync_verified') is True
        revision=result.get('merge_sha') or r.get('merged',{}).get('merge_sha') or r['link']['observed_revision_sha']
        with bridge.queue.transaction() as db:a=bridge.approvals._get(db,key)
        return ExecutionReceipt(method='pr',plan_hash=key,event_id=plan.event_id,job_id=plan.job_id,target=plan.target,
            deployment_uid=p.target_uid,application_uid=r['link']['application_uid'],revision=revision,
            digest=p.desired_digest,marker=None,old_pod_uid_hash=None,generation=None,
            applied=applied,applied_at=r.get('sync_verified_at'),cancelled=r['cancel_requested'] or a['state']=='cancelled',
            source_hash=sha256_json([r['proposal'],r.get('locked'),r.get('merged'),result,r.get('sync_verified_at')]),
            lease_hash=sha256_json(r['locked']['lease']) if r.get('locked') else None,lease_released=False,
            retained_until=plan.retained_until)
    return load


class EffectPolicy(VerificationPolicy):
    # No implicit production defaults. Existing C04 policy is not edited.
    minimum_span_seconds: int = Field(ge=120,le=900)
    maximum_gap_seconds: int = Field(ge=1,le=45)
    minimum_samples: int = Field(ge=4,le=100)
    maximum_report_delay_seconds: int = Field(ge=1,le=120)
    read_seconds: int = Field(ge=1,le=15)


class RuntimeEffect(StrictModel):
    kind: Literal['runtime']='runtime'
    revision: str
    application_uid: str | None
    digest: str = Field(pattern=r'^sha256:[a-f0-9]{64}$')
    generation: int = Field(ge=1)
    observed_generation: int = Field(ge=0)
    replicas: int = Field(ge=0)
    ready_replicas: int = Field(ge=0)
    pod_uid_hashes: list[Sha256] = Field(max_length=20)
    ready_pods: int = Field(ge=0)
    marker: Sha256 | None
    argo_synced_healthy: bool | None


Reading=Annotated[RuntimeEffect|HTTPReading|MetricReading|AlertReading,Field(discriminator='kind')]
class EffectSample(StrictModel):
    at: UTCDateTime
    reading: Reading
    # Captured only: hash of the raw response(s) read for this sample. It binds what
    # was seen at ``at``; it does not authenticate it (bodies are never stored).
    source_sha256: Sha256 | None=None


class EffectSeries(StrictModel):
    kind: Literal['runtime','http','metric','alert']
    name: str = Field(min_length=1,max_length=128)
    target: Target
    deployment_uid: str = Field(pattern=UUID)
    execution_hash: Sha256
    definition_hash: Sha256 | None=None
    source_hash: Sha256
    status: Literal['available','missing','collection_failed']
    samples: list[EffectSample] = Field(default_factory=list,max_length=100)

    @model_validator(mode='after')
    def valid(self):
        if (self.status!='available' and self.samples) or any(x.reading.kind!=self.kind for x in self.samples):raise ValueError('effect_series_invalid')
        return self


class EffectBatch(StrictModel):
    provenance: Literal['synthetic','captured']='synthetic'
    run_id: str | None = Field(default=None,pattern=r'^effect-[a-f0-9]{32}$')
    collector: str | None = Field(default=None,pattern=r'^local-restart-[a-z0-9-]{1,40}$')
    series: list[EffectSeries] = Field(max_length=32)
    @model_validator(mode='after')
    def unique(self):
        if len({(x.kind,x.name) for x in self.series})!=len(self.series):raise ValueError('effect_series_duplicate')
        hashed=[x.source_sha256 is not None for s in self.series for x in s.samples]
        if self.provenance=='captured':
            if self.run_id is None or self.collector is None or not all(hashed):raise ValueError('effect_capture_unbound')
        elif self.run_id is not None or self.collector is not None or any(hashed):raise ValueError('effect_synthetic_carries_capture')
        return self


def verify_effect(receipt,policy,batch,*,now):
    """Pure evaluator. Missing, stale or wrong-identity data never becomes zero/healthy."""
    receipt=ExecutionReceipt.model_validate(body(receipt));policy=EffectPolicy.model_validate(body(policy));batch=EffectBatch.model_validate(body(batch))
    wanted=[('runtime','lab-api',None),*[('http',p,None) for p in policy.http_paths],
            *[('metric',m.name,m) for m in policy.metrics],*[('alert',a.name,a) for a in policy.alerts]]
    index={(s.kind,s.name):s for s in batch.series};checks=[]
    valid_window=(receipt.applied and receipt.applied_at is not None and policy.window_start is not None
        and receipt.applied_at<=policy.window_start<policy.window_end<=now<receipt.retained_until
        and (now-policy.window_end).total_seconds()<=policy.maximum_report_delay_seconds)
    for kind,name,requirement in wanted:
        s=index.get((kind,name));reasons=[];outcomes=[];times=[]
        if not valid_window:reasons.append('invalid_or_unconfirmed_execution_window')
        elif s is None or s.status!='available':reasons.append('missing_or_failed_collection')
        elif s.target!=receipt.target or s.deployment_uid!=receipt.deployment_uid or s.execution_hash!=receipt.digest_key():reasons.append('identity_mismatch')
        elif requirement and s.definition_hash!=requirement.definition_hash:reasons.append('definition_mismatch')
        else:
            for sample in s.samples:
                if not policy.window_start<=sample.at<=policy.window_end:reasons.append('outside_window');continue
                times.append(sample.at);reading=sample.reading
                if isinstance(reading,RuntimeEffect):
                    common=(reading.revision==receipt.revision and reading.digest==receipt.digest
                        and reading.generation==reading.observed_generation
                        and reading.replicas==reading.ready_replicas==reading.ready_pods==len(reading.pod_uid_hashes)==1)
                    if receipt.method=='pr':
                        ok=common and reading.application_uid==receipt.application_uid and reading.argo_synced_healthy is True
                    else:
                        ok=(common and reading.application_uid is None and reading.marker==receipt.marker
                            and reading.generation==receipt.generation and receipt.old_pod_uid_hash not in reading.pod_uid_hashes)
                    outcome='passed' if ok else 'failed'
                else:outcome=_reading_result(reading,None,requirement,sample.at,policy.maximum_gap_seconds)
                outcomes.append(outcome)
            if len(times)<policy.minimum_samples:reasons.append('insufficient_samples')
            if times:
                if any(not 0<(b-a).total_seconds()<=policy.maximum_gap_seconds for a,b in zip(times,times[1:])):reasons.append('unordered_or_gapped_samples')
                if ((times[-1]-times[0]).total_seconds()<policy.minimum_span_seconds
                    or (times[0]-policy.window_start).total_seconds()>policy.maximum_gap_seconds
                    or (policy.window_end-times[-1]).total_seconds()>policy.maximum_gap_seconds):reasons.append('incomplete_window')
            if 'unknown' in outcomes:reasons.append('unknown_reading')
        state='failed' if 'failed' in outcomes else 'unknown' if reasons or not outcomes else 'passed'
        checks.append(dict(kind=kind,name=name,state=state,reasons=sorted(set(reasons)),evidence_hash=sha256_json(body(s)) if s else None))
    extras=bool(set(index)-{(k,n) for k,n,_ in wanted})
    status='unresolved' if any(c['state']=='failed' for c in checks) else 'succeeded' if all(c['state']=='passed' for c in checks) and not extras else 'verification_unknown'
    return dict(status=status,service_resolved=status=='succeeded',checks=checks,unexpected_series=extras,
        execution_hash=receipt.digest_key(),policy_hash=sha256_json(body(policy)),evidence_hash=sha256_json(body(batch)),
        evaluated_at=now.isoformat(),provenance=batch.provenance,execution_allowed=False)


class CleanupItem(StrictModel):
    kind: Literal['lease','credential','temporary_resource']
    item_hash: Sha256
    owner_hash: Sha256
    action: Literal['release_lease','revoke_credential','remove_temporary']
    @model_validator(mode='after')
    def valid(self):
        if self.action!={'lease':'release_lease','credential':'revoke_credential','temporary_resource':'remove_temporary'}[self.kind]:raise ValueError('cleanup_action_invalid')
        return self


class RollbackScope(StrictModel):
    """Output of a future authenticated approval reader, not an approval issuer."""
    execution_hash: Sha256
    approval_hash: Sha256
    baseline_proof_hash: Sha256
    target: Target
    deployment_uid: str = Field(pattern=UUID)
    application_uid: str = Field(pattern=UUID)
    revision: str = Field(pattern=r'^[a-f0-9]{40}$')
    digest: str = Field(pattern=r'^sha256:[a-f0-9]{64}$')
    issued_at: UTCDateTime
    expires_at: UTCDateTime
    revoked: bool
    attempts_used: int = Field(ge=0)
    maximum_attempts: Literal[1]=1
    @model_validator(mode='after')
    def valid(self):
        if self.target!=TARGET or self.expires_at<=self.issued_at:raise ValueError('rollback_scope_invalid')
        return self


class EffectCoordinator:
    """Uses the existing private queue DB. Only cleanup has a local fixture write port."""
    def __init__(self,queue,*,source,policy):
        self.queue,self.source=queue,source
        self.policy=EffectPolicy.model_validate(body(policy));self.policy_hash=sha256_json(body(self.policy))
        with queue.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS recovery_effects (plan_hash TEXT PRIMARY KEY,document TEXT NOT NULL)')

    def _get(self,db,key):
        row=db.execute('SELECT document FROM recovery_effects WHERE plan_hash=?',(key,)).fetchone()
        if row is None:raise ValueError('effect_unavailable')
        return json.loads(row[0])
    def _save(self,db,r):db.execute('INSERT OR REPLACE INTO recovery_effects VALUES(?,?)',(r['plan_hash'],json.dumps(r,sort_keys=True)))
    def now(self):return datetime.fromtimestamp(self.queue.now(),timezone.utc)
    def _source(self,key,actor):
        e=ExecutionReceipt.model_validate(body(self.source(key,actor)))
        if e.plan_hash!=key or self.now()>=e.retained_until or sha256_json(body(self.policy))!=self.policy_hash:raise ValueError('effect_source_invalid')
        return e
    def _bound(self,db,key,e):
        r=self._get(db,key)
        if (r['expired'] or r['execution_hash']!=e.digest_key() or r['policy_hash']!=self.policy_hash
                or self.queue.now()>=r['retained_until']):raise ValueError('effect_binding_changed')
        if e.cancelled and not r['cancel_requested']:
            r.update(cancel_requested=True,state='cancelled',generation=r['generation']+1)
            self._save(db,r)
        return r

    def open(self,key,*,actor,cleanup_items):
        e=self._source(key,actor);items=[CleanupItem.model_validate(body(x)) for x in cleanup_items]
        if len(items)>16 or len({x.item_hash for x in items})!=len(items):raise ValueError('cleanup_manifest_invalid')
        if e.lease_hash and not e.lease_released and not any(i.kind=='lease' and i.item_hash==e.lease_hash for i in items):raise ValueError('owned_lease_not_accounted')
        manifest=sha256_json([body(x) for x in items])
        with self.queue.transaction() as db:
            old=db.execute('SELECT 1 FROM recovery_effects WHERE plan_hash=?',(key,)).fetchone()
            if old:
                r=self._bound(db,key,e)
                if r['manifest_hash']!=manifest:raise ValueError('cleanup_manifest_changed')
            else:
                if db.execute('SELECT count(*) FROM recovery_effects').fetchone()[0]>=10000:raise ValueError('effect_ledger_full')
                if self.now()>=e.retained_until:raise ValueError('effect_source_expired')
                r=dict(plan_hash=key,event_id=e.event_id,job_id=e.job_id,execution_hash=e.digest_key(),policy_hash=self.policy_hash,
                    retained_until=e.retained_until.timestamp(),expired=False,state='awaiting_evidence',generation=0,cancel_requested=e.cancelled,
                    first_result=None,result=None,rollback=None,rollback_result=None,manifest_hash=manifest,
                    cleanup=[dict(item=body(i),state='pending') for i in items],last_seen=self.queue.now())
                self._save(db,r)
        return self.show(key,actor=actor)

    def show(self,key,*,actor):
        e=self._source(key,actor)
        with self.queue.transaction() as db:r=self._bound(db,key,e)
        return {**r,'service_resolved':bool((r['rollback_result'] or r['result']) and (r['rollback_result'] or r['result'])['service_resolved']),
            'cleanup_complete':all(x['state']=='confirmed_absent' for x in r['cleanup']),
            'execution_allowed':False,'slack_delivered':False,'cleanup_scope':'declared_owned_manifest_only'}

    def cancel(self,key,*,actor):
        e=self._source(key,actor)
        with self.queue.transaction() as db:
            r=self._bound(db,key,e);r.update(cancel_requested=True,state='cancelled',generation=r['generation']+1);self._save(db,r)
        return self.show(key,actor=actor)

    def verify_local(self,key,*,actor,reader):
        e=self._source(key,actor)
        if getattr(reader,'mode',None)!='local_fixture':raise ValueError('live_effect_reader_disabled')
        with self.queue.transaction() as db:
            r=self._bound(db,key,e)
            if r['result'] is not None:return self.show_outside(r)
            if self.queue.now()<r['last_seen']:raise ValueError('effect_clock_regressed')
            generation=r['generation']
        start=self.queue.now();deadline=min(start+self.policy.read_seconds,e.retained_until.timestamp())
        try:
            batch=EffectBatch.model_validate(body(reader.read(e,self.policy,deadline=deadline)))
            current=self._source(key,actor)
            report=verify_effect(e,self.policy,batch,now=self.now())
            with self.queue.transaction() as db:
                r=self._bound(db,key,current)
                if r['generation']!=generation:return self.show_outside(r)
                if not start<=self.queue.now()<deadline:raise ValueError('effect_read_expired')
                # DB admission time is the final evaluation time, not the pre-lock clock.
                report=verify_effect(e,self.policy,batch,now=self.now())
                r['cancel_requested']|=current.cancelled
                r.update(first_result=report,result=report,state='cancelled' if r['cancel_requested'] else report['status'],
                    generation=r['generation']+1,last_seen=self.queue.now());self._save(db,r)
        except Exception:
            with self.queue.transaction() as db:
                r=self._get(db,key)
                if r['generation']==generation and not r['expired']:
                    report=dict(status='verification_unknown',service_resolved=False,checks=[],reason='read_failed_or_expired',execution_allowed=False)
                    r.update(first_result=report,result=report,state='cancelled' if r['cancel_requested'] else 'verification_unknown',generation=r['generation']+1);self._save(db,r)
        return self.show(key,actor=actor)

    @staticmethod
    def show_outside(r):
        return {**r,'service_resolved':bool((r['rollback_result'] or r['result']) and (r['rollback_result'] or r['result'])['service_resolved']),
                'cleanup_complete':all(x['state']=='confirmed_absent' for x in r['cleanup']),'execution_allowed':False,'slack_delivered':False,'cleanup_scope':'declared_owned_manifest_only'}

    def follow_up(self,key,*,actor,scope_reader=None):
        e=self._source(key,actor)
        with self.queue.transaction() as db:r=self._bound(db,key,e);generation=r['generation']
        result=r['result'];decision='manual_handoff';reason='verification_required';scope=None
        if r['cancel_requested'] or e.cancelled:reason='cancelled_no_follow_up'
        elif r['rollback_result'] and r['rollback_result']['service_resolved']:
            decision,reason='report_result','rollback_effect_verified'
        elif r.get('rollback_attempted'):reason='rollback_attempt_consumed'
        elif result and result['status']=='succeeded':decision,reason='report_result','effect_verified'
        elif e.method=='command':reason='restart_not_reversible_no_retry'
        elif not result or result['status']!='unresolved':reason='missing_evidence_no_automatic_rollback'
        elif any(x['state']=='unknown' or x['reasons'] for x in result['checks']):reason='incomplete_evidence_no_automatic_rollback'
        elif scope_reader is None:decision,reason='request_new_approval','no_prior_rollback_scope'
        else:
            scope=RollbackScope.model_validate(body(scope_reader(e)))
            fresh=(result.get('evaluated_at') and 0<=(self.now()-datetime.fromisoformat(result['evaluated_at'])).total_seconds()<=self.policy.maximum_report_delay_seconds)
            if (scope.execution_hash!=e.digest_key() or scope.target!=e.target or scope.deployment_uid!=e.deployment_uid
                or scope.application_uid!=e.application_uid or scope.revoked or e.applied_at is None
                or not scope.issued_at<=e.applied_at<=self.now()<scope.expires_at or not fresh):
                decision,reason='request_new_approval','scope_stale_revoked_or_wrong_binding'
            elif scope.attempts_used>=scope.maximum_attempts:reason='rollback_attempt_consumed'
            elif (scope.revision,scope.digest)==(e.revision,e.digest):reason='not_a_rollback_no_retry'
            else:decision,reason='rollback_candidate','existing_execution_guard_revalidation_required'
        current=self._source(key,actor)
        with self.queue.transaction() as db:
            r=self._bound(db,key,current)
            if r['cancel_requested'] or current.cancelled:
                decision,reason='manual_handoff','cancelled_no_follow_up'
            elif r['generation']!=generation:raise ValueError('effect_follow_up_changed')
            if scope and self.now()>=scope.expires_at and not (r['cancel_requested'] or current.cancelled):
                decision,reason='request_new_approval','scope_expired'
            if decision=='rollback_candidate' and not 0<=(self.now()-datetime.fromisoformat(result['evaluated_at'])).total_seconds()<=self.policy.maximum_report_delay_seconds:
                decision,reason='manual_handoff','effect_result_stale'
            value=dict(decision=decision,reason=reason,scope_hash=sha256_json(body(scope)) if scope else None,
                change={'revision':scope.revision,'digest':scope.digest} if decision=='rollback_candidate' else None,
                requires_execution_revalidation=decision=='rollback_candidate',execution_allowed=False)
            r.update(rollback=value,generation=r['generation']+1);self._save(db,r)
        return value

    def record_rollback_local(self,key,*,actor,scope_reader,reader,policy):
        """Observe a separately authorized rollback, never execute/retry it here."""
        e=self._source(key,actor)
        if getattr(reader,'mode',None)!='local_fixture':raise ValueError('live_rollback_reader_disabled')
        policy=EffectPolicy.model_validate(body(policy))
        if policy.model_dump(exclude={'window_start','window_end'})!=self.policy.model_dump(exclude={'window_start','window_end'}):
            raise ValueError('rollback_observation_policy_changed')
        scope=RollbackScope.model_validate(body(scope_reader(e)))
        with self.queue.transaction() as db:
            r=self._bound(db,key,e)
            if (e.cancelled or r['cancel_requested'] or r['rollback_result'] is not None or not r['rollback']
                or r['rollback']['decision']!='rollback_candidate' or r['rollback']['scope_hash']!=sha256_json(body(scope))
                or r.get('rollback_attempted')):raise ValueError('rollback_observation_not_authorized')
            r['rollback_attempted']=True;r['generation']+=1;generation=r['generation'];self._save(db,r)
        start=self.queue.now();deadline=min(start+policy.read_seconds,e.retained_until.timestamp())
        report=dict(status='verification_unknown',service_resolved=False,reason='rollback_observation_unavailable',execution_allowed=False)
        try:
            restored=ExecutionReceipt.model_validate(body(reader.applied(e,scope,deadline=deadline)))
            if (scope.revoked or scope.attempts_used or e.applied_at is None or scope.issued_at>e.applied_at
                or restored.cancelled or restored.method!='pr' or not restored.applied or restored.applied_at is None
                or not e.applied_at<=restored.applied_at<scope.expires_at
                or restored.plan_hash!=key or restored.event_id!=e.event_id or restored.job_id!=e.job_id
                or restored.target!=e.target or restored.deployment_uid!=e.deployment_uid
                or restored.application_uid!=e.application_uid or restored.revision!=scope.revision
                or restored.digest!=scope.digest or restored.retained_until!=e.retained_until):
                raise ValueError('rollback_receipt_mismatch')
            batch=EffectBatch.model_validate(body(reader.read(restored,policy,deadline=deadline)))
            if sha256_json(body(scope_reader(e)))!=sha256_json(body(scope)):raise ValueError('rollback_scope_changed')
            current=self._source(key,actor)
            with self.queue.transaction() as db:
                r=self._bound(db,key,current)
                if r['generation']!=generation:return self.show_outside(r)
                if current.cancelled or not start<=self.queue.now()<deadline:raise ValueError('rollback_read_expired')
                report=verify_effect(restored,policy,batch,now=self.now())
                r.update(rollback_result=report,state='rollback_verified' if report['service_resolved'] else 'manual_handoff',generation=r['generation']+1)
                self._save(db,r)
        except Exception:
            with self.queue.transaction() as db:
                r=self._get(db,key)
                if r['generation']==generation and not r['expired']:
                    r.update(rollback_result=report,state='manual_handoff',generation=r['generation']+1);self._save(db,r)
        return self.show(key,actor=actor)

    def cleanup_local(self,key,*,actor,port,read_only=False):
        e=self._source(key,actor)
        if getattr(port,'mode',None)!='local_fixture':raise ValueError('live_cleanup_disabled')
        start=self.queue.now();deadline=min(start+self.policy.read_seconds,e.retained_until.timestamp())
        with self.queue.transaction() as db:r=self._bound(db,key,e);items=[x['item'] for x in r['cleanup']]
        for raw in items:
            item=CleanupItem.model_validate(raw)
            current=self._source(key,actor)
            with self.queue.transaction() as db:
                r=self._bound(db,key,current)
                row=next(x for x in r['cleanup'] if x['item']==raw)
                if row['state']=='confirmed_absent':continue
                can_write=not read_only and row['state']=='pending'
                if can_write:row['state']='attempted';self._save(db,r)  # reserve before any disposal
            state='unknown'
            def call_limit():return min(deadline,self.queue.now()+3) if item.kind=='lease' else deadline
            try:
                if not start<=self.queue.now()<deadline:raise ValueError('cleanup_expired')
                seen=port.inspect(item,deadline=call_limit())
                if not start<=self.queue.now()<deadline:raise ValueError('cleanup_expired')
                if seen=={'present':False}:state='confirmed_absent'
                elif seen=={'present':True,'owner_hash':item.owner_hash} and can_write:
                    self._source(key,actor)
                    if not start<=self.queue.now()<deadline:raise ValueError('cleanup_expired')
                    port.dispose(item,deadline=call_limit())
                    if port.inspect(item,deadline=call_limit())=={'present':False} and start<=self.queue.now()<deadline:state='confirmed_absent'
                elif seen.get('present') is True and seen.get('owner_hash')!=item.owner_hash:state='not_owned'
            except Exception:pass  # Never persist token, response body or exception text.
            with self.queue.transaction() as db:
                r=self._get(db,key)
                if not r['expired']:
                    row=next(x for x in r['cleanup'] if x['item']==raw)
                    if row['state']!='confirmed_absent':row['state']=state
                    r['generation']+=1;self._save(db,r)
        return self.show(key,actor=actor)

    def prune(self):
        with self.queue.transaction() as db:
            for row in db.execute('SELECT document FROM recovery_effects').fetchall():
                r=json.loads(row[0])
                if self.queue.now()>=r['retained_until'] and not r['expired']:
                    r.update(expired=True,state='expired',event_id=None,job_id=None,cleanup=[],first_result=None,result=None,rollback=None,rollback_result=None,generation=r['generation']+1)
                    self._save(db,r)

    def result_text(self,key,*,actor):
        r=self.show(key,actor=actor)
        label={'succeeded':'회복 확인','unresolved':'미해결','verification_unknown':'검증불가','cancelled':'취소됨','rollback_verified':'원복 후 회복 확인','manual_handoff':'수동 인계 필요'}.get(r['state'],'관찰 대기')
        return (f'효과 검증: {label}\n사건: {r["event_id"]}\n조사: {r["job_id"]}\n계획: {key}\n'
            f'회수 확인: {r["cleanup_complete"]} · 후속: {(r["rollback"] or {}).get("decision","미정")}\n'
            '로컬 합성 검증입니다. PR 병합·명령 종료·경보 해제만으로 회복을 판정하지 않습니다. Slack 전송 및 추가 실행 승인 결과가 아닙니다.')
