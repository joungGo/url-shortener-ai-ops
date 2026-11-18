"""검증한 저장 증거를 고정 어휘로 설명한다. 원문 로그/예외/경로는 회신하지 않는다."""

KINDS = {
    'workload_status': 'Pod 상태', 'deployment': '배포 정보',
    'config_summary': '설정 요약', 'log': '로그 진단 정보',
    'event': 'Kubernetes 이벤트', 'metric': '관측 지표',
}
# 원인을 합쳐 보여주되 누락을 정상이나 0건으로 해석하지 않는다.
REASONS = (
    ({'truncated', 'response_too_large', 'raw_budget_exceeded', 'log_budget_exceeded', 'payload_too_large'},
     '수집 크기·줄 수 상한 또는 잘린 응답 때문에 전체 자료를 확보하지 못했습니다. 유효한 로그 표본이 있으면 그 범위만 분석했습니다.'),
    ({'no_traffic'}, '요청·표본이 없어 오류율과 지연 시간을 판단할 수 없습니다. 오류 0%나 정상으로 해석하지 않습니다.'),
    ({'empty_response'}, '이벤트 또는 지표 등의 조회 결과가 비어 있습니다. 문제가 없다는 증거로 해석하지 않습니다.'),
    ({'no_events_observed'}, '관련 객체의 이벤트 조회는 성공했지만 반환된 이벤트가 없습니다. 보존 기간·관측 범위 때문에 과거 장애가 없었다는 뜻은 아닙니다.'),
    ({'missing_history'}, '판단에 필요한 배포·CI 이력을 확보하지 못했습니다.'),
    ({'outside_window'}, '조사 시간 범위 밖의 자료는 현재 상태의 근거로 사용하지 않았습니다.'),
    ({'no_diagnostic_fields'}, '수집 자료에서 지원하는 진단 항목을 찾지 못했습니다.'),
    ({'permission_denied', 'not_found', 'not_configured'}, '일부 자료를 조회할 권한·대상·설정이 확인되지 않았습니다.'),
    ({'timeout', 'deadline_exceeded', 'upstream_error'}, '일부 조회가 응답하지 않았거나 기존 수집 시간 제한에 도달했습니다.'),
    ({'scrape_unavailable'}, '지표 수집의 최신성·연속성이 확인되지 않았습니다.'),
)


def evidence_summary(bundle, audit):
    available = [label for kind, label in KINDS.items()
                 if any(item.kind == kind and item.status == 'available' for item in bundle.evidence)]
    reasons = {item.missing_reason for item in bundle.evidence if item.status != 'available'}
    if audit:
        reasons.update(entry.reason for entry in audit.entries if entry.status != 'available')
        if any(entry.truncated for entry in audit.entries):
            reasons.add('truncated')
        if audit.deadline_exceeded:
            reasons.add('deadline_exceeded')
    # A dedicated collector marker distinguishes newly verified empty queries
    # without changing persisted reason enums or reinterpreting legacy data.
    empty = [item for item in bundle.evidence if item.status != 'available'
             and item.missing_reason == 'empty_response']
    if any(item.kind == 'event' and getattr(item, 'evidence_id', '') == 'ev-events-empty-observed'
           for item in empty):
        reasons.add('no_events_observed')
        if all(item.kind == 'event' and getattr(item, 'evidence_id', '') == 'ev-events-empty-observed'
               for item in empty):
            reasons.discard('empty_response')
    limitations = [text for codes, text in REASONS if reasons & codes]
    known = set().union(*(codes for codes, _ in REASONS))
    if reasons - known:
        limitations.append('일부 자료의 형식·시각·대상 일치 검증을 통과하지 못했습니다.')
    result = {'available': available, 'limitations': limitations}
    if audit is not None:
        from ai_ops.evidence_coverage import coverage
        result['coverage'] = coverage(bundle, audit)
    return result


def result_text(target, result):
    state = result.get('investigation_state')
    label = '일부 자료만 확인했습니다 (partial)' if state == 'partial' else '조사 결과'
    text = f'{target} — {label}\n'
    if result['state'] != 'available':
        return text + '이 작업의 조사 결과를 사용할 수 없습니다.\n'
    text += f"판정: {result['summary']}\n현재 상태: {result['observed_state']}\n"
    summary = result.get('evidence_summary', {})
    if summary.get('available'):
        text += '\n확보한 자료: ' + ', '.join(summary['available']) + '\n'
    if summary.get('limitations'):
        text += '\n판단에 부족한 자료\n' + ''.join('- ' + item + '\n' for item in summary['limitations'])
    elif state == 'partial':
        text += '\n판단에 부족한 자료: 전체 상태를 확정할 근거가 충분하지 않습니다.\n'
    if summary.get('coverage'):
        from ai_ops.evidence_coverage import coverage_text
        text += '\n' + coverage_text(summary['coverage'])
    if state == 'partial':
        text += 'partial은 자료 범위가 불완전하다는 뜻입니다. 이것만으로 정상·장애를 확정할 수 없습니다.\n'
    return text + '\n'
