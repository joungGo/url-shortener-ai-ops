"""A7-4 post-restart observation, sealing and Lease cleanup against a fake API and clock."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError

from ai_ops.c_recovery import IMAGE_REPOSITORY, TARGET
from ai_ops.contracts import sha256_json
from ai_ops.recovery_commands import ANNOTATION, ENDPOINT
from ai_ops.recovery_effects import CleanupItem, EffectBatch, EffectPolicy, ExecutionReceipt, verify_effect
from ai_ops.restart_effects import (EFFECT_PROMETHEUS_ROUTES, EFFECT_READER_ROUTES, LEASE_ROUTES, EffectCaptureStore,
                                    LeaseCleanupPort, ObservationConfig, RestartEffectObserver, SealedEffectReader,
                                    effect_window, restart_lease_item)
from ai_ops.restart_k8s import LEASE_PATH, ApiClient, revision_hash
from test_slack_web import Response
import test_recovery_effects as effect_fixture

UID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
DIGEST = 'sha256:' + 'a' * 64
MARKER = 'd' * 64
APPLIED = 1_800_000_000.0
RUN = 'effect-' + 'e' * 32
POD = 'lab-api-6d4f-new12'
RULE = {'name': 'WorkloadUnavailable', 'query': 'up{job="lab-api"} == 0', 'labels': {'severity': 'page'},
        'duration': 60.0, 'keepFiringFor': 0.0}
CONFIG = {'interval_seconds': 10, 'source_max_age_seconds': 60,
          'metrics': [{'name': 'error_rate', 'query': 'lab_api_error_ratio{namespace="ai-ops-lab"}',
                       'labels': {'namespace': 'ai-ops-lab'}, 'comparison': 'lte', 'threshold': 0.05}],
          'alerts': [{'definition': {**RULE, 'group': 'lab', 'file': '/etc/prometheus/rules/lab.yaml'},
                      'scope_labels': {'namespace': 'ai-ops-lab'}}]}


def dt(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc)


def ts(epoch):
    return dt(epoch).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def pod(name, uid, ready=True, deleting=False, image=DIGEST):
    meta = {'name': name, 'uid': uid}
    if deleting:
        meta['deletionTimestamp'] = ts(APPLIED)
    return {'metadata': meta, 'status': {
        'conditions': [{'type': 'Ready', 'status': 'True' if ready else 'False'}],
        'containerStatuses': [{'name': 'api', 'ready': ready, 'imageID': 'docker-pullable://x@' + image}]}}


class FakeEffectApi:
    def __init__(self, test):
        self.test, self.calls = test, []
        self.deployment = {
            'metadata': {'name': 'lab-api', 'namespace': 'ai-ops-lab', 'uid': UID, 'generation': 4},
            'spec': {'replicas': 1, 'selector': {'matchLabels': {'app': 'lab-api'}},
                     'template': {'metadata': {'labels': {'app': 'lab-api'}, 'annotations': {ANNOTATION: MARKER}},
                                  'spec': {'containers': [{'name': 'api', 'image': IMAGE_REPOSITORY + '@' + DIGEST,
                                      'readinessProbe': {'periodSeconds': 5},
                                      'startupProbe': {'periodSeconds': 5, 'failureThreshold': 24}}]}}},
            'status': {'observedGeneration': 4, 'replicas': 1, 'readyReplicas': 1}}
        self.pods = [pod(POD, 'pod-new')]
        self.http = {'/readyz': (200, {'status': 'ready'}),
                     '/items': (200, [{'id': 1, 'name': 'SECRET-ITEM-NAME', 'description': None,
                                       'created_at': '2026-01-01T00:00:00Z'}])}
        self.metric, self.metric_age, self.metric_empty = '0.01', 5.0, False
        self.rule = {**RULE, 'type': 'alerting', 'health': 'ok', 'lastError': '', 'alerts': []}
        self.lease = {'metadata': {'name': 'b6-scenario-lock', 'namespace': 'ai-ops-lab', 'uid': 'lease-uid',
                                   'resourceVersion': '21'}, 'spec': {'holderIdentity': 'local-restart-a7'}}
        self.lease_status, self.put_status = 200, 200
        self.hook = None  # called on every request (mutate state mid-run)

    def connect(self, host, *, port, timeout):
        api = self

        class Connection:
            def request(self, method, target, body=None, headers=None):
                url = urlsplit(target)
                self.call = {'method': method, 'path': url.path, 'params': parse_qs(url.query),
                             'body': json.loads(body) if body else None, 'host': host}
                api.calls.append(self.call)

            def getresponse(self):
                api.test.time += 0.05  # each read takes time
                if api.hook:
                    api.hook()
                return api.respond(self.call)

            def close(self):
                pass
        return Connection()

    def respond(self, c):
        m, p, now = c['method'], c['path'], self.test.time
        if p == ENDPOINT:
            return Response(body=self.deployment)
        if p.endswith('/pods'):
            return Response(body={'items': self.pods})
        if '/proxy/' in p:
            status, value = self.http['/' + p.rsplit('/', 1)[1]]
            return Response(status=status, body=value)
        if p == '/api/v1/query':
            query, at = c['params']['query'][0], datetime.fromisoformat(c['params']['time'][0]).timestamp()
            if self.metric_empty:
                result = []
            elif query.startswith('timestamp('):
                result = [{'metric': {'namespace': 'ai-ops-lab'}, 'value': [at, str(at - self.metric_age)]}]
            else:
                result = [{'metric': {'namespace': 'ai-ops-lab'}, 'value': [at, self.metric]}]
            return Response(body={'status': 'success', 'data': {'resultType': 'vector', 'result': result}})
        if p == '/api/v1/rules':
            rule = {**self.rule, 'lastEvaluation': ts(now - 2)}
            return Response(body={'status': 'success', 'data': {'groups': [
                {'name': 'lab', 'file': '/etc/prometheus/rules/lab.yaml', 'rules': [rule]}]}})
        if p == LEASE_PATH and m == 'GET':
            return Response(status=self.lease_status, body=self.lease)
        if p == LEASE_PATH and m == 'PUT':
            if self.put_status == 200:
                self.lease = c['body']
            return Response(status=self.put_status, body=self.lease)
        return Response(status=404, body={})


class ObserverTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.api = FakeEffectApi(self)
        self.time = APPLIED + 100
        self.sleeps = []
        clients = {}
        for name, routes, base in (('reader', EFFECT_READER_ROUTES, 'https://kubernetes.default.svc'),
                                   ('prom', EFFECT_PROMETHEUS_ROUTES, 'https://prometheus.monitoring.svc'),
                                   ('writer', LEASE_ROUTES, 'https://kubernetes.default.svc')):
            token, ca = self.root / f'{name}-token', self.root / f'{name}-ca.crt'
            token.write_text(f'{name}-token-value')
            ca.write_text('unused')
            clients[name] = ApiClient(base, token, ca, routes, now=lambda: self.time,
                                      connection_factory=self.api.connect)
        self.reader, self.prom, self.writer = clients['reader'], clients['prom'], clients['writer']
        self.observer = RestartEffectObserver(collector='local-restart-a7', reader=self.reader, prometheus=self.prom,
                                              config=CONFIG, now=lambda: self.time, sleep=self.sleep)
        self.receipt = self.make_receipt()
        start, end = effect_window(dt(APPLIED), 120, 160)
        self.policy = self.make_policy(start, end)
        self.store = EffectCaptureStore(self.root / 'effects')

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.time += seconds

    def make_receipt(self, **change):
        template = self.api.deployment['spec']['template']
        values = dict(method='command', plan_hash='1' * 64, event_id='evt-' + '2' * 64, job_id='job-' + '3' * 32,
                      target=TARGET.model_dump(mode='json'), deployment_uid=UID, application_uid=None,
                      revision=revision_hash(template), digest=DIGEST, marker=MARKER,
                      old_pod_uid_hash=sha256_json('pod-old'), generation=4, applied_at=dt(APPLIED), applied=True,
                      cancelled=False, source_hash='4' * 64, lease_hash=None, lease_released=True,
                      retained_until=dt(APPLIED + 86400))
        return ExecutionReceipt.model_validate({**values, **change})

    def make_policy(self, start, end, **change):
        cfg = ObservationConfig.model_validate(CONFIG)
        values = dict(window_start=start, window_end=end, minimum_span_seconds=120, maximum_gap_seconds=45,
                      minimum_samples=4, maximum_report_delay_seconds=120, read_seconds=15,
                      http_paths=['/readyz', '/items'],
                      metrics=[m.requirement().model_dump() for m in cfg.metrics],
                      alerts=[a.requirement().model_dump() for a in cfg.alerts])
        return EffectPolicy.model_validate({**values, **change})

    def run_and_verify(self, **kw):
        out = self.observer.observe(self.receipt, self.policy, run_id=RUN, **kw)
        sealed = self.store.seal(out['batch'])
        self.time = self.policy.window_end.timestamp() + 1
        reader = SealedEffectReader(self.root / 'effects', sealed['sealed_sha256'], now=lambda: self.time)
        batch = reader.read(self.receipt, self.policy, deadline=self.time + 15)
        return out, sealed, verify_effect(self.receipt, self.policy, batch, now=dt(self.time))

    def check(self, report, kind):
        return next(c for c in report['checks'] if c['kind'] == kind)

    # --- complete observation -------------------------------------------------------
    def test_complete_restart_observation_succeeds_from_sealed_capture_only(self):
        out, sealed, report = self.run_and_verify()
        self.assertEqual(report['status'], 'succeeded', report['checks'])
        self.assertEqual((report['provenance'], report['execution_allowed'], out['reasons']), ('captured', False, []))
        batch = EffectBatch.model_validate(out['batch'])
        self.assertEqual((batch.run_id, batch.collector), (RUN, 'local-restart-a7'))
        samples = [x for s in batch.series for x in s.samples]
        self.assertTrue(all(x.source_sha256 for x in samples))
        self.assertTrue(all(len(s.samples) >= 4 for s in batch.series))
        self.assertTrue(all(self.policy.window_start <= x.at <= self.policy.window_end for x in samples))
        path = self.root / 'effects' / f'{sealed["sealed_sha256"]}.json'
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertNotIn(b'SECRET-ITEM-NAME', path.read_bytes())  # bodies are hashed, never stored
        self.assertEqual({c['method'] for c in self.api.calls}, {'GET'})
        proxied = {c['path'] for c in self.api.calls if '/proxy/' in c['path']}
        self.assertEqual(proxied, {f'/api/v1/namespaces/ai-ops-lab/pods/{POD}:8000/proxy/readyz',
                                   f'/api/v1/namespaces/ai-ops-lab/pods/{POD}:8000/proxy/items'})
        self.assertGreaterEqual(min(x.at for x in samples).timestamp(), self.policy.window_start.timestamp())
        self.assertTrue(all(s <= 1.0 for s in self.sleeps))

    # --- must not succeed -------------------------------------------------------------
    def test_pod_not_ready_after_startup_allowance_is_unresolved(self):
        self.api.pods = [pod(POD, 'pod-new', ready=False)]
        self.api.deployment['status']['readyReplicas'] = 0
        _, _, report = self.run_and_verify()
        self.assertEqual(report['status'], 'unresolved')
        self.assertEqual(self.check(report, 'runtime')['state'], 'failed')

    def test_old_pod_still_present_or_other_marker_is_not_recovery(self):
        self.api.pods = [pod('lab-api-6d4f-old12', 'pod-old', deleting=True), pod(POD, 'pod-new')]
        self.assertEqual(self.run_and_verify()[2]['status'], 'unresolved')
        self.setUp()
        self.api.deployment['spec']['template']['metadata']['annotations'][ANNOTATION] = 'f' * 64
        self.receipt = self.make_receipt(revision=revision_hash(self.api.deployment['spec']['template']))
        self.assertEqual(self.run_and_verify()[2]['status'], 'unresolved')

    def test_running_image_differs_from_receipt_is_unresolved(self):
        self.api.pods = [pod(POD, 'pod-new', image='sha256:' + 'b' * 64)]
        self.assertEqual(self.check(self.run_and_verify()[2], 'runtime')['state'], 'failed')

    def test_tick_delay_beyond_gap_is_unknown_not_success(self):
        state = {'n': 0}
        base = self.sleep

        def slow(seconds):
            state['n'] += 1
            base(seconds + (40 if state['n'] == 40 else 0))
        self.observer._sleep = slow
        _, _, report = self.run_and_verify()
        self.assertEqual(report['status'], 'verification_unknown')
        self.assertIn('unordered_or_gapped_samples', self.check(report, 'runtime')['reasons'])

    def test_empty_or_lookback_stale_metric_is_unknown(self):
        self.api.metric_empty = True
        out, _, report = self.run_and_verify()
        self.assertEqual(report['status'], 'verification_unknown')
        self.assertIn('metric_collection_failed', out['reasons'])
        self.setUp()
        self.api.metric_age = 200  # value present but the underlying sample is old (lookback-filled)
        self.assertEqual(self.run_and_verify()[2]['status'], 'verification_unknown')

    def test_metric_threshold_breach_is_unresolved(self):
        self.api.metric = '0.5'
        self.assertEqual(self.run_and_verify()[2]['status'], 'unresolved')

    def test_http_contract_mismatch_is_unresolved_and_proxy_error_unknown(self):
        self.api.http['/readyz'] = (200, {'status': 'starting'})
        self.assertEqual(self.run_and_verify()[2]['status'], 'unresolved')
        self.setUp()
        self.api.http['/items'] = (503, {'kind': 'Status'})
        _, _, report = self.run_and_verify()
        self.assertEqual(report['status'], 'verification_unknown')
        self.assertEqual([c['state'] for c in report['checks'] if c['name'] == '/items'], ['unknown'])

    def test_rule_error_definition_drift_and_firing_alert(self):
        self.api.rule['health'] = 'err'
        self.assertEqual(self.run_and_verify()[2]['status'], 'verification_unknown')
        self.setUp()
        self.api.rule['query'] = 'up == 0'  # live rule differs from the pinned definition
        report = self.run_and_verify()[2]
        self.assertEqual(report['status'], 'verification_unknown')
        self.assertIn('definition_mismatch', self.check(report, 'alert')['reasons'])
        self.setUp()
        start = self.policy.window_start.timestamp() + 60
        self.api.hook = lambda: self.api.rule.update(query='changed') if self.time > start else None
        out, _, report = self.run_and_verify()
        self.assertEqual(report['status'], 'verification_unknown')
        self.assertIn('alert_collection_failed', out['reasons'])
        self.setUp()
        self.api.rule['alerts'] = [{'labels': {'namespace': 'ai-ops-lab'}, 'state': 'firing'}]
        self.assertEqual(self.run_and_verify()[2]['status'], 'unresolved')

    def test_identity_change_or_cancellation_makes_every_series_unknown(self):
        start = self.policy.window_start.timestamp() + 30
        self.api.hook = lambda: (self.api.deployment['metadata'].update(uid='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb')
                                 if self.time > start else None)
        out, _, report = self.run_and_verify()
        self.assertEqual(report['status'], 'verification_unknown')
        self.assertIn('observation_identity_changed', out['reasons'])
        self.assertTrue(all(c['reasons'] == ['missing_or_failed_collection'] for c in report['checks']))
        self.setUp()
        out, _, report = self.run_and_verify(cancelled=lambda: self.time > start)
        self.assertEqual((report['status'], out['reasons']), ('verification_unknown', ['cancelled']))

    # --- preconditions ----------------------------------------------------------------
    def test_window_rules_are_enforced_before_any_sampling(self):
        cases = [
            (self.make_policy(*effect_window(dt(APPLIED), 60, 160)), 'effect_window_before_startup'),
            (self.make_policy(*effect_window(dt(APPLIED), 120, 160), maximum_gap_seconds=20),
             'effect_interval_exceeds_gap'),
            (self.make_policy(*effect_window(dt(APPLIED), 120, 130)), 'effect_window_too_short_for_span'),
            (self.make_policy(*effect_window(dt(APPLIED), 120, 160),
                              metrics=[{'name': 'error_rate', 'definition_hash': '9' * 64, 'comparison': 'lte',
                                        'threshold': 0.05}]), 'effect_observation_config_mismatch'),
        ]
        for policy, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                self.observer.observe(self.receipt, policy, run_id=RUN)
        with self.assertRaisesRegex(ValueError, 'effect_window_after_retention'):
            self.observer.observe(self.make_receipt(retained_until=dt(APPLIED + 300)), self.policy, run_id=RUN)
        with self.assertRaisesRegex(ValueError, 'effect_observation_receipt_rejected'):
            self.observer.observe(self.make_receipt(cancelled=True), self.policy, run_id=RUN)
        with self.assertRaisesRegex(ValueError, 'effect_run_id_rejected'):
            self.observer.observe(self.receipt, self.policy, run_id='effect-x')
        self.time = self.policy.window_start.timestamp() + 1
        with self.assertRaisesRegex(ValueError, 'effect_window_already_started'):
            self.observer.observe(self.receipt, self.policy, run_id=RUN)
        self.assertFalse([c for c in self.api.calls if '/proxy/' in c['path'] or c['path'].startswith('/api/v1/')
                          and 'namespaces' not in c['path']])

    def test_routes_are_fixed_get_only(self):
        for path in ('/api/v1/namespaces/ai-ops-lab/pods/x/log', '/api/v1/namespaces/ai-ops-lab/secrets',
                     f'/api/v1/namespaces/ai-ops-lab/pods/{POD}:9000/proxy/readyz',
                     f'/api/v1/namespaces/ai-ops-lab/pods/{POD}:8000/proxy/admin',
                     f'/api/v1/namespaces/other/pods/{POD}:8000/proxy/readyz'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'kubernetes_route_rejected'):
                self.reader.request('GET', path, deadline=self.time + 5)
        with self.assertRaisesRegex(ValueError, 'kubernetes_route_rejected'):
            self.reader.request('POST', f'/api/v1/namespaces/ai-ops-lab/pods/{POD}:8000/proxy/items',
                                deadline=self.time + 5)
        for path in ('/api/v1/query_range', '/api/v1/admin/tsdb/delete_series'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'kubernetes_route_rejected'):
                self.prom.request('GET', path, deadline=self.time + 5)
        self.assertEqual(self.api.calls, [])

    # --- sealed batch -----------------------------------------------------------------
    def test_sealed_reader_rejects_tamper_symlink_other_receipt_and_late_read(self):
        out = self.observer.observe(self.receipt, self.policy, run_id=RUN)
        sealed = self.store.seal(out['batch'])['sealed_sha256']
        directory = self.root / 'effects'
        reader = SealedEffectReader(directory, sealed, now=lambda: self.time)
        with self.assertRaisesRegex(ValueError, 'effect_seal_binding_rejected'):
            reader.read(self.make_receipt(plan_hash='9' * 64), self.policy, deadline=self.time + 15)
        with self.assertRaises(TimeoutError):
            reader.read(self.receipt, self.policy, deadline=self.time)
        path = directory / f'{sealed}.json'
        original = path.read_bytes()
        path.write_bytes(original.replace(b'"status": "available"', b'"status": "missing"  ', 1))
        with self.assertRaisesRegex(ValueError, 'effect_seal_mismatch'):
            reader.read(self.receipt, self.policy, deadline=self.time + 15)
        path.unlink()
        (directory / 'real.json').write_bytes(original)
        os.chmod(directory / 'real.json', 0o600)
        path.symlink_to(directory / 'real.json')
        with self.assertRaises(OSError):
            reader.read(self.receipt, self.policy, deadline=self.time + 15)
        with self.assertRaisesRegex(ValueError, 'effect_seal_requires_capture'):
            self.store.seal(effect_fixture.batch_for(effect_fixture_receipt(), effect_fixture.policy_for(
                effect_fixture_receipt())))

    def test_capture_provenance_contract(self):
        batch = self.observer.observe(self.receipt, self.policy, run_id=RUN)['batch']
        for change in ({'run_id': None}, {'collector': None}, {'provenance': 'synthetic'}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                EffectBatch.model_validate({**batch, **change})
        unhashed = deepcopy(batch)
        unhashed['series'][0]['samples'][0]['source_sha256'] = None
        with self.assertRaises(ValidationError):
            EffectBatch.model_validate(unhashed)

    # --- Lease cleanup ----------------------------------------------------------------
    def lease_item(self, holder='local-restart-a7'):
        return restart_lease_item({'name': 'b6-scenario-lock', 'namespace': 'ai-ops-lab', 'uid': 'lease-uid',
                                   'holder': holder, 'verified_until': APPLIED + 60})

    def test_lease_cleanup_confirms_release_and_clears_only_own(self):
        port, item = LeaseCleanupPort(self.writer), self.lease_item()
        self.assertEqual(port.inspect(item, deadline=self.time + 3),
                         {'present': True, 'owner_hash': sha256_json('local-restart-a7')})
        port.dispose(item, deadline=self.time + 3)
        put = [c for c in self.api.calls if c['method'] == 'PUT'][-1]['body']
        self.assertNotIn('holderIdentity', put['spec'])
        self.assertEqual(put['metadata']['resourceVersion'], '21')  # optimistic concurrency
        self.assertEqual(port.inspect(item, deadline=self.time + 3), {'present': False})
        self.api.lease['spec'] = {'holderIdentity': 'someone-else'}
        with self.assertRaisesRegex(ValueError, 'restart_lease_not_owned'):
            port.dispose(item, deadline=self.time + 3)
        self.assertEqual(sum(c['method'] == 'PUT' for c in self.api.calls), 1)
        self.api.lease['metadata']['uid'] = 'recreated'
        self.assertEqual(port.inspect(item, deadline=self.time + 3), {'present': False})
        self.api.lease_status = 404
        self.assertEqual(port.inspect(item, deadline=self.time + 3), {'present': False})

    def test_lease_cleanup_conflict_unknown_and_other_kinds(self):
        port, item = LeaseCleanupPort(self.writer), self.lease_item()
        self.api.put_status = 409
        with self.assertRaisesRegex(ValueError, 'restart_lease_cleanup_conflict'):
            port.dispose(item, deadline=self.time + 3)
        self.api.put_status = 500
        with self.assertRaisesRegex(OSError, 'restart_lease_cleanup_unknown'):
            port.dispose(item, deadline=self.time + 3)
        other = CleanupItem(kind='credential', item_hash='1' * 64, owner_hash='2' * 64, action='revoke_credential')
        with self.assertRaisesRegex(ValueError, 'cleanup_kind_unsupported'):
            port.inspect(other, deadline=self.time + 3)
        for bad in ({}, {'name': 'b6-scenario-lock', 'namespace': 'ai-ops-lab', 'uid': 'u', 'holder': ''}):
            with self.assertRaisesRegex(ValueError, 'restart_lease_record_rejected'):
                restart_lease_item(bad)


def effect_fixture_receipt():
    return ExecutionReceipt.model_validate(dict(
        method='command', plan_hash='1' * 64, event_id='evt-' + '2' * 64, job_id='job-' + '3' * 32,
        target=TARGET.model_dump(mode='json'), deployment_uid=UID, application_uid=None, revision='5' * 64,
        digest=DIGEST, marker=MARKER, old_pod_uid_hash='6' * 64, generation=4, applied_at=dt(APPLIED), applied=True,
        cancelled=False, source_hash='4' * 64, lease_hash=None, lease_released=True,
        retained_until=dt(APPLIED + 86400)))


class CoordinatorStillBlocksLiveTest(unittest.TestCase):
    def test_wf09_coordinator_refuses_sealed_reader_observer_and_lease_port(self):
        f = effect_fixture.EffectTest()
        f.setUp()
        self.addCleanup(f.doCleanups)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        reader = SealedEffectReader(tmp.name, 'a' * 64, now=lambda: 0)
        writer = type('Writer', (), {'request': lambda *a, **k: self.fail('no cleanup request expected')})()
        for live in (reader, type('Observer', (), {'mode': RestartEffectObserver.mode})()):
            with self.assertRaisesRegex(ValueError, 'live_effect_reader_disabled'):
                f.c.verify_local(f.key, actor=effect_fixture.ACTOR, reader=live)
        with self.assertRaisesRegex(ValueError, 'live_cleanup_disabled'):
            f.c.cleanup_local(f.key, actor=effect_fixture.ACTOR, port=LeaseCleanupPort(writer))


if __name__ == '__main__':
    unittest.main()
