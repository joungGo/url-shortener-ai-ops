"""실제 큐·규칙 worker·저장 보고서로 요청부터 로컬 결과 전달까지 검증한다."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ai_ops.contracts import canonical_json_bytes
from ai_ops.job_queue import JobQueue, RETENTION_SECONDS
from ai_ops.task_service import LocalTaskServer, dispatch, request, socket_path
from ai_ops.tasks import DeliveryNotSent, TaskService
import test_job_queue as fixtures


class TaskTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.JobQueueTest(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = TaskService(self.fixture.queue, self.fixture.settings)
        self.raw = {'request_id': 'event-1', 'conversation_id': 'thread-1',
                    'target': self.fixture.target.alias, 'question': '상태 확인 token=private-secret-value'}

    def submit(self):
        return self.service.submit(self.raw)['task_id']

    def complete(self):
        job = self.submit()
        with patch('socket.create_connection', side_effect=AssertionError('외부 통신 금지')), \
                patch.object(self.fixture.queue.engine, 'load_api_key', side_effect=AssertionError('키 조회 금지')):
            self.fixture.worker().run_once()
        return job

    def test_acceptance_is_not_analysis_or_execution_success(self):
        receipt = self.service.submit(self.raw)
        self.assertTrue(receipt['accepted'])
        self.assertFalse(receipt['analysis_completed']); self.assertFalse(receipt['execution_allowed'])
        view = self.service.show(receipt['task_id'])
        self.assertEqual(view['investigation']['state'], 'queued')
        self.assertEqual(view['result']['state'], 'pending')
        self.assertEqual(view['delivery']['state'], 'not_prepared')
        self.assertFalse(view['recovery']['execution_allowed'])

    def test_duplicate_and_changed_question_target_or_conversation(self):
        job = self.submit()
        self.assertTrue(self.service.submit(self.raw)['duplicate'])
        for field, value in [('question', '다른 질문'), ('conversation_id', 'other-thread'),
                             ('target', 'foreign'), ('request_id', '../bad')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.service.submit({**self.raw, field: value})
        with self.fixture.queue.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)
            text = str([dict(row) for row in db.execute('SELECT * FROM task_requests')])
            self.assertNotIn(self.raw['question'], text)
            self.assertNotIn('private-secret-value', text)
        self.assertEqual(self.service.submit(self.raw)['task_id'], job)

    def test_submit_rejects_unknown_fields_modes_approval_and_execution(self):
        for extra in ({'mode': 'ai'}, {'approved': True}, {'action': 'recover'}, {'token': 'private'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.service.submit({**self.raw, **extra})

    def test_resume_after_queue_commit_before_ack_uses_same_job_and_route(self):
        original = self.fixture.queue.submit
        def lost_reply(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('private-internal-error')
        with patch.object(self.fixture.queue, 'submit', side_effect=lost_reply), self.assertRaises(OSError):
            self.submit()
        # 예약이 남아 있으므로 회신 위치를 바꾼 재전송도 거절한다.
        with self.assertRaises(ValueError):
            self.service.submit({**self.raw, 'conversation_id': 'other-thread'})
        reopened = TaskService(JobQueue(self.fixture.store, clock=lambda: self.fixture.time), self.fixture.settings)
        self.assertTrue(reopened.submit(self.raw)['duplicate'])
        with reopened.queue.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)

    def test_expired_unlinked_reservation_never_enqueues_a_new_job(self):
        with patch.object(self.fixture.queue, 'submit', side_effect=OSError('before-queue-commit')):
            with self.assertRaises(OSError): self.submit()
        self.fixture.time += RETENTION_SECONDS
        with self.assertRaises(ValueError): self.submit()
        with self.fixture.queue.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_duplicate_after_report_reports_actual_result_availability(self):
        self.complete()
        receipt = self.service.submit(self.raw)
        self.assertTrue(receipt['duplicate'])
        self.assertTrue(receipt['analysis_completed'])
        self.assertEqual(receipt['result_state'], 'available')
        self.assertEqual(receipt['investigation_state'], 'partial')

    def test_partial_report_result_and_local_delivery_are_separate_and_restart_safe(self):
        job = self.complete()
        result = self.service.result(job)
        self.assertEqual(result['state'], 'available')
        self.assertEqual(result['investigation_state'], 'partial')
        self.assertFalse(result['execution_allowed'])
        notice = self.service.deliver(job)
        self.assertEqual(notice['state'], 'delivered')
        self.assertEqual(notice['attempts'], 1)
        reopened = TaskService(JobQueue(self.fixture.store, clock=lambda: self.fixture.time), self.fixture.settings)
        self.assertEqual(reopened.deliver(job)['attempts'], 1)
        paths = list(self.service.mailbox.glob('notice-*.json'))
        self.assertEqual(len(paths), 1)
        value = json.loads(paths[0].read_text())
        self.assertEqual(value['conversation_id'], 'thread-1')
        self.assertEqual(value['result']['reference'], result['reference'])
        self.assertFalse(value['human_read_confirmed'])
        self.assertNotIn('private-secret-value', paths[0].read_text())
        self.assertEqual(paths[0].stat().st_mode & 0o777, 0o600)

    def test_common_lookup_reads_existing_queue_without_inventing_delivery_route(self):
        job = self.submit()
        with self.assertRaises(ValueError): self.service.deliver(job)
        other, _ = self.fixture.submit(key='outside-task-api')
        self.assertEqual(self.service.show(other)['source'], 'existing_queue')
        self.assertIsNone(self.service.show(other)['conversation_id'])
        with self.assertRaises(ValueError): self.service.deliver(other)

    def test_definite_prepublication_failure_retries_notice_only_at_most_three_times(self):
        job = self.complete()
        with patch.object(self.service, '_publish', side_effect=DeliveryNotSent):
            self.assertEqual(self.service.deliver(job)['state'], 'pending')
            self.assertEqual(self.service.deliver(job)['state'], 'pending')
            self.assertEqual(self.service.deliver(job)['state'], 'failed')
            self.assertEqual(self.service.deliver(job)['attempts'], 3)
        self.assertEqual(self.fixture.queue.show(job)['attempts'], 1)

    def test_notice_retry_does_not_reexecute_investigation(self):
        job = self.complete()
        with patch.object(self.service, '_publish', side_effect=DeliveryNotSent):
            self.assertEqual(self.service.deliver(job)['state'], 'pending')
        self.assertEqual(self.service.deliver(job)['state'], 'delivered')
        self.assertEqual(self.fixture.queue.show(job)['attempts'], 1)

    def test_cancelled_and_review_held_jobs_do_not_claim_analysis_success(self):
        job = self.submit(); self.fixture.queue.cancel(job)
        self.assertEqual(self.service.result(job)['state'], 'unavailable')
        self.assertEqual(self.service.deliver(job)['state'], 'delivered')
        value = json.loads(next(self.service.mailbox.glob('notice-*.json')).read_text())
        self.assertEqual(value['investigation_state'], 'cancelled')
        self.assertNotIn('bundle_hash', value['result'])

    def test_uncertain_delivery_is_not_resent_and_existing_file_is_reconciled(self):
        job = self.complete(); original = self.service._publish
        def lost_ack(*args):
            original(*args)
            raise OSError('lost-local-ack')
        with patch.object(self.service, '_publish', side_effect=lost_ack):
            self.assertEqual(self.service.deliver(job)['state'], 'needs_review')
        with patch.object(self.service, '_publish', side_effect=AssertionError('재전송 금지')):
            self.assertEqual(self.service.deliver(job)['state'], 'needs_review')
            self.assertEqual(self.service.reconcile_delivery(job)['state'], 'delivered')
        self.assertEqual(len(list(self.service.mailbox.glob('notice-*.json'))), 1)

    def test_crash_before_publish_is_held_and_never_silently_retried(self):
        job = self.complete()
        with patch.object(self.service, '_publish', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.service.deliver(job)
        self.assertEqual(self.service.deliver(job)['state'], 'sending')
        self.assertEqual(self.service.reconcile_delivery(job)['state'], 'needs_review')
        self.assertEqual(self.service.deliver(job)['state'], 'needs_review')
        self.assertEqual(list(self.service.mailbox.glob('notice-*.json')), [])

    def test_altered_or_cross_task_mailbox_never_counts_as_delivered(self):
        job = self.complete(); self.service.deliver(job)
        path = next(self.service.mailbox.glob('notice-*.json'))
        value = json.loads(path.read_text()); value['conversation_id'] = 'other-thread'
        path.write_bytes(canonical_json_bytes(value))
        self.assertEqual(self.service.reconcile_delivery(job)['state'], 'needs_review')

    def test_repeat_delivery_rechecks_deleted_or_changed_mailbox_without_resending(self):
        for change in ('deleted', 'changed'):
            with self.subTest(change=change):
                self.raw['request_id'] = change
                job = self.complete(); self.service.deliver(job)
                notice = self.service._delivery(job)
                path = self.service._mailbox_path(notice['delivery_id'])
                if change == 'deleted': path.unlink()
                else: path.write_text('{}')
                with patch.object(self.service, '_publish', side_effect=AssertionError('재전송 금지')):
                    self.assertEqual(self.service.deliver(job)['state'], 'needs_review')

    def test_expiry_between_envelope_view_and_result_never_publishes_a_notice(self):
        job = self.complete(); original = self.service.result
        def expire(*args):
            self.fixture.time += RETENTION_SECONDS
            return original(*args)
        with patch.object(self.service, 'result', side_effect=expire), self.assertRaises(ValueError):
            self.service.deliver(job)
        self.assertEqual(list(self.service.mailbox.glob('notice-*.json')), [])

    def test_result_validates_saved_report_binding(self):
        job = self.complete()
        with self.fixture.queue.transaction() as db:
            db.execute('UPDATE jobs SET bundle_hash=? WHERE job_id=?', ('0'*64, job))
        with self.assertRaises(ValueError): self.service.result(job)
        with self.assertRaises(ValueError): self.service.deliver(job)
        self.assertEqual(list(self.service.mailbox.glob('notice-*.json')), [])

    def test_result_refuses_retention_change_during_verified_report_read(self):
        job = self.complete(); original = self.fixture.queue.engine.show
        def expire(*args):
            record = original(*args)
            self.fixture.time += RETENTION_SECONDS
            return record
        with patch.object(self.fixture.queue.engine, 'show', side_effect=expire), self.assertRaises(ValueError):
            self.service.result(job)

    def test_delivery_lock_serializes_publish_reconcile_and_prune(self):
        job = self.complete()
        with self.service._delivery_lock():
            for action in (lambda:self.service.deliver(job),lambda:self.service.reconcile_delivery(job),self.service.prune):
                with self.assertRaisesRegex(ValueError,'task_delivery_busy'):action()
        self.assertEqual(self.service.deliver(job)['state'],'delivered')

    def test_existing_queue_result_uses_same_verified_report_without_delivery_route(self):
        job,_ = self.fixture.submit(key='legacy-local-job')
        self.fixture.worker().run_once()
        self.assertEqual(self.service.result(job)['state'],'available')
        self.assertEqual(self.service.show(job)['source'],'existing_queue')
        with self.assertRaises(ValueError):self.service.deliver(job)

    def test_retention_expiry_blocks_result_and_removes_notice_preserving_dedup(self):
        job = self.complete(); self.service.deliver(job)
        self.fixture.time += RETENTION_SECONDS
        self.assertEqual(self.service.result(job)['state'], 'expired')
        with self.assertRaises(ValueError): self.service.deliver(job)
        self.assertEqual(self.service.prune()['expired_deliveries'], 1)
        self.assertEqual(list(self.service.mailbox.glob('notice-*.json')), [])
        self.assertEqual(self.service.prune()['expired_deliveries'], 0)
        self.assertTrue(self.service.submit(self.raw)['duplicate'])

    def test_mailbox_symlink_is_rejected_without_touching_victim(self):
        job = self.complete(); self.service.deliver(job)
        path = next(self.service.mailbox.glob('notice-*.json'))
        victim = self.fixture.root / 'victim.json'; victim.write_text('keep')
        path.unlink(); path.symlink_to(victim)
        self.assertEqual(self.service.reconcile_delivery(job)['state'], 'needs_review')
        self.assertEqual(victim.read_text(), 'keep')

    def test_dispatch_closed_contract_has_no_approval_recovery_or_model_actions(self):
        for body in ({'action': 'recover'}, {'action': 'approve'}, {'action': 'work'},
                     {'action': 'submit', 'request': self.raw, 'approved': True},
                     {'action': 'show', 'task_id': 'job-'+'0'*32, 'token': 'private'}):
            with self.subTest(body=body), self.assertRaises(ValueError): dispatch(self.service, body)


class LocalTaskAPITest(unittest.TestCase):
    def setUp(self):
        # macOS의 긴 기본 TMPDIR 대신 Unix socket의 경로 제한 안에서 검증한다.
        self.temp = tempfile.TemporaryDirectory(prefix='tasks-', dir='/private/tmp' if sys.platform=='darwin' else '/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'AI_OPS_FILES_ROOT': str(self.root)})
        self.env.start(); self.addCleanup(self.env.stop)
        self.settings = fixtures.load_b_settings(fixtures.ROOT / 'config/targets.practice.example.json',
            fixtures.ROOT / 'config/b-runtime.example.json', fixtures.ROOT / 'config/metric-queries.json')
        self.service = TaskService(JobQueue(self.root/'store'), self.settings)
        self.raw = {'request_id': 'local-event', 'conversation_id': 'local-thread',
                    'target': self.settings.bindings.bindings[0].alias, 'question': '상태 확인'}

    def server(self):
        server = LocalTaskServer(self.service)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
        thread.start()
        def close():
            server.shutdown();thread.join(timeout=3);server.server_close()
        self.addCleanup(close)
        return server

    def test_actual_unix_request_then_fixture_worker_then_result_and_notice(self):
        server = self.server()
        receipt = request(self.service.queue.root, {'action': 'submit', 'request': self.raw})
        self.assertEqual(receipt['status'], 'ok')
        job = receipt['value']['task_id']
        self.assertEqual(request(self.service.queue.root, {'action':'show','task_id':job})['value']['result']['state'], 'pending')
        clock = fixtures.Clock(); api = fixtures.API(self.settings, clock)
        api.docs['deployment']['spec']['replicas'] = 1
        snapshot = fixtures.collect_snapshot(self.settings,self.settings.bindings.bindings[0].target,transport=api,clock=clock)
        with patch('socket.create_connection',side_effect=AssertionError('외부 통신 금지')):
            fixtures.Worker(self.service.queue,self.settings,collector=lambda *a,**kw:snapshot).run_once()
            result = request(self.service.queue.root, {'action':'result','task_id':job})
            delivered = request(self.service.queue.root, {'action':'deliver','task_id':job})
        self.assertEqual(result['value']['state'], 'available')
        self.assertEqual(delivered['value']['state'], 'delivered')
        self.assertEqual(Path(server.server_address).stat().st_mode & 0o777, 0o600)

    def test_malformed_duplicate_keys_extra_fields_and_secrets_are_not_reflected(self):
        self.server()
        bodies = [b'{bad-private-value}\n', b'{"action":"show","action":"recover"}\n',
                  b'['*1200+b'0'+b']'*1200+b'\n',
                  canonical_json_bytes({'action':'approve','secret':'private-value'})+b'\n', b'x'*65537+b'\n']
        for body in bodies:
            with self.subTest(body_length=len(body)), socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                client.settimeout(4); client.connect(str(socket_path(self.service.queue.root)))
                try: client.sendall(body)
                except BrokenPipeError: pass
                self.assertEqual(json.loads(client.recv(65536)), {'status':'rejected'})

    def test_socket_parent_symlink_is_rejected_before_connecting(self):
        directory = self.service.directory
        moved = directory.with_name('tasks-moved')
        directory.rename(moved); directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ValueError): socket_path(self.service.queue.root)

    def test_second_server_preserves_first_socket_and_clean_shutdown_removes_it(self):
        server = LocalTaskServer(self.service)
        path = Path(server.server_address)
        try:
            with self.assertRaises(ValueError): LocalTaskServer(self.service)
            self.assertTrue(path.exists())
        finally: server.server_close()
        self.assertFalse(path.exists())

    def test_socket_permission_setup_failure_removes_only_own_socket(self):
        original=os.chmod
        def fail_socket(path, mode):
            if str(path).endswith('service.sock'):raise OSError('permission-failure')
            return original(path,mode)
        with patch('ai_ops.task_service.os.chmod',side_effect=fail_socket),self.assertRaises(OSError):
            LocalTaskServer(self.service)
        self.assertFalse(socket_path(self.service.queue.root).exists())

    def test_real_process_sigterm_removes_own_socket(self):
        command=[sys.executable,'-m','ai_ops.task_service','serve','--store',str(self.service.queue.root),
                 '--bindings',str(fixtures.ROOT/'config/targets.practice.example.json'),
                 '--runtime',str(fixtures.ROOT/'config/b-runtime.example.json'),
                 '--metric-queries',str(fixtures.ROOT/'config/metric-queries.json')]
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        def cleanup():
            if process.poll() is None:process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        path=socket_path(self.service.queue.root)
        until=time.monotonic()+5
        while not path.exists() and process.poll() is None and time.monotonic()<until:time.sleep(.02)
        self.assertTrue(path.exists())
        process.terminate();out,err=process.communicate(timeout=5)
        self.assertEqual(process.returncode,0,err.decode())
        self.assertFalse(path.exists())


if __name__ == '__main__':unittest.main()
