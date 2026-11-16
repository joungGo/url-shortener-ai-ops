"""검사를 생략하지 않는 병렬 구간·마지막 ACK·실패 시 무쓰기·진단 비출력을 검증한다."""

from datetime import timedelta
import json
import threading
import unittest

from ai_ops.c_trial_startup_trace import StartupTrace
import test_c_trial_ready as fixtures


class StartupTraceTest(unittest.TestCase):
    def test_approval_and_critical_window_are_distinguished_without_moving_deadlines(self):
        tick = iter([1., 2., 3.])
        trace = StartupTrace(clock=lambda: next(tick))
        trace.begin(); trace.critical_start(); trace.finish('first_write_ready')
        report = trace.report()
        self.assertEqual(report['elapsed_ms'], 2000.)
        self.assertEqual(report['critical_elapsed_ms'], 1000.)

    def test_errors_are_reraised_without_payload_and_trace_freezes(self):
        tick = iter([1., 2., 3., 4., 5., 6.])
        trace = StartupTrace(clock=lambda: next(tick)); trace.begin()
        with self.assertRaisesRegex(ValueError, 'credential-body'):
            with trace.stage('start.acks'):
                raise ValueError('credential-body')
        trace.finish('rejected')
        with trace.stage('cleanup.read'):
            pass
        report = trace.report()
        self.assertEqual(report['outcome'], 'rejected')
        self.assertEqual(report['events'], [dict(stage='start.acks', status='error',
            started_ms=1000., duration_ms=1000.)])
        self.assertNotIn('credential-body', json.dumps(report))

    def test_trace_is_bounded_and_rejects_arbitrary_command_labels(self):
        trace = StartupTrace(); trace.begin()
        for _ in range(140):
            with trace.stage('kube.lease'):
                pass
        trace.finish('first_write_ready')
        self.assertEqual(len(trace.report()['events']), 128)
        self.assertEqual(trace.report()['dropped_events'], 12)
        with self.assertRaises(ValueError):
            with trace.stage('https://credential@example.test'):
                pass


class ReadyStartupTest(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ReadyStartTest(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.barrier = threading.Barrier(3)
        self.completed = []
        self.lock = threading.Lock()
        self.snapshot_count = 0
        self.bad_second_snapshot = False
        self.fail_images = False
        for name in ('require_guards', 'require_images', 'snapshot'):
            original = getattr(self.f.port, name)
            def wrap(*args, _name=name, _original=original):
                if threading.current_thread() is threading.main_thread():
                    return _original(*args)
                # 실제 세 검사가 함께 도착해야 통과한다. 순차 실행은 이 barrier를 통과할 수 없다.
                self.barrier.wait(timeout=2)
                value = _original(*args)
                if _name == 'require_images' and self.fail_images:
                    raise ValueError('image_check_failed')
                if _name == 'snapshot':
                    self.snapshot_count += 1
                    if self.snapshot_count == 2 and self.bad_second_snapshot:
                        value['deployment']['metadata']['uid'] = 'replacement'
                with self.lock:
                    self.completed.append(_name)
                return value
            setattr(self.f.port, name, wrap)

    def assert_stopped_without_write(self):
        self.assertEqual(self.f.port.calls, [])
        self.assertFalse(self.f.fx.server.value['spec'].get('holderIdentity'))
        self.assertTrue(self.f.controller.journal.exists())
        self.assertEqual(self.f.controller.startup_trace.report()['outcome'], 'rejected')

    def test_both_read_cohorts_complete_before_final_fresh_ack_and_first_write(self):
        ack_counts = []
        original = self.f.port.require_ready_guard_acks
        def ack(activation):
            ack_counts.append(len(self.completed))
            return original(activation)
        self.f.port.require_ready_guard_acks = ack
        executor, _ = self.f.start()
        self.assertEqual(sorted(self.completed), sorted(['require_guards', 'require_images', 'snapshot']*2))
        self.assertEqual(ack_counts[-1], 6)
        self.assertEqual([x[1][1] for x in self.f.port.calls], ['set', 'sync'])
        report = self.f.controller.startup_trace.report()
        self.assertEqual(report['outcome'], 'first_write_ready')
        stages = {x['stage'] for x in report['events']}
        self.assertTrue({'start.lease', 'start.arm', 'inject.first.snapshot',
                         'inject.second.snapshot', 'inject.final_acks', 'inject.journal_budget'} <= stages)
        self.assertEqual(executor.plan.rollback_deadline-executor.plan.planned_at, timedelta(seconds=300))

    def test_failed_parallel_image_check_cannot_write(self):
        self.fail_images = True
        with self.assertRaisesRegex(ValueError, 'image_check_failed'):
            self.f.start()
        self.assert_stopped_without_write()
        self.assertEqual(self.snapshot_count, 1)

    def test_changed_target_in_second_snapshot_cannot_write(self):
        self.bad_second_snapshot = True
        with self.assertRaises(ValueError):
            self.f.start()
        self.assert_stopped_without_write()
        errors = [v['stage'] for v in self.f.controller.startup_trace.report()['events'] if v['status']=='error']
        self.assertIn('inject.second_validate', errors)

    def test_lost_ack_after_parallel_reads_cannot_write(self):
        original = self.f.port.require_ready_guard_acks
        def ack(activation):
            if len(self.completed) == 6:
                raise ValueError('lost_final_ack')
            return original(activation)
        self.f.port.require_ready_guard_acks = ack
        with self.assertRaisesRegex(ValueError, 'lost_final_ack'):
            self.f.start()
        self.assert_stopped_without_write()

    def test_parallel_reads_do_not_relax_fifteen_second_budget(self):
        self.f.arm_delay = 14
        original = self.f.port.require_ready_guard_acks
        def ack(activation):
            if len(self.completed) == 6:
                self.f.advance(2)
            return original(activation)
        self.f.port.require_ready_guard_acks = ack
        with self.assertRaises(ValueError):
            self.f.start()
        self.assert_stopped_without_write()
        errors = [v['stage'] for v in self.f.controller.startup_trace.report()['events'] if v['status']=='error']
        self.assertIn('inject.write_budget', errors)

    def test_timing_bookkeeping_cannot_push_first_write_outside_budget(self):
        original = self.f.controller.startup_trace.finish
        def delayed(outcome):
            if outcome == 'first_write_ready':
                self.f.advance(16)
            original(outcome)
        self.f.controller.startup_trace.finish = delayed
        with self.assertRaisesRegex(ValueError, 'startup_timing_crossed_injection_budget'):
            self.f.start()
        self.assert_stopped_without_write()


if __name__ == '__main__':
    unittest.main()
