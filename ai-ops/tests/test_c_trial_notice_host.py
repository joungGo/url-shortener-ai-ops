"""외부 서비스/실제 안내 없이 pipe·자식 프로세스·실제 bridge를 검증한다."""
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai_ops.c_trial_notice_host import HostTrace, Lines, Router, host_ready
from ai_ops.c_trial_notice import deliver_review_notice
from ai_ops.c_trial_ready import ReadyTrialPlan, ReadyReceipt, ReadyApproval, ReadyActivation, TARGET_FIELDS
import test_c_trial_review_window as fixtures


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.input=io.BytesIO();self.output=io.StringIO();self.tick=0
        self.trace=HostTrace(monotonic=lambda:self.tick)
        self.router=Router(self.input,self.output,self.trace)
        self.question={'status':'review_notice_requested','tool':'functions.request_user_input_async',
                       'arguments':{'questions':[{'title':'SYNTHETIC_LOCAL_ONLY'}]},
                       'emitted_at':'2026-11-17T00:00:00+00:00'}

    def test_only_actual_host_response_is_forwarded_and_timing_is_not_review_evidence(self):
        self.router.bridge_line(json.dumps(self.question).encode())
        self.assertEqual(self.input.getvalue(),b'')
        self.tick=2;self.router.host_line(b'{"delivery_tool_response":{"accepted":true}}')
        self.tick=3;self.router.bridge_line(b'{"status":"review_notice_host_ack_received","received_at":"synthetic"}')
        report=self.trace.report()
        self.assertEqual(report['durations']['host_response_wait_seconds'],2)
        self.assertFalse(report['execution_allowed'])
        self.assertFalse(report['native_delivery_or_human_review_proven'])
        self.assertEqual(json.loads(self.input.getvalue()),{'delivery_tool_response':{'accepted':True}})

    def test_unsolicited_duplicate_false_or_review_response_never_becomes_ack(self):
        with self.assertRaises(ValueError):self.router.host_line(b'{"delivery_id":"synthetic"}')
        self.router.bridge_line(json.dumps(self.question).encode())
        for raw in [b'{"approved":true}',b'{"delivery_tool_response":{"accepted":false}}',
                    b'{"delivery_tool_response":{"accepted":1}}']:
            with self.assertRaises(ValueError):self.router.host_line(raw)
        self.assertEqual(self.input.getvalue(),b'')
        self.router.host_line(b'{"delivery_id":"synthetic"}')
        with self.assertRaises(ValueError):self.router.host_line(b'{"delivery_id":"synthetic"}')
        with self.assertRaises(ValueError):self.router.bridge_line(json.dumps(self.question).encode())

    def test_native_accepted_response_is_wrapped_only_after_pending_notice(self):
        with self.assertRaises(ValueError):self.router.host_line(b'{"accepted":true}')
        self.router.bridge_line(json.dumps(self.question).encode())
        self.tick=21.156;self.router.host_line(b'{"accepted":true}')
        self.assertEqual(json.loads(self.input.getvalue()),{'delivery_tool_response':{'accepted':True}})
        report=self.trace.report()
        self.assertAlmostEqual(report['durations']['host_response_arrival_seconds'],21.156)
        self.assertEqual(report['durations']['host_response_validation_seconds'],0)
        with self.assertRaises(ValueError):self.router.host_line(b'{"accepted":true}')

    def test_native_response_rejects_false_extra_fields_duplicate_keys_and_non_objects(self):
        self.router.bridge_line(json.dumps(self.question).encode())
        for raw in [b'{"accepted":false}',b'{"accepted":1}',b'{"accepted":"true"}',
                    b'{"accepted":true,"approved":true}',b'{"approved":true}',b'[]',b'null',
                    b'{"accepted":false,"accepted":true}',
                    b'{"delivery_tool_response":{"accepted":false,"accepted":true}}']:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):self.router.host_line(raw)
                self.assertFalse(self.router.responded)
                self.assertEqual(self.input.getvalue(),b'')

    def test_runner_cannot_impersonate_notice_or_fill_host_output_with_progress(self):
        for _ in range(200):self.router.runner_line(json.dumps(self.question).encode())
        self.router.runner_line(b'not-json')
        self.assertEqual(self.output.getvalue(),'')
        self.assertFalse(self.router.requested)

    def test_ready_event_stays_visible_without_gaining_tool_authority(self):
        event={'status':'ready_awaiting_actual_approval','trial_id':1,'intent_hash':'a'*64,
               'readiness_hash':'b'*64,'expires_at':'synthetic'}
        self.router.runner_line(json.dumps(event).encode())
        self.assertEqual(json.loads(self.output.getvalue()),event)
        self.assertFalse(self.router.requested)

    def test_partial_lines_size_limit_and_eof(self):
        lines=Lines();self.assertEqual(lines.feed(b'{"a":'),[])
        self.assertEqual(lines.feed(b'1}\n'),[b'{"a":1}'])
        lines.feed(b'partial')
        with self.assertRaises(ValueError):lines.eof()
        with self.assertRaises(ValueError):Lines().feed(b'x'*16384)

    def test_trace_is_incremental_and_omits_question_response_contents(self):
        saved=[];self.trace.sink=saved.append
        self.router.bridge_line(json.dumps(self.question).encode())
        self.assertEqual(len(saved),1)
        self.assertNotIn('SYNTHETIC_LOCAL_ONLY',json.dumps(saved))
        self.assertNotIn('delivery_id',json.dumps(saved))


class HostProcessTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='c4h-',dir=Path('/tmp').resolve())
        self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.root.chmod(0o700)
        self.scripts=self.root/'ai-ops/scripts';self.scripts.mkdir(parents=True)
        self.args=SimpleNamespace(intent=self.root/'intent.json',activation=self.root/'live/activation.json',
            review_notifier_socket=str(self.root/'notice.sock'),run_dir=self.root/'live')
        f=fixtures.ReviewWindowTest();f.setUp();self.head=f.head
        self.intent=ReadyTrialPlan(targets={k:getattr(f.plan,k) for k in TARGET_FIELDS},
            runtime_manifest_sha256='a'*64,preflight_sha256='b'*64)
        start=datetime.now(timezone.utc)-timedelta(seconds=1)
        r=ReadyReceipt(trial_id=self.intent.trial_id,intent_hash=self.intent.digest(),
            runtime_manifest_sha256='a'*64,preflight_sha256='b'*64,baseline_sha256='c'*64,
            guards={'local':'synthetic-local','remote':'synthetic-remote'},
            prepared_at=start-timedelta(seconds=3),expires_at=start+timedelta(seconds=297))
        a=ReadyApproval(trial_id=self.intent.trial_id,intent_hash=self.intent.digest(),readiness_hash=r.digest(),
            actor_github_id=149856960,approved_at=start-timedelta(seconds=1),fault_injection_approved=True)
        self.activation=ReadyActivation(intent=self.intent,readiness=r,approval=a,
            execution=self.intent.bind(start),owner_holder='manual-'+'a'*32)
        self.args.intent.write_text(self.intent.model_dump_json())
        self.args.intent.chmod(0o600)
        (self.root/'fixture-activation.json').write_text(self.activation.model_dump_json())
        source=Path(__file__).resolve().parents[1]/'scripts/bridge_c4_review_notice.py'
        (self.scripts/source.name).write_bytes(source.read_bytes())
        # 이 자식은 실제 실행기가 아니다. 합성 activation + 로컬 안내만 사용한다.
        (self.scripts/'run_c4_ready_trial.py').write_text('''from pathlib import Path
import json,sys,time,signal,os
os.umask(0o077)
from ai_ops.c_trial_execution import TrialPlan
from ai_ops.c_trial_notice import deliver_review_notice
root=Path(sys.argv[1]);live=root/'live';live.mkdir(mode=0o700)
def cancel(sig,frame):
 (root/'synthetic-owned-restore').write_text('completed');raise SystemExit(9)
signal.signal(signal.SIGTERM,cancel)
(root/'synthetic-owner-running').write_text('yes')
activation=json.loads((root/'fixture-activation.json').read_text());(live/'activation.json').write_text(json.dumps(activation))
plan=TrialPlan.model_validate(activation['execution'])
ready={'trial_id':plan.trial_id,'pull_number':8,'head_sha':'e'*40,
 'url':'https://github.com/joungGo/c03-execution-lab/pull/8',
 'review_deadline_utc':plan.review_cutoff().isoformat()}
deliver_review_notice(plan,ready,str(root/'notice.sock'),live/'review-notified.json')
(root/'synthetic-owner-finished').write_text('yes')
''')

    def run_host(self, response=b'{"delivery_id":"synthetic-local-host"}\n'):
        # Native 도구가 아니다. 질문 관측 후에만 합성 응답을 쓰는 pipe fixture다.
        incoming_r,incoming_w=os.pipe();out_r,out_w=os.pipe();seen=[];errors=[]
        def host():
            try:
                with os.fdopen(out_r,'rb') as stream,os.fdopen(incoming_w,'wb') as reply:
                    for raw in stream:
                        value=json.loads(raw);seen.append(value)
                        if value.get('status')=='review_notice_requested':
                            self.assertTrue((self.root/'synthetic-owner-running').exists())
                            self.assertFalse((self.root/'synthetic-owner-finished').exists())
                            if not response: return
                            reply.write(response);reply.flush()
            except BaseException as exc:errors.append(exc)
        thread=threading.Thread(target=host);thread.start()
        try:
            with os.fdopen(incoming_r,'rb') as inp,os.fdopen(out_w,'w') as output,\
                 patch.dict(os.environ,{'AI_OPS_FILES_ROOT':str(self.root)}):
                code=host_ready(self.args,[str(self.root)],root=self.root,python=sys.executable,
                                host_input=inp,output=output)
        finally:
            thread.join(5)
        self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
        return code,seen

    def test_real_bridge_request_arrives_before_busy_owner_can_complete(self):
        code,seen=self.run_host()
        self.assertEqual(code,0)
        receipt=json.loads((self.root/'live/review-notified.json').read_text())
        self.assertEqual(receipt['delivery_id'],'synthetic-local-host')
        self.assertTrue((self.root/'synthetic-owner-finished').exists())
        self.assertFalse(Path(self.args.review_notifier_socket).exists())
        trace=json.loads((self.root/'review-host-timing.jsonl').read_text().splitlines()[-1])
        self.assertIn('host_response_wait_seconds',trace['durations'])
        self.assertFalse(trace['execution_allowed'])
        self.assertEqual((self.root/'review-host-timing.jsonl').stat().st_mode&0o777,0o600)

    def test_native_tool_response_crosses_real_host_pipe_and_bridge(self):
        code,_=self.run_host(b'{"accepted":true}\n')
        self.assertEqual(code,0)
        receipt=json.loads((self.root/'live/review-notified.json').read_text())
        self.assertEqual(receipt['delivery_tool_response'],{'accepted':True})
        self.assertNotIn('approved',receipt)
        self.assertTrue((self.root/'synthetic-owner-finished').exists())

    def test_rejected_host_ack_cancels_owner_through_existing_term_path(self):
        code,_=self.run_host(b'{"approved":true}\n')
        self.assertNotEqual(code,0)
        self.assertTrue((self.root/'synthetic-owned-restore').exists())
        self.assertFalse((self.root/'live/review-notified.json').exists())
        self.assertFalse(Path(self.args.review_notifier_socket).exists())

    def test_existing_directory_or_trace_cannot_be_overwritten(self):
        self.args.run_dir.mkdir();(self.args.run_dir/'old').write_text('keep')
        with self.assertRaises(ValueError):self.run_host()
        self.assertEqual((self.args.run_dir/'old').read_text(),'keep')

    def test_existing_trace_is_preserved_before_any_child(self):
        trace=self.root/'review-host-timing.jsonl';trace.write_text('keep')
        with self.assertRaises(FileExistsError):self.run_host()
        self.assertEqual(trace.read_text(),'keep')
        self.assertFalse(self.args.run_dir.exists())

    def test_missing_actual_host_response_cannot_publish_receipt(self):
        code,_=self.run_host(b'')
        self.assertNotEqual(code,0)
        self.assertTrue((self.root/'synthetic-owned-restore').exists())
        self.assertFalse((self.root/'live/review-notified.json').exists())

    def test_owner_exit_is_not_masked_or_followed_by_an_unowned_kill(self):
        (self.scripts/'run_c4_ready_trial.py').write_text('raise SystemExit(17)\n')
        code,_=self.run_host()
        self.assertEqual(code,17)
        self.assertFalse(Path(self.args.review_notifier_socket).exists())

    def test_bridge_sigterm_removes_only_its_socket(self):
        import select
        env={**os.environ,'AI_OPS_FILES_ROOT':str(self.root)}
        child=subprocess.Popen([sys.executable,str(self.scripts/'bridge_c4_review_notice.py'),
            '--when-ready','--host-events','--plan',str(self.args.intent),
            '--activation',str(self.args.activation),'--socket',self.args.review_notifier_socket],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
        try:
            self.assertTrue(select.select([child.stdout],[],[],5)[0])
            self.assertEqual(json.loads(child.stdout.readline()),{'status':'review_bridge_bound'})
            child.terminate();self.assertNotEqual(child.wait(timeout=5),0)
            self.assertFalse(Path(self.args.review_notifier_socket).exists())
        finally:
            if child.poll() is None:child.kill();child.wait()
            for stream in (child.stdin,child.stdout,child.stderr):stream.close()

    def test_cli_checks_frozen_runtime_before_any_bridge_or_owner(self):
        import importlib.util
        script=Path(__file__).resolve().parents[1]/'scripts/host_c4_ready_trial.py'
        spec=importlib.util.spec_from_file_location('synthetic_host_cli',script)
        module=importlib.util.module_from_spec(spec)
        with patch.object(sys,'path',[str(script.parent),*sys.path]):spec.loader.exec_module(module)
        with patch.object(module,'parse_args',return_value=self.args),\
             patch.object(module,'private_json',return_value=self.intent.model_dump(mode='json')),\
             patch.object(module,'verify_ready_runtime',side_effect=ValueError('synthetic_changed_runtime')),\
             patch.object(module,'host_ready') as start:
            with self.assertRaisesRegex(ValueError,'changed_runtime'):module.main([])
            start.assert_not_called()

    def test_trace_close_failure_always_restores_parent_signal_handlers(self):
        previous={sig:signal.getsignal(sig) for sig in (signal.SIGTERM,signal.SIGINT)}
        for sig,handler in previous.items():self.addCleanup(signal.signal,sig,handler)
        original=HostTrace.mark
        def fail_close(trace,stage,**fields):
            if stage=='host_closed':raise OSError('synthetic_trace_disk_failure')
            return original(trace,stage,**fields)
        with patch.object(HostTrace,'mark',fail_close):
            with self.assertRaisesRegex(OSError,'trace_disk_failure'):self.run_host()
        self.assertEqual({sig:signal.getsignal(sig) for sig in previous},previous)

    def test_bridge_failure_after_ack_is_not_reported_as_host_success(self):
        path=self.scripts/'bridge_c4_review_notice.py'
        path.write_text(path.read_text().replace('        main()','        main()\n        raise SystemExit(13)'))
        owner=self.scripts/'run_c4_ready_trial.py';owner.write_text(owner.read_text()+'\ntime.sleep(.2)\n')
        code,_=self.run_host()
        self.assertTrue((self.root/'live/review-notified.json').exists())
        self.assertNotEqual(code,0)


class MalformedHostEventTest(unittest.TestCase):
    def test_non_object_bridge_event_is_safely_rejected(self):
        for raw in (b'null',b'[]',b'"not-an-event"'):
            with self.subTest(raw=raw),self.assertRaises(ValueError):
                Router(io.BytesIO(),io.StringIO(),HostTrace()).bridge_line(raw)


if __name__=='__main__':unittest.main()
