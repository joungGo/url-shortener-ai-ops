"""Explicitly enabled Slack Web API transport. No retries, redirects or implicit startup.

A successful post is an API receipt, not a human read/approval. Ambiguous outcomes
stay needs_review in the existing durable adapter and must never be reposted.
"""
import http.client
import json
import os
import re
import ssl
import stat
import time
from pathlib import Path
from urllib.parse import urlencode

from ai_ops.contracts import sha256_json
from ai_ops.evidence import _json_object
from ai_ops.slack_adapter import BODY_LIMIT, STAMP, SlackAdapter, SlackPolicy

# Only errors documenting a definite rejection are terminal. Unknown Slack errors
# (including internal_error/fatal_error) may follow a successful side effect.
REJECTED = frozenset({'invalid_auth', 'not_authed', 'token_revoked', 'account_inactive',
    'missing_scope', 'channel_not_found', 'not_in_channel', 'is_archived',
    'msg_too_long', 'no_text', 'invalid_arguments', 'restricted_action', 'ekm_access_denied'})
FLAGS = {'mrkdwn':False, 'parse':'none', 'unfurl_links':False,
         'unfurl_media':False, 'reply_broadcast':False}


def read_private_secret(path, *, token=False):
    """Read a mounted private file; no environment fallback or secret in errors."""
    fd = None
    try:
        fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError('invalid')
        raw = os.read(fd, 4097)
        if len(raw) > 4096:
            raise ValueError('invalid')
        value = raw.removesuffix(b'\n')
        pattern = rb'xoxb-[A-Za-z0-9-]{16,2048}' if token else rb'[a-fA-F0-9]{32}'
        if not re.fullmatch(pattern, value):
            raise ValueError('invalid')
        return value
    except (OSError, ValueError, TypeError):
        raise ValueError('slack_private_credential_rejected') from None
    finally:
        if fd is not None:
            os.close(fd)


METHODS = frozenset({('POST','auth.test'), ('GET','bots.info'), ('POST','chat.postMessage')})


def post_payload_valid(policy, payload):
    """Shared fixed reply contract: granted channel, existing thread, bounded text, fixed flags.

    Optional ``blocks`` are only the canonical recovery approval buttons that
    re-render exactly from their own subject and the same fallback text.
    """
    base = {'channel','thread_ts','text',*FLAGS}
    if isinstance(payload,dict) and set(payload) == base | {'blocks'}:
        from ai_ops.recovery_slack_approval import blocks_valid
        if not blocks_valid(payload['blocks'], payload.get('text')):
            return False
        payload = {k: v for k, v in payload.items() if k != 'blocks'}
    return (isinstance(payload,dict) and set(payload) == base
            and payload.get('channel') in {g.channel_id for g in policy.grants}
            and isinstance(payload.get('thread_ts'),str) and bool(STAMP.fullmatch(payload['thread_ts']))
            and isinstance(payload.get('text'),str) and 1 <= len(payload['text']) <= 4000
            and all(type(payload.get(k)) is type(v) and payload[k] == v for k,v in FLAGS.items()))


class SlackWebTransport:
    mode = 'slack_web_api'

    def __init__(self, policy, token_file, *, enabled=False,
                 connection_factory=None, monotonic=time.monotonic):
        if enabled is not True:
            raise ValueError('slack_web_disabled')
        self._policy = SlackPolicy.model_validate(policy).model_copy(deep=True)
        self.team_id = self._policy.team_id
        self.app_id = self._policy.app_id
        self.bot_user_id = self._policy.bot_user_id
        self._policy_hash = sha256_json(self._policy.model_dump())
        self._token = read_private_secret(token_file, token=True).decode('ascii')
        self._connect = connection_factory or http.client.HTTPSConnection
        self._clock = monotonic
        self._verified_at = None
        self._bot = None

    def _request(self, method, name, payload=None):
        if (method, name) not in METHODS:
            raise ValueError('slack_method_rejected')
        conn = None
        try:
            context = ssl.create_default_context()
            conn = self._connect('slack.com', port=443, timeout=3.0, context=context)
            path = '/api/' + name
            body = None
            if method == 'GET':
                path += '?' + urlencode(payload or {})
            else:
                body = json.dumps(payload or {}, ensure_ascii=False, allow_nan=False).encode()
            conn.request(method, path, body=body, headers={
                'Authorization':'Bearer ' + self._token,
                'Content-Type':'application/json; charset=utf-8', 'Accept':'application/json'})
            response = conn.getresponse()
            # No automatic retries or redirects, and never return the response headers/body raw.
            if response.status == 429:
                delay = response.getheader('Retry-After', '')
                if re.fullmatch(r'[0-9]{1,3}', delay) and 1 <= int(delay) <= 300:
                    return {'status':429, 'retry_after':int(delay)}
                return {'status':503}
            if response.status != 200:
                return {'status':503}
            raw = response.read(BODY_LIMIT + 1)
            length = response.getheader('Content-Length')
            if (len(raw) > BODY_LIMIT or (length is not None and
                    (not re.fullmatch(r'[0-9]{1,8}', length) or int(length) != len(raw)))):
                return {'status':503}
            value = _json_object(raw)
            if not isinstance(value, dict):
                return {'status':503}
            return {'status':200, 'body':value}
        except Exception:
            # Exceptions may embed token/header/body. Do not retain their strings or chain.
            return {'status':503}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def verify_identity(self):
        """Explicit startup/relay check; never performed in ingress ACK handling."""
        self._verified_at = None
        self._bot = None
        started = self._clock()
        auth = self._request('POST', 'auth.test')
        body = auth.get('body', {})
        bot = body.get('bot_id')
        if (auth['status'] != 200 or body.get('ok') is not True
                or body.get('team_id') != self.team_id or body.get('user_id') != self.bot_user_id
                or not isinstance(bot, str) or not re.fullmatch(r'B[A-Z0-9]{2,31}', bot)):
            raise ValueError('slack_identity_rejected')
        info = self._request('GET', 'bots.info', {'bot':bot})
        data = info.get('body', {})
        b = data.get('bot')
        if (info['status'] != 200 or data.get('ok') is not True or not isinstance(b,dict)
                or b.get('id') != bot or b.get('app_id') != self.app_id
                or b.get('user_id') != self.bot_user_id or b.get('deleted') is not False
                or not 0 <= self._clock()-started < 300):
            raise ValueError('slack_identity_rejected')
        self._verified_at, self._bot = started, bot
        return {'identity_verified':True, 'execution_allowed':False}

    def verified_for(self, policy):
        return (self._verified_at is not None and 0 <= self._clock()-self._verified_at < 300
                and sha256_json(policy.model_dump()) == self._policy_hash
                and (self.team_id,self.app_id,self.bot_user_id) ==
                    (policy.team_id,policy.app_id,policy.bot_user_id))

    def post_message(self, payload):
        if not self.verified_for(self._policy):
            raise ValueError('slack_identity_required')
        if not post_payload_valid(self._policy, payload):
            raise ValueError('slack_payload_rejected')
        response = self._request('POST','chat.postMessage',payload)
        body = response.get('body',{})
        if response['status'] != 200:
            return response
        if body.get('ok') is False and isinstance(body.get('error'),str) and body['error'] in REJECTED:
            return {'status':200,'body':{'ok':False}}
        if not SlackAdapter._matches(body,payload):
            return {'status':503}
        message = body['message']
        # Slack bot messages may identify the author by bot_id without user.
        # Require an observed matching identity; never synthesize a missing user.
        if (not ('user' in message or 'bot_id' in message)
                or ('user' in message and message['user'] != self.bot_user_id)
                or ('bot_id' in message and message['bot_id'] != self._bot)):
            return {'status':503}
        return {'status':200,'body':{'ok':True,'channel':body['channel'],'ts':body['ts'],
            'message':{k:message[k] for k in ('user','bot_id','thread_ts','ts','text') if k in message}}}

    def find_message(self, payload):
        # Deliberately no broad history/user-token scopes or fabricated cached readback.
        raise OSError('slack_reconciliation_unavailable')
