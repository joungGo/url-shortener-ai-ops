"""WF07 PR publication with the C03 writer App (WF11 follow-up A2, 2026-11-26). Offline.

* the writer sends only allowlisted requests (no merge/review/delete/settings), to
  api.github.com, with an installation token, through the configured proxy only;
* the payload must be exactly the bridge's single values.yaml change;
* the bridge accepts exactly this production class (or test fixtures) and the relay
  publishes in the same step as preparation, inside the freshness window.
"""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler

from ai_ops import recovery_pr_writer as w
from ai_ops.recovery_pr_writer import GitHubAppPRWriter
import test_recovery_approval_wiring as wiring

KEY = 'a' * 64
BASE = 'b' * 40
TOKEN = 'ghs_' + 'T' * 36
PROXY = 'http://ai-ops-egress.ai-ops.svc:8080'
REPO = '/repos/joungGo/c03-execution-lab'


def payload(**changes):
    p = {'repository': 'joungGo/c03-execution-lab', 'base': 'main', 'base_sha': BASE,
         'branch': 'recovery/wf07-' + KEY, 'values_path': 'ai-ops/lab/values.yaml',
         'before': '{"api": {"image": {"digest": "sha256:bad"}}}\n',
         'after': '{"api": {"image": {"digest": "sha256:good"}}}\n', 'diff': '-bad\n+good\n',
         'commit_message': '격리 앱 정상 이미지 복구', 'title': '격리 앱 이미지 복구 계획',
         'description': {'plan_hash': KEY, 'link_hash': 'c' * 64}, 'write_enabled': False}
    p.update(changes)
    return p


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeGitHub:
    """Records requests; answers the allowlisted calls."""
    def __init__(self, fail=None, pulls=()):
        self.requests, self.fail, self.pulls = [], fail, list(pulls)

    def open(self, request, timeout=None):
        path = request.full_url.removeprefix('https://api.github.com')
        body = json.loads(request.data) if request.data else None
        self.requests.append((request.get_method(), request.full_url, body, dict(request.header_items())))
        self.timeouts = getattr(self, 'timeouts', []) + [timeout]
        if self.fail and self.fail in path:
            raise HTTPError(request.full_url, 422, 'secret-upstream-text', {}, io.BytesIO(b'secret-body'))
        answers = {'/git/blobs': {'sha': '1' * 40}, '/git/commits/' + BASE: {'tree': {'sha': '2' * 40}},
                   '/git/trees': {'sha': '3' * 40}, '/git/commits': {'sha': '4' * 40},
                   '/git/refs': {'ref': 'refs/heads/x'}, '/pulls': {'number': 7}}
        for suffix, answer in answers.items():
            if path == REPO + suffix:
                return Response(json.dumps(answer).encode())
        if path.startswith(REPO + '/pulls?head='):
            return Response(json.dumps(self.pulls).encode())
        raise AssertionError('unexpected request ' + path)


class WriterTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.token = Path(tmp.name) / 'token'
        self.token.write_text(TOKEN + '\n')
        self.token.chmod(0o600)
        self.gh = FakeGitHub()
        self.writer = GitHubAppPRWriter(self.token, proxy=PROXY, opener=self.gh)

    def test_create_writes_exactly_one_file_branch_and_pr(self):
        self.assertEqual(self.writer.create(KEY, payload()), 7)
        calls = [(m, u.removeprefix('https://api.github.com')) for m, u, _, _ in self.gh.requests]
        self.assertEqual(calls, [('POST', REPO + '/git/blobs'), ('GET', REPO + '/git/commits/' + BASE),
                                 ('POST', REPO + '/git/trees'), ('POST', REPO + '/git/commits'),
                                 ('POST', REPO + '/git/refs'), ('POST', REPO + '/pulls')])
        bodies = {u.rsplit('/', 1)[-1] if 'commits/' not in u else 'base': b for _, u, b, _ in self.gh.requests}
        self.assertEqual(bodies['blobs'], {'content': payload()['after'], 'encoding': 'utf-8'})
        self.assertEqual(bodies['trees'], {'base_tree': '2' * 40, 'tree': [
            {'path': 'ai-ops/lab/values.yaml', 'mode': '100644', 'type': 'blob', 'sha': '1' * 40}]})
        self.assertEqual(bodies['commits'], {'message': '격리 앱 정상 이미지 복구', 'tree': '3' * 40, 'parents': [BASE]})
        self.assertEqual(bodies['refs'], {'ref': 'refs/heads/recovery/wf07-' + KEY, 'sha': '4' * 40})
        pull = bodies['pulls']
        self.assertEqual((pull['head'], pull['base'], pull['draft'], pull['maintainer_can_modify']),
                         ('recovery/wf07-' + KEY, 'main', False, False))
        self.assertIn('자동으로 병합·배포되지 않으며', pull['body'])
        for _, url, _, headers in self.gh.requests:
            self.assertTrue(url.startswith('https://api.github.com/repos/joungGo/c03-execution-lab/'))
            self.assertEqual(headers['Authorization'], 'Bearer ' + TOKEN)

    def test_allowlist_has_no_merge_review_delete_or_other_repository(self):
        for method, path in (('PUT', REPO + '/pulls/7/merge'), ('POST', REPO + '/pulls/7/reviews'),
                             ('DELETE', REPO + '/git/refs/heads/main'), ('PATCH', REPO + '/git/refs/heads/main'),
                             ('PATCH', REPO + '/pulls/7'), ('POST', '/repos/joungGo/other/pulls'),
                             ('POST', REPO + '/git/refs/x'), ('GET', REPO + '/git/commits/' + BASE + '/x'),
                             ('PUT', REPO + '/branches/main/protection'), ('POST', REPO + '/merges'),
                             ('GET', REPO + '/pulls?head=joungGo%3Amain&state=all&per_page=5')):
            with self.subTest(method=method, path=path):
                self.assertFalse(w._allowed(method, path))

    def test_payload_outside_the_prepared_change_is_rejected_before_any_request(self):
        cases = {'repo': payload(repository='joungGo/other'), 'path': payload(values_path='ai-ops/lab/Chart.yaml'),
                 'branch': payload(branch='main'), 'base': payload(base='develop'), 'sha': payload(base_sha='x'),
                 'write_flag': payload(write_enabled=True), 'unchanged': payload(after=payload()['before']),
                 'extra': {**payload(), 'merge': True}}
        for name, p in cases.items():
            with self.subTest(case=name), self.assertRaisesRegex(ValueError, 'pr_writer_payload_rejected'):
                self.writer.create(KEY, p)
        with self.assertRaisesRegex(ValueError, 'pr_writer_payload_rejected'):
            self.writer.create('b' * 64, payload())  # branch must match the plan hash
        self.assertEqual(self.gh.requests, [])

    def test_token_and_proxy_rules(self):
        self.token.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'pr_writer_token_file_rejected'):
            self.writer.create(KEY, payload())
        self.token.chmod(0o600)
        for bad in ('github_pat_' + 'x' * 30, 'ghp_' + 'x' * 36, 'ghs_short'):
            self.token.write_text(bad)
            with self.subTest(token=bad[:8]), self.assertRaisesRegex(ValueError, 'pr_writer_token_rejected'):
                self.writer.create(KEY, payload())
        for bad in ('https://p:8080', 'http://p', 'http://u@p:8080'):
            with self.subTest(proxy=bad), self.assertRaisesRegex(ValueError, 'pr_writer_proxy_rejected'):
                GitHubAppPRWriter(self.token, proxy=bad)
        with self.assertRaisesRegex(ValueError, 'pr_writer_token_path_rejected'):
            GitHubAppPRWriter('relative/token', proxy=PROXY)
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://evil.test:3128'}):
            handlers = [h for h in GitHubAppPRWriter(self.token, proxy=PROXY).opener.handlers
                        if isinstance(h, ProxyHandler)]
            self.assertEqual([h.proxies for h in handlers], [{'https': PROXY}])

    def test_http_errors_become_fixed_codes(self):
        writer = GitHubAppPRWriter(self.token, proxy=PROXY, opener=FakeGitHub(fail='/git/refs'))
        with self.assertRaises(ValueError) as caught:
            writer.create(KEY, payload())
        self.assertEqual(str(caught.exception), 'pr_writer_http_422')
        self.assertNotIn('secret', str(caught.exception))

    def test_lookup(self):
        for pulls, expected in (([], None), ([{'number': 9}], 9)):
            writer = GitHubAppPRWriter(self.token, proxy=PROXY, opener=FakeGitHub(pulls=pulls))
            self.assertEqual(writer.lookup(KEY), expected)
        writer = GitHubAppPRWriter(self.token, proxy=PROXY, opener=FakeGitHub(pulls=[{'number': 1}, {'number': 2}]))
        with self.assertRaisesRegex(ValueError, 'pr_writer_lookup_ambiguous'):
            writer.lookup(KEY)
        gh = FakeGitHub()
        GitHubAppPRWriter(self.token, proxy=PROXY, opener=gh).lookup(KEY)
        self.assertEqual(gh.requests[0][1], 'https://api.github.com' + REPO + '/pulls?head=joungGo%3Arecovery%2Fwf07-'
                         + KEY + '&state=all&per_page=5')


class RelayPublishTest(wiring.AdvanceFixture, unittest.TestCase):
    """Real WF07 fixture: prepare + publish in one relay step with the production writer type."""
    def production_writer(self):
        token = self.f.queue.root / 'pr-writer-token'
        token.write_text(TOKEN)
        token.chmod(0o600)
        writer = GitHubAppPRWriter(token, proxy=PROXY, opener=FakeGitHub())
        fixture = self.t.writer   # creates the local Git branch/commit and the PR the reader sees
        return writer, patch.object(GitHubAppPRWriter, 'create', lambda self, key, p: fixture.create(key, p))

    def test_relay_prepares_then_publishes_on_the_next_tick(self):
        writer, created = self.production_writer()
        service = wiring.RecoveryApprovalService(self.adapter, wiring.config(self.t.policy), pr_bridge=self.bridge,
                                                 observe=self.t.observation, pr_writer=writer)
        self.adapter.recovery_approvals = service
        self.approved(service)
        with created, patch('socket.socket.connect', side_effect=AssertionError('network')):
            self.assertEqual(service.advance_once()['state'], 'proposal_ready')
            prepared = service.advance_once()   # one tick: prepare only (relay budget)
            step = service.advance_once()       # next tick: publish while still fresh
            self.assertEqual(service.advance_once()['action'], 'none')  # never published twice
        self.assertEqual((prepared['action'], prepared['state']), ('prepare', 'prepared'))
        self.assertEqual((step['action'], step['state'], step['execution_allowed']), ('publish', 'awaiting_review', False))
        link = service.pr_bridge.show(self.key, actor=self.actor)
        self.assertEqual((link['state'], link['pull_number']), ('awaiting_review', 123))
        texts = self.deliver_all(service)
        [opened] = [t for t in texts if 'C03 writer 앱으로 복구 PR을 열었습니다' in t]
        self.assertIn('(PR #123)', opened)
        self.assertIn('자동 병합·배포는 하지 않습니다', opened)
        self.assertNotIn('https://', opened)

    def test_without_writer_the_relay_only_prepares(self):
        service = self.service()
        self.approved(service)
        service.advance_once()
        self.assertEqual(service.advance_once()['action'], 'prepare')
        self.assertEqual(self.t.writer.calls, 0)

    def test_bridge_accepts_only_the_exact_production_class(self):
        class Subclass(GitHubAppPRWriter):
            pass
        service = self.service()
        self.approved(service)
        service.advance_once()
        service.advance_once()  # prepared
        token = self.f.queue.root / 'pr-writer-token'
        token.write_text(TOKEN)
        token.chmod(0o600)
        for writer in (Subclass(token, proxy=PROXY), type('Fake', (), {'mode': 'github_app'})()):
            with self.subTest(writer=type(writer).__name__), self.assertRaisesRegex(ValueError, 'live_pr_writer_not_enabled'):
                service.pr_bridge.publish_local(self.key, actor=self.actor, writer=writer)

    def test_writer_requires_a_bridge(self):
        writer, _ = self.production_writer()
        with self.assertRaisesRegex(ValueError, 'recovery_pr_writer_without_bridge'):
            wiring.RecoveryApprovalService(self.adapter, wiring.config(self.t.policy), pr_writer=writer)


class ConfigTest(unittest.TestCase):
    def test_publish_token_needs_absolute_path_and_the_proxy(self):
        from ai_ops.recovery_proof_live import live_pr_writer
        from ai_ops.recovery_sources import PrepareConfig
        base = {'principals': {'UUSER01': 1}, 'author_id': 2, 'drafts_dir': '/data/d', 'github_token_file': '/run/g',
                'normal_binding_file': '/etc/b', 'aws_role_name': 'r', 'egress_proxy': PROXY}
        self.assertIsNone(PrepareConfig.model_validate(base).publish_token_file)
        ok = PrepareConfig.model_validate({**base, 'publish_token_file': '/run/ai-pr-writer/token'})
        writer = live_pr_writer(type('C', (), {'prepare': ok})())
        self.assertIs(type(writer), GitHubAppPRWriter)
        self.assertIsNone(live_pr_writer(type('C', (), {'prepare': PrepareConfig.model_validate(base)})()))
        for bad in ({'publish_token_file': 'run/token'}, {'publish_token_file': '/run/t', 'egress_proxy': None}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                PrepareConfig.model_validate({**base, **bad})


class CallGuardTest(WriterTest):
    def test_call_refuses_unlisted_requests_before_sending(self):
        for method, path in (('PUT', REPO + '/pulls/7/merge'), ('POST', REPO + '/pulls/7/reviews'),
                             ('DELETE', REPO + '/git/refs/heads/main')):
            with self.subTest(method=method), self.assertRaisesRegex(ValueError, 'pr_writer_request_rejected'):
                self.writer._call(method, path, {})
        self.assertEqual(self.gh.requests, [])


class RelayStartupTest(unittest.TestCase):
    """The relay entry point hands the configured writer to the approval service."""
    def setUp(self):
        self.w = wiring.ServerWiringTest()
        self.w.setUp()
        self.addCleanup(self.w.doCleanups)

    def test_relay_passes_bridge_and_writer(self):
        from contextlib import redirect_stdout
        from ai_ops import slack_server
        self.w.write(observe={'api_url': 'https://kubernetes.default.svc', 'token_file': '/run/recovery-reader/token',
                              'ca_file': '/run/recovery-reader/ca.crt', 'argo_namespace': 'argocd',
                              'argo_application': 'lab-target'},
                     prepare={'principals': {'UALICE01': 1}, 'author_id': 2, 'drafts_dir': '/data/recovery-drafts',
                              'github_token_file': '/run/ai-github/token', 'normal_binding_file': '/etc/ai-ops/b.json',
                              'aws_role_name': 'r', 'egress_proxy': PROXY,
                              'publish_token_file': '/run/ai-pr-writer/token'})
        with (patch.object(slack_server, 'live_pr_bridge', return_value='bridge-factory') as bridge,
              patch.object(slack_server, 'live_pr_writer', return_value='writer') as writer,
              patch.object(slack_server, 'RecoveryApprovalService') as service,
              patch.object(slack_server, 'relay_loop'), patch.object(slack_server, 'relay_retention'),
              redirect_stdout(io.StringIO())):
            self.assertEqual(slack_server.main(self.w.s.args('relay', '--enable-web')), 0)
        self.assertEqual(writer.call_args.args[0].prepare.publish_token_file, '/run/ai-pr-writer/token')
        self.assertEqual((service.call_args.kwargs['pr_bridge'], service.call_args.kwargs['pr_writer']),
                         ('bridge-factory', 'writer'))
        bridge.assert_called_once()


class BudgetTest(WriterTest):
    def test_six_calls_share_one_time_budget(self):
        now = [0.0]

        def clock():
            now[0] += 4.0   # every budget check and the start each take 4s
            return now[0]
        writer = GitHubAppPRWriter(self.token, proxy=PROXY, opener=self.gh, clock=clock)
        with self.assertRaisesRegex(ValueError, 'pr_writer_budget_exceeded'):
            writer.create(KEY, payload())
        self.assertLess(len(self.gh.requests), 6)
        self.assertEqual(self.gh.timeouts, [5, 2.0])   # min(per-call 5s, remaining budget)
        self.assertIsNone(writer.deadline)   # reset after the attempt

    def test_normal_publication_fits_and_timeouts_are_bounded(self):
        self.assertEqual(self.writer.create(KEY, payload()), 7)
        self.assertEqual(len(self.gh.timeouts), 6)
        self.assertTrue(all(0 < x <= 5 for x in self.gh.timeouts))
