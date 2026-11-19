"""A7-3a Kubernetes adapter against an in-memory fake API (no cluster, no network)."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from ai_ops.c_recovery import IMAGE_REPOSITORY
from ai_ops.contracts import sha256_json
from ai_ops.recovery_commands import ANNOTATION, ENDPOINT, patch_request
from ai_ops.restart_gates import REQUIRED_EVIDENCE, evaluate
from ai_ops.restart_k8s import (ApiClient, KubernetesRestartPort, LEASE_PATH, PROMETHEUS_ROUTES, PrometheusSignals,
                                READER_ROUTES, WRITER_ROUTES, derive_snapshot, revision_hash)
from test_slack_web import Response

UID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
IMAGE = IMAGE_REPOSITORY + '@sha256:' + 'a' * 64
NOW = 1_800_000_000.0


def ts(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class FakeApi:
    def __init__(self):
        self.calls = []
        self.deployment = {'metadata': {'name': 'lab-api', 'namespace': 'ai-ops-lab', 'uid': UID, 'resourceVersion': '7',
                                        'generation': 3, 'managedFields': [{'secret': 'noise'}]},
                           'spec': {'replicas': 1, 'strategy': {'type': 'Recreate'},
                                    'selector': {'matchLabels': {'app': 'lab-api'}},
                                    'template': {'metadata': {'labels': {'app': 'lab-api'}, 'annotations': {'x': 'y'}},
                                                 'spec': {'containers': [{'name': 'api', 'image': IMAGE,
                                                     'readinessProbe': {'periodSeconds': 5},
                                                     'startupProbe': {'periodSeconds': 5, 'failureThreshold': 24}}]}}},
                           'status': {'observedGeneration': 3, 'replicas': 1, 'updatedReplicas': 1}}
        self.pod = {'metadata': {'name': 'lab-api-6d4f-xyz12', 'uid': 'pod-1', 'labels': {'pod-template-hash': '6d4f'}},
                    'status': {'startTime': ts(NOW - 3600),
                               'conditions': [{'type': 'Ready', 'status': 'False', 'lastTransitionTime': ts(NOW - 900)}],
                               'containerStatuses': [{'name': 'api', 'state': {'running': {}}, 'restartCount': 0,
                                                      'imageID': 'docker-pullable://x@sha256:' + 'b' * 64}]}}
        self.pods = [self.pod]
        self.hpas, self.events = [], [{'reason': 'Unhealthy'}]
        self.app = {'spec': {'syncPolicy': {}}}
        self.app_status = 200
        self.lease = {'metadata': {'name': 'b6-scenario-lock', 'namespace': 'ai-ops-lab', 'uid': 'lease-uid',
                                   'resourceVersion': '11'}, 'spec': {}}
        self.logs = {False: (200, b'current log line\n'), True: (400, b'')}
        self.prom = {'prior': [{'value': [0, '1']}], 'deps': [{'value': [0, '1']}]}
        self.patch_status = 200
        self.put_status = 200

    def connect(self, host, *, port, timeout):
        api = self

        class Connection:
            def request(self, method, target, body=None, headers=None):
                url = urlsplit(target)
                self.call = {'method': method, 'path': url.path, 'params': parse_qs(url.query),
                             'body': json.loads(body) if body else None, 'headers': headers, 'host': host}
                api.calls.append(self.call)

            def getresponse(self):
                return api.respond(self.call)

            def close(self):
                pass
        return Connection()

    def respond(self, c):
        m, p = c['method'], c['path']
        if p == ENDPOINT and m == 'GET':
            return Response(body=self.deployment)
        if p == ENDPOINT and m == 'PATCH':
            return Response(status=self.patch_status, body={})
        if p.endswith('/pods') and m == 'GET':
            return Response(body={'items': self.pods})
        if p.endswith('/log'):
            status, raw = self.logs[c['params'].get('previous') == ['true']]
            return Response(status=status, raw=raw)
        if p.endswith('/events'):
            return Response(body={'items': self.events})
        if p.endswith('/horizontalpodautoscalers'):
            return Response(body={'items': self.hpas})
        if '/applications/' in p:
            return Response(status=self.app_status, body=self.app)
        if p == LEASE_PATH and m == 'GET':
            return Response(body=self.lease)
        if p == LEASE_PATH and m == 'PUT':
            if self.put_status == 200:
                self.lease = {**c['body'], 'metadata': {**c['body']['metadata'], 'resourceVersion': '12'}}
            return Response(status=self.put_status, body=self.lease)
        if p == '/api/v1/query':
            key = 'prior' if 'template' in c['params']['query'][0] else 'deps'
            return Response(body={'status': 'success', 'data': {'resultType': 'vector', 'result': self.prom[key]}})
        return Response(status=404, body={})


class AdapterFixture:
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.api = FakeApi()
        self.time = NOW
        files = {}
        for name in ('reader', 'writer', 'prom'):
            token = self.root / f'{name}-token'
            token.write_text(f'{name}-token-value')
            ca = self.root / f'{name}-ca.crt'
            ca.write_text('unused')
            files[name] = (token, ca)
        now = lambda: self.time
        self.reader = ApiClient('https://kubernetes.default.svc', *files['reader'], READER_ROUTES, now=now,
                                connection_factory=self.api.connect)
        self.writer = ApiClient('https://kubernetes.default.svc', *files['writer'], WRITER_ROUTES, now=now,
                                connection_factory=self.api.connect)
        prom = ApiClient('https://prometheus.monitoring.svc:9090', *files['prom'], PROMETHEUS_ROUTES, now=now,
                         connection_factory=self.api.connect)
        self.signals = PrometheusSignals(prom, prior_ready='max_over_time(ready{template="$TEMPLATE_HASH"}[1d])',
                                         dependencies=['min(db_up)'])
        self.port = KubernetesRestartPort(executor_id='local-restart-test', reader=self.reader, writer=self.writer,
                                          signals=self.signals, evidence_dir=self.root / 'evidence', now=now,
                                          argo_application=('argocd', 'ai-ops-lab'))

    def read(self):
        return self.port.read(deadline=self.time + 10)


class ReadDerivationTest(AdapterFixture, unittest.TestCase):
    def test_snapshot_fields_from_api_objects(self):
        s = self.read()
        self.assertEqual((s.deployment_uid, s.resource_version, s.generation, s.observed_generation), (UID, '7', 3, 3))
        self.assertEqual((s.strategy, s.ready_replicas, s.container_running, s.restart_count), ('Recreate', 0, True, 0))
        self.assertEqual(s.readiness_failing_since.timestamp(), NOW - 900)
        self.assertEqual(s.pod_started_at.timestamp(), NOW - 3600)
        self.assertEqual(s.readiness_window_seconds, 120)  # max(readiness 3x5, startup 24x5)
        self.assertEqual((s.prior_ready, s.dependencies), ('observed', 'pass'))
        self.assertEqual(s.pod_uid_hash, sha256_json('pod-1'))
        self.assertEqual(s.image_id_hash, sha256_json('docker-pullable://x@sha256:' + 'b' * 64))
        self.assertFalse(s.rollout_in_progress or s.autoscaling or s.gitops_auto_sync)
        self.assertTrue(s.annotations_exist)
        self.assertIsNone(s.restart_marker)
        self.assertEqual(s.scheduling_signals, [])

    def test_revision_hash_ignores_only_restart_annotation(self):
        template = self.api.deployment['spec']['template']
        marked = deepcopy(template)
        marked['metadata']['annotations'][ANNOTATION] = 'm' * 64
        self.assertEqual(revision_hash(template), revision_hash(marked))
        changed = deepcopy(template)
        changed['spec']['containers'][0]['image'] = IMAGE_REPOSITORY + '@sha256:' + 'c' * 64
        self.assertNotEqual(revision_hash(template), revision_hash(changed))

    def test_fail_closed_signals(self):
        cases = [
            (lambda a: a.hpas.append({'spec': {'scaleTargetRef': {'kind': 'Deployment', 'name': 'lab-api'}}}),
             'autoscaling', True),
            (lambda a: a.app.update(spec={'syncPolicy': {'automated': {}}}), 'gitops_auto_sync', True),
            (lambda a: setattr(a, 'app_status', 404), 'gitops_auto_sync', True),
            (lambda a: a.prom.update(prior=[]), 'prior_ready', 'unknown'),
            (lambda a: a.prom.update(prior=[{'value': [0, '0']}]), 'prior_ready', 'absent'),
            (lambda a: a.prom.update(deps=[{'value': [0, '0']}]), 'dependencies', 'fail'),
            (lambda a: a.prom.update(deps=[]), 'dependencies', 'unknown'),
            (lambda a: a.deployment['status'].update(updatedReplicas=0), 'rollout_in_progress', True),
            (lambda a: a.pod['status']['containerStatuses'][0].update(state={'waiting': {}}), 'container_running', False),
            (lambda a: a.pod['status']['conditions'][0].update(status='True'), 'readiness_failing_since', None),
        ]
        for mutate, field, expected in cases:
            with self.subTest(field=field, expected=expected):
                self.setUp()
                mutate(self.api)
                self.assertEqual(getattr(self.read(), field), expected)
        self.setUp()
        self.signals.prior_query = None
        self.signals.dependency_queries = ()
        s = self.read()
        self.assertEqual((s.prior_ready, s.dependencies), ('unknown', 'unknown'))

    def test_promql_braces_and_placeholder(self):
        self.read()
        query = [c for c in self.api.calls if c['path'] == '/api/v1/query'][0]['params']['query'][0]
        self.assertEqual(query, 'max_over_time(ready{template="6d4f"}[1d])')
        self.api.pod['metadata']['labels']['pod-template-hash'] = '6d4f"}or vector(1)#'
        self.assertEqual(self.read().prior_ready, 'unknown')  # injected label value never reaches PromQL
    def test_inconsistent_objects_raise(self):
        for mutate in (lambda a: a.pods.append(deepcopy(a.pod)),
                       lambda a: a.deployment['spec']['template']['spec']['containers'][0].pop('readinessProbe'),
                       lambda a: a.deployment['spec']['template']['spec']['containers'][0].update(image='lab:latest'),
                       lambda a: a.deployment['spec'].update(replicas=2),
                       lambda a: a.pod['status'].pop('startTime')):
            self.setUp()
            mutate(self.api)
            with self.assertRaises(ValueError):
                self.read()

    def test_derived_snapshot_feeds_the_3b_gates(self):
        s = self.read()
        plan = type('Plan', (), dict(deployment_uid=UID, resource_version='7', revision_hash=s.revision_hash,
                                     image_hash=sha256_json(IMAGE), pod_uid_hash=s.pod_uid_hash, generation=3,
                                     strategy='Recreate', annotations_exist=True, restart_count=0))
        verdicts = {g.name: g.verdict for g in evaluate(plan, s, NOW)}
        self.assertTrue(all(v == 'pass' for v in verdicts.values()), verdicts)


class EvidenceTest(AdapterFixture, unittest.TestCase):
    def test_bundle_is_private_hashed_and_complete(self):
        result = self.port.capture_evidence(deadline=self.time + 10)
        self.assertEqual(result['items'], list(REQUIRED_EVIDENCE))
        path = self.root / 'evidence' / f"{result['bundle_sha256']}.json"
        data = path.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), result['bundle_sha256'])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        bundle = json.loads(data)
        self.assertEqual(set(bundle), set(REQUIRED_EVIDENCE))
        self.assertEqual(bundle['logs_previous'], {'absent': True})
        self.assertIn('current log line', bundle['logs_current']['text'])
        self.assertNotIn('managedFields', json.dumps(bundle['describe']))
        self.assertNotIn('current log line', json.dumps(result))  # logs never leave the private file

    def test_capture_fails_closed(self):
        for mutate in (lambda a: a.logs.update({False: (500, b'')}), lambda a: a.pods.append(deepcopy(a.pod)),
                       lambda a: a.logs.update({True: (403, b'')})):
            self.setUp()
            mutate(self.api)
            with self.assertRaises((OSError, ValueError)):
                self.port.capture_evidence(deadline=self.time + 10)


class WriteAndLeaseTest(AdapterFixture, unittest.TestCase):
    def request(self, exist=True):
        return patch_request(UID, '7', 'm' * 64, exist)

    def test_only_the_sealed_patch_is_sent_by_the_writer(self):
        for exist in (True, False):
            self.api.calls.clear()
            self.assertEqual(self.port.patch(self.request(exist), deadline=self.time + 10), {'accepted': True})
            call = self.api.calls[-1]
            self.assertEqual((call['method'], call['path']), ('PATCH', ENDPOINT))
            self.assertEqual(call['body'], self.request(exist)['body'])
            self.assertEqual(call['headers']['Content-Type'], 'application/json-patch+json')
            self.assertEqual(call['headers']['Authorization'], 'Bearer writer-token-value')
        self.api.calls.clear()
        tampered = self.request()
        tampered['body'].append({'op': 'replace', 'path': '/spec/replicas', 'value': 0})
        for bad in (tampered, {**self.request(), 'endpoint': '/apis/apps/v1/namespaces/default/deployments/x'},
                    {**self.request(), 'content_type': 'application/merge-patch+json'}, None):
            with self.assertRaisesRegex(ValueError, 'patch_request_rejected'):
                self.port.patch(bad, deadline=self.time + 10)
        self.assertEqual(self.api.calls, [])

    def test_patch_outcomes(self):
        self.api.patch_status = 422
        with self.assertRaisesRegex(ValueError, 'json_patch_test_conflict'):
            self.port.patch(self.request(), deadline=self.time + 10)
        self.api.patch_status = 500
        with self.assertRaisesRegex(OSError, 'outcome_unknown'):
            self.port.patch(self.request(), deadline=self.time + 10)

    def test_lease_acquire_inspect_release_only_own(self):
        held = self.port.acquire(owner='manual-abc', deadline=self.time + 3)
        self.assertEqual((held['holder'], held['uid']), ('manual-abc', 'lease-uid'))
        self.assertEqual(held['verified_until'], NOW + 60)
        put = [c for c in self.api.calls if c['method'] == 'PUT'][-1]
        self.assertEqual(put['body']['metadata']['resourceVersion'], '11')  # optimistic concurrency
        self.assertEqual(self.port.inspect_lease(held, deadline=self.time + 3)['holder'], 'manual-abc')
        self.assertTrue(self.port.release(held, deadline=self.time + 3))
        self.assertNotIn('holderIdentity', self.api.lease['spec'])

    def test_lease_never_taken_over_or_released_for_others(self):
        self.api.lease['spec'] = {'holderIdentity': 'someone-else', 'renewTime': '2020-01-01T00:00:00.000000Z',
                                  'leaseDurationSeconds': 1}
        with self.assertRaisesRegex(ValueError, 'lease_held'):
            self.port.acquire(owner='manual-abc', deadline=self.time + 3)
        self.assertFalse(self.port.release({'uid': 'lease-uid', 'holder': 'manual-abc'}, deadline=self.time + 3))
        self.assertEqual([c['method'] for c in self.api.calls if c['path'] == LEASE_PATH], ['GET', 'GET'])
        self.setUp()
        self.api.put_status = 409
        with self.assertRaisesRegex(ValueError, 'lease_conflict'):
            self.port.acquire(owner='manual-abc', deadline=self.time + 3)


class ClientBoundaryTest(AdapterFixture, unittest.TestCase):
    def test_route_allowlists_separate_identities(self):
        with self.assertRaisesRegex(ValueError, 'route_rejected'):
            self.reader.request('PATCH', ENDPOINT, deadline=self.time + 3, body=[])
        with self.assertRaisesRegex(ValueError, 'route_rejected'):
            self.writer.request('GET', '/api/v1/namespaces/ai-ops-lab/pods', deadline=self.time + 3)
        for path in ('/api/v1/namespaces/ai-ops-lab/secrets', '/apis/apps/v1/namespaces/ai-ops-lab/deployments/other',
                     '/api/v1/namespaces/ai-ops-lab/pods/../secrets/x/log'):
            with self.assertRaisesRegex(ValueError, 'route_rejected'):
                self.reader.request('GET', path, deadline=self.time + 3)
        self.assertEqual(self.api.calls, [])

    def test_deadline_token_rotation_redirect_and_size(self):
        with self.assertRaises(TimeoutError):
            self.reader.request('GET', ENDPOINT, deadline=self.time)
        (self.root / 'reader-token').write_text('rotated-token')
        self.reader.request('GET', ENDPOINT, deadline=self.time + 3)
        self.assertEqual(self.api.calls[-1]['headers']['Authorization'], 'Bearer rotated-token')
        original = self.api.respond
        self.api.respond = lambda c: Response(status=302, body={})
        with self.assertRaises(OSError):
            self.reader.request('GET', ENDPOINT, deadline=self.time + 3)
        self.api.respond = lambda c: Response(raw=b'{' + b' ' * (2 * 1024 * 1024 + 2) + b'}')
        with self.assertRaises(OSError):
            self.reader.request('GET', ENDPOINT, deadline=self.time + 3)
        self.api.respond = original
        for url in ('http://kubernetes.default.svc', 'https://h/path'):
            with self.assertRaises(ValueError):
                ApiClient(url, '/t', '/c', READER_ROUTES, now=lambda: 0)

    def test_executor_still_refuses_live_port(self):
        import test_recovery_commands as commands
        f = commands.RestartCommandTest()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.consent()
        live = KubernetesRestartPort(executor_id='local-restart-test', reader=self.reader, writer=self.writer,
                                     signals=self.signals, evidence_dir=self.root / 'ev2', now=lambda: self.time,
                                     argo_application=('argocd', 'ai-ops-lab'))
        with self.assertRaisesRegex(ValueError, 'live_command_executor_disabled'):
            f.coordinator.execute_local(f.key, actor=commands.ACTOR, ledger=f.ledger, port=live)
        self.assertEqual([c for c in self.api.calls if c['method'] in ('PATCH', 'PUT')], [])


if __name__ == '__main__':
    unittest.main()
