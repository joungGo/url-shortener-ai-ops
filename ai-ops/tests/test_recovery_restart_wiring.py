"""A7 restart (command) path wiring up to command_ready; execution stays disabled.

Real WF05/06/08 Slack/queue/worker fixtures, fake clock, fake Slack wire, fake
current-state reader (or the fake Kubernetes API for the real read-only port).
No network, no subprocess, no cluster write.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import test_recovery_plans as plans
from test_recovery_slack_approval import SIGNING
from test_slack_web import TOKEN, Wire
from ai_ops.c_recovery import TARGET, IMAGE_REPOSITORY
from ai_ops.contracts import sha256_json
from ai_ops.recovery_approval_wiring import (FULL, NOT_OBSERVED, RecoveryApprovalConfig,
                                             RecoveryApprovalService)
from ai_ops.recovery_approvals import ApprovalPolicy
from ai_ops.recovery_commands import RestartSnapshot
from ai_ops.recovery_observe import ObserveConfig
from ai_ops.recovery_plans import plan_text
from ai_ops.recovery_restart_wiring import (RESTART_DISABLED, RESTART_EXISTING, RESTART_OPENED, RESTART_STALE,
                                            RESTART_UNAVAILABLE, PrometheusConfig, RestartConfig,
                                            make_restart_reader)
from ai_ops.recovery_slack_approval import blocks_valid, parse_button
from ai_ops.slack_web import SlackWebTransport

ACTOR = 'UALICE01'
UID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
OBSERVE = ObserveConfig(api_url='https://kubernetes.default.svc', token_file='/nonexistent/reader-token',
                        ca_file='/nonexistent/reader-ca', argo_namespace='argocd', argo_application='ai-ops-lab')
PROM = PrometheusConfig(api_url='https://prometheus.monitoring.svc:9090', token_file='/nonexistent/prom-token',
                        ca_file='/nonexistent/prom-ca',
                        prior_ready_query='max_over_time(ready{template="$TEMPLATE_HASH"}[1d])',
                        dependency_queries=['min(db_up)'])


def policy(scope='prepare_proposal', **changes):
    values = dict(version=1, approvers=[ACTOR], wait_seconds=7200, approval_seconds=300,
                  observation_seconds=30, revalidation_seconds=3, scope=scope)
    values.update(changes)
    return ApprovalPolicy(**values)


def restart_config(**changes):
    values = dict(approval=policy('execute_command'), executor_id='local-restart-test', execution_seconds=10,
                  prometheus=PROM)
    values.update(changes)
    return RestartConfig(**values)


def config(restart=True, **changes):
    values = dict(enabled=True, prepare_proposal=policy(), observe=OBSERVE,
                  restart=restart_config() if restart is True else restart)
    values.update(changes)
    return RecoveryApprovalConfig(**values)


class RestartWiringFixture:
    def setUp(self):
        self.f = plans.RecoveryPlansTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.snapshot(reason='Unhealthy', message='Readiness probe failed: timeout', phase='Running',
                        deployment_uid=UID, pod_deployment_uid=UID)
        self.job = self.f.investigate()
        receipt, _ = self.f.plan(self.job, 'deployment-restart')
        self.plan_request = receipt['request_id']
        self.candidate = self.f.saved(self.plan_request)
        self.adapter = self.f.adapter
        self.state = RestartSnapshot(
            target=TARGET, deployment_uid=UID, resource_version='1', revision_hash='b' * 64,
            image=IMAGE_REPOSITORY + '@sha256:' + 'a' * 64, pod_uid_hash=sha256_json('pod-1'), observed_at=self.now(),
            generation=1, observed_generation=1, ready_replicas=0, strategy='Recreate', container_running=True,
            restart_count=0, pod_started_at=self.now() - timedelta(hours=1),
            readiness_failing_since=self.now() - timedelta(minutes=10), readiness_window_seconds=30,
            prior_ready='observed', dependencies='pass', rollout_in_progress=False, autoscaling=False,
            gitops_auto_sync=False, annotations_exist=True)
        self.reads = 0
        self.read_error = None
        token = self.f.queue.root / 'fake-bot-token'
        token.write_bytes(TOKEN)
        token.chmod(0o600)
        self.wire = Wire(self.adapter.policy)
        self.transport = SlackWebTransport(self.adapter.policy, token, enabled=True,
                                           connection_factory=self.wire.connect, monotonic=lambda: 10.0)
        self.transport.verify_identity()
        self.service = self.make_service()
        self.presses = 0

    def now(self):
        return datetime.fromtimestamp(self.f.fixture.time, timezone.utc)

    def read(self, plan, *, deadline):
        self.reads += 1
        if self.read_error is not None:
            raise self.read_error
        return self.state.model_copy(update={'observed_at': self.now()})

    def pr_observe(self, plan, *, deadline=None):
        raise AssertionError('the PR path must not observe a restart candidate')

    def make_service(self, cfg=None):
        service = RecoveryApprovalService(self.adapter, cfg or config(), observe=self.pr_observe,
                                          restart_read=self.read)
        self.adapter.recovery_approvals = service
        return service

    def ask(self, plan_hash=None):
        event = self.f.command('request-approval', reference='this')
        event['event']['text'] += ' ' + (plan_hash or self.candidate.plan_hash)
        receipt, _ = self.f.run_request(event)
        self.adapter.reply(receipt['request_id'], self.f.transport)
        return self.f.transport.last_payload['text']

    def restart_records(self):
        return self.service.restart.ledger.page()[0]

    def pr_records(self):
        return self.service.ledger.page()[0]

    def posts(self):
        return [p['body'] for p in self.wire.posts()]

    def flush(self):
        self.service.sync_once()
        while self.service.deliver_once(self.transport)['state'] == 'delivered':
            pass

    def press(self, decision='approve'):
        """Press the button exactly as posted to the thread (signed Slack payload)."""
        post = next(p for p in reversed(self.posts()) if 'blocks' in p)
        button = next(e for e in post['blocks'][1]['elements'] if parse_button(e['action_id'], e['value'])['decision'] == decision)
        self.presses += 1
        body = {'type': 'block_actions', 'api_app_id': 'ALOCAL01', 'team': {'id': 'TLOCAL01'},
                'user': {'id': ACTOR, 'team_id': 'TLOCAL01'}, 'channel': {'id': 'CLOCAL01'},
                'container': {'type': 'message', 'channel_id': 'CLOCAL01', 'message_ts': '1000.000010'},
                'is_enterprise_install': False, 'enterprise': None, 'trigger_id': 'ignored',
                'response_url': 'https://hooks.slack.invalid/ignored',
                'actions': [{'type': 'button', 'action_id': button['action_id'], 'value': button['value'],
                             'action_ts': f'1000.{self.presses:06d}', 'block_id': 'b'}]}
        raw = urlencode({'payload': json.dumps(body)}).encode()
        stamp = str(int(self.f.fixture.time))
        signature = 'v0=' + hmac.new(SIGNING, b'v0:' + stamp.encode() + b':' + raw, hashlib.sha256).hexdigest()
        headers = {'Content-Type': 'application/x-www-form-urlencoded', 'X-Slack-Request-Timestamp': stamp,
                   'X-Slack-Signature': signature}
        return self.service.receiver(SIGNING).receive(headers, raw)

    def runs(self):
        with self.f.queue.transaction() as db:
            return [json.loads(r['document']) for r in db.execute('SELECT document FROM restart_runs')]


class PlanReplyTest(RestartWiringFixture, unittest.TestCase):
    def test_command_candidate_reply_points_to_request_approval_without_contract_change(self):
        self.assertEqual((self.candidate.assessment, self.candidate.reason, self.candidate.method),
                         ('unsupported', 'action_not_implemented', 'command'))
        text = plan_text(self.candidate)
        self.assertIn('명령 후보 (조건은 승인 요청 시 판정, 실행 비활성)', text)
        self.assertIn(f'request-approval this {self.candidate.plan_hash}', text)
        self.assertIn('실제 재시작은 실행하지 않습니다', text)
        self.assertNotIn('실행기는 아직 구현되지 않았습니다', text)
        self.assertNotIn('새 복구안 요청', text)
        self.assertNotIn('미지원', text)


class RestartRequestTest(RestartWiringFixture, unittest.TestCase):
    def test_request_seals_plan_opens_command_wait_and_sends_command_buttons(self):
        with patch('socket.socket.connect', side_effect=AssertionError('network')), \
             patch('subprocess.Popen', side_effect=AssertionError('executor')):
            text = self.ask()
            [view] = self.restart_records()
            self.assertTrue(text.startswith(RESTART_OPENED.split('(')[0]), text)
            self.assertIn(view['plan_hash'][:12], text)
            self.assertEqual((view['state'], view['approval_scope'], view['execution_allowed'], view['command_ready']),
                             ('awaiting_approval', 'execute_command', False, False))
            self.assertEqual(self.service.restart.candidate_of(view['plan_hash']), self.candidate.plan_hash)
            self.assertEqual(self.pr_records(), [])  # never the PR ledger
            self.assertEqual(self.reads, 2)  # prepare (gates) + open
            self.assertEqual(self.service.sync_once()['queued'], 1)
            self.assertEqual(self.service.deliver_once(self.transport)['state'], 'delivered')
            self.assertEqual(self.service.sync_once()['queued'], 0)
        [post] = self.posts()
        row = self.adapter._row(self.plan_request)
        self.assertEqual((post['channel'], post['thread_ts']), (row['channel_id'], row['thread_ts']))
        self.assertTrue(post['text'].startswith('[고정 복구 명령 실행]'))
        self.assertTrue(blocks_valid(post['blocks'], post['text']))
        pressed = [parse_button(e['action_id'], e['value']) for e in post['blocks'][1]['elements']]
        self.assertEqual({(p['scope'], p['decision']) for p in pressed},
                         {('execute_command', 'approve'), ('execute_command', 'reject')})
        [run] = self.runs()
        self.assertEqual((run['state'], run['applied']), ('prepared', False))
        self.assertEqual(self.f.count('jobs'), 1)

    def test_repeat_request_does_not_reseal_or_reopen(self):
        self.ask()
        first = self.restart_records()[0]
        self.f.fixture.time += 30
        self.assertTrue(self.ask().startswith(RESTART_OPENED.split('(')[0]))
        again = self.restart_records()[0]
        self.assertEqual((again['plan_hash'], again['approval_hash'], again['wait_until']),
                         (first['plan_hash'], first['approval_hash'], first['wait_until']))
        self.assertEqual(self.reads, 2)

    def test_decided_restart_cannot_be_reopened(self):
        self.ask()
        self.flush()
        self.press('reject')
        self.assertTrue(self.ask().startswith(RESTART_EXISTING.format(state='rejected')))

    def test_gate_rejection_names_failed_conditions_and_opens_nothing(self):
        self.state = self.state.model_copy(update={'autoscaling': True, 'gitops_auto_sync': True})
        text = self.ask()
        self.assertIn('재시작 조건을 통과하지 못해', text)
        self.assertIn('no_competing_controller', text)
        self.assertEqual((self.restart_records(), self.runs()), ([], []))

    def test_stale_investigation_requires_new_investigation(self):
        self.f.fixture.time += 200  # > 120 s after the investigation's pod observation
        self.assertTrue(self.ask().startswith(RESTART_STALE))
        self.assertEqual((self.restart_records(), self.runs()), ([], []))
        self.assertEqual(self.reads, 1)

    def test_changed_workload_requires_new_plan(self):
        self.state = self.state.model_copy(update={'pod_uid_hash': sha256_json('pod-2')})
        self.assertTrue(self.ask().startswith(RESTART_STALE))
        self.assertEqual(self.runs(), [])

    def test_read_failures_fail_closed_without_echo(self):
        for error in (OSError('secret-token-in-error'), TimeoutError('t'), KeyError('spec'), TypeError('x')):
            with self.subTest(error=type(error).__name__):
                self.f.fixture.time += 61  # Slack admission: 5 requests per user per minute
                self.read_error = error
                text = self.ask()
                self.assertTrue(text.startswith(NOT_OBSERVED), text)
                self.assertNotIn('secret-token', text)
        self.assertEqual((self.restart_records(), self.runs()), ([], []))

    def test_full_ledger_is_reported(self):
        with patch.object(type(self.service.restart.coordinator), 'prepare',
                          side_effect=ValueError('restart_ledger_full')):
            self.assertTrue(self.ask().startswith(FULL))

    def test_unknown_code_is_uniform(self):
        with patch.object(type(self.service.restart.coordinator), 'prepare',
                          side_effect=ValueError('/private/path leaked')):
            text = self.ask()
        self.assertTrue(text.startswith(RESTART_UNAVAILABLE))
        self.assertNotIn('/private', text)

    def test_disabled_restart_path_replies_without_any_restart_table(self):
        self.service = self.make_service(config(restart=None, observe=None))
        self.assertIsNone(self.service.restart)
        self.assertTrue(self.ask().startswith(RESTART_DISABLED))
        self.assertEqual((self.runs(), self.reads), ([], 0))


class CommandReadyTest(RestartWiringFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ask()
        self.flush()

    def test_approve_revalidate_command_ready_never_executes(self):
        with patch('socket.socket.connect', side_effect=AssertionError('network')), \
             patch('subprocess.Popen', side_effect=AssertionError('executor')):
            result = self.press()
            self.assertEqual((result['scope'], result['state'], result['execution_allowed']),
                             ('execute_command', 'approved', False))
            self.flush()
            self.assertIn('승인이 기록되었습니다', self.posts()[-1]['text'])
            step = self.service.advance_once()
            self.assertEqual(step, {'action': 'revalidate_command', 'state': 'proposal_ready',
                                    'execution_allowed': False})
            [view] = self.restart_records()
            self.assertTrue(view['command_ready'])
            self.assertFalse(view['proposal_ready'])
            self.flush()
        notice = self.posts()[-1]['text']
        self.assertTrue(notice.startswith('[고정 복구 명령 실행]'))
        self.assertIn('실제 재시작 실행은 실환경 실행 결정 전까지 비활성', notice)
        self.assertNotIn('PR', notice)
        self.assertEqual(self.service.advance_once()['action'], 'none')  # nothing after command_ready
        [run] = self.runs()
        self.assertEqual((run['state'], run['applied'], run['lease']), ('prepared', False, None))
        self.assertEqual(self.reads, 3)

    def test_revalidation_gate_failure_is_manual_review(self):
        self.press()
        self.state = self.state.model_copy(update={'rollout_in_progress': True})
        self.assertEqual(self.service.advance_once()['state'], 'needs_review')
        [view] = self.restart_records()
        self.assertEqual(view['state'], 'needs_review')
        self.assertFalse(view['command_ready'])
        self.flush()
        self.assertIn('사람의 확인이 필요합니다', self.posts()[-1]['text'])

    def test_observation_expired_after_ready_uses_command_wording(self):
        self.press()
        self.service.advance_once()
        self.flush()
        self.f.fixture.time = self.restart_records()[0]['fresh_until'] + 1
        self.flush()
        notice = self.posts()[-1]['text']
        self.assertIn('재시작은 실행하지 않았으며', notice)
        self.assertNotIn('PR', notice)

    def test_command_button_needs_the_command_ledger(self):
        button = self.posts()[-1]['blocks'][1]['elements'][0]
        self.assertTrue(button['action_id'].startswith('recovery_execute_'))
        # A process without the restart section serves only PR-preparation buttons.
        self.service = RecoveryApprovalService(self.adapter, config(restart=None), observe=self.pr_observe)
        with self.assertRaises(ValueError):
            self.press()
        self.assertEqual(self.restart_records_of(RecoveryApprovalService(
            self.adapter, config(), observe=self.pr_observe, restart_read=self.read))[0]['state'],
            'awaiting_approval')

    def restart_records_of(self, service):
        return service.restart.ledger.page()[0]

    def test_retention_covers_restart_ledger_and_plans(self):
        retention = self.service.retention()
        self.assertIn('execute_command', retention.ledgers)
        self.assertIs(retention.commands, self.service.restart.coordinator)
        self.f.fixture.time = self.restart_records()[0]['retained_until'] + 1
        report = retention.tick()
        self.assertEqual(report['failures'], [])
        [run] = self.runs()
        self.assertEqual(run['state'], 'expired')
        # History is deleted TOMBSTONE_SECONDS after retention (caps never fill by history alone).
        from ai_ops.recovery_approvals import TOMBSTONE_SECONDS
        retained = run['retained_until']
        self.f.fixture.time = retained + TOMBSTONE_SECONDS - 1
        retention.tick()
        self.assertEqual(len(self.runs()), 1)
        self.f.fixture.time = retained + TOMBSTONE_SECONDS
        self.assertEqual(retention.tick()['failures'], [])
        self.assertEqual((self.runs(), self.restart_records()), ([], []))
        with self.f.queue.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM restart_plans').fetchone()[0], 0)


class RestartConfigTest(unittest.TestCase):
    def test_restart_requires_observe_relay_budget_and_command_scope(self):
        with self.assertRaisesRegex(ValueError, 'restart_requires_observe'):
            config(observe=None)
        with self.assertRaisesRegex(ValueError, 'relay_budget'):
            config(restart=restart_config(execution_seconds=13))  # 13 + 3 > 15
        config(restart=restart_config(execution_seconds=12))
        with self.assertRaisesRegex(ValueError, 'restart_approval_scope_rejected'):
            restart_config(approval=policy())
        with self.assertRaises(ValueError):
            PrometheusConfig(api_url='http://prometheus', token_file='/t', ca_file='/c')
        with self.assertRaises(ValueError):
            PrometheusConfig(api_url='https://p', token_file='relative', ca_file='/c')
        with self.assertRaises(ValueError):
            restart_config(executor_id='remote-restart')

    def test_restart_approvers_must_be_granted(self):
        fixture = RestartWiringFixture()
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        cfg = config(restart=restart_config(approval=policy('execute_command', approvers=['UNOBODY1'])))
        with self.assertRaisesRegex(ValueError, 'approvers_not_granted'):
            RecoveryApprovalService(fixture.adapter, cfg)


class ReadOnlyPortTest(unittest.TestCase):
    def test_real_reader_uses_observer_identity_and_has_no_writer(self):
        import tempfile
        from test_restart_k8s import FakeApi, NOW
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name in ('reader-token', 'reader-ca', 'prom-token', 'prom-ca'):
            (root / name).write_text('value')
        observe = OBSERVE.model_copy(update={'token_file': str(root / 'reader-token'),
                                             'ca_file': str(root / 'reader-ca')})
        prom = PROM.model_copy(update={'token_file': str(root / 'prom-token'), 'ca_file': str(root / 'prom-ca')})
        api = FakeApi()
        port = make_restart_reader(observe, restart_config(prometheus=prom), now=lambda: NOW,
                                   connection_factory=api.connect)
        self.assertIsNone(port.writer)
        self.assertEqual(port.mode, 'kubernetes')  # the WF08 executor refuses non-fixture ports
        snapshot = port.read(deadline=NOW + 10)
        self.assertEqual((snapshot['prior_ready'] if isinstance(snapshot, dict) else snapshot.prior_ready), 'observed')
        self.assertEqual({c['method'] for c in api.calls}, {'GET'})
        from ai_ops.recovery_commands import patch_request
        with self.assertRaises(AttributeError):  # no writer client at all
            port.patch(patch_request(UID, '7', 'c' * 64, True), deadline=NOW + 10)
        self.assertEqual({c['method'] for c in api.calls}, {'GET'})


class ServerEntrypointTest(unittest.TestCase):
    def setUp(self):
        import test_recovery_approval_wiring as wiring
        self.w = wiring.ServerWiringTest()
        self.w.setUp()
        self.addCleanup(self.w.doCleanups)

    def write(self, **changes):
        value = config().model_dump()
        value.update(changes)
        self.w.s.write('recovery-approval.json', value)

    def test_check_config_relay_and_ingress_wire_the_read_only_restart_path(self):
        from ai_ops import slack_server
        from ai_ops.slack_ingress import SlackRouter
        self.write()
        with patch('socket.create_connection', side_effect=AssertionError('network')):
            self.assertTrue(self.w.check()[1]['recovery_restart_configured'])
            kwargs = self.w.relay_kwargs()
            app = slack_server.make_ingress(self.w.s.tasks, self.w.s.policy, self.w.s.web,
                                            slack_server.load_recovery(self.w.s.config))
        service = kwargs['recovery']
        self.assertIsNone(service.restart.port.writer)
        self.assertEqual(kwargs['retention'].ledgers['execute_command'], service.restart.ledger)
        self.assertIs(kwargs['retention'].commands, service.restart.coordinator)
        self.assertIsInstance(app, SlackRouter)

    def test_check_config_without_restart_and_with_ungranted_restart_approver(self):
        self.write(restart=None)
        self.assertFalse(self.w.check()[1]['recovery_restart_configured'])
        self.write(restart={**restart_config().model_dump(),
                            'approval': policy('execute_command', approvers=['UBOB0001']).model_dump()})
        code, output = self.w.check()
        self.assertEqual(code, 1)
        self.assertNotIn('UBOB0001', output)


class TransitionTest(RestartWiringFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ask()
        self.dir = Path(os.environ['AI_OPS_FILES_ROOT']) / 'restart-transition'
        self.dir.mkdir(mode=0o700)
        (self.dir / 'config').mkdir()
        self.previous = self.dir / 'previous.json'
        self.previous.write_text(json.dumps(config().model_dump()))

    def transition(self, new):
        from ai_ops import slack_server
        (self.dir / 'config' / 'recovery-approval.json').write_text(json.dumps(new.model_dump()))
        return slack_server.transition_approval_policy(self.f.queue.root, self.dir / 'config', self.previous)

    def test_restart_policy_change_supersedes_only_restart_approvals(self):
        new = config(restart=restart_config(approval=policy('execute_command', version=2, wait_seconds=9000)))
        result = self.transition(new)
        self.assertEqual((result['status'], result['superseded']), ('transitioned', 1))
        self.assertEqual(result['scopes']['execute_command']['status'], 'transitioned')
        self.assertEqual(result['scopes']['prepare_proposal']['status'], 'unchanged')
        service = self.make_service(new)
        [view] = service.restart.ledger.page()[0]
        self.assertEqual((view['state'], view['reason']), ('superseded', 'approval_policy_changed'))

    def test_restart_previous_must_match_and_nothing_changes_on_refusal(self):
        self.previous.write_text(json.dumps(config(restart=restart_config(
            approval=policy('execute_command', wait_seconds=1234))).model_dump()))
        new = config(restart=restart_config(approval=policy('execute_command', version=2, wait_seconds=9000)))
        with self.assertRaisesRegex(ValueError, 'previous_policy_mismatch'):
            self.transition(new)
        self.assertEqual(self.restart_records()[0]['state'], 'awaiting_approval')

    def test_unchanged_policies_are_nothing_to_transition(self):
        result = self.transition(config())
        self.assertEqual(result['status'], 'nothing_to_transition')
        self.assertEqual(result['scopes']['execute_command']['status'], 'unchanged')
        self.assertEqual(self.restart_records()[0]['state'], 'awaiting_approval')


if __name__ == '__main__':
    unittest.main()
