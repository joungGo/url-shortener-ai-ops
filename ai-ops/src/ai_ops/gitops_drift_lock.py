"""GitOps drift lock: blocks WF07 while a baseline-pinned rollback leaves main ahead.

A rollback pins Argo to the pre-approved baseline commit; GitHub ``main`` still
contains the failed change. A later WF07 PR built on that ``main`` would pass the
"main head == deployed SHA" sync check and redeploy the failure. So, *before* a
rollback writes anything, it engages this lock for the target, and while the
lock is engaged every WF07 step that moves toward a PR, merge or sync is refused
inside the same database transaction that would advance it.

The lock is released only when GitHub ``main``'s ``ai-ops/lab`` tree equals the
baseline commit's ``ai-ops/lab`` tree (a separately approved alignment change
was merged). Git tree SHAs are content hashes, so equal trees mean equal files.
An unreadable or changing ``main`` keeps the lock (fail closed). There is no
expiry and no unauthenticated override: a fix-forward that leaves a different
tree needs a future, separately authorized release path.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json

from ai_ops.c_approval import _lab_tree, _main_sha
from ai_ops.c_recovery import RECOVERY_REPOSITORIES, TARGET
from ai_ops.contracts import sha256_json
from ai_ops.recovery_effects import RollbackScope

TABLE = 'gitops_drift_locks'
TARGET_HASH = sha256_json(TARGET.model_dump(mode='json'))


def assert_unlocked(db):
    """Call inside the transaction that advances WF07; raises while engaged."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is None:
        return
    row = db.execute(f'SELECT document FROM {TABLE} WHERE target_hash=?', (TARGET_HASH,)).fetchone()
    if row is not None and json.loads(row[0])['state'] == 'engaged':
        raise ValueError('gitops_drift_locked')


class GitOpsDriftLock:
    def __init__(self, queue):
        self.queue = queue
        with queue.transaction() as db:
            db.execute(f'CREATE TABLE IF NOT EXISTS {TABLE} (target_hash TEXT PRIMARY KEY, document TEXT NOT NULL)')

    def _now(self):
        return datetime.fromtimestamp(self.queue.now(), timezone.utc).isoformat()

    @staticmethod
    def _get(db):
        row = db.execute(f'SELECT document FROM {TABLE} WHERE target_hash=?', (TARGET_HASH,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _save(db, doc):
        db.execute(f'INSERT OR REPLACE INTO {TABLE} VALUES (?,?)',
                   (TARGET_HASH, json.dumps(doc, ensure_ascii=False, sort_keys=True)))

    def engage(self, *, plan_hash, scope, repository):
        """Idempotent for the same plan/scope; another engaged rollback conflicts."""
        scope = RollbackScope.model_validate(scope.model_dump(mode='json') if hasattr(scope, 'model_dump') else scope)
        if repository not in RECOVERY_REPOSITORIES or not isinstance(plan_hash, str) or len(plan_hash) != 64:
            raise ValueError('gitops_drift_lock_rejected')
        scope_hash = sha256_json(scope.model_dump(mode='json'))
        with self.queue.transaction() as db:
            current = self._get(db)
            if current is not None and current['state'] == 'engaged':
                if (current['plan_hash'], current['scope_hash'], current['repository']) == (plan_hash, scope_hash,
                                                                                           repository):
                    return {**current, 'execution_allowed': False}
                raise ValueError('gitops_drift_lock_conflict')
            doc = dict(target_hash=TARGET_HASH, state='engaged', plan_hash=plan_hash, scope_hash=scope_hash,
                       revision=scope.revision, digest=scope.digest, repository=repository,
                       engaged_at=self._now(), released_at=None, release=None,
                       generation=(current or {}).get('generation', 0) + 1)
            self._save(db, doc)
        return {**doc, 'execution_allowed': False}

    def status(self):
        with self.queue.transaction() as db:
            doc = self._get(db)
        return None if doc is None else {**doc, 'execution_allowed': False}

    def release_if_aligned(self, *, reader):
        """Release only if main's ai-ops/lab tree equals the baseline commit's tree."""
        doc = self.status()
        if doc is None or doc['state'] != 'engaged':
            return doc
        repository = doc['repository']
        try:
            main = _main_sha(reader, repository)
            main_tree = _lab_tree(reader, main, repository)
            baseline_tree = _lab_tree(reader, doc['revision'], repository)
            if _main_sha(reader, repository) != main:
                raise ValueError('main_changed_during_check')
        except Exception:  # never exception text; the lock simply stays
            return {**doc, 'release_check': 'alignment_unverified'}
        if main_tree != baseline_tree:
            return {**doc, 'release_check': 'main_lab_tree_differs'}
        with self.queue.transaction() as db:
            current = self._get(db)
            if current is None or current['state'] != 'engaged' or current['generation'] != doc['generation']:
                return None if current is None else {**current, 'execution_allowed': False}
            current.update(state='released', released_at=self._now(), generation=current['generation'] + 1,
                           release={'main_sha': main, 'lab_tree_sha': main_tree})
            self._save(db, current)
        return {**current, 'release_check': 'aligned', 'execution_allowed': False}
