"""WF07 live read sources, slice B: ECR, ``read_proof`` assembly and the live bridge.

User decisions (2026-11-24, all recommended):

* normal-CI evidence comes from ``NormalCIInbox`` (receipts the CI relay already
  verified against GitHub, HMAC-protected in the queue DB); the relay needs no
  GitHub Actions read access of its own;
* the run used is the latest ``ready`` receipt bound to the configured normal-CI
  binding (``normal-ci-binding.json``), finished before the proof time;
* ECR is read with boto3 under the Pod's IRSA role (read-only ``DescribeImages``),
  after checking the caller is that role in the fixed account.

``read_proof`` returns ``(assess_recovery_proof result, values bytes)`` exactly as
``ImageRecoveryPRBridge`` expects. Every read is bounded by the deadline; any
failure raises (WF07 then records ``needs_review``). Nothing here writes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re

from ai_ops.c_recovery import IMAGE_REPOSITORY, ISOLATED_REPOSITORY, RecoveryDraftStore, _values
from ai_ops.c_recovery_proof import assess_recovery_proof
from ai_ops.normal_ci_contract import NormalCIBinding
from ai_ops.normal_ci_inbox import NormalCIInbox
from ai_ops.recovery_sources import ProofObjectsReader, TokenRepoReader, make_proof_reader, values_at

AWS_ACCOUNT, AWS_REGION, ECR_REPOSITORY = '764428911194', 'ap-northeast-2', 'urlshortener'
DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')
SHA = re.compile(r'[a-f0-9]{40}\Z')


PROXY = re.compile(r'http://[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?:[1-9][0-9]{1,4}\Z')
STS_ENDPOINT = f'https://sts.{AWS_REGION}.amazonaws.com'
ECR_ENDPOINT = f'https://api.ecr.{AWS_REGION}.amazonaws.com'


def irsa_clients(role_name, *, proxy=None):
    """STS/ECR clients whose ONLY credential source is IRSA web identity.

    boto3's default chain would fall back to profiles or the instance metadata
    service (the EKS node role) when IRSA is missing; here every other provider is
    removed, and the projected role must be the configured one. Credentials stay
    deferred (no network) until the first call.

    With ``proxy`` (the fixed-destination egress proxy, design B'), every HTTPS
    call goes through it, *including* the provider's own AssumeRoleWithWebIdentity
    STS client: the region, regional STS endpoint and proxy are set on the botocore
    session before the credential resolver is built. Endpoints are pinned to the
    regional STS/ECR hosts the proxy allows.
    """
    import os
    role_arn, token_file = os.environ.get('AWS_ROLE_ARN', ''), os.environ.get('AWS_WEB_IDENTITY_TOKEN_FILE', '')
    if not token_file.startswith('/') or role_arn != f'arn:aws:iam::{AWS_ACCOUNT}:role/{role_name}':
        raise ValueError('ecr_irsa_not_configured')
    if proxy is not None and not PROXY.fullmatch(str(proxy)):
        raise ValueError('egress_proxy_rejected')
    import boto3  # runtime lock pins boto3; imported only when actually used
    import botocore.session
    from botocore.config import Config
    core = botocore.session.get_session()
    # Session-level (overrides AWS_* env/config): the provider's internal STS client
    # must resolve to exactly the regional host the proxy allows.
    core.set_config_variable('region', AWS_REGION)
    core.set_config_variable('sts_regional_endpoints', 'regional')
    core.set_config_variable('use_dualstack_endpoint', False)
    core.set_config_variable('use_fips_endpoint', False)
    # AWS_ENDPOINT_URL[_STS] would redirect the provider's AssumeRoleWithWebIdentity
    # (carrying the projected token) to an arbitrary host: never honour them here.
    core.set_config_variable('ignore_configured_endpoint_urls', True)
    config = Config(connect_timeout=2, read_timeout=3, retries={'total_max_attempts': 2, 'mode': 'standard'},
                    proxies={'https': proxy} if proxy else None)
    core.set_default_client_config(config)  # also used by the web-identity provider's STS client
    resolver = core.get_component('credential_provider')
    resolver.providers = [p for p in resolver.providers if p.METHOD == 'assume-role-with-web-identity']
    if len(resolver.providers) != 1:
        raise ValueError('ecr_irsa_not_configured')
    session = boto3.Session(botocore_session=core, region_name=AWS_REGION)
    return (session.client('sts', config=config, endpoint_url=STS_ENDPOINT),
            session.client('ecr', config=config, endpoint_url=ECR_ENDPOINT))


class EcrImageChecker:
    """``check(prior, bad) -> (ecr_response, bad_absent)`` for ``assess_recovery_proof``.

    Only the three fields the proof reads are passed on (no tags, sizes, URIs).
    """

    def __init__(self, *, role_name, now, clients=None, proxy=None):
        if not re.fullmatch(r'[A-Za-z0-9+=,.@_-]{1,64}', str(role_name)):
            raise ValueError('ecr_role_rejected')
        if proxy is not None and not PROXY.fullmatch(str(proxy)):
            raise ValueError('egress_proxy_rejected')
        self.role_name, self._now, self.proxy = role_name, now, proxy
        # Created on first use: a missing IRSA setup then stops only WF07 preparation
        # (needs_review), never the relay process that also serves Slack replies.
        self.sts, self.ecr = clients if clients is not None else (None, None)

    def _clients(self):
        if self.sts is None:
            try:
                self.sts, self.ecr = irsa_clients(self.role_name, proxy=self.proxy)
            except ValueError:
                raise
            except Exception:
                raise ValueError('ecr_irsa_not_configured') from None
        return self.sts, self.ecr

    def _in_time(self, deadline):
        if self._now() >= deadline:
            raise TimeoutError('ecr_read_deadline')

    def check(self, prior_digest, bad_digest, *, deadline):
        if not DIGEST.fullmatch(str(prior_digest)) or not DIGEST.fullmatch(str(bad_digest)):
            raise ValueError('ecr_digest_rejected')
        self._in_time(deadline)
        self._clients()
        try:  # SDK errors can carry ARNs, request IDs or messages: never pass them on
            identity = self.sts.get_caller_identity()
        except Exception:
            raise ValueError('ecr_caller_read_failed') from None
        arn = identity.get('Arn', '') if isinstance(identity, dict) else ''
        if (not isinstance(identity, dict) or identity.get('Account') != AWS_ACCOUNT
                or not arn.startswith(f'arn:aws:sts::{AWS_ACCOUNT}:assumed-role/{self.role_name}/')):
            raise ValueError('ecr_caller_rejected')
        self._in_time(deadline)
        try:
            found = self.ecr.describe_images(registryId=AWS_ACCOUNT, repositoryName=ECR_REPOSITORY,
                                             imageIds=[{'imageDigest': prior_digest}])
        except Exception:
            raise ValueError('ecr_read_failed') from None
        details = found.get('imageDetails') if isinstance(found, dict) else None
        if not isinstance(details, list):
            raise ValueError('ecr_response_rejected')
        response = {'imageDetails': [{k: d.get(k) for k in ('registryId', 'repositoryName', 'imageDigest')}
                                     for d in details if isinstance(d, dict)]}
        self._in_time(deadline)
        try:
            self.ecr.describe_images(registryId=AWS_ACCOUNT, repositoryName=ECR_REPOSITORY,
                                     imageIds=[{'imageDigest': bad_digest}])
            bad_absent = False
        except Exception as error:  # only a definite "not found" proves absence
            code = (getattr(error, 'response', None) or {}).get('Error', {}).get('Code')
            if 'ImageNotFoundException' not in (type(error).__name__, code):
                raise ValueError('ecr_read_failed') from None
            bad_absent = True
        self._in_time(deadline)
        return response, bad_absent


class LiveRecoveryProof:
    """The ``read_proof`` port of ``ImageRecoveryPRBridge``."""

    def __init__(self, *, inbox, binding, proof_reader, git_reader, ecr, now):
        if (not isinstance(inbox, NormalCIInbox) or not isinstance(binding, NormalCIBinding)
                or not isinstance(proof_reader, ProofObjectsReader) or not isinstance(ecr, EcrImageChecker)
                or getattr(git_reader, 'repository', None) != ISOLATED_REPOSITORY):
            raise ValueError('live_proof_rejected')
        if inbox.binding_hash != NormalCIInbox(inbox.queue, binding).binding_hash:
            raise ValueError('live_proof_binding_mismatch')
        self.inbox, self.binding, self.objects = inbox, binding, proof_reader
        self.git, self.ecr, self._now = git_reader, ecr, now

    def __call__(self, projection, request, *, deadline):
        objects = self.objects.read(deadline=deadline)
        source = objects['application'].get('spec', {}).get('source', {})
        revision = source.get('targetRevision') if isinstance(source, dict) else None
        if not isinstance(revision, str) or not SHA.fullmatch(revision):
            raise ValueError('live_proof_revision_rejected')
        values = values_at(self.git, revision)
        bad_digest = _values(values)['api']['image']['digest']
        moment = datetime.fromtimestamp(self._now(), timezone.utc)
        history = self.inbox.latest_ready_history(before=moment)
        prior_digest = self.binding.app.image.removeprefix(IMAGE_REPOSITORY + '@')
        ecr_response, bad_absent = self.ecr.check(prior_digest, bad_digest, deadline=deadline)
        if self._now() >= deadline:
            raise TimeoutError('live_proof_deadline')
        result = assess_recovery_proof(objects['application'], objects['deployment'], objects['lease'], values,
                                       objects['replica_set'], objects['pods'], self.binding, history,
                                       ecr_response, bad_absent, now=moment, repository=ISOLATED_REPOSITORY)
        return result, values


def load_binding(path):
    raw = Path(path).read_bytes()
    if len(raw) > 65536:
        raise ValueError('normal_binding_too_large')
    return NormalCIBinding.model_validate_json(raw)


def live_pr_bridge(config, queue, *, kube_connection=None, github_opener=None, ecr_clients=None):
    """Factory ``ledger -> ImageRecoveryPRBridge`` from ``config.prepare`` (relay only).

    Reads the (non-secret) normal-CI binding file at startup; tokens are read
    per request by the readers, never here.
    """
    from ai_ops.recovery_pr import ImageRecoveryPRBridge
    prepare, observe = config.prepare, config.observe
    if prepare is None or observe is None:
        raise ValueError('live_pr_bridge_not_configured')
    binding = load_binding(prepare.normal_binding_file)
    now = queue.now

    def factory(ledger):
        git = TokenRepoReader(prepare.github_token_file, opener=github_opener, proxy=prepare.egress_proxy)
        proof = LiveRecoveryProof(
            inbox=NormalCIInbox(queue, binding), binding=binding,
            proof_reader=make_proof_reader(observe, now=now, connection_factory=kube_connection),
            git_reader=git, ecr=EcrImageChecker(role_name=prepare.aws_role_name, now=now, clients=ecr_clients,
                                                 proxy=prepare.egress_proxy),
            now=now)
        return ImageRecoveryPRBridge(ledger, RecoveryDraftStore(Path(prepare.drafts_dir)),
                                     principals=dict(prepare.principals), reader=git, read_proof=proof,
                                     author_id=prepare.author_id, prepare_seconds=prepare.prepare_seconds)
    return factory
