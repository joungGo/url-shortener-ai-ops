"""WF10 CI relay 네트워크(2026-11-26, 사용자 결정 P1+N1): 고정 목적지 proxy 경유.

메인 Pod는 인터넷 직접 송신이 없다. CI relay는 GitHub API와 정상 CI 영수증 artifact의
서명 다운로드를 WF10 egress proxy(CONNECT, TLS 유지)로만 읽는다. proxy는 GitHub 소유
artifact 저장소를 정확한 패턴으로, ``--github-artifacts``일 때만 허용한다.
"""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.request import ProxyHandler

from ai_ops import ci_relay, recovery_egress_proxy as egress
from ai_ops.ci_token_reader import TokenGitHubReader

PROXY = 'http://ai-ops-egress.ai-ops.svc:8080'


def connect(target):
    return f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n'.encode()


def proxies(reader):
    """urllib registers no ProxyHandler for ``{}``; the default (environment) one is then not added either."""
    handlers = [h for h in reader.opener.handlers if isinstance(h, ProxyHandler)]
    assert len(handlers) <= 1
    return handlers[0].proxies if handlers else {}


class TokenReaderProxyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.token = Path(tmp.name) / 'token'
        self.token.write_text('github_pat_' + 'a' * 30)
        self.token.chmod(0o600)

    def test_only_the_explicit_proxy_is_used_never_environment_variables(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://evil.test:3128', 'https_proxy': 'http://evil.test:3128'}):
            self.assertEqual(proxies(TokenGitHubReader(self.token)), {})
            self.assertEqual(proxies(TokenGitHubReader(self.token, proxy=PROXY)), {'https': PROXY})

    def test_rejects_proxy_addresses_outside_the_fixed_form(self):
        for bad in ('https://ai-ops-egress.ai-ops.svc:8080', 'http://user@ai-ops-egress:8080',
                    'http://ai-ops-egress.ai-ops.svc:8080/path', 'http://ai-ops-egress.ai-ops.svc',
                    'http://EVIL.test:8080', ' http://a:8080', 8080):
            with self.subTest(proxy=bad), self.assertRaisesRegex(ValueError, 'CI egress proxy rejected'):
                TokenGitHubReader(self.token, proxy=bad)


class RelayMainProxyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'config').mkdir()
        (self.root / 'store').mkdir()
        self.token = self.root / 'token'
        self.token.write_text('github_pat_' + 'b' * 30)
        self.token.chmod(0o600)
        env = patch.dict(os.environ, {'AI_OPS_FILES_ROOT': str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def run_main(self, env):
        relay = Mock()
        relay.submit_run.return_value = {'job_id': 'j', 'duplicate': False, 'state': 'queued'}
        out = io.StringIO()
        with (patch.dict(os.environ, env), patch.object(ci_relay, 'load_b_settings', return_value=Mock()),
              patch.object(ci_relay, 'runbook_settings_kwargs', return_value={}),
              patch.object(ci_relay, 'JobQueue'), patch.object(ci_relay, 'CIRelay', return_value=relay) as factory,
              redirect_stdout(out)):
            code = ci_relay.main(['--config', str(self.root / 'config'), '--store', str(self.root / 'store'),
                                  '--run-id', '1', '--expected-head', 'a' * 40])
        return code, out.getvalue().strip(), factory

    def test_relay_hands_the_proxy_to_the_token_reader(self):
        code, _, factory = self.run_main({'AI_CI_GITHUB_TOKEN_FILE': str(self.token), 'AI_CI_EGRESS_PROXY': PROXY})
        self.assertEqual(code, 0)
        reader = factory.call_args.kwargs['reader']
        self.assertIsInstance(reader, TokenGitHubReader)
        self.assertEqual(proxies(reader), {'https': PROXY})

    def test_proxy_without_token_reader_fails_closed_with_a_fixed_code(self):
        os.environ.pop('AI_CI_GITHUB_TOKEN_FILE', None)
        code, out, factory = self.run_main({'AI_CI_EGRESS_PROXY': PROXY})
        self.assertEqual((code, out), (1, '{"error":"ci_relay_failed"}'))
        factory.assert_not_called()

    def test_invalid_proxy_fails_closed_without_echoing_it(self):
        code, out, _ = self.run_main({'AI_CI_GITHUB_TOKEN_FILE': str(self.token),
                                      'AI_CI_EGRESS_PROXY': 'https://secret-marker.evil:1'})
        self.assertEqual((code, out), (1, '{"error":"ci_relay_failed"}'))
        self.assertNotIn('secret-marker', out)


class ProxyArtifactTargetsTest(unittest.TestCase):
    ALLOWED = ('productionresultssa0.blob.core.windows.net:443', 'productionresultssa9.blob.core.windows.net:443',
               'productionresultssa19.blob.core.windows.net:443',
               'results-receiver.actions.githubusercontent.com:443')
    DENIED = ('productionresultssa100.blob.core.windows.net:443', 'productionresultssa09.blob.core.windows.net:443',
              'evil.blob.core.windows.net:443', 'productionresultssa9.blob.core.windows.net.evil.test:443',
              'productionresultssa9.blob.core.windows.net:80', 'xproductionresultssa9.blob.core.windows.net:443',
              'results-receiver.actions.githubusercontent.com.evil.test:443', 'actions.githubusercontent.com:443',
              'productionresultssa9.blob.core.windows.net:4430', 'results-receiver.actions.githubusercontent.com:4430')

    def test_artifact_storage_is_denied_unless_enabled(self):
        for target in self.ALLOWED:
            with self.subTest(target=target), self.assertRaises(ValueError):
                egress.parse_connect(connect(target))

    def test_enabled_patterns_allow_only_github_artifact_storage(self):
        for target in self.ALLOWED:
            with self.subTest(target=target):
                self.assertEqual(egress.parse_connect(connect(target), egress.TARGETS, egress.GITHUB_ARTIFACTS), target)
        for target in self.DENIED:
            with self.subTest(target=target), self.assertRaises(ValueError):
                egress.parse_connect(connect(target), egress.TARGETS, egress.GITHUB_ARTIFACTS)
        # Bare HTTP/1.0 CONNECT (Python 3.11 clients) reaches artifact storage too.
        self.assertEqual(egress.parse_connect(b'CONNECT productionresultssa9.blob.core.windows.net:443 HTTP/1.0\r\n\r\n',
                                              egress.TARGETS, egress.GITHUB_ARTIFACTS),
                         'productionresultssa9.blob.core.windows.net:443')
        # The fixed WF07 targets stay allowed; a mismatched Host header is still rejected.
        self.assertEqual(egress.parse_connect(connect('api.github.com:443'), egress.TARGETS, egress.GITHUB_ARTIFACTS),
                         'api.github.com:443')
        raw = b'CONNECT productionresultssa9.blob.core.windows.net:443 HTTP/1.1\r\nHost: evil.test:443\r\n\r\n'
        with self.assertRaises(ValueError):
            egress.parse_connect(raw, egress.TARGETS, egress.GITHUB_ARTIFACTS)

    def test_main_flag_controls_the_patterns(self):
        for argv, expected in (([], ()), (['--github-artifacts'], egress.GITHUB_ARTIFACTS)):
            server = Mock()
            server.__enter__ = Mock(return_value=server)
            server.__exit__ = Mock(return_value=False)
            with self.subTest(argv=argv), patch.object(egress, 'ProxyServer', return_value=server) as factory:
                egress.main(argv)
                self.assertEqual(factory.call_args.kwargs['patterns'], expected)
                server.serve_forever.assert_called_once()

    def test_server_rejects_non_443_patterns(self):
        import re
        with self.assertRaisesRegex(ValueError, 'proxy_targets_rejected'):
            egress.ProxyServer(('127.0.0.1', 0), patterns=(re.compile(r'a\.test:80'),))


class ProxyServerArtifactTunnelTest(unittest.TestCase):
    """Real ProxyServer: the resolver claims a public address, the connector reaches a local echo."""
    def setUp(self):
        import socket, threading
        self.socket, self.threading = socket, threading
        self.echo = socket.socket()
        self.echo.bind(('127.0.0.1', 0))
        self.echo.listen(4)
        self.addCleanup(self.echo.close)
        threading.Thread(target=self._echo, daemon=True).start()

    def _echo(self):
        while True:
            try:
                conn, _ = self.echo.accept()
            except OSError:
                return
            with conn:
                data = conn.recv(4096)
                if data:
                    conn.sendall(data)

    def start(self, patterns):
        answers = [(self.socket.AF_INET, self.socket.SOCK_STREAM, 6, '', ('20.150.0.1', 443))]
        server = egress.ProxyServer(('127.0.0.1', 0), patterns=patterns, resolver=lambda *a, **k: answers,
                                    connect=lambda addr: self.socket.create_connection(self.echo.getsockname(), timeout=5))
        self.threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def status(self, server, target):
        with self.socket.create_connection(server.server_address, timeout=5) as sock:
            sock.sendall(connect(target))
            head = b''
            while not head.endswith(b'\r\n\r\n'):
                part = sock.recv(1)
                if not part:
                    break
                head += part
            return head.split(b' ')[1].decode()

    def test_server_relays_artifact_storage_only_when_enabled(self):
        target = 'productionresultssa9.blob.core.windows.net:443'
        self.assertEqual(self.status(self.start(()), target), '403')
        enabled = self.start(egress.GITHUB_ARTIFACTS)
        self.assertEqual(self.status(enabled, target), '200')
        self.assertEqual(self.status(enabled, 'evil.blob.core.windows.net:443'), '403')
