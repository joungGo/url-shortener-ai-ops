"""WF-10 B' separate Slack sender: offline doubles only, Slack itself is never contacted."""
import asyncio
import hashlib
import hmac
import http.client
import io
import json
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from ai_ops.contracts import sha256_json
from ai_ops.slack_adapter import SlackPolicy
from ai_ops.slack_runtime import SlackRuntime, SlackWebConfig
from ai_ops.slack_sender import (RATE_PER_MINUTE, SenderApp, SenderServer, SlackSenderClient,
                                 main as sender_main)
from ai_ops.slack_web import FLAGS, SlackWebTransport
from test_slack_web import Response, SIGNING, TOKEN, WebFixture, Wire, invoke

CLIENT = b'abcdef0123456789abcdef0123456789'
URL = 'http://wf10-fixture-sender.ai-ci.svc:9446'


class Bridge:
    """Client connection factory routed to SenderApp without sockets."""

    def __init__(self, app):
        self.app, self.requests, self.override = app, [], None

    def connect(self, host, port=None, timeout=None):
        bridge = self
        bridge.target = (host, port, timeout)

        class Connection:
            def request(self, method, path, body=None, headers=None):
                bridge.requests.append({'method': method, 'path': path, 'headers': dict(headers), 'body': body})
                self.result = (bridge.app.handle(path, headers.get('Authorization'),
                                                 headers.get('Idempotency-Key'), body)
                               if method == 'POST' else (405, {'status': 'method_not_allowed'}))

            def getresponse(self):
                if bridge.override is not None:
                    return bridge.override
                status, value = self.result
                return Response(status=status, body=value)

            def close(self):
                pass
        return Connection()


class SenderFixture(WebFixture):
    def setUp(self):
        super().setUp()
        self.client_file = self.queue.root / 'fake-sender-client'
        self.client_file.write_bytes(CLIENT)
        self.client_file.chmod(0o600)
        # The sender owns the only bot-token transport (fake Slack wire).
        self.app = SenderApp(self.policy, self.web, CLIENT, monotonic=lambda: self.now)
        self.bridge = Bridge(self.app)
        self.config = SlackWebConfig(enabled=True, sender={'url': URL, 'client_secret_file': str(self.client_file)},
                                     signing_secret_file=str(self.secret_file))

    def client(self):
        return SlackSenderClient(self.policy, self.config.sender, connection_factory=self.bridge.connect,
                                 monotonic=lambda: self.now)

    def call(self, name, payload, *, auth=None, key=None):
        auth = 'Bearer ' + CLIENT.decode() if auth is None else auth
        return self.app.handle('/v1/' + name, auth, key, json.dumps({'payload': payload}).encode())


class SenderRelayTest(SenderFixture, unittest.TestCase):
    def test_relay_delivers_through_sender_without_reading_bot_token(self):
        self.token_file.rename(self.queue.root / 'moved-away')  # main Pod has no bot token
        runtime = SlackRuntime(self.tasks, self.policy, self.config, connection_factory=self.bridge.connect,
                               monotonic=lambda: self.now)
        self.assertIsInstance(runtime.transport, SlackSenderClient)
        headers, body = self.signed()
        headers['X-Slack-Signature'] = 'v0=' + hmac.new(
            SIGNING, b'v0:' + headers['X-Slack-Request-Timestamp'].encode() + b':' + body, hashlib.sha256).hexdigest()
        status, _ = asyncio.run(invoke(runtime.app, headers, body))
        self.assertEqual(status, 200)
        runtime.prepare()
        self.assertEqual(runtime.relay_once()['reply']['state'], 'idle')  # admits inbox to queue
        self.fixture.worker().run_once()
        result = runtime.relay_once()['reply']
        self.assertEqual(result['state'], 'delivered')
        self.assertFalse(result['execution_allowed'])
        self.assertEqual(len(self.wire.posts()), 1)
        self.assertEqual(self.bridge.target, ('wf10-fixture-sender.ai-ci.svc', 9446, 8.0))
        sent = repr(self.bridge.requests)
        self.assertNotIn(TOKEN.decode(), sent)  # bot token never crosses Pods
        self.assertIn('Bearer ' + TOKEN.decode(), json.dumps([c['headers'] for c in self.wire.calls]))
        self.assertNotIn('private-secret-value', self.wire.posts()[0]['body']['text'])

    def test_same_reply_retry_returns_first_outcome_without_reposting(self):
        client = self.client()
        client.verify_identity()
        payload = {'channel': 'CLOCAL01', 'thread_ts': '1000.000001', 'text': '로컬 결과', **FLAGS}
        first = client.post_message(dict(payload))
        second = client.post_message(dict(payload))
        self.assertEqual(first, second)
        self.assertEqual(first['status'], 200)
        self.assertEqual(len(self.wire.posts()), 1)

    def test_rate_limited_and_ambiguous_outcomes(self):
        payload = {'channel': 'CLOCAL01', 'thread_ts': '1000.000001', 'text': '재시도', **FLAGS}
        key = sha256_json(payload)
        self.wire.outcomes.append(Response(status=429, headers={'Retry-After': '5'}, raw=b''))
        self.assertEqual(self.call('chat.postMessage', payload, key=key), (200, {'status': 429, 'retry_after': 5}))
        # 429 is a definite non-send: not cached, so the retry is forwarded.
        self.assertEqual(self.call('chat.postMessage', payload, key=key)[1]['status'], 200)
        payload['text'] = '모호'
        key = sha256_json(payload)
        self.wire.outcomes.append(OSError('reset'))
        self.assertEqual(self.call('chat.postMessage', payload, key=key), (200, {'status': 503}))
        # Ambiguous outcome is cached: never reposted within the window.
        self.assertEqual(self.call('chat.postMessage', payload, key=key), (200, {'status': 503}))
        self.assertEqual(len(self.wire.posts()), 3)

    def test_global_rate_limit(self):
        for _ in range(RATE_PER_MINUTE):
            self.assertEqual(self.call('auth.test', {})[1]['status'], 200)
        self.assertEqual(self.call('auth.test', {}), (200, {'status': 429, 'retry_after': 60}))
        self.now += 61
        self.assertEqual(self.call('auth.test', {})[1]['status'], 200)


class SenderContractTest(SenderFixture, unittest.TestCase):
    def test_rejections_never_reach_slack(self):
        good = {'channel': 'CLOCAL01', 'thread_ts': '1000.000001', 'text': 'x', **FLAGS}
        cases = [
            (self.call('auth.test', {}, auth='Bearer ' + 'f' * 32), 401),
            (self.call('auth.test', {}, auth=''), 401),
            (self.call('conversations.history', {}), 404),
            (self.app.handle('/api/auth.test', 'Bearer ' + CLIENT.decode(), None, b'{"payload":{}}'), 404),
            (self.call('auth.test', {'x': 1}), 400),
            (self.call('bots.info', {'bot': 'not-a-bot'}), 400),
            (self.call('chat.postMessage', {**good, 'channel': 'COTHER01'}, key=sha256_json({**good, 'channel': 'COTHER01'})), 400),
            (self.call('chat.postMessage', {**good, 'mrkdwn': True}, key=sha256_json({**good, 'mrkdwn': True})), 400),
            (self.call('chat.postMessage', {**good, 'extra': 1}, key=sha256_json({**good, 'extra': 1})), 400),
            (self.call('chat.postMessage', good), 400),
            (self.call('chat.postMessage', good, key='0' * 64), 400),
            (self.app.handle('/v1/auth.test', 'Bearer ' + CLIENT.decode(), None, b'{"payload":{},"payload":{}}'), 400),
            (self.app.handle('/v1/auth.test', 'Bearer ' + CLIENT.decode(), None, b'not json'), 400),
            (self.app.handle('/v1/auth.test', 'Bearer ' + CLIENT.decode(), None, b'{"payload":{},"x":1}'), 400),
        ]
        for (status, value), expected in cases:
            self.assertEqual(status, expected)
            self.assertNotIn('payload', json.dumps(value))
        self.assertEqual(self.wire.calls, [])

    def test_client_failures_become_503_and_never_raise(self):
        client = self.client()
        for override in (Response(status=500, body={}), Response(raw=b'x' * 70000),
                         Response(body={'status': 200}), Response(body={'status': 429, 'retry_after': 0}),
                         Response(body={'status': 503, 'leak': 'x'}), Response(raw=b'not json')):
            self.bridge.override = override
            self.assertEqual(client._request('POST', 'auth.test'), {'status': 503})
        self.bridge.override = None

        def broken(*args, **kwargs):
            raise OSError('connection refused with secret-looking text')
        broken_client = SlackSenderClient(self.policy, self.config.sender, connection_factory=broken)
        self.assertEqual(broken_client._request('POST', 'auth.test'), {'status': 503})
        with self.assertRaisesRegex(ValueError, 'method_rejected'):
            client._request('POST', 'conversations.history')

    def test_sender_requires_bot_transport_and_matching_policy(self):
        with self.assertRaises(ValueError):
            SenderApp(self.policy, object(), CLIENT)
        other = SlackPolicy.model_validate({**self.policy.model_dump(), 'team_id': 'TOTHER01'})
        with self.assertRaises(ValueError):
            SenderApp(other, self.web, CLIENT)

    def test_config_exactly_one_mode_and_absolute_paths(self):
        for kwargs in ({}, {'bot_token_file': '/run/slack-bot/token',
                            'sender': {'url': URL, 'client_secret_file': '/run/c/token'}}):
            with self.assertRaises(ValueError):
                SlackWebConfig(signing_secret_file='/run/s/token', **kwargs)
        for url in ('https://sender:9446', 'http://sender', 'http://Sender:9446', 'http://sender:9446/path',
                    'http://sender:0'):
            with self.assertRaises(ValueError):
                SlackWebConfig(signing_secret_file='/x', sender={'url': url, 'client_secret_file': '/c'})
        path = self.queue.root / 'slack-web.json'
        path.write_text(json.dumps({'schema_version': 1, 'enabled': True, 'signing_secret_file': '/s',
                                    'sender': {'url': URL, 'client_secret_file': 'relative'}}))
        with self.assertRaisesRegex(ValueError, 'config_rejected'):
            SlackWebConfig.load(path)
        path.write_text(json.dumps({'schema_version': 1, 'enabled': True, 'signing_secret_file': '/s',
                                    'sender': {'url': URL, 'client_secret_file': '/c'}}))
        self.assertEqual(SlackWebConfig.load(path).sender.url, URL)


class SenderServerLoopbackTest(unittest.TestCase):
    """Real HTTP handling on 127.0.0.1; the inner Slack transport is a fake wire."""

    def setUp(self):
        from test_slack_adapter import fixtures
        import tempfile
        from pathlib import Path
        self.policy = SlackPolicy.model_validate_json((fixtures.ROOT / 'config/slack-local.example.json').read_bytes())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        token = Path(tmp.name) / 'token'
        token.write_bytes(TOKEN)
        token.chmod(0o600)
        self.wire = Wire(self.policy)
        transport = SlackWebTransport(self.policy, token, enabled=True, connection_factory=self.wire.connect)
        self.server = SenderServer(SenderApp(self.policy, transport, CLIENT), ('127.0.0.1', 0))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_http_contract(self):
        auth = {'Authorization': 'Bearer ' + CLIENT.decode(), 'Content-Type': 'application/json'}
        status, value = self.request('POST', '/v1/auth.test', b'{"payload":{}}', auth)
        self.assertEqual(status, 200)
        self.assertEqual(value['status'], 200)
        self.assertEqual(self.request('GET', '/v1/auth.test')[0], 405)
        self.assertEqual(self.request('POST', '/v1/auth.test', b'', auth)[0], 400)
        self.assertEqual(self.request('POST', '/v1/auth.test', b'x' * 20000, auth)[0], 400)
        self.assertEqual(len(self.wire.calls), 1)


class SenderMainTest(unittest.TestCase):
    def test_explicit_gate_and_no_path_echo(self):
        for argv in ([], ['--enable-web', '--policy', '/nonexistent/secret-path.json']):
            output = io.StringIO()
            with redirect_stdout(output), patch('ai_ops.slack_sender.SenderServer',
                                                side_effect=AssertionError('no server')):
                self.assertEqual(sender_main(argv), 1)
            self.assertEqual(output.getvalue().strip(), '{"status":"slack_sender_rejected"}')


if __name__ == '__main__':
    unittest.main()
