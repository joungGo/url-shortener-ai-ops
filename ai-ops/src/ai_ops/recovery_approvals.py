"""WF-06 durable, local approval lifecycle for *preparing* a recovery proposal.

No HTTP/Slack approval endpoint or executor is installed. The observation port must
honor its supplied absolute deadline; late results are rejected again on return. `load_plan` and `observe`
are trusted, read-only integration ports: the former MUST authenticate/authorize the
principal and recheck source retention/current policy, the latter MUST verify captured
proofs. User text/JSON must never be passed to these ports as trusted observations.
A ready result still cannot authorize a PR merge, command, token or cluster lock.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, model_validator

from ai_ops.contracts import StrictModel, Sha256, Target, UTCDateTime, sha256_json
from ai_ops.c_recovery import ImageFailureBinding
from ai_ops.recovery_plans import RecoveryPlan


class ApprovalPolicy(StrictModel):
    version: int = Field(ge=1)
    # Explicit configuration; no new production waiting/execution defaults.
    approvers: list[str] = Field(min_length=1, max_length=32)
    wait_seconds: int = Field(ge=1, le=604800)
    approval_seconds: int = Field(ge=1, le=3600)
    observation_seconds: int = Field(ge=1, le=300)
    revalidation_seconds: int = Field(ge=1, le=60)
    scope: Literal['prepare_proposal', 'execute_command'] = 'prepare_proposal'

    @model_validator(mode='after')
    def valid(self):
        import re
        if len(set(self.approvers)) != len(self.approvers) or any(
                not re.fullmatch(r'[A-Z0-9]{2,64}', actor) for actor in self.approvers):
            raise ValueError('approval_principals_rejected')
        return self


class CurrentRecoveryState(StrictModel):
    """Verified read-only observation; a resolved alert alone is not health proof."""
    plan_hash: Sha256
    event_id: str = Field(pattern=r'^evt-[0-9a-f]{64}$')
    target: Target
    deployment_uid_hash: Sha256
    revision_hash: Sha256
    image_hash: Sha256
    proof_hash: Sha256
    observed_at: UTCDateTime
    status: Literal['image_failure', 'healthy', 'unknown']
    failure: ImageFailureBinding | None = None
    healthy_checks: list[str] = Field(default_factory=list, max_length=16)


def _body(model):
    return model.model_dump(mode='json')


class RecoveryApprovalLedger:
    """Shares the private JobQueue DB; never holds a DB transaction during a probe.

    The complete approval subject includes draft, current identity/revision/image,
    configured policy and absolute deadlines. `generation` is a CAS fence for races.
    All public results have execution_allowed=False, including proposal_ready.
    """
    TABLE_PREFIX = 'recovery_approval'
    PLAN_MODEL = RecoveryPlan
    SCOPE = 'prepare_proposal'
    FAILURE_STATUS = 'image_failure'

    def __init__(self, queue, policy, *, load_plan, observe):
        if (self.TABLE_PREFIX,self.SCOPE) not in {('recovery_approval','prepare_proposal'),('restart_approval','execute_command')}:
            raise ValueError('approval_storage_scope_rejected')
        if policy.scope != self.SCOPE:
            raise ValueError('approval_scope_mismatch')
        self.queue = queue
        self.policy = ApprovalPolicy.model_validate(_body(policy))
        self.policy_hash = sha256_json(_body(self.policy))
        self.load_plan, self.observe = load_plan, observe
        with queue.transaction() as db:
            db.execute(f'CREATE TABLE IF NOT EXISTS {self.TABLE_PREFIX}_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL, policy_hash TEXT NOT NULL)')
            meta = db.execute(f'SELECT * FROM {self.TABLE_PREFIX}_meta').fetchone()
            if meta and (meta['version'] != 1 or meta['policy_hash'] != self.policy_hash):
                raise ValueError('approval_policy_requires_explicit_transition')
            db.execute(f'INSERT OR IGNORE INTO {self.TABLE_PREFIX}_meta VALUES(1,1,?)', (self.policy_hash,))
            db.execute(f'CREATE TABLE IF NOT EXISTS {self.TABLE_PREFIX}s (plan_hash TEXT PRIMARY KEY, document TEXT NOT NULL)')
            db.execute(f'CREATE TABLE IF NOT EXISTS {self.TABLE_PREFIX}_receipts (request_hash TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, plan_hash TEXT NOT NULL)')

    def _active(self, db):
        meta = db.execute(f'SELECT policy_hash FROM {self.TABLE_PREFIX}_meta WHERE singleton=1').fetchone()
        if meta is None or meta['policy_hash'] != self.policy_hash or sha256_json(_body(self.policy)) != self.policy_hash:
            raise ValueError('approval_policy_changed')

    def _plan(self, plan_hash, actor):
        if actor not in self.policy.approvers:
            raise ValueError('approval_unavailable')
        plan = self.load_plan(plan_hash, actor)
        if plan is None:
            raise ValueError('approval_unavailable')
        plan = self.PLAN_MODEL.model_validate(_body(plan))
        if (plan.plan_hash != plan_hash or plan.assessment != 'supported'
                or plan.retained_until.timestamp() <= self.queue.now()):
            raise ValueError('approval_unavailable')
        return plan

    @staticmethod
    def _identity(observation):
        return {k: _body(observation)[k] for k in ('event_id','target','deployment_uid_hash','revision_hash','image_hash')}

    def _observation(self, plan, *, after, deadline):
        observation = CurrentRecoveryState.model_validate(_body(self.observe(plan, deadline=deadline)))
        now = self.queue.now()
        when = observation.observed_at.timestamp()
        if (observation.plan_hash != plan.plan_hash or observation.event_id != plan.event_id
                or observation.target != plan.target or when < after or when > now
                or now - when > self.policy.observation_seconds):
            raise ValueError('revalidation_observation_rejected')
        if observation.status == 'image_failure':
            failure = observation.failure
            if (failure is None or failure.deployment_uid_hash != observation.deployment_uid_hash
                    or failure.image_hash != observation.image_hash
                    or failure.observed_at.timestamp() < after
                    or failure.observed_at.timestamp() > when):
                raise ValueError('revalidation_failure_proof_rejected')
        if observation.status == 'healthy' and set(observation.healthy_checks) != set(plan.verification):
            raise ValueError('revalidation_health_proof_rejected')
        return observation

    def _save(self, db, record):
        db.execute(f'INSERT OR REPLACE INTO {self.TABLE_PREFIX}s VALUES(?,?)',
                   (record['plan_hash'], json.dumps(record, sort_keys=True)))

    def _get(self, db, plan_hash):
        self._active(db)
        row = db.execute(f'SELECT document FROM {self.TABLE_PREFIX}s WHERE plan_hash=?',(plan_hash,)).fetchone()
        if row is None:
            raise ValueError('approval_unavailable')
        record = json.loads(row['document'])
        now = self.queue.now()
        if now < record['last_seen'] and record['state'] in ('awaiting_approval','approved','revalidating','proposal_ready'):
            self._transition(record, 'needs_review', 'clock_regressed')
        record['last_seen'] = max(now, record['last_seen'])
        active = record['state'] in ('awaiting_approval','approved','revalidating','proposal_ready')
        if active and (now >= record['wait_until'] or now >= record['retained_until']
                       or record['approved_until'] is not None and now >= record['approved_until']):
            self._transition(record, 'expired', 'approval_or_plan_expired')
        elif record['state'] == 'revalidating' and now >= record['probe_until']:
            self._transition(record, 'needs_review', 'revalidation_timed_out')
        elif record['state'] == 'proposal_ready' and now >= record['fresh_until']:
            self._transition(record, 'needs_review', 'observation_expired')
        self._save(db, record)
        return record

    @staticmethod
    def _transition(record, state, reason):
        record.update(state=state, reason=reason, generation=record['generation']+1)

    @classmethod
    def _view(cls, record):
        return {**record, 'execution_allowed': False,
                'proposal_ready': record['state']=='proposal_ready',
                'approval_scope':cls.SCOPE}

    def _require_open_observation(self, plan, observation):
        original = plan.evidence.image_failure
        if observation.status != 'image_failure' or (
                observation.deployment_uid_hash != original.deployment_uid_hash
                or observation.image_hash != original.image_hash):
            raise ValueError('new_recovery_plan_required')

    def open(self, plan_hash, *, actor):
        plan = self._plan(plan_hash, actor)
        # Retry must not refresh a waiting/expired subject or its absolute deadlines.
        with self.queue.transaction() as db:
            self._active(db)
            if db.execute(f'SELECT 1 FROM {self.TABLE_PREFIX}s WHERE plan_hash=?',(plan_hash,)).fetchone():
                return self._view(self._get(db, plan_hash))
        start = self.queue.now()
        observation = self._observation(plan, after=start, deadline=start+self.policy.revalidation_seconds)
        now = self.queue.now()
        if now-start >= self.policy.revalidation_seconds:
            raise ValueError('revalidation_timed_out')
        self._plan(plan_hash, actor)  # Source permission/retention may change during the probe.
        now = self.queue.now()
        if now-start >= self.policy.revalidation_seconds or now-observation.observed_at.timestamp() >= self.policy.observation_seconds:
            raise ValueError('revalidation_timed_out')
        self._require_open_observation(plan, observation)
        subject = dict(plan_hash=plan_hash, policy_hash=self.policy_hash,
                       identity=self._identity(observation), created_at=now,
                       wait_until=min(now+self.policy.wait_seconds,plan.retained_until.timestamp()),
                       retained_until=plan.retained_until.timestamp())
        record = dict(**subject, approval_hash=sha256_json(subject), state='awaiting_approval',
                      generation=0, last_seen=now, approved_until=None, approved_by_hash=None,
                      probe_until=None, fresh_until=None, proof_hash=observation.proof_hash, reason='waiting')
        with self.queue.transaction() as db:
            self._active(db)
            if db.execute(f'SELECT 1 FROM {self.TABLE_PREFIX}s WHERE plan_hash=?',(plan_hash,)).fetchone():
                return self._view(self._get(db, plan_hash))
            # BEGIN IMMEDIATE may wait: admission must still fit the original budget.
            now = self.queue.now()
            if (now < start or now-start >= self.policy.revalidation_seconds
                    or now-observation.observed_at.timestamp() >= self.policy.observation_seconds
                    or now >= record['wait_until']):
                raise ValueError('revalidation_timed_out')
            if db.execute(f'SELECT COUNT(*) FROM {self.TABLE_PREFIX}s').fetchone()[0] >= 10000:
                raise ValueError('approval_ledger_full')
            self._save(db, record)
        return self._view(record)

    def show(self, plan_hash, *, actor):
        self._plan(plan_hash, actor)
        with self.queue.transaction() as db:
            return self._view(self._get(db, plan_hash))

    def decide(self, plan_hash, *, actor, approval_hash, generation, request_id, decision):
        self._plan(plan_hash, actor)
        if decision not in ('approve','reject','cancel') or type(generation) is not int:
            raise ValueError('approval_decision_rejected')
        # IDs are transport-generated and scoped to an authenticated principal.
        if not isinstance(request_id,str) or not 1 <= len(request_id) <= 128:
            raise ValueError('approval_request_id_rejected')
        key = sha256_json([actor, request_id])
        payload = sha256_json([plan_hash, approval_hash, generation, decision])
        with self.queue.transaction() as db:
            record = self._get(db, plan_hash)
            receipt = db.execute(f'SELECT payload_hash FROM {self.TABLE_PREFIX}_receipts WHERE request_hash=?',(key,)).fetchone()
            if receipt:
                if receipt['payload_hash'] != payload:
                    raise ValueError('approval_replay_conflict')
                return self._view(record)
            if approval_hash != record['approval_hash'] or generation != record['generation']:
                raise ValueError('approval_subject_changed')
            if record['state'] not in ('awaiting_approval','approved','revalidating','proposal_ready'):
                return self._view(record)
            if decision != 'cancel' and record['state'] != 'awaiting_approval':
                raise ValueError('approval_already_decided')
            if decision == 'approve':
                self._transition(record, 'approved', 'fresh_revalidation_required')
                record['approved_until'] = min(self.queue.now()+self.policy.approval_seconds,record['wait_until'])
                record['approved_by_hash'] = sha256_json(actor)
            else:
                self._transition(record, 'cancelled' if decision=='cancel' else 'rejected', decision)
            if db.execute(f'SELECT COUNT(*) FROM {self.TABLE_PREFIX}_receipts').fetchone()[0] >= 30000:
                raise ValueError('approval_receipts_full')
            db.execute(f'INSERT INTO {self.TABLE_PREFIX}_receipts VALUES(?,?,?)',(key,payload,plan_hash))
            self._save(db, record)
        return self._view(record)

    def revalidate(self, plan_hash, *, actor, generation):
        plan = self._plan(plan_hash, actor)
        with self.queue.transaction() as db:
            record = self._get(db, plan_hash)
            if record['state'] != 'approved' or record['generation'] != generation:
                raise ValueError('approval_not_revalidatable')
            self._transition(record, 'revalidating', 'reading_current_state')
            record['probe_until'] = min(self.queue.now()+self.policy.revalidation_seconds,
                                         record['approved_until'],record['wait_until'])
            fence, start = record['generation'], self.queue.now()
            self._save(db, record)
        try:
            observation = self._observation(plan, after=start, deadline=record['probe_until'])
            self._plan(plan_hash, actor)
        except Exception:
            # Never persist exception text: probes can contain URLs/credentials.
            observation = None
        with self.queue.transaction() as db:
            record = self._get(db, plan_hash)
            if record['generation'] != fence or record['state'] != 'revalidating':
                return self._view(record)  # Cancellation/expiry/policy fence wins.
            if observation is not None and self.queue.now()-observation.observed_at.timestamp() >= self.policy.observation_seconds:
                observation = None
            if observation is None:
                self._transition(record,'needs_review','read_only_revalidation_failed')
            elif self._identity(observation) != record['identity']:
                self._transition(record,'superseded','target_or_revision_changed')
            elif observation.status == 'healthy':
                self._transition(record,'already_recovered','current_health_verified_no_change')
            elif observation.status != self.FAILURE_STATUS:
                self._transition(record,'needs_review','cause_not_verified')
            else:
                self._transition(record,'proposal_ready','fresh_proposal_preparation_only')
                record['fresh_until'] = min(observation.observed_at.timestamp()+self.policy.observation_seconds,
                                             record['approved_until'],record['wait_until'])
            if observation:
                record['proof_hash'] = observation.proof_hash
            self._save(db, record)
            return self._view(record)

    STATES = ('awaiting_approval','approved','revalidating','proposal_ready','expired','rejected',
              'cancelled','superseded','already_recovered','needs_review')

    def page(self, *, after='', limit=50, states=None):
        """Maintenance read for the notifier: current views of still-retained records.

        Applies the same time transitions as ``show`` (expiry, timeouts) without a
        principal. It never opens, decides or revalidates. ``states`` filters on the
        stored state *before* those transitions, so callers re-check the returned
        state. Returns (views, cursor); the cursor is '' after the last page.
        """
        if not isinstance(after, str) or len(after) > 64 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('approval_page_rejected')
        if states is not None and (not isinstance(states, tuple) or not states
                                   or any(s not in self.STATES for s in states)):
            raise ValueError('approval_page_rejected')
        wanted = states or ()
        only = f"AND json_extract(document,'$.state') IN ({','.join('?' * len(wanted))}) " if wanted else ''
        with self.queue.transaction() as db:
            self._active(db)
            rows = db.execute(f'SELECT plan_hash FROM {self.TABLE_PREFIX}s WHERE plan_hash>? '
                              f"AND json_extract(document,'$.retained_until')>? {only}ORDER BY plan_hash LIMIT ?",
                              (after, self.queue.now(), *wanted, limit)).fetchall()
            views = [self._view(self._get(db, row['plan_hash'])) for row in rows]
        return views, (rows[-1]['plan_hash'] if len(rows) == limit else '')

    def replace_policy(self, policy):
        """Explicit trusted configuration transition. Old instances are fenced out."""
        policy = ApprovalPolicy.model_validate(_body(policy))
        if policy.scope != self.SCOPE:raise ValueError('approval_scope_mismatch')
        new_hash = sha256_json(_body(policy))
        if policy.version <= self.policy.version:
            raise ValueError('approval_policy_version_must_increase')
        with self.queue.transaction() as db:
            self._active(db)
            for row in db.execute(f'SELECT document FROM {self.TABLE_PREFIX}s').fetchall():
                record = json.loads(row['document'])
                if record['state'] in ('awaiting_approval','approved','revalidating','proposal_ready'):
                    self._transition(record,'superseded','approval_policy_changed')
                    self._save(db,record)
            db.execute(f'UPDATE {self.TABLE_PREFIX}_meta SET policy_hash=? WHERE singleton=1',(new_hash,))
        self.policy, self.policy_hash = policy, new_hash

    def prune(self):
        """Drop expired metadata, retain only hashed dedup tombstones; no source copy."""
        with self.queue.transaction() as db:
            self._active(db)
            for row in db.execute(f'SELECT plan_hash,document FROM {self.TABLE_PREFIX}s').fetchall():
                record = self._get(db,row['plan_hash'])
                if self.queue.now() >= record['retained_until']:
                    record.update(identity=None, approved_by_hash=None, proof_hash=None)
                    self._save(db,record)


def slack_plan_source(adapter, request_id):
    """Local integration with WF-05's stored, currently authorized request.

    This is not Slack authentication. A future verified interactivity adapter must
    derive `actor` from the authenticated request, never from message text.
    Initially only the original plan requester can use this source.
    """
    def load(plan_hash, actor):
        row = adapter._row(request_id)
        if row['user_id'] != actor or not adapter._authorized(row):
            return None
        entry = adapter._event_bridge().entry(request_id)
        if entry is None or not entry['context']:
            return None
        body = json.loads(entry['context']).get('recovery_plan')
        if body is None:
            return None
        plan = RecoveryPlan.model_validate(body)
        if plan.plan_hash != plan_hash or plan.evidence is None:
            return None
        # A retained Slack snapshot is not proof that its original result still exists.
        try:
            result = adapter.tasks.result(plan.job_id)
            expected = {'incident_id':plan.evidence.incident_id,'run_id':plan.evidence.run_id}
            if (result.get('state') != 'available' or result.get('reference') != expected
                    or result.get('bundle_hash') != plan.evidence.bundle_hash):
                return None
            record = adapter.tasks.queue.engine.show(**expected)
            if (record.bundle.bundle_hash != plan.evidence.bundle_hash
                    or record.bundle.target != plan.target
                    or sha256_json(_body(record.report)) != plan.evidence.report_hash
                    or (sha256_json(_body(record.collection_audit)) if record.collection_audit else None)
                       != plan.evidence.audit_hash):
                return None
        except (ValueError, OSError):
            return None
        return plan if adapter._authorized(row) else None
    return load
