"""Signed Slack interactivity -> existing WF06 approval ledger decision (record only).

A Slack button press is authenticated (v0 signature, 300s window, exact form
content type), bound to the configured team/app, an allowed (user, channel) pair
of the Slack policy and the exact ledger subject (plan hash, approval hash,
generation) carried in the button value. The receiver only calls
``ledger.decide``. It never revalidates, prepares, executes or posts, and every
result keeps ``execution_allowed`` false. Natural-language "approve" text is not
an approval path. Errors are generic and never echo request content.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs

from ai_ops.contracts import sha256_json
from ai_ops.evidence import _json_object
from ai_ops.slack_adapter import ID, STAMP, SlackPolicy, verify_slack_request

VERSION = 'v1'
# Approval scope -> Slack action_id prefix. Preparation and command execution
# approvals stay separate ledgers; a button can only address its own scope.
SCOPES = {'prepare_proposal': 'recovery_prepare', 'execute_command': 'recovery_execute'}
DECISIONS = ('approve', 'reject', 'cancel')
FORM = 'application/x-www-form-urlencoded'
HEX = re.compile(r'[a-f0-9]{64}\Z')


def action_id(scope, decision):
    if scope not in SCOPES or decision not in DECISIONS:
        raise ValueError('approval_action_rejected')
    return f'{SCOPES[scope]}_{decision}'


def button_value(scope, plan_hash, approval_hash, generation):
    """Exact ledger subject for one button; parsed back strictly by the receiver."""
    if (scope not in SCOPES or not HEX.fullmatch(str(plan_hash)) or not HEX.fullmatch(str(approval_hash))
            or type(generation) is not int or not 0 <= generation <= 1_000_000):
        raise ValueError('approval_button_rejected')
    return f'{VERSION}:{scope}:{plan_hash}:{approval_hash}:{generation}'


def parse_button(action, value):
    match = re.fullmatch(r'(recovery_prepare|recovery_execute)_(approve|reject|cancel)', action or '')
    parts = value.split(':') if isinstance(value, str) else []
    if (match is None or len(parts) != 5 or parts[0] != VERSION or parts[1] not in SCOPES
            or SCOPES[parts[1]] != match.group(1) or not HEX.fullmatch(parts[2])
            or not HEX.fullmatch(parts[3]) or not re.fullmatch(r'0|[1-9][0-9]{0,6}', parts[4])):
        raise ValueError('approval_button_rejected')
    return {'scope': parts[1], 'decision': match.group(2), 'plan_hash': parts[2],
            'approval_hash': parts[3], 'generation': int(parts[4])}


class SlackApprovalReceiver:
    def __init__(self, policy, secret, ledgers, *, now):
        self.policy = SlackPolicy.model_validate(policy).model_copy(deep=True)
        if not isinstance(secret, bytes) or not re.fullmatch(rb'[a-fA-F0-9]{32}', secret):
            raise ValueError('slack_signing_secret_rejected')
        if not ledgers or any(scope not in SCOPES or getattr(ledger, 'SCOPE', None) != scope
                              for scope, ledger in ledgers.items()):
            raise ValueError('approval_ledgers_rejected')
        self._secret = secret
        self._ledgers = dict(ledgers)
        self._now = now

    def _payload(self, body):
        try:
            text = body.decode('ascii')
            form = parse_qs(text, keep_blank_values=False, strict_parsing=True, max_num_fields=1)
        except (UnicodeDecodeError, ValueError):
            raise ValueError('slack_interaction_rejected') from None
        if set(form) != {'payload'} or len(form['payload']) != 1:
            raise ValueError('slack_interaction_rejected')
        value = _json_object(form['payload'][0].encode())
        if not isinstance(value, dict):
            raise ValueError('slack_interaction_rejected')
        return value

    def receive(self, headers, body):
        verify_slack_request(self._secret, headers, body, self._now(), FORM)
        value = self._payload(body)
        team, user, channel = value.get('team'), value.get('user'), value.get('channel')
        container, actions = value.get('container'), value.get('actions')
        if (value.get('type') != 'block_actions' or value.get('api_app_id') != self.policy.app_id
                or not isinstance(team, dict) or team.get('id') != self.policy.team_id
                or value.get('is_enterprise_install', False) is not False or value.get('enterprise') is not None
                or not isinstance(user, dict) or not isinstance(user.get('id'), str) or not ID.fullmatch(user['id'])
                or user.get('team_id', self.policy.team_id) != self.policy.team_id
                or user['id'] == self.policy.bot_user_id
                or not isinstance(channel, dict) or not isinstance(channel.get('id'), str)
                or not ID.fullmatch(channel['id'])
                or not isinstance(container, dict) or container.get('type') != 'message'
                or container.get('channel_id') != channel['id']
                or not isinstance(container.get('message_ts'), str) or not STAMP.fullmatch(container['message_ts'])
                or not isinstance(actions, list) or len(actions) != 1 or not isinstance(actions[0], dict)):
            raise ValueError('slack_interaction_rejected')
        action = actions[0]
        if (action.get('type') != 'button' or not isinstance(action.get('action_ts'), str)
                or not STAMP.fullmatch(action['action_ts'])):
            raise ValueError('slack_interaction_rejected')
        subject = parse_button(action.get('action_id'), action.get('value'))
        actor, where = user['id'], channel['id']
        # Only the configured requester/channel pairs may act; the ledger then
        # re-checks its own approver allowlist and the original request source.
        if not any(g.user_id == actor and g.channel_id == where for g in self.policy.grants):
            raise ValueError('slack_principal_rejected')
        ledger = self._ledgers.get(subject['scope'])
        if ledger is None:
            raise ValueError('approval_scope_unavailable')
        # Deterministic per press: a re-delivered payload is idempotent, a second
        # press is a new receipt that cannot renew an already decided subject.
        request_id = 'slack-action-' + sha256_json({
            'team': self.policy.team_id, 'app': self.policy.app_id, 'user': actor, 'channel': where,
            'message_ts': container['message_ts'], 'action_ts': action['action_ts'],
            'action_id': action['action_id']})[:64]
        record = ledger.decide(subject['plan_hash'], actor=actor, approval_hash=subject['approval_hash'],
                               generation=subject['generation'], request_id=request_id,
                               decision=subject['decision'])
        return {'status': 'recorded', 'scope': subject['scope'], 'state': record['state'],
                'execution_allowed': False}
