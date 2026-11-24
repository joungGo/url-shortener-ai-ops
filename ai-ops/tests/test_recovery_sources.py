"""WF07 live sources slice A: token GitHub Git reader, Kubernetes proof objects, config.

Fake HTTP opener over a real local Git repository and an in-memory fake Kubernetes
API. No network, no cluster, no credentials.
"""
from copy import deepcopy
from datetime import timedelta
import os
import re
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler

from ai_ops.c_recovery import _encoded_values
from ai_ops.c_recovery_proof import assess_recovery_proof
from ai_ops.ci_token_reader import NoRedirect
from ai_ops.recovery_approval_wiring import RecoveryApprovalConfig
from ai_ops.recovery_approvals import ApprovalPolicy
from ai_ops.recovery_commands import ENDPOINT
from ai_ops.recovery_observe import REPLICASETS, ObserveConfig, argo_path
from ai_ops.recovery_sources import (GITHUB_API, PrepareConfig, TokenRepoReader, make_proof_reader,
                                     proof_routes, values_at)
from ai_ops.restart_k8s import LEASE_PATH, PODS
from test_slack_web import Response

REPO_API = 'repos/joungGo/c03-execution-lab'
SHA = 'a' * 40
TOKEN = 'github_pat_' + 'A1b2' * 10


class Reply:
    def __init__(self, body, status=200):
        self.raw, self.status = body if isinstance(body, bytes) else json.dumps(body).encode(), status

    def read(self, n=-1):
        return self.raw if n < 0 else self.raw[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Opener:
    """Records requests; answers from a callable (endpoint -> body) or raises."""
    def __init__(self, answer):
        self.answer, self.requests = answer, []

    def open(self, request, timeout):
        self.requests.append((request.full_url, dict(request.header_items()), timeout))
        result = self.answer(urlsplit(request.full_url).path.lstrip('/'))
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, Reply) else Reply(result)


class TokenReaderTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.token = Path(self.dir.name) / 'github-token'
        self.token.write_text(TOKEN + '\n')
        self.token.chmod(0o600)
        self.opener = Opener(lambda path: {'sha': SHA})
        self.reader = TokenRepoReader(str(self.token), opener=self.opener)

    def test_only_git_data_endpoints_of_the_isolated_repository(self):
        for ok in (f'{REPO_API}/git/ref/heads/main', f'{REPO_API}/git/commits/{SHA}',
                   f'{REPO_API}/git/trees/{SHA}', f'{REPO_API}/git/blobs/{SHA}'):
            self.assertEqual(self.reader.get(ok), {'sha': SHA})
        for bad in ('user', f'{REPO_API}/pulls/1', f'{REPO_API}/git/commits/{SHA[:-1]}',
                    f'{REPO_API}/git/trees/{SHA}?recursive=1', f'repos/f-lab-edu/F-Lab-DevOps/git/commits/{SHA}',
                    f'{REPO_API}/git/refs/heads/main', f'/{REPO_API}/git/commits/{SHA}', None):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'path_rejected'):
                self.reader.get(bad)
        self.assertEqual(len(self.opener.requests), 4)

    def test_token_only_in_header_and_reread_on_every_request(self):
        self.reader.get(f'{REPO_API}/git/commits/{SHA}')
        url, headers, timeout = self.opener.requests[-1]
        self.assertEqual(url, GITHUB_API + f'{REPO_API}/git/commits/{SHA}')
        self.assertEqual(headers['Authorization'], 'Bearer ' + TOKEN)
        self.assertNotIn(TOKEN, url)
        self.assertEqual(timeout, 8)
        rotated = 'ghs_' + 'Z9' * 20
        self.token.write_text(rotated)
        self.reader.get(f'{REPO_API}/git/commits/{SHA}')
        self.assertEqual(self.opener.requests[-1][1]['Authorization'], 'Bearer ' + rotated)

    def test_unsafe_or_malformed_token_file_is_rejected_before_any_request(self):
        for mode, text in ((0o620, TOKEN), (0o602, TOKEN), (0o600, 'short'), (0o600, TOKEN + ' x'),
                           (0o600, 'a' * 5000)):
            self.token.write_text(text)
            self.token.chmod(mode)
            with self.subTest(mode=oct(mode)), self.assertRaises(ValueError):
                self.reader.get(f'{REPO_API}/git/commits/{SHA}')
        self.assertEqual(self.opener.requests, [])
        with self.assertRaisesRegex(ValueError, 'path_rejected'):
            TokenRepoReader('relative-token')

    def test_failures_are_generic_and_never_carry_bodies(self):
        cases = {
            'http_error': HTTPError(GITHUB_API, 404, 'secret-detail', {}, io.BytesIO(b'private-body')),
            'redirect_not_followed': HTTPError(GITHUB_API, 302, 'Found', {'Location': 'https://evil.invalid'},
                                               io.BytesIO(b'')),
            'network': OSError('connection reset by 10.0.0.1'),
            'non_200': Reply({'sha': SHA}, status=203),
        }
        for name, answer in cases.items():
            reader = TokenRepoReader(str(self.token), opener=Opener(lambda path, a=answer: a))
            with self.subTest(case=name), self.assertRaisesRegex(ValueError, r'^github_read_failed$'):
                reader.get(f'{REPO_API}/git/commits/{SHA}')
        for body, code in ((b'x' * (2 * 1024 * 1024 + 1), 'too_large'), (b'[1]', 'response_rejected'),
                           (b'not json', 'response_rejected')):
            reader = TokenRepoReader(str(self.token), opener=Opener(lambda path, b=body: Reply(b)))
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                reader.get(f'{REPO_API}/git/commits/{SHA}')

    def test_default_opener_uses_no_proxy_and_never_follows_redirects(self):
        reader = TokenRepoReader(str(self.token))
        handlers = reader.opener.handlers
        self.assertTrue(any(isinstance(h, NoRedirect) for h in handlers))
        with patch.dict(os.environ, {'https_proxy': 'http://proxy.invalid:3128',
                                     'HTTPS_PROXY': 'http://proxy.invalid:3128'}):
            handlers = TokenRepoReader(str(self.token)).opener.handlers
        self.assertEqual([h for h in handlers if isinstance(h, ProxyHandler) and h.proxies], [])
        with self.assertRaises(ValueError):
            TokenRepoReader(str(self.token), read_seconds=16)


class GitSourceTest(unittest.TestCase):
    """The token reader is a drop-in WF07 reader over real Git objects."""
    def setUp(self):
        import test_recovery_pr as pr
        self.t = pr.RecoveryPRTest()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        token = self.t.root / 'github-token'
        token.write_text(TOKEN)
        token.chmod(0o600)
        self.opener = Opener(lambda path: self.t.reader.get(path))
        self.reader = TokenRepoReader(str(token), opener=self.opener)
        self.pr = pr

    def test_values_at_revision_matches_the_committed_lab_values(self):
        self.assertEqual(values_at(self.reader, self.t.base), _encoded_values(self.pr.BAD).encode())
        self.assertTrue(all(url.startswith(GITHUB_API + REPO_API + '/git/') for url, _, _ in self.opener.requests))

    def test_bridge_prepares_the_same_exact_pr_through_the_token_reader(self):
        from ai_ops.recovery_pr import ImageRecoveryPRBridge
        t = self.t
        t.bridge = ImageRecoveryPRBridge(t.approvals, t.drafts, principals={self.pr.ACTOR: 21},
                                         reader=self.reader, read_proof=t.proof, author_id=22, prepare_seconds=15)
        view = t.prepare()
        self.assertEqual((view['state'], view['url']), ('prepared', None))
        self.assertGreater(len(self.opener.requests), 5)


class FakeKube:
    def __init__(self, objects):
        self.objects, self.calls = objects, []

    def connect(self, host, *, port, timeout):
        kube = self

        class Connection:
            def request(self, method, target, body=None, headers=None):
                self.path = urlsplit(target).path
                kube.calls.append((method, self.path))

            def getresponse(self):
                return Response(body=kube.objects[self.path])

            def close(self):
                pass
        return Connection()


class ProofObjectsTest(unittest.TestCase):
    def setUp(self):
        import test_c_recovery_proof as proof
        self.p = proof.RecoveryProofTest()
        self.p.setUp()
        p = self.p
        self.deployment = deepcopy(p.deployment)
        self.deployment['spec']['selector'] = {'matchLabels': {'app': 'lab-api'}}
        self.pods = deepcopy(p.pods)
        self.pods[0]['metadata']['labels'] = {'app': 'lab-api'}
        self.other = {'metadata': {'uid': 'redis-1', 'labels': {'app': 'redis'}}}
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        token = Path(self.dir.name) / 'kube-token'
        token.write_text('reader-token')
        self.config = ObserveConfig(api_url='https://kubernetes.default.svc', token_file=str(token),
                                    ca_file='/var/run/ca.crt', argo_namespace='argocd', argo_application='lab-target')
        self.now = 1_800_000_000.0
        self.when = p.history.receipt.finished_at + timedelta(hours=2)  # same as the proof fixture

    def kube(self, pods=None, replica_sets=None):
        return FakeKube({argo_path('argocd', 'lab-target'): self.p.app, ENDPOINT: self.deployment,
                         LEASE_PATH: self.p.lease, REPLICASETS: {'items': replica_sets or [self.p.rs]},
                         PODS: {'items': pods if pods is not None else [*self.pods, self.other]}})

    def read(self, kube, deadline=None):
        reader = make_proof_reader(self.config, now=lambda: self.now, connection_factory=kube.connect)
        return reader.read(deadline=deadline or self.now + 5)

    def test_objects_feed_the_existing_recovery_proof_unchanged(self):
        kube = self.kube()
        objects = self.read(kube)
        self.assertEqual([c for c in kube.calls], [('GET', p) for p in (
            argo_path('argocd', 'lab-target'), ENDPOINT, LEASE_PATH, REPLICASETS, PODS)])
        self.assertEqual((objects['replica_set'], objects['pods']), (self.p.rs, self.pods))
        p = self.p
        result = assess_recovery_proof(objects['application'], objects['deployment'], objects['lease'],
                                       p.values, objects['replica_set'], objects['pods'], p.binding, p.history,
                                       p.ecr, True, now=self.when)
        self.assertTrue(result['proof_ready'], result['blockers'])

    def test_ambiguous_pod_binding_leaves_replica_set_empty_so_proof_blocks(self):
        two = [*self.pods, deepcopy(self.pods[0]) | {'metadata': {**self.pods[0]['metadata'], 'uid': 'pod-2'}}]
        foreign = deepcopy(self.pods)
        foreign[0]['metadata']['ownerReferences'][0]['uid'] = 'rs-other'
        stray_rs = deepcopy(self.p.rs)
        stray_rs['metadata']['ownerReferences'][0]['uid'] = 'other-deployment'
        for name, kube in {'two_pods': self.kube(pods=two), 'foreign_owner': self.kube(pods=foreign),
                           'rs_of_other_deployment': self.kube(replica_sets=[stray_rs])}.items():
            with self.subTest(case=name):
                objects = self.read(kube)
                self.assertEqual(objects['replica_set'], {})
                p = self.p
                result = assess_recovery_proof(objects['application'], objects['deployment'], objects['lease'],
                                               p.values, objects['replica_set'], objects['pods'], p.binding,
                                               p.history, p.ecr, True, now=self.when)
                self.assertFalse(result['proof_ready'])
                self.assertIn('current_pod_binding_unverified', result['blockers'])

    def test_deadline_identity_and_fixed_application_are_enforced(self):
        with self.assertRaises(TimeoutError):
            self.read(self.kube(), deadline=self.now)
        kube = self.kube()
        def slow(*args, **kwargs):
            self.now += 1  # each read takes a second: the fifth ends past the deadline
            return kube.connect(*args, **kwargs)
        reader = make_proof_reader(self.config, now=lambda: self.now, connection_factory=slow)
        with self.assertRaisesRegex(TimeoutError, 'proof_read_deadline'):
            reader.read(deadline=self.now + 4.5)
        self.assertEqual(len(kube.calls), 5)
        self.deployment['spec'].pop('selector')
        with self.assertRaisesRegex(ValueError, 'proof_deployment_rejected'):
            self.read(self.kube())
        other = self.config.model_copy(update={'argo_application': 'other-app'})
        with self.assertRaisesRegex(ValueError, 'argo_application_rejected'):
            make_proof_reader(other, now=lambda: self.now)
        self.assertTrue(any(m == 'GET' and re.fullmatch(p, LEASE_PATH) for m, p in proof_routes()))
        self.assertTrue(all(m == 'GET' for m, _ in proof_routes()))


class PrepareConfigTest(unittest.TestCase):
    def base(self, **changes):
        value = dict(principals={'UALICE01': 21}, author_id=22, drafts_dir='/data/incidents/drafts',
                     github_token_file='/run/ai-github/token')
        value.update(changes)
        return value

    def test_valid_and_rejected_shapes(self):
        self.assertEqual(PrepareConfig(**self.base()).prepare_seconds, 15)
        for changes in ({'principals': {}}, {'principals': {'alice': 21}}, {'principals': {'UALICE01': 0}},
                        {'author_id': 21}, {'principals': {'UALICE01': 21, 'UBOB0001': 21}},
                        {'drafts_dir': 'drafts'}, {'github_token_file': 'token'}, {'prepare_seconds': 16},
                        {'unexpected': True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                PrepareConfig(**self.base(**changes))

    def test_approval_config_requires_observe_and_mapped_approvers(self):
        policy = ApprovalPolicy(version=1, approvers=['UALICE01'], wait_seconds=60, approval_seconds=60,
                                observation_seconds=30, revalidation_seconds=3)
        observe = ObserveConfig(api_url='https://kubernetes.default.svc', token_file='/t', ca_file='/c',
                                argo_namespace='argocd', argo_application='lab-target')
        ok = RecoveryApprovalConfig(enabled=True, prepare_proposal=policy, observe=observe,
                                    prepare=PrepareConfig(**self.base()))
        self.assertEqual(ok.prepare.author_id, 22)
        with self.assertRaisesRegex(ValueError, 'prepare_requires_observe'):
            RecoveryApprovalConfig(enabled=True, prepare_proposal=policy, prepare=PrepareConfig(**self.base()))
        with self.assertRaisesRegex(ValueError, 'prepare_principal_missing'):
            RecoveryApprovalConfig(enabled=True, prepare_proposal=policy, observe=observe,
                                   prepare=PrepareConfig(**self.base(principals={'UBOB0001': 21})))

    def test_check_config_reports_prepare_without_reading_tokens_or_paths(self):
        import test_slack_server
        from contextlib import redirect_stdout
        from ai_ops import slack_server
        s = test_slack_server.ServerTest()
        s.setUp()
        self.addCleanup(s.doCleanups)
        policy = ApprovalPolicy(version=1, approvers=['UALICE01'], wait_seconds=60, approval_seconds=60,
                                observation_seconds=30, revalidation_seconds=3)
        observe = ObserveConfig(api_url='https://kubernetes.default.svc', token_file='/t', ca_file='/c',
                                argo_namespace='argocd', argo_application='lab-target')
        config = RecoveryApprovalConfig(enabled=True, prepare_proposal=policy, observe=observe,
                                        prepare=PrepareConfig(**self.base()))
        s.write('recovery-approval.json', config.model_dump())
        output = io.StringIO()
        with patch('ai_ops.recovery_sources.TokenRepoReader._token', side_effect=AssertionError('token')), \
             redirect_stdout(output):
            self.assertEqual(slack_server.main(s.args('check-config')), 0)
        value = json.loads(output.getvalue())
        self.assertTrue(value['recovery_prepare_configured'])
        self.assertNotIn('/run/ai-github', output.getvalue())
