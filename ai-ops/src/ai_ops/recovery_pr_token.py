"""Operator-Mac tool: short-lived C03 writer App installation token for WF07 PR publication.

WF11 A2 (user decision 2026-11-26). The App private key stays on the operator Mac. Before a
scenario, ``issue`` mints an installation token narrowed to ``joungGo/c03-execution-lab`` with
only ``contents: write`` and ``pull_requests: write`` and stores it in the WF10 Secret the
renderer mounts into the relay (``recovery.publish.secret``). After the scenario, ``revoke``
revokes it on GitHub and deletes the Secret.

* The key is read only by ``openssl``; the JWT and the token stay in process memory.
* The token reaches Kubernetes via ``kubectl apply -f -`` (stdin), never argv, files or output.
* A response with broader permissions, other repositories or a lifetime above one hour is
  rejected and the token is revoked immediately (no Secret written).
* Output is fixed JSON (status, expiry); tokens, keys and response bodies are never printed.

Independent of the C-04 trial code (WF11 procedure: ``c_trial_*`` is not reused).
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

APP_ID = 5696038                 # c03-execution-lab-writer (verified 2026-11-27)
INSTALLATION_ID = 180499782      # joungGo, selected repositories
REPOSITORY = 'joungGo/c03-execution-lab'
REPOSITORY_ID = 1452572723
PERMISSIONS = {'contents': 'write', 'pull_requests': 'write'}
ALLOWED_GRANTED = ({'contents': 'write', 'pull_requests': 'write'},
                   {'contents': 'write', 'pull_requests': 'write', 'metadata': 'read'})
TOKEN = re.compile(r'ghs_[A-Za-z0-9]{20,255}')
NAME = re.compile(r'[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?')
MAX_LIFETIME = timedelta(hours=1, minutes=1)


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=')


def _key(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 16384:
        raise ValueError('pr_token_key_rejected')
    return path


def app_jwt(key, *, now=None, sign=None):
    """RS256 App JWT signed by openssl (the key is never read into Python)."""
    now = int(time.time() if now is None else now)
    message = b'.'.join((_b64(b'{"alg":"RS256","typ":"JWT"}'),
                         _b64(json.dumps({'iat': now - 60, 'exp': now + 300, 'iss': str(APP_ID)},
                                         separators=(',', ':')).encode())))
    sign = sign or (lambda m: subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(_key(key))],
                                             input=m, capture_output=True, check=True).stdout)
    return (message + b'.' + _b64(sign(message))).decode()


def github(method, path, credential, body=None):
    """Fixed host; returns (status, json). Error bodies are never surfaced."""
    if (method, path) not in (('POST', f'/app/installations/{INSTALLATION_ID}/access_tokens'),
                              ('DELETE', '/installation/token')):
        raise ValueError('pr_token_request_rejected')
    connection = http.client.HTTPSConnection('api.github.com', timeout=15)
    try:
        connection.request(method, path, body=None if body is None else json.dumps(body).encode(), headers={
            'Authorization': 'Bearer ' + credential, 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'ai-ops-wf07-pr-token',
            'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read(65537)
        return response.status, (json.loads(raw) if raw and len(raw) <= 65536 and 200 <= response.status < 300 else {})
    finally:
        connection.close()


def kubectl(args, *, stdin=None, kubeconfig=None):
    command = ['kubectl'] + (['--kubeconfig', str(kubeconfig)] if kubeconfig else []) + list(args)
    result = subprocess.run(command, input=stdin, capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError('pr_token_kubectl_failed')
    return result.stdout


def _expiry(value, now):
    expires = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if expires.tzinfo is None or not now < expires <= now + MAX_LIFETIME:
        raise ValueError('pr_token_lifetime_rejected')
    return expires


def _granted(response, now):
    token = response.get('token')
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ValueError('pr_token_response_rejected')
    repositories = response.get('repositories')
    if (response.get('permissions') not in ALLOWED_GRANTED or response.get('repository_selection') != 'selected'
            or not isinstance(repositories, list) or len(repositories) != 1
            or repositories[0].get('id') != REPOSITORY_ID or repositories[0].get('full_name') != REPOSITORY):
        raise ValueError('pr_token_scope_rejected')
    return token, _expiry(response.get('expires_at'), now)


def issue(key, namespace, secret, *, kubeconfig=None, http=github, kube=kubectl, now=None, sign=None):
    if not NAME.fullmatch(namespace) or not NAME.fullmatch(secret):
        raise ValueError('pr_token_name_rejected')
    if kube(['-n', namespace, 'get', 'secret', secret, '--ignore-not-found', '-o', 'name'], kubeconfig=kubeconfig).strip():
        raise ValueError('pr_token_secret_exists')  # revoke first; never overwrite a live token
    now = now or datetime.now(timezone.utc)
    status, response = http('POST', f'/app/installations/{INSTALLATION_ID}/access_tokens',
                             app_jwt(key, sign=sign), {'repository_ids': [REPOSITORY_ID], 'permissions': PERMISSIONS})
    if status != 201:
        raise ValueError(f'pr_token_http_{status}')
    try:
        token, expires = _granted(response, now)
    except ValueError:
        if isinstance(response.get('token'), str) and TOKEN.fullmatch(response['token']):
            http('DELETE', '/installation/token', response['token'])  # never leave a wrong-scope token alive
        raise
    manifest = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
                'metadata': {'name': secret, 'namespace': namespace,
                             'labels': {'app.kubernetes.io/part-of': 'wf10', 'ai-ops.wf11/token': 'c03-writer-pr'}},
                'data': {'token': base64.b64encode(token.encode()).decode()}}
    try:
        kube(['apply', '-f', '-'], stdin=json.dumps(manifest).encode(), kubeconfig=kubeconfig)
    except ValueError:
        http('DELETE', '/installation/token', token)
        raise
    return {'status': 'issued', 'secret': f'{namespace}/{secret}', 'repository': REPOSITORY,
            'permissions': PERMISSIONS, 'expires_at': expires.isoformat()}


def revoke(namespace, secret, *, kubeconfig=None, http=github, kube=kubectl):
    if not NAME.fullmatch(namespace) or not NAME.fullmatch(secret):
        raise ValueError('pr_token_name_rejected')
    raw = kube(['-n', namespace, 'get', 'secret', secret, '--ignore-not-found', '-o', 'jsonpath={.data.token}'],
               kubeconfig=kubeconfig).strip()
    if not raw:
        return {'status': 'absent', 'secret': f'{namespace}/{secret}'}
    token = base64.b64decode(raw).decode('ascii').strip()
    if not TOKEN.fullmatch(token):
        raise ValueError('pr_token_secret_rejected')
    status, _ = http('DELETE', '/installation/token', token)
    if status not in (204, 401):  # 401: already expired or revoked
        raise ValueError(f'pr_token_http_{status}')
    kube(['-n', namespace, 'delete', 'secret', secret, '--wait=true'], kubeconfig=kubeconfig)
    return {'status': 'revoked' if status == 204 else 'already_invalid', 'secret': f'{namespace}/{secret}'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Short-lived C03 writer App token for WF07 PR publication')
    sub = parser.add_subparsers(dest='action', required=True)
    first = sub.add_parser('issue')
    first.add_argument('--key', type=Path, default=Path.home() / '.local/share/url-shortener-ai-ops/c03-app-keys/writer.pem')
    for p in (first, sub.add_parser('revoke')):
        p.add_argument('--namespace', default='ai-ops')
        p.add_argument('--secret', default='ai-ops-pr-writer')
        p.add_argument('--kubeconfig', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == 'issue':
            result = issue(args.key, args.namespace, args.secret, kubeconfig=args.kubeconfig)
        else:
            result = revoke(args.namespace, args.secret, kubeconfig=args.kubeconfig)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError, http.client.HTTPException):
        print(json.dumps({'status': 'pr_token_failed', 'action': args.action}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
