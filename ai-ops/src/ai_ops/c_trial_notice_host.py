"""안내와 실행기 출력을 함께 기다린다. 실행기 대기가 안내 채널을 막지 않는다.

이 모듈은 native 도구를 호출하거나 성공 응답/리뷰를 생성하지 않는다.
호스트는 출력된 질문을 실제 도구로 전송한 뒤 그 응답만 stdin으로 돌려준다. 정확한 {"accepted":true} 응답은 호스트가
기존 delivery_tool_response 형식으로 감싼다. 실제 도구 호출 없이 성공을 만들지 않는다.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

from ai_ops.c_trial_notice import LIMIT, delivery_evidence


class HostTrace:
    def __init__(self, *, monotonic=time.monotonic, clock=lambda: datetime.now(timezone.utc)):
        self.monotonic, self.clock = monotonic, clock
        self.started = monotonic()
        self.events = []
        self.sink = None

    def mark(self, stage, **fields):
        if len(self.events) < 32:
            self.events.append({'stage':stage, 'at':self.clock().isoformat(),
                                'elapsed_seconds':self.monotonic()-self.started, **fields})
            if self.sink is not None:
                self.sink(self.report())

    def report(self):
        by = {e['stage']:e for e in self.events}
        durations = {}
        for name, first, last in (
            ('host_response_wait_seconds','notice_forwarded','host_response_forwarded'),
            ('host_response_arrival_seconds','notice_forwarded','host_response_received'),
            ('host_response_validation_seconds','host_response_received','host_response_forwarded'),
            ('bridge_response_observation_seconds','host_response_forwarded','bridge_ack_received')):
            if first in by and last in by:
                durations[name] = by[last]['elapsed_seconds']-by[first]['elapsed_seconds']
        if 'notice_forwarded' in by:
            event = by['notice_forwarded']
            delta = (datetime.fromisoformat(event['at'])-
                     datetime.fromisoformat(event['bridge_emitted_at'])).total_seconds()
            durations['bridge_emit_to_forward_wall_seconds'] = delta
        return {'record_type':'c4_notice_host_timing','execution_allowed':False,
                'events':self.events, 'durations':durations,
                'native_delivery_or_human_review_proven':False}


class Lines:
    def __init__(self):
        self.pending = b''

    def feed(self, raw):
        self.pending += raw
        lines = []
        while b'\n' in self.pending:
            line, self.pending = self.pending.split(b'\n',1)
            if len(line)+1 > LIMIT:
                raise ValueError('notice_host_line_too_large')
            lines.append(line)
        if len(self.pending) >= LIMIT:
            raise ValueError('notice_host_line_too_large')
        return lines

    def eof(self):
        if self.pending:
            raise ValueError('notice_host_partial_line')


def emit(stream, value):
    stream.write(json.dumps(value,ensure_ascii=False)+'\n')
    stream.flush()


def host_delivery_evidence(raw):
    """Accept the exact native tool response on host stdin, never a review approval.

    Only the host boundary wraps the observed response. The bridge/receipt contract
    stays strict; unsolicited/duplicate responses are rejected by Router first.
    """
    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('notice_host_duplicate_response_field')
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=unique_pairs)
    if not isinstance(value, dict):
        raise ValueError('notice_host_response_object_required')
    if set(value) == {'accepted'} and value['accepted'] is True:
        value = {'delivery_tool_response': value}
    return delivery_evidence(value)


class Router:
    """한 소유 bridge의 질문/응답만 통과시킨다. runner는 질문을 가장할 수 없다."""
    def __init__(self, bridge_input, output, trace):
        self.bridge_input, self.output, self.trace = bridge_input, output, trace
        self.requested = self.responded = False

    def bridge_line(self, raw):
        event = json.loads(raw)
        if not isinstance(event,dict):
            raise ValueError('notice_host_bridge_event_rejected')
        if event.get('status') == 'review_notice_requested':
            if self.requested or event.get('tool') != 'functions.request_user_input_async':
                raise ValueError('notice_host_request_rejected')
            if set(event) != {'status','tool','arguments','emitted_at'}:
                raise ValueError('notice_host_request_rejected')
            self.requested = True
            self.trace.mark('notice_forwarded',bridge_emitted_at=event['emitted_at'])
            emit(self.output,event)
        elif event.get('status') == 'review_notice_host_ack_received' and self.responded:
            self.trace.mark('bridge_ack_received',bridge_received_at=event['received_at'])
        else:
            raise ValueError('notice_host_bridge_event_rejected')

    def runner_line(self, raw):
        # 진행 로그가 stdout을 채워 안내를 다시 막지 않게 준비 이벤트만 통과시킨다.
        try:
            event = json.loads(raw)
        except (ValueError,UnicodeError):
            return
        if (isinstance(event,dict) and event.get('status') == 'ready_awaiting_actual_approval'
                and set(event) == {'status','trial_id','intent_hash','readiness_hash','expires_at'}):
            emit(self.output,event)

    def host_line(self, raw):
        if not self.requested or self.responded:
            raise ValueError('notice_host_unsolicited_response')
        self.trace.mark('host_response_received')
        value = host_delivery_evidence(raw)
        self.bridge_input.write(json.dumps(value).encode()+b'\n')
        self.bridge_input.flush()
        self.responded = True
        self.trace.mark('host_response_forwarded')


def wait_bound(bridge, timeout=5):
    lines = Lines()
    end = time.monotonic()+timeout
    with selectors.DefaultSelector() as selector:
        selector.register(bridge.stdout,selectors.EVENT_READ)
        while time.monotonic() < end:
            if not selector.select(max(0,end-time.monotonic())):
                break
            raw = os.read(bridge.stdout.fileno(),4096)
            if not raw:
                break
            values = lines.feed(raw)
            if values:
                if len(values) != 1 or json.loads(values[0]) != {'status':'review_bridge_bound'} or lines.pending:
                    raise ValueError('notice_host_bridge_start_rejected')
                return
    raise ValueError('notice_host_bridge_not_bound')


def pump(bridge, runner, host_input, output, trace, *, cancel_owner=None):
    """동일 select에서 안내를 우선 처리하며 runner 완료를 기다려 안내를 읽지 않는다."""
    router = Router(bridge.stdin,output,trace)
    buffers = {key:Lines() for key in ('bridge','runner','host')}
    failed = False
    cancelled = False
    def cancel(signum=None, frame=None):
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            try:
                trace.mark('owner_cancellation_requested')
            finally:
                if cancel_owner is not None:
                    cancel_owner()
                elif runner.poll() is None:
                    # 기존 실행기의 SIGTERM 예외/소유 원복/finally 경로를 그대로 사용한다.
                    runner.send_signal(signal.SIGTERM)
    previous = {}
    try:
        for sig in (signal.SIGTERM,signal.SIGINT):
            previous[sig] = signal.signal(sig,cancel)
        with selectors.DefaultSelector() as selector:
            for source, stream in [('bridge',bridge.stdout),('runner',runner.stdout),('host',host_input)]:
                selector.register(stream,selectors.EVENT_READ,source)
            while runner.poll() is None:
                for key, _ in sorted(selector.select(.1),key=lambda item:
                                     {'bridge':0,'host':1,'runner':2}[item[0].data]):
                    source = key.data
                    try:
                        raw = os.read(key.fd,4096)
                        if not raw:
                            selector.unregister(key.fileobj)
                            buffers[source].eof()
                            if source == 'host' and not router.responded:
                                raise ValueError('notice_host_input_closed')
                            if source == 'bridge' and not router.responded:
                                raise ValueError('notice_host_bridge_closed_before_response')
                            continue
                        for line in buffers[source].feed(raw):
                            getattr(router,source+'_line')(line)
                    except (ValueError,KeyError,TypeError,OSError,UnicodeError):
                        failed = True
                        try: selector.unregister(key.fileobj)
                        except KeyError: pass
                        # ACK를 합성하지 않고 기존 실행기가 원복/회수하도록 취소를 전달한다.
                        try: trace.mark('host_channel_rejected',source=source)
                        finally: cancel()
            return runner.wait(), failed or cancelled
    finally:
        for sig, old in previous.items():
            signal.signal(sig,old)


def host_ready(args, argv, *, root, python, host_input, output):
    """고정된 기존 bridge/ready 실행기만 시작한다. 실행 승인 검증은 기존 실행기 소유다."""
    trace = HostTrace()
    bridge = runner = None
    if args.run_dir.exists() or args.run_dir.is_symlink():
        raise ValueError('notice_host_run_directory_already_exists')
    # 실행기 소유 디렉터리를 미리 만들거나 다른 실행의 파일을 덮어쓰지 않는다.
    from ai_ops.path_policy import permitted_path
    trace_path = permitted_path(args.run_dir.parent/'review-host-timing.jsonl','store')
    fd = os.open(trace_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    trace_stream = os.fdopen(fd,'w',encoding='utf-8')
    def persist(value):
        trace_stream.write(json.dumps(value,ensure_ascii=False)+'\n')
        trace_stream.flush(); os.fsync(trace_stream.fileno())
    trace.sink = persist
    stopped = [False]
    term_sent = [False]
    def stop(signum=None,frame=None):
        stopped[0] = True
        if runner is not None and runner.poll() is None and not term_sent[0]:
            term_sent[0] = True
            runner.send_signal(signal.SIGTERM)
    previous = {}
    try:
        for sig in (signal.SIGTERM,signal.SIGINT):
            previous[sig] = signal.signal(sig,stop)
        command = [python,str(root/'ai-ops/scripts/bridge_c4_review_notice.py'),
                   '--when-ready','--host-events','--plan',str(args.intent),
                   '--activation',str(args.activation),'--socket',args.review_notifier_socket]
        bridge = subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE)
        if stopped[0]: raise ValueError('notice_host_cancelled_before_runner')
        wait_bound(bridge)
        trace.mark('bridge_bound')
        if stopped[0]: raise ValueError('notice_host_cancelled_before_runner')
        runner = subprocess.Popen([python,str(root/'ai-ops/scripts/run_c4_ready_trial.py'),*argv],
                                  stdin=subprocess.DEVNULL,stdout=subprocess.PIPE)
        if stopped[0]: raise ValueError('notice_host_cancelled_during_runner_start')
        emit(output,{'status':'notice_host_started','timing_path':str(trace_path)})
        code, failed = pump(bridge,runner,host_input,output,trace,cancel_owner=stop)
        trace.mark('runner_exited',returncode=code)
        if code:
            return code
        try:
            bridge_code = bridge.wait(timeout=5)
            trace.mark('bridge_exited',returncode=bridge_code)
            failed = failed or bridge_code != 0
        except subprocess.TimeoutExpired:
            trace.mark('bridge_exit_not_observed')
            failed = True
        return int(failed)
    finally:
        try:
            if runner is not None and runner.poll() is None:
                stop()
                runner.wait()  # 소유 원복을 강제 kill/새 시간제한으로 자르지 않는다.
            if bridge is not None:
                if bridge.poll() is None:
                    bridge.terminate()
                    try: bridge.wait(timeout=5)
                    except subprocess.TimeoutExpired: bridge.kill();bridge.wait()
                for stream in (bridge.stdin,bridge.stdout):
                    try: stream.close()
                    except OSError: pass
            if runner is not None:
                runner.stdout.close()
        finally:
            try:
                trace.mark('host_closed')
            finally:
                try: trace_stream.close()
                finally:
                    for sig, handler in previous.items():
                        signal.signal(sig,handler)
