"""Local proof of unchanged read barriers, bounded timing, and cold Git duplication."""
from collections import Counter
from concurrent.futures import Future
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_ops.c_approval import ApprovalRejected, prepare_pr_link
from ai_ops.c_recovery import RecoveryProposal, ISOLATED_REPOSITORY
from ai_ops.c_recovery_read_timing import proposal_read_timing, phase, read_timeout
from ai_ops.c_trial_read_cache import ImmutableGitReader
from ai_ops.contracts import sha256_json
import test_c_execution_source as sources
import test_c_recovery_proof as proofs
import test_c_approval as approvals


class ProposalLatencyTest(unittest.TestCase):
    def setUp(self):
        self.f = proofs.RecoveryProofTest(); self.f.setUp()
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.cluster = {'arn': self.f.binding.cluster_arn, 'status':'ACTIVE',
                        'endpoint':'https://synthetic.invalid','certificateAuthority':{'data':'synthetic-ca'}}
        fingerprint = sha256_json({'arn':self.cluster['arn'],'endpoint':self.cluster['endpoint'],'ca':'synthetic-ca'})
        self.binding = self.f.binding.model_copy(update={'cluster_fingerprint':fingerprint})
        self.history = proofs.VerifiedNormalHistory(self.f.history.run_id,self.f.history.run_attempt,
            self.f.history.artifact_id,self.f.history.archive_digest,self.f.history.source_revision,
            self.f.history.receipt.model_copy(update={'binding_hash':sha256_json(self.binding.model_dump(mode='json'))}))
        path=Path(self.temp.name)/'binding.json'; path.write_text(self.binding.model_dump_json())
        self.args=SimpleNamespace(kubeconfig='/tmp/synthetic-only',normal_binding=path,
            normal_run_id=12345,normal_head='a'*40,operator_state_root=None)
        self.calls=Counter(); self.lock=threading.Lock(); self.delay=0.; self.barrier=None
        self.change=None

    def count(self,key):
        with self.lock:self.calls[key]+=1; return self.calls[key]

    def command(self,*args):
        if args[0]=='aws':
            self.count(args[1]);return json.dumps({'cluster':self.cluster} if args[1]=='eks' else
                {'Account':'wrong' if self.change=='account' else '764428911194'})
        if 'config' in args:
            return json.dumps({'clusters':[{'cluster':{'server':self.cluster['endpoint'],
                'certificate-authority-data':'synthetic-ca'}}]})
        kind=args[6]; n=self.count(kind)
        data={'application':self.f.app,'deployment':self.f.deployment,'lease':self.f.lease,
              'pods':{'items':self.f.pods},'replicasets':{'items':[self.f.rs]}}[kind]
        if kind=='replicasets':time.sleep(self.delay)
        data=deepcopy(data)
        if self.change==kind and n%2==0:data['metadata']['uid']='replacement'
        return json.dumps(data)

    def history_read(self,*args):
        self.count('history')
        if self.barrier:self.barrier.wait(timeout=2)
        time.sleep(self.delay)
        if self.change=='history':raise ValueError('synthetic-invalid-history')
        return self.history

    def ecr(self,args,**kwargs):
        self.assertEqual(args[:3],('aws','ecr','describe-images'))
        good=self.f.binding.app.image.split('@')[1] in args[-3]
        self.count('normal_image' if good else 'failure_image')
        if good and self.barrier:self.barrier.wait(timeout=2)
        time.sleep(self.delay)
        if self.change=='ecr':raise TimeoutError('synthetic-secret-error')
        return SimpleNamespace(returncode=0 if good else 1,stdout=json.dumps(self.f.ecr),
                               stderr='' if good else 'ImageNotFoundException')

    def collect(self,serial=False):
        original=sources.audit.submit
        def submit(pool,fn,*args):
            # Reproduce the former identity -> images ordering only, same leaf reads.
            if serial and getattr(fn,'__name__','') in ('identity_history','images_pods'):
                f=Future()
                try:f.set_result(fn(*args))
                except BaseException as e:f.set_exception(e)
                return f
            return original(pool,fn,*args)
        with patch.object(sources.audit,'_run',side_effect=self.command), \
             patch.object(sources.audit,'verify_normal_ci_history',side_effect=self.history_read), \
             patch.object(sources.audit.subprocess,'run',side_effect=self.ecr), \
             patch.object(sources.audit,'submit',side_effect=submit):
            return sources.audit._collect(self.args,include_snapshot=True,read_values=lambda _:self.f.values)

    def test_independent_lanes_overlap_but_all_three_fresh_samples_and_final_reads_remain(self):
        self.barrier=threading.Barrier(2)
        for _ in range(3):self.assertTrue(self.collect()[0]['proof_ready'])
        for key in ['history','eks','sts','normal_image','failure_image','pods','replicasets']:
            self.assertEqual(self.calls[key],3,key)
        for key in ['application','deployment','lease']:self.assertEqual(self.calls[key],6,key)

    def test_changed_identity_resources_or_failed_history_ecr_still_reject(self):
        for kind in ['account','application','deployment','lease','history','ecr']:
            with self.subTest(kind=kind):
                self.change=kind;self.calls.clear()
                with self.assertRaises((ValueError,TimeoutError)):self.collect()

    def test_local_delayed_reads_are_faster_without_removing_checks(self):
        self.delay=.10
        reports={};results={};counts={}
        now=datetime.now(timezone.utc)
        plan=SimpleNamespace(minimum_review_seconds=120,review_cutoff=lambda:now+timedelta(seconds=210))
        for serial,name in [(True,'serial'),(False,'parallel')]:
            self.calls.clear();start=time.monotonic()
            with proposal_read_timing(plan,lambda report:reports.update({name:report}),clock=lambda:datetime.now(timezone.utc)):
                for stage in ['proposal.first','proposal.second','proposal.third']:
                    with phase(stage,read=True):self.assertTrue(self.collect(serial=serial)[0]['proof_ready'])
            results[name]=round((time.monotonic()-start)*1000,3)
            counts[name]=deepcopy(self.calls)
        self.assertEqual(counts['serial'],counts['parallel'])
        self.assertEqual(counts['parallel']['history'],3)
        self.latency_report={'injected_leaf_delay_ms':100,'milliseconds':results,
            'read_counts':dict(counts['parallel']),'timings':reports}

    def test_timing_uses_review_notice_deadline_and_restores_context_on_error_cancel(self):
        now=datetime(2026,11,18,tzinfo=timezone.utc); mono=[0.]; wall=[now]
        plan=SimpleNamespace(minimum_review_seconds=120,review_cutoff=lambda:now+timedelta(seconds=210))
        for exception in [None, ValueError('private-credential'), KeyboardInterrupt()]:
            reports=[];mono[0]=0.;wall[0]=now
            try:
                with proposal_read_timing(plan,reports.append,clock=lambda:wall[0],monotonic=lambda:mono[0]):
                    self.assertEqual(read_timeout(180),57)
                    with phase('proposal.first',read=True):
                        if exception is not None:raise exception
                        wall[0]-=timedelta(seconds=20);mono[0]=91
            except (ApprovalRejected, ValueError, KeyboardInterrupt):pass
            else:self.fail('must stop')
            self.assertEqual(len(reports),1)
            self.assertNotIn('private-credential',json.dumps(reports))
            self.assertEqual(reports[0]['events'][0]['status'],'error')
            self.assertEqual(read_timeout(180),180)

    def test_expired_phase_does_not_start_next_sample_or_writer(self):
        now=datetime(2026,11,18,tzinfo=timezone.utc);mono=[0.];calls=[];reports=[]
        plan=SimpleNamespace(minimum_review_seconds=120,review_cutoff=lambda:now+timedelta(seconds=154))
        with self.assertRaisesRegex(ApprovalRejected,'^recovery_read_deadline_exceeded$'):
            with proposal_read_timing(plan,reports.append,clock=lambda:now,monotonic=lambda:mono[0]):
                with phase('proposal.first',read=True):calls.append('first');mono[0]=2.
                calls.append('second-or-writer')
        self.assertEqual(calls,['first']);self.assertTrue(reports)

    def test_link_reuses_only_sha_objects_with_new_reader_and_rechecks_main_twice(self):
        f=approvals.CApprovalTest();f.setUp();self.addCleanup(f.doCleanups)
        local,_,proposal,prior,refreshed,main=f.setup_link(approvals.CURRENT)
        body=proposal.model_dump(mode='json',exclude={'proposal_hash'})
        from ai_ops.c_recovery_proof import proposal_identity
        for proof in (prior,refreshed):
            proof['source_repository']=ISOLATED_REPOSITORY
            proof['proposal_readiness_hash']=sha256_json({**proposal_identity(proof),
                'proof_checked_at':proof['proof_checked_at']})
        body['repository']=ISOLATED_REPOSITORY
        body['recheck_hash']=prior['proposal_readiness_hash']
        proposal=RecoveryProposal.model_validate({**body,'proposal_hash':sha256_json(body)})
        # Local Git adapter has the original repository prefix; only the API adapter maps it.
        class Reader:
            def __init__(self):self.deadline=time.monotonic()+45;self.calls=[];self.change=False
            def get(self,path):
                self.calls.append(path)
                time.sleep(.01)
                result=local.get(path.replace('repos/'+ISOLATED_REPOSITORY,approvals.REPO_API))
                if self.change and '/git/ref/' in path and sum('/git/ref/' in p for p in self.calls)>1:
                    result['object']['sha']='f'*40
                return result
        original=Reader();start=time.monotonic()
        cold_decision,cold_link=prepare_pr_link(proposal,prior,refreshed,reader=original,clock=lambda:approvals.NOW)
        cold_ms=(time.monotonic()-start)*1000
        cold=Reader();cached=ImmutableGitReader(cold)
        sources.audit._github_values(proposal.base_sha,reader=cached,repository=ISOLATED_REPOSITORY)
        warm_objects=set(cached.cache);self.assertGreater(len(warm_objects),0)
        cold.deadline=0.;fresh=Reader();reader=cached.renew(fresh)
        start=time.monotonic()
        decision,link=prepare_pr_link(proposal,prior,refreshed,reader=reader,clock=lambda:approvals.NOW)
        warm_ms=(time.monotonic()-start)*1000
        self.assertEqual(link,cold_link)
        self.assertEqual(decision,cold_decision)
        self.assertLess(len(fresh.calls),len(original.calls))
        self.link_metrics={'injected_get_delay_ms':10,'cold_gets':len(original.calls),
            'reused_gets':len(fresh.calls),'main_gets':sum('/git/ref/' in x for x in fresh.calls),
            'cold_ms':round(cold_ms,3),'reused_ms':round(warm_ms,3)}
        self.assertEqual(decision['status'],'pr_candidate');self.assertEqual(link.pr_base_sha,main)
        self.assertFalse(warm_objects.intersection(fresh.calls))
        self.assertEqual(sum('/git/ref/' in p for p in fresh.calls),2)
        newer=Reader();newer.change=True
        with self.assertRaisesRegex(ApprovalRejected,'^base_sha_changed$'):
            prepare_pr_link(proposal,prior,refreshed,reader=reader.renew(newer),clock=lambda:approvals.NOW)
        self.assertEqual(sum('/git/ref/' in p for p in newer.calls),2)
        newer.deadline=0.
        with self.assertRaisesRegex(ApprovalRejected,'^github_read_timeout$'):
            reader.renew(newer).get(next(iter(warm_objects)))

    def test_actual_script_persists_failed_stage_before_any_later_sample(self):
        import ast
        from unittest.mock import Mock
        script=Path(__file__).resolve().parents[1]/'scripts/prepare_c4_recovery_pr.py'
        tree=ast.parse(script.read_text())
        block=next(n for n in ast.walk(tree) if isinstance(n,ast.With) and
            any(isinstance(i.context_expr,ast.Call) and isinstance(i.context_expr.func,ast.Name)
                and i.context_expr.func.id=='proposal_read_timing' for i in n.items))
        code=compile(ast.fix_missing_locations(ast.Module(body=[block],type_ignores=[])),str(script),'exec')
        now=datetime.now(timezone.utc)
        plan=SimpleNamespace(minimum_review_seconds=120,review_cutoff=lambda:now+timedelta(seconds=210))
        source=Mock();source.collect.side_effect=ValueError('secret-source-response')
        saved={}
        with self.assertRaisesRegex(ValueError,'secret-source-response'):
            exec(code,{'proposal_read_timing':proposal_read_timing,'plan':plan,'phase':phase,
                'datetime':datetime,'timezone':timezone,'source':source,
                'save':lambda name,value:saved.update({name:value})})
        self.assertEqual(source.collect.call_count,1)
        self.assertEqual(set(saved),{'proposal-timing.json'})
        self.assertIn('proposal.first',[e['stage'] for e in saved['proposal-timing.json']['events']])
        self.assertNotIn('secret-source-response',json.dumps(saved))

    def test_recorded_50_second_recheck_cannot_consume_minimum_review_window(self):
        now=datetime(2026,11,18,10,8,52,593312,tzinfo=timezone.utc)
        cutoff=datetime(2026,11,18,10,11,42,870338,tzinfo=timezone.utc)
        mono=[0.];reports=[]
        plan=SimpleNamespace(minimum_review_seconds=120,review_cutoff=lambda:cutoff)
        with self.assertRaisesRegex(ApprovalRejected,'^recovery_read_deadline_exceeded$'):
            with proposal_read_timing(plan,reports.append,clock=lambda:now,monotonic=lambda:mono[0]):
                self.assertAlmostEqual(read_timeout(180),17.277026)
                with phase('proposal.link',read=True):mono[0]=50.740117
        self.assertEqual(reports[0]['events'][0]['status'],'error')

    def test_real_local_github_process_is_clipped_to_remaining_review_budget(self):
        import os,sys
        from ai_ops.c_approval import ApprovalGitHubReader
        executable=Path(self.temp.name)/'gh'
        executable.write_text('#!'+sys.executable+'\nimport time; time.sleep(2)\n')
        executable.chmod(0o700)
        now=datetime.now(timezone.utc);reports=[]
        plan=SimpleNamespace(minimum_review_seconds=120,review_cutoff=lambda:now+timedelta(seconds=153.2))
        start=time.monotonic()
        with patch.dict(os.environ,{'PATH':self.temp.name}):
            with self.assertRaises(ApprovalRejected):
                with proposal_read_timing(plan,reports.append,clock=lambda:datetime.now(timezone.utc)):
                    with phase('proposal.link',read=True):ApprovalGitHubReader().get(approvals.MAIN)
        self.assertLess(time.monotonic()-start,1.)
        self.assertEqual(reports[0]['events'][0]['status'],'error')


if __name__=='__main__':unittest.main()
