"""WF-05 local planning only. No executor, credentials, PR or network transport.

A supported action is a matched planning template, never an executable proposal.
Current-state/known-good proof and approval belong to WF-06/07, not user text.
"""
from dataclasses import asdict, dataclass
from typing import Literal
import re

from pydantic import Field, StrictStr, model_validator

from ai_ops.contracts import (StrictModel, Target, Sha256, UTCDateTime,
                              _utc_datetime, sha256_json)
from ai_ops.c_recovery import (TARGET, IMAGE_REPOSITORY, ISOLATED_REPOSITORY,
                              VALUES_PATH, ImageFailureBinding, _image_failure_binding)
from ai_ops.job_queue import RETENTION_SECONDS, utc
from ai_ops.alerts import alert_time


@dataclass(frozen=True)
class ActionSpec:
    action_id: str
    version: int
    method: str
    planning_supported: bool
    impact: str
    verification: tuple[str, ...]
    failure_action: str


ACTIONS = (
    ActionSpec('image-rollback', 1, 'pr', True, 'ai-ops-lab/lab-api image only',
        ('argo_synced_healthy', 'pod_image_matches_desired_digest', 'pod_ready',
         'readyz_success', 'items_flow_success'),
        'stop_and_report_without_unapproved_rollback'),
    ActionSpec('deployment-restart', 1, 'command', False,
        'candidate_only_not_implemented', (), 'manual_handoff_without_retry'),
)


def action_registry():
    # Return copies, not a mutable live registry; no runtime registration API.
    return [asdict(action) for action in ACTIONS]


def parse_plan_arguments(text):
    match = re.fullmatch(r'(job-[0-9a-f]{32})(?: ([-a-z0-9]{1,64}))?', text or '')
    if match is None:
        raise ValueError('recovery_plan_arguments_rejected')
    return match[1], match[2] or 'image-rollback'


class PlanEvidence(StrictModel):
    incident_id: str = Field(pattern=r'^inc-[0-9a-f]{32}$')
    run_id: str = Field(pattern=r'^run-[0-9a-f]{32}$')
    bundle_hash: Sha256
    report_hash: Sha256
    audit_hash: Sha256 | None
    collected_at: UTCDateTime
    image_failure: ImageFailureBinding | None


class RecoveryPlan(StrictModel):
    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=r'^slack-[0-9a-f]{64}$')
    event_id: str = Field(pattern=r'^evt-[0-9a-f]{64}$')
    job_id: str = Field(pattern=r'^job-[0-9a-f]{32}$')
    event_status: Literal['firing', 'resolved']
    # The event protocol preserves nanoseconds; datetime only preserves microseconds.
    event_started_at: StrictStr = Field(pattern=r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{9}Z$')
    target: Target
    action_id: str = Field(pattern=r'^[-a-z0-9]{1,64}$')
    action_version: int
    method: Literal['pr', 'command', 'manual']
    assessment: Literal['supported', 'unsupported', 'insufficient_evidence']
    reason: Literal['image_failure_bound', 'action_not_registered', 'action_not_implemented',
                    'target_not_supported', 'result_not_available', 'image_failure_not_proven',
                    'observation_outside_event', 'event_resolved']
    evidence: PlanEvidence | None
    authorization_hash: Sha256
    config_hash: Sha256
    audience_hash: Sha256
    created_at: UTCDateTime
    retained_until: UTCDateTime
    impact: str
    verification: list[str]
    failure_action: str
    repository: str | None
    values_path: str | None
    field: str | None
    blockers: list[str]
    state: Literal['draft'] = 'draft'
    approval: Literal['not_requested'] = 'not_requested'
    proposal_ready: Literal[False] = False
    execution_allowed: Literal[False] = False
    plan_hash: Sha256

    @model_validator(mode='after')
    def contract(self):
        action = next((a for a in ACTIONS if a.action_id == self.action_id), None)
        if alert_time(self.event_started_at)[0] != self.event_started_at:
            raise ValueError('recovery_event_time_rejected')
        if self.retained_until <= self.created_at or not self.blockers:
            raise ValueError('recovery_plan_retention_or_blockers_rejected')
        expected = (action.version, action.method, action.impact,
                    list(action.verification), action.failure_action) if action else (
                        0, 'manual', 'unsupported_action', [], 'manual_handoff_without_retry')
        if (self.action_version, self.method, self.impact, self.verification, self.failure_action) != expected:
            raise ValueError('recovery_plan_registry_mismatch')
        scope = (ISOLATED_REPOSITORY, VALUES_PATH, 'api.image.digest') if self.action_id=='image-rollback' and self.target==TARGET else (None,None,None)
        if (self.repository, self.values_path, self.field) != scope:
            raise ValueError('recovery_plan_scope_rejected')
        if self.assessment == 'supported' and (self.reason != 'image_failure_bound'
                or self.action_id != 'image-rollback' or self.target != TARGET
                or self.evidence is None or self.evidence.image_failure is None
                or self.event_status!='firing'
                or self.event_started_at > alert_time(self.evidence.image_failure.observed_at.isoformat())[0]
                or self.evidence.image_failure.observed_at > self.created_at
                or self.evidence.collected_at > self.created_at):
            raise ValueError('recovery_plan_support_rejected')
        if self.plan_hash != sha256_json(self.model_dump(mode='json', exclude={'plan_hash'})):
            raise ValueError('recovery_plan_hash_rejected')
        return self


def prepare_plan(tasks, store, *, view, job_id, action_id, request_id,
                 authorization_hash, audience_hash):
    """Internal trusted adapter API. Audience authorization precedes this call.

    Independently verifies event→job membership and the stored result. No free text,
    desired digest, shell arguments, evidence booleans or caller-supplied report accepted.
    """
    if view['registration'] != 'registered':
        raise ValueError('recovery_event_unavailable')
    current = store.authorized_view(view['event_id'], allowed_targets={view['target']})
    if current is None or any(current[k]!=view[k] for k in current):
        raise ValueError('recovery_event_changed')
    with tasks.queue.transaction() as db:
        linked = db.execute('SELECT 1 FROM event_requests WHERE event_id=? AND job_id=? AND state="delivered"',
                            (view['event_id'],job_id)).fetchone()
    if not linked:
        raise ValueError('recovery_job_event_mismatch')
    binding = next(b for b in tasks.settings.bindings.bindings if b.alias==view['target'])
    shown = tasks.show(job_id)
    result = tasks.result(job_id)
    now = tasks.queue.now()
    deadline = min(_utc_datetime(view['starts_at']).timestamp() + RETENTION_SECONDS,
                   _utc_datetime(shown['investigation']['retention_deadline']).timestamp())
    if deadline <= now:
        raise ValueError('recovery_evidence_expired')
    action = next((a for a in ACTIONS if a.action_id==action_id), None)
    evidence = None
    assessment, reason = 'unsupported', 'action_not_registered'
    if action:
        if not action.planning_supported:
            reason = 'action_not_implemented'
        elif binding.target != TARGET:
            reason = 'target_not_supported'
        else:
            assessment, reason = 'insufficient_evidence', 'result_not_available'
            if result['state']=='available':
                reference = result['reference']
                record = tasks.queue.engine.show(reference['incident_id'], reference['run_id'])
                if (record.bundle.bundle_hash != result['bundle_hash'] or record.bundle.target != binding.target
                        or record.report.target != binding.target):
                    raise ValueError('recovery_result_binding_rejected')
                failure = (_image_failure_binding(record) if record.collection_audit is not None else None)
                if failure:
                    # Evidence detector binds image hashes; also require this registered image repository.
                    images = [e.payload.get('image') for e in record.bundle.evidence
                              if e.evidence_id in failure.evidence_ids and e.kind=='deployment']
                    if len(images)!=1 or not re.fullmatch(re.escape(IMAGE_REPOSITORY)+r'@sha256:[a-f0-9]{64}', images[0] or ''):
                        failure = None
                evidence = dict(**reference, bundle_hash=result['bundle_hash'],
                    report_hash=sha256_json(record.report.model_dump(mode='json')),
                    audit_hash=sha256_json(record.collection_audit.model_dump(mode='json')) if record.collection_audit else None,
                    collected_at=utc(record.bundle.created_at.timestamp()),
                    image_failure=failure.model_dump(mode='json') if failure else None)
                reason = 'image_failure_not_proven'
                if view['status']=='resolved':
                    reason = 'event_resolved'
                elif failure:
                    if (alert_time(view['starts_at'])[0] > alert_time(failure.observed_at.isoformat())[0]
                            or failure.observed_at > _utc_datetime(utc(now))
                            or record.bundle.created_at.timestamp()>now):
                        reason = 'observation_outside_event'
                    else:
                        assessment, reason = 'supported', 'image_failure_bound'
    if tasks.show(job_id)!=shown or tasks.result(job_id)!=result:
        raise ValueError('recovery_result_changed')
    body = dict(schema_version=1, request_id=request_id, event_id=view['event_id'],job_id=job_id,
        event_status=view['status'],event_started_at=view['starts_at'],
        target=binding.target.model_dump(mode='json'), action_id=action_id,
        action_version=action.version if action else 0, method=action.method if action else 'manual',
        assessment=assessment,reason=reason,evidence=evidence,authorization_hash=authorization_hash,
        config_hash=tasks.settings.config_hash,audience_hash=audience_hash,
        created_at=utc(now),retained_until=utc(deadline),
        impact=action.impact if action else 'unsupported_action',
        verification=list(action.verification) if action else [],
        failure_action=action.failure_action if action else 'manual_handoff_without_retry',
        repository=ISOLATED_REPOSITORY if action_id=='image-rollback' and binding.target==TARGET else None,
        values_path=VALUES_PATH if action_id=='image-rollback' and binding.target==TARGET else None,
        field='api.image.digest' if action_id=='image-rollback' and binding.target==TARGET else None,
        blockers=['current_target_uid_revision_revalidation','verified_known_good_image_and_exact_diff',
                  'plan_approval_policy_and_fresh_approval','exact_head_review_and_ci','scoped_execution_and_rollback']
            if assessment=='supported' else [reason,'manual_investigation_or_supported_action_required'],
        state='draft',approval='not_requested',proposal_ready=False,execution_allowed=False)
    body['event_started_at']=alert_time(body['event_started_at'])[0]
    return RecoveryPlan.model_validate({**body,'plan_hash':sha256_json(body)})


REASONS = {
    'image_failure_bound':'이미지 가져오기 실패 근거가 연결돼 이미지 복구 계획을 검토할 수 있습니다.',
    'action_not_registered':'등록되지 않은 조치입니다. 수동 대응이 필요합니다.',
    'action_not_implemented':('명령 방식(재시작) 후보입니다. 재시작 조건은 이 초안이 아니라 승인 요청 시점의 현재 상태로 '
                              '판정하며, 실제 실행은 아직 비활성입니다.'),
    'target_not_supported':'이 복구 조치는 격리 lab-api만 지원합니다.',
    'result_not_available':'선택한 조사의 완료된 증거가 없습니다. 조사 완료 후 새 요청이 필요합니다.',
    'image_failure_not_proven':'이미지 복구에 필요한 Pod·이벤트·Deployment 결합 근거가 부족합니다.',
    'observation_outside_event':'관측 시각이 사건 발생 전이거나 미래여서 복구 근거로 쓸 수 없습니다.',
    'event_resolved':'경보가 해제됐습니다. 현재 상태를 다시 조사한 뒤 조치 필요성을 판단하세요.',
}


def plan_text(plan):
    plan = RecoveryPlan.model_validate(plan.model_dump(mode='json'))
    command = plan.method=='command' and plan.reason=='action_not_implemented'
    label = {'supported':'지원 조치와 근거 일치 (실행 준비 아님)',
             'unsupported':'미지원', 'insufficient_evidence':'근거 부족'}[plan.assessment]
    if command:
        label = '명령 후보 (조건은 승인 요청 시 판정, 실행 비활성)'
    text = (f'복구 계획 초안: {label}\n사건: {plan.event_id}\n조사: {plan.job_id}\n'
            f'대상: {plan.target.namespace}/{plan.target.workload}\n'
            f'조치: {plan.action_id} v{plan.action_version} · 방식: {plan.method}\n'
            f'{REASONS[plan.reason]}\n계획 해시: {plan.plan_hash}\n')
    if plan.evidence is not None:
        text += f'근거 묶음: {plan.evidence.bundle_hash}\n수집 시각: {plan.evidence.collected_at.isoformat()}\n'
        if plan.evidence.image_failure:
            text += '인용 근거: '+', '.join(plan.evidence.image_failure.evidence_ids)+'\n'
    if plan.assessment=='supported':
        text += ('변경 범위: 격리 저장소 ai-ops/lab/values.yaml의 api.image.digest 한 항목.\n'
                 '정상 이미지 출처·현재 UID/revision·정확한 diff를 아직 재검증하지 않았습니다.\n'
                 '예상 영향: 격리 앱의 Pod 교체와 일시적 가용성 저하 가능.\n'
                 '효과 확인: 이미지 일치·Pod Ready·Argo 상태·정상 응답·업무 요청.\n'
                 '실패 시: 중단·보고, 승인되지 않은 추가 원복은 실행하지 않습니다.\n')
    elif command:
        text += ('다음 단계: 재시작 경로가 켜져 있으면 이 스레드에서 '
                 f'봇을 멘션해 `request-approval this {plan.plan_hash}`로 재시작 승인 대기를 요청할 수 있습니다. '
                 '그때 현재 상태를 읽어 재시작 조건(단일 Pod·Ready 아님·경쟁 제어기 없음 등)을 판정하고, '
                 '통과할 때만 승인 대기를 엽니다. 승인 뒤에도 실제 재시작은 실행하지 않습니다.\n')
    else:
        text += '다음 단계: 부족한 증거를 보완하거나 지원되는 조치를 확인한 뒤 새 복구안 요청.\n'
    return text + ('이 초안은 저장된 관측 기준입니다. 실제 PR·명령·실행 승인·쓰기 자격을 만들지 않았습니다.\n'
                   '명령 방식은 PR을 강제하지 않으며 별도 실행기·정확한 계획 승인이 필요합니다.\n'
                   f'자료 보관 종료: {plan.retained_until.isoformat()} (실행 승인 유효기간이 아닙니다)')
