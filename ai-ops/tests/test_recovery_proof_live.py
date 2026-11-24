"""WF07 live read sources slice B: inbox history, ECR, read_proof assembly, live bridge.

Real WF07 fixture (local Git, proof objects, drafts, approval ledger) behind fake
Kubernetes / GitHub / AWS transports. No network, no cluster, no credentials.
"""
from copy import deepcopy
from dataclasses import replace
import re
from datetime import timedelta
import io
import json
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch

from ai_ops.c_recovery import IMAGE_REPOSITORY
from ai_ops.normal_ci_inbox import NormalCIInbox
from ai_ops.recovery_approval_wiring import RecoveryApprovalConfig
from ai_ops.recovery_commands import ENDPOINT
from ai_ops.recovery_observe import REPLICASETS, ObserveConfig, argo_path
from ai_ops.recovery_proof_live import AWS_ACCOUNT, EcrImageChecker, live_pr_bridge
from ai_ops.recovery_sources import PrepareConfig
from ai_ops.restart_k8s import LEASE_PATH, PODS
from test_recovery_sources import FakeKube, Opener, TOKEN

ROLE = 'ai-ops-recovery-reader'


class ImageNotFoundException(Exception):
    pass


class FakeAws:
    def __init__(self, prior):
        self.prior, self.calls = prior, []
        self.identity = {'Account': AWS_ACCOUNT, 'Arn': f'arn:aws:sts::{AWS_ACCOUNT}:assumed-role/{ROLE}/session-1'}
        self.present = {prior}
        self.error = None

    def get_caller_identity(self):
        self.calls.append(('sts', None))
        return dict(self.identity)

    def describe_images(self, *, registryId, repositoryName, imageIds):
        digest = imageIds[0]['imageDigest']
        self.calls.append(('ecr', digest))
        if self.error:
            raise self.error
        if (registryId, repositoryName) != (AWS_ACCOUNT, 'urlshortener') or digest not in self.present:
            raise ImageNotFoundException('not found')
        return {'imageDetails': [{'registryId': AWS_ACCOUNT, 'repositoryName': 'urlshortener',
                                  'imageDigest': digest, 'imageTags': ['private-tag'], 'imageSizeInBytes': 1}]}


def valid_history(history, finished=None):
    """The PR fixture moves only ``finished_at``; shift *every* receipt time by the same
    amount instead, so started <= collected <= finished and the view hash stay valid."""
    from datetime import datetime
    from ai_ops.contracts import sha256_json
    receipt = history.receipt
    original = receipt.model_validate(receipt.model_dump(mode='json')) if False else None
    target = finished or receipt.finished_at
    import test_c_recovery_proof as proof
    base = proof.RecoveryProofTest()
    base.setUp()
    pristine = base.history.receipt  # valid, unshifted
    delta = target - pristine.finished_at

    def shift(value):
        if isinstance(value, dict):
            return {k: shift(v) for k, v in value.items()}
        if isinstance(value, list):
            return [shift(v) for v in value]
        if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T[0-9:.]+(Z|[+-]\d{2}:\d{2})', value):
            moved = datetime.fromisoformat(value.replace('Z', '+00:00')) + delta
            return moved.isoformat().replace('+00:00', 'Z')
        return value
    body = shift(pristine.model_dump(mode='json'))
    body['binding_hash'] = receipt.binding_hash
    body['view_hash'] = sha256_json(body['view'])
    shifted = type(receipt).model_validate(body)
    return replace(history, receipt=shifted)


class LiveFixture:
    def setUp(self):
        import test_recovery_pr as pr
        self.pr = pr
        self.t = t = pr.RecoveryPRTest()
        t.setUp()
        self.addCleanup(t.doCleanups)
        p = t.raw
        self.deployment = deepcopy(p.deployment)
        self.deployment['spec']['selector'] = {'matchLabels': {'app': 'lab-api'}}
        self.pods = deepcopy(p.pods)
        self.pods[0]['metadata']['labels'] = {'app': 'lab-api'}
        self.kube = FakeKube({argo_path('argocd', 'lab-target'): p.app, ENDPOINT: self.deployment,
                              LEASE_PATH: p.lease, REPLICASETS: {'items': [p.rs]}, PODS: {'items': self.pods}})
        self.prior = p.binding.app.image.removeprefix(IMAGE_REPOSITORY + '@')
        self.aws = FakeAws(self.prior)
        files = t.root / 'live'
        files.mkdir()
        (files / 'github-token').write_text(TOKEN)
        (files / 'kube-token').write_text('reader-token')
        (files / 'binding.json').write_text(p.binding.model_dump_json())
        for name in ('github-token', 'kube-token'):
            (files / name).chmod(0o600)
        self.github = Opener(lambda path: t.reader.get(path))
        self.config = RecoveryApprovalConfig(
            enabled=True, prepare_proposal=t.policy,
            observe=ObserveConfig(api_url='https://kubernetes.default.svc', token_file=str(files / 'kube-token'),
                                  ca_file='/var/run/ca.crt', argo_namespace='argocd', argo_application='lab-target'),
            prepare=PrepareConfig(principals={pr.ACTOR: 21}, author_id=22, drafts_dir=str(t.root / 'live-drafts'),
                                  github_token_file=str(files / 'github-token'),
                                  normal_binding_file=str(files / 'binding.json'), aws_role_name=ROLE))
        p.history = valid_history(p.history)
        self.inbox = NormalCIInbox(t.f.queue, p.binding)

    def ready(self, history=None):
        self.inbox.submit(history or self.t.raw.history)
        self.assertEqual(self.inbox.process_one()['state'], 'ready')

    def connect(self, *args, **kwargs):
        self.t.f.fixture.time += 0.05  # real reads take time; the recheck needs a later second proof
        return self.kube.connect(*args, **kwargs)

    def bridge(self):
        factory = live_pr_bridge(self.config, self.t.f.queue, kube_connection=self.connect,
                                 github_opener=self.github, ecr_clients=(self.aws, self.aws))
        return factory(self.t.approvals)

    def prepare(self):
        self.t.consent()
        bridge = self.bridge()
        try:
            return bridge.prepare(self.t.key, actor=self.pr.ACTOR)
        except ValueError:
            return bridge.show(self.t.key, actor=self.pr.ACTOR)


class LiveProofTest(LiveFixture, unittest.TestCase):
    def test_live_sources_prepare_the_same_exact_pr_without_writes(self):
        self.ready()
        with patch('socket.socket.connect', side_effect=AssertionError('network')):
            view = self.prepare()
        self.assertEqual((view['state'], view['url'], view['pull_number']), ('prepared', None, None))
        self.assertTrue(all(method == 'GET' for method, _ in self.kube.calls))
        self.assertTrue(all('/git/' in url for url, _, _ in self.github.requests))
        # Two proof reads per preparation; each: caller check + known-good present + failed absent.
        self.assertEqual(self.aws.calls, [('sts', None), ('ecr', self.prior), ('ecr', self.bad())] * 2)
        self.assertEqual(self.t.writer.calls, 0)

    def bad(self):
        from ai_ops.c_recovery import _values
        return _values(self.t.raw.values)['api']['image']['digest']

    def test_failed_image_still_in_registry_blocks_preparation(self):
        self.ready()
        self.aws.present.add(self.bad())
        self.assertNotEqual(self.prepare()['state'], 'prepared')

    def test_wrong_caller_or_registry_error_blocks_preparation(self):
        cases = {'other_role': {'Arn': f'arn:aws:sts::{AWS_ACCOUNT}:assumed-role/admin/s'},
                 'other_account': {'Account': '111111111111'},
                 'user_not_role': {'Arn': f'arn:aws:iam::{AWS_ACCOUNT}:user/{ROLE}'}}
        self.ready()
        for name, change in cases.items():
            with self.subTest(case=name):
                fixture = LiveFixture()
                fixture.addCleanup, fixture.assertEqual = self.addCleanup, self.assertEqual
                fixture.setUp()
                fixture.ready()
                fixture.aws.identity.update(change)
                self.assertNotEqual(fixture.prepare()['state'], 'prepared')
                self.assertFalse(any(kind == 'ecr' for kind, _ in fixture.aws.calls))

    def test_missing_or_tampered_normal_ci_history_blocks_preparation(self):
        self.assertNotEqual(self.prepare()['state'], 'prepared')  # empty inbox
        fixture = LiveFixture()
        fixture.addCleanup, fixture.assertEqual = self.addCleanup, self.assertEqual
        fixture.setUp()
        fixture.ready()
        with fixture.t.f.queue.transaction() as db:
            db.execute("UPDATE normal_ci_receipts SET fingerprint='0'")
        self.assertNotEqual(fixture.prepare()['state'], 'prepared')


class InboxHistoryTest(LiveFixture, unittest.TestCase):
    def test_latest_ready_bound_receipt_finished_before_is_chosen(self):
        history = self.t.raw.history
        self.ready()
        moved = valid_history(history, history.receipt.finished_at + timedelta(seconds=1))
        later = moved.receipt.model_copy(update={
            'identity': moved.receipt.identity.model_copy(update={'run_id': '99999'})})
        self.ready(replace(moved, run_id='99999', receipt=later))
        moment = history.receipt.finished_at + timedelta(hours=1)
        self.assertEqual(self.inbox.latest_ready_history(before=moment).run_id, '99999')
        self.assertEqual(self.inbox.latest_ready_history(before=later.finished_at).run_id, history.run_id)
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            self.inbox.latest_ready_history(before=history.receipt.finished_at)

    def test_queued_or_needs_review_receipts_are_not_used(self):
        self.inbox.submit(self.t.raw.history)
        moment = self.t.raw.history.receipt.finished_at + timedelta(hours=1)
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            self.inbox.latest_ready_history(before=moment)
        with self.t.f.queue.transaction() as db:
            db.execute("UPDATE normal_ci_receipts SET state='needs_review'")
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            self.inbox.latest_ready_history(before=moment)

    def test_receipt_of_another_binding_is_not_used(self):
        self.ready()
        other = self.t.raw.binding.model_copy(update={'config_hash': 'f' * 64})
        moment = self.t.raw.history.receipt.finished_at + timedelta(hours=1)
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            NormalCIInbox(self.t.f.queue, other).latest_ready_history(before=moment)


class EcrCheckerTest(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.aws = FakeAws('sha256:' + 'b' * 64)
        self.checker = EcrImageChecker(role_name=ROLE, now=lambda: self.now, clients=(self.aws, self.aws))

    def test_response_carries_only_proof_fields_and_absence_needs_not_found(self):
        response, absent = self.checker.check('sha256:' + 'b' * 64, 'sha256:' + 'c' * 64, deadline=200)
        self.assertEqual(response, {'imageDetails': [{'registryId': AWS_ACCOUNT, 'repositoryName': 'urlshortener',
                                                      'imageDigest': 'sha256:' + 'b' * 64}]})
        self.assertTrue(absent)
        self.aws.error = type('ClientError', (Exception,), {})('denied')
        self.aws.error.response = {'Error': {'Code': 'AccessDeniedException'}}
        with self.assertRaisesRegex(ValueError, 'ecr_read_failed|ecr_response'):
            self.checker.check('sha256:' + 'b' * 64, 'sha256:' + 'c' * 64, deadline=200)

    def test_client_error_code_not_found_counts_as_absent(self):
        class FakeClientError(Exception):
            response = {'Error': {'Code': 'ImageNotFoundException'}}
        original = self.aws.describe_images
        def describe(**kwargs):
            if kwargs['imageIds'][0]['imageDigest'] == 'sha256:' + 'c' * 64:
                raise FakeClientError('x')
            return original(**kwargs)
        self.aws.describe_images = describe
        self.assertTrue(self.checker.check('sha256:' + 'b' * 64, 'sha256:' + 'c' * 64, deadline=200)[1])

    def test_digests_deadline_and_role_validation(self):
        for bad in (('sha256:x', 'sha256:' + 'c' * 64), ('sha256:' + 'b' * 64, 'latest')):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'digest_rejected'):
                self.checker.check(*bad, deadline=200)
        with self.assertRaises(TimeoutError):
            self.checker.check('sha256:' + 'b' * 64, 'sha256:' + 'c' * 64, deadline=100)
        with self.assertRaises(ValueError):
            EcrImageChecker(role_name='bad role', now=lambda: 0, clients=(self.aws, self.aws))
        self.assertEqual(self.aws.calls, [])

    def irsa_env(self, role=ROLE):
        import os, tempfile
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        token = os.path.join(directory.name, 'token')
        with open(token, 'w') as handle:
            handle.write('projected-service-account-token')
        missing = os.path.join(directory.name, 'none')
        return {'AWS_ROLE_ARN': f'arn:aws:iam::{AWS_ACCOUNT}:role/{role}', 'AWS_WEB_IDENTITY_TOKEN_FILE': token,
                'AWS_CONFIG_FILE': missing, 'AWS_SHARED_CREDENTIALS_FILE': missing,
                'AWS_ACCESS_KEY_ID': 'AKIAFAKEFORTESTONLY0', 'AWS_SECRET_ACCESS_KEY': 'fake-secret-must-be-ignored'}

    def test_irsa_is_the_only_credential_source_and_nothing_is_fetched_eagerly(self):
        import os
        from ai_ops.recovery_proof_live import irsa_clients
        with patch.dict(os.environ, self.irsa_env()), \
             patch('socket.socket.connect', side_effect=AssertionError('network')):
            sts, ecr = irsa_clients(ROLE)
            credentials = sts._request_signer._credentials
        self.assertEqual((sts.meta.region_name, ecr.meta.region_name), ('ap-northeast-2', 'ap-northeast-2'))
        self.assertEqual((sts.meta.service_model.service_name, ecr.meta.service_model.service_name), ('sts', 'ecr'))
        # Environment keys present but ignored: only the web identity provider remains.
        self.assertEqual(credentials.method, 'assume-role-with-web-identity')
        self.assertEqual(type(credentials).__name__, 'DeferredRefreshableCredentials')

    def test_missing_or_other_irsa_role_fails_only_at_first_use_without_network(self):
        import os
        with patch('socket.socket.connect', side_effect=AssertionError('network')):
            checker = EcrImageChecker(role_name=ROLE, now=lambda: 0)  # construction never needs IRSA
            for env in ({k: '' for k in ('AWS_ROLE_ARN', 'AWS_WEB_IDENTITY_TOKEN_FILE')},
                        self.irsa_env(role='ec2-node-role')):
                with self.subTest(env=sorted(env)), patch.dict(os.environ, env), \
                     self.assertRaisesRegex(ValueError, r'^ecr_irsa_not_configured$'):
                    checker.check('sha256:' + 'b' * 64, 'sha256:' + 'c' * 64, deadline=10)


class EntrypointTest(LiveFixture, unittest.TestCase):
    def test_relay_builds_live_bridge_only_with_prepare_and_ingress_never(self):
        import test_slack_server
        from ai_ops import slack_server
        from ai_ops.recovery_pr import ImageRecoveryPRBridge
        s = test_slack_server.ServerTest()
        s.setUp()
        self.addCleanup(s.doCleanups)
        s.write('recovery-approval.json', self.config.model_dump())
        with patch.object(slack_server, 'relay_loop') as loop, redirect_stdout(io.StringIO()), \
             patch('socket.socket.connect', side_effect=AssertionError('network')):
            self.assertEqual(slack_server.main(s.args('relay', '--enable-web')), 0)
        service = loop.call_args.kwargs['recovery']
        self.assertIsInstance(service.pr_bridge, ImageRecoveryPRBridge)
        self.assertEqual(set(service.retention().ledgers), {'prepare_proposal', 'recovery_pr'})
        with patch('ai_ops.slack_server.live_pr_bridge', side_effect=AssertionError('ingress bridge')):
            app = slack_server.make_ingress(s.tasks, s.policy, s.web, self.config)
        self.assertEqual(set(app.apps), {'/slack/events', '/slack/interactivity'})
        s.write('recovery-approval.json', self.config.model_copy(update={'prepare': None}).model_dump())
        with patch.object(slack_server, 'relay_loop') as loop, redirect_stdout(io.StringIO()):
            self.assertEqual(slack_server.main(s.args('relay', '--enable-web')), 0)
        self.assertIsNone(loop.call_args.kwargs['recovery'].pr_bridge)


class GapTest(LiveFixture, unittest.TestCase):
    def test_access_error_on_the_failed_digest_is_never_read_as_absent(self):
        checker = EcrImageChecker(role_name=ROLE, now=lambda: 0, clients=(self.aws, self.aws))
        original = self.aws.describe_images
        def describe(**kwargs):
            if kwargs['imageIds'][0]['imageDigest'] != self.prior:
                denied = Exception('AccessDenied for arn:aws:iam::764428911194:role/x')
                denied.response = {'Error': {'Code': 'AccessDeniedException'}}
                raise denied
            return original(**kwargs)
        self.aws.describe_images = describe
        with self.assertRaisesRegex(ValueError, r'^ecr_read_failed$'):
            checker.check(self.prior, 'sha256:' + 'c' * 64, deadline=10)

    def test_non_sha_argo_revision_is_rejected_before_any_github_read(self):
        from ai_ops.recovery_proof_live import LiveRecoveryProof
        from ai_ops.recovery_sources import TokenRepoReader, make_proof_reader
        self.ready()
        self.t.raw.app['spec']['source']['targetRevision'] = 'main'
        proof = LiveRecoveryProof(
            inbox=self.inbox, binding=self.t.raw.binding,
            proof_reader=make_proof_reader(self.config.observe, now=self.t.f.queue.now,
                                           connection_factory=self.connect),
            git_reader=TokenRepoReader(self.config.prepare.github_token_file, opener=self.github),
            ecr=EcrImageChecker(role_name=ROLE, now=self.t.f.queue.now, clients=(self.aws, self.aws)),
            now=self.t.f.queue.now)
        with self.assertRaisesRegex(ValueError, 'live_proof_revision_rejected'):
            proof(None, None, deadline=self.t.f.queue.now() + 10)
        self.assertEqual((self.github.requests, self.aws.calls), ([], []))

    def test_bridge_receives_exactly_the_configured_principals_author_and_budget(self):
        bridge = self.bridge()
        self.assertEqual((bridge.principals, bridge.author_id, bridge.prepare_seconds),
                         ({self.pr.ACTOR: 21}, 22, 15))
        self.assertIs(bridge.approvals, self.t.approvals)
        self.assertEqual(bridge.reader.repository, 'joungGo/c03-execution-lab')
