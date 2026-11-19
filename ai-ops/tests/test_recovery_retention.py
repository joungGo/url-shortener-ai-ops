"""A7-5 periodic retention over real WF05/06/08 fixtures and temporary private directories."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ai_ops.job_queue import RETENTION_SECONDS
from ai_ops.recovery_retention import RecoveryRetention
import test_recovery_commands as command_fixture
from test_recovery_outbox import OutboxFixture

NOW = 1_900_000_000.0
DAY = 86400


class StubCommands:
    """restart_runs rows only: live / expired / shared evidence references."""

    def __init__(self, runs):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE restart_runs (plan_hash TEXT PRIMARY KEY, document TEXT NOT NULL)')
        for n, (state, digest) in enumerate(runs):
            doc = {'state': state, 'evidence_bundle': {'bundle_sha256': digest} if digest else None}
            self.db.execute('INSERT INTO restart_runs VALUES (?,?)', (str(n), json.dumps(doc)))
        self.pruned = 0
        self.queue = self

    @contextmanager
    def transaction(self):
        yield self.db

    def prune(self):
        self.pruned += 1


class Ledger:
    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail

    def prune(self):
        self.calls += 1
        if self.fail:
            raise OSError('/secret/path token-value')


class FileRetentionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.evidence, self.capture = self.root / 'evidence', self.root / 'effects'
        for d in (self.evidence, self.capture):
            d.mkdir(mode=0o700)
        self.now = NOW

    def stub(self, runs):
        commands = StubCommands(runs)
        self.addCleanup(commands.db.close)
        return commands

    def put(self, directory, name, age_days, data=b'{"logs":"private"}\n'):
        path = directory / name
        path.write_bytes(data)
        path.chmod(0o600)
        os.utime(path, (self.now - age_days * DAY, self.now - age_days * DAY))
        return path

    def retention(self, commands, **kw):
        return RecoveryRetention(now=lambda: self.now, commands=commands, evidence_dir=self.evidence,
                                 capture_dir=self.capture, **kw)

    def test_evidence_follows_restart_runs_and_age_only_for_unreferenced(self):
        live, gone, shared, orphan_old, orphan_new = ('a' * 64, 'b' * 64, 'c' * 64, 'd' * 64, 'f' * 64)
        commands = self.stub([('succeeded', live), ('expired', gone), ('expired', shared), ('needs_review', shared),
                                 ('expired', None)])
        files = {h: self.put(self.evidence, h + '.json', age) for h, age in
                 ((live, 30), (gone, 1), (shared, 30), (orphan_old, 8), (orphan_new, 1))}
        report = self.retention(commands).tick()
        self.assertEqual(commands.pruned, 1)  # runs expire before references are read
        self.assertEqual({h: p.exists() for h, p in files.items()},
                         {live: True, gone: False, shared: True, orphan_old: False, orphan_new: True})
        self.assertEqual((report['evidence_removed'], report['evidence_live_kept'], report['evidence_recent_kept']),
                         (2, 2, 1))
        self.assertEqual((report['failures'], report['execution_allowed']), ([], False))

    def test_only_own_hash_named_regular_files_are_touched(self):
        commands = self.stub([])
        keep = [self.put(self.evidence, 'notes.txt', 30), self.put(self.evidence, ('a' * 63) + '.json', 30)]
        (self.evidence / 'sub').mkdir(mode=0o700)
        outside = self.put(self.root, 'outside.json', 30)
        link = self.evidence / ('b' * 64 + '.json')
        link.symlink_to(outside)
        temp_old, temp_new = self.put(self.evidence, '.tmp-abc123', 8), self.put(self.evidence, '.tmp-def456', 0)
        report = self.retention(commands).tick()
        self.assertTrue(all(p.exists() for p in keep) and outside.exists() and link.is_symlink())
        self.assertEqual((temp_old.exists(), temp_new.exists()), (False, True))
        self.assertEqual(report['evidence_unrecognized_kept'], 4)

    def test_sealed_captures_expire_by_age_and_unsafe_directory_is_not_swept(self):
        old, new = self.put(self.capture, 'a' * 64 + '.json', 8), self.put(self.capture, 'b' * 64 + '.json', 6)
        report = self.retention(self.stub([])).tick()
        self.assertEqual((old.exists(), new.exists(), report['capture_removed'], report['capture_recent_kept']),
                         (False, True, 1, 1))
        self.capture.chmod(0o755)
        stale = self.put(self.capture, 'c' * 64 + '.json', 30)
        report = self.retention(self.stub([])).tick()
        self.assertTrue(stale.exists())
        self.assertEqual(report['failures'], ['capture'])
        self.capture.chmod(0o700)

    def test_missing_directories_are_not_created(self):
        r = RecoveryRetention(now=lambda: self.now, commands=self.stub([]), evidence_dir=self.root / 'none',
                              capture_dir=self.root / 'none2')
        self.assertEqual(r.tick()['failures'], [])
        self.assertFalse((self.root / 'none').exists() or (self.root / 'none2').exists())

    def test_ledger_failures_are_isolated_and_sanitized(self):
        good, bad = Ledger(), Ledger(fail=True)
        report = RecoveryRetention(now=lambda: self.now, ledgers={'approvals': good, 'effects': bad}).tick()
        self.assertEqual((good.calls, bad.calls, report['failures']), (1, 1, ['ledger_effects']))
        self.assertNotIn('secret', json.dumps(report))

    def test_configuration_guards(self):
        with self.assertRaisesRegex(ValueError, 'retention_period_too_short'):
            RecoveryRetention(now=lambda: self.now, retention_seconds=RETENTION_SECONDS - 1)
        with self.assertRaisesRegex(ValueError, 'evidence_retention_requires_restart_ledger'):
            RecoveryRetention(now=lambda: self.now, evidence_dir=self.evidence)
        commands = self.stub([])
        r = RecoveryRetention(now=lambda: self.now, ledgers={'restart': commands}, commands=commands)
        r.tick()
        self.assertEqual(commands.pruned, 1)  # passed twice, pruned once


class RestartEvidenceRetentionTest(unittest.TestCase):
    def test_real_restart_run_keeps_evidence_until_its_retention_then_removes_it(self):
        t = command_fixture.RestartCommandTest()
        t.setUp()
        self.addCleanup(t.doCleanups)
        t.consent()
        t.execute()
        digest = t.coordinator.show(t.key, actor=command_fixture.ACTOR)['evidence_bundle']['bundle_sha256']
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name) / 'evidence'
        directory.mkdir(mode=0o700)
        path = directory / f'{digest}.json'
        path.write_bytes(b'{"logs":"private"}\n')
        path.chmod(0o600)
        now = lambda: t.f.fixture.time
        os.utime(path, (now() - 30 * DAY, now() - 30 * DAY))  # age alone never removes a live run's evidence
        retention = RecoveryRetention(now=now, commands=t.coordinator, evidence_dir=directory)
        self.assertEqual(retention.tick()['evidence_live_kept'], 1)
        self.assertTrue(path.exists())
        t.f.fixture.time = t.plan.retained_until.timestamp() + 1
        report = retention.tick()
        self.assertEqual((report['evidence_removed'], path.exists()), (1, False))
        with t.coordinator.queue.transaction() as db:
            run = json.loads(db.execute('SELECT document FROM restart_runs').fetchone()['document'])
        self.assertEqual((run['state'], run['evidence_bundle']['bundle_sha256']), ('expired', digest))


class OutboxRetentionTest(OutboxFixture, unittest.TestCase):
    def test_messages_of_expired_requests_are_deleted_and_busy_is_retried_later(self):
        self.outbox.enqueue(self.request, self.view)
        retention = RecoveryRetention(now=lambda: self.f.fixture.time, outbox=self.outbox)
        self.assertEqual(retention.tick()['outbox_removed'], 0)
        with self.adapter._db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM recovery_outbox').fetchone()[0], 1)
        self.f.fixture.time += RETENTION_SECONDS + 1
        self.adapter.prune()
        with self.adapter._lock():
            self.assertEqual(retention.tick()['failures'], ['outbox'])  # Slack processing busy
        self.assertEqual(retention.tick()['outbox_removed'], 1)
        with self.adapter._db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM recovery_outbox').fetchone()[0], 0)
        self.assertEqual(self.deliver()['state'], 'idle')
        self.assertEqual(self.wire.posts(), [])


if __name__ == '__main__':
    unittest.main()
