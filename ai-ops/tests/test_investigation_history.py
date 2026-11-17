"""Local verified CI context for manual/Slack investigations; no external APIs."""
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from ai_ops.ci_handoff import CIInputError, TARGET
from ai_ops.job_queue import JobQueue
from ai_ops.tasks import TaskService
import test_ci_queue as ci_fixture


class InvestigationHistoryTest(unittest.TestCase):
    def setUp(self):
        self.fx = ci_fixture.CIQueueTest()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.q = self.fx.queue
        self.settings = self.fx.settings

    def source(self):
        job, _ = self.q.submit_ci(self.settings, self.fx.history)
        result = self.fx.worker().run_once()
        self.assertEqual(result['state'], 'completed')
        return job

    def manual(self, collector=None):
        job, _ = self.q.submit(self.settings, target=TARGET, question='현재 상태와 관련 이력을 알려줘', request_id='manual-context')
        with patch('ai_ops.ci_history.GitHubReader.get', side_effect=AssertionError('network forbidden')):
            result = self.fx.worker(collector=collector).run_once()
        self.assertEqual(result['job_id'], job)
        return result

    def test_verified_context_reaches_manual_result_with_original_time_and_source(self):
        source = self.source()
        result = self.manual()
        self.assertEqual(result['state'], 'completed')
        value = TaskService(self.q, self.settings).result(result['job_id'])
        ci = next(x for x in value['evidence_summary']['coverage']['history'] if x['category'] == 'ci')
        self.assertEqual(ci['status'], 'available')
        record = self.q.engine.show(result['incident_id'], result['run_id'])
        self.assertEqual(record.report.current_state, 'unknown')
        self.assertEqual(record.collection_audit.entries[0].ci_archive_digest, self.fx.history.archive_digest[7:])
        with self.q.transaction() as db:
            manual = json.loads(db.execute('SELECT payload FROM jobs WHERE job_id=?', (result['job_id'],)).fetchone()[0])
            self.assertEqual(db.execute('SELECT attempts FROM jobs WHERE job_id=?', (source,)).fetchone()[0], 1)
        self.assertNotIn('ci_history', manual)  # original user request is retained
        self.assertEqual(manual['question'], '현재 상태와 관련 이력을 알려줘')

    def test_missing_and_pending_history_are_not_imported(self):
        self.assertIsNone(self.q.investigation_history(self.settings, TARGET))
        self.q.submit_ci(self.settings, self.fx.history)
        self.assertIsNone(self.q.investigation_history(self.settings, TARGET))

    def test_changed_configuration_or_target_is_not_reused(self):
        self.source()
        self.assertIsNone(self.q.investigation_history(self.settings, 'other-target'))
        settings = replace(self.settings, config_hash='f' * 64)
        self.assertIsNone(self.q.investigation_history(settings, TARGET))

    def test_tampered_source_hmac_rejects_context(self):
        source = self.source()
        with self.q.transaction() as db:
            p = json.loads(db.execute('SELECT payload FROM jobs WHERE job_id=?', (source,)).fetchone()[0])
            p['ci_history']['artifact_id'] = '99'
            db.execute('UPDATE jobs SET payload=? WHERE job_id=?', (json.dumps(p), source))
        with self.assertRaises(CIInputError):
            self.q.investigation_history(self.settings, TARGET)

    def test_reopened_queue_revalidates_context_without_github(self):
        self.source()
        reopened = JobQueue(self.q.root, clock=self.q.clock)
        history = reopened.investigation_history(self.settings, TARGET)
        self.assertEqual(history.archive_digest, self.fx.history.archive_digest)

    def test_current_uid_mismatch_stays_invalid(self):
        self.source()
        self.fx.app['metadata']['uid'] = 'replacement-app'
        result = self.manual()
        self.assertEqual(result['state'], 'partial')
        record = self.q.engine.show(result['incident_id'], result['run_id'])
        self.assertEqual(record.collection_audit.entries[0].reason, 'scope_mismatch')

    def test_old_history_remains_stale_in_manual_results(self):
        from datetime import timedelta
        self.source()
        now = self.fx.clock.now() + timedelta(seconds=1800)
        self.fx.clock.now = lambda: now
        result = self.manual()
        record = self.q.engine.show(result['incident_id'], result['run_id'])
        self.assertTrue(all(e.status == 'stale' for e in record.collection_audit.entries))
        self.assertEqual(record.collection_audit.entries[0].observed_raw,
                         self.fx.history.parsed()[0].observed_at.isoformat().replace('+00:00', 'Z'))

    def test_new_chart_repository_is_not_silently_accepted(self):
        self.source()
        self.settings.bindings.bindings[0].argo.repo_url = 'https://github.com/joungGo/c03-execution-lab.git'
        result = self.manual()
        self.assertEqual(result['state'], 'partial')
        record = self.q.engine.show(result['incident_id'], result['run_id'])
        self.assertEqual(record.collection_audit.entries[0].reason, 'scope_mismatch')

    def test_purging_history_is_not_reused(self):
        source = self.source()
        with self.q.transaction() as db:
            db.execute('INSERT INTO purges VALUES (?,?,?,?,?)', (source, 1000, 1000, '{}', None))
        self.assertIsNone(self.q.investigation_history(self.settings, TARGET))

    def test_result_revalidation_rejects_modified_history(self):
        source = self.source()
        result = self.manual()
        with self.q.transaction() as db:
            db.execute("UPDATE jobs SET fingerprint=? WHERE job_id=?", ('0' * 64, source))
        with self.assertRaises(ValueError):
            TaskService(self.q, self.settings).result(result['job_id'])

    def test_previous_result_keeps_exact_attempt_after_ci_rerun(self):
        from ai_ops.ci_handoff import pack_history, unpack_history
        self.source()
        result = self.manual()
        payload = pack_history(self.fx.history)
        payload['run_attempt'] = '2'
        payload['artifact_id'] = '999'
        for record in payload['records']:
            record['run_attempt'] = '2'
        history = unpack_history(payload)
        self.q.submit_ci(self.settings, history)
        self.assertEqual(self.fx.worker().run_once()['state'], 'completed')
        value = TaskService(self.q, self.settings).result(result['job_id'])
        self.assertEqual(value['state'], 'available')
        original = self.q.engine.show(result['incident_id'], result['run_id'])
        self.assertEqual(original.collection_audit.entries[0].ci_run_attempt, '1')

    def test_result_reference_survives_twenty_newer_ci_records(self):
        from ai_ops.ci_handoff import pack_history, unpack_history
        self.source()
        result = self.manual()
        for n in range(21):
            payload = pack_history(self.fx.history)
            payload['run_id'] = str(90000 + n)
            for record in payload['records']:
                record['run_id'] = payload['run_id']
            self.fx.time += 1
            self.q.submit_ci(self.settings, unpack_history(payload))
            self.assertEqual(self.fx.worker().run_once()['state'], 'completed')
        value = TaskService(self.q, self.settings).result(result['job_id'])
        self.assertEqual(value['state'], 'available')


if __name__ == '__main__':
    unittest.main()
