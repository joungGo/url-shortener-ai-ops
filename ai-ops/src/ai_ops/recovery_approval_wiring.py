"""A7 process wiring, slice 1: Slack approval path for *preparing* a recovery proposal.

* ``@bot request-approval <event ref|this> <plan hash>`` from the original plan
  requester, in the plan's own thread, opens the WF06 approval wait
  (``RecoveryApprovalLedger.open``). Nothing else opens one; a plan reply never
  opens an approval by itself.
* The relay periodically maps ledger states to the A7-2 outbox (approval request
  with buttons, status notices) and delivers at most one message per tick.
* The ingress serves signed button presses on ``/slack/interactivity`` (A7-1),
  which only record a ledger decision.

* Slice 3: after approval the relay revalidates (read-only) and, with a WF07
  bridge, prepares the exact PR payload locally (never publishes).

Disabled unless ``recovery-approval.json`` exists with ``enabled: true``. The
current-state observation uses the read-only Kubernetes/Argo observer
(``recovery_observe``) only when the optional ``observe`` section is present;
otherwise the default port always fails, so ``open`` (which needs a fresh
observation) is refused and no approval wait can start. Nothing here executes,
publishes or writes a cluster; every result keeps ``execution_allowed`` false.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Literal

from pydantic import StrictBool, model_validator

from ai_ops.contracts import StrictModel
from ai_ops.recovery_approvals import ApprovalPolicy, RecoveryApprovalLedger, slack_plan_source
from ai_ops.recovery_observe import ObserveConfig, make_observer
from ai_ops.recovery_sources import PrepareConfig
from ai_ops.recovery_outbox import RecoveryOutbox, prune_existing
from ai_ops.recovery_retention import RecoveryRetention
from ai_ops.recovery_slack_approval import SlackApprovalReceiver
from ai_ops.slack_adapter import SlackAdapter
from ai_ops.workflow_config import read_config

CONFIG_NAME = 'recovery-approval.json'
PLAN_HASH = re.compile(r'[a-f0-9]{64}\Z')
SYNC_PAGE = 50
# Revalidation and WF07 preparation (<= 15s) block the relay loop; at most one per
# tick, so a tick stays below the relay progress age (slack_process.MAX_PROGRESS_AGE=30).
RELAY_BLOCK_SECONDS = 15

UNAVAILABLE = ('승인 요청을 열 수 없습니다. 이 스레드에서 본인이 요청한 지원 복구 계획인지, '
               '승인 권한과 보관 기간을 확인하세요. 승인 대기를 만들지 않았습니다.')
NOT_OBSERVED = ('현재 상태를 확인하지 못해 승인 대기를 열지 않았습니다. '
                '확인되지 않은 상태로는 승인 요청을 만들지 않습니다.')
CHANGED = ('현재 상태가 계획 당시 근거와 달라 이 계획으로는 승인 요청을 열 수 없습니다. '
           '필요하면 새 복구 계획을 요청하세요.')
FULL = '승인 장부가 가득 차 새 승인 대기를 열 수 없습니다. 관리자의 확인이 필요합니다.'
OPENED = ('승인 대기를 열었습니다(응답 마감 {until}). 승인·거절 버튼이 있는 메시지를 이 스레드에 곧 보냅니다. '
          '버튼은 계획 요청자이자 지정된 승인자만 사용할 수 있고, 승인 뒤에도 현재 상태를 다시 확인하기 전에는 '
          '아무것도 변경하지 않습니다.')
EXISTING = ('이 계획의 승인 요청은 이미 처리되었습니다(상태: {state}). 같은 계획으로 다시 열지 않으며, '
            '필요하면 새 복구 계획을 요청하세요.')
# Error codes raised after the plan/principal checks passed, i.e. safe to distinguish.
OBSERVATION_CODES = frozenset({
    'recovery_observation_not_connected', 'revalidation_observation_rejected',
    'revalidation_failure_proof_rejected', 'revalidation_health_proof_rejected', 'revalidation_timed_out'})


def parse_approval_arguments(text):
    if not isinstance(text, str) or not PLAN_HASH.fullmatch(text):
        raise ValueError('recovery_approval_arguments_rejected')
    return text


def observation_not_connected(plan, *, deadline):
    """Default port until a verified current-state reader exists: always fail closed."""
    raise ValueError('recovery_observation_not_connected')


class RecoveryApprovalConfig(StrictModel):
    schema_version: Literal[1] = 1
    enabled: StrictBool = False
    prepare_proposal: ApprovalPolicy
    # Absent -> no current-state reads at all (approval waits cannot open).
    observe: ObserveConfig | None = None
    # WF07 live sources (slice A config; read_proof/entrypoint wiring is slice B).
    prepare: PrepareConfig | None = None

    @model_validator(mode='after')
    def scope_matches(self):
        if self.prepare_proposal.scope != 'prepare_proposal':
            raise ValueError('approval_scope_mismatch')
        # One blocking read per relay tick must stay well inside the relay health age.
        if self.prepare_proposal.revalidation_seconds > RELAY_BLOCK_SECONDS:
            raise ValueError('revalidation_exceeds_relay_budget')
        if self.prepare is not None:
            # Proof objects use the observer's Kubernetes read identity.
            if self.observe is None:
                raise ValueError('prepare_requires_observe')
            if not set(self.prepare_proposal.approvers) <= set(self.prepare.principals):
                raise ValueError('prepare_principal_missing')
        return self

    @classmethod
    def load_optional(cls, path):
        """None when the file is absent (feature off). Invalid content is rejected."""
        path = Path(path)
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        try:
            return cls.model_validate(read_config(path))
        except (OSError, ValueError, TypeError, RecursionError):
            raise ValueError('recovery_approval_config_rejected') from None


def plan_request_id(adapter, plan_hash, *, actor=None):
    """The one live plan-recovery request whose frozen plan has this hash, else None."""
    if not isinstance(plan_hash, str) or not PLAN_HASH.fullmatch(plan_hash):
        return None
    with adapter._db() as db:
        rows = db.execute("SELECT r.request_id,r.user_id FROM slack_event_requests e JOIN slack_requests r "
                          "USING(request_id) WHERE e.command='plan-recovery' AND e.context IS NOT NULL "
                          "AND r.state!='expired' AND json_extract(e.context,'$.recovery_plan.plan_hash')=? "
                          'LIMIT 2', (plan_hash,)).fetchall()
    if len(rows) != 1 or (actor is not None and rows[0]['user_id'] != actor):
        return None
    return rows[0]['request_id']


def slack_plan_index(adapter):
    """Process-wide ``load_plan``: plan hash -> its request -> WF06 ``slack_plan_source``."""
    def load(plan_hash, actor):
        request_id = plan_request_id(adapter, plan_hash, actor=actor)
        if request_id is None:
            return None
        return slack_plan_source(adapter, request_id)(plan_hash, actor)
    return load


def _utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


class RecoveryApprovalService:
    """``pr_bridge`` is a factory ``ledger -> ImageRecoveryPRBridge`` with trusted
    read ports. The process entrypoint passes none (no live GitHub/CI/ECR readers
    exist yet), so WF07 preparation stays unconnected in a real deployment."""
    def __init__(self, adapter, config, *, observe=None, pr_bridge=None, connection_factory=None):
        self._connection_factory = connection_factory  # tests only: fake Kubernetes API
        if not isinstance(adapter, SlackAdapter):
            raise ValueError('recovery_approval_adapter_required')
        if not isinstance(config, RecoveryApprovalConfig) or config.enabled is not True:
            raise ValueError('recovery_approval_disabled')
        # An approver outside the Slack grants could never press a button.
        if not set(config.prepare_proposal.approvers) <= {g.user_id for g in adapter.policy.grants}:
            raise ValueError('recovery_approvers_not_granted')
        self.adapter = adapter
        self.ledger = RecoveryApprovalLedger(adapter.tasks.queue, config.prepare_proposal,
                                             load_plan=slack_plan_index(adapter),
                                             observe=observe or self._configured_observer(config))
        self.outbox = RecoveryOutbox(adapter)
        self.pr_bridge = None
        if pr_bridge is not None:
            from ai_ops.recovery_pr import ImageRecoveryPRBridge
            bridge = pr_bridge(self.ledger)
            if not isinstance(bridge, ImageRecoveryPRBridge) or bridge.approvals is not self.ledger:
                raise ValueError('recovery_pr_bridge_rejected')
            self.pr_bridge = bridge
        self._cursor = ''

    def _configured_observer(self, config):
        """Read-only observer from the optional config; never reads credentials here."""
        if config.observe is None:
            return observation_not_connected
        return make_observer(config.observe, now=self.adapter.tasks.queue.now,
                             connection_factory=self._connection_factory)

    def receiver(self, signing_secret):
        """Ingress side: signed button presses -> ledger decision record only."""
        return SlackApprovalReceiver(self.adapter.policy, signing_secret,
                                     {'prepare_proposal': self.ledger}, now=self.adapter.tasks.queue.now)

    def request(self, row, event_id, plan_hash):
        """Relay side, from the event bridge: reply text only. Messages come from ``sync_once``."""
        actor = row['user_id']
        request_id = plan_request_id(self.adapter, plan_hash, actor=actor)
        if request_id is None:
            return UNAVAILABLE
        plan_row = self.adapter._row(request_id)
        entry = self.adapter._event_bridge().entry(request_id)
        # Same signed audience: the plan's own thread/channel and event.
        if ((plan_row['channel_id'], plan_row['thread_ts']) != (row['channel_id'], row['thread_ts'])
                or entry is None or entry['event_id'] != event_id):
            return UNAVAILABLE
        try:
            view = self.ledger.open(plan_hash, actor=actor)
        except (ValueError, OSError, TimeoutError) as error:
            # Never echo exception text; only fixed codes after authorization passed.
            code = str(error) if type(error) in (ValueError, OSError, TimeoutError) else ''
            if code in OBSERVATION_CODES or isinstance(error, TimeoutError):
                return NOT_OBSERVED
            if code == 'new_recovery_plan_required':
                return CHANGED
            if code == 'approval_ledger_full':
                return FULL
            return UNAVAILABLE
        if view['state'] == 'awaiting_approval':
            return OPENED.format(until=_utc(view['wait_until']))
        return EXISTING.format(state=view['state'])

    def sync_once(self):
        """Enqueue the message for each retained record's current state (idempotent per state)."""
        views, self._cursor = self.ledger.page(after=self._cursor, limit=SYNC_PAGE)
        report = {'checked': 0, 'queued': 0, 'skipped': 0, 'execution_allowed': False}
        for view in views:
            report['checked'] += 1
            request_id = plan_request_id(self.adapter, view['plan_hash'])
            if request_id is None:
                report['skipped'] += 1
                continue
            try:
                result = self.outbox.enqueue(request_id, view)
            except (ValueError, OSError):
                report['skipped'] += 1  # e.g. request no longer authorized: never sent
                continue
            if result['state'] == 'queued':
                report['queued'] += 1
            pr = self._pr_view(view['plan_hash'], request_id)
            if pr is not None:
                try:
                    if self.outbox.enqueue_pr(request_id, pr)['state'] == 'queued':
                        report['queued'] += 1
                except (ValueError, OSError):
                    report['skipped'] += 1
        return report

    def _requester(self, plan_hash):
        request_id = plan_request_id(self.adapter, plan_hash)
        if request_id is None:
            return None
        return request_id, self.adapter._row(request_id)['user_id']

    def _pr_exists(self, plan_hash):
        with self.adapter.tasks.queue.transaction() as db:
            return db.execute('SELECT 1 FROM recovery_pr_links WHERE plan_hash=?', (plan_hash,)).fetchone() is not None

    def _pr_view(self, plan_hash, request_id):
        if self.pr_bridge is None or not self._pr_exists(plan_hash):
            return None
        try:
            return self.pr_bridge.show(plan_hash, actor=self.adapter._row(request_id)['user_id'])
        except (ValueError, OSError):
            return None

    def advance_once(self):
        """At most one read-only step per call (bounded blocking in the relay loop):

        * ``approved`` -> ``ledger.revalidate`` as the approver (the plan requester):
          the button press is consent to this read, as its notice says;
        * ``proposal_ready`` without a WF07 record -> ``pr_bridge.prepare`` (exact
          diff/PR payload, local only; publication is not wired).

        A crash mid-step never repeats it: the ledger/WF07 records time out to
        ``needs_review`` and a WF07 record, once created, is never prepared again.
        """
        views, _ = self.ledger.page(limit=SYNC_PAGE, states=('approved', 'proposal_ready'))
        for view in views:
            who = self._requester(view['plan_hash'])
            if who is None:
                continue
            _, actor = who
            if view['state'] == 'approved':
                try:
                    result = self.ledger.revalidate(view['plan_hash'], actor=actor, generation=view['generation'])
                except (ValueError, OSError):
                    return {'action': 'revalidate', 'state': 'rejected', 'execution_allowed': False}
                return {'action': 'revalidate', 'state': result['state'], 'execution_allowed': False}
            if (view['state'] == 'proposal_ready' and self.pr_bridge is not None
                    and not self._pr_exists(view['plan_hash'])):
                try:
                    result = self.pr_bridge.prepare(view['plan_hash'], actor=actor)
                except (ValueError, OSError):
                    return {'action': 'prepare', 'state': 'failed', 'execution_allowed': False}
                return {'action': 'prepare', 'state': result['state'], 'execution_allowed': False}
        return {'action': 'none', 'execution_allowed': False}

    def deliver_once(self, transport):
        return self.outbox.deliver_one(transport)

    def retention(self):
        """A7-5 pass over what this process owns: approval ledger, WF07 records, outbox."""
        ledgers = {'prepare_proposal': self.ledger}
        if self.pr_bridge is not None:
            ledgers['recovery_pr'] = self.pr_bridge  # its own prune drops copied proposals/payloads
        return RecoveryRetention(now=self.adapter.tasks.queue.now, ledgers=ledgers, outbox=self.outbox)


class OrphanOutboxRetention:
    """Relay retention while the approval feature is off: only the outbox rows an
    earlier enabled run may have left; never creates tables or reads a policy."""
    def __init__(self, adapter):
        if not isinstance(adapter, SlackAdapter):
            raise ValueError('recovery_approval_adapter_required')
        self.adapter = adapter

    def tick(self):
        report = {'failures': [], 'outbox_removed': 0, 'execution_allowed': False}
        try:
            report['outbox_removed'] = prune_existing(self.adapter)
        except Exception:
            report['failures'].append('outbox')
        return report


def relay_retention(adapter, service=None):
    return service.retention() if service is not None else OrphanOutboxRetention(adapter)
