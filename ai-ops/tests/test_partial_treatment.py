"""Bounded evidence explanations with synthetic transports; no live actions."""
import copy
import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import test_live_collectors as live
import test_slack_adapter as slack
from ai_ops.b_contracts import ReadResponse
from ai_ops.collection import CapturedSnapshot
from ai_ops.contracts import build_evidence_bundle
from ai_ops.data_policy import validate_snapshot
from ai_ops.evidence_coverage import coverage_text
from ai_ops.live_collectors import collect_snapshot
from ai_ops.result_explanation import evidence_summary

EVENTS = '/api/v1/namespaces/url-shortener/events'
LOGS = '/api/v1/namespaces/url-shortener/pods/url-shortener-api-123-abc/log'
EMPTY = {'kind': 'EventList', 'metadata': {}, 'items': []}


def history(api, at):
    app = api.docs['app']
    app['status']['history'] = [{'source': app['spec']['source'],
                                'revision': 'a' * 40, 'deployedAt': live.stamp(at)}]


class PartialTreatmentTest(unittest.TestCase):
    def setUp(self):
        self.f = live.LiveCollectorsTest()
        self.f.setUp()

    def collect(self):
        result = self.f.snapshot()
        validate_snapshot(result, self.f.settings)
        return result, evidence_summary(result.bundle, result.audit)

    def test_empty_events_mean_successful_query_not_health_or_complete(self):
        self.f.api.overrides[EVENTS] = EMPTY
        result, summary = self.collect()
        item = self.f.item(result, 'events-empty-observed')
        self.assertEqual((item.status, item.missing_reason), ('missing', 'empty_response'))
        self.assertIsNone(item.payload)
        self.assertEqual(summary['coverage']['events']['status'], 'no_events_observed')
        self.assertEqual(result.audit.collection_status, 'partial')
        queries = [r for r, _ in self.f.api.calls if r.path == EVENTS]
        self.assertEqual(len(queries), 3)
        self.assertEqual({dict(r.params)['fieldSelector'] for r in queries},
                         {'involvedObject.uid=dep-uid', 'involvedObject.uid=rs-uid', 'involvedObject.uid=pod-uid'})
        self.assertTrue(any('과거 장애가 없었다는 뜻은 아닙니다' in s for s in summary['limitations']))

    def test_failed_or_incomplete_event_queries_never_claim_empty_success(self):
        wrong = copy.deepcopy(self.f.api.docs['event'])
        wrong['involvedObject']['uid'] = 'other-uid'
        failures = [ReadResponse(403, b'PRIVATE_CANARY'), TimeoutError('PRIVATE_CANARY'),
                    {'kind': 'EventList', 'metadata': {'continue': 'PRIVATE_CANARY'}, 'items': []},
                    {'kind': 'EventList', 'metadata': {}, 'items': [wrong]},
                    {'kind': 'PRIVATE_CANARY', 'items': []}]
        for failure in failures:
            with self.subTest(failure_type=type(failure).__name__):
                self.setUp()
                def response(req):
                    if dict(req.params)['fieldSelector'] != 'involvedObject.uid=pod-uid':
                        return self.f.api.response(EMPTY)
                    if isinstance(failure, Exception):
                        raise failure
                    return failure if isinstance(failure, ReadResponse) else self.f.api.response(failure)
                self.f.api.overrides[EVENTS] = response
                result, summary = self.collect()
                self.assertFalse(any(i.evidence_id.startswith('ev-events-empty') for i in result.bundle.evidence))
                self.assertNotEqual(summary['coverage']['events']['status'], 'no_events_observed')
                self.assertNotIn('PRIVATE_CANARY', json.dumps(summary))
                self.assertEqual(result.audit.collection_status, 'partial')

    def test_legacy_empty_response_does_not_acquire_new_success_claim(self):
        item = SimpleNamespace(kind='event', evidence_id='ev-events-empty', status='missing',
                               missing_reason='empty_response')
        summary = evidence_summary(SimpleNamespace(evidence=[item]), SimpleNamespace(
            entries=[], deadline_exceeded=False))
        self.assertEqual(summary['coverage']['events']['status'], 'empty_unverified')
        self.assertIn('전체 조회 성공 여부 미확인', coverage_text(summary['coverage']))

    def test_no_events_marker_cannot_hide_another_event_or_change_kind(self):
        self.f.api.overrides[EVENTS] = EMPTY
        result, _ = self.collect()
        old = self.f.item(result, 'events-empty-observed')
        entry = next(e for e in result.audit.entries if e.evidence_id == old.evidence_id)
        # Duplicate empty marker, with a different ID, makes the assertion invalid.
        extra = old.model_copy(update={'evidence_id': 'ev-events-extra'})
        bundle = build_evidence_bundle(target=result.bundle.target, window=result.bundle.window,
            created_at=result.bundle.created_at, evidence=[*result.bundle.evidence, extra])
        audit = result.audit.model_copy(update={'entries': [*result.audit.entries,
            entry.model_copy(update={'evidence_id': extra.evidence_id})]})
        with self.assertRaises(ValueError):
            validate_snapshot(CapturedSnapshot(bundle, audit), self.f.settings)

    def test_bounded_log_sample_explains_retained_range_without_loss_estimate(self):
        self.f.api.docs['pod']['status']['containerStatuses'][0]['restartCount'] = 0
        def capped(req):
            count = int(dict(req.params)['tailLines'])
            lines = []
            for n in range(count):
                message = ('INFO:     192.0.2.1:12 - "GET /healthz HTTP/1.1" 200 OK' if n % 3 == 0
                    else 'INFO:     192.0.2.1:12 - "GET /items/1?secret=PRIVATE_CANARY HTTP/1.1" 200 OK' if n % 3 == 1
                    else 'RedisError secret=PRIVATE_CANARY')
                lines.append(live.stamp(live.AT-timedelta(seconds=count-n)) + ' ' + message + '\n')
            return ReadResponse(200, ''.join(lines).encode())
        self.f.api.overrides[LOGS] = capped
        result, summary = self.collect()
        logs = summary['coverage']['logs']
        self.assertEqual(logs['limited_sources'], 1)
        self.assertGreater(logs['non_probe_lines'], 0)
        self.assertGreater(logs['diagnostic_lines'], 0)
        self.assertEqual(logs['retained_lines'], logs['access_lines'] + logs['diagnostic_lines'])
        self.assertLess(logs['sample_start'], logs['sample_end'])
        self.assertNotIn('PRIVATE_CANARY', json.dumps(summary))
        self.assertIn('미수집 줄 수는 알 수 없습니다', coverage_text(summary['coverage']))
        self.assertEqual(result.audit.collection_status, 'partial')
        self.assertIsNone(self.f.item(result, 'pod-0-log').payload)

    def test_rejected_sample_and_capped_original_count_one_source_per_stream(self):
        for restarts, expected in ((0, 1), (1, 2)):
            with self.subTest(restarts=restarts):
                self.setUp()
                self.f.settings.runtime.limits.payload_bytes = 600
                self.f.api.docs['pod']['status']['containerStatuses'][0]['restartCount'] = restarts
                self.f.api.overrides[LOGS] = lambda req: ReadResponse(200, (
                    '2026-10-16T01:00:00Z RedisError\n' * int(dict(req.params)['tailLines'])).encode())
                result, summary = self.collect()
                self.assertEqual(self.f.item(result, 'pod-0-log').missing_reason, 'truncated')
                self.assertEqual(self.f.item(result, 'pod-0-log-sample').missing_reason, 'payload_too_large')
                self.assertEqual(summary['coverage']['logs']['limited_sources'], expected)
                self.assertEqual(summary['coverage']['logs']['retained_lines'], 0)
                self.assertEqual(result.audit.collection_status, 'partial')

    def test_history_keeps_old_times_without_fabricating_failure_or_recent_deploy(self):
        history(self.f.api, live.AT-timedelta(days=1))
        result, summary = self.collect()
        rows = {r['category']: r for r in summary['coverage']['history']}
        self.assertEqual(rows['argo']['latest_outside_window_at'], '2026-10-15T01:00:00Z')
        self.assertEqual(rows['argo']['in_window_records'], 0)
        self.assertEqual(rows['argo']['outside_window_records'], 1)
        self.assertEqual(rows['current']['status'], 'available')
        self.assertEqual(rows['ci']['status'], 'missing')
        self.assertIsNone(self.f.item(result, 'deployment-history-0').payload)
        self.assertIsNone(self.f.item(result, 'deployment-history-0').observed_at)
        text = coverage_text(summary['coverage'])
        self.assertIn('현재 증거로 재사용하지 않습니다', text)
        self.assertIn('이력을 채우기 위한 재배포·장애 주입은 하지 않습니다', text)
        # Untrusted history text fails validation rather than leaking into Slack.
        self.f.clock.elapsed = 0
        self.f.api.docs['app']['status']['history'][0]['deployedAt'] = 'PRIVATE_CANARY'
        _, summary = self.collect()
        self.assertNotIn('PRIVATE_CANARY', json.dumps(summary))
        self.assertNotIn('latest_outside_window_at', summary['coverage']['history'][1])

    def test_empty_events_add_no_read_retry_and_do_not_extend_limits(self):
        self.f.snapshot()
        before = [(r.path, r.params, limits['timeout_seconds']) for r, limits in self.f.api.calls]
        settings = self.f.settings.runtime.model_dump()
        self.setUp()
        self.f.api.overrides[EVENTS] = EMPTY
        _, summary = self.collect()
        after = [(r.path, r.params, limits['timeout_seconds']) for r, limits in self.f.api.calls]
        self.assertEqual(before, after)
        self.assertEqual(settings, self.f.settings.runtime.model_dump())


class PartialTreatmentReplyTest(slack.SlackFixture, unittest.TestCase):
    def test_expanded_explanation_preserves_three_citations_within_reply_budget(self):
        from ai_ops.result_explanation import REASONS
        request, job = self.complete()
        result = self.tasks.result(job)
        summary = result['evidence_summary']
        summary['limitations'] = [text for _, text in REASONS]
        logs = summary['coverage']['logs']
        logs.update(retained_lines=2400, access_lines=2400, probe_lines=2400,
                    non_probe_lines=2400, cache_lines=2400, diagnostic_lines=2400,
                    limited_sources=24, sample_start='2026-10-16T00:45:00.123456Z',
                    sample_end='2026-10-16T01:00:00.123456Z')
        for row in summary['coverage']['history'][1:]:
            row['latest_outside_window_at'] = '2026-10-15T01:00:00.123456Z'
        result['runbook_context'] = {'status': 'available', 'citations': [
            {'document_id': 'd'*64, 'document_version': 1000000,
             'section': {'id': 'L65536-L65536'}, 'content_sha256': 'a'*64} for _ in range(3)]}
        with patch.object(self.tasks, 'result', return_value=result):
            payload = self.adapter._payload(self.adapter._row(request))
        self.assertLessEqual(len(payload['text']), 4000)
        self.assertEqual(payload['text'].count('SHA256'), 3)

    def test_persisted_queue_worker_reply_keeps_partial_and_execution_denied(self):
        clock = live.Clock()
        api = live.API(self.fixture.settings, clock)
        api.overrides[EVENTS] = EMPTY
        history(api, live.AT-timedelta(days=1))
        self.fixture.snapshot = collect_snapshot(self.fixture.settings, self.fixture.target.target,
                                                  transport=api, clock=clock)
        request, job = self.complete()
        with patch('socket.create_connection', side_effect=AssertionError('no network')):
            result = self.tasks.result(job)
            self.adapter.reply(request, self.transport)
        self.assertEqual(result['investigation_state'], 'partial')
        self.assertFalse(result['execution_allowed'])
        self.assertNotEqual(result['observed_state'], '정상')
        text = self.transport.last_payload['text']
        self.assertIn('조회 성공·반환 이벤트 없음', text)
        self.assertIn('2026-10-15T01:00:00Z', text)
        self.assertIn('정상 CI 영수증은 실패·원복 이력을 대신하지 않습니다', text)
        self.assertLessEqual(len(text), 4000)
        self.assertEqual(self.transport.last_payload['thread_ts'], self.adapter._row(request)['thread_ts'])
        self.assertEqual(self.transport.calls, 1)


if __name__ == '__main__':
    unittest.main()
