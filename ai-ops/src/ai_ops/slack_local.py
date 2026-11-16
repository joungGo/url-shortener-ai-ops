"""Slack 서명 HTTP 요청을 로컬 loopback에서만 재현한다. 외부 서비스 연결은 없다."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import sqlite3

from ai_ops.slack_adapter import BODY_LIMIT


class SlackLocalHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _respond(self, status, value, *, no_retry=False):
        body=json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close')
        if no_retry:self.send_header('X-Slack-No-Retry','1')
        self.end_headers()
        try:self.wfile.write(body)
        except OSError:pass
        self.close_connection=True

    def do_POST(self):
        self.connection.settimeout(1)
        try:
            lengths=self.headers.get_all('Content-Length',[])
            if (self.path!='/slack/events' or len(lengths)!=1 or not lengths[0].isdigit()
                    or self.headers.get_all('Transfer-Encoding') or not 0<int(lengths[0])<=BODY_LIMIT):
                raise ValueError('slack_http_request_rejected')
            body=self.rfile.read(int(lengths[0]))
            if len(body)!=int(lengths[0]):raise ValueError('slack_http_body_rejected')
            value=self.server.adapter.receive(list(self.headers.raw_items()),body)
        except (OSError,sqlite3.Error):
            self._respond(503,{'status':'unavailable'});return
        except (ValueError,TypeError,KeyError,RecursionError):
            self._respond(403,{'status':'rejected'},no_retry=True);return
        self._respond(200,value)

    def do_GET(self):
        self._respond(405,{'status':'rejected'})


class SlackLocalServer(HTTPServer):
    """어댑터 검증 전용이다. 주소 인자를 받지 않고 127.0.0.1에서만 기다린다."""
    def __init__(self,adapter):
        self.adapter=adapter
        super().__init__(('127.0.0.1',0),SlackLocalHandler)

    def handle_error(self,*_):
        pass
