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
* ``healthy`` (user decisions 2026-11-25, replacing "never healthy") only when all
  five plan checks pass in one bounded read: Argo ``Synced``+``Healthy``; the single
  owned Pod's running image digest equals the Deployment's desired digest; the
  rollout is complete and the Pod ``Ready`` for at least ``STABLE_SECONDS``;
  ``/readyz`` and ``/items`` (read through the API server Pod proxy, fixed GET
  routes, no new network path) match the app response contracts. Anything else
  that is not a proven image failure stays ``unknown``;
* the revision is the Argo ``spec.source.targetRevision`` only when it equals
  ``status.sync.revision`` (same value WF07 binds as ``base_sha``).

A read that fails, is malformed or exceeds the deadline raises: the ledger then
refuses to open, or records ``needs_review`` (fail closed). Only hashes of the raw
responses are kept (``proof_hash``); no object bodies are stored or returned.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Literal

from pydantic import Field, StrictStr, model_validator

from ai_ops.c_recovery import IMAGE_REPOSITORY, TARGET, ImageFailureBinding
from ai_ops.c_verification_collect import valid_body
from ai_ops.contracts import StrictModel, sha256_json
from ai_ops.recovery_approvals import CurrentRecoveryState
from ai_ops.recovery_commands import ENDPOINT
from ai_ops.restart_k8s import NAME, PODS, ApiClient, ApiError, parse_time

NAMESPACE, DEPLOYMENT, CONTAINER = 'ai-ops-lab', 'lab-api', 'api'
REPLICASETS = f'/apis/apps/v1/namespaces/{NAMESPACE}/replicasets'
PULL_FAILURES = ('ErrImagePull', 'ImagePullBackOff')
EVIDENCE_IDS = sorted(['live-deployment', 'live-pod', 'live-replicaset'])
# Same names as the image-rollback plan's verification list (WF05 registry).
HEALTH_CHECKS = ['argo_synced_healthy', 'pod_image_matches_desired_digest', 'pod_ready',
                 'readyz_success', 'items_flow_success']
STABLE_SECONDS = 60  # Ready must have held this long (a flapping Pod is not "recovered")
HTTP_PORT = 8000
HTTP_PATHS = ('/readyz', '/items')
PROXY = re.escape(PODS) + '/' + NAME + re.escape(f':{HTTP_PORT}/proxy/') + '(readyz|items)'
DESIRED = re.compile(re.escape(IMAGE_REPOSITORY) + r'@(sha256:[a-f0-9]{64})')


def _reject_constant(value):
    raise ValueError('non_finite_json')


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
                ('GET', re.escape(argo_path(self.argo_namespace, self.argo_application))), ('GET', PROXY))


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


def health_target(deployment, replica_sets, pods):
    """Name of the one live owned Pod whose container reports ready (HTTP is read only then)."""
    uid = deployment.get('metadata', {}).get('uid')
    selector = deployment.get('spec', {}).get('selector', {}).get('matchLabels')
    if not isinstance(uid, str) or not isinstance(selector, dict) or not selector:
        return None
    sets = {rs.get('metadata', {}).get('uid') for rs in replica_sets if _owned(rs, 'Deployment', uid)}
    live = [p for p in pods if not p.get('metadata', {}).get('deletionTimestamp')
            and isinstance(p.get('metadata', {}).get('labels'), dict)
            and all(p['metadata']['labels'].get(k) == v for k, v in selector.items())]
    if len(live) != 1 or not any(_owned(live[0], 'ReplicaSet', s) for s in sets):
        return None
    states = live[0].get('status', {}).get('containerStatuses', [])
    name = live[0]['metadata'].get('name')
    if (not isinstance(states, list) or len(states) != 1 or states[0].get('ready') is not True
            or not isinstance(name, str) or not re.fullmatch(NAME, name)):
        return None
    return name


def _healthy(meta, spec, status, image, pod, rs, application, http, observed_at):
    """All five checks or nothing. Missing/odd fields never pass."""
    app = application.get('status', {})
    desired = DESIRED.fullmatch(image)
    if (http is None or any(http.get(path) is not True for path in HTTP_PATHS)
            or app.get('sync', {}).get('status') != 'Synced' or app.get('health', {}).get('status') != 'Healthy'
            or desired is None):
        return False
    if (spec.get('replicas', 1) != 1 or status.get('observedGeneration') != meta.get('generation')
            or any(status.get(k) != 1 for k in ('replicas', 'readyReplicas', 'updatedReplicas', 'availableReplicas'))
            or status.get('unavailableReplicas') not in (None, 0)):
        return False
    states = pod.get('status', {}).get('containerStatuses', [])
    state = states[0] if isinstance(states, list) and len(states) == 1 else {}
    image_id = state.get('imageID')
    if (pod.get('status', {}).get('phase') != 'Running' or _image_of(pod) != image
            or _image_of(rs.get('spec', {}).get('template', {})) != image
            or state.get('name') != CONTAINER or state.get('ready') is not True or state.get('image') != image
            or not isinstance(state.get('state'), dict) or 'running' not in state['state']
            or not isinstance(image_id, str) or not image_id.endswith('@' + desired.group(1))):
        return False
    ready = [c for c in pod.get('status', {}).get('conditions', []) if isinstance(c, dict) and c.get('type') == 'Ready']
    if len(ready) != 1 or ready[0].get('status') != 'True':
        return False
    try:
        since = parse_time(ready[0].get('lastTransitionTime'))
    except ValueError:
        return False
    return observed_at.timestamp() - since.timestamp() >= STABLE_SECONDS


def derive_state(plan, deployment, replica_sets, pods, application, *, observed_at, proof_hash, http=None):
    """Pure: verified API objects -> CurrentRecoveryState (``image_failure``, ``healthy`` or ``unknown``).

    ``http`` maps ``/readyz`` and ``/items`` to whether the response matched the app
    contract (None/absent = not read or unreadable); without it nothing is healthy.
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
        if (len(live) == 1 and len(candidates) == 1
                and _healthy(meta, spec, status, image, candidates[0],
                             set_uids[candidates[0]['metadata']['ownerReferences'][0]['uid']],
                             application, http, observed_at)):
            return CurrentRecoveryState(**identity, status='healthy', healthy_checks=list(HEALTH_CHECKS))
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
        http = None
        pod = health_target(deployment, replica_sets, pods)
        if pod is not None:
            http = {path: self._http(pod, path, deadline, digests) for path in HTTP_PATHS}
        now = self._now()
        if now >= deadline:
            raise TimeoutError('observation_deadline')
        observed_at = datetime.fromtimestamp(now, timezone.utc)
        return derive_state(plan, deployment, replica_sets, pods, application, observed_at=observed_at,
                            proof_hash=hashlib.sha256(''.join(digests).encode()).hexdigest(), http=http)

    def _http(self, pod, path, deadline, digests):
        """True only for HTTP 200 with a contract-valid body; errors are unknown (None), never a pass."""
        try:
            data = self.client.request('GET', f'{PODS}/{pod}:{HTTP_PORT}/proxy{path}', deadline=deadline, raw=True)
        except ApiError as error:
            # A proxy error cannot be told apart from an app error: unknown.
            digests.append(sha256_json([path, 'status', error.status]))
            return None
        digests.append(sha256_json([path, hashlib.sha256(data).hexdigest()]))
        try:
            return valid_body(path, json.loads(data, parse_constant=_reject_constant))
        except (ValueError, UnicodeDecodeError, RecursionError):
            return False


def make_observer(config, *, now, connection_factory=None):
    client = ApiClient(config.api_url, config.token_file, config.ca_file, config.routes(), now=now,
                       connection_factory=connection_factory)
    return KubernetesRecoveryObserver(client, config, now=now)
