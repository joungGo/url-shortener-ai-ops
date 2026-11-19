"""Separate in-cluster Slack sender (WF-10 B'): the only process holding the bot token.

The sender Pod has no PVC, no Kubernetes token and no signing secret. It accepts
only the three fixed Slack Web API calls from the main Pod (authenticated by a
private client secret and limited by NetworkPolicy), re-validates the reply
contract against the Slack policy grants and forwards with the existing
``SlackWebTransport``. Nothing is sent at import or without the explicit gate.

Limits (documented, not hidden): slack.com itself remains a possible exfiltration
path for a compromised sender; the idempotency cache is in memory only; traffic
between the Pods is plain HTTP inside the cluster.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import re
import signal
import threading
import time
from urllib.parse import urlsplit

from ai_ops.contracts import sha256_json
from ai_ops.evidence import _json_object
from ai_ops.slack_adapter import BODY_LIMIT, SlackPolicy
from ai_ops.slack_web import METHODS, SlackWebTransport, post_payload_valid, read_private_secret
from ai_ops.workflow_config import read_config

PORT = 9446
REQUEST_LIMIT = 16384
RESPONSE_LIMIT = BODY_LIMIT + 1024
CACHE_SECONDS = 3600
CACHE_SIZE = 1024
RATE_PER_MINUTE = 60
BOT = re.compile(r'B[A-Z0-9]{2,31}\Z')
KEY = re.compile(r'[a-f0-9]{64}\Z')
ROUTES = {name: method for method, name in METHODS}


def payload_valid(policy, name, payload):
    if name == 'auth.test':
        return payload == {}
    if name == 'bots.info':
        return (isinstance(payload, dict) and set(payload) == {'bot'}
                and isinstance(payload['bot'], str) and bool(BOT.fullmatch(payload['bot'])))
    return post_payload_valid(policy, payload)


def response_valid(value):
    """Exactly the dict shapes SlackWebTransport._request returns."""
    if not isinstance(value, dict) or type(value.get('status')) is not int:
        return False
    if value['status'] == 200:
        return set(value) == {'status', 'body'} and isinstance(value['body'], dict)
    if value['status'] == 429:
        return (set(value) == {'status', 'retry_after'} and type(value['retry_after']) is int
                and 1 <= value['retry_after'] <= 300)
    return value == {'status': 503}


class SenderApp:
    """Transport-independent request handling so the contract is testable offline."""

    def __init__(self, policy, transport, client_secret, *, monotonic=time.monotonic):
        self.policy = SlackPolicy.model_validate(policy).model_copy(deep=True)
        if not isinstance(transport, SlackWebTransport) or transport.team_id != self.policy.team_id:
            raise ValueError('slack_sender_transport_rejected')
        self.transport = transport
        self._secret = b'Bearer ' + client_secret
        self._clock = monotonic
        self._cache = OrderedDict()
        self._window = []
        self._lock = threading.Lock()

    def _limited(self, now):
        self._window = [t for t in self._window if now - t < 60]
        if len(self._window) >= RATE_PER_MINUTE:
            return True
        self._window.append(now)
        return False

    def handle(self, path, authorization, idempotency_key, raw):
        """Return (http_status, response_dict). Never echoes request content."""
        if not hmac.compare_digest((authorization or '').encode('latin-1', 'replace'), self._secret):
            return 401, {'status': 'unauthorized'}
        name = path.removeprefix('/v1/') if path.startswith('/v1/') else None
        if name not in ROUTES:
            return 404, {'status': 'not_found'}
        try:
            request = _json_object(raw)
        except (ValueError, TypeError, RecursionError):
            return 400, {'status': 'rejected'}
        if not isinstance(request, dict) or set(request) != {'payload'}:
            return 400, {'status': 'rejected'}
        payload = request['payload']
        if not payload_valid(self.policy, name, payload):
            return 400, {'status': 'rejected'}
        key = None
        if name == 'chat.postMessage':
            # The key is bound to the exact payload: a retry of the same reply
            # returns the first outcome instead of posting twice.
            if not isinstance(idempotency_key, str) or not KEY.fullmatch(idempotency_key) \
                    or idempotency_key != sha256_json(payload):
                return 400, {'status': 'rejected'}
            key = idempotency_key
        with self._lock:
            now = self._clock()
            for k, (at, _) in list(self._cache.items()):
                if now - at >= CACHE_SECONDS or now < at:
                    del self._cache[k]
            if key is not None and key in self._cache:
                return 200, self._cache[key][1]
            if self._limited(now):
                return 200, {'status': 429, 'retry_after': 60}
            result = self.transport._request(ROUTES[name], name, payload)
            if not response_valid(result):
                result = {'status': 503}
            # 429 is a definite non-send; 200/503 may have had a side effect.
            if key is not None and result['status'] != 429:
                self._cache[key] = (now, result)
                while len(self._cache) > CACHE_SIZE:
                    self._cache.popitem(last=False)
            return 200, result


class SenderHandler(BaseHTTPRequestHandler):
    server_version = 'ai-ops-slack-sender'
    sys_version = ''
    timeout = 5

    def log_message(self, *args):
        # No request line, path, header or body in logs.
        pass

    def _reply(self, status, value):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = self.headers.get('Content-Length', '')
        if not re.fullmatch(r'[0-9]{1,5}', length) or not 0 < int(length) <= REQUEST_LIMIT:
            return self._reply(400, {'status': 'rejected'})
        raw = self.rfile.read(int(length))
        status, value = self.server.app.handle(self.path, self.headers.get('Authorization'),
                                               self.headers.get('Idempotency-Key'), raw)
        self._reply(status, value)

    def _reject(self):
        self._reply(405, {'status': 'method_not_allowed'})

    do_GET = do_PUT = do_DELETE = do_PATCH = _reject


class SenderServer(HTTPServer):
    # Single-threaded on purpose: serial posts keep the idempotency check atomic.
    request_queue_size = 8

    def __init__(self, app, address=('0.0.0.0', PORT)):
        self.app = app
        super().__init__(address, SenderHandler)


class SlackSenderClient(SlackWebTransport):
    """Main-Pod transport: same identity/payload/response checks, no bot token.

    Subclasses SlackWebTransport so the existing adapter gate still applies.
    Any failure is reported as 503, which the adapter keeps as needs_review and
    never reposts.
    """

    def __init__(self, policy, sender, *, connection_factory=None, monotonic=time.monotonic):
        self._policy = SlackPolicy.model_validate(policy).model_copy(deep=True)
        self.team_id = self._policy.team_id
        self.app_id = self._policy.app_id
        self.bot_user_id = self._policy.bot_user_id
        self._policy_hash = sha256_json(self._policy.model_dump())
        self._secret = read_private_secret(sender.client_secret_file).decode('ascii')
        url = urlsplit(sender.url)
        if url.scheme != 'http' or not url.hostname or not url.port or url.path not in ('', '/'):
            raise ValueError('slack_sender_url_rejected')
        self._host, self._port = url.hostname, url.port
        self._connect = connection_factory or http.client.HTTPConnection
        self._clock = monotonic
        self._verified_at = None
        self._bot = None

    def _request(self, method, name, payload=None):
        if (method, name) not in METHODS:
            raise ValueError('slack_method_rejected')
        payload = payload or {}
        conn = None
        try:
            body = json.dumps({'payload': payload}, ensure_ascii=False, allow_nan=False).encode()
            headers = {'Authorization': 'Bearer ' + self._secret,
                       'Content-Type': 'application/json', 'Accept': 'application/json'}
            if name == 'chat.postMessage':
                headers['Idempotency-Key'] = sha256_json(payload)
            # Longer than the sender's own 3s Slack timeout so its answer can arrive.
            conn = self._connect(self._host, port=self._port, timeout=8.0)
            conn.request('POST', '/v1/' + name, body=body, headers=headers)
            response = conn.getresponse()
            if response.status != 200:
                return {'status': 503}
            raw = response.read(RESPONSE_LIMIT + 1)
            if len(raw) > RESPONSE_LIMIT:
                return {'status': 503}
            value = _json_object(raw)
            return value if response_valid(value) else {'status': 503}
        except Exception:
            # Exceptions may embed secrets/headers/bodies; keep no strings.
            return {'status': 503}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def main(argv=None):
    parser = argparse.ArgumentParser(description='In-cluster Slack sender (explicit opt-in)')
    parser.add_argument('--enable-web', action='store_true')
    parser.add_argument('--policy', default='/etc/ai-ops/slack-policy.json')
    parser.add_argument('--token-file', default='/run/slack-bot/token')
    parser.add_argument('--client-secret-file', default='/run/slack-sender-client/token')
    args = parser.parse_args(argv)
    try:
        if not args.enable_web:
            raise ValueError('slack_explicit_enable_required')
        from pathlib import Path
        policy = SlackPolicy.model_validate(read_config(Path(args.policy)))
        transport = SlackWebTransport(policy, args.token_file, enabled=True)
        app = SenderApp(policy, transport, read_private_secret(args.client_secret_file))
        server = SenderServer(app)
        stopped = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stopped.set())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print('{"status":"slack_sender_started"}', flush=True)
        stopped.wait()
        server.shutdown()
        server.server_close()
        return 0
    except Exception:
        print('{"status":"slack_sender_rejected"}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
