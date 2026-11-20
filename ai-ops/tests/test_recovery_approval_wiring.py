"""A7 process wiring slice 1: request-approval command, ledger->outbox sync, ingress routing.

Real WF05/06 Slack/queue/worker fixtures, fake clock, fake Slack wire. No network.
"""
import asyncio
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from ai_ops import slack_server
from ai_ops.recovery_approval_wiring import (CHANGED, EXISTING, NOT_OBSERVED, OPENED, UNAVAILABLE,
                                             RecoveryApprovalConfig, RecoveryApprovalService,
                                             parse_approval_arguments, plan_request_id)
from ai_ops.recovery_slack_approval import blocks_valid, parse_button
from ai_ops.slack_events import APPROVAL_DISABLED
from ai_ops.slack_ingress import SlackEventsApp, SlackInteractivityApp, SlackRouter
from ai_ops.slack_web import SlackWebTransport
from test_slack_web import TOKEN, Wire
from test_recovery_slack_approval import SIGNING, ReceiverFixture
import test_recovery_approvals as approvals


def config(policy, enabled=True):
    return RecoveryApprovalConfig(enabled=enabled, prepare_proposal=policy)


async def call(app, path, headers, body):
    events = [{'type': 'http.request', 'body': body}]
    sent = []
    async def receive(): return events.pop(0)
    async def send(event): sent.append(event)
    scope = {'type': 'http', 'scheme': 'https', 'method': 'POST', 'path': path,
             'headers': [(k.encode(), v.encode()) for k, v in headers.items()]
                        + [(b'content-length', str(len(body)).encode())]}
    await app(scope, receive, send)
    return sent[0]['status'], json.loads(sent[1]['body'])


class WiringFixture:
    def setUp(self):
        self.a = approvals.RecoveryApprovalsTest()
        self.a.setUp()
        self.addCleanup(self.a.doCleanups)
        self.f, self.key, self.actor = self.a.f, self.a.key, self.a.actor
        self.adapter = self.f.adapter
        self.plan_request = self.a.request
        token = self.f.queue.root / 'fake-bot-token'
        token.write_bytes(TOKEN)
        token.chmod(0o600)
        self.wire = Wire(self.adapter.policy)
        self.transport = SlackWebTransport(self.adapter.policy, token, enabled=True,
                                           connection_factory=self.wire.connect, monotonic=lambda: 10.0)
        self.transport.verify_identity()
        self.service = self.make_service(observe=self.a.observe)

    def make_service(self, **kwargs):
        service = RecoveryApprovalService(self.adapter, config(self.a.policy), **kwargs)
        self.adapter.recovery_approvals = service
        return service

    def ask(self, plan_hash=None, reference=None, **fields):
        event = self.f.command('request-approval', reference=reference, **fields)
        event['event']['text'] += ' ' + (plan_hash or self.key)
        receipt, _ = self.f.run_request(event)
        self.adapter.reply(receipt['request_id'], self.f.transport)
        return self.f.transport.last_payload['text']

    def records(self):
        return self.service.ledger.page()[0]

    def posts(self):
        return [p['body'] for p in self.wire.posts()]


class RequestApprovalTest(WiringFixture, unittest.TestCase):
    def test_command_opens_wait_and_sync_sends_buttons_to_plan_thread_once(self):
        with patch('subprocess.Popen', side_effect=AssertionError('executor')):
            text = self.ask()
            self.assertIn(OPENED.split('(')[0], text)
            self.assertIn('변경·복구를 실행하지 않았습니다', text)
            [view] = self.records()
            self.assertEqual((view['plan_hash'], view['state']), (self.key, 'awaiting_approval'))
            self.assertFalse(view['execution_allowed'])
            self.assertEqual(self.service.sync_once()['queued'], 1)
            self.assertEqual(self.service.deliver_once(self.transport)['state'], 'delivered')
            self.assertEqual(self.service.sync_once()['queued'], 0)  # idempotent per state
            self.assertEqual(self.service.deliver_once(self.transport)['state'], 'idle')
        [post] = self.posts()
        row = self.adapter._row(self.plan_request)
        self.assertEqual((post['channel'], post['thread_ts']), (row['channel_id'], row['thread_ts']))
        self.assertTrue(blocks_valid(post['blocks'], post['text']))
        pressed = [parse_button(e['action_id'], e['value']) for e in post['blocks'][1]['elements']]
        self.assertEqual([p['decision'] for p in pressed], ['approve', 'reject'])
        self.assertEqual({p['plan_hash'] for p in pressed}, {self.key})
        self.assertEqual(self.a.probe_count, 1)  # one open observation; sync never observes
        self.assertEqual(self.f.count('jobs'), 1)

    def test_repeat_command_does_not_reopen_or_extend(self):
        self.ask()
        first = self.records()[0]
        self.f.fixture.time += 60
        self.assertIn(OPENED.split('(')[0], self.ask())
        again = self.records()[0]
        self.assertEqual((again['approval_hash'], again['wait_until']), (first['approval_hash'], first['wait_until']))
        self.assertEqual(self.a.probe_count, 1)

    def test_decided_plan_cannot_be_reopened(self):
        self.ask()
        view = self.records()[0]
        self.service.ledger.decide(self.key, actor=self.actor, approval_hash=view['approval_hash'],
                                   generation=view['generation'], request_id='r1', decision='reject')
        self.assertEqual(self.ask(), EXISTING.format(state='rejected') + '\n변경·복구를 실행하지 않았습니다. 사람 승인 결과가 아닙니다.')

    def test_default_observation_port_fails_closed(self):
        self.make_service()
        with patch('socket.socket.connect', side_effect=AssertionError('network')):
            self.assertTrue(self.ask().startswith(NOT_OBSERVED))
        self.assertEqual(self.records(), [])
        self.assertEqual(self.service.sync_once()['checked'], 0)
        self.assertEqual(self.posts(), [])

    def test_changed_current_state_requires_new_plan(self):
        def observe(plan, *, deadline=None):
            # A consistent current failure, but on a different image than the plan's evidence.
            state = self.a.observe(plan, deadline=deadline)
            return state.model_copy(update={'image_hash': 'e' * 64,
                                            'failure': state.failure.model_copy(update={'image_hash': 'e' * 64})})
        self.make_service(observe=observe)
        self.assertTrue(self.ask().startswith(CHANGED))
        self.assertEqual(self.records(), [])

    def test_disabled_feature_replies_without_ledger(self):
        self.adapter.recovery_approvals = None
        self.assertTrue(self.ask().startswith(APPROVAL_DISABLED))
        self.assertEqual(self.records(), [])

    def test_unknown_hash_other_thread_or_other_event_are_uniform(self):
        for kwargs in ({'plan_hash': 'f' * 64}, {'thread_ts': '1000.000099'}):
            with self.subTest(kwargs=kwargs):
                self.assertTrue(self.ask(**kwargs).startswith(UNAVAILABLE))
        self.assertEqual(self.records(), [])
        self.assertEqual(self.a.probe_count, 0)

    def test_non_approver_requester_cannot_open(self):
        policy = self.a.policy.model_copy(update={'approvers': ['UBOB0001', self.actor]})
        self.assertEqual(plan_request_id(self.adapter, self.key, actor='UBOB0001'), None)
        self.assertEqual(plan_request_id(self.adapter, self.key, actor=self.actor), self.plan_request)
        with self.assertRaisesRegex(ValueError, 'approvers_not_granted'):
            RecoveryApprovalService(self.adapter, config(policy))

    def test_arguments_only_exact_plan_hash(self):
        for bad in (None, '', 'f' * 63, 'F' * 64, 'f' * 64 + ' approve', 'job-' + 'a' * 32):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_approval_arguments(bad)
        for suffix in ('', ' ' + 'f' * 63, ' ' + self.key + ' approve'):
            event = self.f.command('request-approval')
            event['event']['text'] += suffix
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                self.f.receive(event)

    def test_disabled_config_cannot_build_service(self):
        with self.assertRaisesRegex(ValueError, 'disabled'):
            RecoveryApprovalService(self.adapter, config(self.a.policy, enabled=False))


class DecisionNoticeTest(WiringFixture, ReceiverFixture, unittest.TestCase):
    def setUp(self):
        WiringFixture.setUp(self)
        self.ask()
        self.service.sync_once()
        self.service.deliver_once(self.transport)
        self.record = self.records()[0]
        self.ledger = self.service.ledger
        self.receiver = self.service.receiver(SIGNING)
        self.presses = 0

    def test_button_decision_then_status_notice_with_cancel_only(self):
        result = self.press()
        self.assertEqual((result['state'], result['execution_allowed']), ('approved', False))
        self.assertEqual(self.service.sync_once()['queued'], 1)
        self.service.deliver_once(self.transport)
        notice = self.posts()[-1]
        self.assertIn('승인이 기록되었습니다', notice['text'])
        self.assertEqual([parse_button(e['action_id'], e['value'])['decision']
                          for e in notice['blocks'][1]['elements']], ['cancel'])
        # Nobody revalidates in this slice: the approval expires and that is announced.
        self.f.fixture.time = self.records()[0]['approved_until']
        self.assertEqual(self.service.sync_once()['queued'], 1)
        self.service.deliver_once(self.transport)
        self.assertIn('만료', self.posts()[-1]['text'])
        self.assertEqual(self.a.probe_count, 1)

    def test_router_serves_interactivity_and_rejects_unknown_paths(self):
        class Events(SlackEventsApp):  # stands in for the real events app (tested elsewhere)
            def __init__(self):
                self.adapter, self._inflight, self._tasks = None, 0, set()
            def _handle(self, headers, body):
                raise AssertionError('events app must not see interactivity payloads')
        interactivity = SlackInteractivityApp(self.receiver)
        router = SlackRouter(Events(), interactivity)
        headers, body = self.signed(self.payload())
        self.assertEqual(asyncio.run(call(router, '/slack/interactivity', headers, body)),
                         (200, {'status': 'recorded', 'scope': 'prepare_proposal', 'state': 'approved',
                                'execution_allowed': False}))
        status, _ = asyncio.run(call(router, '/slack/other', headers, body))
        self.assertEqual(status, 403)
        with self.assertRaises(ValueError):
            SlackRouter(interactivity, SlackInteractivityApp(self.receiver))


class RelayTickTest(unittest.TestCase):
    def test_sync_interval_one_delivery_and_failures_contained(self):
        recovery, stopped = Mock(), Mock()
        stopped.is_set.return_value = False
        recovery.sync_once.side_effect = OSError('busy')
        recovery.deliver_once.side_effect = ValueError('slack_web_identity_required')
        state = {'next_sync': 0.0}
        slack_server.recovery_tick(recovery, 'transport', stopped, state, 100.0)
        slack_server.recovery_tick(recovery, 'transport', stopped, state, 101.0)
        slack_server.recovery_tick(recovery, 'transport', stopped, state, 105.0)
        self.assertEqual(recovery.sync_once.call_count, 2)
        self.assertEqual(recovery.deliver_once.call_count, 3)
        recovery.deliver_once.assert_called_with('transport')
        stopped.is_set.return_value = True
        slack_server.recovery_tick(recovery, 'transport', stopped, state, 200.0)
        self.assertEqual(recovery.deliver_once.call_count, 3)


class LedgerPageTest(unittest.TestCase):
    def test_page_cursor_and_retention_filter(self):
        a = approvals.RecoveryApprovalsTest()
        a.setUp()
        self.addCleanup(a.doCleanups)
        a.opened()
        views, cursor = a.ledger.page(limit=1)
        self.assertEqual(([v['plan_hash'] for v in views], cursor), ([a.key], a.key))
        self.assertEqual(a.ledger.page(after=cursor, limit=1), ([], ''))
        a.f.fixture.time = views[0]['retained_until']
        self.assertEqual(a.ledger.page(), ([], ''))
        for bad in ({'limit': 0}, {'limit': True}, {'after': 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                a.ledger.page(**bad)


class ServerWiringTest(unittest.TestCase):
    def setUp(self):
        import test_slack_server
        self.s = test_slack_server.ServerTest()
        self.s.setUp()
        self.addCleanup(self.s.doCleanups)
        self.policy = approvals.ApprovalPolicy(version=1, approvers=['UALICE01'], wait_seconds=7200,
                                               approval_seconds=300, observation_seconds=30,
                                               revalidation_seconds=3)

    def write(self, **changes):
        value = config(self.policy).model_dump()
        value.update(changes)
        self.s.write('recovery-approval.json', value)

    def check(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = slack_server.main(self.s.args('check-config'))
        return code, (json.loads(output.getvalue()) if code == 0 else output.getvalue())

    def test_absent_or_disabled_config_keeps_existing_ingress(self):
        self.assertIsNone(slack_server.load_recovery(self.s.config))
        self.assertEqual(self.check()[1]['recovery_approval'], False)
        self.write(enabled=False)
        self.assertIsNone(slack_server.load_recovery(self.s.config))
        app = slack_server.make_ingress(self.s.tasks, self.s.policy, self.s.web, None)
        self.assertIs(type(app), SlackEventsApp)

    def test_enabled_config_routes_interactivity_without_bot_token_or_network(self):
        self.write()
        self.assertEqual(self.check()[1]['recovery_approval'], True)
        self.s.token.unlink()
        with patch('socket.create_connection', side_effect=AssertionError('network')):
            app = slack_server.make_ingress(self.s.tasks, self.s.policy, self.s.web,
                                            slack_server.load_recovery(self.s.config))
            self.assertIsInstance(app, SlackRouter)
            self.assertEqual(set(app.apps), {'/slack/events', '/slack/interactivity'})
            status, receipt = asyncio.run(call(app, '/slack/events', *self.s.signed_web()))
        self.assertEqual(status, 200)
        self.assertTrue(receipt['accepted'])

    def test_invalid_or_ungranted_config_is_rejected(self):
        self.write(enabled='yes')
        with self.assertRaisesRegex(ValueError, 'recovery_approval_config_rejected'):
            slack_server.load_recovery(self.s.config)
        self.assertEqual(self.check()[0], 1)
        self.write(prepare_proposal={**self.policy.model_dump(), 'approvers': ['UBOB0001']})
        code, output = self.check()
        self.assertEqual(code, 1)
        self.assertNotIn('UBOB0001', output)
        self.write(prepare_proposal={**self.policy.model_dump(), 'scope': 'execute_command'})
        with self.assertRaises(ValueError):
            slack_server.load_recovery(self.s.config)
