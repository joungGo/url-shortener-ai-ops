"""A7 current-state observation for the PR recovery path (WF06 ``observe`` port).

Read-only Kubernetes/Argo reads for the one supported target (``ai-ops-lab/lab-api``)
that produce a ``CurrentRecoveryState`` for ``ledger.open`` and ``ledger.revalidate``.

User decisions (2026-11-21):

* an image failure is judged from the **current container state**, the same rule as
  the WF07 proof re-check: exactly one live Pod owned (via its ReplicaSet) by the
  Deployment, container ``api`` not ready and waiting with ``ErrImagePull`` or
  ``ImagePullBackOff``, Pod/ReplicaSet image equal to the Deployment image, latest
  generation observed and no ready replica. Event timestamps are not used (kubelet
  pull back-off reaches 5 minutes);
* ``healthy`` is **never** reported. Anything that is not a proven image failure
  is ``unknown``, so no "already recovered" decision is made automatically;
* the revision is the Argo ``spec.source.targetRevision`` only when it equals
  ``status.sync.revision`` (same value WF07 binds as ``base_sha``).

A read that fails, is malformed or exceeds the deadline raises: the ledger then
refuses to open, or records ``needs_review`` (fail closed). Only hashes of the raw
responses are kept (``proof_hash``); no object bodies are stored or returned.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
from typing import Literal

from pydantic import Field, StrictStr, model_validator

from ai_ops.c_recovery import TARGET, ImageFailureBinding
from ai_ops.contracts import StrictModel, sha256_json
from ai_ops.recovery_approvals import CurrentRecoveryState
from ai_ops.recovery_commands import ENDPOINT
from ai_ops.restart_k8s import NAME, PODS, ApiClient

NAMESPACE, DEPLOYMENT, CONTAINER = 'ai-ops-lab', 'lab-api', 'api'
REPLICASETS = f'/apis/apps/v1/namespaces/{NAMESPACE}/replicasets'
PULL_FAILURES = ('ErrImagePull', 'ImagePullBackOff')
EVIDENCE_IDS = sorted(['live-deployment', 'live-pod', 'live-replicaset'])


def argo_path(namespace, application):
    return f'/apis/argoproj.io/v1alpha1/namespaces/{namespace}/applications/{application}'


class ObserveConfig(StrictModel):
    """Optional ``observe`` section of recovery-approval.json (read identity only)."""
    schema_version: Literal[1] = 1
    api_url: StrictStr = Field(pattern=r'^https://[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:[1-9][0-9]{0,4})?$')
    token_file: StrictStr
    ca_file: StrictStr
    argo_namespace: StrictStr = Field(pattern='^' + NAME + '$')
    argo_application: StrictStr = Field(pattern='^' + NAME + '$')

    @model_validator(mode='after')
    def absolute(self):
        if not self.token_file.startswith('/') or not self.ca_file.startswith('/'):
            raise ValueError('observe_credential_path_rejected')
        return self

    def routes(self):
        return (('GET', re.escape(ENDPOINT)), ('GET', re.escape(REPLICASETS)), ('GET', re.escape(PODS)),
                ('GET', re.escape(argo_path(self.argo_namespace, self.argo_application))))


def _owned(item, kind, uid):
    refs = item.get('metadata', {}).get('ownerReferences', [])
    return (isinstance(refs, list) and len(refs) == 1 and refs[0].get('kind') == kind
            and refs[0].get('uid') == uid and refs[0].get('controller') is True)


def _image_of(template):
    containers = template.get('spec', {}).get('containers', [])
    if not isinstance(containers, list) or len(containers) != 1 or containers[0].get('name') != CONTAINER:
        return None
    image = containers[0].get('image')
    return image if isinstance(image, str) and image else None


def derive_state(plan, deployment, replica_sets, pods, application, *, observed_at, proof_hash):
    """Pure: verified API objects -> CurrentRecoveryState (``image_failure`` or ``unknown``).

    Raises when the identity itself cannot be established (no usable Deployment or
    revision): there is then nothing safe to report.
    """
    meta, spec, status = (deployment.get('metadata', {}), deployment.get('spec', {}),
                          deployment.get('status', {}))
    uid, image = meta.get('uid'), _image_of(spec.get('template', {}))
    if (meta.get('name') != DEPLOYMENT or meta.get('namespace') != NAMESPACE
            or not isinstance(uid, str) or not uid or image is None):
        raise ValueError('observation_deployment_rejected')
    source = application.get('spec', {}).get('source', {})
    revision = source.get('targetRevision') if isinstance(source, dict) else None
    synced = application.get('status', {}).get('sync', {}).get('revision')
    if not isinstance(revision, str) or not revision or synced != revision:
        raise ValueError('observation_revision_unverified')
    identity = dict(plan_hash=plan.plan_hash, event_id=plan.event_id, target=plan.target,
                    deployment_uid_hash=sha256_json(uid), revision_hash=sha256_json(revision),
                    image_hash=sha256_json(image), proof_hash=proof_hash, observed_at=observed_at)
    owned_sets = [rs for rs in replica_sets if _owned(rs, 'Deployment', uid)]
    set_uids = {rs['metadata'].get('uid'): rs for rs in owned_sets}
    selector = spec.get('selector', {}).get('matchLabels')
    if not isinstance(selector, dict) or not selector:
        raise ValueError('observation_deployment_rejected')
    # Other workloads may share the namespace: only this Deployment's Pods count.
    live = [p for p in pods if not p.get('metadata', {}).get('deletionTimestamp')
            and isinstance(p.get('metadata', {}).get('labels'), dict)
            and all(p['metadata']['labels'].get(k) == v for k, v in selector.items())]
    candidates = [p for p in live if any(_owned(p, 'ReplicaSet', rs_uid) for rs_uid in set_uids)]
    failure = None
    if (len(live) == 1 and len(candidates) == 1
            and status.get('observedGeneration') == meta.get('generation')
            and status.get('readyReplicas', 0) == 0):
        pod = candidates[0]
        owner = pod['metadata']['ownerReferences'][0]['uid']
        states = pod.get('status', {}).get('containerStatuses', [])
        state = states[0] if isinstance(states, list) and len(states) == 1 else {}
        waiting = state.get('state', {}).get('waiting', {}) if isinstance(state.get('state'), dict) else {}
        pod_uid = pod['metadata'].get('uid')
        if (isinstance(pod_uid, str) and pod_uid and _image_of(pod) == image
                and _image_of(set_uids[owner].get('spec', {}).get('template', {})) == image
                and state.get('name') == CONTAINER and state.get('ready') is False
                and state.get('image') == image and waiting.get('reason') in PULL_FAILURES):
            failure = ImageFailureBinding(pod_uid_hash=sha256_json(pod_uid), deployment_uid_hash=sha256_json(uid),
                                          image_hash=sha256_json(image), observed_at=observed_at,
                                          evidence_ids=EVIDENCE_IDS)
    if failure is None:
        return CurrentRecoveryState(**identity, status='unknown')
    return CurrentRecoveryState(**identity, status='image_failure', failure=failure)


class KubernetesRecoveryObserver:
    """``observe(plan, *, deadline)`` port over a fixed-route read-only client."""
    mode = 'kubernetes'

    def __init__(self, client, config, *, now):
        if not isinstance(client, ApiClient) or not isinstance(config, ObserveConfig):
            raise ValueError('recovery_observer_rejected')
        self.client, self.config, self._now = client, config, now

    def _get(self, path, deadline, digests, params=None):
        value = self.client.request('GET', path, deadline=deadline, params=params)
        digests.append(sha256_json([path, value]))
        return value

    @staticmethod
    def _items(value):
        items = value.get('items')
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError('observation_list_rejected')
        return items

    def __call__(self, plan, *, deadline):
        if plan.target != TARGET:
            raise ValueError('observation_target_unsupported')
        digests = []
        deployment = self._get(ENDPOINT, deadline, digests)
        replica_sets = self._items(self._get(REPLICASETS, deadline, digests))
        pods = self._items(self._get(PODS, deadline, digests))
        application = self._get(argo_path(self.config.argo_namespace, self.config.argo_application),
                                deadline, digests)
        now = self._now()
        if now >= deadline:
            raise TimeoutError('observation_deadline')
        observed_at = datetime.fromtimestamp(now, timezone.utc)
        return derive_state(plan, deployment, replica_sets, pods, application, observed_at=observed_at,
                            proof_hash=hashlib.sha256(''.join(digests).encode()).hexdigest())


def make_observer(config, *, now, connection_factory=None):
    client = ApiClient(config.api_url, config.token_file, config.ca_file, config.routes(), now=now,
                       connection_factory=connection_factory)
    return KubernetesRecoveryObserver(client, config, now=now)
