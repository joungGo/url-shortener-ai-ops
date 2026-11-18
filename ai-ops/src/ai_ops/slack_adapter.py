"""서명·권한을 확인한 Slack 요청의 영속 접수와 명시적으로 선택한 전송기의 회신 어댑터.

기본은 fixture이며 실제 Slack 전송은 별도 opt-in transport만 허용한다. 접수 ACK와 큐 전달,
조사 결과, 회신 성공, 사람 수신 확인을 각각 구분한다.
"""

from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from pydantic import Field, StrictStr, model_validator

from ai_ops.b_contracts import _json_object
from ai_ops.contracts import StrictModel, sha256_json
from ai_ops.evidence import sanitize_question
from ai_ops.incidents import _private_directory
from ai_ops.job_queue import QueueBusy, QueueFull, RETENTION_SECONDS


BODY_LIMIT = 65536
ID = re.compile(r'[A-Z][A-Z0-9]{2,31}\Z')
STAMP = re.compile(r'[0-9]{1,12}\.[0-9]{6}\Z')
REQUEST_ID = re.compile(r'slack-[a-f0-9]{64}\Z')


class SlackGrant(StrictModel):
    user_id: StrictStr = Field(pattern=r'^[UW][A-Z0-9]{2,31}$')
    channel_id: StrictStr = Field(pattern=r'^[CG][A-Z0-9]{2,31}$')
    targets: list[StrictStr] = Field(min_length=1, max_length=20)


class SlackPolicy(StrictModel):
    team_id: StrictStr = Field(pattern=r'^T[A-Z0-9]{2,31}$')
    app_id: StrictStr = Field(pattern=r'^A[A-Z0-9]{2,31}$')
    bot_user_id: StrictStr = Field(pattern=r'^[UW][A-Z0-9]{2,31}$')
    grants: list[SlackGrant] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def unique(self):
        keys = [(g.user_id, g.channel_id) for g in self.grants]
        if len(set(keys)) != len(keys) or any(len(set(g.targets)) != len(g.targets) for g in self.grants):
            raise ValueError('slack_policy_duplicate')
        return self

    def allows(self, user, channel, target):
        return any(g.user_id == user and g.channel_id == channel and target in g.targets for g in self.grants)


class SlackNotSent(OSError):
    """전송을 시도하지 않았음이 확실한 fixture transport 실패만 뜻한다."""


class SlackAdmissionBusy(OSError):
    """일시적인 접수 상한이다. 영구 인가 거절과 달리 HTTP 재시도를 허용한다."""


class SlackAdapter:
    def __init__(self, tasks, policy, signing_secret, *, monotonic=time.monotonic, transport_mode="local_fixture", event_sources=None):
        if not isinstance(signing_secret, bytes) or not 16 <= len(signing_secret) <= 128:
            raise ValueError('slack_signing_secret_rejected')
        self.tasks, self.policy = tasks, SlackPolicy.model_validate(policy).model_copy(deep=True)
        aliases = {b.alias for b in tasks.settings.bindings.bindings}
        if any(not set(g.targets) <= aliases for g in self.policy.grants):
            raise ValueError('slack_policy_target_rejected')
        if transport_mode not in ('local_fixture', 'slack_web_api'):
            raise ValueError('slack_transport_mode_rejected')
        self.transport_mode = transport_mode
        self._secret = signing_secret
        self.monotonic = monotonic
        self.policy_hash = sha256_json(self.policy.model_dump())
        self.config_hash = tasks.settings.config_hash
        self.authorization_hash = tasks.queue.engine.request_fingerprint({'policy':self.policy_hash,
            'config':self.config_hash,'signing_key_hash':hashlib.sha256(signing_secret).hexdigest()})
        if transport_mode == 'slack_web_api':
            self.authorization_hash = sha256_json({'mode':transport_mode,'authorization':self.authorization_hash})
        self.events = None
        if event_sources:
            from ai_ops.event_store import EventStore
            self.events = EventStore(tasks.queue, tasks.settings, sources=event_sources)
            self.authorization_hash = sha256_json({'authorization':self.authorization_hash,
                                                  'event_sources':self.events.sources})
        self.event_owner = 'slack-web' if transport_mode == 'slack_web_api' else 'slack-local'
        self.directory = tasks.queue.root / ('slack-web' if transport_mode == 'slack_web_api' else 'slack')
        _private_directory(self.directory)
        self.path = self.directory / 'inbox.sqlite3'
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600); os.close(fd)
        with self._lock():
            with self._db() as db:
                db.execute('CREATE TABLE IF NOT EXISTS slack_installation ('
                    'singleton INTEGER PRIMARY KEY CHECK(singleton=1),identity TEXT NOT NULL,active_hash TEXT NOT NULL)')
                identity=sha256_json({'team':self.policy.team_id,'app':self.policy.app_id,'bot':self.policy.bot_user_id})
                prior=db.execute('SELECT identity,active_hash FROM slack_installation WHERE singleton=1').fetchone()
                if prior is not None and prior['identity']!=identity:raise ValueError('slack_installation_mismatch')
                # Fail closed across the two SQLite commits. Retire the previous
                # authority durably BEFORE exposing a changed Slack policy; a crash
                # leaves old jobs fenced. Hold the processing lock through activation
                # so a concurrent constructor cannot overwrite a newer authority.
                if prior is None or prior['active_hash']!=self.authorization_hash or self.events is None:
                    with tasks.queue.transaction() as queue_db:
                        if queue_db.execute("SELECT 1 FROM sqlite_master WHERE name='event_request_authorities'").fetchone():
                            queue_db.execute('DELETE FROM event_request_authorities WHERE owner=?',(self.event_owner,))
                db.execute('INSERT INTO slack_installation VALUES (1,?,?) ON CONFLICT(singleton) DO UPDATE '
                           'SET active_hash=excluded.active_hash',(identity,self.authorization_hash))
                db.execute('CREATE TABLE IF NOT EXISTS slack_requests ('
                    'request_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,user_id TEXT NOT NULL,'
                    'channel_id TEXT NOT NULL,thread_ts TEXT NOT NULL,target TEXT NOT NULL,question TEXT,'
                    'policy_hash TEXT NOT NULL,config_hash TEXT NOT NULL,state TEXT NOT NULL,'
                    'job_id TEXT UNIQUE,created_at REAL NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS slack_replies ('
                    'request_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,state TEXT NOT NULL,'
                    'attempts INTEGER NOT NULL,retry_at REAL NOT NULL,message_ts TEXT,updated_at REAL NOT NULL)')

                db.execute('CREATE TABLE IF NOT EXISTS slack_request_formats (request_id TEXT PRIMARY KEY,version INTEGER NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS slack_event_requests ('
                    'request_id TEXT PRIMARY KEY,command TEXT NOT NULL,event_id TEXT NOT NULL,'
                    'context TEXT,reply_text TEXT)')
            if self.events is not None:
                self.events.set_request_authority(self.event_owner,self.authorization_hash)

    def _event_bridge(self):
        from ai_ops.slack_events import SlackEventBridge
        return SlackEventBridge(self)

    @contextmanager
    def _db(self):
        _private_directory(self.directory)
        for suffix in ('', '-wal', '-shm', '-journal'):
            p = Path(str(self.path) + suffix)
            try: info = p.lstat()
            except FileNotFoundError: continue
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise ValueError('slack_storage_rejected')
        db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=.25, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            if db.execute('PRAGMA journal_mode=WAL').fetchone()[0]!='wal':raise ValueError('slack_storage_wal_required')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA secure_delete=ON'); db.execute('BEGIN IMMEDIATE')
            yield db
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction: db.execute('ROLLBACK')
            raise
        finally: db.close()

    @contextmanager
    def _lock(self):
        _private_directory(self.directory)
        fd = os.open(self.directory / 'processing.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            s = os.fstat(fd)
            if (not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_uid != os.getuid()
                    or stat.S_IMODE(s.st_mode) != 0o600): raise ValueError('slack_lock_rejected')
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise ValueError('slack_processing_busy') from None
            yield
        finally: os.close(fd)

    def _verify(self, headers, body):
        if not isinstance(body, bytes) or not 0 < len(body) <= BODY_LIMIT:
            raise ValueError('slack_body_rejected')
        pairs = list(headers.items()) if hasattr(headers, 'items') else list(headers)
        values = {}
        for name, value in pairs:
            name = name.lower()
            if name in ('x-slack-signature', 'x-slack-request-timestamp', 'content-type'):
                if name in values or not isinstance(value, str): raise ValueError('slack_headers_rejected')
                values[name] = value
        timestamp = values.get('x-slack-request-timestamp', '')
        signature = values.get('x-slack-signature', '')
        if (values.get('content-type', '').split(';')[0].strip().lower() != 'application/json'
                or not re.fullmatch(r'[0-9]{1,12}', timestamp)
                or abs(self.tasks.queue.now() - int(timestamp)) > 300
                or not re.fullmatch(r'v0=[a-f0-9]{64}', signature)):
            raise ValueError('slack_signature_rejected')
        expected = 'v0=' + hmac.new(self._secret, b'v0:' + timestamp.encode() + b':' + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature): raise ValueError('slack_signature_rejected')

    def _active(self):
        with self._db() as db:
            row=db.execute('SELECT active_hash FROM slack_installation WHERE singleton=1').fetchone()
        return row is not None and hmac.compare_digest(row['active_hash'],self.authorization_hash)

    def receive(self, headers, body):
        """서명·인가 후 별도 inbox까지만 영속 접수한다. worker/외부 API는 호출하지 않는다."""
        started = self.monotonic()
        self._verify(headers, body)
        if (sha256_json(self.policy.model_dump())!=self.policy_hash or self.tasks.settings.config_hash!=self.config_hash
                or not self._active()):
            raise ValueError('slack_policy_or_config_changed')
        value = _json_object(body)
        if value.get('type') == 'url_verification':
            challenge = value.get('challenge')
            if not isinstance(challenge, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,1024}', challenge):
                raise ValueError('slack_challenge_rejected')
            return {'challenge': challenge}
        event = value.get('event')
        if (value.get('type') != 'event_callback' or value.get('team_id') != self.policy.team_id
                or value.get('api_app_id') != self.policy.app_id or not isinstance(event, dict)
                or 'response_url' in value or (isinstance(event,dict) and 'response_url' in event)
                or value.get('is_ext_shared_channel', False) is not False
                or value.get('context_team_id', self.policy.team_id) != self.policy.team_id
                or event.get('type') != 'app_mention' or 'bot_id' in event or 'subtype' in event
                or event.get('hidden', False) is not False or event.get('user') == self.policy.bot_user_id
                or event.get('user_team', self.policy.team_id) != self.policy.team_id
                or event.get('team', self.policy.team_id) != self.policy.team_id):
            raise ValueError('slack_event_rejected')
        event_id, user, channel = value.get('event_id'), event.get('user'), event.get('channel')
        text, stamp = event.get('text'), event.get('ts')
        thread = event.get('thread_ts', stamp)
        if (not isinstance(event_id, str) or not re.fullmatch(r'Ev[A-Za-z0-9]{3,48}', event_id)
                or not isinstance(user, str) or not ID.fullmatch(user)
                or not isinstance(channel, str) or not ID.fullmatch(channel)
                or not isinstance(stamp, str) or not STAMP.fullmatch(stamp)
                or not isinstance(thread, str) or not STAMP.fullmatch(thread)
                or not isinstance(text, str) or len(text.encode()) > 16384):
            raise ValueError('slack_event_fields_rejected')
        command = re.fullmatch(re.escape('<@' + self.policy.bot_user_id + '>') + r'\s+(investigate|investigate-event|status-event)\s+(\S+)(?:\s+(.+))?', text, re.DOTALL)
        if command is None: raise ValueError('slack_command_rejected')
        action, reference, question = command.groups()
        event_command = None
        if action == 'investigate':
            target = reference
            if not self.policy.allows(user, channel, target): raise ValueError('slack_principal_target_rejected')
        else:
            event_command = self._event_bridge().parse(action, reference, user, channel, thread)
            target = ''  # resolve only in relay; ACK never claims a queued investigation
        question, _ = sanitize_question(question or '현재 상태를 확인해줘')
        identity = {'team':self.policy.team_id,'app':self.policy.app_id,'event':event_id}
        if self.transport_mode == 'slack_web_api':
            identity['transport'] = self.transport_mode
        request_id = 'slack-' + sha256_json(identity)
        fingerprint = self.tasks.queue.engine.request_fingerprint({'body_sha256':hashlib.sha256(body).hexdigest(),
                         'policy':self.policy_hash,'config':self.config_hash})
        now = self.tasks.queue.now()
        with self._db() as db:
            active=db.execute('SELECT active_hash FROM slack_installation WHERE singleton=1').fetchone()
            if active is None or not hmac.compare_digest(active['active_hash'],self.authorization_hash):
                raise ValueError('slack_instance_inactive')
            row = db.execute('SELECT * FROM slack_requests WHERE request_id=?', (request_id,)).fetchone()
            if row:
                if row['fingerprint'] != fingerprint or now >= row['created_at'] + RETENTION_SECONDS or row['state']=='expired':
                    raise ValueError('slack_event_conflict_or_expired')
            else:
                count = db.execute('SELECT count(*) FROM slack_requests WHERE user_id=? AND created_at>?', (user,now-60)).fetchone()[0]
                pending = db.execute('SELECT count(*) FROM slack_requests WHERE state="accepted"').fetchone()[0]
                if count >= 5 or pending >= 100: raise SlackAdmissionBusy('slack_admission_limit')
                db.execute('INSERT INTO slack_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                           (request_id,fingerprint,user,channel,thread,target,question,self.policy_hash,self.config_hash,'accepted',None,now))
                db.execute('INSERT INTO slack_request_formats VALUES (?,1)',(request_id,))
                if event_command is not None:
                    db.execute('INSERT INTO slack_event_requests VALUES (?,?,?,NULL,NULL)',
                               (request_id,*event_command))
        # 늦은 ACK는 성공으로 위장하지 않는다. durable event ID로 재전송을 조정한다.
        if self.monotonic()-started >= 2.5: raise TimeoutError('slack_ack_deadline')
        return {'accepted':True,'request_id':request_id,'duplicate':row is not None,
                'queue_accepted':bool(row and row['job_id']),
                'intake_state':row['state'] if row else 'accepted','execution_allowed':False}

    def _row(self, request_id):
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id): raise ValueError('slack_request_id_rejected')
        with self._db() as db:
            row = db.execute('SELECT * FROM slack_requests WHERE request_id=?', (request_id,)).fetchone()
        if row is None: raise ValueError('slack_request_missing')
        return dict(row)

    def _authorized(self, row):
        return (row['state'] != 'expired' and self.tasks.queue.now() < row['created_at'] + RETENTION_SECONDS
                and row['policy_hash']==self.policy_hash and row['config_hash']==self.config_hash
                and sha256_json(self.policy.model_dump())==self.policy_hash
                and self.tasks.settings.config_hash==self.config_hash
                and self._event_bridge().authorized(row) and self._active())

    def drain_one(self):
        """접수 relay 역할만 수행한다. 실제 조사는 기존 Worker가 별도로 담당한다."""
        with self._lock():
            if not self._active():raise ValueError('slack_instance_inactive')
            with self._db() as db:
                row=db.execute('SELECT * FROM slack_requests WHERE state="accepted" ORDER BY created_at,request_id LIMIT 1').fetchone()
            if row is None:return {'state':'idle'}
            row=dict(row)
            if not self._authorized(row):
                with self._db() as db:db.execute('UPDATE slack_requests SET state="failed",question=NULL WHERE request_id=?',(row['request_id'],))
                return {'state':'rejected','execution_allowed':False}
            try:
                if self._event_bridge().entry(row['request_id']) is not None:
                    return self._event_bridge().drain(row)
                receipt=self.tasks.submit({'request_id':row['request_id'],
                    'conversation_id':'slack:'+sha256_json({'team':self.policy.team_id,'channel':row['channel_id'],'thread':row['thread_ts']}),
                    'target':row['target'],'question':row['question']})
            except (QueueBusy,QueueFull):
                raise
            except ValueError:
                with self._db() as db:db.execute('UPDATE slack_requests SET state="failed",question=NULL WHERE request_id=?',(row['request_id'],))
                return {'state':'rejected','execution_allowed':False}
            with self._db() as db:
                db.execute('UPDATE slack_requests SET state="queued",job_id=?,question=NULL WHERE request_id=?',
                           (receipt['task_id'],row['request_id']))
            return {'request_id':row['request_id'],'state':'queued','task_id':receipt['task_id'],'execution_allowed':False}

    def _task_result(self, row):
        if self._event_bridge().entry(row['request_id']) is None:
            return self.tasks.result(row['job_id'])
        return self.tasks.result(row['job_id'],include_observation=True)

    def _payload(self, row):
        if row['state']=='ready' and self._authorized(row):
            return self._event_bridge().status_payload(row)
        if row['state']!='queued' or not row['job_id'] or not self._authorized(row): raise ValueError('slack_reply_scope_rejected')
        result=self._task_result(row)
        if result['state'] in ('pending','expired'):raise ValueError('slack_result_not_ready')
        from ai_ops.result_explanation import result_text
        text=self._event_bridge().prefix(row, result)+result_text(row['target'], result)
        if result.get('runbook_context'):
            context=result['runbook_context']
            text+=f"런북 참고 상태: {context['status']} (현재 소스/실환경 미검증)\n"
            for citation in context['citations']:
                text+=f"참고: {citation['document_id']} v{citation['document_version']} {citation['section']['id']} SHA256 {citation['content_sha256']}\n"
        text+='\n추적 정보\n'
        text+=f"작업: {row['job_id']}\n요청: {row['request_id']}\n"
        if result.get('reference'):
            text+=f"근거: {result['reference']['incident_id']} / {result['reference']['run_id']}\n"
        text+='저장된 증거 기준이며 변경·복구를 실행하지 않았습니다. 사람 승인 결과가 아닙니다.'
        if not self._authorized(row):raise ValueError('slack_reply_scope_changed')
        if result.get('runbook_context') and self._task_result(row) != result:
            raise ValueError('slack_runbook_scope_changed')
        return {'channel':row['channel_id'],'thread_ts':row['thread_ts'],'text':text,
                'mrkdwn':False,'parse':'none','unfurl_links':False,'unfurl_media':False,'reply_broadcast':False}

    def _transport(self, transport):
        if self.transport_mode == 'slack_web_api':
            from ai_ops.slack_web import SlackWebTransport
            if not isinstance(transport, SlackWebTransport) or not transport.verified_for(self.policy):
                raise ValueError('slack_web_identity_required')
        if (transport is None or getattr(transport,'mode',None)!=self.transport_mode
                or getattr(transport,'team_id',None)!=self.policy.team_id
                or getattr(transport,'app_id',None)!=self.policy.app_id
                or getattr(transport,'bot_user_id',None)!=self.policy.bot_user_id):
            raise ValueError('slack_live_transport_not_enabled')

    def _reply_status(self, request_id):
        with self._db() as db:
            row=db.execute('SELECT state,attempts,retry_at,message_ts FROM slack_replies WHERE request_id=?',(request_id,)).fetchone()
        return {'request_id':request_id,'transport':('slack_web_api' if self.transport_mode == 'slack_web_api' else 'local_slack_fixture'),**(dict(row) if row else {'state':'not_prepared'}),
                'human_read_confirmed':False,'execution_allowed':False}

    def _finish(self, request_id, state, *, retry_at=0, message_ts=None):
        with self._db() as db:
            prior=db.execute('SELECT message_ts FROM slack_replies WHERE request_id=?',(request_id,)).fetchone()
            if prior is None:raise ValueError('slack_reply_not_prepared')
            if prior['message_ts'] is not None:
                if message_ts is not None and message_ts!=prior['message_ts']:raise ValueError('slack_reply_message_changed')
                # 조회 실패가 이전에 확인한 메시지의 결합을 지우지 않도록 한다.
                message_ts=prior['message_ts']
            db.execute('UPDATE slack_replies SET state=?,retry_at=?,message_ts=?,updated_at=? WHERE request_id=?',
                       (state,retry_at,message_ts,self.tasks.queue.now(),request_id))
        return self._reply_status(request_id)

    @staticmethod
    def _matches(body, payload, *, bot=None):
        if not isinstance(body,dict) or body.get('ok') is not True or body.get('channel')!=payload['channel']:return False
        message=body.get('message'); stamp=body.get('ts')
        return (isinstance(stamp,str) and bool(STAMP.fullmatch(stamp)) and isinstance(message,dict)
                and message.get('thread_ts')==payload['thread_ts'] and message.get('text')==payload['text']
                and message.get('ts')==stamp and (bot is None or message.get('user')==bot))

    def reply(self, request_id, transport=None):
        self._transport(transport)
        with self._lock():
            row=self._row(request_id);payload=self._payload(row);digest=sha256_json(payload);now=self.tasks.queue.now()
            with self._db() as db:
                prior=db.execute('SELECT * FROM slack_replies WHERE request_id=?',(request_id,)).fetchone()
                if prior:
                    if prior['payload_hash']!=digest:raise ValueError('slack_reply_changed')
                    if prior['state']!='pending' or now<prior['retry_at']:return self._reply_status_values(request_id,dict(prior))
                    attempts=prior['attempts']
                else:attempts=0
                if attempts>=3:raise ValueError('slack_reply_retry_exhausted')
                db.execute('INSERT INTO slack_replies VALUES (?,?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE '
                           'SET state="sending",attempts=excluded.attempts,updated_at=excluded.updated_at',
                           (request_id,digest,'sending',attempts+1,0,None,now))
            try:
                if not self._authorized(self._row(request_id)):raise ValueError('slack_reply_scope_changed')
                if self.tasks.settings.runbooks is not None and self._payload(self._row(request_id)) != payload:
                    raise ValueError('slack_runbook_scope_changed')
                response=transport.post_message(dict(payload))
            except SlackNotSent:
                return self._finish(request_id,'pending' if attempts+1<3 else 'failed',retry_at=self.tasks.queue.now()+5)
            except Exception:
                return self._finish(request_id,'needs_review')
            if not isinstance(response,dict):return self._finish(request_id,'needs_review')
            status=response.get('status');body=response.get('body')
            if type(status) is int and status==429:
                delay=response.get('retry_after')
                if type(delay) is not int or not 1<=delay<=300:return self._finish(request_id,'needs_review')
                return self._finish(request_id,'pending' if attempts+1<3 else 'failed',retry_at=self.tasks.queue.now()+delay)
            if type(status) is int and 400<=status<500:return self._finish(request_id,'failed')
            if status==200 and isinstance(body,dict) and body.get('ok') is False:
                return self._finish(request_id,'failed')
            if type(status) is int and status==200 and self._matches(body,payload):
                return self._finish(request_id,'delivered',message_ts=body['ts'])
            return self._finish(request_id,'needs_review')

    def _reply_status_values(self, request_id, row):
        return {'request_id':request_id,'state':row['state'],'attempts':row['attempts'],
                'message_ts':row['message_ts'],'retry_at':row['retry_at'],'transport':('slack_web_api' if self.transport_mode == 'slack_web_api' else 'local_slack_fixture'),
                'human_read_confirmed':False,'execution_allowed':False}

    def reconcile_reply(self, request_id, transport=None):
        self._transport(transport)
        with self._lock():
            row=self._row(request_id);payload=self._payload(row)
            with self._db() as db:
                prior=db.execute('SELECT * FROM slack_replies WHERE request_id=?',(request_id,)).fetchone()
            if prior is None or prior['payload_hash']!=sha256_json(payload):raise ValueError('slack_reply_not_prepared')
            if prior['state'] not in ('sending','needs_review','delivered'):return self._reply_status(request_id)
            try:found=transport.find_message(dict(payload))
            except Exception:return self._finish(request_id,'needs_review')
            valid=self._matches(found,payload,bot=self.policy.bot_user_id)
            if valid and prior['message_ts'] and prior['message_ts']!=found['ts']:valid=False
            if valid:
                try:
                    current=self._row(request_id)
                    valid=(self._authorized(current) and (self.tasks.settings.runbooks is None
                           or self._payload(current)==payload))
                except (ValueError,OSError,sqlite3.Error):
                    valid=False
            return self._finish(request_id,'delivered' if valid else 'needs_review',message_ts=found['ts'] if valid else None)

    def prune(self):
        with self._lock():
            if not self._active():raise ValueError('slack_instance_inactive')
            with self._db() as db:
                rows=db.execute('SELECT request_id FROM slack_requests WHERE created_at<=? AND state!="expired"',
                                (self.tasks.queue.now()-RETENTION_SECONDS,)).fetchall()
                for row in rows:
                    db.execute('UPDATE slack_requests SET state="expired",question=NULL,user_id="",channel_id="",thread_ts="",target="" WHERE request_id=?',(row['request_id'],))
                    db.execute('UPDATE slack_replies SET state="expired",message_ts=NULL WHERE request_id=?',(row['request_id'],))
                    db.execute('UPDATE slack_event_requests SET event_id="",context=NULL,reply_text=NULL WHERE request_id=?',(row['request_id'],))
            from ai_ops.event_store import expire_events
            # Maintenance is independent of whether new event commands are enabled.
            expire_events(self.tasks.queue,apply=True)
            with self.tasks.queue.transaction() as db:
                has_events=db.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone()
                expired={r[0] for r in db.execute('SELECT event_id FROM events WHERE expired=1')} if has_events else set()
            if expired:
                with self._db() as db:
                    linked=db.execute('SELECT request_id,event_id FROM slack_event_requests WHERE context IS NOT NULL').fetchall()
                    for item in linked:
                        if item['event_id'] not in expired:
                            continue
                        db.execute('UPDATE slack_requests SET state="expired",question=NULL,user_id="",channel_id="",thread_ts="",target="" WHERE request_id=?',(item['request_id'],))
                        db.execute('UPDATE slack_replies SET state="expired",message_ts=NULL WHERE request_id=?',(item['request_id'],))
                        db.execute('UPDATE slack_event_requests SET event_id="",context=NULL,reply_text=NULL WHERE request_id=?',(item['request_id'],))
                        rows.append(item)
        return {'expired_requests':len(rows)}
