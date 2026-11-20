"""Rollback execution slice 2: one baseline-pin rollback for a WF09 rollback candidate.

Decided by llm-council (2026-11-20): a rollback is only "pin the Argo Application
back to the pre-approved baseline commit and sync once". A revert PR is a new
change, not a rollback. Order of one attempt, never retried:

1. reserve the single attempt for the plan (a crashed or uncertain attempt is
   never re-run; it stays for manual review);
2. revalidate immediately before writing: the WF09 decision is still this exact
   ``rollback_candidate``; the scope is unrevoked, unexpired, unused and bound to
   this execution; the ``ai-ops/lab`` tree of the deployed revision and of the
   baseline differ *only* in ``values.yaml``, whose canonical digests are exactly
   failed -> baseline (nothing else is rolled back); the Argo Application has the
   plain pinned source (no parameters/overrides), manual sync, no operation in
   progress; the Deployment runs the failed digest;
3. hold the scenario Lease (never taken over), then engage the GitOps drift lock
   *before* the write so WF07 cannot build on the failed ``main``;
4. pin once; a definite conflict means not applied, anything else is unknown;
5. observe the pinned target; whether the sync completed and the service
   recovered is judged later by WF09 rollback observation, not here.

The port is accepted only in ``local_fixture`` mode: no live Argo writer exists.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import re

from ai_ops.c_approval import _blob, _entry, _lab_tree, _tree
from ai_ops.c_recovery import IMAGE_REPOSITORY, RECOVERY_REPOSITORIES, _encoded_values
from ai_ops.contracts import sha256_json
from ai_ops.recovery_effects import RollbackScope
from ai_ops.restart_effects import argo_path, body

LEASE = ('b6-scenario-lock', 'ai-ops-lab')


class RollbackExecutor:
    def __init__(self, queue, *, effects, drift_lock, github, repository, executor_id, execution_seconds,
                 argo_application=('argocd', 'lab-target')):
        if (not re.fullmatch(r'local-rollback-[a-z0-9-]{1,40}', executor_id or '')
                or type(execution_seconds) is not int or not 1 <= execution_seconds <= 15
                or repository not in RECOVERY_REPOSITORIES):
            raise ValueError('rollback_executor_configuration_rejected')
        argo_path(argo_application)
        self.queue, self.effects, self.drift_lock, self.github = queue, effects, drift_lock, github
        self.repository, self.executor_id, self.execution_seconds = repository, executor_id, execution_seconds
        self.application = tuple(argo_application)
        self.identity = {'executor_id': executor_id, 'namespace': argo_application[0],
                         'resource': f'applications/{argo_application[1]}', 'verbs': ['get', 'patch']}
        with queue.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS rollback_runs (plan_hash TEXT PRIMARY KEY, document TEXT NOT NULL)')

    # --- ledger -----------------------------------------------------------------------------
    @staticmethod
    def _get(db, key):
        row = db.execute('SELECT document FROM rollback_runs WHERE plan_hash=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _save(db, run):
        db.execute('INSERT OR REPLACE INTO rollback_runs VALUES (?,?)', (run['plan_hash'], json.dumps(run, sort_keys=True)))

    def _now(self):
        return datetime.fromtimestamp(self.queue.now(), timezone.utc)

    def show(self, key, *, actor):
        self.effects._source(key, actor)  # same access rule as the effect ledger
        with self.queue.transaction() as db:
            run = self._get(db, key)
        if run is None:
            raise ValueError('rollback_run_unknown')
        return {**run, 'execution_allowed': False}

    # --- checks -----------------------------------------------------------------------------
    def _port(self, port):
        if getattr(port, 'mode', None) != 'local_fixture' or getattr(port, 'identity', None) != self.identity:
            raise ValueError('live_rollback_executor_disabled')

    def _candidate(self, key, actor, scope):
        """Current WF09 decision and scope, revalidated; returns the receipt."""
        e = self.effects._source(key, actor)
        view = self.effects.show(key, actor=actor)
        decision = view.get('rollback') or {}
        now = self._now()
        if (e.method != 'pr' or e.cancelled or view.get('cancel_requested') or view.get('rollback_result') is not None
                or view.get('rollback_attempted') or decision.get('decision') != 'rollback_candidate'
                or decision.get('scope_hash') != sha256_json(body(scope))
                or decision.get('change') != {'revision': scope.revision, 'digest': scope.digest}):
            raise ValueError('rollback_candidate_not_current')
        if (scope.revoked or scope.attempts_used or scope.execution_hash != e.digest_key()
                or scope.target != e.target or scope.deployment_uid != e.deployment_uid
                or scope.application_uid != e.application_uid or e.applied_at is None
                or not scope.issued_at <= e.applied_at <= now < scope.expires_at
                or (scope.revision, scope.digest) == (e.revision, e.digest)):
            raise ValueError('rollback_scope_not_valid')
        return e

    def _only_digest_rolls_back(self, e, scope):
        """The lab trees differ only in values.yaml, and only failed -> baseline digest."""
        deployed = _tree(self.github, _lab_tree(self.github, e.revision, self.repository), self.repository)
        baseline = _tree(self.github, _lab_tree(self.github, scope.revision, self.repository), self.repository)
        if set(deployed) != set(baseline) or any(deployed[p] != baseline[p] for p in deployed if p != 'values.yaml'):
            raise ValueError('rollback_would_revert_more_than_the_image')
        failed = _blob(self.github, _entry(deployed, 'values.yaml', 'blob', '100644'), repository=self.repository)
        good = _blob(self.github, _entry(baseline, 'values.yaml', 'blob', '100644'), repository=self.repository)
        if failed != _encoded_values(e.digest).encode() or good != _encoded_values(scope.digest).encode():
            raise ValueError('rollback_values_not_exact_digest_change')

    def _live(self, observed, e, revision, digest):
        app, deployment = observed['application'], observed['deployment']
        meta, spec, status = app.get('metadata', {}), app.get('spec', {}), app.get('status', {})
        dmeta = deployment.get('metadata', {})
        containers = deployment.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])
        if not (meta.get('uid') == e.application_uid and (meta.get('namespace'), meta.get('name')) == self.application
                and not meta.get('deletionTimestamp') and app.get('operation') is None
                and status.get('operationState', {}).get('phase') != 'Running'
                and spec.get('source') == {'repoURL': f'https://github.com/{self.repository}.git', 'path': 'ai-ops/lab',
                                           'targetRevision': revision}
                and spec.get('syncPolicy', {}).get('automated') is None
                and dmeta.get('uid') == e.deployment_uid and not dmeta.get('deletionTimestamp')
                and len(containers) == 1 and containers[0].get('name') == 'api'
                and containers[0].get('image') == IMAGE_REPOSITORY + '@' + digest):
            raise ValueError('rollback_live_state_rejected')

    @staticmethod
    def _held(lease, owner, deadline):
        if (not isinstance(lease, dict) or set(lease) != {'name', 'namespace', 'uid', 'holder', 'verified_until'}
                or (lease['name'], lease['namespace']) != LEASE or not isinstance(lease['uid'], str) or not lease['uid']
                or lease['holder'] != owner or type(lease['verified_until']) not in (int, float)
                or not math.isfinite(lease['verified_until']) or lease['verified_until'] <= deadline):
            raise ValueError('rollback_lease_unconfirmed')

    # --- one attempt ---------------------------------------------------------------------------
    def execute_local(self, key, *, actor, scope_reader, port):
        self._port(port)
        e = self.effects._source(key, actor)
        scope = RollbackScope.model_validate(body(scope_reader(e)))
        e = self._candidate(key, actor, scope)
        start = self.queue.now()
        deadline = min(start + self.execution_seconds, scope.expires_at.timestamp(), e.retained_until.timestamp())
        scope_hash, owner = sha256_json(body(scope)), self.executor_id
        with self.queue.transaction() as db:
            if self._get(db, key) is not None:
                raise ValueError('rollback_already_attempted')  # one attempt, even after a crash
            self._save(db, dict(plan_hash=key, scope_hash=scope_hash, revision=scope.revision, digest=scope.digest,
                                state='checking', reason='reserved_once', lease=None, lease_released=False,
                                drift_lock_engaged=False, pinned=False, reserved_at=self._now().isoformat(),
                                retained_until=e.retained_until.isoformat(), generation=1))

        def advance(**changes):
            with self.queue.transaction() as db:
                run = self._get(db, key)
                if run['state'] not in ('checking', 'executing') or not start <= self.queue.now() < deadline:
                    raise ValueError('rollback_cancelled_or_expired')
                run.update(changes, generation=run['generation'] + 1)
                self._save(db, run)

        def recheck():
            if sha256_json(body(RollbackScope.model_validate(body(scope_reader(e))))) != scope_hash:
                raise ValueError('rollback_scope_changed')
            self._candidate(key, actor, scope)

        pinned_attempted = False
        try:
            self._only_digest_rolls_back(e, scope)
            self._live(port.read(deadline=deadline), e, e.revision, e.digest)
            held = port.acquire(owner=owner, deadline=min(deadline, self.queue.now() + 3))
            self._held(held, owner, deadline)
            advance(lease=deepcopy(held))
            # Before any write: WF07 must not build on the failed main from now on.
            self.drift_lock.engage(plan_hash=key, scope=scope, repository=self.repository)
            advance(drift_lock_engaged=True)
            self._live(port.read(deadline=deadline), e, e.revision, e.digest)
            lease = port.inspect_lease(held, deadline=min(deadline, self.queue.now() + 3))
            self._held(lease, owner, deadline)
            if lease['uid'] != held['uid']:
                raise ValueError('rollback_lease_changed')
            recheck()
            self._port(port)
            advance(state='executing', reason='pin_outcome_pending')
            pinned_attempted = True
            port.pin({'application_uid': e.application_uid, 'from_revision': e.revision,
                      'to_revision': scope.revision}, deadline=deadline)
            observed = port.read(deadline=deadline)
            if observed['application'].get('spec', {}).get('source', {}).get('targetRevision') != scope.revision:
                raise ValueError('rollback_pin_unconfirmed')
            final = port.inspect_lease(held, deadline=min(deadline, self.queue.now() + 3))
            self._held(final, owner, deadline)
            if final['uid'] != held['uid'] or port.release(held, deadline=min(deadline, self.queue.now() + 3)) is not True:
                raise ValueError('rollback_lease_release_unconfirmed')
            with self.queue.transaction() as db:
                run = self._get(db, key)
                run.update(state='pinned_unverified', reason='sync_and_service_judged_by_wf09_observation',
                           pinned=True, lease_released=True, generation=run['generation'] + 1)
                if self.queue.now() >= deadline:
                    run.update(state='needs_review', reason='completed_after_budget')
                self._save(db, run)
        except Exception as exc:
            conflict = pinned_attempted and str(exc) == 'argo_pin_conflict'
            with self.queue.transaction() as db:
                run = self._get(db, key)
                run.update(state='not_applied' if conflict else 'needs_review',
                           reason='definite_conflict_no_retry' if conflict
                           else ('pin_outcome_unknown_no_retry' if pinned_attempted else 'precondition_failed_no_write'),
                           generation=run['generation'] + 1)
                self._save(db, run)
        return self.show(key, actor=actor)
