"""Read-only recovery ledger status for operators (WF11 evidence; user decision 2026-11-25).

``python -m ai_ops.recovery_status --store <store> [--plan <hash>] [--limit N]``

* Opens the existing SQLite files with ``mode=ro``: never creates a database,
  table or row (SQLite may open its own WAL ``-shm`` index), never applies time transitions (the relay does that on its next tick), and
  never takes the relay/ingress process locks, so it is safe while they run.
* Prints hashes, states, reasons, generations, deadlines (UTC) and counts only.
  No Slack user/channel IDs, message text, payloads, approver hashes, URLs or
  filesystem paths. Failures print one fixed code.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

from ai_ops.path_policy import permitted_path

HEX = re.compile(r'[a-f0-9]{64}\Z')
REASON = re.compile(r'[a-z_]{1,64}\Z')
STATE = re.compile(r'[a-z_]{1,32}\Z')
APPROVAL_TIMES = ('created_at', 'wait_until', 'approved_until', 'fresh_until', 'retained_until')
SLACK_DIRECTORIES = ('slack-web', 'slack')
DEFAULT_LIMIT, MAX_LIMIT = 50, 500


def _utc(value):
    if type(value) not in (int, float):
        return None
    return datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _word(pattern, value):
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _open(path):
    """Read-only connection to an existing database, or None. Never creates it."""
    if not path.is_file() or path.is_symlink():
        return None
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    return db


def _tables(db):
    return {r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _documents(db, table, plan):
    query = f'SELECT plan_hash, document FROM {table}'
    rows = db.execute(query + ' WHERE plan_hash=?', (plan,)) if plan else db.execute(query)
    for row in rows.fetchall():
        try:
            document = json.loads(row['document'])
        except (TypeError, ValueError):
            yield row['plan_hash'], None
            continue
        yield row['plan_hash'], document if isinstance(document, dict) else None


def _approval(plan_hash, record, now):
    if record is None:
        return {'plan_hash': plan_hash, 'state': 'unreadable'}
    view = {'plan_hash': plan_hash, 'state': _word(STATE, record.get('state')),
            'reason': _word(REASON, record.get('reason')),
            'generation': record.get('generation') if type(record.get('generation')) is int else None,
            'tombstone': record.get('identity') is None}
    view.update({k: _utc(record.get(k)) for k in APPROVAL_TIMES})
    # Deadlines already passed but not yet applied by the relay (no transition here).
    due = [k for k in ('wait_until', 'approved_until', 'fresh_until', 'retained_until')
           if type(record.get(k)) in (int, float) and now >= record[k]]
    view['deadlines_passed'] = due
    return view


def _pr(plan_hash, record):
    if record is None:
        return {'plan_hash': plan_hash, 'state': 'unreadable'}
    return {'plan_hash': plan_hash, 'state': _word(STATE, record.get('state')),
            'reason': _word(REASON, record.get('reason')),
            'generation': record.get('generation') if type(record.get('generation')) is int else None,
            'pull_number': record.get('pull_number') if type(record.get('pull_number')) is int else None,
            'payload_hash': _word(HEX, record.get('payload_hash')),
            'retained_until': _utc(record.get('retained_until'))}


def _restart(plan_hash, record):
    if record is None:
        return {'plan_hash': plan_hash, 'state': 'unreadable'}
    failed = []
    for gate in record.get('gate_results') or []:
        if isinstance(gate, dict) and gate.get('blocking') and gate.get('verdict') != 'pass':
            name = _word(REASON, gate.get('name'))
            if name:
                failed.append(name)
    return {'plan_hash': plan_hash, 'state': _word(STATE, record.get('state')),
            'reason': _word(REASON, record.get('reason')),
            'generation': record.get('generation') if type(record.get('generation')) is int else None,
            'applied': record.get('applied') is True, 'lease_released': record.get('lease_released') is True,
            'failed_gates': sorted(failed), 'retained_until': _utc(record.get('retained_until'))}


def _recent(items, limit):
    items.sort(key=lambda x: (x.get('created_at') or x.get('retained_until') or '', x['plan_hash']), reverse=True)
    return items[:limit]


def status(store, *, plan=None, limit=DEFAULT_LIMIT, now=time.time):
    """Status document for ``store`` (an already permitted path)."""
    if plan is not None and not (isinstance(plan, str) and HEX.fullmatch(plan)):
        raise ValueError('recovery_status_plan_rejected')
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError('recovery_status_limit_rejected')
    store, at = Path(store), now()
    report = {'status': 'ok', 'observed_at': _utc(at), 'read_only': True, 'execution_allowed': False,
              'policies': {}, 'approvals': {}, 'recovery_pr': [], 'restart_runs': [], 'outbox': {}}
    queue = _open(store / 'queue' / 'jobs.sqlite3')
    if queue is None:
        report['status'] = 'no_store'
        return report
    with closing(queue) as db:
        tables = _tables(db)
        for scope, prefix in (('prepare_proposal', 'recovery_approval'), ('execute_command', 'restart_approval')):
            if prefix + 's' not in tables:
                continue
            meta = db.execute(f'SELECT policy_hash FROM {prefix}_meta WHERE singleton=1').fetchone() \
                if prefix + '_meta' in tables else None
            report['policies'][scope] = _word(HEX, meta['policy_hash']) if meta else None
            report['approvals'][scope] = _recent(
                [_approval(k, r, at) for k, r in _documents(db, prefix + 's', plan)], limit)
        if 'recovery_pr_links' in tables:
            report['recovery_pr'] = _recent([_pr(k, r) for k, r in _documents(db, 'recovery_pr_links', plan)], limit)
        if 'restart_runs' in tables:
            report['restart_runs'] = _recent([_restart(k, r) for k, r in _documents(db, 'restart_runs', plan)], limit)
    for name in SLACK_DIRECTORIES:
        slack = _open(store / name / 'inbox.sqlite3')
        if slack is None:
            continue
        with closing(slack) as db:
            if 'recovery_outbox' not in _tables(db):
                continue
            counts = {}
            for row in db.execute('SELECT kind, state, COUNT(*) AS n FROM recovery_outbox GROUP BY kind, state'):
                kind, state = _word(REASON, row['kind']), _word(STATE, row['state'])
                if kind and state:
                    counts.setdefault(kind, {})[state] = row['n']
            report['outbox'][name] = counts
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='read-only recovery ledger status (hashes and states only)')
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--plan', help='one plan hash (64 hex)')
    parser.add_argument('--limit', type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args(argv)
    try:
        store = permitted_path(args.store, 'store')
        print(json.dumps(status(store, plan=args.plan, limit=args.limit), sort_keys=True))
        return 0
    except (ValueError, OSError, sqlite3.Error):
        print(json.dumps({'status': 'recovery_status_rejected'}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
