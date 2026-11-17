"""Explain verified evidence coverage without collecting data or changing verdicts.

Only fixed labels, finite numbers and evidence IDs leave this projection. Missing
samples are never filled in. Scrape samples and request counts are different;
request increases can be fractional, so they are not exact event counts.
"""
from __future__ import annotations

import math
import re

from ai_ops.evidence import QUERY_OUTCOMES

METRICS = {
    'cache_outcomes': '캐시 결과', 'cache_hit_ratio': '캐시 적중률',
    'http_error_ratio': 'HTTP 오류율', 'api_latency': 'API 지연', 'db_latency': 'DB 지연',
}
STATUS = {
    'no_traffic': 'no_traffic', 'empty_response': 'missing_series',
    'scrape_unavailable': 'scrape_unavailable', 'outside_window': 'stale',
    'missing_history': 'missing',
}
STATUS_TEXT = {
    'available': '표본 확보', 'partial': '일부만 확보', 'no_traffic': '요청·표본량 0',
    'missing_series': '시계열 누락', 'scrape_unavailable': '수집 품질 부족',
    'stale': '조사 시간 밖', 'missing': '미확보', 'unavailable': '검증·조회 실패',
    'mixed': '일부만 확보',
}


def _refs(items):
    return sorted({i.evidence_id for i in items if
                   re.fullmatch(r'ev-[A-Za-z0-9._-]{1,100}', getattr(i, 'evidence_id', ''))})[:24]


def _state(items):
    if not items:
        return 'missing'
    states = {'available' if i.status == 'available' else
              STATUS.get(i.missing_reason, 'unavailable') for i in items}
    return next(iter(states)) if len(states) == 1 else 'mixed'


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10**15


def coverage(bundle, audit):
    """Called only after TaskService validates persisted bundle, audit and report."""
    items = bundle.evidence
    entries = {entry.evidence_id: entry for entry in audit.entries} if audit else {}
    logs = [i for i in items if i.kind == 'log']
    observations = {'access_lines': 0, 'probe_lines': 0, 'cache_lines': 0, 'diagnostic_lines': 0}
    for item in logs:
        if item.status != 'available':
            continue
        for line in (getattr(item, 'payload', None) or {}).get('lines', []):
            message = line.partition(' ')[2]
            if message.startswith('http_method='):
                observations['access_lines'] += 1
                observations['probe_lines'] += int(message.endswith('http_path_kind=probe'))
            elif message.startswith('cache_signal='):
                observations['cache_lines'] += 1
            else:
                observations['diagnostic_lines'] += 1
    log_summary = {'status': _state(logs), **observations, 'evidence_ids': _refs(logs)}
    metrics = []
    for query, label in METRICS.items():
        selected = [i for i in items if i.kind == 'metric' and (
            getattr(i, 'evidence_id', '') == 'ev-metric-' + query or
            (i.status == 'available' and (getattr(i, 'payload', None) or {}).get('query_id') == query))]
        present = set()
        amounts = []
        for item in selected:
            if item.status != 'available':
                continue
            for sample in (item.payload or {}).get('samples', []):
                if sample.get('outcome') in QUERY_OUTCOMES[query] and _number(sample.get('value')):
                    present.add(sample['outcome'])
                    if sample['outcome'] in {'requests', 'count'}:
                        amounts.append(sample['value'])
        missing = QUERY_OUTCOMES[query] - present
        status = _state(selected)
        if status == 'available' and missing:
            status = 'partial'
        row = {'query_id': query, 'label': label, 'status': status,
               'present_outcomes': sorted(present), 'missing_outcomes': sorted(missing),
               'evidence_ids': _refs(selected)}
        # An available, normalized single metric has an observed denominator.
        # Do not invent one for empty/scrape failure or combine different scopes.
        if len(amounts) == 1 and len(selected) == 1:
            row['observed_increase'] = amounts[0]
        metrics.append(row)
    histories = []
    for category, label, field in (
        ('current', '현재 배포 정보', {'response_received_at', 'status_reconciled_at'}),
        ('argo', 'Argo 배포 이력', {'history_deployed_at'}),
        ('ci', '검증된 CI 실패·원복 이력', {'artifact_observed_at'}),
    ):
        selected = []
        for item in items:
            if item.kind != 'deployment':
                continue
            key = getattr(item, 'evidence_id', '')
            entry = entries.get(key)
            # Missing/invalid history entries in legacy collectors have the
            # default response_received_at. Handle their fixed keys first.
            if key.startswith('ev-deployment-ci-'):
                matches = category == 'ci'
            elif key.startswith('ev-deployment-history'):
                matches = category == 'argo'
            else:
                matches = entry is not None and entry.time_field in field
            if matches:
                selected.append(item)
        histories.append({'category': category, 'label': label,
                          'status': _state(selected), 'evidence_ids': _refs(selected)})
    checks = []
    if logs and (observations['access_lines'] or _state(logs) != 'available'):
        checks.append('로그는 제한된 표본입니다. probe 응답과 업무 요청을 구분하고 지원하지 않는 형식은 정제 수집 항목을 확인하세요.')
    if any(m['status'] == 'no_traffic' for m in metrics):
        checks.append('트래픽 표본이 없습니다. 별도 승인된 정상 요청 뒤 같은 관측 구간에서 다시 확인하세요. 자동 요청은 보내지 않습니다.')
    if any(m['status'] in {'missing_series', 'partial'} for m in metrics):
        checks.append('누락 시계열의 exporter·등록 label·카운터 초기화를 확인하세요. 없는 시계열을 0으로 채우지 않습니다.')
    if any(m['status'] == 'scrape_unavailable' for m in metrics):
        checks.append('scrape 최신성·연속성·표본 수를 기존 기준으로 확인하세요. 대기·재시도로 수집 기한을 늘리지 않습니다.')
    if any(h['status'] != 'available' for h in histories[1:]):
        checks.append('대상·revision·UID·원래 시각이 검증된 배포/CI 이력이 필요합니다. 정상 CI 영수증은 실패·원복 이력을 대신하지 않습니다.')
    return {'logs': log_summary, 'metrics': metrics, 'history': histories, 'next_checks': checks}


def coverage_text(value):
    """Compact presentation; keep three source citations within Slack's limit."""
    lines = []
    logs = value['logs']
    if logs['access_lines'] or logs['cache_lines']:
        lines.append(f"로그 표본: HTTP {logs['access_lines']}줄(probe {logs['probe_lines']}줄), 캐시 {logs['cache_lines']}줄. 응답 기록만으로 전체 정상은 확정하지 않습니다.")
    metric_parts = []
    for row in value['metrics']:
        suffix = STATUS_TEXT[row['status']]
        if 'observed_increase' in row:
            suffix += f", 추정 증가량 {row['observed_increase']:.6g}"
        if row['query_id'] == 'cache_outcomes' and row['status'] == 'partial':
            suffix += ' (' + ', '.join(row['missing_outcomes']) + ' 누락)'
        metric_parts.append(row['label'] + ': ' + suffix)
    lines.append('지표별 자료: ' + '; '.join(metric_parts))
    lines.append('이력 구분: ' + '; '.join(row['label'] + ': ' + STATUS_TEXT[row['status']] for row in value['history']))
    if value['next_checks']:
        lines.append('보완할 항목\n' + '\n'.join('- ' + check for check in value['next_checks']))
    return '\n'.join(lines) + '\n'
