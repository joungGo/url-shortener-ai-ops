"""Synthetic evidence gaps through collection, persistence and Slack; no network."""
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

import test_live_collectors as live
import test_slack_adapter as slack
from ai_ops.b_contracts import ReadResponse
from ai_ops.data_policy import validate_snapshot
from ai_ops.evidence import NoDiagnosticFields, normalize_captured_payload
from ai_ops.result_explanation import evidence_summary
from ai_ops.contracts import TimeWindow

LOG_PATH = '/api/v1/namespaces/url-shortener/pods/url-shortener-api-123-abc/log'
ACCESS = 'INFO:     192.0.2.1:45678 - "GET /items/123?token=PRIVATE_CANARY&x=RedisError HTTP/1.1" 200 OK'
CACHE = 'INFO:app.api.routes.item:cache_hit endpoint=get_item item_id=123'


def body(message):
    return ('2026-10-16T01:00:00Z ' + message + '\n').encode()


def rows(summary, kind, key):
    return {r[key]: r for r in summary['coverage'][kind]}


class CoverageCollectorTest(unittest.TestCase):
    def setUp(self):
        self.f = live.LiveCollectorsTest()
        self.f.setUp()

    def summary(self):
        self.f.clock.elapsed = 0
        snapshot = self.f.snapshot()
        validate_snapshot(snapshot, self.f.settings)
        return snapshot, evidence_summary(snapshot.bundle, snapshot.audit)

    def test_access_and_cache_projection_roundtrip_keeps_only_observations(self):
        self.f.api.overrides[LOG_PATH] = ReadResponse(200, body(ACCESS) + body(CACHE))
        snapshot, summary = self.summary()
        payload = self.f.item(snapshot, 'pod-0-log').payload
        text = json.dumps(payload)
        self.assertIn('http_status=200', text)
        self.assertIn('cache_signal=hit', text)
        for secret in ('192.0.2.1', '/items/123', 'PRIVATE_CANARY', 'RedisError', 'item_id='):
            self.assertNotIn(secret, text)
        self.assertEqual(summary['coverage']['logs']['access_lines'], 2)  # current + previous
        self.assertEqual(snapshot.audit.collection_status, 'partial')

    def test_new_observations_do_not_expand_event_projection(self):
        window = TimeWindow(start=live.AT-timedelta(minutes=1), end=live.AT)
        payload,_ = normalize_captured_payload('event', {
            'reason':'Started','type':'Normal','involved_uid':'pod-uid',
            'message':'INFO:     192.0.2.1:1234 - "GET /healthz HTTP/1.1" 200 OK'}, window)
        self.assertEqual(payload['message'],'자유 메시지는 수집 정책에 따라 생략됨')

    def test_unsupported_access_logs_do_not_fall_back_to_url_markers(self):
        window = TimeWindow(start=live.AT-timedelta(minutes=1), end=live.AT)
        messages = (
            'INFO:     192.0.2.1:1234 - "TRACE /items?x=RedisError HTTP/1.1" 200 OK',
            'INFO:     192.0.2.1:1234 - "GET /items?x=RedisError HTTP/1.1" 203 Non-Authoritative Information',
            'INFO:     192.0.2.1:1234 - "GET /items?x=RedisError HTTP/1.1" 999 Invalid',
        )
        for message in messages:
            with self.subTest(message=message), self.assertRaises(NoDiagnosticFields):
                normalize_captured_payload('log', {
                    'container':'api','lines':[body(message).decode().rstrip()]}, window)

    def test_unsupported_cache_records_do_not_extract_untrusted_tail_markers(self):
        window = TimeWindow(start=live.AT-timedelta(minutes=1), end=live.AT)
        with self.assertRaises(NoDiagnosticFields):
            normalize_captured_payload('log', {'container':'api','lines':[
                body('INFO:app.api.routes.item:cache_hit endpoint=get_item name=RedisError').decode().rstrip()]}, window)

    def test_legacy_normalized_diagnostics_remain_idempotent(self):
        window = TimeWindow(start=live.AT-timedelta(minutes=1), end=live.AT)
        source = {'container':'api','lines':[
            '2026-10-16T01:00:00Z RedisError db_route=primary operation=select_one result=error']}
        first,_ = normalize_captured_payload('log',source,window)
        second,_ = normalize_captured_payload('log',first,window)
        self.assertEqual(first,second)

    def test_all_allowed_methods_statuses_and_probe_classification(self):
        for method in ('GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'):
            for code in (200, 404, 503):
                self.f.api.overrides[LOG_PATH] = ReadResponse(200, body(
                    f'INFO:     [2001:db8::1]:8000 - "{method} /healthz HTTP/1.1" {code} OK'))
                _, summary = self.summary()
                logs = summary['coverage']['logs']
                self.assertEqual(logs['probe_lines'], 2)
                self.assertEqual(logs['access_lines'], 2)

    def test_cache_signal_does_not_become_redis_error_or_recent_write_fact(self):
        for signal, tail in (('hit',''), ('miss',''), ('bypass',' reason=recent_write')):
            self.f.api.overrides[LOG_PATH] = ReadResponse(200, body(
                f'INFO:app.api.routes.item:cache_{signal} endpoint=list_items{tail}'))
            snapshot, summary = self.summary()
            text = snapshot.bundle.model_dump_json()
            self.assertIn('cache_signal='+signal, text)
            self.assertNotIn('recent write marker', text)
            self.assertEqual(summary['coverage']['logs']['cache_lines'], 2)

    def test_unrecognized_and_malformed_logs_still_have_no_diagnostic_fields(self):
        window = TimeWindow(start=live.AT-timedelta(minutes=1), end=live.AT)
        for message in ('hello PRIVATE_CANARY', 'cache_hit endpoint=private',
                        'INFO: 192.0.2.1:80 - "GET / HTTP/1.1" 999 Bad',
                        'http_method=GET http_status=200 http_path_kind=private',
                        'INFO:app.api.routes.item:cache_hit endpoint=get_item token=PRIVATE_CANARY'):
            with self.subTest(message=message):
                with self.assertRaises(NoDiagnosticFields):
                    normalize_captured_payload('log', {'container':'api','lines':[body(message).decode().rstrip()]}, window)

    def test_cut_access_line_cannot_create_observation(self):
        self.f.api.overrides[LOG_PATH] = ReadResponse(200, body(ACCESS).rstrip())
        snapshot, summary = self.summary()
        self.assertEqual(self.f.item(snapshot,'pod-0-log').missing_reason, 'truncated')
        self.assertEqual(summary['coverage']['logs']['access_lines'], 0)

    def test_access_collection_uses_same_call_count_and_budgets_as_legacy(self):
        baseline = self.f.snapshot()
        before = [(r.path, r.params, limits) for r,limits in self.f.api.calls]
        self.setUp()
        self.f.api.overrides[LOG_PATH] = ReadResponse(200, body(ACCESS))
        after, _ = self.summary()
        after_calls = [(r.path,r.params,limits) for r,limits in self.f.api.calls]
        self.assertEqual([p for p,_,_ in before], [p for p,_,_ in after_calls])
        for (path,params,limits),(_,old_params,old_limits) in zip(after_calls,before):
            if path.endswith('/log'):
                self.assertLessEqual(int(dict(params)['limitBytes']),self.f.settings.runtime.limits.raw_log_bytes)
                self.assertEqual(dict(params)['tailLines'],dict(old_params)['tailLines'])
            else:
                self.assertEqual((params,limits),(old_params,old_limits))
        self.assertEqual(baseline.audit.collection_status,after.audit.collection_status)

    def test_zero_missing_and_fractional_samples_remain_distinct(self):
        binding = self.f.settings.bindings.bindings[0]
        expr = self.f.settings.metrics.render('http_error_ratio',binding)
        self.f.api.expression_values[expr['requests']] = 0
        cache_expr = self.f.settings.metrics.render('cache_outcomes',binding)
        self.f.api.metric_overrides[cache_expr['error']] = {'status':'success','data':{'resultType':'vector','result':[]}}
        snapshot, summary = self.summary()
        metrics = rows(summary,'metrics','query_id')
        self.assertEqual(metrics['http_error_ratio']['status'],'no_traffic')
        self.assertNotIn('observed_increase',metrics['http_error_ratio'])
        self.assertEqual(metrics['cache_outcomes']['missing_outcomes'],['error'])
        self.assertEqual(metrics['cache_outcomes']['status'],'partial')
        self.assertEqual(self.f.item(snapshot,'metric-cache-outcome-gaps').missing_reason,'empty_response')
        self.f.api.expression_values[expr['requests']] = 0.25
        _, summary = self.summary()
        metric = rows(summary,'metrics','query_id')['http_error_ratio']
        self.assertEqual(metric['observed_increase'],0.25)
        self.assertEqual(metric['status'],'available')

    def test_scrape_quality_is_not_a_request_count(self):
        self.f.api.quality_samples = 1
        snapshot, summary = self.summary()
        for metric in summary['coverage']['metrics']:
            self.assertEqual(metric['status'],'scrape_unavailable')
            self.assertNotIn('observed_increase',metric)
        self.assertEqual(snapshot.audit.collection_status,'partial')
        self.assertFalse(any('별도 승인된 정상 요청' in s for s in summary['coverage']['next_checks']))

    def test_missing_denominator_or_invalid_values_never_become_zero(self):
        binding = self.f.settings.bindings.bindings[0]
        expression = self.f.settings.metrics.render('api_latency',binding)['count']
        for values, expected in (([], 'missing_series'), ([({},'NaN')], 'unavailable')):
            self.f.api.metric_overrides[expression] = json.loads(self.f.api.vector(values).body)
            snapshot, summary = self.summary()
            row = rows(summary,'metrics','query_id')['api_latency']
            self.assertEqual(row['status'],expected)
            self.assertNotIn('observed_increase',row)
            self.assertNotEqual(snapshot.audit.collection_status,'complete')

    def test_current_argo_stale_history_and_absent_ci_are_separate(self):
        app = self.f.api.docs['app']
        app['status']['history'] = [{'source':app['spec']['source'], 'revision':'a'*40,
            'deployedAt':live.stamp(live.AT-timedelta(days=1))}]
        snapshot, summary = self.summary()
        history = rows(summary,'history','category')
        self.assertEqual(history['current']['status'],'available')
        self.assertEqual(history['argo']['status'],'stale')
        self.assertEqual(history['ci']['status'],'missing')
        self.assertIsNone(self.f.item(snapshot,'deployment-history-0').payload)
        self.assertTrue(any('정상 CI 영수증은' in c for c in summary['coverage']['next_checks']))

    def test_mixed_history_does_not_hide_stale_entry_and_scope_mismatch(self):
        app = self.f.api.docs['app']
        app['status']['history'] = [
            {'source':app['spec']['source'], 'revision':'a'*40,'deployedAt':live.stamp(live.AT-timedelta(days=1))},
            {'source':app['spec']['source'], 'revision':'b'*40,'deployedAt':live.stamp(live.AT-timedelta(seconds=10))}]
        _, summary = self.summary()
        self.assertEqual(rows(summary,'history','category')['argo']['status'],'mixed')
        app['status']['history'][0]['source'] = {'repoURL':'https://private.invalid','path':'private'}
        _, summary = self.summary()
        text = json.dumps(summary)
        self.assertNotIn('private.invalid',text)
        self.assertNotIn('a'*40,text)
        self.assertEqual(rows(summary,'history','category')['argo']['status'],'mixed')


class CoverageReplyTest(slack.SlackFixture, unittest.TestCase):
    def test_persistent_worker_result_and_same_thread_reply_show_specific_gaps(self):
        clock = live.Clock()
        api = live.API(self.fixture.settings,clock)
        api.overrides[LOG_PATH] = ReadResponse(200,body(ACCESS))
        binding = self.fixture.settings.bindings.bindings[0]
        for query,outcome in (('http_error_ratio','requests'),('api_latency','count'),('db_latency','count')):
            api.expression_values[self.fixture.settings.metrics.render(query,binding)[outcome]] = 0
        from ai_ops.live_collectors import collect_snapshot
        self.fixture.snapshot = collect_snapshot(self.fixture.settings,self.fixture.target.target,transport=api,clock=clock)
        request, job = self.complete()
        result = self.tasks.result(job)
        self.assertEqual(result['investigation_state'],'partial')
        self.assertNotEqual(result['observed_state'],'정상')
        self.assertFalse(result['execution_allowed'])
        with patch('socket.create_connection',side_effect=AssertionError('no network')):
            self.adapter.reply(request,self.transport)
        text = self.transport.last_payload['text']
        for expected in ('HTTP 오류율: 요청·표본량 0','API 지연: 요청·표본량 0','검증된 CI 실패·원복 이력: 미확보','HTTP 2줄'):
            self.assertIn(expected,text)
        self.assertNotIn('PRIVATE_CANARY',text)
        self.assertLessEqual(len(text),4000)
        self.assertEqual(self.transport.last_payload['channel'],self.adapter._row(request)['channel_id'])

    def test_all_gap_explanations_and_three_citations_fit(self):
        request, job = self.complete()
        result = self.tasks.result(job)
        from ai_ops.result_explanation import REASONS
        result['evidence_summary']['limitations'] = [s for _,s in REASONS] + ['일부 자료의 형식·시각·대상 일치 검증을 통과하지 못했습니다.']
        # Maximal fixed coverage checks and missing outcome names.
        from ai_ops.evidence_coverage import coverage
        value = coverage(self.fixture.snapshot.bundle,self.fixture.snapshot.audit)
        value['logs'].update(access_lines=2400,probe_lines=2400,cache_lines=2400)
        states = ['partial','no_traffic','missing_series','scrape_unavailable','unavailable']
        for row,state in zip(value['metrics'],states): row['status'] = state
        value['metrics'][0]['missing_outcomes'] = ['bypass','consistency_primary','error','hit','miss','unavailable']
        value['next_checks'] = [
            '로그는 제한된 표본입니다. probe 응답과 업무 요청을 구분하고 지원하지 않는 형식은 정제 수집 항목을 확인하세요.',
            '트래픽 표본이 없습니다. 별도 승인된 정상 요청 뒤 같은 관측 구간에서 다시 확인하세요. 자동 요청은 보내지 않습니다.',
            '누락 시계열의 exporter·등록 label·카운터 초기화를 확인하세요. 없는 시계열을 0으로 채우지 않습니다.',
            'scrape 최신성·연속성·표본 수를 기존 기준으로 확인하세요. 대기·재시도로 수집 기한을 늘리지 않습니다.',
            '대상·revision·UID·원래 시각이 검증된 배포/CI 이력이 필요합니다. 정상 CI 영수증은 실패·원복 이력을 대신하지 않습니다.']
        result['evidence_summary']['coverage'] = value
        result['runbook_context'] = {'status':'available','citations':[
            {'document_id':'d'*64,'document_version':1000000,'section':{'id':'L65536-L65536'},'content_sha256':'a'*64} for _ in range(3)]}
        with patch.object(self.tasks,'result',return_value=result):
            payload = self.adapter._payload(self.adapter._row(request))
        self.assertLessEqual(len(payload['text']),4000)
        self.assertEqual(payload['text'].count('SHA256'),3)


if __name__ == '__main__':
    unittest.main()
