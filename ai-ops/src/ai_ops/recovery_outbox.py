"""A7-2 recovery result outbox: approval requests and status notices to the original thread.

Messages are derived only from approval-ledger views (fixed templates, no free
text) and addressed to the thread of the original, still-authorized Slack plan
request. Each message has a deterministic key (subject, generation, state), so
re-enqueueing never duplicates it. Delivery follows the existing reply rules:
only a rate limit (429) is retried; a definite rejection fails; anything
ambiguous or interrupted becomes ``needs_review`` and is never re-sent.
Nothing here approves, executes or changes a ledger.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re

from ai_ops.contracts import sha256_json
from ai_ops.recovery_slack_approval import SCOPES, approval_blocks
from ai_ops.slack_adapter import SlackAdapter
from ai_ops.slack_web import FLAGS, SlackWebTransport

MAX_ROWS = 10000
MAX_ATTEMPTS = 3
SCOPE_LABEL = {'prepare_proposal': '복구 PR 준비', 'execute_command': '고정 복구 명령 실행'}
NOTICES = {
    'approved': '승인이 기록되었습니다. 실행 전 현재 상태를 다시 확인하며, 확인 전에는 아무 변경도 하지 않습니다.',
    'rejected': '거절이 기록되었습니다. 이 계획으로는 진행하지 않습니다.',
    'cancelled': '취소가 기록되었습니다. 이 계획으로는 진행하지 않습니다.',
    'expired': '승인 대기 또는 유효 시간이 지나 만료되었습니다. 필요하면 새 계획을 요청하세요.',
    'superseded': '승인 정책이 바뀌어 이 요청은 대체되었습니다. 필요하면 새 계획을 요청하세요.',
    'already_recovered': '재확인 결과 이미 정상으로 확인되어 변경하지 않습니다.',
    'needs_review': '자동으로 판단할 수 없어 사람의 확인이 필요합니다. 자동 재시도나 변경은 하지 않습니다.',
    'proposal_ready': '재확인을 통과해 복구 PR 준비 단계로 넘길 수 있습니다. 실제 변경은 별도 PR 리뷰·CI가 필요합니다.',
}
# WF07 preparation results (ImageRecoveryPRBridge view states). Publication is not wired.
PR_NOTICES = {
    'prepared': ('정확한 변경(diff)과 PR 내용을 준비했습니다. GitHub PR 생성·병합·배포는 하지 않았으며, '
                 '진행하려면 별도 PR 리뷰·CI와 실행 직전 검증이 필요합니다.'),
    'no_pr': '현재 근거로는 복구 PR을 만들 조건이 아니어서 준비하지 않았습니다. 변경은 없습니다.',
    'needs_review': 'PR 준비 결과를 확정할 수 없어 중지했습니다. 자동 재시도나 변경은 하지 않습니다.',
    'cancelled': 'PR 준비가 취소되었습니다. 변경은 없습니다.',
    'awaiting_review': ('C03 writer 앱으로 복구 PR을 열었습니다. 자동 병합·배포는 하지 않습니다. '
                        '정확한 head 리뷰와 필수 CI 통과 후 사람이 병합하고, Argo sync도 별도 승인이 필요합니다.'),
}
# (state, reason) wording that replaces the generic notice when it would mislead.
REASON_NOTICES = {
    ('needs_review', 'observation_expired'): (
        '재확인 이후 현재 상태 확인이 오래되어 이 승인으로는 더 진행하지 않습니다. '
        '준비된 PR 내용이 있어도 발행·변경은 하지 않으며, 진행하려면 새 승인 요청과 재검증이 필요합니다.'),
}
# Command (restart) scope: the PR wording would mislead. Execution is not wired, so
# command_ready (ledger state proposal_ready) is the end of this path.
COMMAND_NOTICES = {
    'proposal_ready': ('재확인을 통과해 재시작 명령의 실행 조건이 충족되었습니다. 실제 재시작 실행은 실환경 실행 결정 '
                       '전까지 비활성이며, 아무것도 변경하지 않았습니다.'),
    ('needs_review', 'observation_expired'): (
        '재확인 이후 현재 상태 확인이 오래되어 이 승인으로는 더 진행하지 않습니다. 재시작은 실행하지 않았으며, '
        '진행하려면 새 조사·복구 계획과 승인 요청이 필요합니다.'),
}
REASON = re.compile(r'[a-z_]{1,64}\Z')
HEX = re.compile(r'[a-f0-9]{64}\Z')


def prune_existing(adapter):
    """Outbox prune that never creates the table: also usable after the approval
    feature was switched off, so earlier messages still end with their request."""
    with adapter._lock():
        with adapter._db() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_outbox'").fetchone() is None:
                return 0
            return db.execute('DELETE FROM recovery_outbox WHERE request_id NOT IN '
                              '(SELECT request_id FROM slack_requests WHERE state!="expired")').rowcount


def _utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


class RecoveryOutbox:
    def __init__(self, adapter):
        if not isinstance(adapter, SlackAdapter):
            raise ValueError('recovery_outbox_adapter_required')
        self.adapter = adapter
        with adapter._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS recovery_outbox ('
                       'message_key TEXT PRIMARY KEY,request_id TEXT NOT NULL,kind TEXT NOT NULL,'
                       'payload TEXT NOT NULL,payload_hash TEXT NOT NULL,state TEXT NOT NULL,'
                       'attempts INTEGER NOT NULL,retry_at REAL NOT NULL,message_ts TEXT,'
                       'created_at REAL NOT NULL,updated_at REAL NOT NULL)')

    def _now(self):
        return self.adapter.tasks.queue.now()

    @staticmethod
    def _subject(view):
        scope = view.get('approval_scope')
        if (scope not in SCOPES or not HEX.fullmatch(str(view.get('plan_hash')))
                or not HEX.fullmatch(str(view.get('approval_hash'))) or type(view.get('generation')) is not int
                or view.get('execution_allowed') is not False):
            raise ValueError('recovery_outbox_view_rejected')
        return scope, view['plan_hash'], view['approval_hash'], view['generation']

    def _thread(self, request_id):
        row = self.adapter._row(request_id)
        if not self.adapter._authorized(row):
            raise ValueError('recovery_outbox_request_unauthorized')
        return row['channel_id'], row['thread_ts']

    def message_for(self, request_id, view):
        """(kind, payload) for the view's current state, or None if nothing to say."""
        scope, plan_hash, approval_hash, generation = self._subject(view)
        state = view.get('state')
        channel, thread = self._thread(request_id)
        head = f'[{SCOPE_LABEL[scope]}] 계획 {plan_hash[:12]}'
        if state == 'awaiting_approval':
            text = (f'{head} 승인 요청입니다. {_utc(view["wait_until"])}까지 응답할 수 있습니다. '
                    '승인은 이 계획 대상에만 적용되며, 승인 뒤에도 현재 상태를 다시 확인하기 전에는 변경하지 않습니다.')
            blocks = approval_blocks(text, scope, plan_hash, approval_hash, generation, ('approve', 'reject'))
            return 'approval_request', {'channel': channel, 'thread_ts': thread, 'text': text, **FLAGS, 'blocks': blocks}
        if state not in NOTICES:
            return None
        reason = view.get('reason')
        notice = REASON_NOTICES.get((state, reason), NOTICES[state])
        if scope == 'execute_command':
            notice = COMMAND_NOTICES.get((state, reason), COMMAND_NOTICES.get(state, notice))
        text = f'{head} {notice}'
        if isinstance(reason, str) and REASON.fullmatch(reason):
            text += f' (사유 코드: {reason})'
        payload = {'channel': channel, 'thread_ts': thread, 'text': text, **FLAGS}
        if state == 'approved':
            payload['blocks'] = approval_blocks(text, scope, plan_hash, approval_hash, generation, ('cancel',))
        return 'status_' + state, payload

    def enqueue(self, request_id, view):
        """Idempotent: the same subject/generation/state is stored once."""
        message = self.message_for(request_id, view)
        if message is None:
            return {'state': 'nothing_to_send', 'execution_allowed': False}
        kind, payload = message
        scope, plan_hash, approval_hash, generation = self._subject(view)
        return self._store(sha256_json([kind, scope, plan_hash, approval_hash, generation, view['state']]),
                           request_id, kind, payload)

    def enqueue_pr(self, request_id, view):
        """WF07 preparation result for the same thread: fixed text, no URL/head/diff, idempotent."""
        plan_hash, state, generation = view.get('plan_hash'), view.get('state'), view.get('generation')
        if (not HEX.fullmatch(str(plan_hash)) or type(generation) is not int
                or view.get('execution_allowed') is not False):
            raise ValueError('recovery_outbox_view_rejected')
        if state not in PR_NOTICES:
            return {'state': 'nothing_to_send', 'execution_allowed': False}
        channel, thread = self._thread(request_id)
        text = f'[{SCOPE_LABEL["prepare_proposal"]}] 계획 {plan_hash[:12]} {PR_NOTICES[state]}'
        number = view.get('pull_number')
        if state == 'awaiting_review' and type(number) is int and number > 0:
            text += f' (PR #{number})'
        reason = view.get('reason')
        if isinstance(reason, str) and REASON.fullmatch(reason):
            text += f' (사유 코드: {reason})'
        kind = 'pr_' + state
        return self._store(sha256_json([kind, plan_hash, generation, state]), request_id, kind,
                           {'channel': channel, 'thread_ts': thread, 'text': text, **FLAGS})

    def _store(self, key, request_id, kind, payload):
        digest = sha256_json(payload)
        now = self._now()
        with self.adapter._db() as db:
            prior = db.execute('SELECT payload_hash,state FROM recovery_outbox WHERE message_key=?', (key,)).fetchone()
            if prior is not None:
                if prior['payload_hash'] != digest:
                    raise ValueError('recovery_outbox_payload_changed')
                return {'state': prior['state'], 'message_key': key, 'execution_allowed': False}
            if db.execute('SELECT count(*) FROM recovery_outbox').fetchone()[0] >= MAX_ROWS:
                raise ValueError('recovery_outbox_full')
            db.execute('INSERT INTO recovery_outbox VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (key, request_id, kind, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        digest, 'queued', 0, 0, None, now, now))
        return {'state': 'queued', 'message_key': key, 'execution_allowed': False}

    def _finish(self, key, state, *, retry_at=0, message_ts=None):
        with self.adapter._db() as db:
            db.execute('UPDATE recovery_outbox SET state=?,retry_at=?,message_ts=?,updated_at=? WHERE message_key=?',
                       (state, retry_at, message_ts, self._now(), key))
        return {'state': state, 'message_key': key, 'message_ts': message_ts, 'execution_allowed': False}

    def deliver_one(self, transport):
        """Send at most one due message. Never re-sends an interrupted or ambiguous one."""
        if not isinstance(transport, SlackWebTransport) or not transport.verified_for(self.adapter.policy):
            raise ValueError('slack_web_identity_required')
        with self.adapter._lock():
            now = self._now()
            with self.adapter._db() as db:
                # A row left in 'sending' means a crash mid-send: outcome unknown.
                db.execute('UPDATE recovery_outbox SET state="needs_review",updated_at=? WHERE state="sending"', (now,))
                row = db.execute('SELECT * FROM recovery_outbox WHERE state IN ("queued","pending") AND retry_at<=? '
                                 'ORDER BY created_at,message_key LIMIT 1', (now,)).fetchone()
                if row is None:
                    return {'state': 'idle', 'execution_allowed': False}
                row = dict(row)
                payload = json.loads(row['payload'])
                if sha256_json(payload) != row['payload_hash']:
                    db.execute('UPDATE recovery_outbox SET state="needs_review",updated_at=? WHERE message_key=?',
                               (now, row['message_key']))
                    return {'state': 'needs_review', 'message_key': row['message_key'], 'execution_allowed': False}
            key, attempts = row['message_key'], row['attempts'] + 1
            try:
                channel, thread = self._thread(row['request_id'])
            except (ValueError, OSError):
                return self._finish(key, 'withdrawn')
            if (channel, thread) != (payload['channel'], payload['thread_ts']):
                return self._finish(key, 'withdrawn')
            with self.adapter._db() as db:
                db.execute('UPDATE recovery_outbox SET state="sending",attempts=?,updated_at=? WHERE message_key=?',
                           (attempts, self._now(), key))
            try:
                response = transport.post_message(dict(payload))
            except Exception:
                return self._finish(key, 'needs_review')
            status = response.get('status') if isinstance(response, dict) else None
            body = response.get('body') if isinstance(response, dict) else None
            if status == 429:
                delay = response.get('retry_after')
                if type(delay) is not int or not 1 <= delay <= 300:
                    return self._finish(key, 'needs_review')
                return self._finish(key, 'pending' if attempts < MAX_ATTEMPTS else 'failed',
                                    retry_at=self._now() + delay)
            if type(status) is int and 400 <= status < 500:
                return self._finish(key, 'failed')
            if status == 200 and isinstance(body, dict) and body.get('ok') is False:
                return self._finish(key, 'failed')
            if status == 200 and SlackAdapter._matches(body, payload):
                return self._finish(key, 'delivered', message_ts=body['ts'])
            return self._finish(key, 'needs_review')

    def prune(self):
        """Delete messages whose Slack request expired: they can never be sent again
        (delivery re-checks the request) and their payload must not outlive it."""
        return prune_existing(self.adapter)

    def status(self, key):
        with self.adapter._db() as db:
            row = db.execute('SELECT state,attempts,message_ts,kind FROM recovery_outbox WHERE message_key=?',
                             (key,)).fetchone()
        if row is None:
            raise ValueError('recovery_outbox_unknown')
        return {**dict(row), 'execution_allowed': False}
