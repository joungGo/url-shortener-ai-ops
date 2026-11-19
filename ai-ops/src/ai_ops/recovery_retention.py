"""A7-5 periodic retention for recovery ledgers and their private files.

One bounded pass (``tick``) that a long-running process calls on an interval:

* each recovery ledger's own ``prune`` (approvals, PR links, restart runs,
  effects): existing tombstone semantics are unchanged, dedup hashes stay;
* the recovery outbox: messages whose Slack request expired are deleted (they
  can never be delivered again and must not outlive the request);
* restart evidence files (pod logs, describe, events): a file is deleted once
  every restart run that references it has expired; a file no run references
  (capture finished but the run was never recorded) is deleted after the
  retention period; a file referenced by a live run is always kept;
* sealed effect captures: deleted after the retention period.

Only regular, own, ``<sha256>.json`` or ``.tmp-*`` files directly inside an own
0700 directory are touched; anything else is counted and left alone. Failures
are recorded by name only (no paths, bodies or exception text) and retried on
the next pass. Nothing here executes, sends or calls a cluster.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

from ai_ops.job_queue import RETENTION_SECONDS

INTERVAL_SECONDS = 600
FILE = re.compile(r'([a-f0-9]{64})\.json')
TEMP = re.compile(r'\.tmp-[A-Za-z0-9_]{1,64}')


def _private_dir(path):
    """Own 0700 directory, not a symlink; None if absent."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError('retention_directory_rejected')
    return Path(path)


class RecoveryRetention:
    def __init__(self, *, now, ledgers=None, outbox=None, commands=None, evidence_dir=None, capture_dir=None,
                 retention_seconds=RETENTION_SECONDS):
        if not isinstance(retention_seconds, int) or retention_seconds < RETENTION_SECONDS:
            raise ValueError('retention_period_too_short')  # never shorter than the ledger policy
        if evidence_dir is not None and commands is None:
            raise ValueError('evidence_retention_requires_restart_ledger')
        self._now, self.ledgers, self.outbox, self.commands = now, dict(ledgers or {}), outbox, commands
        self.evidence_dir, self.capture_dir, self.retention = evidence_dir, capture_dir, retention_seconds
        if commands is not None and all(x is not commands for x in self.ledgers.values()):
            self.ledgers['restart'] = commands  # expire runs before reading their evidence references

    def _evidence_references(self):
        live, expired = set(), set()
        with self.commands.queue.transaction() as db:
            for row in db.execute('SELECT document FROM restart_runs').fetchall():
                r = json.loads(row['document'])
                digest = (r.get('evidence_bundle') or {}).get('bundle_sha256')
                if isinstance(digest, str) and FILE.fullmatch(digest + '.json'):
                    (expired if r.get('state') == 'expired' else live).add(digest)
        return live, expired - live  # a hash any live run still uses is never removed

    def _sweep(self, directory, report, prefix, live=frozenset(), expired=frozenset()):
        base = _private_dir(directory)
        if base is None:
            return
        cutoff = self._now() - self.retention
        for entry in os.scandir(base):
            name = entry.name
            match = FILE.fullmatch(name)
            try:
                info = os.lstat(base / name)
            except FileNotFoundError:
                continue
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or not (match or TEMP.fullmatch(name))):
                report[prefix + '_unrecognized_kept'] += 1
                continue
            digest = match.group(1) if match else None
            if digest in live:
                report[prefix + '_live_kept'] += 1
                continue
            if digest in expired or info.st_mtime <= cutoff:
                os.unlink(base / name)
                report[prefix + '_removed'] += 1
            else:
                report[prefix + '_recent_kept'] += 1

    def tick(self):
        """One pass. Returns counts and failure names only."""
        report = {'failures': [], 'outbox_removed': 0, 'execution_allowed': False}
        for prefix in ('evidence', 'capture'):
            for kind in ('removed', 'live_kept', 'recent_kept', 'unrecognized_kept'):
                report[f'{prefix}_{kind}'] = 0
        for name, ledger in sorted(self.ledgers.items()):
            try:
                ledger.prune()
            except Exception:
                report['failures'].append(f'ledger_{name}')
        if self.outbox is not None:
            try:
                report['outbox_removed'] = self.outbox.prune()
            except Exception:
                report['failures'].append('outbox')  # e.g. Slack processing busy: next pass
        if self.evidence_dir is not None:
            try:
                live, expired = self._evidence_references()
                self._sweep(self.evidence_dir, report, 'evidence', live, expired)
            except Exception:
                report['failures'].append('evidence')
        if self.capture_dir is not None:
            try:
                self._sweep(self.capture_dir, report, 'capture')
            except Exception:
                report['failures'].append('capture')
        return report
