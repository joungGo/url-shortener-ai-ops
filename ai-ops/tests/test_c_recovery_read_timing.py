"""기존 복구 gate 전체를 사용해 중복 조회·예산 소진을 로컬 재현한다."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from copy import deepcopy
import json
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_ops.c_approval import ApprovalRejected
from ai_ops.c_sync_guard import verify_sync_result
from ai_ops.c_trial_read_budget import recovery_github_reader
from ai_ops.c_trial_read_cache import ImmutableGitReader
from ai_ops.c_recovery_read_timing import RecoveryReadTiming, phase, read_timeout, read_map
import test_c_trial_runtime as runtime_fixtures


class RecoveryReadTimingTest(unittest.TestCase):
    def test_full_recovery_fits_same_budget_without_caching_mutable_gates(self):
        fixture = runtime_fixtures.TrialRuntimeTest(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = fixture.fixture.fixture.reader
        elapsed = [0.0]

        class LatencyReader:
            def __init__(self, **kwargs):
                self.deadline = kwargs['max_read_seconds']

            def get(self, path):
                if elapsed[0] >= self.deadline:
                    raise ApprovalRejected('github_read_timeout')
                elapsed[0] += .4
                return source.get(path)

        def handoff(**args):
            clock = args.pop('clock')
            result = verify_sync_result(**args, now=clock())
            return SimpleNamespace(sync_verified=result['sync_verified'], merge_sha=result['merge_sha'])

        with patch('ai_ops.c_trial_read_budget.ApprovalGitHubReader', LatencyReader):
            reader = recovery_github_reader(fixture.fixture.now+timedelta(seconds=30),
                repository='joungGo/c03-execution-lab', clock=lambda: fixture.fixture.now)
        args = fixture.fixture.fixture.preflight_args(); args['reader'] = reader
        with patch('ai_ops.c_trial_read_cache.time.monotonic', side_effect=lambda:elapsed[0]), \
             patch('ai_ops.c_verification_handoff.verify_c03_handoff', side_effect=handoff):
            receipt = fixture.executor.recover(**args)
        self.assertTrue(receipt.sync_verified)
        self.assertLess(elapsed[0], 30)
        immutable = [p for p in source.calls if '/git/' in p and '/ref/' not in p]
        self.assertEqual(len(immutable), len(set(immutable)))
        dynamic = Counter(p for p in source.calls if p not in immutable)
        self.assertEqual(dynamic['repos/joungGo/c03-execution-lab/git/ref/heads/main'], 12)
        self.assertEqual(dynamic['repos/joungGo/c03-execution-lab/pulls/123'], 11)
        self.assertEqual(dynamic['repos/joungGo/c03-execution-lab/pulls/123/reviews?per_page=100'], 5)
        self.assertEqual(fixture.fixture.fixture.live.calls, 4)
        self.assertEqual(fixture.merge_calls, 1)

    def test_mutable_gates_are_rechecked_even_when_git_objects_are_cached(self):
        for kind in ('main', 'pull', 'reviews', 'checks', 'protection'):
            with self.subTest(kind=kind):
                f = runtime_fixtures.TrialRuntimeTest(); f.setUp()
                try:
                    source = f.fixture.fixture.reader
                    prefix = 'repos/joungGo/c03-execution-lab'
                    paths = {'main': prefix+'/git/ref/heads/main', 'pull': prefix+'/pulls/123',
                        'reviews': prefix+'/pulls/123/reviews?per_page=100',
                        'checks': prefix+'/commits/'+'b'*40+'/check-runs?per_page=100',
                        'protection': prefix+'/branches/main/protection'}
                    path = paths[kind]; calls = []
                    def change():
                        calls.append(True)
                        if len(calls) < 2: return
                        value = source.data[path]
                        if kind == 'main': value['object']['sha'] = 'f'*40
                        elif kind == 'pull': value['head']['sha'] = 'f'*40
                        elif kind == 'reviews': value[0]['state'] = 'DISMISSED'
                        elif kind == 'checks': value['check_runs'][0]['conclusion'] = 'failure'
                        else: value['allow_force_pushes']['enabled'] = True
                    source.hooks[path] = change
                    args = f.fixture.fixture.preflight_args(); args['reader'] = ImmutableGitReader(source)
                    with self.assertRaises(ApprovalRejected): f.executor.recover(**args)
                    self.assertGreaterEqual(len(calls), 2)
                    self.assertEqual(f.merge_calls, 0)
                    self.assertEqual(f.argo_calls, [])
                finally: f.doCleanups()

    def test_expired_underlying_budget_rejects_even_a_cached_git_object(self):
        from unittest.mock import Mock
        source = Mock(deadline=10.); source.get.return_value = {'sha': 'a'*40}
        reader = ImmutableGitReader(source, enforce_deadline=True)
        path = 'repos/joungGo/c03-execution-lab/git/commits/'+'a'*40
        with patch('ai_ops.c_trial_read_cache.time.monotonic', return_value=9.): reader.get(path)
        with patch('ai_ops.c_trial_read_cache.time.monotonic', return_value=10.):
            with self.assertRaisesRegex(ApprovalRejected, '^github_read_timeout$'): reader.get(path)
        self.assertEqual(source.get.call_count, 1)

    def test_read_timeout_and_deadline_do_not_extend_when_wall_clock_moves_back(self):
        from datetime import datetime, timezone
        now = datetime(2026,11,16,tzinfo=timezone.utc); wall = [now]; mono = [0.]
        trace = RecoveryReadTiming(now+timedelta(seconds=3), clock=lambda:wall[0], monotonic=lambda:mono[0])
        with trace.activate():
            self.assertEqual(read_timeout(15), 3.)
            wall[0] -= timedelta(seconds=30); mono[0] = 2.
            self.assertEqual(read_timeout(8), 1.)
            mono[0] = 3.
            with self.assertRaisesRegex(ApprovalRejected, '^recovery_read_deadline_exceeded$'): read_timeout(20)
        self.assertEqual(read_timeout(20), 20)

    def test_parallel_reads_inherit_same_budget_without_leaking_context(self):
        from datetime import datetime, timezone
        now = datetime(2026,11,16,tzinfo=timezone.utc)
        trace = RecoveryReadTiming(now+timedelta(seconds=3), clock=lambda:now, monotonic=lambda:0.)
        def task(value):
            with phase('audit.values', read=True): return read_timeout(value)
        with trace.activate(), ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(read_map(pool, task, [8,15,20]), [3.,3.,3.])
        self.assertEqual(len(trace.report()['events']), 3)
        self.assertEqual(read_timeout(20), 20)

    def test_stage_failure_is_bounded_and_does_not_copy_exception_or_endpoint(self):
        from datetime import datetime, timezone
        now = datetime(2026,11,16,tzinfo=timezone.utc); mono=[0.]
        trace = RecoveryReadTiming(now+timedelta(seconds=30), clock=lambda:now, monotonic=lambda:mono[0])
        with trace.activate():
            with self.assertRaisesRegex(ValueError, 'secret-response'):
                with phase('source.audit', read=True):
                    mono[0]=2.; raise ValueError('secret-response')
            with self.assertRaisesRegex(ValueError, 'phase_rejected'):
                with phase('https://secret.invalid/token'): pass
            for _ in range(300):
                with phase('source.before'): pass
        report=trace.report()
        self.assertEqual(len(report['events']),256)
        self.assertEqual(report['dropped_events'],45)
        self.assertNotIn('secret',json.dumps(report))
        self.assertEqual(report['events'][0]['duration_ms'],2000.)
        self.assertEqual(report['events'][0]['status'],'error')

    def test_non_github_audit_stops_before_values_or_second_snapshot_after_budget(self):
        from datetime import datetime, timezone
        import test_c_execution_source as source_fixtures
        fixture=source_fixtures.ExecutionSourceTest(); fixture.setUp()
        now=datetime(2026,11,16,tzinfo=timezone.utc); mono=[0.]
        trace=RecoveryReadTiming(now+timedelta(seconds=3),clock=lambda:now,monotonic=lambda:mono[0])
        calls=[]
        def command(*args):
            calls.append(args); result=fixture.run_command(*args); mono[0]=4.; return result
        args=SimpleNamespace(kubeconfig='/tmp/fixture-only',normal_binding=None,operator_state_root=None)
        with trace.activate(), patch.object(source_fixtures.audit,'_run',side_effect=command):
            with self.assertRaisesRegex(ApprovalRejected,'^recovery_read_deadline_exceeded$'):
                source_fixtures.audit._collect(args,include_snapshot=True)
        self.assertTrue(calls)
        self.assertTrue(all(c[0]=='kubectl' for c in calls))
        self.assertEqual(fixture.git_reads,0)
        self.assertEqual(trace.report()['events'][-1]['stage'],'audit.initial')

    def test_real_local_subprocess_uses_remaining_budget_instead_of_15_seconds(self):
        from datetime import datetime, timezone
        import test_c_execution_source as source_fixtures
        now=datetime.now(timezone.utc)
        trace=RecoveryReadTiming(now+timedelta(seconds=.1),clock=lambda:datetime.now(timezone.utc))
        start=time.monotonic()
        with trace.activate(), self.assertRaises(subprocess.TimeoutExpired):
            with phase('audit.identity_history',read=True):
                source_fixtures.audit._run(sys.executable,'-c','import time; time.sleep(5)')
        self.assertLess(time.monotonic()-start,1.)
        self.assertEqual(trace.report()['events'][0]['status'],'error')

    def test_slow_socket_fragments_do_not_reset_absolute_read_deadline(self):
        from datetime import datetime, timezone
        import socket
        import threading
        import tempfile
        from pathlib import Path
        from ai_ops.c_operator import _send
        release=threading.Event()
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            path=Path(folder)/'operator.sock'
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as server:
                server.bind(str(path));server.listen(1);server.settimeout(.5)
                def peer():
                    try:connection,_=server.accept()
                    except OSError:return
                    with connection:
                        connection.recv(4096)
                        for _ in range(100):
                            if release.wait(.015):break
                            try:connection.sendall(b' ')
                            except OSError:break
                thread=threading.Thread(target=peer);thread.start()
                now=datetime.now(timezone.utc)
                trace=RecoveryReadTiming(now+timedelta(seconds=.15),clock=lambda:datetime.now(timezone.utc))
                try:
                    start=time.monotonic()
                    with trace.activate(), self.assertRaises((socket.timeout,ApprovalRejected)):
                        _send(path,{'action':'show','incident_id':'local','run_id':'local'})
                    self.assertLess(time.monotonic()-start,1.)
                finally:
                    release.set();thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

    def test_expired_reads_do_not_prevent_merge_token_cleanup(self):
        from datetime import datetime, timezone
        from unittest.mock import Mock
        from ai_ops.c_trial_github import GitHubMergeSession
        now=datetime(2026,11,16,tzinfo=timezone.utc); mono=[0.]
        trace=RecoveryReadTiming(now+timedelta(seconds=1),clock=lambda:now,monotonic=lambda:mono[0])
        merge=GitHubMergeSession(); merge.token='fixture-token'; merge.identity={'fixture':True}
        connection=Mock();connection.getresponse.return_value.status=204
        connection.getresponse.return_value.read.return_value=b''
        with trace.activate(),patch('ai_ops.c_trial_github.http.client.HTTPSConnection',return_value=connection) as factory:
            mono[0]=2.
            with self.assertRaisesRegex(ApprovalRejected,'^recovery_read_deadline_exceeded$'):
                merge._api('GET','/app','fixture-credential')
            factory.assert_not_called()
            merge.close()
        self.assertIsNone(merge.token)
        factory.assert_called_once_with('api.github.com',timeout=12)
        self.assertEqual(connection.request.call_args.args[0],'DELETE')
        self.assertNotIn('fixture-token',json.dumps(trace.report()))

    def test_all_reads_share_existing_github_deadline_not_a_later_owner_time(self):
        from datetime import datetime, timezone
        now=datetime(2026,11,16,tzinfo=timezone.utc);mono=[3.]
        trace=RecoveryReadTiming(now+timedelta(seconds=60),clock=lambda:now,
            monotonic=lambda:mono[0],read_deadline=8.)
        with trace.activate():
            self.assertEqual(read_timeout(15),5.)
            mono[0]=8.
            with self.assertRaisesRegex(ApprovalRejected,'^recovery_read_deadline_exceeded$'):
                with phase('audit.initial',read=True):pass

    def test_runtime_inherits_reader_deadline_before_any_gate_request(self):
        fixture=runtime_fixtures.TrialRuntimeTest();fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source=fixture.fixture.fixture.reader
        def unexpected_read():raise AssertionError('deadline_was_not_propagated')
        source.hooks['user']=unexpected_read
        args=fixture.fixture.fixture.preflight_args()
        args['reader']=ImmutableGitReader(SimpleNamespace(deadline=0.,get=source.get))
        with patch('ai_ops.c_recovery_read_timing.time.monotonic',return_value=1.):
            with self.assertRaisesRegex(ApprovalRejected,'^recovery_read_deadline_exceeded$'):
                fixture.executor.recover(**args)
        self.assertEqual(source.calls,[])
        self.assertEqual(fixture.merge_calls,0)
        self.assertEqual(read_timeout(15),15)

    def test_slow_app_get_body_cannot_spend_multiple_socket_timeouts(self):
        from datetime import datetime,timezone
        from unittest.mock import Mock
        from ai_ops.c_trial_github import GitHubMergeSession
        now=datetime(2026,11,16,tzinfo=timezone.utc);mono=[0.]
        trace=RecoveryReadTiming(now+timedelta(seconds=1),clock=lambda:now,monotonic=lambda:mono[0])
        stream=Mock();stream.timeout=1.
        stream.settimeout.side_effect=lambda seconds:setattr(stream,'timeout',seconds)
        class Response:
            status=200
            remaining=5
            def read1(self,maximum):
                if self.remaining==0:return b''
                self.remaining-=1
                if stream.timeout<.4:
                    mono[0]+=stream.timeout;raise TimeoutError('fixture timeout')
                mono[0]+=.4;return b' '
            def read(self,maximum):
                while self.remaining:self.read1(maximum)
                return b'{}'
            def close(self):pass
        connection=Mock();connection.sock=stream
        connection.getresponse.return_value=Response()
        with trace.activate(),patch('ai_ops.c_trial_github.http.client.HTTPSConnection',return_value=connection):
            with self.assertRaises((ApprovalRejected,TimeoutError)):
                GitHubMergeSession()._api('GET','/app','fixture-credential')
        self.assertLessEqual(mono[0],1.001)
        connection.close.assert_called_once()

    def test_app_get_slow_headers_are_interrupted_and_connection_is_closed(self):
        from datetime import datetime,timezone
        import http.client
        import socketserver
        import threading
        from ai_ops.c_trial_github import GitHubMergeSession
        stopped=threading.Event()
        class Peer(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(4096)
                try:
                    self.request.sendall(b'HTTP/1.1 200 OK\r\n')
                    while not stopped.wait(.02):self.request.sendall(b'X-Slow: 1\r\n')
                except OSError:pass
        with socketserver.TCPServer(('127.0.0.1',0),Peer) as server:
            thread=threading.Thread(target=server.serve_forever);thread.start()
            connection=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=1)
            now=datetime.now(timezone.utc)
            trace=RecoveryReadTiming(now+timedelta(seconds=.15),clock=lambda:datetime.now(timezone.utc))
            try:
                start=time.monotonic()
                with trace.activate(),patch('ai_ops.c_trial_github.http.client.HTTPSConnection',return_value=connection):
                    with self.assertRaises((ValueError,OSError,ApprovalRejected)):
                        GitHubMergeSession()._api('GET','/app','fixture')
                self.assertLess(time.monotonic()-start,1.)
                self.assertIsNone(connection.sock)
            finally:
                stopped.set();server.shutdown();thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_response_close_error_does_not_skip_connection_close(self):
        from unittest.mock import Mock
        from ai_ops.c_trial_github import GitHubMergeSession
        connection=Mock();response=connection.getresponse.return_value
        response.status=204;response.read.return_value=b''
        response.close.side_effect=OSError('fixture close failed')
        with patch('ai_ops.c_trial_github.http.client.HTTPSConnection',return_value=connection):
            with self.assertRaises(OSError):
                GitHubMergeSession()._api('DELETE','/installation/token','fixture-token')
        connection.close.assert_called_once()


if __name__ == '__main__': unittest.main()
