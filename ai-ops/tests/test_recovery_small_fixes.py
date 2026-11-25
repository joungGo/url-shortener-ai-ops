"""Small A7 fixes: tombstone deletion after retention, and the observation-expired notice.

Real WF05/06/07 fixtures, fake clock, fake Slack wire. No network.
"""
import unittest

from ai_ops.recovery_approvals import TOMBSTONE_SECONDS
import test_recovery_approval_wiring as wiring
import test_recovery_approvals as approvals


class TombstoneTest(unittest.TestCase):
    def setUp(self):
        self.a = approvals.RecoveryApprovalsTest()
        self.a.setUp()
        self.addCleanup(self.a.doCleanups)

    def rows(self, table):
        with self.a.f.queue.transaction() as db:
            return db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]

    def test_tombstone_and_receipts_are_deleted_30_days_after_retention_only(self):
        record = self.a.approved()  # one ledger record, one decision receipt
        self.assertEqual((self.rows('recovery_approvals'), self.rows('recovery_approval_receipts')), (1, 1))
        end = record['retained_until']
        self.a.f.fixture.time = end
        self.a.ledger.prune()
        with self.a.f.queue.transaction() as db:
            document = db.execute('SELECT document FROM recovery_approvals').fetchone()['document']
        self.assertIn('"identity": null', document)  # tombstone: hashes and times only
        self.a.f.fixture.time = end + TOMBSTONE_SECONDS - 1
        self.a.ledger.prune()
        self.assertEqual((self.rows('recovery_approvals'), self.rows('recovery_approval_receipts')), (1, 1))
        self.a.f.fixture.time = end + TOMBSTONE_SECONDS
        self.a.ledger.prune()
        self.assertEqual((self.rows('recovery_approvals'), self.rows('recovery_approval_receipts')), (0, 0))
        # The plan itself is long expired: it can never be reopened or decided again.
        with self.assertRaisesRegex(ValueError, 'approval_unavailable'):
            self.a.ledger.open(self.a.key, actor=self.a.actor)
        with self.assertRaises(ValueError):
            self.a.decision(record)
        self.assertEqual(self.rows('recovery_approvals'), 0)

    def test_retention_tombstone_window_is_30_days(self):
        self.assertEqual(TOMBSTONE_SECONDS, 30 * 24 * 60 * 60)


class Wf07TombstoneTest(wiring.AdvanceFixture, unittest.TestCase):
    def test_prepared_wf07_record_is_deleted_30_days_after_retention(self):
        service = self.service()
        self.approved(service)
        service.advance_once()
        self.assertEqual(service.advance_once()['state'], 'prepared')
        with self.f.queue.transaction() as db:
            end = __import__('json').loads(db.execute('SELECT document FROM recovery_pr_links').fetchone()
                                           ['document'])['retained_until']
        self.f.fixture.time = end + TOMBSTONE_SECONDS - 1
        self.t.bridge.prune()
        with self.f.queue.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM recovery_pr_links').fetchone()[0], 1)
        self.f.fixture.time = end + TOMBSTONE_SECONDS
        self.t.bridge.prune()
        with self.f.queue.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM recovery_pr_links').fetchone()[0], 0)


class ObservationExpiredNoticeTest(wiring.AdvanceFixture, unittest.TestCase):
    def test_after_preparation_the_expiry_notice_says_what_happened(self):
        service = self.service()
        self.approved(service)
        service.advance_once()
        self.assertEqual(service.advance_once()['state'], 'prepared')
        self.deliver_all(service)
        view = service.ledger.page()[0][0]
        self.f.fixture.time = view['fresh_until']
        texts = self.deliver_all(service)
        [expired] = [t for t in texts if 'observation_expired' in t]
        self.assertIn('현재 상태 확인이 오래되어 이 승인으로는 더 진행하지 않습니다', expired)
        self.assertIn('새 승인 요청과 재검증이 필요합니다', expired)
        self.assertNotIn('자동으로 판단할 수 없어', expired)

    def test_other_needs_review_reasons_keep_the_generic_wording(self):
        service = self.service(observe=None)
        self.approved(self.service())
        self.assertEqual(service.advance_once()['state'], 'needs_review')
        [text] = [t for t in self.deliver_all(service) if 'read_only_revalidation_failed' in t]
        self.assertIn('자동으로 판단할 수 없어 사람의 확인이 필요합니다', text)


class PolicyTransitionTest(wiring.WiringFixture, unittest.TestCase):
    """Explicit operator transition of the approval policy (stopped processes, exact previous)."""
    def setUp(self):
        super().setUp()
        import json
        import tempfile
        from pathlib import Path
        self.json = json
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.config_dir = self.dir / 'config'
        self.config_dir.mkdir()
        self.previous = self.dir / 'previous.json'
        self.write(self.previous, self.a.policy)
        self.store = self.f.queue.root

    def write(self, path, policy):
        path.write_text(self.json.dumps(wiring.config(policy).model_dump()))

    def new_policy(self, **changes):
        return self.a.policy.model_copy(update={'version': 2, 'wait_seconds': 9000, **changes})

    def transition(self):
        from ai_ops import slack_server
        return slack_server.transition_approval_policy(self.store, self.config_dir, self.previous)

    def test_open_approvals_are_superseded_announced_and_the_new_policy_starts(self):
        from ai_ops.recovery_approval_wiring import RecoveryApprovalService
        self.ask()
        self.service.sync_once()
        self.service.deliver_once(self.transport)
        self.write(self.config_dir / 'recovery-approval.json', self.new_policy())
        with self.assertRaisesRegex(ValueError, 'explicit_transition'):
            RecoveryApprovalService(self.adapter, wiring.config(self.new_policy()))  # refused before
        result = self.transition()
        self.assertEqual((result['status'], result['superseded'], result['execution_allowed']),
                         ('transitioned', 1, False))
        self.assertNotIn(str(self.dir), self.json.dumps(result))
        service = RecoveryApprovalService(self.adapter, wiring.config(self.new_policy()), observe=self.a.observe)
        [view] = service.ledger.page()[0]
        self.assertEqual((view['state'], view['reason']), ('superseded', 'approval_policy_changed'))
        service.sync_once()
        service.deliver_once(self.transport)
        self.assertIn('승인 정책이 바뀌어 이 요청은 대체되었습니다', self.posts()[-1]['text'])

    def test_previous_must_match_and_version_must_increase(self):
        self.ask()
        self.write(self.config_dir / 'recovery-approval.json', self.new_policy())
        self.write(self.previous, self.a.policy.model_copy(update={'wait_seconds': 1234}))
        with self.assertRaisesRegex(ValueError, 'previous_policy_mismatch'):
            self.transition()
        self.write(self.previous, self.a.policy)
        self.write(self.config_dir / 'recovery-approval.json', self.new_policy(version=1))
        with self.assertRaisesRegex(ValueError, 'version_must_increase'):
            self.transition()
        self.assertEqual(self.service.ledger.page()[0][0]['state'], 'awaiting_approval')  # unchanged

    def test_refused_while_relay_or_ingress_runs(self):
        from ai_ops.slack_process import process_lock
        self.ask()
        self.write(self.config_dir / 'recovery-approval.json', self.new_policy())
        for role in ('relay', 'ingress'):
            with self.subTest(role=role), process_lock(self.store, role), \
                 self.assertRaisesRegex(ValueError, 'requires_stopped_processes'):
                self.transition()
        self.assertEqual(self.service.ledger.page()[0][0]['state'], 'awaiting_approval')

    def test_without_a_ledger_or_configs_nothing_happens(self):
        import tempfile
        from pathlib import Path
        from ai_ops.job_queue import JobQueue
        self.write(self.config_dir / 'recovery-approval.json', self.new_policy())
        store = self.store.parent / 'empty-store'  # inside the fixture's permitted files root
        JobQueue(store)
        from ai_ops import slack_server
        self.assertEqual(slack_server.transition_approval_policy(store, self.config_dir, self.previous)['status'],
                         'nothing_to_transition')
        with JobQueue(store).transaction() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='recovery_approval_meta'").fetchone())
        (self.config_dir / 'recovery-approval.json').unlink()
        with self.assertRaisesRegex(ValueError, 'config_required'):
            self.transition()

    def test_cli_action_never_echoes_paths_on_failure(self):
        import io
        import os
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from ai_ops import slack_server
        self.write(self.config_dir / 'recovery-approval.json', self.new_policy())
        self.write(self.previous, self.a.policy.model_copy(update={'wait_seconds': 1234}))
        self.ask()
        output = io.StringIO()
        with patch.dict(os.environ, {'AI_OPS_FILES_ROOT': str(self.dir.parent)}), redirect_stdout(output):
            code = slack_server.main(['transition-approval-policy', '--config', str(self.config_dir),
                                      '--store', str(self.store), '--previous', str(self.previous)])
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue().strip(), '{"status":"slack_process_rejected"}')

    def test_cli_action_transitions_with_the_exact_previous_config(self):
        import io
        import os
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from ai_ops import slack_server
        self.write(self.config_dir / 'recovery-approval.json', self.new_policy())
        self.ask()
        output = io.StringIO()
        with patch.dict(os.environ, {'AI_OPS_FILES_ROOT': str(self.dir.parent)}), redirect_stdout(output):
            code = slack_server.main(['transition-approval-policy', '--config', str(self.config_dir),
                                      '--store', str(self.store), '--previous', str(self.previous)])
        self.assertEqual((code, self.json.loads(output.getvalue())['status']), (0, 'transitioned'))
