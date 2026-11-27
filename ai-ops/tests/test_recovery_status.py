"""Read-only recovery ledger status (operator evidence for WF11). Real WF05/06/08 fixtures, no network."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import sqlite3
import unittest
from unittest.mock import patch

from ai_ops.recovery_status import main, status
import test_recovery_restart_wiring as restart


def digest_files(root):
    """Logical content of every SQLite database (WAL included) plus the set of database files."""
    result = {}
    for path in sorted(root.rglob('*.sqlite3')):
        with sqlite3.connect(path) as db:
            result[str(path.relative_to(root))] = hashlib.sha256('\n'.join(db.iterdump()).encode()).hexdigest()
    return result


class StatusTest(restart.RestartWiringFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ask()
        self.flush()
        self.store = self.f.queue.root

    def report(self, **kwargs):
        with patch('socket.socket.connect', side_effect=AssertionError('network')), \
             patch('subprocess.Popen', side_effect=AssertionError('process')):
            return status(self.store, now=lambda: self.f.fixture.time, **kwargs)

    def test_reports_states_deadlines_and_outbox_counts(self):
        report = self.report()
        self.assertEqual((report['status'], report['read_only'], report['execution_allowed']), ('ok', True, False))
        [view] = report['approvals']['execute_command']
        record = self.restart_records()[0]
        self.assertEqual((view['plan_hash'], view['state'], view['reason'], view['generation'], view['tombstone']),
                         (record['plan_hash'], 'awaiting_approval', record['reason'], record['generation'], False))
        self.assertTrue(view['wait_until'].endswith('Z'))
        self.assertEqual(view['deadlines_passed'], [])
        self.assertEqual(report['approvals']['prepare_proposal'], [])  # PR ledger exists, no record
        self.assertRegex(report['policies']['execute_command'], r'^[a-f0-9]{64}$')
        [run] = report['restart_runs']
        self.assertEqual((run['state'], run['applied'], run['lease_released'], run['failed_gates']),
                         ('prepared', False, False, []))
        outbox = {kind: states for counts in report['outbox'].values() for kind, states in counts.items()}
        self.assertEqual(outbox, {'approval_request': {'delivered': 1}})

    def test_never_writes_or_creates_and_leaks_no_identifiers(self):
        self.press()
        before = digest_files(self.store)
        with sqlite3.connect(self.f.queue.path) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        text = json.dumps(self.report())
        self.assertEqual(digest_files(self.store), before)
        with sqlite3.connect(self.f.queue.path) as db:
            self.assertEqual({r[0] for r in db.execute("SELECT name FROM sqlite_master")}, tables)
        row = self.adapter._row(self.plan_request)
        record = self.restart_records()[0]
        for secret in (row['user_id'], row['channel_id'], row['thread_ts'], str(self.store),
                       record['approved_by_hash'], record['approval_hash']):
            self.assertNotIn(secret, text)

    def test_every_connection_is_read_only(self):
        real, uris = sqlite3.connect, []
        def spy(target, *args, **kwargs):
            uris.append((str(target), kwargs.get('uri')))
            return real(target, *args, **kwargs)
        with patch('ai_ops.recovery_status.sqlite3.connect', side_effect=spy):
            self.report()
        self.assertGreaterEqual(len(uris), 2)  # queue + Slack outbox databases
        self.assertTrue(all(uri and target.startswith('file:') and target.endswith('?mode=ro')
                            for target, uri in uris), uris)

    def test_passed_deadlines_are_shown_without_applying_them(self):
        self.f.fixture.time = self.restart_records()[0]['wait_until'] + 1
        with sqlite3.connect(self.f.queue.path) as db:
            stored = json.loads(db.execute('SELECT document FROM restart_approvals').fetchone()[0])
        self.assertEqual(stored['state'], 'awaiting_approval')
        [view] = self.report()['approvals']['execute_command']
        self.assertEqual((view['state'], view['deadlines_passed']), ('awaiting_approval', ['wait_until']))
        with sqlite3.connect(self.f.queue.path) as db:  # still not transitioned
            self.assertEqual(json.loads(db.execute('SELECT document FROM restart_approvals').fetchone()[0])['state'],
                             'awaiting_approval')

    def test_plan_filter_limit_and_validation(self):
        key = self.restart_records()[0]['plan_hash']
        self.assertEqual(len(self.report(plan=key)['approvals']['execute_command']), 1)
        self.assertEqual(self.report(plan='0' * 64)['approvals']['execute_command'], [])
        for bad in ({'plan': 'x' * 64}, {'plan': 'A' * 64}, {'limit': 0}, {'limit': 501}, {'limit': True}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.report(**bad)

    def test_malformed_documents_gate_names_and_tombstones(self):
        with sqlite3.connect(self.f.queue.path) as db:
            key = db.execute('SELECT plan_hash FROM restart_runs').fetchone()[0]
            document = json.loads(db.execute('SELECT document FROM restart_runs').fetchone()[0])
            document['gate_results'] = [{'name': 'no_competing_controller', 'verdict': 'fail', 'blocking': True},
                                        {'name': 'dependencies_display', 'verdict': 'unknown', 'blocking': False},
                                        {'name': 'Bad Name', 'verdict': 'fail', 'blocking': True}]
            db.execute('UPDATE restart_runs SET document=? WHERE plan_hash=?', (json.dumps(document), key))
            db.execute('CREATE TABLE IF NOT EXISTS recovery_pr_links (plan_hash TEXT PRIMARY KEY, document TEXT NOT NULL)')
            db.execute('INSERT INTO recovery_pr_links VALUES(?,?)', ('1' * 64, 'not json'))
            db.execute('INSERT INTO recovery_pr_links VALUES(?,?)', ('2' * 64, json.dumps(
                {'state': 'prepared', 'reason': 'exact_payload_ready', 'generation': 2, 'pull_number': None,
                 'payload_hash': '3' * 64, 'url': 'https://github.com/x/y/pull/1', 'retained_until': 1.0})))
            approval = json.loads(db.execute('SELECT document FROM restart_approvals').fetchone()[0])
            approval['identity'] = None
            db.execute('UPDATE restart_approvals SET document=?', (json.dumps(approval),))
        report = self.report()
        self.assertEqual(report['restart_runs'][0]['failed_gates'], ['no_competing_controller'])
        self.assertEqual({p['plan_hash']: p['state'] for p in report['recovery_pr']},
                         {'1' * 64: 'unreadable', '2' * 64: 'prepared'})
        self.assertNotIn('github.com', json.dumps(report))
        self.assertTrue(report['approvals']['execute_command'][0]['tombstone'])

    def test_cli_prints_json_and_fixed_failure_without_paths(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--store', str(self.store)]), 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'ok')
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--store', '/etc/not-permitted-store']), 1)
        self.assertEqual(output.getvalue().strip(), '{"status": "recovery_status_rejected"}')
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--store', str(self.store), '--plan', 'bad']), 1)
        self.assertNotIn(str(self.store), output.getvalue())


class EmptyStoreTest(unittest.TestCase):
    def test_missing_store_creates_nothing(self):
        import os
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'store'
            root.mkdir()
            with patch.dict(os.environ, {'AI_OPS_FILES_ROOT': tmp}):
                report = status(root)
            self.assertEqual(report['status'], 'no_store')
            self.assertEqual(list(root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
