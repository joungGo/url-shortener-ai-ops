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
from ai_ops.recovery_approval_wiring import (CHANGED, EXISTING, NOT_OBSERVED, OPENED, OTHER_EVENT,
                                             OTHER_THREAD, UNAVAILABLE,
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

    def test_unknown_hash_or_another_principals_plan_stay_uniform(self):
        unknown = self.ask(plan_hash='f' * 64)
        self.assertTrue(unknown.startswith(UNAVAILABLE))
        # Another principal's plan: same thread, another thread or another event -> the same reply.
        with self.adapter._db() as db:
            db.execute('UPDATE slack_requests SET user_id=? WHERE request_id=?', ('UBOB0001', self.plan_request))
        other = self.adapter.events.ingest('lab-seoul', dict(self.f.alert, fingerprint='fedcba9876543210'))
        for kwargs in ({}, {'thread_ts': '1000.000099'}, {'reference': other}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.ask(**kwargs), unknown)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.a.probe_count, 0)

    def test_own_plan_from_another_thread_names_the_thread_only(self):
        text = self.ask(thread_ts='1000.000099')
        self.assertTrue(text.startswith(OTHER_THREAD))
        row = self.adapter._row(self.plan_request)
        for value in (self.key, row['thread_ts'], row['channel_id'], self.actor):
            self.assertNotIn(value, text)  # no identifiers, only the instruction
        self.assertEqual(self.records(), [])
        self.assertEqual(self.a.probe_count, 0)  # nothing observed or opened
        self.assertTrue(self.ask().startswith(OPENED.split('(')[0]))  # the plan thread still works

    def test_own_plan_with_another_event_names_the_event_only(self):
        other = self.adapter.events.ingest('lab-seoul', dict(self.f.alert, fingerprint='fedcba9876543210'))
        self.assertNotEqual(other, self.f.eid)
        text = self.ask(reference=other)
        self.assertTrue(text.startswith(OTHER_EVENT))
        for value in (self.key, other, self.f.eid):
            self.assertNotIn(value, text)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.a.probe_count, 0)
        # A wrong thread is reported first (both differ).
        self.assertTrue(self.ask(reference=other, thread_ts='1000.000099').startswith(OTHER_THREAD))

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
        for bad in ({'limit': 0}, {'limit': True}, {'after': 1}, {'states': ('bogus',)},
                    {'states': ['approved']}, {'states': ()}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                a.ledger.page(**bad)

    def test_page_state_filter(self):
        a = approvals.RecoveryApprovalsTest()
        a.setUp()
        self.addCleanup(a.doCleanups)
        a.opened()
        self.assertEqual(a.ledger.page(states=('approved', 'proposal_ready')), ([], ''))
        self.assertEqual([v['state'] for v in a.ledger.page(states=('awaiting_approval',))[0]],
                         ['awaiting_approval'])


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

    def relay_kwargs(self):
        with patch.object(slack_server, 'relay_loop') as loop, redirect_stdout(io.StringIO()):
            self.assertEqual(slack_server.main(self.s.args('relay', '--enable-web')), 0)
        return loop.call_args.kwargs

    def test_relay_entrypoint_wires_retention_with_or_without_feature(self):
        from ai_ops.recovery_approval_wiring import OrphanOutboxRetention
        from ai_ops.recovery_retention import RecoveryRetention
        with patch('socket.create_connection', side_effect=AssertionError('network')):
            off = self.relay_kwargs()
            self.assertIsNone(off['recovery'])
            self.assertIsInstance(off['retention'], OrphanOutboxRetention)
            self.write()
            on = self.relay_kwargs()
        self.assertIsInstance(on['recovery'], RecoveryApprovalService)
        self.assertIsInstance(on['retention'], RecoveryRetention)
        self.assertEqual(on['retention'].ledgers, {'prepare_proposal': on['recovery'].ledger})

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


class RetentionWiringTest(WiringFixture, unittest.TestCase):
    def outbox_rows(self):
        with self.adapter._db() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='recovery_outbox'").fetchone() is None:
                return None
            return db.execute('SELECT count(*) FROM recovery_outbox').fetchone()[0]

    def ledger_document(self):
        with self.f.queue.transaction() as db:
            return json.loads(db.execute('SELECT document FROM recovery_approvals WHERE plan_hash=?',
                                         (self.key,)).fetchone()['document'])

    def expire_plan_request(self):
        self.f.fixture.time = self.ledger_document()['retained_until'] + 1
        self.adapter.prune()

    def test_pass_tombstones_ledger_and_deletes_messages_of_expired_requests(self):
        self.ask()
        self.service.sync_once()
        self.service.deliver_once(self.transport)
        retention = self.service.retention()
        kept = retention.tick()
        self.assertEqual((kept['failures'], kept['outbox_removed']), ([], 0))
        self.assertEqual(self.outbox_rows(), 1)
        self.assertIsNotNone(self.ledger_document()['identity'])
        self.expire_plan_request()
        report = retention.tick()
        self.assertEqual((report['failures'], report['outbox_removed'], report['execution_allowed']), ([], 1, False))
        self.assertEqual(self.outbox_rows(), 0)
        document = self.ledger_document()
        self.assertEqual((document['identity'], document['approved_by_hash'], document['proof_hash']),
                         (None, None, None))
        self.assertEqual(self.posts()[-1]['blocks'][1]['elements'][0]['action_id'], 'recovery_prepare_approve')
        self.assertEqual(len(self.posts()), 1)  # retention never sends

    def test_enabled_relay_uses_full_pass_over_its_own_ledger(self):
        from ai_ops.recovery_approval_wiring import relay_retention
        from ai_ops.recovery_retention import RecoveryRetention
        retention = relay_retention(self.adapter, self.service)
        self.assertIsInstance(retention, RecoveryRetention)
        self.assertEqual(retention.ledgers, {'prepare_proposal': self.service.ledger})
        self.assertIs(retention.outbox, self.service.outbox)

    def test_feature_off_prunes_left_messages_without_creating_tables(self):
        from ai_ops.recovery_approval_wiring import OrphanOutboxRetention, relay_retention
        self.ask()
        self.service.sync_once()
        self.adapter.recovery_approvals = None
        orphan = relay_retention(self.adapter)
        self.assertIsInstance(orphan, OrphanOutboxRetention)
        self.assertEqual(orphan.tick()['outbox_removed'], 0)
        self.expire_plan_request()
        self.assertEqual(orphan.tick(), {'failures': [], 'outbox_removed': 1, 'execution_allowed': False})
        with self.adapter._db() as db:
            db.execute('DROP TABLE recovery_outbox')
        self.assertEqual(orphan.tick()['outbox_removed'], 0)
        self.assertIsNone(self.outbox_rows())  # never created by the feature-off pass

    def test_busy_processing_lock_is_a_named_failure_retried_later(self):
        from ai_ops.recovery_approval_wiring import OrphanOutboxRetention
        with self.adapter._lock():
            self.assertEqual(OrphanOutboxRetention(self.adapter).tick()['failures'], ['outbox'])
            self.assertEqual(self.service.retention().tick()['failures'], ['outbox'])


class RelayRetentionLoopTest(unittest.TestCase):
    def test_interval_failure_containment_and_none(self):
        retention = Mock()
        retention.tick.side_effect = OSError('busy')
        state = {'next_retention': 0.0}
        for now in (0.0, 1.0, 599.0, 600.0):
            slack_server.retention_tick(retention, state, now)
        self.assertEqual(retention.tick.call_count, 2)
        slack_server.retention_tick(None, state, 5000.0)

    def test_loop_runs_retention_after_request_prune_and_not_when_stopping(self):
        import threading
        calls = Mock()
        stopped = threading.Event()
        runtime = Mock()
        runtime.relay_once.side_effect = lambda **_: calls.relay()
        runtime.adapter.prune.side_effect = lambda: calls.adapter_prune()
        runtime.tasks.prune.side_effect = lambda: calls.tasks_prune()
        retention = Mock()
        retention.tick.side_effect = lambda: calls.retention()
        progress = Mock()
        progress.tick.side_effect = stopped.set
        slack_server.relay_loop(runtime, stopped, progress, monotonic=lambda: 100.0, retention=retention)
        self.assertEqual([c[0] for c in calls.mock_calls], ['relay', 'adapter_prune', 'tasks_prune', 'retention'])
        stopped.clear()
        runtime.relay_once.side_effect = lambda **_: stopped.set()
        slack_server.relay_loop(runtime, stopped, progress, monotonic=lambda: 9999.0, retention=retention)
        self.assertEqual(retention.tick.call_count, 1)
        # Stop requested during the request prune: no retention pass afterwards.
        stopped.clear()
        runtime.relay_once.side_effect = None
        runtime.tasks.prune.side_effect = stopped.set
        slack_server.relay_loop(runtime, stopped, progress, monotonic=lambda: 99999.0, retention=retention)
        self.assertEqual(retention.tick.call_count, 1)


class AdvanceFixture:
    """Real WF07 fixture (local Git, proof reads, drafts) under the wiring service."""
    def setUp(self):
        import test_recovery_pr as pr
        from ai_ops.recovery_pr import ImageRecoveryPRBridge
        self.t = pr.RecoveryPRTest()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.f, self.key, self.actor = self.t.f, self.t.key, pr.ACTOR
        self.adapter = self.f.adapter
        token = self.f.queue.root / 'fake-bot-token'
        token.write_bytes(TOKEN)
        token.chmod(0o600)
        self.wire = Wire(self.adapter.policy)
        self.transport = SlackWebTransport(self.adapter.policy, token, enabled=True,
                                           connection_factory=self.wire.connect, monotonic=lambda: 10.0)
        self.transport.verify_identity()
        self.Bridge = ImageRecoveryPRBridge
        self.principals = {self.actor: 21}

    def bridge(self, ledger):
        t = self.t
        return self.Bridge(ledger, t.drafts, principals=self.principals, reader=t.reader,
                           read_proof=t.proof, author_id=22, prepare_seconds=15)

    def service(self, *, observe='fixture', bridge=True):
        kwargs = {'observe': self.t.observation} if observe == 'fixture' else {}
        service = RecoveryApprovalService(self.adapter, config(self.t.policy),
                                          pr_bridge=self.bridge if bridge else None, **kwargs)
        self.adapter.recovery_approvals = service
        return service

    def approved(self, service):
        event = self.f.command('request-approval')
        event['event']['text'] += ' ' + self.key
        self.f.run_request(event)
        view = service.ledger.page()[0][0]
        self.assertEqual(view['state'], 'awaiting_approval')
        return service.ledger.decide(self.key, actor=self.actor, approval_hash=view['approval_hash'],
                                     generation=view['generation'], request_id='press-1', decision='approve')

    def deliver_all(self, service):
        service.sync_once()
        while service.deliver_once(self.transport)['state'] != 'idle':
            pass
        return [p['body']['text'] for p in self.wire.posts()]


class AdvanceTest(AdvanceFixture, unittest.TestCase):
    def test_approval_revalidates_then_prepares_exact_pr_once_and_announces_both(self):
        service = self.service()
        self.approved(service)
        with patch('socket.socket.connect', side_effect=AssertionError('network')):
            self.assertEqual(service.advance_once()['state'], 'proposal_ready')
            prepared = service.advance_once()
            self.assertEqual((prepared['action'], prepared['state'], prepared['execution_allowed']),
                             ('prepare', 'prepared', False))
            proof_calls = self.t.proof_calls
            self.assertEqual(service.advance_once()['action'], 'none')  # never prepared twice
            self.assertEqual(self.t.proof_calls, proof_calls)
        texts = self.deliver_all(service)
        self.assertTrue(any('재확인을 통과해 복구 PR 준비 단계로' in t for t in texts))
        [pr_text] = [t for t in texts if '정확한 변경(diff)' in t]
        self.assertIn('GitHub PR 생성·병합·배포는 하지 않았으며', pr_text)
        self.assertNotIn('https://', pr_text)
        self.assertEqual(self.t.writer.calls, 0)  # publication is not wired
        link = self.t.bridge.show(self.key, actor=self.actor)
        self.assertEqual((link['state'], link['url'], link['pull_number']), ('prepared', None, None))
        self.assertEqual(self.deliver_all(service), texts)  # idempotent per state

    def test_default_observation_turns_approval_into_needs_review_without_prepare(self):
        self.approved(self.service())
        service = self.service(observe=None)
        result = service.advance_once()
        self.assertEqual((result['action'], result['state']), ('revalidate', 'needs_review'))
        self.assertEqual(service.advance_once()['action'], 'none')
        self.assertFalse(service._pr_exists(self.key))
        self.assertTrue(any('사람의 확인이 필요' in t and 'read_only_revalidation_failed' in t
                            for t in self.deliver_all(service)))

    def test_without_bridge_proposal_ready_is_announced_but_not_prepared(self):
        service = self.service(bridge=False)
        self.approved(service)
        self.assertEqual(service.advance_once()['state'], 'proposal_ready')
        self.assertEqual(service.advance_once()['action'], 'none')
        self.assertEqual(self.t.proof_calls, 0)
        texts = self.deliver_all(service)
        self.assertTrue(any('복구 PR 준비 단계로' in t for t in texts))
        self.assertFalse(any('정확한 변경(diff)' in t for t in texts))

    def test_failed_preparation_is_announced_once_and_never_retried(self):
        service = self.service()
        self.approved(service)
        service.advance_once()
        self.t.proof_hook = lambda: (_ for _ in ()).throw(OSError('private-detail'))
        self.assertEqual(service.advance_once(), {'action': 'prepare', 'state': 'failed', 'execution_allowed': False})
        calls = self.t.proof_calls
        self.assertEqual(service.advance_once()['action'], 'none')
        self.assertEqual(self.t.proof_calls, calls)
        [text] = [t for t in self.deliver_all(service) if 'PR 준비 결과를 확정할 수 없어' in t]
        self.assertNotIn('private-detail', text)

    def test_unmapped_github_principal_creates_no_record(self):
        self.principals = {'UOTHER01': 21}
        service = self.service()
        self.approved(service)
        service.advance_once()
        self.assertEqual(service.advance_once()['state'], 'failed')
        self.assertFalse(service._pr_exists(self.key))
        self.assertEqual(self.t.proof_calls, 0)

    def test_pending_or_unapproved_records_are_not_advanced(self):
        service = self.service()
        event = self.f.command('request-approval')
        event['event']['text'] += ' ' + self.key
        self.f.run_request(event)
        probes = len(self.wire.posts())
        self.assertEqual(service.advance_once()['action'], 'none')
        self.assertEqual(service.ledger.page()[0][0]['state'], 'awaiting_approval')
        self.assertEqual(len(self.wire.posts()), probes)

    def test_bridge_must_wrap_the_service_ledger_and_revalidation_fits_relay(self):
        with self.assertRaisesRegex(ValueError, 'recovery_pr_bridge_rejected'):
            RecoveryApprovalService(self.adapter, config(self.t.policy), pr_bridge=lambda _: self.t.bridge)
        with self.assertRaisesRegex(ValueError, 'revalidation_exceeds_relay_budget'):
            config(self.t.policy.model_copy(update={'revalidation_seconds': 16}))

    def test_pr_notice_rejects_unbound_views_and_ignores_other_states(self):
        service = self.service()
        request = self.t.plan.request_id
        base = {'plan_hash': self.key, 'generation': 1, 'execution_allowed': False, 'state': 'prepared'}
        for bad in ({'plan_hash': 'x'}, {'generation': True}, {'execution_allowed': True}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                service.outbox.enqueue_pr(request, {**base, **bad})
        self.assertEqual(service.outbox.enqueue_pr(request, {**base, 'state': 'publishing'})['state'],
                         'nothing_to_send')
        service.outbox.enqueue_pr(request, {**base, 'url': 'https://example.invalid/pr/1', 'reason': 'Bad Reason!'})
        service.deliver_once(self.transport)
        text = self.wire.posts()[-1]['body']['text']
        self.assertNotIn('example.invalid', text)
        self.assertNotIn('Bad Reason', text)


class RecoveryTickOrderTest(unittest.TestCase):
    def test_advance_before_sync_and_not_while_stopping(self):
        calls, stopped = Mock(), Mock()
        stopped.is_set.return_value = False
        state = {'next_sync': 0.0}
        slack_server.recovery_tick(calls, 'transport', stopped, state, 10.0)
        self.assertEqual([c[0] for c in calls.mock_calls], ['advance_once', 'sync_once', 'deliver_once'])
        calls.reset_mock()
        stopped.is_set.return_value = True
        slack_server.recovery_tick(calls, 'transport', stopped, state, 20.0)
        self.assertEqual([c[0] for c in calls.mock_calls], ['sync_once'])


class AdvanceRetentionTest(AdvanceFixture, unittest.TestCase):
    def test_retention_also_expires_prepared_wf07_records(self):
        service = self.service()
        self.approved(service)
        service.advance_once()
        self.assertEqual(service.advance_once()['state'], 'prepared')
        retention = service.retention()
        self.assertEqual(set(retention.ledgers), {'prepare_proposal', 'recovery_pr'})
        with self.f.queue.transaction() as db:
            document = json.loads(db.execute('SELECT document FROM recovery_pr_links WHERE plan_hash=?',
                                             (self.key,)).fetchone()['document'])
        self.assertIsNotNone(document['payload'])
        self.f.fixture.time = document['retained_until']
        self.assertEqual(retention.tick()['failures'], [])
        with self.f.queue.transaction() as db:
            document = json.loads(db.execute('SELECT document FROM recovery_pr_links WHERE plan_hash=?',
                                             (self.key,)).fetchone()['document'])
        self.assertEqual((document['state'], document['payload'], document['proposal']), ('expired', None, None))
        self.assertEqual(set(self.service(bridge=False).retention().ledgers), {'prepare_proposal'})
