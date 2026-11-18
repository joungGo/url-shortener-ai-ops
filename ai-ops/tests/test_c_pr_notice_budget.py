"""006의 실제 시간 예산 재현. 외부 호출은 모두 로컬 대역으로 검증한다."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_ops.c_approval import ApprovalRejected
from ai_ops.c_recovery_read_timing import (proposal_read_timing, writer_pr_timing,
    RecoveryReadTiming, phase, read_timeout, WRITER_RESERVE_SECONDS, NOTICE_RESERVE_SECONDS)
from ai_ops import c_trial_writer_api as writer
import test_c_trial_writer_path as writer_tests
import test_c_proposal_latency as latency_tests
import test_c_execution_source as sources

NOW = datetime(2026, 11, 18, 11, 0, 45, 701462, tzinfo=timezone.utc)


class NoticeBudgetTest(unittest.TestCase):
    def plan(self, seconds=169.808440):
        return SimpleNamespace(minimum_review_seconds=120,
                               review_cutoff=lambda:NOW+timedelta(seconds=seconds))

    def test_recorded_006_old_path_misses_notice_and_new_gate_stops_before_writer(self):
        available, reads, writer_seconds = 49.808440, 43.423622, 7.415729
        self.assertLess(available-reads, writer_seconds)
        self.assertAlmostEqual(reads+writer_seconds-available, 1.030911)
        tick=[0.];reports=[];calls=[]
        with self.assertRaises(ApprovalRejected):
            with proposal_read_timing(self.plan(), reports.append, clock=lambda:NOW,
                                      monotonic=lambda:tick[0]):
                self.assertAlmostEqual(read_timeout(99), available-WRITER_RESERVE_SECONDS-NOTICE_RESERVE_SECONDS)
                for name,seconds in [('first',13.192296),('second',13.150268),('third',13.884172)]:
                    with phase('proposal.'+name,read=True):
                        calls.append(name);tick[0]+=seconds
                calls.append('writer')
        self.assertNotIn('writer',calls)
        self.assertEqual(reports[0]['events'][-1]['status'],'error')

    def test_sufficient_path_preserves_writer_notice_and_full_review_window(self):
        tick=[0.];reports=[];plan=self.plan()
        with proposal_read_timing(plan,reports.append,clock=lambda:NOW+timedelta(seconds=tick[0]),monotonic=lambda:tick[0]):
            # Shorter synthetic collection, not a claim of live completion.
            for name in ['first','second','third']:
                with phase('proposal.'+name,read=True):tick[0]+=4.
            with phase('proposal.link',read=True):tick[0]+=2.
        with writer_pr_timing(plan,reports.append,clock=lambda:NOW+timedelta(seconds=tick[0]),monotonic=lambda:tick[0]):
            with phase('writer.pull',read=True):tick[0]+=7.415729
        self.assertGreaterEqual(49.808440-tick[0],NOTICE_RESERVE_SECONDS)
        tick[0]+=NOTICE_RESERVE_SECONDS
        self.assertGreaterEqual((plan.review_cutoff()-NOW).total_seconds()-tick[0],120)

    def test_recorded_007_roundtrip_cannot_fit_old_notice_slack(self):
        available=49.768043  # from fresh proposal start to minimum-review notice cutoff
        reads=37.098700;writer_seconds=5.843562;tool_roundtrip=21.156398
        self.assertAlmostEqual(reads+writer_seconds+tool_roundtrip-available,14.330617)
        self.assertGreater(NOTICE_RESERVE_SECONDS,tool_roundtrip)
        tick=[0.];calls=[]
        with self.assertRaises(ApprovalRejected):
            with proposal_read_timing(self.plan(seconds=120+available),lambda _:None,
                    clock=lambda:NOW,monotonic=lambda:tick[0]):
                with phase('proposal.first',read=True):tick[0]+=reads
                calls.append('writer')
        self.assertEqual(calls,[])

    def test_late_start_refuses_writer_even_though_original_notice_cutoff_not_reached(self):
        reports=[];entered=[]
        with self.assertRaisesRegex(ValueError,'writer_reserve_exhausted'):
            with writer_pr_timing(self.plan(seconds=152.9),reports.append,clock=lambda:NOW):
                entered.append(True)
        self.assertFalse(entered)
        self.assertEqual(reports[0]['events'][0]['status'],'error')

    def test_clock_rollback_does_not_extend_writer_budget_or_leak_errors(self):
        tick=[0.];wall=[NOW];reports=[]
        with self.assertRaises(ApprovalRejected):
            with writer_pr_timing(self.plan(seconds=153),reports.append,clock=lambda:wall[0],monotonic=lambda:tick[0]):
                with phase('writer.pull',read=True):
                    wall[0]-=timedelta(seconds=30);tick[0]=8.
        self.assertEqual(read_timeout(12),12)
        self.assertNotIn('token',json.dumps(reports))

    def test_clock_rollback_between_proposal_and_writer_cannot_reopen_reserve(self):
        tick=[0.];reports=[];wall=[NOW]
        with proposal_read_timing(self.plan(), reports.append,clock=lambda:wall[0],monotonic=lambda:tick[0]) as trace:
            tick[0]=15.
        tick[0]=18.;wall[0]-=timedelta(seconds=30)
        with self.assertRaisesRegex(ValueError,'writer_reserve_exhausted'):
            with writer_pr_timing(self.plan(),reports.append,clock=lambda:wall[0],monotonic=lambda:tick[0],
                                  read_deadline=trace.end+WRITER_RESERVE_SECONDS):
                self.fail('must not reopen time')

    def test_token_returning_after_deadline_is_still_revoked(self):
        tick=[0.];calls=[];reports=[]
        def api(method,path,credential,payload=None):
            calls.append((method,path,read_timeout(12)))
            if method=='POST':
                tick[0]=9.
                return {'token':'synthetic-secret','permissions':writer.PERMISSIONS}
            return {}
        valid=SimpleNamespace(diagnostic={'status':'synthetic-valid'},require_current=lambda:None)
        with patch.object(writer,'api',side_effect=api),patch.object(writer,'validate_token_lifetime',return_value=valid):
            with self.assertRaises(ApprovalRejected):
                with writer_pr_timing(self.plan(seconds=153),reports.append,clock=lambda:NOW,monotonic=lambda:tick[0]):
                    with writer.installation_token('synthetic-jwt',report=lambda _:None):
                        with phase('writer.scope_base',read=True):self.fail('expired')
        self.assertEqual(calls[-1],('DELETE','/installation/token',12))
        self.assertNotIn('synthetic-secret',json.dumps(reports))

    def test_http_timeout_uses_remaining_budget_and_cleanup_is_not_blocked(self):
        trace=RecoveryReadTiming(NOW+timedelta(seconds=2),clock=lambda:NOW)
        class Response:
            status=200
            def read(self,limit):return b'{}'
        class Connection:
            def __init__(self,*args,**kwargs):self.timeout=kwargs['timeout'];timeouts.append(self.timeout)
            def request(self,*args,**kwargs):pass
            def getresponse(self):return Response()
            def close(self):closed.append(True)
        timeouts=[];closed=[]
        with patch.object(writer.http.client,'HTTPSConnection',Connection),trace.activate():
            writer.api('GET','/app','synthetic')
        self.assertTrue(0<timeouts[0]<=2);self.assertEqual(closed,[True])

    def test_writer_parallel_get_barriers_and_repository_validation(self):
        barriers={name:threading.Barrier(count,timeout=2) for name,count in [('identity',2),('scope',3),('commit',2)]}
        def hook(method,path):
            group=None
            if method=='GET':
                if path=='/app' or path.startswith('/app/installations/'):group='identity'
                elif path.startswith('/installation/repositories') or '/git/ref/heads/main' in path or '/contents/' in path:group='scope'
                elif '/commits/' in path or '/compare/' in path:group='commit'
            if group:barriers[group].wait()
        f=writer_tests.WriterPathTest();f.run_path(hook=hook)
        self.assertEqual(f.state['stage'],'pr_created_waiting_notice')
        self.assertEqual(f.calls[-1],('DELETE','/installation/token'))
        for changed in ['app','scope','repositories','values','author']:
            f=writer_tests.WriterPathTest()
            with self.subTest(changed=changed),self.assertRaises(ValueError):f.run_path(changed=changed)
            self.assertFalse(any(m=='POST' and p.endswith('/pulls') for m,p in f.calls))
            if changed in ['repositories','values']:
                self.assertFalse(any(m=='POST' and p.endswith('/git/refs') for m,p in f.calls))

    def test_cancel_in_writer_still_revokes_and_persists_timing(self):
        f=writer_tests.WriterPathTest()
        def hook(method,path):
            if method=='PUT':raise KeyboardInterrupt('synthetic-private-cancellation')
        with self.assertRaises(KeyboardInterrupt):f.run_path(hook=hook)
        self.assertEqual(f.calls[-1],('DELETE','/installation/token'))
        self.assertIn('writer-timing.json',f.saved)
        self.assertNotIn('synthetic-private-cancellation',json.dumps(f.saved))

    def audit_fixture(self):
        f=latency_tests.ProposalLatencyTest();f.setUp();self.addCleanup(f.doCleanups);return f

    def test_pod_replicaset_reads_overlap_and_wrong_owner_or_partial_list_is_rejected(self):
        f=self.audit_fixture();barrier=threading.Barrier(2,timeout=2);original=f.command
        def command(*args):
            if len(args)>6 and args[6] in ('pods','replicasets'):barrier.wait()
            return original(*args)
        f.command=command
        self.assertTrue(f.collect()[0]['proof_ready'])
        for mode in ['uid','name','duplicate','truncated-pods','truncated-sets']:
            def changed(*args):
                value=json.loads(original(*args))
                kind=args[6] if len(args)>6 else ''
                if kind=='replicasets':
                    if mode in ('uid','name'):value['items'][0]['metadata'][mode]='foreign'
                    if mode=='duplicate':value['items'].append(deepcopy(value['items'][0]))
                    if mode=='truncated-sets':value['metadata']={'continue':'cursor'}
                if kind=='pods' and mode=='truncated-pods':value['metadata']={'continue':'cursor'}
                return json.dumps(value)
            f.command=changed
            with self.subTest(mode=mode):
                if mode.startswith('truncated'):
                    with self.assertRaises(ValueError):f.collect()
                else:self.assertFalse(f.collect()[0]['proof_ready'])

    def test_incident_inputs_overlap_but_assessment_waits_for_verified_proof(self):
        f=self.audit_fixture();barrier=threading.Barrier(3,timeout=2)
        f.args.operator_state_root=Path('/tmp/synthetic');f.args.incident_id='synthetic';f.args.incident_run_id='synthetic';f.args.request_id='synthetic'
        history=f.history_read;ecr=f.ecr;command=f.command;done=set()
        def history_read(*args):barrier.wait();value=history(*args);done.add('history');return value
        def image_read(args,**kwargs):
            if f.f.binding.app.image.split('@')[1] in args[-3]:barrier.wait()
            value=ecr(args,**kwargs);done.add('image');return value
        def client(*args):barrier.wait();return {'status':'ok','projection':{}}
        def run(*args):return '149856960' if args[0]=='gh' else command(*args)
        def assess(result,projection,request):
            self.assertTrue(result['proof_ready']);self.assertEqual(done,{'history','image'})
            return result
        f.history_read=history_read;f.ecr=image_read;f.command=run
        with patch.object(sources.audit,'_client',side_effect=client), \
             patch.object(sources.audit.IncidentProjection,'model_validate',return_value=object()), \
             patch.object(sources.audit,'read_existing_request',return_value=SimpleNamespace(requester_github_id=149856960)), \
             patch.object(sources.audit,'assess_incident_request',side_effect=assess) as checked:
            self.assertTrue(f.collect()[0]['proof_ready']);checked.assert_called_once()


if __name__=='__main__':unittest.main()
