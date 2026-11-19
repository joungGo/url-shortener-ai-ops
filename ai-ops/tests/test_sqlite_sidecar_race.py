"""SQLite sidecar unlink race: deterministic checks for every private store.

SQLite removes -wal/-shm/-journal when the last connection closes. A concurrent
lstat may observe that in-flight unlink as st_nlink == 0. That must count as an
absent sidecar, while hard links, symlinks, foreign owners and group/other
permission bits stay rejected, and the main database file stays strict.
"""
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_ops.continuous_model import ContinuousModelStore
from ai_ops.job_queue import JobQueue
from ai_ops.observability import Telemetry
from ai_ops.slack_adapter import SlackAdapter

REAL_LSTAT = Path.lstat


def fake(suffix, *, nlink, mode=0o600, uid=None, kind=stat.S_IFREG):
    """Path.lstat replacement that reports one file with chosen metadata."""
    uid = os.getuid() if uid is None else uid

    def lstat(self, *args, **kwargs):
        if str(self).endswith(suffix):
            return os.stat_result((kind | mode, 1, 1, nlink, uid, os.getgid(), 0, 0, 0, 0))
        return REAL_LSTAT(self, *args, **kwargs)
    return patch.object(Path, 'lstat', lstat)


class SidecarRaceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {'AI_OPS_FILES_ROOT': str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def stores(self):
        queue = JobQueue(self.root / 'queue-store', clock=lambda: 1000.0)

        def use_queue():
            with queue.transaction() as db:
                db.execute('SELECT 1')

        slack_dir = self.root / 'slack'
        slack_dir.mkdir(mode=0o700)
        slack_path = slack_dir / 'inbox.sqlite3'
        with sqlite3.connect(slack_path) as db:
            db.execute('PRAGMA journal_mode=WAL')
        slack_path.chmod(0o600)
        slack = SimpleNamespace(directory=slack_dir, path=slack_path)

        def use_slack():
            with SlackAdapter._db(slack) as db:
                db.execute('SELECT 1')

        telemetry_root = self.root / 'telemetry-store'
        telemetry_root.mkdir(mode=0o700)
        Telemetry(telemetry_root)

        def use_telemetry():
            Telemetry(telemetry_root)

        model = ContinuousModelStore(self.root / 'model', clock=lambda: 1000.0)

        def use_model():
            with model._transaction() as db:
                db.execute('SELECT 1')
        return {'job_queue': (use_queue, '큐 파일'), 'slack_adapter': (use_slack, 'slack_storage_rejected'),
                'observability': (use_telemetry, None), 'continuous_model': (use_model, 'model_store_permissions')}

    def test_in_flight_sidecar_unlink_is_treated_as_absent(self):
        for name, (use, _) in self.stores().items():
            for suffix in ('-wal', '-shm', '-journal'):
                with self.subTest(store=name, suffix=suffix), fake(suffix, nlink=0):
                    use()

    def test_hard_link_permission_owner_and_type_still_rejected(self):
        bad = [('-wal', {'nlink': 2}), ('-shm', {'nlink': 0, 'mode': 0o644}),
               ('-wal', {'nlink': 0, 'uid': os.getuid() + 1}), ('-journal', {'nlink': 1, 'mode': 0o660}),
               ('-shm', {'nlink': 0, 'kind': stat.S_IFLNK, 'mode': 0o777})]
        for name, (use, message) in self.stores().items():
            for suffix, kwargs in bad:
                with self.subTest(store=name, suffix=suffix, **kwargs), fake(suffix, **kwargs):
                    with self.assertRaises(ValueError) as error:
                        use()
                    if message:
                        self.assertIn(message, str(error.exception))

    def test_main_database_file_stays_strict(self):
        for name, (use, _) in self.stores().items():
            for nlink in (0, 2):
                with self.subTest(store=name, nlink=nlink), fake('.sqlite3', nlink=nlink):
                    with self.assertRaises(ValueError):
                        use()

    def test_model_store_tolerates_sidecar_vanishing_between_checks(self):
        # Previously exists() then lstat(): a sidecar removed in between raised
        # FileNotFoundError out of the safety check.
        model = ContinuousModelStore(self.root / 'model', clock=lambda: 1000.0)
        calls = []

        def vanishing(self, *args, **kwargs):
            if str(self).endswith('-wal'):
                calls.append(1)
                raise FileNotFoundError(str(self))
            return REAL_LSTAT(self, *args, **kwargs)
        with patch.object(Path, 'exists', lambda self: True), patch.object(Path, 'lstat', vanishing):
            with model._transaction() as db:
                db.execute('SELECT 1')
        self.assertTrue(calls)


if __name__ == '__main__':
    unittest.main()
