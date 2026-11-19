"""Signed Slack button -> WF06 ledger decision only. Fake clock, real WF05/06 fixtures, no network."""
import asyncio
from copy import deepcopy
import hashlib
import hmac
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from ai_ops.recovery_approvals import ApprovalPolicy, RecoveryApprovalLedger
from ai_ops.recovery_slack_approval import (SlackApprovalReceiver, action_id, button_value, parse_button)
from ai_ops.slack_ingress import SlackInteractivityApp
import test_recovery_approvals as approvals

SIGNING = b'1234567890abcdef1234567890abcdef'


class ReceiverFixture:
    """Composes (not inherits) the WF06 fixture so its own tests are not re-run here."""

    def setUp(self):
        self.a = approvals.RecoveryApprovalsTest()
        self.a.setUp()
        self.addCleanup(self.a.doCleanups)
        self.f, self.ledger, self.key, self.actor = self.a.f, self.a.ledger, self.a.key, self.a.actor
        self.record = self.a.opened()
        self.receiver = SlackApprovalReceiver(self.f.adapter.policy, SIGNING, {'prepare_proposal': self.ledger},
                                              now=lambda: self.f.fixture.time)
        self.presses = 0

    def payload(self, decision='approve', **changes):
        self.presses += 1
        value = button_value('prepare_proposal', self.key, self.record['approval_hash'], self.record['generation'])
        body = {'type': 'block_actions', 'api_app_id': 'ALOCAL01', 'team': {'id': 'TLOCAL01'},
                'user': {'id': 'UALICE01', 'team_id': 'TLOCAL01'}, 'channel': {'id': 'CLOCAL01'},
                'container': {'type': 'message', 'channel_id': 'CLOCAL01', 'message_ts': '1000.000010'},
                'is_enterprise_install': False, 'enterprise': None, 'trigger_id': 'ignored',
                'response_url': 'https://hooks.slack.invalid/ignored',
                'actions': [{'type': 'button', 'action_id': action_id('prepare_proposal', decision),
                             'value': value, 'action_ts': f'1000.{self.presses:06d}', 'block_id': 'b'}]}
        for path, item in changes.items():
            target = body
            keys = path.split('.')
            for key in keys[:-1]:
                target = target[int(key)] if isinstance(target, list) else target[key]
            if item is ...:
                del target[keys[-1]]
            elif isinstance(target, list):
                target[int(keys[-1])] = item
            else:
                target[keys[-1]] = item
        return body

    def signed(self, payload=None, *, raw=None, secret=SIGNING, stamp=None,
               content_type='application/x-www-form-urlencoded'):
        body = raw if raw is not None else urlencode({'payload': json.dumps(payload)}).encode()
        stamp = str(int(self.f.fixture.time) if stamp is None else stamp)
        signature = 'v0=' + hmac.new(secret, b'v0:' + stamp.encode() + b':' + body, hashlib.sha256).hexdigest()
        return {'Content-Type': content_type, 'X-Slack-Request-Timestamp': stamp,
                'X-Slack-Signature': signature}, body

    def press(self, payload=None, **kwargs):
        return self.receiver.receive(*self.signed(payload or self.payload(), **kwargs))

    def state(self):
        return self.ledger.show(self.key, actor=self.actor)['state']


class ApprovalDecisionTest(ReceiverFixture, unittest.TestCase):
    def test_signed_approve_records_decision_only(self):
        probes = self.a.probe_count
        with patch('socket.socket.connect', side_effect=AssertionError('network')):
            result = self.press()
        self.assertEqual(result, {'status': 'recorded', 'scope': 'prepare_proposal', 'state': 'approved',
                                  'execution_allowed': False})
        self.assertEqual(self.state(), 'approved')
        self.assertEqual(self.a.probe_count, probes)  # no revalidation/observation from the endpoint

    def test_reject_and_cancel(self):
        self.assertEqual(self.press(self.payload('reject'))['state'], 'rejected')
        self.setUp()
        self.assertEqual(self.press(self.payload('cancel'))['state'], 'cancelled')

    def test_redelivery_is_idempotent_and_second_press_cannot_renew(self):
        headers, body = self.signed(self.payload())
        first = self.receiver.receive(headers, body)
        before = self.ledger.show(self.key, actor=self.actor)
        self.assertEqual(self.receiver.receive(headers, body), first)
        self.assertEqual(self.ledger.show(self.key, actor=self.actor)['approved_until'], before['approved_until'])
        with self.assertRaisesRegex(ValueError, 'approval_subject_changed|approval_already_decided'):
            self.press()  # a new press (new action_ts) on the already approved subject

    def test_stale_subject_is_rejected_by_ledger(self):
        payload = self.payload()
        payload['actions'][0]['value'] = button_value('prepare_proposal', self.key, 'f' * 64, 0)
        with self.assertRaisesRegex(ValueError, 'approval_subject_changed'):
            self.press(payload)
        payload = self.payload()
        payload['actions'][0]['value'] = button_value('prepare_proposal', self.key, self.record['approval_hash'], 7)
        with self.assertRaisesRegex(ValueError, 'approval_subject_changed'):
            self.press(payload)
        self.assertEqual(self.state(), 'awaiting_approval')


class AuthenticationTest(ReceiverFixture, unittest.TestCase):
    def test_signature_time_and_content_type(self):
        for kwargs in ({'secret': b'f' * 32}, {'stamp': int(self.f.fixture.time) - 301},
                       {'content_type': 'application/json'}):
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
                with self.assertRaisesRegex(ValueError, 'slack_signature_rejected'):
                    self.press(**kwargs)
        self.assertEqual(self.state(), 'awaiting_approval')

    def test_form_and_json_shape(self):
        payload = json.dumps(self.payload())
        for raw in (urlencode([('payload', payload), ('payload', payload)]).encode(),
                    urlencode({'payload': payload, 'x': '1'}).encode(), urlencode({'other': payload}).encode(),
                    b'payload=%7B%22type%22%3A1%2C%22type%22%3A2%7D', 'payload=é'.encode(), b'payload=not-json'):
            with self.assertRaises(ValueError):
                self.receiver.receive(*self.signed(raw=raw))
        self.assertEqual(self.state(), 'awaiting_approval')

    def test_identity_binding(self):
        cases = {
            'wrong type': {'type': 'view_submission'}, 'wrong team': {'team.id': 'TOTHER01'},
            'wrong app': {'api_app_id': 'AOTHER01'}, 'enterprise install': {'is_enterprise_install': True},
            'enterprise org': {'enterprise': {'id': 'E1'}}, 'foreign user team': {'user.team_id': 'TOTHER01'},
            'bot user': {'user.id': 'UBOT0001'}, 'container mismatch': {'container.channel_id': 'COTHER01'},
            'not a message': {'container.type': 'view'}, 'bad ts': {'container.message_ts': 'x'},
            'not a button': {'actions.0.type': 'static_select'}, 'bad action ts': {'actions.0.action_ts': '1'},
            'unknown action': {'actions.0.action_id': 'recovery_prepare_force'},
            'scope mismatch': {'actions.0.action_id': 'recovery_execute_approve'},
            'missing user': {'user': ...},
        }
        for label, change in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                self.press(self.payload(**change))
        payload = self.payload()
        payload['actions'].append(deepcopy(payload['actions'][0]))
        with self.assertRaises(ValueError):
            self.press(payload)
        self.assertEqual(self.state(), 'awaiting_approval')

    def test_principal_and_channel_must_be_granted_and_approver(self):
        with self.assertRaisesRegex(ValueError, 'slack_principal_rejected'):
            self.press(self.payload(**{'channel.id': 'COTHER01', 'container.channel_id': 'COTHER01'}))
        with self.assertRaisesRegex(ValueError, 'slack_principal_rejected'):
            self.press(self.payload(**{'user.id': 'UMALLORY1'}))
        # Granted Slack pair but not in the ledger approver allowlist.
        policy = self.f.adapter.policy.model_dump()
        policy['grants'].append({'user_id': 'UBOB0001', 'channel_id': 'CLOCAL01', 'targets': ['url-shortener-practice']})
        receiver = SlackApprovalReceiver(policy, SIGNING, {'prepare_proposal': self.ledger}, now=lambda: self.f.fixture.time)
        with self.assertRaisesRegex(ValueError, 'approval_unavailable'):
            receiver.receive(*self.signed(self.payload(**{'user.id': 'UBOB0001'})))
        self.assertEqual(self.state(), 'awaiting_approval')

    def test_unconfigured_scope_and_constructor_checks(self):
        payload = self.payload()
        payload['actions'][0].update(action_id=action_id('execute_command', 'approve'),
                                     value=button_value('execute_command', self.key, self.record['approval_hash'], 0))
        with self.assertRaisesRegex(ValueError, 'approval_scope_unavailable'):
            self.press(payload)
        for secret, ledgers in ((b'short', {'prepare_proposal': self.ledger}), (SIGNING, {}),
                                (SIGNING, {'execute_command': self.ledger})):
            with self.assertRaises(ValueError):
                SlackApprovalReceiver(self.f.adapter.policy, secret, ledgers, now=lambda: 0)

    def test_button_value_round_trip_and_strictness(self):
        value = button_value('prepare_proposal', 'a' * 64, 'b' * 64, 3)
        self.assertEqual(parse_button('recovery_prepare_approve', value),
                         {'scope': 'prepare_proposal', 'decision': 'approve', 'plan_hash': 'a' * 64,
                          'approval_hash': 'b' * 64, 'generation': 3})
        for bad in (value + ':x', value.replace('v1', 'v2'), value.replace(':3', ':03'), value.upper()):
            with self.assertRaises(ValueError):
                parse_button('recovery_prepare_approve', bad)
        with self.assertRaises(ValueError):
            button_value('prepare_proposal', 'a' * 64, 'b' * 64, True)


class InteractivityAppTest(ReceiverFixture, unittest.TestCase):
    def invoke(self, headers, body, *, path='/slack/interactivity', scheme='https'):
        app = SlackInteractivityApp(self.receiver)
        sent = []
        events = [{'type': 'http.request', 'body': body}]

        async def receive():
            return events.pop(0)

        async def send(event):
            sent.append(event)
        scope = {'type': 'http', 'scheme': scheme, 'method': 'POST', 'path': path,
                 'headers': [(k.encode(), v.encode()) for k, v in headers.items()]
                 + [(b'content-length', str(len(body)).encode())]}
        asyncio.run(app(scope, receive, send))
        return sent[0]['status'], json.loads(sent[1]['body'])

    def test_asgi_route_ack_and_rejections(self):
        headers, body = self.signed(self.payload())
        self.assertEqual(self.invoke(headers, body, path='/slack/events'), (403, {'status': 'rejected'}))
        self.assertEqual(self.invoke(headers, body, scheme='http'), (403, {'status': 'rejected'}))
        status, result = self.invoke(headers, body)
        self.assertEqual(status, 200)
        self.assertEqual(result['state'], 'approved')
        self.assertFalse(result['execution_allowed'])
        bad_headers, bad_body = self.signed(self.payload(), secret=b'f' * 32)
        self.assertEqual(self.invoke(bad_headers, bad_body), (403, {'status': 'rejected'}))
        with self.assertRaises(ValueError):
            SlackInteractivityApp(object())


if __name__ == '__main__':
    unittest.main()
