"""WF07 PR publication with the C03 writer GitHub App (WF11 follow-up A2, user decision 2026-11-26).

The relay publishes right after preparation, inside the same freshness window the
bridge enforces (``ImageRecoveryPRBridge.publish_local``). Only a short-lived
installation token (minted on the operator Mac, repository and permissions narrowed,
revoked after the scenario) is mounted; the App private key never enters the cluster.

This writer can only:

* create one blob / tree / commit / branch and open one PR in ``joungGo/c03-execution-lab``
  for the single ``ai-ops/lab/values.yaml`` change the bridge prepared;
* list PRs for the exact ``recovery/wf07-<plan hash>`` branch (``lookup``).

There is no merge, review, approval, workflow, settings or deletion call: every request
is checked against a fixed (method, path) allowlist before it is sent. Requests go only
to ``api.github.com`` through the fixed-destination egress proxy; redirects are refused.
Tokens, response bodies and error text are never logged or echoed.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import stat
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ai_ops.c_recovery import ISOLATED_REPOSITORY, VALUES_PATH

API = 'https://api.github.com'
REPO_PATH = '/repos/' + ISOLATED_REPOSITORY
PROXY = re.compile(r'http://[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?:[1-9][0-9]{1,4}\Z')
TOKEN = re.compile(r'ghs_[A-Za-z0-9]{20,255}')   # GitHub App installation tokens only
SHA = re.compile(r'[0-9a-f]{40}')
PLAN = re.compile(r'[0-9a-f]{64}')
MAX_BYTES = 1024 * 1024
TIMEOUT_SECONDS = 5
# All six requests of one publication share this budget, so a relay tick (prepare and
# publish run in separate ticks) stays inside the relay progress age.
BUDGET_SECONDS = 10
PAYLOAD_KEYS = {'repository', 'base', 'base_sha', 'branch', 'values_path', 'before', 'after', 'diff',
                'commit_message', 'title', 'description', 'write_enabled'}


def _allowed(method, path):
    """Fixed allowlist: nothing here can merge, approve, review, delete or change settings."""
    rules = (
        ('POST', re.escape(REPO_PATH + '/git/blobs')),
        ('GET', re.escape(REPO_PATH + '/git/commits/') + '[0-9a-f]{40}'),
        ('POST', re.escape(REPO_PATH + '/git/trees')),
        ('POST', re.escape(REPO_PATH + '/git/commits')),
        ('POST', re.escape(REPO_PATH + '/git/refs')),
        ('POST', re.escape(REPO_PATH + '/pulls')),
        ('GET', re.escape(REPO_PATH + '/pulls?head=joungGo%3Arecovery%2Fwf07-') + '[0-9a-f]{64}'
                + re.escape('&state=all&per_page=5')),
    )
    return any(method == m and re.fullmatch(p, path) for m, p in rules)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubAppPRWriter:
    """Production writer accepted by ``ImageRecoveryPRBridge`` (exact type check there)."""
    mode = 'github_app'

    def __init__(self, token_file, *, proxy=None, opener=None, clock=time.monotonic):
        if not str(token_file).startswith('/'):
            raise ValueError('pr_writer_token_path_rejected')
        if proxy is not None and not PROXY.fullmatch(str(proxy)):
            raise ValueError('pr_writer_proxy_rejected')
        self.path = Path(token_file)
        self.clock, self.deadline = clock, None
        # Only the configured proxy; environment proxy variables are never used.
        self.opener = opener or build_opener(ProxyHandler({'https': proxy} if proxy else {}), NoRedirect())

    def _token(self):
        info = self.path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
            raise ValueError('pr_writer_token_file_rejected')
        token = self.path.read_text(encoding='ascii').strip()
        if not TOKEN.fullmatch(token):
            raise ValueError('pr_writer_token_rejected')
        return token

    def _call(self, method, path, body=None):
        if not _allowed(method, path):
            raise ValueError('pr_writer_request_rejected')
        remaining = TIMEOUT_SECONDS if self.deadline is None else self.deadline - self.clock()
        if remaining <= 0:
            raise ValueError('pr_writer_budget_exceeded')
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        request = Request(API + path, data=data, method=method, headers={
            'Authorization': 'Bearer ' + self._token(), 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'ai-ops-wf07-pr-writer',
            'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=min(TIMEOUT_SECONDS, remaining)) as response:
                raw = response.read(MAX_BYTES + 1)
        except HTTPError as error:
            error.close()
            raise ValueError(f'pr_writer_http_{error.code}') from None
        if len(raw) > MAX_BYTES:
            raise ValueError('pr_writer_response_too_large')
        return json.loads(raw) if raw else {}

    @staticmethod
    def _check(key, payload):
        if (not isinstance(key, str) or not PLAN.fullmatch(key) or not isinstance(payload, dict)
                or set(payload) != PAYLOAD_KEYS or payload['repository'] != ISOLATED_REPOSITORY
                or payload['base'] != 'main' or not SHA.fullmatch(str(payload['base_sha']))
                or payload['branch'] != 'recovery/wf07-' + key or payload['values_path'] != VALUES_PATH
                or payload['write_enabled'] is not False
                or any(not isinstance(payload[k], str) or not payload[k]
                       for k in ('before', 'after', 'commit_message', 'title'))
                or payload['before'] == payload['after'] or not isinstance(payload['description'], dict)):
            raise ValueError('pr_writer_payload_rejected')

    @staticmethod
    def _body(payload):
        d = payload['description']
        lines = ['WF07이 승인된 복구 계획으로 준비한 격리 앱 이미지 복구 변경입니다.', '',
                 '- 변경: `' + VALUES_PATH + '`의 이미지 digest 한 줄', '- 계획: `' + str(d.get('plan_hash', '')) + '`',
                 '- 근거 연결: `' + str(d.get('link_hash', '')) + '`', '',
                 '**병합 전 정확한 head 리뷰와 필수 CI가 필요합니다. 이 PR은 자동으로 병합·배포되지 않으며, '
                 'Argo sync도 별도 승인 후 사람이 합니다.**']
        return '\n'.join(lines)

    def create(self, key, payload):
        """Branch + one-file commit + PR. Returns the PR number; the bridge re-verifies everything."""
        self._check(key, payload)
        self.deadline = self.clock() + BUDGET_SECONDS
        try:
            return self._create(payload)
        finally:
            self.deadline = None

    def _create(self, payload):
        blob = self._call('POST', REPO_PATH + '/git/blobs', {'content': payload['after'], 'encoding': 'utf-8'})
        base = self._call('GET', REPO_PATH + '/git/commits/' + payload['base_sha'])
        tree = self._call('POST', REPO_PATH + '/git/trees', {
            'base_tree': base['tree']['sha'],
            'tree': [{'path': VALUES_PATH, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']}]})
        commit = self._call('POST', REPO_PATH + '/git/commits', {
            'message': payload['commit_message'], 'tree': tree['sha'], 'parents': [payload['base_sha']]})
        if not SHA.fullmatch(str(commit.get('sha'))):
            raise ValueError('pr_writer_commit_rejected')
        self._call('POST', REPO_PATH + '/git/refs', {'ref': 'refs/heads/' + payload['branch'], 'sha': commit['sha']})
        pull = self._call('POST', REPO_PATH + '/pulls', {
            'title': payload['title'], 'head': payload['branch'], 'base': 'main', 'body': self._body(payload),
            'draft': False, 'maintainer_can_modify': False})
        number = pull.get('number')
        if type(number) is not int or number <= 0:
            raise ValueError('pr_writer_number_rejected')
        return number

    def lookup(self, key):
        """Read-only: the PR opened for this plan's branch, or None. Ambiguity is an error."""
        if not isinstance(key, str) or not PLAN.fullmatch(key):
            raise ValueError('pr_writer_payload_rejected')
        head = quote('joungGo:recovery/wf07-' + key, safe='')
        pulls = self._call('GET', REPO_PATH + '/pulls?head=' + head + '&state=all&per_page=5')
        if not isinstance(pulls, list) or len(pulls) > 1:
            raise ValueError('pr_writer_lookup_ambiguous')
        if not pulls:
            return None
        number = pulls[0].get('number')
        if type(number) is not int or number <= 0:
            raise ValueError('pr_writer_number_rejected')
        return number
