"""Synthetic bounded log and result explanation regressions; no remote services."""
import unittest
from unittest.mock import patch
import test_live_collectors as live
import test_slack_adapter as slack
from ai_ops.b_contracts import ReadResponse

LOG_PATH = '/api/v1/namespaces/url-shortener/pods/url-shortener-api-123-abc/log'
LINE = '2026-10-16T01:00:00Z RedisError token=private-secret-value\n'

class PartialLogsTest(unittest.TestCase):
    def setUp(self):
        self.fixture = live.LiveCollectorsTest()
        self.fixture.setUp()
    def test_capped_current_and_previous_preserve_signal_without_complete_claim(self):
        f = self.fixture
        f.api.overrides[LOG_PATH] = lambda req: ReadResponse(200, (LINE * int(dict(req.params)['tailLines'])).encode())
        snapshot = f.snapshot()
        for key in ('pod-0-log', 'pod-0-previous-log'):
            self.assertEqual(f.item(snapshot, key).missing_reason, 'truncated')
            self.assertEqual(f.item(snapshot, key+'-sample').status, 'available')
        calls = [dict(req.params) for req,_ in f.api.calls if req.path.endswith('/log')]
        self.assertEqual([c['previous'] for c in calls], ['false','true'])
        self.assertEqual(sum(int(c['tailLines']) for c in calls), f.settings.runtime.limits.log_lines_per_pod)
        self.assertEqual(snapshot.audit.collection_status, 'partial')
        self.assertNotIn('private-secret-value', snapshot.bundle.model_dump_json())
    def test_cut_final_line_is_not_a_diagnostic_signal(self):
        f = self.fixture
        f.api.overrides[LOG_PATH] = ReadResponse(200, b'2026-10-16T01:00:00Z db_route=primary\n2026-10-16T01:00:00Z RedisError\xe3')
        snapshot=f.snapshot()
        self.assertEqual(f.item(snapshot,'pod-0-log').missing_reason,'truncated')
        sample=f.item(snapshot,'pod-0-log-sample')
        self.assertEqual(sample.status,'available')
        self.assertNotIn('RedisError',sample.model_dump_json())
    def test_capped_log_does_not_bypass_uid_check(self):
        f=self.fixture
        f.api.overrides[LOG_PATH] = lambda req: ReadResponse(200,(LINE*int(dict(req.params)['tailLines'])).encode())
        import copy
        pod=copy.deepcopy(f.api.docs['pod']);pod['metadata']['uid']='replacement-uid'
        f.api.overrides[LOG_PATH.removesuffix('/log')]=pod
        snapshot=f.snapshot()
        self.assertEqual(f.item(snapshot,'pod-0-log').missing_reason,'revision_changed')
        self.assertFalse(any(item.kind=='log' and item.status=='available' for item in snapshot.bundle.evidence))

    def test_single_unterminated_line_never_becomes_a_fact(self):
        f=self.fixture
        f.api.overrides[LOG_PATH]=ReadResponse(200,LINE.rstrip().encode())
        snapshot=f.snapshot()
        self.assertEqual(f.item(snapshot,'pod-0-log').missing_reason,'truncated')
        self.assertFalse(any(i.kind=='log' and i.status=='available' for i in snapshot.bundle.evidence))

    def test_no_restart_retains_full_line_budget(self):
        f=self.fixture
        f.api.docs['pod']['status']['containerStatuses'][0]['restartCount']=0
        f.api.overrides[LOG_PATH]=lambda req: ReadResponse(200,(LINE*int(dict(req.params)['tailLines'])).encode())
        snapshot=f.snapshot()
        calls=[dict(req.params) for req,_ in f.api.calls if req.path.endswith('/log')]
        self.assertEqual(len(calls),1)
        self.assertEqual(int(calls[0]['tailLines']),f.settings.runtime.limits.log_lines_per_pod)
        self.assertEqual(f.item(snapshot,'pod-0-log-sample').status,'available')

    def test_byte_cap_and_candidate_overhead_stay_bounded(self):
        f=self.fixture
        def capped(req):
            budget=int(dict(req.params)['limitBytes'])
            prefix=(LINE*2).encode()
            return ReadResponse(200,prefix+b'x'*(budget-len(prefix)))
        f.api.overrides[LOG_PATH]=capped
        snapshot=f.snapshot()
        for key in ('pod-0-log','pod-0-previous-log'):
            self.assertEqual(f.item(snapshot,key).missing_reason,'truncated')
            self.assertEqual(f.item(snapshot,key+'-sample').status,'available')
        calls=[dict(req.params) for req,_ in f.api.calls if req.path.endswith('/log')]
        self.assertLessEqual(sum(int(c['limitBytes']) for c in calls),f.settings.runtime.limits.raw_log_bytes)
        self.assertEqual(snapshot.audit.collection_status,'partial')

    def test_malformed_complete_line_and_oversize_response_remain_rejected(self):
        for body in (b'not-a-time RedisError\n', b'bad-utf8 \xff\n', b'x'*65537):
            with self.subTest(body_length=len(body)):
                self.setUp();f=self.fixture
                f.api.overrides[LOG_PATH]=ReadResponse(200,body)
                snapshot=f.snapshot()
                self.assertFalse(any(i.kind=='log' and i.status=='available' for i in snapshot.bundle.evidence))

    def test_no_traffic_stays_unknown_and_explained(self):
        from ai_ops.result_explanation import evidence_summary
        f=self.fixture
        expressions=f.settings.metrics.render('http_error_ratio',f.settings.bindings.bindings[0])
        f.api.expression_values[expressions['requests']]=0
        snapshot=f.snapshot()
        self.assertEqual(f.item(snapshot,'metric-http_error_ratio').missing_reason,'no_traffic')
        summary=evidence_summary(snapshot.bundle,snapshot.audit)
        self.assertTrue(any('오류 0%' in text for text in summary['limitations']))
        self.assertEqual(snapshot.audit.collection_status,'partial')

class PartialReplyTest(slack.SlackFixture,unittest.TestCase):
    def test_sampled_logs_survive_persistent_worker_and_report_revalidation(self):
        clock=live.Clock();api=live.API(self.fixture.settings,clock)
        api.overrides[LOG_PATH]=lambda req: ReadResponse(200,(LINE*int(dict(req.params)['tailLines'])).encode())
        from ai_ops.live_collectors import collect_snapshot
        self.fixture.snapshot=collect_snapshot(self.fixture.settings,self.fixture.target.target,transport=api,clock=clock)
        request_id,job=self.complete()
        result=self.tasks.result(job)
        self.assertEqual(result['investigation_state'],'partial')
        self.assertNotEqual(result['observed_state'],'정상')
        self.assertIn('로그 진단 정보',result['evidence_summary']['available'])
        self.assertTrue(any('상한' in reason for reason in result['evidence_summary']['limitations']))
        self.adapter.reply(request_id,self.transport)
        text=self.transport.last_payload['text']
        self.assertIn('유효한 로그 표본',text)
        self.assertNotIn('private-secret-value',text)

    def test_all_explanations_plus_three_citations_fit_slack_limit(self):
        from types import SimpleNamespace as Obj
        from ai_ops.result_explanation import REASONS,evidence_summary
        request_id,job=self.complete()
        result=self.tasks.result(job)
        reasons=set().union(*(codes for codes,_ in REASONS)) | {'invalid_time'}
        result['evidence_summary']=evidence_summary(Obj(evidence=[Obj(kind='log',status='missing',missing_reason=x) for x in reasons]),None)
        result['runbook_context']={'status':'available','citations':[
            {'document_id':'d'*64,'document_version':1000000,'section':{'id':'L65536-L65536'},'content_sha256':'a'*64} for _ in range(3)]}
        with patch.object(self.tasks,'result',return_value=result):
            payload=self.adapter._payload(self.adapter._row(request_id))
        self.assertLessEqual(len(payload['text']),4000)
        self.assertEqual(payload['text'].count('SHA256'),3)
        self.assertFalse(payload['mrkdwn']);self.assertFalse(payload['unfurl_links'])

    def test_same_queue_worker_reply_explains_partial_and_available_sources(self):
        request_id,job=self.complete()
        with patch('socket.create_connection',side_effect=AssertionError('no network')):
            self.adapter.reply(request_id,self.transport)
        result=self.tasks.result(job)
        self.assertIn('evidence_summary',result)
        text=self.transport.last_payload['text']
        for phrase in ('일부 자료만 확인했습니다','확보한 자료','판단에 부족한 자료','배포·CI 이력','실행하지 않았습니다'):
            self.assertIn(phrase,text)
        self.assertLessEqual(len(text),4000)
        self.assertNotIn('private-secret-value',text)
        self.assertFalse(result['execution_allowed'])


class PartialExplanationTest(unittest.TestCase):
    def test_all_missing_reason_groups_are_bounded_and_do_not_echo_payload(self):
        from types import SimpleNamespace as Obj
        from ai_ops.result_explanation import REASONS,evidence_summary,result_text
        reasons=set().union(*(codes for codes,_ in REASONS)) | {'invalid_time'}
        bundle=Obj(evidence=[Obj(kind='log',status='missing',missing_reason=x,payload='PRIVATE_UNTRUSTED') for x in reasons])
        summary=evidence_summary(bundle,None)
        result={'state':'available','investigation_state':'partial','summary':'증거 부족','observed_state':'확인 불가','evidence_summary':summary}
        text=result_text('synthetic-target',result)
        self.assertLess(len(text),1600)
        self.assertNotIn('PRIVATE_UNTRUSTED',text)
        self.assertIn('정상·장애를 확정할 수 없습니다',text)

    def test_deployment_object_does_not_claim_history_and_stale_logs_are_not_ci_history(self):
        from types import SimpleNamespace as Obj
        from ai_ops.result_explanation import evidence_summary
        bundle=Obj(evidence=[Obj(kind='deployment',status='available',missing_reason=None),
                             Obj(kind='log',status='stale',missing_reason='outside_window')])
        summary=evidence_summary(bundle,None)
        self.assertEqual(summary['available'],['배포 정보'])
        self.assertIn('조사 시간 범위 밖',summary['limitations'][0])
        self.assertFalse(any('CI 이력' in text for text in summary['limitations']))

    def test_non_partial_failure_is_not_reported_as_insufficient_only(self):
        from ai_ops.result_explanation import result_text
        result={'state':'available','investigation_state':'completed','summary':'장애 징후 확인','observed_state':'장애','evidence_summary':{}}
        text=result_text('synthetic-target',result)
        self.assertIn('장애 징후 확인',text)
        self.assertNotIn('일부 자료만',text)

if __name__=='__main__':unittest.main()
