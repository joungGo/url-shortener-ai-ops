"""Rollback execution slice 2 over real WF07/WF09 ledgers, a local Git repository and a fixture Argo port."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
import unittest

from ai_ops.c_recovery import IMAGE_REPOSITORY, ISOLATED_REPOSITORY
from ai_ops.gitops_drift_lock import GitOpsDriftLock, assert_unlocked
from ai_ops.recovery_effects import (CleanupItem, EffectBatch, EffectCoordinator, RollbackScope, body,
                                     pr_effect_source)
from ai_ops.rollback_executor import RollbackExecutor
import test_recovery_effects as effects
import test_recovery_pr as pr_fixture
from test_rollback_observe import policy_for

ACTOR = effects.ACTOR
EXECUTOR = 'local-rollback-a7'


class ArgoPort:
    mode = 'local_fixture'

    def __init__(self, t):
        self.t, self.e = t, t.e
        self.identity = {'executor_id': EXECUTOR, 'namespace': 'argocd', 'resource': 'applications/lab-target',
                         'verbs': ['get', 'patch']}
        self.app = {'metadata': {'name': 'lab-target', 'namespace': 'argocd', 'uid': t.e.application_uid},
                    'spec': {'source': {'repoURL': f'https://github.com/{ISOLATED_REPOSITORY}.git', 'path': 'ai-ops/lab',
                                        'targetRevision': t.e.revision}, 'syncPolicy': {}},
                    'status': {'operationState': {'phase': 'Succeeded'}}}
        self.deployment = {'metadata': {'uid': t.e.deployment_uid},
                           'spec': {'template': {'spec': {'containers': [
                               {'name': 'api', 'image': IMAGE_REPOSITORY + '@' + t.e.digest}]}}}}
        self.holder, self.pins, self.pin_error, self.hook, self.reads = None, [], None, None, 0

    def read(self, *, deadline):
        self.reads += 1
        if self.hook:
            self.hook(self)
        return deepcopy({'application': self.app, 'deployment': self.deployment})

    def _view(self):
        return {'name': 'b6-scenario-lock', 'namespace': 'ai-ops-lab', 'uid': 'lease-uid', 'holder': self.holder,
                'verified_until': self.t.clock.time + 60}

    def acquire(self, *, owner, deadline):
        if self.holder:
            raise ValueError('lease_held')
        self.holder = owner
        return self._view()

    def inspect_lease(self, held, *, deadline):
        return self._view()

    def release(self, held, *, deadline):
        self.holder = None
        return True

    def pin(self, request, *, deadline):
        self.pins.append(deepcopy(request))
        if self.pin_error:
            raise self.pin_error
        self.app['spec']['source']['targetRevision'] = request['to_revision']


class Clock:
    def __init__(self, fixture):
        self.fixture = fixture

    @property
    def time(self):
        return self.fixture.time

    @time.setter
    def time(self, value):
        self.fixture.time = value


class RollbackExecutorTest(unittest.TestCase):
    def setUp(self):
        self.f = pr_fixture.RecoveryPRTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        locked, request, response = self.f.merged_local()
        self.baseline = self.f.reader.git('rev-parse', response['sha'] + '^').decode().strip()
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
        _, record = self.f.bridge._record(self.key, ACTOR)
        self.good_digest = record['proposal']['current_digest']
        self.e = pr_effect_source(self.f.bridge)(self.key, ACTOR)
        first = policy_for((self.e.applied_at + timedelta(seconds=1), self.e.applied_at + timedelta(seconds=121)))
        self.c = EffectCoordinator(self.f.f.queue, source=pr_effect_source(self.f.bridge), policy=first)
        self.c.open(self.key, actor=ACTOR, cleanup_items=[CleanupItem(kind='lease', item_hash=self.e.lease_hash,
                                                                       owner_hash='2' * 64, action='release_lease')])
        failing = body(effects.batch_for(self.e, first))
        hashes = {m.name: m.definition_hash for m in first.metrics + first.alerts}
        for series in failing['series']:
            series['definition_hash'] = hashes.get(series['name'], series['definition_hash'])
        failing['series'][1]['samples'][0]['reading']['status_code'] = 503
        self.clock.time = first.window_end.timestamp() + 1
        self.c.verify_local(self.key, actor=ACTOR, reader=SimpleNamespace(
            mode='local_fixture', read=lambda *a, **k: EffectBatch.model_validate(failing)))
        self.scope = self.make_scope()
        self.candidate(self.scope)
        self.lock = GitOpsDriftLock(self.f.f.queue)
        self.executor = RollbackExecutor(self.f.f.queue, effects=self.c, drift_lock=self.lock, github=self.f.reader,
                                         repository=ISOLATED_REPOSITORY, executor_id=EXECUTOR, execution_seconds=15)
        self.port = ArgoPort(self)

    def make_scope(self, **change):
        values = dict(execution_hash=self.e.digest_key(), approval_hash='1' * 64, baseline_proof_hash='2' * 64,
                      target=body(self.e.target), deployment_uid=self.e.deployment_uid,
                      application_uid=self.e.application_uid, revision=self.baseline, digest=self.good_digest,
                      issued_at=self.e.applied_at - timedelta(seconds=1),
                      expires_at=self.e.applied_at + timedelta(seconds=900), revoked=False, attempts_used=0)
        return RollbackScope.model_validate({**values, **change})

    def candidate(self, scope):
        decision = self.c.follow_up(self.key, actor=ACTOR, scope_reader=lambda e: scope)
        self.assertEqual(decision['decision'], 'rollback_candidate')
        return decision

    def attempt(self, scope=None, port=None, scope_reader=None):
        scope = scope or self.scope
        return self.executor.execute_local(self.key, actor=ACTOR, scope_reader=scope_reader or (lambda e: scope),
                                           port=port or self.port)

    def wf07_locked(self):
        try:
            with self.f.f.queue.transaction() as db:
                assert_unlocked(db)
        except ValueError:
            return True
        return False

    def no_run(self):
        with self.f.f.queue.transaction() as db:
            return db.execute('SELECT count(*) FROM rollback_runs').fetchone()[0] == 0

    # --- the one attempt ------------------------------------------------------------------------
    def test_single_baseline_pin_after_lock_and_never_again(self):
        r = self.attempt()
        self.assertEqual((r['state'], r['pinned'], r['drift_lock_engaged'], r['lease_released'], r['execution_allowed']),
                         ('pinned_unverified', True, True, True, False))
        self.assertEqual(self.port.pins, [{'application_uid': self.e.application_uid, 'from_revision': self.e.revision,
                                           'to_revision': self.baseline}])
        self.assertEqual(self.port.app['spec']['source']['targetRevision'], self.baseline)
        self.assertEqual((self.lock.status()['state'], self.lock.status()['revision']), ('engaged', self.baseline))
        self.assertTrue(self.wf07_locked())
        self.assertIsNone(self.port.holder)
        with self.assertRaisesRegex(ValueError, 'rollback_already_attempted'):
            self.attempt()
        self.assertEqual(len(self.port.pins), 1)

    def test_lock_is_engaged_before_the_write(self):
        order = []
        engage = self.lock.engage
        self.lock.engage = lambda **k: (order.append('lock'), engage(**k))[1]
        pin = self.port.pin
        self.port.pin = lambda request, **k: (order.append('pin'), pin(request, **k))[1]
        self.attempt()
        self.assertEqual(order, ['lock', 'pin'])

    # --- refused before any reservation ----------------------------------------------------------
    def test_invalid_scope_or_candidate_reserves_nothing(self):
        for change, reason in (({'revoked': True}, 'rollback_scope_not_valid'),
                               ({'attempts_used': 1}, 'rollback_scope_not_valid'),
                               ({'execution_hash': '0' * 64}, 'rollback_scope_not_valid')):
            scope = self.make_scope(**change)
            self.candidate(self.scope)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'rollback_candidate_not_current'):
                self.attempt(scope)  # a different scope than the stored decision is not current
        self.assertTrue(self.no_run())
        self.clock.time = self.scope.expires_at.timestamp() + 1
        with self.assertRaisesRegex(ValueError, 'rollback_scope_not_valid'):
            self.attempt()
        self.assertTrue(self.no_run())
        self.assertEqual(self.port.pins, [])

    def test_live_port_or_wrong_identity_is_refused(self):
        for port in (SimpleNamespace(**{**vars(self.port), 'mode': 'kubernetes'}),
                     SimpleNamespace(**{**vars(self.port), 'identity': {**self.port.identity, 'verbs': ['*']}})):
            with self.assertRaisesRegex(ValueError, 'live_rollback_executor_disabled'):
                self.attempt(port=port)
        self.assertTrue(self.no_run())

    def test_cancelled_effect_record_is_not_a_candidate(self):
        self.c.cancel(self.key, actor=ACTOR)
        with self.assertRaisesRegex(ValueError, 'rollback_candidate_not_current'):
            self.attempt()
        self.assertTrue(self.no_run())

    # --- refused after reservation, before any write ------------------------------------------------
    def assert_no_write(self, r, *, locked):
        self.assertEqual((r['state'], r['reason'], r['pinned']), ('needs_review', 'precondition_failed_no_write', False))
        self.assertEqual(self.port.pins, [])
        self.assertEqual(self.wf07_locked(), locked)

    def test_baseline_that_would_revert_more_than_the_image_is_refused(self):
        self.f.reader.git('checkout', '--detach', self.baseline)
        chart = self.f.reader.root / 'ai-ops/lab/Chart.yaml'
        chart.write_text(chart.read_text() + '# another change\n')
        other = self.f.reader.commit('baseline with an extra lab change')
        scope = self.make_scope(revision=other)
        self.candidate(scope)
        self.assert_no_write(self.attempt(scope), locked=False)

    def test_live_state_with_overrides_auto_sync_operation_or_other_image_is_refused(self):
        cases = {
            'helm parameters': lambda p: p.app['spec']['source'].update(helm={'parameters': [{'name': 'x'}]}),
            'automated sync': lambda p: p.app['spec'].update(syncPolicy={'automated': {}}),
            'operation running': lambda p: p.app.update(operation={'sync': {}}),
            'other image': lambda p: p.deployment['spec']['template']['spec']['containers'][0].update(
                image=IMAGE_REPOSITORY + '@sha256:' + 'c' * 64),
            'not the deployed revision': lambda p: p.app['spec']['source'].update(targetRevision=self.baseline),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.setUp()
                change(self.port)
                self.assert_no_write(self.attempt(), locked=False)

    def test_lease_held_by_someone_else_is_never_taken_over(self):
        self.port.holder = 'someone-else'
        self.assert_no_write(self.attempt(), locked=False)
        self.assertEqual(self.port.holder, 'someone-else')

    def test_scope_changed_or_cancelled_after_the_lock_stops_before_the_write(self):
        calls = []

        def scope_reader(e):
            calls.append(1)
            return self.scope if len(calls) == 1 else self.make_scope(approval_hash='9' * 64)
        r = self.attempt(scope_reader=scope_reader)
        self.assert_no_write(r, locked=True)  # lock stays: fail closed, manual review
        self.assertTrue(r['drift_lock_engaged'])
        self.setUp()

        def cancel_after_lock(port):
            if port.reads == 2:
                self.c.cancel(self.key, actor=ACTOR)
        self.port.hook = cancel_after_lock
        self.assert_no_write(self.attempt(), locked=True)

    # --- the write's outcome -------------------------------------------------------------------------
    def test_pin_conflict_unknown_or_unconfirmed_is_never_retried(self):
        self.port.pin_error = ValueError('argo_pin_conflict')
        r = self.attempt()
        self.assertEqual((r['state'], r['reason']), ('not_applied', 'definite_conflict_no_retry'))
        self.setUp()
        self.port.pin_error = OSError('response lost token-value')
        r = self.attempt()
        self.assertEqual((r['state'], r['reason']), ('needs_review', 'pin_outcome_unknown_no_retry'))
        self.assertNotIn('token-value', json.dumps(r))
        self.setUp()
        self.port.pin = lambda request, **k: self.port.pins.append(request)  # accepted, not applied
        r = self.attempt()
        self.assertEqual((r['state'], r['reason']), ('needs_review', 'pin_outcome_unknown_no_retry'))
        with self.assertRaisesRegex(ValueError, 'rollback_already_attempted'):
            self.attempt()

    def test_configuration_guards(self):
        for kwargs in (dict(executor_id='bad'), dict(execution_seconds=16), dict(repository='someone/else')):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, 'rollback_executor_configuration'):
                RollbackExecutor(self.f.f.queue, effects=self.c, drift_lock=self.lock, github=self.f.reader,
                                 **{**dict(repository=ISOLATED_REPOSITORY, executor_id=EXECUTOR, execution_seconds=15),
                                    **kwargs})


if __name__ == '__main__':
    unittest.main()
