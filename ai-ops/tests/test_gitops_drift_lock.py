"""GitOps drift lock over the real WF07 bridge and a local Git repository behind the GitHub reader."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from ai_ops.c_approval import _lab_tree, _main_sha
from ai_ops.c_recovery import ISOLATED_REPOSITORY, TARGET
from ai_ops.gitops_drift_lock import GitOpsDriftLock, assert_unlocked
from ai_ops.recovery_effects import RollbackScope
import test_recovery_pr as pr_fixture

ACTOR = pr_fixture.ACTOR
UID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'


def scope(revision, **change):
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    values = dict(execution_hash='1' * 64, approval_hash='2' * 64, baseline_proof_hash='3' * 64,
                  target=TARGET.model_dump(mode='json'), deployment_uid=UID, application_uid=pr_fixture.APP_UID,
                  revision=revision, digest='sha256:' + '4' * 64, issued_at=now, expires_at=now + timedelta(hours=1),
                  revoked=False, attempts_used=0)
    return RollbackScope.model_validate({**values, **change})


class DriftLockTest(unittest.TestCase):
    def setUp(self):
        self.f = pr_fixture.RecoveryPRTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.queue, self.reader = self.f.bridge.queue, self.f.reader
        self.baseline = _main_sha(self.reader, ISOLATED_REPOSITORY)
        self.lock = GitOpsDriftLock(self.queue)

    def engage(self, plan_hash='5' * 64, **change):
        return self.lock.engage(plan_hash=plan_hash, scope=scope(self.baseline, **change),
                                repository=ISOLATED_REPOSITORY)

    def commit_main(self, path, text, message):
        self.reader.git('checkout', 'main')
        target = self.reader.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return self.reader.commit(message)

    def locked(self):
        try:
            with self.queue.transaction() as db:
                assert_unlocked(db)
        except ValueError as error:
            return str(error) == 'gitops_drift_locked'
        return False

    # --- lock state ---------------------------------------------------------------------
    def test_engage_is_idempotent_and_a_second_rollback_conflicts(self):
        self.assertFalse(self.locked())
        first = self.engage()
        self.assertEqual((first['state'], first['revision'], first['execution_allowed']),
                         ('engaged', self.baseline, False))
        self.assertEqual(self.engage(), first)
        with self.assertRaisesRegex(ValueError, 'gitops_drift_lock_conflict'):
            self.engage(plan_hash='6' * 64)
        with self.assertRaisesRegex(ValueError, 'gitops_drift_lock_conflict'):
            self.engage(digest='sha256:' + '9' * 64)
        self.assertTrue(self.locked())
        for bad in (dict(plan_hash='x', scope=scope(self.baseline), repository=ISOLATED_REPOSITORY),
                    dict(plan_hash='5' * 64, scope=scope(self.baseline), repository='someone/else')):
            with self.assertRaisesRegex(ValueError, 'gitops_drift_lock_rejected'):
                GitOpsDriftLock(self.queue).engage(**bad)

    # --- WF07 is blocked at every forward step --------------------------------------------
    def test_lock_blocks_new_preparation_before_any_proof_read(self):
        self.f.consent()
        self.engage()
        reads = []
        original = self.f.bridge.read_proof
        self.f.bridge.read_proof = lambda *a, **k: reads.append(1) or original(*a, **k)
        with self.assertRaisesRegex(ValueError, 'gitops_drift_locked'):
            self.f.bridge.prepare(self.f.key, actor=ACTOR)
        self.assertEqual(reads, [])

    def test_lock_blocks_publication_without_a_github_write(self):
        self.f.prepare()
        self.engage()
        with self.assertRaisesRegex(ValueError, 'gitops_drift_locked'):
            self.f.bridge.publish_local(self.f.key, actor=ACTOR, writer=self.f.writer)
        self.assertEqual(self.f.writer.calls, 0)

    def test_lock_blocks_preflight_merge_and_sync_planning(self):
        self.f.check_setup()
        locked, request = self.f.locked_plan()
        self.engage()
        with self.assertRaisesRegex(ValueError, 'gitops_drift_locked'):
            self.f.preflight()
        with self.assertRaisesRegex(ValueError, 'gitops_drift_locked'):
            self.f.bridge.plan_merge(self.f.key, actor=ACTOR, locked=locked, live_reader=self.f.live,
                                     executor_identity=self.f.executor)
        with self.assertRaisesRegex(ValueError, 'gitops_drift_locked'):
            self.f.bridge.plan_sync(self.f.key, actor=ACTOR, merge_response={'merged': True, 'sha': 'a' * 40},
                                    live_reader=self.f.live)

    def test_lock_engaged_mid_step_is_caught_in_the_advancing_transaction(self):
        self.f.consent()
        original = self.f.bridge.read_proof

        def engage_during_proof(*a, **k):
            if not self.locked():
                self.engage()
            return original(*a, **k)
        self.f.bridge.read_proof = engage_during_proof
        # Start check passed; the commit transaction refuses. prepare() reports its own
        # sanitized failure (existing contract) and the record never becomes 'prepared'.
        with self.assertRaisesRegex(ValueError, 'recovery_pr_preparation_failed'):
            self.f.bridge.prepare(self.f.key, actor=ACTOR)
        _, record = self.f.bridge._record(self.f.key, ACTOR)
        self.assertEqual((record['state'], record['reason']), ('needs_review', 'preparation_failed'))
        with self.assertRaises(ValueError):
            self.f.bridge.publish_local(self.f.key, actor=ACTOR, writer=self.f.writer)
        self.assertEqual(self.f.writer.calls, 0)

    def test_lock_engaged_during_preflight_read_is_caught_before_it_returns(self):
        self.f.check_setup()
        live = self.f.live

        def read(**kwargs):
            if not self.locked():
                self.engage()
            return live.read(**kwargs)
        with self.assertRaisesRegex(ValueError, 'gitops_drift_locked'):
            self.f.bridge.preflight(self.f.key, actor=ACTOR, live_reader=SimpleNamespace(read=read),
                                    executor_identity=self.f.executor)

    def test_lock_engaged_after_the_last_start_check_is_caught_in_the_transaction(self):
        self.f.check_setup()
        approvals, calls = self.f.bridge.approvals, []
        show = approvals.show

        def show_then_engage(*a, **k):
            calls.append(1)
            value = show(*a, **k)
            if len(calls) == 2:  # second authority read in preflight, after its start check
                self.engage()
            return value
        approvals.show = show_then_engage
        with self.assertRaisesRegex(ValueError, 'gitops_drift_locked'):
            self.f.preflight()
        self.assertEqual(len(calls), 2)

    # --- release ----------------------------------------------------------------------------
    def test_release_only_when_main_lab_tree_equals_the_baseline_tree(self):
        values = self.reader.root / 'ai-ops/lab/values.yaml'
        self.reader.git('checkout', 'main')
        original = values.read_text()
        self.commit_main('ai-ops/lab/values.yaml', original + '# failed change\n', 'failed change')
        self.engage()
        check = self.lock.release_if_aligned(reader=self.reader)
        self.assertEqual((check['state'], check['release_check']), ('engaged', 'main_lab_tree_differs'))
        self.commit_main('docs/elsewhere.md', 'unrelated\n', 'change outside the lab path')
        self.assertEqual(self.lock.release_if_aligned(reader=self.reader)['state'], 'engaged')
        self.assertTrue(self.locked())
        aligned = self.commit_main('ai-ops/lab/values.yaml', original, 'separately approved alignment')
        released = self.lock.release_if_aligned(reader=self.reader)
        self.assertEqual((released['state'], released['release_check'], released['release']['main_sha']),
                         ('released', 'aligned', aligned))
        self.assertEqual(released['release']['lab_tree_sha'], _lab_tree(self.reader, self.baseline,
                                                                         ISOLATED_REPOSITORY))
        self.assertFalse(self.locked())
        again = self.engage(plan_hash='7' * 64)  # a later rollback can lock again
        self.assertEqual((again['state'], again['generation']), ('engaged', released['generation'] + 1))

    def test_fix_forward_or_unreadable_github_keeps_the_lock(self):
        self.reader.git('checkout', 'main')
        original = (self.reader.root / 'ai-ops/lab/values.yaml').read_text()
        self.commit_main('ai-ops/lab/values.yaml', original + '# failed\n', 'failed change')
        self.engage()
        self.commit_main('ai-ops/lab/values.yaml', original + '# fix forward\n', 'fix forward')
        self.assertEqual(self.lock.release_if_aligned(reader=self.reader)['release_check'], 'main_lab_tree_differs')

        class Broken:
            def get(self, endpoint):
                raise OSError('token-value leaked?')
        check = self.lock.release_if_aligned(reader=Broken())
        self.assertEqual((check['state'], check['release_check']), ('engaged', 'alignment_unverified'))
        self.assertNotIn('token-value', str(check))

        class Moving:
            def __init__(self, inner):
                self.inner, self.reads = inner, 0

            def get(self, endpoint):
                value = self.inner.get(endpoint)
                if endpoint.endswith('/git/ref/heads/main'):
                    self.reads += 1
                    if self.reads == 2:
                        value = {**value, 'object': {**value['object'], 'sha': 'c' * 40}}
                return value
        self.commit_main('ai-ops/lab/values.yaml', original, 'alignment')
        self.assertEqual(self.lock.release_if_aligned(reader=Moving(self.reader))['release_check'],
                         'alignment_unverified')
        self.assertTrue(self.locked())


if __name__ == '__main__':
    unittest.main()
