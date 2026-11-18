"""복구 읽기의 고정 단계·남은 예산만 기록한다. 원문/경로/자격은 받지 않는다."""

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar, copy_context
import threading
import time
import math

from ai_ops.contracts import _utc_datetime


_ACTIVE = ContextVar('c_recovery_read_timing', default=None)
_PHASES = frozenset({
    'writer.total', 'writer.identity', 'writer.scope_base', 'writer.commit_diff',
    'writer.branch', 'writer.commit', 'writer.pull', 'writer.pull_verify', 'writer.revoke',
    'writer.token', 'writer.jwt',
    'proposal.total', 'proposal.first', 'proposal.second', 'proposal.third',
    'proposal.assess', 'proposal.draft', 'proposal.link', 'proposal.render',
    'audit.command', 'audit.cluster', 'audit.account', 'audit.operator_identity',
    'audit.kubeconfig', 'audit.normal_image', 'audit.failure_image',
    'recovery.total', 'review.initial', 'merge.session', 'merge.open', 'github.app', 'preflight.locked',
    'merge.plan', 'merge.verify', 'sync.plan', 'sync.wait', 'handoff.verify',
    'source.before', 'source.audit', 'source.after', 'live.collect',
    'audit.initial', 'audit.values', 'audit.identity_history', 'audit.images_pods',
    'audit.replicaset', 'audit.incident', 'audit.final', 'transport.command',
    'github.immutable.fetch', 'github.immutable.cache', 'github.main',
    'github.pull', 'github.reviews', 'github.checks', 'github.protection',
    'github.identity', 'github.other', 'history.metadata', 'history.archive',
})


class RecoveryReadTiming:
    def __init__(self, deadline, *, clock, monotonic=None, read_deadline=None):
        self.clock = clock
        self.monotonic = monotonic or time.monotonic
        self.origin = self.monotonic()
        self.deadline = _utc_datetime(deadline)
        self.end = self.origin + max(0., (self.deadline-_utc_datetime(clock())).total_seconds())
        if read_deadline is not None:
            if type(read_deadline) not in (int,float) or not math.isfinite(read_deadline):
                raise ValueError('recovery_read_deadline_rejected')
            self.end = min(self.end,read_deadline)
        self.events, self.dropped = [], 0
        self.lock = threading.Lock()

    def remaining(self):
        return max(0., min(self.end-self.monotonic(),
                          (self.deadline-_utc_datetime(self.clock())).total_seconds()))

    def require(self):
        if self.remaining() <= 0:
            from ai_ops.c_approval import ApprovalRejected
            raise ApprovalRejected('recovery_read_deadline_exceeded')

    @contextmanager
    def activate(self):
        token = _ACTIVE.set(self)
        try:
            yield self
        finally:
            _ACTIVE.reset(token)

    @contextmanager
    def phase(self, name, *, read=False):
        if name not in _PHASES:
            raise ValueError('recovery_timing_phase_rejected')
        start, remaining, status = self.monotonic(), self.remaining(), 'ok'
        try:
            if read: self.require()
            yield
            if read: self.require()
        except BaseException:
            status = 'error'
            raise
        finally:
            end = self.monotonic()
            with self.lock:
                if len(self.events) < 256:
                    self.events.append(dict(stage=name, status=status,
                        started_ms=round((start-self.origin)*1000, 3),
                        duration_ms=round((end-start)*1000, 3),
                        remaining_before_ms=round(remaining*1000, 3),
                        remaining_after_ms=round(self.remaining()*1000, 3)))
                else: self.dropped += 1

    def report(self):
        with self.lock:
            return dict(record_type='c04_recovery_read_timing', schema_version=1,
                events=sorted((dict(v) for v in self.events), key=lambda v:v['started_ms']),
                dropped_events=self.dropped)


def phase(name, *, read=False):
    current = _ACTIVE.get()
    return current.phase(name, read=read) if current is not None else nullcontext()


def read_timeout(maximum):
    current = _ACTIVE.get()
    if current is None: return maximum
    current.require()
    return min(maximum, current.remaining())


def submit(pool, function, *args):
    # future마다 Context를 복사해 같은 Context의 동시 실행을 피한다.
    return pool.submit(copy_context().run, function, *args)


def read_map(pool, function, values):
    futures = [submit(pool, function, value) for value in values]
    return [future.result() for future in futures]


def github_phase(path):
    # 결과에는 API 경로·ID·SHA·질문을 저장하지 않는다.
    if path == 'user': return 'github.identity'
    if '/git/ref/heads/main' in path: return 'github.main'
    if '/reviews?' in path: return 'github.reviews'
    if '/check-runs?' in path: return 'github.checks'
    if '/branches/main/protection' in path or '/rulesets/' in path: return 'github.protection'
    if '/pulls/' in path: return 'github.pull'
    return 'github.other'


@contextmanager
def proposal_read_timing(plan, report, *, clock, monotonic=None):
    """Reserve PR creation and delivery before the existing minimum review window.

    Always persist bounded, value-free timing, including timeout/cancellation paths.
    Exiting this scope restores the context so token cleanup is not budget-blocked.
    """
    from datetime import timedelta
    deadline = plan.review_cutoff() - timedelta(
        seconds=(plan.minimum_review_seconds or 30) + WRITER_RESERVE_SECONDS + NOTICE_RESERVE_SECONDS)
    trace = RecoveryReadTiming(deadline, clock=clock, monotonic=monotonic)
    try:
        with trace.activate(), trace.phase('proposal.total', read=True):
            yield trace
    finally:
        report(trace.report())


# Internal sub-budgets; neither the review cutoff nor rollback deadlines move.
# 006 writer took 7.416 s; 007 host output -> response arrival took 21.156 s.
# Reserve the whole tool round trip (25 s), not just socket forwarding. This is
# a conservative admission floor, not proof of future latency. Slow paths stop
# before PR creation; the 120/210/240/300 s external deadlines do not move.
WRITER_RESERVE_SECONDS = 8
NOTICE_RESERVE_SECONDS = 25


@contextmanager
def without_read_budget():
    """Revocation must still run when the preparation budget has expired."""
    token = _ACTIVE.set(None)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


@contextmanager
def writer_pr_timing(plan, report, *, clock, monotonic=None, read_deadline=None):
    from datetime import timedelta
    deadline = plan.review_cutoff() - timedelta(
        seconds=(plan.minimum_review_seconds or 30) + NOTICE_RESERVE_SECONDS)
    trace = RecoveryReadTiming(deadline, clock=clock, monotonic=monotonic, read_deadline=read_deadline)
    try:
        with trace.activate(), trace.phase('writer.total', read=True):
            if trace.remaining() < WRITER_RESERVE_SECONDS:
                raise ValueError('manual_recovery_writer_reserve_exhausted')
            yield trace
    finally:
        report(trace.report())
