"""WF07 fixed-destination egress proxy and the relay's explicit proxy use.

Loopback only: a real ProxyServer with an injected resolver/connector in front of a
local echo server, and a recording fake proxy for the clients. No external network.
"""
import os
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_ops.recovery_egress_proxy import TARGETS, ProxyServer, parse_connect, public_addresses
from ai_ops.recovery_sources import PrepareConfig, TokenRepoReader

ROLE = 'ai-ops-recovery-reader'
GITHUB = 'CONNECT api.github.com:443 HTTP/1.1\r\nHost: api.github.com:443\r\n\r\n'


def answer(ip, family=socket.AF_INET):
    return [(family, socket.SOCK_STREAM, 6, '', (ip, 443))]


class ParseTest(unittest.TestCase):
    def test_only_the_three_fixed_destinations(self):
        for target in TARGETS:
            for version in ('HTTP/1.0', 'HTTP/1.1'):
                raw = f'CONNECT {target} {version}\r\nHost: {target}\r\n\r\n'.encode()
                self.assertEqual(parse_connect(raw), target)
        self.assertEqual(len(TARGETS), 3)

    def test_everything_else_is_rejected(self):
        bad = [
            'CONNECT api.openai.com:443 HTTP/1.1\r\nHost: api.openai.com:443\r\n\r\n',
            'CONNECT api.github.com:80 HTTP/1.1\r\nHost: api.github.com:80\r\n\r\n',
            'CONNECT 169.254.169.254:443 HTTP/1.1\r\nHost: 169.254.169.254:443\r\n\r\n',
            'GET https://api.github.com/ HTTP/1.1\r\nHost: api.github.com\r\n\r\n',
            'connect api.github.com:443 HTTP/1.1\r\nHost: api.github.com:443\r\n\r\n',
            'CONNECT api.github.com:443 HTTP/2\r\nHost: api.github.com:443\r\n\r\n',
            'CONNECT api.github.com:443  HTTP/1.1\r\nHost: api.github.com:443\r\n\r\n',
            'CONNECT api.github.com:443 HTTP/1.1\r\n\r\n',
            'CONNECT api.github.com:443 HTTP/1.1\r\nHost: sts.ap-northeast-2.amazonaws.com:443\r\n\r\n',
            GITHUB[:-2] + 'Proxy-Authorization: x\r\n\r\n',
            GITHUB[:-4],
        ]
        for raw in bad:
            with self.subTest(raw=raw[:40]), self.assertRaises(ValueError):
                parse_connect(raw.encode())
        for raw in (b'CONNECT api.github.com:443 HTTP/1.1\r\nHost: \xff\r\n\r\n', GITHUB.encode() + b'x' * 5000):
            with self.assertRaises(ValueError):
                parse_connect(raw)

    def test_every_dns_answer_must_be_public_ipv4(self):
        self.assertEqual(public_addresses('api.github.com:443', lambda *a, **k: answer('140.82.112.5'))[0][4][0],
                         '140.82.112.5')
        for answers in ([], answer('10.0.0.5'), answer('169.254.169.254'), answer('127.0.0.1'),
                        answer('100.64.0.1'), answer('1.1.1.1') + answer('192.168.0.1'),
                        answer('2606:4700::1111', socket.AF_INET6)):
            with self.subTest(answers=answers), self.assertRaises(ValueError):
                public_addresses("api.github.com:443", lambda *a, r=answers, **k: r)


class ServerTest(unittest.TestCase):
    """Real ProxyServer; the resolver claims a public address, the connector reaches a local echo."""
    def setUp(self):
        self.echo = socket.socket()
        self.echo.bind(('127.0.0.1', 0))
        self.echo.listen(4)
        threading.Thread(target=self._echo, daemon=True).start()
        self.connects, self.answers, self.now = [], answer('140.82.112.5'), 0.0
        self.server = self.start()

    def tearDown(self):
        self.echo.close()

    def _echo(self):
        while True:
            try:
                conn, _ = self.echo.accept()
            except OSError:
                return
            threading.Thread(target=self._pump, args=(conn,), daemon=True).start()

    @staticmethod
    def _pump(conn):
        with conn:
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                conn.sendall(data)

    def start(self, **kwargs):
        def connect(sockaddr):
            self.connects.append(sockaddr)
            return socket.create_connection(self.echo.getsockname(), timeout=5)
        server = ProxyServer(('127.0.0.1', 0), resolver=lambda *a, **k: self.answers, connect=connect,
                             clock=lambda: self.now, **kwargs)
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def client(self, request=GITHUB, server=None):
        sock = socket.create_connection((server or self.server).server_address, timeout=5)
        sock.sendall(request.encode())
        return sock

    def head(self, sock):
        data = b''
        while not data.endswith(b'\r\n\r\n'):
            part = sock.recv(1)
            if not part:
                break
            data += part
        return data.split(b'\r\n')[0]

    def test_allowed_destination_is_tunnelled_byte_for_byte(self):
        with self.client() as sock:
            self.assertEqual(self.head(sock), b'HTTP/1.1 200 Connection Established')
            sock.sendall(b'\x16\x03\x01 tls bytes stay opaque')
            self.assertEqual(sock.recv(100), b'\x16\x03\x01 tls bytes stay opaque')
        self.assertEqual(self.connects, [('140.82.112.5', 443)])

    def test_other_destination_or_internal_dns_gets_403_without_connecting(self):
        with self.client('CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n') as sock:
            self.assertEqual(self.head(sock), b'HTTP/1.1 403 Forbidden')
        self.answers = answer('169.254.169.254')
        with self.client() as sock:
            self.assertEqual(self.head(sock), b'HTTP/1.1 403 Forbidden')
        self.assertEqual(self.connects, [])

    def test_concurrency_limit_and_tunnel_lifetime(self):
        server = self.start(max_tunnels=1)
        first = self.client(server=server)
        self.assertEqual(self.head(first), b'HTTP/1.1 200 Connection Established')
        with self.client(server=server) as second:
            self.assertEqual(self.head(second), b'HTTP/1.1 503 Service Unavailable')
        first.sendall(b'a')
        self.assertEqual(first.recv(10), b'a')  # within its lifetime the tunnel relays
        self.now = 1000.0  # past the lifetime: traffic does not keep it alive
        first.sendall(b'x')
        first.settimeout(3)
        closed = False
        while True:
            try:
                part = first.recv(100)
            except OSError:  # still open after 3s: the lifetime was not enforced
                break
            if not part:
                closed = True
                break
        first.close()
        self.assertTrue(closed, 'tunnel must be closed by the proxy once its lifetime is over')

    def test_targets_must_be_tls_port(self):
        with self.assertRaises(ValueError):
            ProxyServer(('127.0.0.1', 0), targets=('api.github.com:80',))


class Recorder:
    """A fake proxy that records the first request line and closes."""
    def __init__(self):
        self.lines = []
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.url = f'http://127.0.0.1:{self.sock.getsockname()[1]}'
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                self.lines.append(conn.recv(4096).split(b'\r\n')[0].decode())


class ClientProxyTest(unittest.TestCase):
    def setUp(self):
        self.recorder = Recorder()
        self.addCleanup(self.recorder.sock.close)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.github_token = os.path.join(self.dir.name, 'github')
        with open(self.github_token, 'w') as handle:
            handle.write('github_pat_' + 'A1b2' * 10)
        os.chmod(self.github_token, 0o600)

    def irsa_env(self):
        token = os.path.join(self.dir.name, 'web-identity')
        with open(token, 'w') as handle:
            handle.write('projected-service-account-token')
        missing = os.path.join(self.dir.name, 'none')
        return {'AWS_ROLE_ARN': f'arn:aws:iam::764428911194:role/{ROLE}', 'AWS_WEB_IDENTITY_TOKEN_FILE': token,
                'AWS_CONFIG_FILE': missing, 'AWS_SHARED_CREDENTIALS_FILE': missing,
                'HTTPS_PROXY': 'http://env-proxy.invalid:3128', 'https_proxy': 'http://env-proxy.invalid:3128',
                # Settings that would otherwise steer boto3 to hosts the proxy does not allow:
                'AWS_STS_REGIONAL_ENDPOINTS': 'legacy', 'AWS_USE_DUALSTACK_ENDPOINT': 'true',
                'AWS_USE_FIPS_ENDPOINT': 'true', 'AWS_ENDPOINT_URL': 'https://endpoint-override.invalid',
                'AWS_ENDPOINT_URL_STS': 'https://sts-override.invalid',
                'AWS_DEFAULT_REGION': 'us-east-1', 'AWS_REGION': 'us-east-1'}

    def test_github_reader_tunnels_only_through_the_configured_proxy(self):
        with patch.dict(os.environ, {'https_proxy': 'http://env-proxy.invalid:3128'}):
            reader = TokenRepoReader(self.github_token, proxy=self.recorder.url)
            with self.assertRaisesRegex(ValueError, 'github_read_failed'):
                reader.get('repos/joungGo/c03-execution-lab/git/ref/heads/main')
        self.assertEqual(self.recorder.lines, ['CONNECT api.github.com:443 HTTP/1.1'])
        for bad in ('https://proxy:8080', 'http://proxy', 'http://user:pw@proxy:8080', 'http://Proxy:8080'):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'egress_proxy_rejected'):
                TokenRepoReader(self.github_token, proxy=bad)

    def test_without_a_configured_proxy_environment_proxies_are_still_ignored(self):
        from urllib.request import ProxyHandler
        with patch.dict(os.environ, {'https_proxy': 'http://env-proxy.invalid:3128',
                                     'HTTPS_PROXY': 'http://env-proxy.invalid:3128'}):
            reader = TokenRepoReader(self.github_token)
        self.assertEqual([h for h in reader.opener.handlers if isinstance(h, ProxyHandler) and h.proxies], [])

    def test_sts_web_identity_and_ecr_go_through_the_proxy_to_pinned_regional_hosts(self):
        from botocore.credentials import Credentials
        from ai_ops.recovery_proof_live import irsa_clients
        with patch.dict(os.environ, self.irsa_env()):
            sts, ecr = irsa_clients(ROLE, proxy=self.recorder.url)
            with self.assertRaises(Exception):
                sts.get_caller_identity()  # triggers the provider's AssumeRoleWithWebIdentity first
            ecr._request_signer._credentials = Credentials('AKIAFAKEFORTESTONLY0', 'fake')  # isolate ECR path
            with self.assertRaises(Exception):
                ecr.describe_images(registryId='764428911194', repositoryName='urlshortener',
                                    imageIds=[{'imageDigest': 'sha256:' + 'a' * 64}])
        self.assertEqual(sorted(set(self.recorder.lines)),
                         ['CONNECT api.ecr.ap-northeast-2.amazonaws.com:443 HTTP/1.1',
                          'CONNECT sts.ap-northeast-2.amazonaws.com:443 HTTP/1.1'])
        self.assertEqual((sts.meta.endpoint_url, ecr.meta.endpoint_url),
                         ('https://sts.ap-northeast-2.amazonaws.com', 'https://api.ecr.ap-northeast-2.amazonaws.com'))
        with patch.dict(os.environ, self.irsa_env()), self.assertRaisesRegex(ValueError, 'egress_proxy_rejected'):
            irsa_clients(ROLE, proxy='http://proxy')

    def test_ecr_checker_uses_its_proxy_on_first_use(self):
        from ai_ops.recovery_proof_live import EcrImageChecker
        checker = EcrImageChecker(role_name=ROLE, now=lambda: 0, proxy=self.recorder.url)
        with patch.dict(os.environ, self.irsa_env()), self.assertRaisesRegex(ValueError, 'ecr_caller_read_failed'):
            checker.check('sha256:' + 'b' * 64, 'sha256:' + 'c' * 64, deadline=10)
        self.assertEqual(self.recorder.lines[:1], ['CONNECT sts.ap-northeast-2.amazonaws.com:443 HTTP/1.1'])

    def test_prepare_config_proxy_reaches_both_readers(self):
        import test_recovery_proof_live as live
        fixture = live.LiveFixture()
        fixture.addCleanup, fixture.assertEqual = self.addCleanup, self.assertEqual
        fixture.setUp()
        fixture.config = fixture.config.model_copy(update={'prepare': fixture.config.prepare.model_copy(
            update={'egress_proxy': 'http://ai-ops-egress.ai-ops.svc:8080'})})
        fixture.github = None  # let the bridge build its own opener
        bridge = fixture.bridge()
        handlers = [h for h in bridge.reader.opener.handlers if getattr(h, 'proxies', None)]
        self.assertEqual([h.proxies for h in handlers], [{'https': 'http://ai-ops-egress.ai-ops.svc:8080'}])
        self.assertEqual(bridge.read_proof.ecr.proxy, 'http://ai-ops-egress.ai-ops.svc:8080')
        base = fixture.config.prepare.model_dump()
        for bad in ('https://x:8080', 'http://x', 'ftp://x:21'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                PrepareConfig(**{**base, 'egress_proxy': bad})
