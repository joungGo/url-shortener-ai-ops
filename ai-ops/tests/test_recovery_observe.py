"""A7 PR-path current-state observer against an in-memory fake API (no cluster, no network)."""
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
from contextlib import redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from ai_ops import slack_server
from ai_ops.c_recovery import IMAGE_REPOSITORY, TARGET
from ai_ops.contracts import sha256_json
from ai_ops.recovery_approval_wiring import RecoveryApprovalConfig, RecoveryApprovalService
from ai_ops.recovery_commands import ENDPOINT
from ai_ops.recovery_observe import (EVIDENCE_IDS, REPLICASETS, KubernetesRecoveryObserver, ObserveConfig,
                                     argo_path, derive_state, make_observer)
from ai_ops.restart_k8s import PODS, ApiError
from test_slack_web import Response

UID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
RS_UID = 'cccccccc-cccc-cccc-cccc-cccccccccccc'
IMAGE = IMAGE_REPOSITORY + '@sha256:' + 'a' * 64
REVISION = 'f' * 40
NOW = 1_800_000_000.0
PLAN = SimpleNamespace(plan_hash='1' * 64, event_id='evt-' + '2' * 64, target=TARGET)


class FakeApi:
    def __init__(self):
        self.calls = []
        self.status = {}
        self.deployment = {'metadata': {'name': 'lab-api', 'namespace': 'ai-ops-lab', 'uid': UID, 'generation': 4},
                           'spec': {'selector': {'matchLabels': {'app': 'lab-api'}},
                                    'template': {'spec': {'containers': [{'name': 'api', 'image': IMAGE}]}}},
                           'status': {'observedGeneration': 4, 'readyReplicas': 0}}
        self.rs = {'metadata': {'uid': RS_UID, 'ownerReferences': [{'kind': 'Deployment', 'uid': UID, 'controller': True}]},
                   'spec': {'template': {'spec': {'containers': [{'name': 'api', 'image': IMAGE}]}}}}
        self.pod = {'metadata': {'uid': 'pod-1', 'labels': {'app': 'lab-api'},
                                 'ownerReferences': [{'kind': 'ReplicaSet', 'uid': RS_UID, 'controller': True}]},
                    'spec': {'containers': [{'name': 'api', 'image': IMAGE}]},
                    'status': {'containerStatuses': [{'name': 'api', 'ready': False, 'image': IMAGE,
                                                      'state': {'waiting': {'reason': 'ImagePullBackOff'}}}]}}
        self.other = {'metadata': {'uid': 'redis-1', 'labels': {'app': 'redis'}}}
        self.pods = [self.pod, self.other]
        self.app = {'spec': {'source': {'targetRevision': REVISION}}, 'status': {'sync': {'revision': REVISION}}}
        self.app_path = argo_path('argocd', 'lab-target')

    def connect(self, host, *, port, timeout):
        api = self

        class Connection:
            def request(self, method, target, body=None, headers=None):
                self.call = (method, urlsplit(target).path)
                api.calls.append(self.call)

            def getresponse(self):
                method, path = self.call
                if path in api.status:
                    return Response(status=api.status[path], body={})
                body = {ENDPOINT: api.deployment, REPLICASETS: {'items': [api.rs]},
                        PODS: {'items': api.pods}, api.app_path: api.app}[path]
                return Response(body=body)

            def close(self):
                pass
        return Connection()


def derive(api):
    return derive_state(PLAN, api.deployment, [api.rs], api.pods, api.app,
                        observed_at=datetime.fromtimestamp(NOW, timezone.utc), proof_hash='9' * 64)


class DeriveStateTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()

    def test_current_pull_failure_is_bound_to_deployment_image_and_revision(self):
        state = derive(self.api)
        self.assertEqual(state.status, 'image_failure')
        self.assertEqual((state.deployment_uid_hash, state.image_hash, state.revision_hash),
                         (sha256_json(UID), sha256_json(IMAGE), sha256_json(REVISION)))
        self.assertEqual((state.failure.pod_uid_hash, state.failure.deployment_uid_hash, state.failure.image_hash),
                         (sha256_json('pod-1'), sha256_json(UID), sha256_json(IMAGE)))
        self.assertEqual(state.failure.observed_at, state.observed_at)
        self.assertEqual(state.failure.evidence_ids, EVIDENCE_IDS)
        self.assertEqual(state.healthy_checks, [])
        self.assertNotIn('pod-1', state.model_dump_json())

    def test_err_image_pull_also_counts(self):
        self.api.pod['status']['containerStatuses'][0]['state'] = {'waiting': {'reason': 'ErrImagePull'}}
        self.assertEqual(derive(self.api).status, 'image_failure')

    def test_anything_short_of_a_proven_pull_failure_is_unknown_never_healthy(self):
        cases = {
            'running_and_ready': lambda a: a.pod['status']['containerStatuses'][0].update(
                ready=True, state={'running': {}}),
            'crash_loop': lambda a: a.pod['status']['containerStatuses'][0].update(
                state={'waiting': {'reason': 'CrashLoopBackOff'}}),
            'ready_flag_missing': lambda a: a.pod['status']['containerStatuses'][0].pop('ready'),
            'two_pods': lambda a: a.pods.append(deepcopy(a.pod) | {'metadata': {**a.pod['metadata'], 'uid': 'pod-2'}}),
            'only_deleting_pod': lambda a: a.pod['metadata'].update(deletionTimestamp='2026-11-21T00:00:00Z'),
            'foreign_replicaset': lambda a: a.pod['metadata']['ownerReferences'][0].update(uid='other-rs'),
            'replicaset_of_other_deployment': lambda a: a.rs['metadata']['ownerReferences'][0].update(uid='other'),
            'pod_image_differs': lambda a: a.pod['spec']['containers'][0].update(image=IMAGE + 'x'),
            'status_image_differs': lambda a: a.pod['status']['containerStatuses'][0].update(image=IMAGE + 'x'),
            'replicaset_image_differs': lambda a: a.rs['spec']['template']['spec']['containers'][0].update(image='x'),
            'ready_replica': lambda a: a.deployment['status'].update(readyReplicas=1),
            'stale_generation': lambda a: a.deployment['status'].update(observedGeneration=3),
            'sidecar_container': lambda a: a.pod['status']['containerStatuses'].append({'name': 'proxy'}),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                api = FakeApi()
                change(api)
                state = derive(api)
                self.assertEqual((state.status, state.failure), ('unknown', None))
                self.assertNotEqual(state.status, 'healthy')

    def test_other_workloads_in_namespace_are_ignored(self):
        self.api.pods.append({'metadata': {'uid': 'x', 'labels': {'app': 'other'}}})
        self.assertEqual(derive(self.api).status, 'image_failure')

    def test_unverified_revision_or_deployment_identity_raises(self):
        cases = [lambda a: a.app['status']['sync'].update(revision='e' * 40),
                 lambda a: a.app['spec']['source'].pop('targetRevision'),
                 lambda a: a.deployment['metadata'].update(name='other'),
                 lambda a: a.deployment['metadata'].pop('uid'),
                 lambda a: a.deployment['spec']['template']['spec']['containers'].append({'name': 'x'}),
                 lambda a: a.deployment['spec'].pop('selector')]
        for change in cases:
            api = FakeApi()
            change(api)
            with self.subTest(), self.assertRaises(ValueError):
                derive(api)


class ObserverTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        token = Path(self.dir.name) / 'token'
        token.write_text('reader-token')
        self.config = ObserveConfig(api_url='https://kubernetes.default.svc', token_file=str(token),
                                    ca_file='/var/run/ca.crt', argo_namespace='argocd', argo_application='lab-target')
        self.now = NOW
        self.observer = make_observer(self.config, now=lambda: self.now, connection_factory=self.api.connect)

    def test_reads_only_four_fixed_paths_and_returns_bound_state(self):
        state = self.observer(PLAN, deadline=NOW + 3)
        self.assertEqual(state.status, 'image_failure')
        self.assertEqual(self.api.calls, [('GET', ENDPOINT), ('GET', REPLICASETS), ('GET', PODS),
                                          ('GET', argo_path('argocd', 'lab-target'))])
        self.assertIsInstance(self.observer, KubernetesRecoveryObserver)

    def test_proof_hash_follows_the_raw_responses(self):
        first = self.observer(PLAN, deadline=NOW + 3).proof_hash
        self.assertEqual(self.observer(PLAN, deadline=NOW + 3).proof_hash, first)
        self.api.pods.append({'metadata': {'uid': 'x', 'labels': {'app': 'other'}}})
        self.assertNotEqual(self.observer(PLAN, deadline=NOW + 3).proof_hash, first)

    def test_failures_raise_and_unsupported_target_reads_nothing(self):
        other = SimpleNamespace(**{**vars(PLAN), 'target': TARGET.model_copy(update={'workload': 'other'})})
        with self.assertRaisesRegex(ValueError, 'target_unsupported'):
            self.observer(other, deadline=NOW + 3)
        self.assertEqual(self.api.calls, [])
        self.api.status[PODS] = 403
        with self.assertRaises(ApiError):
            self.observer(PLAN, deadline=NOW + 3)
        self.api.status.clear()
        with self.assertRaises(TimeoutError):
            self.observer(PLAN, deadline=NOW)
        self.api.pods = 'not-a-list'
        with self.assertRaises(ValueError):
            self.observer(PLAN, deadline=NOW + 3)

    def test_deadline_reached_during_reads_is_rejected(self):
        def tick(*args, **kwargs):
            self.now += 1
            return self.api.connect(*args, **kwargs)
        observer = make_observer(self.config, now=lambda: self.now, connection_factory=tick)
        with self.assertRaises(TimeoutError):
            observer(PLAN, deadline=NOW + 3.5)

    def test_config_rejects_plain_http_relative_paths_and_bad_names(self):
        base = self.config.model_dump()
        for change in ({'api_url': 'http://kubernetes.default.svc'}, {'token_file': 'token'},
                       {'ca_file': 'ca.crt'}, {'argo_application': 'Lab_Target'}, {'argo_namespace': ''}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                ObserveConfig(**{**base, **change})


class ObserverWiringTest(unittest.TestCase):
    """Real WF05/06 fixtures: the configured observer opens and revalidates a real plan."""
    def setUp(self):
        import test_recovery_approvals as approvals
        self.a = approvals.RecoveryApprovalsTest()
        self.a.setUp()
        self.addCleanup(self.a.doCleanups)
        self.f = self.a.f
        self.f.snapshot(deployment_uid=UID, pod_deployment_uid=UID)
        receipt, _ = self.f.plan(self.f.investigate())
        self.key = self.f.saved(receipt['request_id']).plan_hash
        self.api = FakeApi()
        token = self.f.queue.root / 'k8s-reader-token'
        token.write_text('reader-token')
        token.chmod(0o600)
        self.observe = ObserveConfig(api_url='https://kubernetes.default.svc', token_file=str(token),
                                     ca_file='/var/run/ca.crt', argo_namespace='argocd',
                                     argo_application='lab-target')

    def service(self, observe=True):
        config = RecoveryApprovalConfig(enabled=True, prepare_proposal=self.a.policy,
                                        observe=self.observe if observe else None)
        service = RecoveryApprovalService(self.f.adapter, config, connection_factory=self.api.connect)
        self.f.adapter.recovery_approvals = service
        return service

    def ask(self):
        event = self.f.command('request-approval')
        event['event']['text'] += ' ' + self.key
        self.f.run_request(event)

    def view(self, service):
        return {v['plan_hash']: v for v in service.ledger.page()[0]}[self.key]

    def test_configured_observer_opens_then_revalidates_to_proposal_ready(self):
        service = self.service()
        self.ask()
        opened = self.view(service)
        self.assertEqual(opened['state'], 'awaiting_approval')
        self.assertEqual(opened['identity']['revision_hash'], sha256_json(REVISION))
        approved = service.ledger.decide(self.key, actor=self.a.actor, approval_hash=opened['approval_hash'],
                                         generation=opened['generation'], request_id='p', decision='approve')
        self.f.fixture.time += 1
        self.assertEqual(service.advance_once()['state'], 'proposal_ready')
        self.assertEqual(len(self.api.calls), 8)  # open + revalidate, four reads each
        self.assertTrue(all(method == 'GET' for method, _ in self.api.calls))
        self.assertFalse(approved['execution_allowed'])

    def test_recovered_or_moved_state_never_becomes_already_recovered(self):
        service = self.service()
        self.ask()
        opened = self.view(service)
        service.ledger.decide(self.key, actor=self.a.actor, approval_hash=opened['approval_hash'],
                              generation=opened['generation'], request_id='p', decision='approve')
        self.api.pod['status']['containerStatuses'][0].update(ready=True, state={'running': {}})
        self.f.fixture.time += 1
        self.assertEqual(service.advance_once()['state'], 'needs_review')
        self.assertEqual(self.view(service)['reason'], 'cause_not_verified')
        self.api.app['spec']['source']['targetRevision'] = self.api.app['status']['sync']['revision'] = 'e' * 40
        self.assertNotEqual(self.view(service)['state'], 'already_recovered')

    def test_revision_change_supersedes(self):
        service = self.service()
        self.ask()
        opened = self.view(service)
        service.ledger.decide(self.key, actor=self.a.actor, approval_hash=opened['approval_hash'],
                              generation=opened['generation'], request_id='p', decision='approve')
        self.api.app['spec']['source']['targetRevision'] = self.api.app['status']['sync']['revision'] = 'e' * 40
        self.f.fixture.time += 1
        self.assertEqual(service.advance_once()['state'], 'superseded')

    def test_without_observe_section_nothing_is_read(self):
        service = self.service(observe=False)
        self.ask()
        self.assertEqual(service.ledger.page()[0], [])
        self.assertEqual(self.api.calls, [])

    def test_check_config_reports_observe_without_reading_credentials(self):
        import test_slack_server
        s = test_slack_server.ServerTest()
        s.setUp()
        self.addCleanup(s.doCleanups)
        config = RecoveryApprovalConfig(enabled=True, prepare_proposal=self.a.policy, observe=self.observe)
        s.write('recovery-approval.json', config.model_dump())
        output = io.StringIO()
        with patch('ai_ops.restart_k8s.ApiClient.request', side_effect=AssertionError('read')), redirect_stdout(output):
            self.assertEqual(slack_server.main(s.args('check-config')), 0)
        value = json.loads(output.getvalue())
        self.assertEqual((value['recovery_approval'], value['recovery_observe']), (True, True))
        self.assertNotIn('kubernetes.default.svc', output.getvalue())
