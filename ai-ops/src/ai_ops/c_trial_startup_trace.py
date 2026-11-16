"""시작 경로의 단계·소요 시간만 메모리에 기록한다. 요청·자격·오류 본문은 받지 않는다."""

from contextlib import contextmanager, nullcontext
import re
import threading
import time


class StartupTrace:
    def __init__(self, *, clock=time.perf_counter):
        self.clock = clock
        self.lock = threading.Lock()
        self.origin = self.end = None
        self.critical_origin = None
        self.outcome = None
        self.events = []
        self.dropped = 0

    def begin(self):
        with self.lock:
            if self.origin is None:
                self.origin = self.clock()

    @contextmanager
    def stage(self, name):
        if not re.fullmatch(r'[a-z][a-z0-9_.]{0,79}', name):
            raise ValueError('startup_trace_stage_rejected')
        start = self.clock()
        status = 'ok'
        try:
            yield
        except BaseException:
            status = 'error'
            raise
        finally:
            stop = self.clock()
            with self.lock:
                if self.origin is not None and self.end is None:
                    if len(self.events) < 128:
                        self.events.append(dict(stage=name, status=status,
                            started_ms=round((start-self.origin)*1000, 3),
                            duration_ms=round((stop-start)*1000, 3)))
                    else:
                        self.dropped += 1

    def finish(self, outcome):
        if outcome not in {'first_write_ready', 'rejected', 'not_started', 'read_only_probe'}:
            raise ValueError('startup_trace_outcome_rejected')
        with self.lock:
            if self.end is None:
                self.end = self.clock()
                self.outcome = outcome if self.origin is not None else 'not_started'

    def critical_start(self):
        """실행 기한을 한 번 고정한 경계만 기록한다. 보안 시계/기한을 제어하지 않는다."""
        with self.lock:
            if self.origin is None or self.critical_origin is not None or self.end is not None:
                raise ValueError('startup_trace_boundary_rejected')
            self.critical_origin = self.clock()
            if len(self.events) < 128:
                self.events.append(dict(stage='start.boundary', status='ok',
                    started_ms=round((self.critical_origin-self.origin)*1000, 3), duration_ms=0.))
            else:
                self.dropped += 1

    def reject_before_write(self):
        """진단 종료 뒤 최종 시각 검사도 실패하면 성공 후보 표식을 남기지 않는다."""
        with self.lock:
            self.end = self.clock()
            self.outcome = 'rejected' if self.origin is not None else 'not_started'

    def report(self):
        with self.lock:
            return dict(record_type='c04_startup_timing', schema_version=1,
                outcome=self.outcome, started=self.origin is not None,
                elapsed_ms=None if self.origin is None or self.end is None else
                    round((self.end-self.origin)*1000, 3),
                critical_elapsed_ms=None if self.critical_origin is None or self.end is None else
                    round((self.end-self.critical_origin)*1000, 3),
                events=sorted((dict(v) for v in self.events), key=lambda v: v['started_ms']),
                dropped_events=self.dropped)


def stage(owner, name):
    trace = getattr(owner, 'startup_trace', None)
    return trace.stage(name) if isinstance(trace, StartupTrace) else nullcontext()
