"""A7 healthy judgment (user decisions 2026-11-25): all five plan checks or unknown.

Pure derivation and the read-only observer against an in-memory fake API, then the
ledger/relay/outbox flow with real WF05/06 fixtures. No cluster, no network.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import test_recovery_approval_wiring as wiring
from test_recovery_observe import IMAGE, NOW, PLAN, FakeApi as BaseApi
from test_slack_web import Response
from ai_ops.recovery_approval_wiring import ALREADY_HEALTHY
from ai_ops.recovery_commands import ENDPOINT
from ai_ops.recovery_observe import (HEALTH_CHECKS, REPLICASETS, STABLE_SECONDS, ObserveConfig, argo_path,
                                     derive_state, health_target, make_observer)
from ai_ops.restart_k8s import PODS

DIGEST = IMAGE.split('@')[1]
POD = 'lab-api-6d4f-xyz12'


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class HealthyApi(BaseApi):
    """The same workload after a natural recovery: one Ready Pod on the desired digest."""
    def __init__(self):
        super().__init__()
        self.deployment['spec']['replicas'] = 1
        self.deployment['status'] = {'observedGeneration': 4, 'replicas': 1, 'readyReplicas': 1,
                                     'updatedReplicas': 1, 'availableReplicas': 1}
        self.pod['metadata']['name'] = POD
        self.pod['status'] = {
            'phase': 'Running',
            'conditions': [{'type': 'Ready', 'status': 'True', 'lastTransitionTime': stamp(NOW - 120)}],
            'containerStatuses': [{'name': 'api', 'ready': True, 'image': IMAGE, 'state': {'running': {}},
                                   'imageID': 'docker-pullable://registry.example/x@' + DIGEST}]}
        self.app['status'].update(sync={'revision': self.app['status']['sync']['revision'], 'status': 'Synced'},
                                  health={'status': 'Healthy'})
        self.http = {'/readyz': (200, json.dumps({'status': 'ready'}).encode()),
                     '/items': (200, json.dumps([{'id': 1, 'name': 'a', 'description': None,
                                                  'created_at': '2026-11-25T00:00:00Z'}]).encode())}

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
                prefix = f'{PODS}/{POD}:8000/proxy'
                if path.startswith(prefix):
                    status, raw = api.http[path[len(prefix):]]
                    return Response(status=status, raw=raw)
                body = {ENDPOINT: api.deployment, REPLICASETS: {'items': [api.rs]},
                        PODS: {'items': api.pods}, api.app_path: api.app}[path]
                return Response(body=body)

            def close(self):
                pass
        return Connection()


def derive(api, http=None, now=NOW):
    return derive_state(PLAN, api.deployment, [api.rs], api.pods, api.app,
                        observed_at=datetime.fromtimestamp(now, timezone.utc), proof_hash='9' * 64,
                        http={'/readyz': True, '/items': True} if http is None else http)


class DeriveHealthyTest(unittest.TestCase):
    def test_all_five_checks_report_healthy_without_raw_values(self):
        state = derive(HealthyApi())
        self.assertEqual((state.status, state.healthy_checks, state.failure), ('healthy', HEALTH_CHECKS, None))
        self.assertEqual(HEALTH_CHECKS, ['argo_synced_healthy', 'pod_image_matches_desired_digest', 'pod_ready',
                                         'readyz_success', 'items_flow_success'])
        self.assertNotIn(POD, state.model_dump_json())
        self.assertNotIn(DIGEST, state.model_dump_json())

    def test_ready_must_have_held_for_the_stable_window(self):
        self.assertEqual(STABLE_SECONDS, 60)  # user decision 2026-11-25 (fixed literal, not derived)
        api = HealthyApi()
        api.pod['status']['conditions'][0]['lastTransitionTime'] = stamp(NOW - 60)
        self.assertEqual(derive(api).status, 'healthy')
        api.pod['status']['conditions'][0]['lastTransitionTime'] = stamp(NOW - 59)
        self.assertEqual(derive(api).status, 'unknown')
        api.pod['status']['conditions'][0]['lastTransitionTime'] = stamp(NOW + 30)  # future: never
        self.assertEqual(derive(api).status, 'unknown')

    def test_any_single_missing_or_failing_check_is_unknown(self):
        status = lambda a: a.pod['status']['containerStatuses'][0]
        cases = {
            'no_http_proof': (lambda a: None, {}),
            'http_not_read': (lambda a: None, {'/readyz': None, '/items': True}),
            'readyz_contract_invalid': (lambda a: None, {'/readyz': False, '/items': True}),
            'items_unreadable': (lambda a: None, {'/readyz': True, '/items': None}),
            'argo_out_of_sync': (lambda a: a.app['status']['sync'].update(status='OutOfSync'), None),
            'argo_progressing': (lambda a: a.app['status']['health'].update(status='Progressing'), None),
            'argo_health_missing': (lambda a: a.app['status'].pop('health'), None),
            'tag_not_digest': (lambda a: (a.deployment['spec']['template']['spec']['containers'][0].update(image='x:latest'),
                                          a.rs['spec']['template']['spec']['containers'][0].update(image='x:latest'),
                                          a.pod['spec']['containers'][0].update(image='x:latest'),
                                          status(a).update(image='x:latest')), None),
            'running_digest_differs': (lambda a: status(a).update(imageID='x@sha256:' + 'b' * 64), None),
            'running_digest_missing': (lambda a: status(a).pop('imageID'), None),
            'pod_spec_image_differs': (lambda a: a.pod['spec']['containers'][0].update(image=IMAGE + 'x'), None),
            'replicaset_image_differs': (lambda a: a.rs['spec']['template']['spec']['containers'][0].update(image='y'), None),
            'container_not_ready': (lambda a: status(a).update(ready=False), None),
            'container_waiting': (lambda a: status(a).update(state={'waiting': {'reason': 'ContainerCreating'}}), None),
            'phase_pending': (lambda a: a.pod['status'].update(phase='Pending'), None),
            'ready_condition_false': (lambda a: a.pod['status']['conditions'][0].update(status='False'), None),
            'ready_condition_missing': (lambda a: a.pod['status'].update(conditions=[]), None),
            'ready_time_malformed': (lambda a: a.pod['status']['conditions'][0].update(lastTransitionTime='soon'), None),
            'rollout_not_observed': (lambda a: a.deployment['status'].update(observedGeneration=3), None),
            'not_updated': (lambda a: a.deployment['status'].update(updatedReplicas=0), None),
            'not_available': (lambda a: a.deployment['status'].pop('availableReplicas'), None),
            'unavailable_replica': (lambda a: a.deployment['status'].update(unavailableReplicas=1), None),
            'scaled_to_two': (lambda a: a.deployment['spec'].update(replicas=2), None),
            'two_pods': (lambda a: a.pods.append(deepcopy(a.pod) | {'metadata': {**a.pod['metadata'], 'uid': 'pod-2'}}), None),
            'sidecar': (lambda a: a.pod['status']['containerStatuses'].append({'name': 'proxy', 'ready': True}), None),
        }
        for name, (change, http) in cases.items():
            with self.subTest(case=name):
                api = HealthyApi()
                change(api)
                state = derive(api, http=http)
                self.assertEqual((state.status, state.healthy_checks), ('unknown', []))

    def test_pull_failure_still_wins_and_http_targets_only_a_ready_pod(self):
        failing = BaseApi()
        failing.pod['metadata']['name'] = POD
        self.assertEqual(derive(failing).status, 'image_failure')
        self.assertIsNone(health_target(failing.deployment, [failing.rs], failing.pods))
        healthy = HealthyApi()
        self.assertEqual(health_target(healthy.deployment, [healthy.rs], healthy.pods), POD)
        healthy.pod['metadata']['name'] = 'Bad/Name'
        self.assertIsNone(health_target(healthy.deployment, [healthy.rs], healthy.pods))


class ObserverHealthyTest(unittest.TestCase):
    def setUp(self):
        self.api = HealthyApi()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        token = Path(self.dir.name) / 'token'
        token.write_text('reader-token')
        config = ObserveConfig(api_url='https://kubernetes.default.svc', token_file=str(token),
                               ca_file='/var/run/ca.crt', argo_namespace='argocd', argo_application='lab-target')
        self.observer = make_observer(config, now=lambda: NOW, connection_factory=self.api.connect)

    def test_reads_fixed_get_paths_including_pod_proxy_and_reports_healthy(self):
        state = self.observer(PLAN, deadline=NOW + 3)
        self.assertEqual(state.status, 'healthy')
        proxy = f'{PODS}/{POD}:8000/proxy'
        self.assertEqual(self.api.calls, [('GET', ENDPOINT), ('GET', REPLICASETS), ('GET', PODS),
                                          ('GET', argo_path('argocd', 'lab-target')),
                                          ('GET', proxy + '/readyz'), ('GET', proxy + '/items')])

    def test_failing_pod_is_never_probed_over_http(self):
        api = BaseApi()
        api.pod['metadata']['name'] = POD
        observer = make_observer(self.observer.config, now=lambda: NOW, connection_factory=api.connect)
        self.assertEqual(observer(PLAN, deadline=NOW + 3).status, 'image_failure')
        self.assertEqual(len(api.calls), 4)

    def test_http_errors_and_bad_bodies_are_unknown_and_change_the_proof(self):
        first = self.observer(PLAN, deadline=NOW + 3).proof_hash
        for path, response in [('/readyz', (503, b'{}')), ('/items', (200, b'not json')),
                               ('/items', (200, b'[{"id": NaN}]')), ('/readyz', (200, b'{"status": "starting"}'))]:
            with self.subTest(path=path, response=response):
                api = HealthyApi()
                api.http[path] = response
                observer = make_observer(self.observer.config, now=lambda: NOW, connection_factory=api.connect)
                state = observer(PLAN, deadline=NOW + 3)
                self.assertEqual(state.status, 'unknown')
                self.assertNotEqual(state.proof_hash, first)

    def test_proxy_route_is_fixed(self):
        client = self.observer.client
        with self.assertRaises(ValueError):
            client.request('GET', f'{PODS}/{POD}:8000/proxy/admin', deadline=NOW + 3, raw=True)
        with self.assertRaises(ValueError):
            client.request('POST', f'{PODS}/{POD}:8000/proxy/items', deadline=NOW + 3, raw=True)
        with self.assertRaises(ValueError):
            client.request('GET', f'{PODS}/{POD}:9000/proxy/readyz', deadline=NOW + 3, raw=True)


class LedgerFlowTest(wiring.WiringFixture, unittest.TestCase):
    def healthy(self):
        self.a.overrides = {'status': 'healthy', 'healthy_checks': list(HEALTH_CHECKS), 'failure': None}

    def test_open_while_already_healthy_replies_without_any_record(self):
        self.healthy()
        with patch('socket.socket.connect', side_effect=AssertionError('network')):
            text = self.ask()
        self.assertTrue(text.startswith(ALREADY_HEALTHY), text)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.service.sync_once()['checked'], 0)

    def test_revalidation_healthy_is_already_recovered_and_announced_without_change(self):
        self.ask()
        view = self.records()[0]
        self.service.ledger.decide(self.key, actor=self.actor, approval_hash=view['approval_hash'],
                                   generation=view['generation'], request_id='r1', decision='approve')
        self.healthy()
        with patch('subprocess.Popen', side_effect=AssertionError('executor')):
            step = self.service.advance_once()
        self.assertEqual(step, {'action': 'revalidate', 'state': 'already_recovered', 'execution_allowed': False})
        self.assertEqual(self.service.advance_once()['action'], 'none')  # nothing after a terminal state
        self.service.sync_once()
        while self.service.deliver_once(self.transport)['state'] == 'delivered':
            pass
        self.assertIn('재확인 결과 이미 정상으로 확인되어 변경하지 않습니다', self.posts()[-1]['text'])
        self.assertEqual(self.records()[0]['state'], 'already_recovered')

    def test_partial_health_stays_manual_review(self):
        self.ask()
        view = self.records()[0]
        self.service.ledger.decide(self.key, actor=self.actor, approval_hash=view['approval_hash'],
                                   generation=view['generation'], request_id='r1', decision='approve')
        self.a.overrides = {'status': 'unknown', 'failure': None}
        self.assertEqual(self.service.advance_once()['state'], 'needs_review')


if __name__ == '__main__':
    unittest.main()
