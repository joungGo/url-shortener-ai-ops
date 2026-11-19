"""Deterministic pre-write gates for the fixed lab restart (WF08, 3b redesign).

Why this command exists: kubelet restarts a container on *liveness* failure, never
on readiness failure alone. A Pod that stays Running but NotReady is therefore a
restart candidate only when the failure is persistent and restarting is plausibly
useful. These gates do not try to prove that nothing else is wrong (that cannot be
closed); they block harmful or pointless writes and leave effect to post-write
verification (WF09), with one restart per event and manual handoff on failure.

Verdicts are pass / fail / unknown. A blocking gate must pass; ``unknown`` never
counts as pass. ``dependencies`` blocks only on a definite failure and is
otherwise display-only. Evidence references are field names, never values.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from ai_ops.contracts import StrictModel, sha256_json

RULES_VERSION = 'restart-gates-v1'
# Readiness must have been failing (and the Pod alive) for at least this many
# full probe windows (initialDelay + failureThreshold x period).
PERSIST_MULTIPLIER = 2
GATES = ('spec_unchanged_since_plan', 'not_ready_single_replica', 'no_competing_controller',
         'persistent_readiness_failure', 'liveness_stable', 'prior_ready_evidence', 'dependencies')
# Captured right before the patch; with strategy Recreate the old Pod is
# terminated first, so this is the only remaining record of the failure.
REQUIRED_EVIDENCE = ('describe', 'events', 'logs_current', 'logs_previous', 'metrics', 'status')


class GateResult(StrictModel):
    name: Literal[GATES]
    verdict: Literal['pass', 'fail', 'unknown']
    blocking: bool
    evidence_refs: list[str] = Field(max_length=12)
    rules_version: Literal['restart-gates-v1'] = RULES_VERSION


class RestartGateRejected(ValueError):
    """Same message as before for compatibility; carries the per-gate verdicts."""

    def __init__(self, results):
        super().__init__('restart_current_state_rejected')
        self.results = results


def _gate(name, verdict, refs, *, blocking=True):
    return GateResult(name=name, verdict=verdict, blocking=blocking, evidence_refs=list(refs))


def evaluate(plan, s, now):
    """Pure function of the sealed plan, one verified snapshot and the current time."""
    results = []
    unchanged = (s.deployment_uid == plan.deployment_uid and s.resource_version == plan.resource_version
                 and s.revision_hash == plan.revision_hash and sha256_json(s.image) == plan.image_hash
                 and s.pod_uid_hash == plan.pod_uid_hash and s.generation == plan.generation
                 and s.observed_generation == s.generation and s.strategy == plan.strategy
                 and s.annotations_exist == plan.annotations_exist)
    results.append(_gate('spec_unchanged_since_plan', 'pass' if unchanged else 'fail',
                         ('deployment_uid', 'resource_version', 'revision_hash', 'image', 'pod_uid_hash',
                          'generation', 'observed_generation', 'strategy', 'annotations_exist')))
    results.append(_gate('not_ready_single_replica', 'pass' if s.replicas == 1 and s.ready_replicas == 0 else 'fail',
                         ('replicas', 'ready_replicas')))
    quiet = not (s.rollout_in_progress or s.autoscaling or s.gitops_auto_sync or s.restart_marker is not None)
    results.append(_gate('no_competing_controller', 'pass' if quiet else 'fail',
                         ('rollout_in_progress', 'autoscaling', 'gitops_auto_sync', 'restart_marker')))
    threshold = PERSIST_MULTIPLIER * s.readiness_window_seconds
    since, started = s.readiness_failing_since, s.pod_started_at.timestamp()
    if since is None:
        persistent = 'fail'
    elif since.timestamp() < started or since.timestamp() > now or started > now:
        persistent = 'unknown'  # inconsistent clocks/conditions are not evidence
    else:
        persistent = 'pass' if (now - since.timestamp() >= threshold and now - started >= threshold) else 'fail'
    results.append(_gate('persistent_readiness_failure', persistent,
                         ('readiness_failing_since', 'pod_started_at', 'readiness_window_seconds')))
    alive = s.container_running and s.restart_count == plan.restart_count
    results.append(_gate('liveness_stable', 'pass' if alive else 'fail', ('container_running', 'restart_count')))
    results.append(_gate('prior_ready_evidence',
                         {'observed': 'pass', 'absent': 'fail', 'unknown': 'unknown'}[s.prior_ready],
                         ('prior_ready',)))
    results.append(_gate('dependencies', s.dependencies, ('dependencies',), blocking=s.dependencies == 'fail'))
    return results


def rejected(results):
    return sorted(r.name for r in results if r.blocking and r.verdict != 'pass')


def summary(results):
    """Ledger/handoff form: names and verdicts only, no evidence values."""
    return [{'name': r.name, 'verdict': r.verdict, 'blocking': r.blocking, 'rules_version': r.rules_version}
            for r in results]


def validate_evidence(bundle, *, start, now):
    if (not isinstance(bundle, dict) or set(bundle) != {'bundle_sha256', 'items', 'captured_at'}
            or not isinstance(bundle['bundle_sha256'], str) or len(bundle['bundle_sha256']) != 64
            or any(c not in '0123456789abcdef' for c in bundle['bundle_sha256'])
            or bundle['items'] != list(REQUIRED_EVIDENCE)
            or type(bundle['captured_at']) not in (int, float) or not start <= bundle['captured_at'] <= now):
        raise ValueError('restart_evidence_capture_failed')
    return {'bundle_sha256': bundle['bundle_sha256'], 'items': list(REQUIRED_EVIDENCE),
            'captured_at': bundle['captured_at']}
