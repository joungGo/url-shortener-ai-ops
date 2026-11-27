"""Operator-Mac writer App token tool (WF11 A2, 2026-11-27). Offline: fake GitHub and kubectl."""
import base64
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_ops import recovery_pr_token as t

NOW = datetime(2026, 11, 27, 3, 0, tzinfo=timezone.utc)
TOKEN = 'ghs_' + 'S' * 36


def granted(**changes):
    r = {'token': TOKEN, 'expires_at': (NOW + timedelta(hours=1)).isoformat().replace('+00:00', 'Z'),
         'permissions': {'contents': 'write', 'pull_requests': 'write', 'metadata': 'read'},
         'repository_selection': 'selected',
         'repositories': [{'id': 1452572723, 'full_name': 'joungGo/c03-execution-lab'}]}
    r.update(changes)
    return r


class Fakes:
    def __init__(self, response=None, status=201, delete_status=204, existing=b'', fail_apply=False, stored=None):
        self.calls, self.kube_calls = [], []
        self.response, self.status, self.delete_status = response or granted(), status, delete_status
        self.existing, self.fail_apply, self.stored = existing, fail_apply, stored

    def http(self, method, path, credential, body=None):
        self.calls.append((method, path, credential, body))
        return (self.status, self.response) if method == 'POST' else (self.delete_status, {})

    def kube(self, args, stdin=None, kubeconfig=None):
        self.kube_calls.append((list(args), stdin))
        if args[:1] == ['apply'] and self.fail_apply:
            raise ValueError('pr_token_kubectl_failed')
        if 'jsonpath={.data.token}' in args:
            return self.stored or b''
        if args[-2:] == ['-o', 'name']:
            return self.existing
        return b''


class TokenToolTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.key = Path(tmp.name) / 'writer.pem'
        self.key.write_text('-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n')
        self.key.chmod(0o600)
        self.sign = lambda message: b'signature'

    def issue(self, fakes):
        return t.issue(self.key, 'ai-ops', 'ai-ops-pr-writer', http=fakes.http, kube=fakes.kube, now=NOW, sign=self.sign)

    def test_issue_requests_a_narrow_token_and_stores_it_only_via_stdin(self):
        f = Fakes()
        result = self.issue(f)
        [(method, path, credential, body)] = f.calls
        self.assertEqual((method, path), ('POST', '/app/installations/180499782/access_tokens'))
        self.assertEqual(body, {'repository_ids': [1452572723], 'permissions': {'contents': 'write', 'pull_requests': 'write'}})
        header, claims, _ = credential.split('.')
        claims = json.loads(base64.urlsafe_b64decode(claims + '=='))
        self.assertEqual(claims['iss'], '5696038')
        self.assertLessEqual(claims['exp'] - claims['iat'], 600)
        [apply] = [c for c in f.kube_calls if c[0][:1] == ['apply']]
        self.assertEqual(apply[0], ['apply', '-f', '-'])
        secret = json.loads(apply[1])
        self.assertEqual((secret['metadata']['name'], secret['metadata']['namespace']), ('ai-ops-pr-writer', 'ai-ops'))
        self.assertEqual(base64.b64decode(secret['data']['token']).decode(), TOKEN)
        self.assertTrue(all(TOKEN not in ' '.join(c[0]) for c in f.kube_calls))  # never in argv
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(result['status'], 'issued')

    def test_wrong_scope_or_lifetime_is_revoked_without_writing_a_secret(self):
        cases = {'more_permissions': granted(permissions={'contents': 'write', 'pull_requests': 'write', 'workflows': 'write'}),
                 'admin': granted(permissions={'contents': 'write', 'pull_requests': 'write', 'administration': 'write'}),
                 'all_repos': granted(repository_selection='all'),
                 'other_repo': granted(repositories=[{'id': 1, 'full_name': 'joungGo/other'}]),
                 'same_name_other_id': granted(repositories=[{'id': 999, 'full_name': 'joungGo/c03-execution-lab'}]),
                 'two_repos': granted(repositories=[{'id': 1452572723, 'full_name': 'joungGo/c03-execution-lab'}] * 2),
                 'too_long': granted(expires_at=(NOW + timedelta(hours=2)).isoformat()),
                 'expired': granted(expires_at=(NOW - timedelta(minutes=1)).isoformat())}
        for name, response in cases.items():
            f = Fakes(response=response)
            with self.subTest(case=name), self.assertRaises(ValueError):
                self.issue(f)
            self.assertEqual(f.calls[-1][:3], ('DELETE', '/installation/token', TOKEN))
            self.assertFalse(any(c[0][:1] == ['apply'] for c in f.kube_calls))

    def test_existing_secret_or_failed_store_never_leaves_a_live_token(self):
        f = Fakes(existing=b'secret/ai-ops-pr-writer')
        with self.assertRaisesRegex(ValueError, 'pr_token_secret_exists'):
            self.issue(f)
        self.assertEqual(f.calls, [])  # nothing minted
        f = Fakes(fail_apply=True)
        with self.assertRaises(ValueError):
            self.issue(f)
        self.assertEqual(f.calls[-1][:3], ('DELETE', '/installation/token', TOKEN))
        f = Fakes(status=403)
        with self.assertRaisesRegex(ValueError, 'pr_token_http_403'):
            self.issue(f)

    def test_revoke_deletes_the_token_then_the_secret(self):
        f = Fakes(stored=base64.b64encode(TOKEN.encode()))
        result = t.revoke('ai-ops', 'ai-ops-pr-writer', http=f.http, kube=f.kube)
        self.assertEqual(result['status'], 'revoked')
        self.assertEqual(f.calls, [('DELETE', '/installation/token', TOKEN, None)])
        self.assertEqual(f.kube_calls[-1][0], ['-n', 'ai-ops', 'delete', 'secret', 'ai-ops-pr-writer', '--wait=true'])
        f = Fakes(stored=base64.b64encode(TOKEN.encode()), delete_status=401)
        self.assertEqual(t.revoke('ai-ops', 'ai-ops-pr-writer', http=f.http, kube=f.kube)['status'], 'already_invalid')
        self.assertEqual(t.revoke('ai-ops', 'ai-ops-pr-writer', http=Fakes().http, kube=Fakes().kube)['status'], 'absent')
        f = Fakes(stored=base64.b64encode(TOKEN.encode()), delete_status=500)
        with self.assertRaisesRegex(ValueError, 'pr_token_http_500'):
            t.revoke('ai-ops', 'ai-ops-pr-writer', http=f.http, kube=f.kube)
        self.assertFalse(any('delete' in c[0] for c in f.kube_calls))  # keep the Secret if revocation failed

    def test_inputs_and_requests_are_fixed(self):
        self.key.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'pr_token_key_rejected'):
            t.app_jwt(self.key)
        for ns, name in (('Ai-Ops', 'x'), ('ai-ops', 'Bad_Name')):
            with self.subTest(ns=ns), self.assertRaisesRegex(ValueError, 'pr_token_name_rejected'):
                t.issue(self.key, ns, name, http=Fakes().http, kube=Fakes().kube, now=NOW, sign=self.sign)
        for method, path in (('GET', '/app'), ('POST', '/repos/joungGo/c03-execution-lab/pulls'),
                             ('DELETE', '/app/installations/180499782')):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'pr_token_request_rejected'):
                t.github(method, path, 'x')

    def test_cli_prints_fixed_json_without_secrets(self):
        out = io.StringIO()
        with patch.object(t, 'issue', side_effect=ValueError(TOKEN)), redirect_stdout(out):
            self.assertEqual(t.main(['issue', '--key', str(self.key)]), 1)
        self.assertEqual(json.loads(out.getvalue()), {'status': 'pr_token_failed', 'action': 'issue'})
        out = io.StringIO()
        with patch.object(t, 'revoke', return_value={'status': 'revoked', 'secret': 'ai-ops/ai-ops-pr-writer'}), redirect_stdout(out):
            self.assertEqual(t.main(['revoke']), 0)
        self.assertNotIn(TOKEN, out.getvalue())
