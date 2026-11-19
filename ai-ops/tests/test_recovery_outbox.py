"""A7-2 outbox over real WF05/06 fixtures and a fake Slack wire. No network."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from ai_ops.contracts import sha256_json
from ai_ops.recovery_outbox import RecoveryOutbox
from ai_ops.recovery_slack_approval import approval_blocks, blocks_valid
from ai_ops.slack_sender import SenderApp
from ai_ops.slack_web import FLAGS, SlackWebTransport, post_payload_valid
from test_slack_web import Response, TOKEN, Wire
import test_recovery_approvals as approvals


class OutboxFixture:
    def setUp(self):
        self.a = approvals.RecoveryApprovalsTest()
        self.a.setUp()
        self.addCleanup(self.a.doCleanups)
        self.f, self.ledger, self.key, self.actor = self.a.f, self.a.ledger, self.a.key, self.a.actor
        self.adapter = self.f.adapter
        self.request = self.a.request
        token = self.f.queue.root / 'fake-bot-token'
        token.write_bytes(TOKEN)
        token.chmod(0o600)
        self.wire = Wire(self.adapter.policy)
        self.transport = SlackWebTransport(self.adapter.policy, token, enabled=True,
                                           connection_factory=self.wire.connect, monotonic=lambda: 10.0)
        self.transport.verify_identity()
        self.outbox = RecoveryOutbox(self.adapter)
        self.view = self.a.opened()
        self.row = self.adapter._row(self.request)

    def deliver(self):
        return self.outbox.deliver_one(self.transport)


class OutboxDeliveryTest(OutboxFixture, unittest.TestCase):
    def test_approval_request_goes_to_original_thread_with_canonical_buttons(self):
        queued = self.outbox.enqueue(self.request, self.view)
        self.assertEqual(queued['state'], 'queued')
        result = self.deliver()
        self.assertEqual(result['state'], 'delivered')
        self.assertFalse(result['execution_allowed'])
        post = self.wire.posts()[0]['body']
        self.assertEqual((post['channel'], post['thread_ts']), (self.row['channel_id'], self.row['thread_ts']))
        self.assertTrue(blocks_valid(post['blocks'], post['text']))
        self.assertEqual([e['action_id'] for e in post['blocks'][1]['elements']],
                         ['recovery_prepare_approve', 'recovery_prepare_reject'])
        self.assertNotIn('private-secret-value', json.dumps(post, ensure_ascii=False))

    def test_enqueue_is_idempotent_and_delivered_message_is_not_resent(self):
        first = self.outbox.enqueue(self.request, self.view)
        self.assertEqual(self.outbox.enqueue(self.request, self.view)['message_key'], first['message_key'])
        self.deliver()
        self.assertEqual(self.outbox.enqueue(self.request, self.view)['state'], 'delivered')
        self.assertEqual(self.deliver()['state'], 'idle')
        self.assertEqual(len(self.wire.posts()), 1)

    def test_status_notices_follow_ledger_state(self):
        approved = self.a.decision(self.view)
        self.outbox.enqueue(self.request, approved)
        self.deliver()
        post = self.wire.posts()[-1]['body']
        self.assertEqual([e['action_id'] for e in post['blocks'][1]['elements']], ['recovery_prepare_cancel'])
        self.assertIn('fresh_revalidation_required', post['text'])
        cancelled = self.ledger.decide(self.key, actor=self.actor, approval_hash=approved['approval_hash'],
                                       generation=approved['generation'], request_id='c1', decision='cancel')
        self.outbox.enqueue(self.request, cancelled)
        self.deliver()
        post = self.wire.posts()[-1]['body']
        self.assertNotIn('blocks', post)
        self.assertIn('취소가 기록되었습니다', post['text'])
        revalidating = {**cancelled, 'state': 'revalidating'}
        self.assertEqual(self.outbox.enqueue(self.request, revalidating)['state'], 'nothing_to_send')

    def test_rate_limit_retries_then_fails_after_three(self):
        for _ in range(3):
            self.wire.outcomes.append(Response(status=429, headers={'Retry-After': '5'}, raw=b''))
        key = self.outbox.enqueue(self.request, self.view)['message_key']
        self.assertEqual(self.deliver()['state'], 'pending')
        self.assertEqual(self.deliver()['state'], 'idle')  # retry_at not reached
        self.f.fixture.time += 5
        self.assertEqual(self.deliver()['state'], 'pending')
        self.f.fixture.time += 5
        self.assertEqual(self.deliver()['state'], 'failed')
        self.assertEqual(self.outbox.status(key)['attempts'], 3)

    def test_ambiguous_and_interrupted_sends_are_never_resent(self):
        self.wire.outcomes.append(OSError('reset'))
        key = self.outbox.enqueue(self.request, self.view)['message_key']
        self.assertEqual(self.deliver()['state'], 'needs_review')
        self.assertEqual(self.deliver()['state'], 'idle')
        self.assertEqual(len(self.wire.posts()), 1)
        approved = self.a.decision(self.view)
        key = self.outbox.enqueue(self.request, approved)['message_key']
        with self.adapter._db() as db:  # simulate a crash after marking 'sending'
            db.execute('UPDATE recovery_outbox SET state="sending" WHERE message_key=?', (key,))
        self.assertEqual(self.deliver()['state'], 'idle')
        self.assertEqual(self.outbox.status(key)['state'], 'needs_review')
        self.assertEqual(len(self.wire.posts()), 1)

    def test_definite_rejection_fails_and_withdrawn_request_is_not_sent(self):
        self.wire.outcomes.append(Response(body={'ok': False, 'error': 'not_in_channel'}))
        self.outbox.enqueue(self.request, self.view)
        self.assertEqual(self.deliver()['state'], 'failed')
        approved = self.a.decision(self.view)
        self.outbox.enqueue(self.request, approved)
        posts = len(self.wire.posts())
        with patch.object(self.adapter, '_authorized', return_value=False):
            self.assertEqual(self.deliver()['state'], 'withdrawn')
        self.assertEqual(len(self.wire.posts()), posts)

    def test_rejects_untrusted_views_and_unverified_transport(self):
        for change in ({'execution_allowed': True}, {'approval_scope': 'other'}, {'plan_hash': 'x'},
                       {'generation': '0'}):
            with self.assertRaises(ValueError):
                self.outbox.enqueue(self.request, {**self.view, **change})
        unverified = SlackWebTransport(self.adapter.policy, self.f.queue.root / 'fake-bot-token', enabled=True,
                                       connection_factory=self.wire.connect)
        with self.assertRaisesRegex(ValueError, 'identity_required'):
            self.outbox.deliver_one(unverified)


class BlockContractTest(OutboxFixture, unittest.TestCase):
    def payload(self):
        text = 'fixed text'
        blocks = approval_blocks(text, 'prepare_proposal', 'a' * 64, 'b' * 64, 0, ('approve', 'reject'))
        return {'channel': 'CLOCAL01', 'thread_ts': '1000.000001', 'text': text, **FLAGS, 'blocks': blocks}

    def test_only_canonical_blocks_pass_transport_and_sender(self):
        policy = self.adapter.policy
        self.assertTrue(post_payload_valid(policy, self.payload()))
        tampered = []
        for mutate in (lambda p: p['blocks'][1]['elements'][0]['text'].update(text='무조건 승인'),
                       lambda p: p['blocks'][1]['elements'].append(deepcopy(p['blocks'][1]['elements'][0])),
                       lambda p: p['blocks'][1]['elements'][1].update(value=p['blocks'][1]['elements'][1]['value'].replace('b' * 64, 'c' * 64)),
                       lambda p: p.update(text='different'),
                       lambda p: p['blocks'].append({'type': 'divider'}),
                       lambda p: p['blocks'][0]['text'].update(type='mrkdwn'),
                       lambda p: p['blocks'][1]['elements'][0].update(url='https://example.invalid'),
                       lambda p: p.update(channel='COTHER01')):
            payload = self.payload()
            mutate(payload)
            tampered.append(payload)
        for payload in tampered:
            self.assertFalse(post_payload_valid(policy, payload))
        app = SenderApp(policy, self.transport, b'abcdef0123456789abcdef0123456789')
        auth = 'Bearer abcdef0123456789abcdef0123456789'
        good = self.payload()
        status, result = app.handle('/v1/chat.postMessage', auth, sha256_json(good),
                                    json.dumps({'payload': good}).encode())
        self.assertEqual((status, result['status']), (200, 200))
        bad = tampered[0]
        self.assertEqual(app.handle('/v1/chat.postMessage', auth, sha256_json(bad),
                                    json.dumps({'payload': bad}).encode())[0], 400)


if __name__ == '__main__':
    unittest.main()
