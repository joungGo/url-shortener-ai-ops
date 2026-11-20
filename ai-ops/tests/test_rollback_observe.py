"""PR-path rollback observation over the real WF07/WF09 ledgers and a fake Argo/K8s/GitHub API."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from ai_ops.c_recovery import IMAGE_REPOSITORY, REPOSITORY
from ai_ops.recovery_effects import (CleanupItem, EffectBatch, EffectCoordinator, EffectPolicy, RollbackScope,
                                     pr_effect_source)
from ai_ops.restart_effects import (EFFECT_PROMETHEUS_ROUTES, EFFECT_READER_ROUTES, EffectCaptureStore,
                                    ObservationConfig, RestartEffectObserver, SealedEffectReader, body,
                                    effect_window)
from ai_ops.restart_k8s import ApiClient
from ai_ops.rollback_observe import RollbackAppliedReader, RollbackObservationReader
from test_slack_web import Response
import test_recovery_effects as effects
import test_recovery_pr as pr_fixture
from test_restart_effects import CONFIG, POD, FakeEffectApi, pod, ts

ACTOR = effects.ACTOR
REVISION, DIGEST = '9' * 40, 'sha256:' + '8' * 64
ARGO = ('argocd', 'lab-target')
RUN = 'effect-' + '7' * 32


class Clock:
    def __init__(self, fixture):
        self.fixture = fixture

    @property
    def time(self):
        return self.fixture.time

    @time.setter
    def time(self, value):
        self.fixture.time = value


class ArgoApi(FakeEffectApi):
    def __init__(self, clock, e):
        super().__init__(clock)
        assert e.application_uid != 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee' and e.deployment_uid != 'ffffffff-ffff-ffff-ffff-ffffffffffff'
        self.deployment['metadata']['uid'] = e.deployment_uid
        self.deployment['spec']['template']['spec']['containers'][0]['image'] = IMAGE_REPOSITORY + '@' + DIGEST
        self.pods = [pod(POD, 'pod-restored', image=DIGEST)]
        self.app = {'metadata': {'name': 'lab-target', 'namespace': 'argocd', 'uid': e.application_uid},
                    'spec': {'source': {'repoURL': f'https://github.com/{REPOSITORY}.git', 'path': 'ai-ops/lab',
                                        'targetRevision': REVISION}, 'syncPolicy': {}},
                    'status': {'sync': {'status': 'Synced', 'revision': REVISION}, 'health': {'status': 'Healthy'},
                               'operationState': {'phase': 'Succeeded', 'syncResult': {'revision': REVISION},
                                                  'finishedAt': ts(clock.time)}}}

    def respond(self, c):
        if '/applications/' in c['path']:
            self.app['metadata']['resourceVersion'] = str(len(self.calls))  # changes every read
            return Response(body=self.app)
        return super().respond(c)


class GitHub:
    def __init__(self, sha):
        self.sha, self.paths = sha, []

    def get(self, path):
        self.paths.append(path)
        if self.sha is None:
            raise OSError('token-value unavailable')
        return {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': self.sha}}


def policy_for(window, **change):
    cfg = ObservationConfig.model_validate(CONFIG)
    values = dict(window_start=window[0], window_end=window[1], minimum_span_seconds=120, maximum_gap_seconds=45,
                  minimum_samples=4, maximum_report_delay_seconds=120, read_seconds=15,
                  http_paths=['/readyz', '/items'], metrics=[m.requirement().model_dump() for m in cfg.metrics],
                  alerts=[a.requirement().model_dump() for a in cfg.alerts])
    return EffectPolicy.model_validate({**values, **change})


class RollbackObserveTest(unittest.TestCase):
    def setUp(self):
        # Real WF07 PR guard path up to a verified sync (same as the WF09 PR fixture).
        self.f = pr_fixture.RecoveryPRTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        locked, request, response = self.f.merged_local()
        self.f.bridge.plan_sync(self.f.key, actor=ACTOR, merge_response=response, live_reader=self.f.live)
        snapshot = dict(checked_at=self.f.now().isoformat(), application=deepcopy(self.f.raw.app),
                        deployment=deepcopy(self.f.raw.deployment), lease=deepcopy(self.f.raw.lease))
        app = snapshot['application']
        app['spec']['source']['targetRevision'] = response['sha']
        app['status'].update(sync={'status': 'Synced', 'revision': response['sha']},
                             operationState={'phase': 'Succeeded', 'syncResult': {'revision': response['sha']}})
        snapshot['deployment']['spec']['template']['spec']['containers'][0]['image'] = self.f.raw.binding.app.image
        self.f.bridge.record_sync(self.f.key, actor=ACTOR, snapshot=snapshot)
        self.key, self.clock = self.f.key, Clock(self.f.f.fixture)
        self.source = pr_effect_source(self.f.bridge)
        self.e = self.source(self.key, ACTOR)
        # First (synthetic) verification is unresolved, then the exact prior scope is a rollback candidate.
        first = policy_for((self.e.applied_at + timedelta(seconds=1), self.e.applied_at + timedelta(seconds=121)))
        self.c = EffectCoordinator(self.f.f.queue, source=self.source, policy=first)
        self.c.open(self.key, actor=ACTOR, cleanup_items=[CleanupItem(kind='lease', item_hash=self.e.lease_hash,
                                                                       owner_hash='2' * 64, action='release_lease')])
        failing = body(effects.batch_for(self.e, first))
        for series in failing['series']:
            series['definition_hash'] = {m.name: m.definition_hash for m in first.metrics + first.alerts}.get(
                series['name'], series['definition_hash'])
        failing['series'][1]['samples'][0]['reading']['status_code'] = 503
        self.clock.time = first.window_end.timestamp() + 1
        self.c.verify_local(self.key, actor=ACTOR, reader=SimpleNamespace(
            mode='local_fixture', read=lambda *a, **k: EffectBatch.model_validate(failing)))
        self.scope = RollbackScope(execution_hash=self.e.digest_key(), approval_hash='1' * 64,
                                   baseline_proof_hash='2' * 64, target=self.e.target,
                                   deployment_uid=self.e.deployment_uid, application_uid=self.e.application_uid,
                                   revision=REVISION, digest=DIGEST, issued_at=self.e.applied_at - timedelta(seconds=1),
                                   expires_at=self.e.applied_at + timedelta(seconds=900), revoked=False,
                                   attempts_used=0)
        decision = self.c.follow_up(self.key, actor=ACTOR, scope_reader=lambda e: self.scope)
        self.assertEqual(decision['decision'], 'rollback_candidate')
        # Fake cluster now runs the separately applied rollback.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.api = ArgoApi(self.clock, self.e)
        clients = {}
        for name, routes in (('reader', EFFECT_READER_ROUTES), ('prom', EFFECT_PROMETHEUS_ROUTES)):
            token, ca = self.root / f'{name}-token', self.root / f'{name}-ca.crt'
            token.write_text(f'{name}-token-value')
            ca.write_text('unused')
            clients[name] = ApiClient('https://kubernetes.default.svc', token, ca, routes,
                                      now=lambda: self.clock.time, connection_factory=self.api.connect)
        self.reader = clients['reader']
        self.github = GitHub('a' * 40)  # main is ahead: rollback pinned an earlier baseline commit
        self.applied_reader = RollbackAppliedReader(reader=self.reader, argo_application=ARGO,
                                                    now=lambda: self.clock.time, github=self.github)
        self.observer = RestartEffectObserver(collector='local-restart-a7', reader=self.reader,
                                              prometheus=clients['prom'], config=CONFIG, now=lambda: self.clock.time,
                                              sleep=self.sleep, argo_application=ARGO)

    def sleep(self, seconds):
        self.clock.time += seconds

    def applied(self):
        return self.applied_reader.applied(self.e, self.scope, deadline=self.clock.time + 5)

    def observe_and_seal(self, during=None):
        restored = self.applied()
        policy = policy_for(effect_window(restored.applied_at, 120, 160))
        if during:
            self.api.hook = during(policy)
        out = self.observer.observe(restored, policy, run_id=RUN)
        self.api.hook = None
        sealed = EffectCaptureStore(self.root / 'effects').seal(out['batch'])['sealed_sha256']
        self.clock.time = policy.window_end.timestamp() + 1
        reader = RollbackObservationReader(self.applied_reader,
                                           SealedEffectReader(self.root / 'effects', sealed, now=lambda: self.clock.time))
        return restored, policy, reader, out

    def record(self, reader, policy):
        harness = SimpleNamespace(mode='local_fixture', applied=reader.applied, read=reader.read,
                                  alignment=reader.alignment)  # real adapters, test-only admission
        return self.c.record_rollback_local(self.key, actor=ACTOR, scope_reader=lambda e: self.scope,
                                            reader=harness, policy=policy)

    # --- end to end ---------------------------------------------------------------------
    def test_observed_rollback_is_verified_with_git_divergence_recorded_not_gated(self):
        restored, policy, reader, out = self.observe_and_seal()
        self.assertEqual(out['reasons'], [])
        with self.assertRaisesRegex(ValueError, 'live_rollback_reader_disabled'):
            self.c.record_rollback_local(self.key, actor=ACTOR, scope_reader=lambda e: self.scope, reader=reader,
                                         policy=policy)
        r = self.record(reader, policy)
        self.assertEqual((r['state'], r['service_resolved']), ('rollback_verified', True))
        self.assertEqual((r['first_result']['status'], r['rollback_result']['provenance']), ('unresolved', 'captured'))
        self.assertEqual(r['rollback_note'], {'gitops_main_matches': False})
        text = self.c.result_text(self.key, actor=ACTOR)
        self.assertIn('Git main 일치: 아니오', text)
        self.assertEqual({c['method'] for c in self.api.calls}, {'GET'})

    def test_main_matching_unknown_or_unavailable_github_is_informational(self):
        self.github.sha = REVISION
        self.assertEqual(self.applied_reader.alignment(self.scope, deadline=self.clock.time + 5),
                         {'gitops_main_matches': True})
        self.github.sha = None
        self.assertEqual(self.applied_reader.alignment(self.scope, deadline=self.clock.time + 5),
                         {'gitops_main_matches': None})
        restored, policy, reader, _ = self.observe_and_seal()
        r = self.record(reader, policy)
        self.assertEqual((r['state'], r['rollback_note']), ('rollback_verified', {'gitops_main_matches': None}))
        self.assertIn('Git main 일치: 미확인', self.c.result_text(self.key, actor=ACTOR))
        self.assertNotIn('token-value', str(r))
        no_github = RollbackAppliedReader(reader=self.reader, argo_application=ARGO, now=lambda: self.clock.time)
        self.assertEqual(no_github.alignment(self.scope, deadline=self.clock.time + 5), {'gitops_main_matches': None})
        self.api.app['spec']['source']['repoURL'] = 'https://github.com/someone/else.git'
        self.github.sha = REVISION
        self.assertEqual(self.applied_reader.alignment(self.scope, deadline=self.clock.time + 5),
                         {'gitops_main_matches': None})

    def test_receipt_is_stable_across_reads_and_a_later_sync_fails_closed(self):
        first, second = self.applied(), self.applied()
        self.assertEqual(first.digest_key(), second.digest_key())  # resourceVersion churn ignored
        restored, policy, reader, _ = self.observe_and_seal()
        self.api.app['status']['operationState']['finishedAt'] = ts(self.clock.time - 1)  # re-synced since
        r = self.record(reader, policy)
        self.assertEqual((r['state'], r['rollback_result']['status']), ('manual_handoff', 'verification_unknown'))

    def test_application_degraded_inside_window_is_unresolved_even_if_healthy_again(self):
        def blip(policy):
            start = policy.window_start.timestamp()
            return lambda: self.api.app['status']['health'].update(
                status='Degraded' if start + 40 < self.clock.time < start + 60 else 'Healthy')
        restored, policy, reader, _ = self.observe_and_seal(during=blip)
        r = self.record(reader, policy)
        self.assertEqual((r['state'], r['rollback_result']['status']), ('manual_handoff', 'unresolved'))

    def test_application_degraded_at_record_time_is_unknown(self):
        restored, policy, reader, _ = self.observe_and_seal()
        self.api.app['status']['health']['status'] = 'Degraded'
        r = self.record(reader, policy)
        self.assertEqual((r['state'], r['rollback_result']['status']), ('manual_handoff', 'verification_unknown'))

    def test_application_identity_change_during_window_makes_everything_unknown(self):
        def swap(policy):
            start = policy.window_start.timestamp() + 30
            return lambda: self.api.app['metadata'].update(uid='eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee') \
                if self.clock.time > start else None
        _, _, _, out = self.observe_and_seal(during=swap)
        self.assertEqual(out['reasons'], ['observation_identity_changed'])
        self.assertTrue(all(s['status'] == 'collection_failed' for s in out['batch']['series']))

    # --- applied receipt guards -----------------------------------------------------------
    def test_rollback_is_observed_only_on_exact_pinned_healthy_manual_sync(self):
        cases = {
            'other revision': lambda a, d: a['status']['sync'].update(revision='b' * 40),
            'consistently synced to another revision': lambda a, d: (
                a['spec']['source'].update(targetRevision='b' * 40), a['status']['sync'].update(revision='b' * 40),
                a['status']['operationState']['syncResult'].update(revision='b' * 40)),
            'not pinned': lambda a, d: a['spec']['source'].update(targetRevision='main'),
            'degraded': lambda a, d: a['status']['health'].update(status='Degraded'),
            'failed operation': lambda a, d: a['status']['operationState'].update(phase='Failed'),
            'automated sync': lambda a, d: a['spec'].update(syncPolicy={'automated': {}}),
            'other application': lambda a, d: a['metadata'].update(uid='eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'),
            'other deployment': lambda a, d: d['metadata'].update(uid='ffffffff-ffff-ffff-ffff-ffffffffffff'),
            'other image': lambda a, d: d['spec']['template']['spec']['containers'][0].update(
                image=IMAGE_REPOSITORY + '@sha256:' + 'b' * 64),
            'generation pending': lambda a, d: d['status'].update(observedGeneration=3),
        }
        for name, change in cases.items():
            with self.subTest(name):
                saved = deepcopy((self.api.app, self.api.deployment))
                change(self.api.app, self.api.deployment)
                with self.assertRaises(ValueError):
                    self.applied()
                self.api.app, self.api.deployment = saved
        self.api.app['status']['operationState']['finishedAt'] = ts(self.clock.time + 60)
        with self.assertRaisesRegex(ValueError, 'rollback_time_in_future'):
            self.applied()
        command = self.e.model_copy(update={'method': 'command'})
        with self.assertRaises(ValueError):
            self.applied_reader.applied(command, self.scope, deadline=self.clock.time + 5)

    def test_pr_observation_requires_configured_application(self):
        observer = RestartEffectObserver(collector='local-restart-a7', reader=self.reader, prometheus=self.reader,
                                         config=CONFIG, now=lambda: self.clock.time, sleep=self.sleep)
        restored = self.applied()
        with self.assertRaisesRegex(ValueError, 'effect_argo_application_required'):
            observer.observe(restored, policy_for(effect_window(restored.applied_at, 120, 160)), run_id=RUN)
        with self.assertRaisesRegex(ValueError, 'argo_application_rejected'):
            RollbackAppliedReader(reader=self.reader, argo_application=('argocd', 'Bad_Name'), now=lambda: 0)


if __name__ == '__main__':
    unittest.main()
