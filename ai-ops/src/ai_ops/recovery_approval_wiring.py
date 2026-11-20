"""A7 process wiring, slice 1: Slack approval path for *preparing* a recovery proposal.

* ``@bot request-approval <event ref|this> <plan hash>`` from the original plan
  requester, in the plan's own thread, opens the WF06 approval wait
  (``RecoveryApprovalLedger.open``). Nothing else opens one; a plan reply never
  opens an approval by itself.
* The relay periodically maps ledger states to the A7-2 outbox (approval request
  with buttons, status notices) and delivers at most one message per tick.
* The ingress serves signed button presses on ``/slack/interactivity`` (A7-1),
  which only record a ledger decision.

Disabled unless ``recovery-approval.json`` exists with ``enabled: true``. The
current-state observation is not connected yet: the default ``observe`` port
always fails, so ``open`` (which needs a fresh observation) is refused and no
approval wait can start in a real deployment until a verified reader is wired.
Nothing here revalidates, prepares a PR, executes, or reads/writes a cluster;
every result keeps ``execution_allowed`` false.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Literal

from pydantic import StrictBool, model_validator

from ai_ops.contracts import StrictModel
from ai_ops.recovery_approvals import ApprovalPolicy, RecoveryApprovalLedger, slack_plan_source
from ai_ops.recovery_outbox import RecoveryOutbox, prune_existing
from ai_ops.recovery_retention import RecoveryRetention
from ai_ops.recovery_slack_approval import SlackApprovalReceiver
from ai_ops.slack_adapter import SlackAdapter
from ai_ops.workflow_config import read_config

CONFIG_NAME = 'recovery-approval.json'
PLAN_HASH = re.compile(r'[a-f0-9]{64}\Z')
SYNC_PAGE = 50

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

    @model_validator(mode='after')
    def scope_matches(self):
        if self.prepare_proposal.scope != 'prepare_proposal':
            raise ValueError('approval_scope_mismatch')
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
    def __init__(self, adapter, config, *, observe=None):
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
                                             observe=observe or observation_not_connected)
        self.outbox = RecoveryOutbox(adapter)
        self._cursor = ''

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
        return report

    def deliver_once(self, transport):
        return self.outbox.deliver_one(transport)

    def retention(self):
        """A7-5 pass over what this process owns: the approval ledger and the outbox."""
        return RecoveryRetention(now=self.adapter.tasks.queue.now,
                                 ledgers={'prepare_proposal': self.ledger}, outbox=self.outbox)


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
