"""WF08 fixed restart contract and local-only executor; no shell or cloud clients.

Ports are trusted internal code, not user JSON/API endpoints. Production approval,
Kubernetes transport, credentials and Slack delivery are intentionally unconnected.
"""
from __future__ import annotations
from copy import deepcopy
import json
import math
import re
from typing import Literal
from pydantic import Field, model_validator

from ai_ops.contracts import StrictModel, Target, Sha256, UTCDateTime, sha256_json, _utc_datetime
from ai_ops.c_recovery import TARGET, IMAGE_REPOSITORY
from ai_ops.recovery_plans import RecoveryPlan
from ai_ops.recovery_approvals import RecoveryApprovalLedger, CurrentRecoveryState
from ai_ops.alerts import alert_time
from ai_ops.job_queue import utc
from ai_ops.restart_gates import (RestartGateRejected, evaluate as evaluate_gates, rejected as rejected_gates,
    summary as gate_summary, validate_evidence)

ACTION = 'deployment-restart'
ENDPOINT = '/apis/apps/v1/namespaces/ai-ops-lab/deployments/lab-api'
ANNOTATION = 'ai-ops.bidservice.store/restart-id'
UUID = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'


def body(model):return model.model_dump(mode='json')


class RestartSource(StrictModel):
    candidate: RecoveryPlan
    bundle_hash: Sha256
    report_hash: Sha256
    audit_hash: Sha256
    evidence_hash: Sha256
    deployment_uid_hash: Sha256
    pod_uid_hash: Sha256
    image_hash: Sha256
    observed_at: UTCDateTime

    @model_validator(mode='after')
    def supported_candidate(self):
        p=self.candidate
        if (p.action_id!=ACTION or p.action_version!=1 or p.method!='command'
                or p.assessment!='unsupported' or p.reason!='action_not_implemented'
                or p.target!=TARGET or p.event_status!='firing'
                or alert_time(self.observed_at.isoformat())[0]<p.event_started_at
                or self.observed_at>p.created_at):
            raise ValueError('restart_candidate_rejected')
        return self


def slack_restart_source(adapter, request_id):
    """Reuse the stored WF05 request and original queue/worker evidence. No IO transport."""
    def read_source(candidate_hash, actor, *, require_firing):
        row=adapter._row(request_id)
        if row['user_id']!=actor or not adapter._authorized(row):raise ValueError('command_source_unavailable')
        entry=adapter._event_bridge().entry(request_id)
        candidate=RecoveryPlan.model_validate(json.loads(entry['context'])['recovery_plan'])
        if candidate.plan_hash!=candidate_hash:raise ValueError('command_source_unavailable')
        aliases={b.alias for b in adapter.tasks.settings.bindings.bindings if b.target==candidate.target}
        event_view=adapter.events.authorized_view(candidate.event_id,allowed_targets=aliases)
        if event_view is None or (require_firing and event_view['status']!='firing') or event_view['starts_at']!=candidate.event_started_at:
            raise ValueError('command_event_changed')
        result=adapter.tasks.result(candidate.job_id)
        if result['state']!='available':raise ValueError('command_source_unavailable')
        record=adapter.tasks.queue.engine.show(**result['reference'])
        if record.bundle.target!=TARGET or record.bundle.bundle_hash!=result['bundle_hash'] or record.collection_audit is None:
            raise ValueError('command_source_unavailable')
        audit={e.evidence_id:e for e in record.collection_audit.entries}
        candidates=[]
        for pod in record.bundle.evidence:
            p=audit.get(pod.evidence_id)
            if (pod.kind!='workload_status' or pod.status!='available' or p is None
                    or pod.payload.get('phase')!='Running' or pod.payload.get('ready') is not False
                    or not all((p.resource_uid,p.resource_version,p.deployment_uid,p.replicaset_uid,p.replicaset_resource_version))):continue
            for event in record.bundle.evidence:
                if (event.kind!='event' or event.status!='available'
                        or event.payload.get('reason')!='Unhealthy' or event.payload.get('type')!='Warning'
                        or event.payload.get('involved_uid')!=p.resource_uid):continue
                for dep in record.bundle.evidence:
                    d=audit.get(dep.evidence_id);e=audit.get(event.evidence_id)
                    if (dep.kind!='deployment' or dep.status!='available' or d is None or e is None
                            or any(x.status!='available' or x.reason!='collected' for x in (p,d,e))
                            or p.time_field!='response_received_at' or d.time_field!='response_received_at'
                            or e.time_field not in ('eventTime','lastTimestamp')
                            or d.resource_uid!=p.deployment_uid
                            or dep.payload.get('image')!=pod.payload.get('image')
                            or not re.fullmatch(re.escape(IMAGE_REPOSITORY)+r'@sha256:[a-f0-9]{64}',pod.payload.get('image',''))
                            or not 0<=(pod.observed_at-event.observed_at).total_seconds()<=120
                            or abs((dep.observed_at-pod.observed_at).total_seconds())>120):continue
                    if any(x.provenance.kind!='captured' or x.target!=TARGET
                            or alert_time(x.observed_at.isoformat())[0]<candidate.event_started_at
                            or x.observed_at>candidate.created_at for x in (pod,event,dep)):continue
                    selected=[body(x) for x in (pod,event,dep)]
                    candidates.append(dict(evidence_hash=sha256_json(selected),deployment_uid_hash=sha256_json(p.deployment_uid),
                        pod_uid_hash=sha256_json(p.resource_uid),image_hash=sha256_json(pod.payload['image']),observed_at=pod.observed_at))
        if (len(candidates)!=1 or not adapter._authorized(row)
                or adapter.tasks.result(candidate.job_id)!=result
                or adapter.events.authorized_view(candidate.event_id,allowed_targets=aliases)!=event_view):
            raise ValueError('restart_evidence_unavailable')
        return RestartSource(candidate=candidate,bundle_hash=result['bundle_hash'],report_hash=sha256_json(body(record.report)),
                             audit_hash=sha256_json(body(record.collection_audit)),**candidates[0])
    def load(candidate_hash, actor):
        return read_source(candidate_hash, actor, require_firing=True)
    # Separate read-only result access; resolving an alert never authorizes execution.
    load.for_effects=lambda candidate_hash,actor:read_source(candidate_hash,actor,require_firing=False)
    def image_for_effects(candidate_hash,actor):
        source=read_source(candidate_hash,actor,require_firing=False)
        result=adapter.tasks.result(source.candidate.job_id)
        if result.get('state')!='available':raise ValueError('effect_source_unavailable')
        record=adapter.tasks.queue.engine.show(**result['reference'])
        images={e.payload.get('image') for e in record.bundle.evidence if e.kind in ('deployment','workload_status')}
        matches=[i for i in images if isinstance(i,str) and sha256_json(i)==source.image_hash]
        if len(matches)!=1:raise ValueError('effect_image_unavailable')
        return matches[0]
    load.image_for_effects=image_for_effects
    return load


class RestartSnapshot(StrictModel):
    """Verified live read adapter output; raw flags are never accepted from Slack text."""
    target: Target
    deployment_uid: str = Field(pattern=UUID)
    resource_version: str = Field(pattern=r'^[0-9]+$',max_length=64)
    revision_hash: Sha256  # Same spec/source, excluding only our restart annotation.
    image: str
    pod_uid_hash: Sha256
    observed_at: UTCDateTime
    generation: int = Field(ge=1)
    observed_generation: int = Field(ge=0)
    replicas: Literal[1] = 1
    ready_replicas: int = Field(ge=0,le=1)
    strategy: Literal['Recreate','RollingUpdate']
    # Raw verified observations for restart_gates (3b); no pre-computed verdicts.
    container_running: bool
    restart_count: int = Field(ge=0,le=1_000_000)
    pod_started_at: UTCDateTime
    readiness_failing_since: UTCDateTime | None
    readiness_window_seconds: int = Field(ge=1,le=3600)
    prior_ready: Literal['observed','absent','unknown']
    dependencies: Literal['pass','fail','unknown']
    # Display-only for the approver; never a gate.
    image_id_hash: Sha256 | None = None
    scheduling_signals: list[Literal['failed_scheduling','quota_exceeded']] = Field(default_factory=list,max_length=2)
    rollout_in_progress: bool
    autoscaling: bool
    gitops_auto_sync: bool
    annotations_exist: bool
    restart_marker: str | None = Field(default=None,max_length=64)

    @model_validator(mode='after')
    def bounded(self):
        if self.target!=TARGET or not re.fullmatch(re.escape(IMAGE_REPOSITORY)+r'@sha256:[a-f0-9]{64}',self.image):
            raise ValueError('restart_target_rejected')
        return self

    def identity(self):
        return dict(event_id='',target=body(self.target),deployment_uid_hash=sha256_json(self.deployment_uid),
                    revision_hash=self.revision_hash,image_hash=sha256_json(self.image))


def patch_request(uid, resource_version, marker, annotations_exist):
    operations=[{'op':'test','path':'/metadata/uid','value':uid},
                {'op':'test','path':'/metadata/resourceVersion','value':resource_version}]
    if not annotations_exist:operations.append({'op':'add','path':'/spec/template/metadata/annotations','value':{}})
    operations.append({'op':'add','path':'/spec/template/metadata/annotations/'+ANNOTATION.replace('/','~1'),'value':marker})
    return {'method':'PATCH','endpoint':ENDPOINT,'content_type':'application/json-patch+json','body':operations}


class RestartPlan(StrictModel):
    schema_version: Literal[1] = 1
    candidate_hash: Sha256
    event_id: str = Field(pattern=r'^evt-[0-9a-f]{64}$')
    job_id: str = Field(pattern=r'^job-[0-9a-f]{32}$')
    target: Target
    action_id: Literal['deployment-restart'] = ACTION
    action_version: Literal[1] = 1
    assessment: Literal['supported'] = 'supported'
    source_hash: Sha256
    deployment_uid: str = Field(pattern=UUID)
    resource_version: str = Field(pattern=r'^[0-9]+$',max_length=64)
    revision_hash: Sha256
    image_hash: Sha256
    pod_uid_hash: Sha256
    generation: int = Field(ge=1)
    annotations_exist: bool
    strategy: Literal['Recreate','RollingUpdate']
    restart_count: int = Field(ge=0,le=1_000_000)  # liveness baseline at plan time
    marker: Sha256
    executor_id: str = Field(pattern=r'^local-restart-[a-z0-9-]{1,40}$')
    request: dict
    impact: Literal['single_lab_pod_replacement_and_possible_downtime'] = 'single_lab_pod_replacement_and_possible_downtime'
    failure_action: Literal['stop_inspect_manual_handoff_no_retry_or_automatic_rollback'] = 'stop_inspect_manual_handoff_no_retry_or_automatic_rollback'
    verification: list[str] = Field(default_factory=lambda:['same_uid_image','new_pod_ready','readyz_success','items_flow_success'])
    execution_seconds: int = Field(ge=1,le=15)
    created_at: UTCDateTime
    retained_until: UTCDateTime
    execution_allowed: Literal[False] = False
    plan_hash: Sha256

    @model_validator(mode='after')
    def sealed(self):
        if (self.target!=TARGET or self.retained_until<=self.created_at
                or self.verification!=['same_uid_image','new_pod_ready','readyz_success','items_flow_success']
                or self.request!=patch_request(self.deployment_uid,self.resource_version,self.marker,self.annotations_exist)
                or self.plan_hash!=sha256_json(self.model_dump(mode='json',exclude={'plan_hash'}))):
            raise ValueError('restart_plan_rejected')
        return self


class RestartObservation(CurrentRecoveryState):
    status: Literal['restart_needed'] = 'restart_needed'


class RestartApprovalLedger(RecoveryApprovalLedger):
    """Same WF06 lifecycle and queue DB, independent command scope/policy tables."""
    TABLE_PREFIX='restart_approval'
    PLAN_MODEL=RestartPlan
    SCOPE='execute_command'
    FAILURE_STATUS='restart_needed'

    def _observation(self,plan,*,after,deadline):
        s=RestartSnapshot.model_validate(body(self.observe(plan,deadline=deadline)))
        validate_before(plan,s,self.queue.now(),after=after)
        return RestartObservation(plan_hash=plan.plan_hash,event_id=plan.event_id,target=plan.target,
            deployment_uid_hash=sha256_json(s.deployment_uid),revision_hash=s.revision_hash,
            image_hash=sha256_json(s.image),proof_hash=sha256_json(body(s)),observed_at=s.observed_at,
            status='restart_needed')

    def _require_open_observation(self,plan,observation):
        if (observation.deployment_uid_hash!=sha256_json(plan.deployment_uid)
                or observation.revision_hash!=plan.revision_hash or observation.image_hash!=plan.image_hash):
            raise ValueError('new_command_plan_required')

    @classmethod
    def _view(cls,record):
        return {**super()._view(record),'proposal_ready':False,
                'command_ready':record['state']=='proposal_ready'}


def validate_before(plan,s,now,*,after):
    at=s.observed_at.timestamp()
    if not after<=at<=now or now-at>15:
        raise ValueError('restart_current_state_rejected')
    results=evaluate_gates(plan,s,now)
    if rejected_gates(results):
        raise RestartGateRejected(results)
    return results


class RestartCoordinator:
    """Durable local command boundary. The existing investigation worker stays read-only."""
    def __init__(self,queue,*,source,read_current,executor_id,execution_seconds):
        if not re.fullmatch(r'local-restart-[a-z0-9-]{1,40}',executor_id) or type(execution_seconds) is not int or not 1<=execution_seconds<=15:
            raise ValueError('restart_configuration_rejected')
        self.queue,self.source,self.read_current=queue,source,read_current
        self.executor_id,self.execution_seconds=executor_id,execution_seconds
        with queue.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS restart_plans (plan_hash TEXT PRIMARY KEY,candidate_hash TEXT UNIQUE NOT NULL,document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS restart_runs (plan_hash TEXT PRIMARY KEY,document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS restart_consumed (slot_hash TEXT PRIMARY KEY,plan_hash TEXT NOT NULL)')

    def _get(self,db,key):
        row=db.execute('SELECT document FROM restart_runs WHERE plan_hash=?',(key,)).fetchone()
        if row is None:raise ValueError('restart_unavailable')
        return json.loads(row[0])

    def _save(self,db,r):
        db.execute('INSERT OR REPLACE INTO restart_runs VALUES(?,?)',(r['plan_hash'],json.dumps(r,sort_keys=True)))

    def prepare(self,candidate_hash,*,actor):
        source=RestartSource.model_validate(body(self.source(candidate_hash,actor)))
        with self.queue.transaction() as db:
            row=db.execute('SELECT plan_hash FROM restart_plans WHERE candidate_hash=?',(candidate_hash,)).fetchone()
        if row:return self.load(row[0],actor)
        start=self.queue.now();s=RestartSnapshot.model_validate(body(self.read_current(None,deadline=start+self.execution_seconds)))
        p=source.candidate
        if (self.queue.now()-start>=self.execution_seconds or self.queue.now()<start
                or s.observed_at.timestamp()<start or s.observed_at.timestamp()>self.queue.now()
                or not 0<=self.queue.now()-source.observed_at.timestamp()<=120
                or sha256_json(s.deployment_uid)!=source.deployment_uid_hash or s.pod_uid_hash!=source.pod_uid_hash
                or sha256_json(s.image)!=source.image_hash):raise ValueError('restart_source_stale_or_changed')
        marker=sha256_json([candidate_hash,s.deployment_uid,s.resource_version])
        values=dict(candidate_hash=candidate_hash,event_id=p.event_id,job_id=p.job_id,target=TARGET.model_copy(deep=True),
            source_hash=sha256_json(body(source)),deployment_uid=s.deployment_uid,resource_version=s.resource_version,
            revision_hash=s.revision_hash,image_hash=sha256_json(s.image),pod_uid_hash=s.pod_uid_hash,generation=s.generation,
            annotations_exist=s.annotations_exist,strategy=s.strategy,restart_count=s.restart_count,
            marker=marker,executor_id=self.executor_id,
            request=patch_request(s.deployment_uid,s.resource_version,marker,s.annotations_exist),
            execution_seconds=self.execution_seconds,created_at=_utc_datetime(utc(self.queue.now())),retained_until=p.retained_until)
        # Include model defaults in the sealed subject.
        draft=RestartPlan.model_construct(**values,plan_hash='0'*64)
        values=body(draft);values['plan_hash']=sha256_json({k:v for k,v in values.items() if k!='plan_hash'})
        plan=RestartPlan.model_validate(values);validate_before(plan,s,self.queue.now(),after=start)
        if sha256_json(body(self.source(candidate_hash,actor)))!=plan.source_hash:raise ValueError('restart_source_changed')
        with self.queue.transaction() as db:
            if not start<=self.queue.now()<min(start+self.execution_seconds,p.retained_until.timestamp()):raise ValueError('restart_prepare_expired')
            previous=db.execute('SELECT document FROM restart_plans WHERE candidate_hash=?',(candidate_hash,)).fetchone()
            if previous:return RestartPlan.model_validate_json(previous[0])
            if db.execute('SELECT count(*) FROM restart_runs').fetchone()[0]>=10000:raise ValueError('restart_ledger_full')
            db.execute('INSERT INTO restart_plans VALUES(?,?,?)',(plan.plan_hash,candidate_hash,plan.model_dump_json()))
            self._save(db,dict(plan_hash=plan.plan_hash,state='prepared',generation=0,cancel_requested=False,
                applied=False,applied_at=None,reason='exact_command_approval_required',lease=None,lease_released=False,retained_until=plan.retained_until.timestamp()))
        return plan

    def load(self,key,actor):
        with self.queue.transaction() as db:
            row=db.execute('SELECT document FROM restart_plans WHERE plan_hash=?',(key,)).fetchone()
        if row is None:raise ValueError('restart_unavailable')
        plan=RestartPlan.model_validate_json(row[0])
        source=RestartSource.model_validate(body(self.source(plan.candidate_hash,actor)))
        if (self.queue.now()>=plan.retained_until.timestamp() or sha256_json(body(source))!=plan.source_hash
                or (plan.executor_id,plan.execution_seconds)!=(self.executor_id,self.execution_seconds)):
            raise ValueError('restart_source_changed_or_expired')
        return plan

    def show(self,key,*,actor):
        self.load(key,actor)
        with self.queue.transaction() as db:r=self._get(db,key)
        return {**r,'execution_allowed':False,'service_resolved':False}

    def cancel(self,key,*,actor):
        self.load(key,actor)
        with self.queue.transaction() as db:
            r=self._get(db,key)
            if not r['cancel_requested']:
                r.update(cancel_requested=True,generation=r['generation']+1,
                    state='needs_review' if r['state'] in ('executing','needs_review') else 'cancelled',reason='cancel_requested')
                self._save(db,r)
        return self.show(key,actor=actor)

    def _approved(self,db,key,actor,ledger,plan,now):
        if type(ledger) is not RestartApprovalLedger or ledger.queue.path!=self.queue.path:raise ValueError('command_approval_scope_required')
        a=ledger._get(db,key)
        if (a['state']!='proposal_ready' or a['approved_by_hash']!=sha256_json(actor)
                or now>=min(a['fresh_until'],a['approved_until'],plan.retained_until.timestamp())
                or a['identity']!={'event_id':plan.event_id,'target':body(plan.target),
                    'deployment_uid_hash':sha256_json(plan.deployment_uid),'revision_hash':plan.revision_hash,'image_hash':plan.image_hash}):
            raise ValueError('command_approval_inactive')
        return a

    def _port(self,port,plan):
        expected={'executor_id':plan.executor_id,'namespace':'ai-ops-lab','resource':'deployments/lab-api','verbs':['get','patch']}
        if getattr(port,'mode',None)!='local_fixture' or port.identity!=expected:raise ValueError('live_command_executor_disabled')

    def _held(self,lease,owner,deadline):
        if (not isinstance(lease,dict) or set(lease)!={'name','namespace','uid','holder','verified_until'}
                or lease['name']!='b6-scenario-lock' or lease['namespace']!='ai-ops-lab'
                or not isinstance(lease['uid'],str) or not lease['uid'] or lease['holder']!=owner
                or type(lease['verified_until']) not in (int,float) or not math.isfinite(lease['verified_until'])
                or lease['verified_until']<=deadline):
            raise ValueError('restart_lease_unconfirmed')

    def execute_local(self,key,*,actor,ledger,port):
        plan=self.load(key,actor);self._port(port,plan)
        start=self.queue.now();deadline=min(start+plan.execution_seconds,plan.retained_until.timestamp())
        with self.queue.transaction() as db:
            r=self._get(db,key)
            if r['state']!='prepared' or r['cancel_requested']:raise ValueError('restart_not_executable')
            a=self._approved(db,key,actor,ledger,plan,self.queue.now())
            deadline=min(deadline,a['fresh_until'],a['approved_until'])
            if not start<=self.queue.now()<deadline:raise ValueError('restart_execution_expired')
            slot=sha256_json([plan.event_id,body(plan.target),plan.deployment_uid,ACTION])
            if db.execute('SELECT 1 FROM restart_consumed WHERE slot_hash=?',(slot,)).fetchone():raise ValueError('restart_already_consumed')
            db.execute('INSERT INTO restart_consumed VALUES(?,?)',(slot,key))
            r.update(state='checking',reason='reserved_once',generation=r['generation']+1);self._save(db,r)
        held=None;owner='manual-'+key[:32]
        try:
            s=RestartSnapshot.model_validate(body(port.read(deadline=deadline)))
            validate_before(plan,s,self.queue.now(),after=start)
            self.load(key,actor)
            with self.queue.transaction() as db:
                pending=self._get(db,key);self._approved(db,key,actor,ledger,plan,self.queue.now())
                if pending['state']!='checking' or pending['cancel_requested'] or not start<=self.queue.now()<deadline:
                    raise ValueError('restart_cancelled_or_expired')
            held=port.acquire(owner=owner,deadline=min(deadline,self.queue.now()+3));self._held(held,owner,deadline)
            with self.queue.transaction() as db:
                current=self._get(db,key);current['lease']=deepcopy(held);self._save(db,current)
            s=RestartSnapshot.model_validate(body(port.read(deadline=deadline)))
            validate_before(plan,s,self.queue.now(),after=start)
            lease=port.inspect_lease(held,deadline=min(deadline,self.queue.now()+3));self._held(lease,owner,deadline)
            if lease['uid']!=held['uid']:raise ValueError('restart_lease_changed')
            # Hard gate: Recreate terminates the old Pod first, so preserve its
            # describe/status/events/logs/metrics before any write. No bundle, no patch.
            evidence=validate_evidence(port.capture_evidence(deadline=deadline),start=start,now=self.queue.now())
            self.load(key,actor);self._port(port,plan)
            with self.queue.transaction() as db:
                r=self._get(db,key)
                self._approved(db,key,actor,ledger,plan,self.queue.now())
                if r['state']!='checking' or r['cancel_requested'] or not start<=self.queue.now()<deadline:raise ValueError('restart_cancelled_or_expired')
                r['evidence_bundle']=evidence
                r.update(state='executing',reason='patch_outcome_pending',generation=r['generation']+1);self._save(db,r)
            port.patch(deepcopy(plan.request),deadline=deadline)
            observed=RestartSnapshot.model_validate(body(port.read(deadline=deadline)))
            self._record_applied(key,actor,plan,observed,after=start,deadline=deadline)
            # Release only a confirmed, owned fixture lease after inspecting the write.
            final_lease=port.inspect_lease(held,deadline=min(deadline,self.queue.now()+3))
            self._held(final_lease,owner,deadline)
            if final_lease['uid']!=held['uid']:raise ValueError('restart_lease_changed')
            if port.release(held,deadline=min(deadline,self.queue.now()+3)) is not True:raise ValueError('restart_lease_release_unconfirmed')
            with self.queue.transaction() as db:
                r=self._get(db,key);r['lease_released']=True
                if self.queue.now()>=deadline:r.update(state='needs_review',reason='cleanup_completed_after_budget')
                self._save(db,r)
        except Exception as exc:
            with self.queue.transaction() as db:
                r=self._get(db,key)
                if r['state']!='cancelled':
                    if isinstance(exc,RestartGateRejected):
                        # Manual handoff with per-gate verdicts (names only, no values).
                        r.update(state='needs_review',reason='gate_rejected',gate_results=gate_summary(exc.results),
                                 generation=r['generation']+1)
                    else:
                        r.update(state='needs_review',reason='inspect_without_retry',generation=r['generation']+1)
                self._save(db,r)
        return self.show(key,actor=actor)

    def _record_applied(self,key,actor,plan,s,*,after,deadline):
        self.load(key,actor)
        now=self.queue.now()
        if (not after<=s.observed_at.timestamp()<=now<deadline or now-s.observed_at.timestamp()>15
                or s.deployment_uid!=plan.deployment_uid or sha256_json(s.image)!=plan.image_hash
                or s.revision_hash!=plan.revision_hash or s.restart_marker!=plan.marker
                or s.resource_version==plan.resource_version or s.generation!=plan.generation+1):
            raise ValueError('restart_application_unconfirmed')
        with self.queue.transaction() as db:
            r=self._get(db,key)
            if r['state'] not in ('checking','executing','needs_review'):raise ValueError('restart_state_changed')
            if not after<=self.queue.now()<min(deadline,plan.retained_until.timestamp()):raise ValueError('restart_observation_expired')
            # Observation is not new execution authority. Preserve the durable WF06
            # cancellation even after a lost response/restart, without requiring a
            # live policy instance or extending an expired approval.
            approval=db.execute('SELECT document FROM restart_approvals WHERE plan_hash=?',(key,)).fetchone()
            if approval is not None and json.loads(approval[0])['state']=='cancelled':
                r['cancel_requested']=True
            r.setdefault('applied_at',None)
            if r['applied_at'] is None:r['applied_at']=s.observed_at.isoformat()
            r.update(applied=True,state='cancelled' if r['cancel_requested'] else 'applied_unverified',
                     reason='patch_observed_service_not_verified',generation=r['generation']+1)
            self._save(db,r)

    def reconcile_local(self,key,*,actor,port):
        plan=self.load(key,actor);self._port(port,plan)
        with self.queue.transaction() as db:r=self._get(db,key)
        if r['state'] not in ('checking','executing','needs_review'):return self.show(key,actor=actor)
        generation=r['generation']
        start=self.queue.now();deadline=min(start+plan.execution_seconds,plan.retained_until.timestamp())
        try:self._record_applied(key,actor,plan,RestartSnapshot.model_validate(body(port.read(deadline=deadline))),after=start,deadline=deadline)
        except Exception:
            with self.queue.transaction() as db:
                r=self._get(db,key)
                # A slow failed reader must not overwrite a later cancellation or
                # another reader's confirmed result.
                if r['generation']==generation and r['state'] in ('checking','executing','needs_review'):
                    r.update(state='needs_review',reason='outcome_unconfirmed_no_retry',generation=r['generation']+1)
                    self._save(db,r)
        return self.show(key,actor=actor)  # Read-only; retained lock needs explicit owner cleanup.

    def prune(self):
        # Same rule as the approval ledgers (TOMBSTONE_SECONDS): the expired run
        # record is deleted 30 days after retention ended, so the 10000-run cap is
        # not reached by history alone. The plan row is already gone by then, so
        # load() refuses it; restart_consumed (the one-execution fence) is kept.
        from ai_ops.recovery_approvals import TOMBSTONE_SECONDS
        with self.queue.transaction() as db:
            for row in db.execute('SELECT plan_hash,document FROM restart_runs').fetchall():
                r=json.loads(row['document'])
                if self.queue.now()>=r['retained_until']+TOMBSTONE_SECONDS:
                    db.execute('DELETE FROM restart_plans WHERE plan_hash=?',(row['plan_hash'],))
                    db.execute('DELETE FROM restart_runs WHERE plan_hash=?',(row['plan_hash'],))
                elif self.queue.now()>=r['retained_until']:
                    db.execute('DELETE FROM restart_plans WHERE plan_hash=?',(row['plan_hash'],))
                    r.update(state='expired',reason='source_expired',lease=None,generation=r['generation']+1);self._save(db,r)

    def result_text(self,key,*,actor):
        p=self.load(key,actor);r=self.show(key,actor=actor)
        return (f'명령 작업: {r["state"]}\n사건: {p.event_id}\n조사: {p.job_id}\n계획: {key}\n'
                f'재시작 변경 관측: {r["applied"]} · 잠금 회수 확인: {r["lease_released"]}\n'
                '명령 적용은 서비스 회복을 뜻하지 않습니다. 불명확하면 재실행하지 않고 수동 인계합니다.')
