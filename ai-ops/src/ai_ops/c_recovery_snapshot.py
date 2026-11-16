"""실행 대상과 잠금은 엄격히 비교하고 일관된 Argo 조회 시각 갱신만 구분한다."""
from copy import deepcopy
from datetime import datetime, timezone

from ai_ops.contracts import _utc_datetime


def _same_controller_observation_time(left, right, old_time, new_time):
    """동일 controller의 동일 소유권에서 관측 시각에 결합된 time 한 개만 구분한다.

    인자는 비교용 deepcopy다. managedFields를 제외하지 않으며, 다른 관리자/
    소유권/필드 값/순서/항목 수와 복수 갱신은 기존 전체 비교로 계속 거절한다.
    """
    before, after = left.get('managedFields'), right.get('managedFields')
    if (not isinstance(before, list) or not isinstance(after, list)
            or len(before) != len(after)
            or type(left.get('generation')) is not int
            or type(right.get('generation')) is not int
            or left['generation'] < 1 or right['generation'] != left['generation'] + 1):
        return False
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    if len(changed) != 1:
        return False
    old, new = changed[0]
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    required = {'manager': 'argocd-application-controller', 'operation': 'Update',
                'apiVersion': 'argoproj.io/v1alpha1', 'fieldsType': 'FieldsV1'}
    if (sum(isinstance(entry, dict) and entry.get('manager') == required['manager']
            for entry in before) != 1
            or any(old.get(key) != value for key, value in required.items())
            or old.get('subresource', '') != ''
            or old.get('fieldsV1', {}).get('f:status', {}).get('f:reconciledAt') != {}
            or not isinstance(old.get('time'), str) or not isinstance(new.get('time'), str)
            or _utc_datetime(old['time']) != _utc_datetime(old_time)
            or _utc_datetime(new['time']) != _utc_datetime(new_time)
            or {k: v for k, v in old.items() if k != 'time'} != {
                k: v for k, v in new.items() if k != 'time'}):
        return False
    # 정확히 검증한 time만 비교용 사본에서 분리한다. 나머지 원본 전체를 대조한다.
    old.pop('time')
    new.pop('time')
    return True


def same_application_observation(before, after, *, now):
    if before == after:
        return True
    try:
        left, right = deepcopy(before), deepcopy(after)
        old_version = left["metadata"].pop("resourceVersion")
        new_version = right["metadata"].pop("resourceVersion")
        old_time = left["status"].pop("reconciledAt")
        new_time = right["status"].pop("reconciledAt")
        if left['metadata'].get('managedFields') != right['metadata'].get('managedFields'):
            if (left.get('apiVersion') != 'argoproj.io/v1alpha1' or left.get('kind') != 'Application'
                    or right.get('apiVersion') != left['apiVersion'] or right.get('kind') != left['kind']):
                return False
            if not _same_controller_observation_time(left['metadata'], right['metadata'], old_time, new_time):
                return False
        if "generation" in left["metadata"] or "generation" in right["metadata"]:
            old_generation = left["metadata"]["generation"]
            new_generation = right["metadata"]["generation"]
            if (type(old_generation) is not int or type(new_generation) is not int
                    or old_generation < 1 or new_generation < 1):
                return False
        if left["metadata"].get("generation") != right["metadata"].get("generation"):
            # 현재 Application CRD는 status 하위 리소스가 없어 status 갱신도
            # generation을 올린다. 원본 전체가 같은 단일 갱신만 허용하며,
            # spec 변경 후 복귀 등의 복수 갱신을 숨기지 않는다.
            if new_generation != old_generation + 1:
                return False
            left["metadata"].pop("generation")
            right["metadata"].pop("generation")
        return (isinstance(old_version, str) and bool(old_version)
                and isinstance(new_version, str) and bool(new_version)
                and old_version != new_version
                and isinstance(old_time, str) and isinstance(new_time, str)
                and _utc_datetime(old_time) < _utc_datetime(new_time) <= _utc_datetime(now)
                and left == right)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def same_recovery_resources(before, after, *, now=None):
    """Application 관측 갱신 외에는 전체 원본의 동일성을 요구한다.

    resourceVersion은 불투명 문자열이다. 수치 순서를 추정하지 않으며,
    generation은 같은 원본의 단일 상태 갱신만 허용한다. managedFields는 동일
    controller 소유권의 관측 시각에 결합된 time 한 개만 구분하며 나머지 원본과
    spec/status 및 Deployment/Lease는 제외하지 않는다.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    return (same_application_observation(before["application"], after["application"], now=now)
            and before["deployment"] == after["deployment"]
            and before["lease"] == after["lease"])


# 진단에는 API 응답의 값이나 임의 key를 넣지 않는다. 고정 필드 경로만 사용한다.
_PHASE_MESSAGES = {
    'transport.first_to_final': '조회 도중 실습 원본이 바뀌었습니다',
    'audit.first_to_final': 'C-03 원본 조회 중 실습 대상 또는 잠금이 바뀌었습니다',
    'trial_source.before_to_raw': '인증된 사건 원본 조회 중 대상이 바뀌었습니다',
    'trial_source.raw_to_after': '인증된 사건 원본 조회 중 대상이 바뀌었습니다',
    'handoff.first_to_result': 'C-03 원본 조회 중 실패 인계 대상이 바뀌었습니다',
    'handoff.result_to_second': 'C-03 원본 조회 중 실패 인계 대상이 바뀌었습니다',
    'preflight.first_to_second': '실행 직전 실습 원본이 바뀌었습니다',
}
_DIAGNOSTIC_FIELDS = {
    'metadata': ('uid', 'name', 'namespace', 'resourceVersion', 'generation', 'managedFields',
                 'annotations', 'labels', 'deletionTimestamp', 'creationTimestamp', 'ownerReferences',
                 'finalizers'),
    'spec': ('source', 'destination', 'project', 'syncPolicy', 'template', 'replicas', 'selector',
             'strategy', 'holderIdentity', 'acquireTime', 'renewTime', 'leaseDurationSeconds',
             'leaseTransitions'),
    'status': ('reconciledAt', 'sync', 'health', 'operationState', 'conditions', 'resources',
               'observedGeneration', 'replicas', 'readyReplicas', 'availableReplicas',
               'unavailableReplicas', 'updatedReplicas'),
}


def _changed_paths(before, after, resource):
    if not isinstance(before, dict) or not isinstance(after, dict):
        return ['/'+resource]
    paths = []
    for section, fields in _DIAGNOSTIC_FIELDS.items():
        if (section in before, before.get(section)) == (section in after, after.get(section)):
            continue
        left, right = before.get(section), after.get(section)
        prefix = '/'+resource+'/'+section
        if not isinstance(left, dict) or not isinstance(right, dict):
            paths.append(prefix)
            continue
        for field in fields:
            if (field in left, left.get(field)) != (field in right, right.get(field)):
                paths.append(prefix+'/'+field)
        if {k: v for k, v in left.items() if k not in fields} != {
                k: v for k, v in right.items() if k not in fields}:
            paths.append(prefix+'/other')
    for field in ('apiVersion', 'kind', 'operation'):
        if (field in before, before.get(field)) != (field in after, after.get(field)):
            paths.append('/'+resource+'/'+field)
    known = {*_DIAGNOSTIC_FIELDS, 'apiVersion', 'kind', 'operation'}
    if {k: v for k, v in before.items() if k not in known} != {
            k: v for k, v in after.items() if k not in known}:
        paths.append('/'+resource+'/other')
    return paths


class RecoveryResourcesChanged(ValueError):
    """전체 원본 판정은 유지하고 실패 단계·고정 경로만 예외에 보존한다."""

    def __init__(self, phase, before, after, *, now=None):
        if phase not in _PHASE_MESSAGES:
            raise ValueError('resource_comparison_phase_rejected')
        now = datetime.now(timezone.utc) if now is None else now
        paths = []
        for resource in ('application', 'deployment', 'lease'):
            left, right = before[resource], after[resource]
            accepted = (same_application_observation(left, right, now=now)
                        if resource == 'application' else left == right)
            if not accepted:
                paths.extend(_changed_paths(left, right, resource))
        self.phase, self.changed_paths = phase, tuple(paths[:32])
        self.truncated = len(paths) > 32
        super().__init__(_PHASE_MESSAGES[phase])

    def diagnostic(self):
        return {'phase': self.phase, 'changed_paths': list(self.changed_paths),
                'truncated': self.truncated}


def require_same_recovery_resources(before, after, *, phase, now=None):
    if not same_recovery_resources(before, after, now=now):
        raise RecoveryResourcesChanged(phase, before, after, now=now)
