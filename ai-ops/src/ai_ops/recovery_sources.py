"""WF07 live read sources, slice A: GitHub Git reads, Kubernetes proof objects, config.

In-cluster counterparts of the operator-laptop readers used during C04
(``gh``/``kubectl`` with operator credentials). Nothing here writes.

* ``TokenRepoReader``: the ``reader`` port of ``ImageRecoveryPRBridge`` for the
  isolated lab repository. Only the Git data endpoints WF07 preparation uses
  (``git/ref/heads/main`` and ``git/{commits,trees,blobs}/<sha>``). A mounted
  token file is re-read on every request (rotation-safe); redirects are never
  followed, no proxy is used, bodies are bounded and errors carry no body.
* ``values_at``: the lab ``values.yaml`` bytes at a revision, through that reader.
* ``ProofObjectsReader``: the Argo application, Deployment, Lease, owning
  ReplicaSet and the Deployment's Pods that ``assess_recovery_proof`` checks,
  through the fixed-route Kubernetes ``ApiClient``.
* ``PrepareConfig``: the trusted WF07 configuration (Slack->GitHub principals,
  PR author, draft store, GitHub token file).

Slice B (normal-CI history, ECR, ``read_proof`` assembly, entrypoint wiring)
uses these; until then nothing in the process entrypoint constructs them.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import stat
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from pydantic import Field, StrictInt, StrictStr, model_validator

from ai_ops.c_approval import MAX_API_BYTES, _blob, _entry, _lab_tree, _repo_api, _tree
from ai_ops.c_recovery import ISOLATED_REPOSITORY
from ai_ops.ci_token_reader import NoRedirect
from ai_ops.contracts import StrictModel
from ai_ops.recovery_commands import ENDPOINT
from ai_ops.recovery_observe import REPLICASETS, ObserveConfig, argo_path
from ai_ops.restart_k8s import LEASE_PATH, PODS, ApiClient

GITHUB_API = 'https://api.github.com/'
GIT_READ = r'/git/(?:ref/heads/main|(?:commits|trees|blobs)/[a-f0-9]{40})\Z'
READ_SECONDS = 8
TOKEN = re.compile(r'[A-Za-z0-9_]{20,4096}')
# assess_live_preflight pins the Argo application to argocd/lab-target.
ARGO_NAMESPACE, ARGO_APPLICATION = 'argocd', 'lab-target'


class TokenRepoReader:
    mode = 'github_token'

    def __init__(self, token_file, *, repository=ISOLATED_REPOSITORY, opener=None, read_seconds=READ_SECONDS):
        if not Path(token_file).is_absolute():
            raise ValueError('github_token_path_rejected')
        if type(read_seconds) is not int or not 1 <= read_seconds <= 15:
            raise ValueError('github_read_budget_rejected')
        self.path = Path(token_file)
        self.repository = repository
        self.read_path = re.compile(re.escape(_repo_api(repository)) + GIT_READ)
        self.read_seconds = read_seconds
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())

    def _token(self):
        info = self.path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > 4096:
            raise ValueError('github_token_file_rejected')
        token = self.path.read_text(encoding='ascii').strip()
        if not TOKEN.fullmatch(token):
            raise ValueError('github_token_rejected')
        return token

    def get(self, endpoint):
        if not isinstance(endpoint, str) or not self.read_path.fullmatch(endpoint):
            raise ValueError('github_read_path_rejected')
        request = Request(GITHUB_API + endpoint, headers={
            'Authorization': 'Bearer ' + self._token(), 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'ai-ops-recovery'})
        try:
            with self.opener.open(request, timeout=self.read_seconds) as response:
                if getattr(response, 'status', 200) != 200:
                    raise ValueError('github_read_failed')
                data = response.read(MAX_API_BYTES + 1)
        except HTTPError as error:
            error.close()  # redirects (3xx) land here too and are never followed
            raise ValueError('github_read_failed') from None
        except (URLError, OSError, TimeoutError):
            raise ValueError('github_read_failed') from None
        if len(data) > MAX_API_BYTES:
            raise ValueError('github_response_too_large')
        try:
            value = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError('github_response_rejected') from None
        if not isinstance(value, dict):
            raise ValueError('github_response_rejected')
        return value


def values_at(reader, revision):
    """Exact lab ``values.yaml`` bytes at ``revision`` (same helpers as WF07 preparation)."""
    repository = reader.repository
    lab = _tree(reader, _lab_tree(reader, revision, repository), repository)
    return _blob(reader, _entry(lab, 'values.yaml', 'blob', '100644'), repository=repository)


def proof_routes():
    return (('GET', re.escape(ENDPOINT)), ('GET', re.escape(REPLICASETS)), ('GET', re.escape(PODS)),
            ('GET', re.escape(LEASE_PATH)), ('GET', re.escape(argo_path(ARGO_NAMESPACE, ARGO_APPLICATION))))


class ProofObjectsReader:
    """Raw objects for ``assess_recovery_proof``; selection only, no verdicts.

    Pods are the Deployment's (selector match, not deleting). ``replica_set`` is the
    Deployment-owned ReplicaSet controlling the single such Pod, else ``{}`` so the
    proof reports ``current_pod_binding_unverified`` (fail closed).
    """

    def __init__(self, client, *, now):
        if not isinstance(client, ApiClient):
            raise ValueError('proof_reader_rejected')
        self.client, self._now = client, now

    def _list(self, path, deadline):
        items = self.client.request('GET', path, deadline=deadline).get('items')
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError('proof_list_rejected')
        return items

    def read(self, *, deadline):
        application = self.client.request('GET', argo_path(ARGO_NAMESPACE, ARGO_APPLICATION), deadline=deadline)
        deployment = self.client.request('GET', ENDPOINT, deadline=deadline)
        lease = self.client.request('GET', LEASE_PATH, deadline=deadline)
        replica_sets = self._list(REPLICASETS, deadline)
        pods = self._list(PODS, deadline)
        if self._now() >= deadline:
            raise TimeoutError('proof_read_deadline')
        uid = deployment.get('metadata', {}).get('uid')
        selector = deployment.get('spec', {}).get('selector', {}).get('matchLabels')
        if not isinstance(uid, str) or not uid or not isinstance(selector, dict) or not selector:
            raise ValueError('proof_deployment_rejected')
        mine = [p for p in pods if not p.get('metadata', {}).get('deletionTimestamp')
                and isinstance(p.get('metadata', {}).get('labels'), dict)
                and all(p['metadata']['labels'].get(k) == v for k, v in selector.items())]
        owned = {rs['metadata']['uid']: rs for rs in replica_sets
                 if isinstance(rs.get('metadata', {}).get('uid'), str)
                 and [(r.get('kind'), r.get('uid'), r.get('controller')) for r in
                      rs['metadata'].get('ownerReferences', [])] == [('Deployment', uid, True)]}
        replica_set = {}
        if len(mine) == 1:
            refs = mine[0].get('metadata', {}).get('ownerReferences', [])
            if (isinstance(refs, list) and len(refs) == 1 and refs[0].get('kind') == 'ReplicaSet'
                    and refs[0].get('controller') is True and refs[0].get('uid') in owned):
                replica_set = owned[refs[0]['uid']]
        return {'application': application, 'deployment': deployment, 'lease': lease,
                'replica_set': replica_set, 'pods': mine}


def make_proof_reader(config, *, now, connection_factory=None):
    """Same read identity/endpoint as the observer, plus the Lease route."""
    if not isinstance(config, ObserveConfig):
        raise ValueError('proof_reader_config_rejected')
    if (config.argo_namespace, config.argo_application) != (ARGO_NAMESPACE, ARGO_APPLICATION):
        raise ValueError('proof_reader_argo_application_rejected')
    client = ApiClient(config.api_url, config.token_file, config.ca_file, proof_routes(), now=now,
                       connection_factory=connection_factory)
    return ProofObjectsReader(client, now=now)


class PrepareConfig(StrictModel):
    """Optional ``prepare`` section of recovery-approval.json (trusted WF07 config)."""
    schema_version: int = Field(default=1, ge=1, le=1)
    principals: dict[StrictStr, StrictInt] = Field(min_length=1, max_length=32)
    author_id: StrictInt = Field(gt=0)
    drafts_dir: StrictStr
    github_token_file: StrictStr
    prepare_seconds: StrictInt = Field(default=15, ge=1, le=15)
    # Slice B: the normal-CI binding that pins the known-good image, and the IRSA
    # role name the ECR reader must be running as.
    normal_binding_file: StrictStr
    aws_role_name: StrictStr = Field(pattern=r'^[A-Za-z0-9+=,.@_-]{1,64}$')

    @model_validator(mode='after')
    def valid(self):
        if any(not re.fullmatch(r'[UW][A-Z0-9]{2,31}', k) or v <= 0 for k, v in self.principals.items()):
            raise ValueError('prepare_principals_rejected')
        if len(set(self.principals.values())) != len(self.principals) or self.author_id in self.principals.values():
            raise ValueError('prepare_author_must_differ_from_reviewers')
        if any(not path.startswith('/') for path in (self.drafts_dir, self.github_token_file,
                                                     self.normal_binding_file)):
            raise ValueError('prepare_path_rejected')
        return self
