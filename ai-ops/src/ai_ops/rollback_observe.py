"""PR-path rollback observation for WF09 ``record_rollback_local``.

WF09 needs two things after a separately authorized rollback: a receipt proving
the rollback is applied (exact pre-approved revision/digest, when) and samples
from a fresh window after it. This module supplies the receipt from cluster
facts only (Argo Application pinned to, synced at and last successfully
operated on the scope revision, healthy, manual sync; Deployment running the
scope digest) and pairs it with the A7-4 sealed capture for the window.

Whether GitHub ``main`` equals the rollback revision is recorded as handoff
information, never as a gate: a rollback that pins Argo to an earlier baseline
commit leaves ``main`` ahead (Git and cluster diverge), which a person must see
but which does not make the observed rollback untrue.

The receipt is derived from stable facts only, so re-reading an unchanged
cluster yields the same receipt (and the same execution hash the sealed capture
is bound to). Any later sync changes it and the observation fails closed. Modes
are not ``local_fixture``: the WF09 coordinator still refuses these readers.
"""
from __future__ import annotations

import re

from ai_ops.c_approval import _main_sha
from ai_ops.c_recovery import IMAGE_REPOSITORY, RECOVERY_REPOSITORIES
from ai_ops.contracts import sha256_json
from ai_ops.recovery_commands import ENDPOINT
from ai_ops.recovery_effects import ExecutionReceipt, RollbackScope
from ai_ops.restart_effects import argo_path, argo_state, body
from ai_ops.restart_k8s import CONTAINER, DEPLOYMENT, NAMESPACE, parse_time

REPO_URL = re.compile(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\.git')


class RollbackAppliedReader:
    mode = 'kubernetes'

    def __init__(self, *, reader, argo_application, now, github=None):
        self.reader, self._argo, self._now, self.github = reader, argo_path(argo_application), now, github

    def applied(self, receipt, scope, *, deadline):
        e = ExecutionReceipt.model_validate(body(receipt))
        scope = RollbackScope.model_validate(body(scope))
        if e.method != 'pr' or e.application_uid is None:
            raise ValueError('rollback_observation_requires_pr')
        app = self.reader.request('GET', self._argo, deadline=deadline)
        deployment = self.reader.request('GET', ENDPOINT, deadline=deadline)
        revision, application_uid, healthy = argo_state(app)
        operation = app['status']['operationState']
        containers = deployment['spec']['template']['spec']['containers']
        meta = deployment['metadata']
        if not (application_uid == e.application_uid and healthy and revision == scope.revision
                and app.get('spec', {}).get('syncPolicy', {}).get('automated') is None
                and meta.get('uid') == e.deployment_uid and meta.get('name') == DEPLOYMENT
                and meta.get('namespace') == NAMESPACE and not meta.get('deletionTimestamp')
                and deployment.get('status', {}).get('observedGeneration') == meta.get('generation')
                and len(containers) == 1 and containers[0].get('name') == CONTAINER
                and containers[0].get('image') == IMAGE_REPOSITORY + '@' + scope.digest):
            raise ValueError('rollback_not_observed')
        applied_at = parse_time(operation['finishedAt'])
        if applied_at.timestamp() > self._now():
            raise ValueError('rollback_time_in_future')
        facts = {'application_uid': application_uid, 'deployment_uid': e.deployment_uid, 'revision': revision,
                 'digest': scope.digest, 'applied_at': applied_at.isoformat()}
        return ExecutionReceipt(
            method='pr', plan_hash=e.plan_hash, event_id=e.event_id, job_id=e.job_id, target=e.target,
            deployment_uid=e.deployment_uid, application_uid=application_uid, revision=revision, digest=scope.digest,
            marker=None, old_pod_uid_hash=None, generation=None, applied_at=applied_at, applied=True,
            cancelled=False, source_hash=sha256_json(facts), lease_hash=None, lease_released=False,
            retained_until=e.retained_until)

    def alignment(self, scope, *, deadline):
        """Handoff information only: does GitHub main equal the rollback revision?"""
        scope = RollbackScope.model_validate(body(scope))
        if self.github is None:
            return {'gitops_main_matches': None}
        try:
            app = self.reader.request('GET', self._argo, deadline=deadline)
            match = REPO_URL.fullmatch(app['spec']['source']['repoURL'])
            if match is None or match.group(1) not in RECOVERY_REPOSITORIES or self._now() >= deadline:
                return {'gitops_main_matches': None}
            return {'gitops_main_matches': _main_sha(self.github, match.group(1)) == scope.revision}
        except Exception:  # unknown, never a gate and never exception text
            return {'gitops_main_matches': None}


class RollbackObservationReader:
    """The one reader ``record_rollback_local`` takes: live receipt + sealed window."""
    mode = 'sealed_capture'  # not 'local_fixture': refused until a live decision

    def __init__(self, applied_reader, sealed_reader):
        self._applied, self._sealed = applied_reader, sealed_reader

    def applied(self, receipt, scope, *, deadline):
        return self._applied.applied(receipt, scope, deadline=deadline)

    def read(self, receipt, policy, *, deadline):
        return self._sealed.read(receipt, policy, deadline=deadline)

    def alignment(self, scope, *, deadline):
        return self._applied.alignment(scope, deadline=deadline)
