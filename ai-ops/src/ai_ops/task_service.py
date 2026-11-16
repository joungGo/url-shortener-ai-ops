"""사용자 전용 Unix socket의 로컬 요청·조회·결과 API. 외부 포트는 열지 않는다."""

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import sqlite3
import stat
import threading

from ai_ops.b_contracts import _json_object, load_b_settings
from ai_ops.contracts import canonical_json_bytes
from ai_ops.job_queue import JobQueue
from ai_ops.path_policy import cli_paths
from ai_ops.tasks import TaskService


LIMIT = 65536


def dispatch(service, value):
    if not isinstance(value, dict) or not isinstance(value.get('action'), str):
        raise ValueError('task_action_rejected')
    action = value['action']
    if action == 'submit' and set(value) == {'action', 'request'}:
        return service.submit(value['request'])
    readers = {'show': service.show, 'result': service.result, 'deliver': service.deliver,
               'reconcile-delivery': service.reconcile_delivery}
    if action in readers and set(value) == {'action', 'task_id'}:
        return readers[action](value['task_id'])
    if action == 'prune' and set(value) == {'action'}:
        return service.prune()
    raise ValueError('task_action_rejected')


def socket_path(root):
    path = root / 'tasks' / 'service.sock'
    if len(os.fsencode(path)) > 90 or path.is_symlink() or path.parent.is_symlink():
        raise ValueError('task_socket_path_rejected')
    info = path.parent.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('task_socket_directory_rejected')
    return path


class TaskHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(3)
        try:
            raw = self.rfile.readline(LIMIT + 1)
            if len(raw) > LIMIT or not raw.endswith(b'\n'):
                raise ValueError('task_message_rejected')
            value = dispatch(self.server.service, _json_object(raw))
            reply = canonical_json_bytes({'status': 'ok', 'value': value})
            if len(reply) > LIMIT:
                raise ValueError('task_reply_too_large')
        except (OSError, ValueError, TypeError, KeyError, RecursionError, sqlite3.Error):
            # 본문·질문·설정·개인 경로·내부 예외를 응답/로그에 반사하지 않는다.
            reply = b'{"status":"rejected"}'
        try:
            self.wfile.write(reply + b'\n')
        except OSError:
            pass  # 접수 응답이 유실돼도 같은 request_id 재시도로 조정한다.


class LocalTaskServer(socketserver.UnixStreamServer):
    def __init__(self, service):
        self.service = service
        path = socket_path(service.queue.root)
        if path.exists():
            raise ValueError('task_socket_already_exists')
        self.socket_identity = None
        # bind 시점부터 소켓 접근을 사용자로 제한한다. 기존 umask는 바로 복원한다.
        previous = os.umask(0o077)
        try:
            super().__init__(str(path), TaskHandler)
            self.socket_identity = (path.stat().st_dev, path.stat().st_ino)
            os.chmod(path, 0o600)
        except BaseException:
            if self.socket_identity is not None:
                self.server_close()
            raise
        finally:
            os.umask(previous)

    def handle_error(self, *_):
        pass

    def server_close(self):
        super().server_close()
        path = Path(self.server_address)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == self.socket_identity:
            path.unlink()


def request(root, body):
    path = socket_path(root)
    info = path.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError('task_socket_rejected')
    raw = canonical_json_bytes(body) + b'\n'
    if len(raw) > LIMIT:
        raise ValueError('task_message_too_large')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(path))
        with client.makefile('rwb') as stream:
            stream.write(raw); stream.flush()
            reply = stream.readline(LIMIT + 1)
    if len(reply) > LIMIT or not reply.endswith(b'\n'):
        raise ValueError('task_reply_rejected')
    return _json_object(reply)


def main(argv=None):
    parser = argparse.ArgumentParser(description='로컬 요청·작업 조회·결과 전달 (외부 통신/변경 실행 없음)')
    parser.add_argument('action', choices=('serve', 'submit', 'show', 'result', 'deliver', 'reconcile-delivery', 'prune'))
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--bindings', type=Path)
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--metric-queries', type=Path)
    parser.add_argument('--target')
    parser.add_argument('--question')
    parser.add_argument('--request-id')
    parser.add_argument('--conversation-id')
    parser.add_argument('--task-id')
    args = cli_paths(parser, parser.parse_args(argv), {'store': 'store', 'bindings': 'input',
                                                     'runtime': 'input', 'metric_queries': 'input'})
    try:
        if args.action == 'serve':
            if None in (args.bindings, args.runtime, args.metric_queries):
                parser.error('serve에는 bindings/runtime/metric-queries가 필요합니다')
            settings = load_b_settings(args.bindings, args.runtime, args.metric_queries)
            with LocalTaskServer(TaskService(JobQueue(args.store), settings)) as server:
                def stop(*_):
                    # serve_forever와 같은 스레드에서 shutdown을 호출해 교착하지 않는다.
                    threading.Thread(target=server.shutdown, daemon=True).start()
                signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
                server.serve_forever(poll_interval=0.1)
            return 0
        if args.action == 'submit':
            body = {'action': 'submit', 'request': {'request_id': args.request_id,
                    'conversation_id': args.conversation_id, 'target': args.target, 'question': args.question}}
        elif args.action == 'prune':
            body = {'action': 'prune'}
        else:
            body = {'action': args.action, 'task_id': args.task_id}
        result = request(args.store, body)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('status') == 'ok' else 2
    except (OSError, ValueError, TypeError, sqlite3.Error):
        print('{"status":"rejected"}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
