"""WF-02 local event ledger. Callers must authenticate sources/users before this API.

No HTTP endpoint, Slack sender, deployment, approval or recovery execution lives here.
The event/outbox extension shares JobQueue's private SQLite transaction and limits.
"""
from __future__ import annotations

import json
import re

from ai_ops.alerts import ALERT_NAMES, TARGET_LABELS, MAX_BODY, MAX_ALERTS, AlertInbox, _labels, alert_time
from ai_ops.contracts import Target, sha256_json
from ai_ops.evidence import sanitize_question, _json_object
from ai_ops.job_queue import QueueFull, RETENTION_SECONDS

MAX_EVENTS = 10000
MAX_REQUESTS = 10000


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise ValueError("invalid configured identifier")
    return value


def _exists(db):
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='event_meta' AND type='table'").fetchone() is not None
    if not exists and db.execute('PRAGMA user_version').fetchone()[0] == 3:
        raise ValueError('event schema missing')
    if exists:
        row = db.execute('SELECT version FROM event_meta WHERE singleton=1').fetchone()
        if row is None or row['version'] != 1:
            raise ValueError('unsupported event schema')
    return exists


def guard_jobs(db, now, *, job_id=None):
    """Fence linked work before claim/collection. Unlinked legacy/manual work is unchanged."""
    if not _exists(db):
        return
    policy = db.execute("SELECT revision FROM event_policy WHERE singleton=1").fetchone()
    sql = ("SELECT j.job_id,j.generation,e.status,e.expires_at,e.expired,r.kind,r.policy_revision "
           "FROM jobs j JOIN event_requests r ON r.job_id=j.job_id JOIN events e ON e.event_id=r.event_id "
           "WHERE j.state IN ('queued','collecting')")
    params = ()
    if job_id is None:
        sql += " AND j.state='queued'"
    if job_id is not None:
        sql += " AND j.job_id=?"
        params = (job_id,)
    for row in db.execute(sql, params).fetchall():
        reason = None
        if row['expired'] or row['expires_at'] <= now:
            reason = 'event_expired'
        elif row['kind'] == 'legacy':
            reason = 'legacy_requires_manual_request'
        elif row['kind'] == 'automatic':
            if row['status'] == 'resolved':
                reason = 'event_resolved'
            elif policy is None or row['policy_revision'] != policy['revision']:
                reason = 'event_policy_changed'
        if reason:
            db.execute("UPDATE jobs SET state='cancelled',reason=?,lease_until=NULL,updated_at=? WHERE job_id=?",
                       (reason, now, row['job_id']))
            db.execute("UPDATE attempts SET state='cancelled',reason=?,finished_at=? WHERE job_id=? AND generation=?",
                       (reason, now, row['job_id'], row['generation']))


class EventStore:
    """Internal trusted API; source registry is immutable after initialization.

    sources: {alias: {source_id, cluster_id}}. User text cannot supply this binding.
    Automatic delivery remains disabled unless a tested adapter is explicitly ready.
    """
    def __init__(self, queue, settings, *, sources):
        self.queue, self.settings = queue, settings
        if not isinstance(sources, dict) or not sources or len(sources) > 32:
            raise ValueError('invalid source registry')
        self.sources = json.loads(json.dumps(sources))
        seen = set()
        for alias, source in self.sources.items():
            _name(alias)
            if not isinstance(source, dict) or set(source) != {'source_id', 'cluster_id'}:
                raise ValueError('invalid source binding')
            key = (_name(source['source_id']), _name(source['cluster_id']))
            if key in seen:
                raise ValueError('duplicate logical source')
            seen.add(key)
        registry_hash = sha256_json(self.sources)
        with queue.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS event_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL, registry_hash TEXT NOT NULL)")
            meta = db.execute('SELECT * FROM event_meta').fetchone()
            if meta and (meta['version'] != 1 or meta['registry_hash'] != registry_hash):
                raise ValueError('event schema/source registry changed; explicit migration required')
            db.execute('INSERT OR IGNORE INTO event_meta VALUES (1,1,?)', (registry_hash,))
            db.execute("CREATE TABLE IF NOT EXISTS event_policy (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL, activated_at REAL NOT NULL, config_hash TEXT NOT NULL, rules TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO event_policy VALUES (1,0,?,?, '[]')", (queue.now(), settings.config_hash))
            db.execute("CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, identity_hash TEXT, metadata TEXT, status TEXT NOT NULL CHECK(status IN ('firing','resolved')), ends_at TEXT, received_at REAL, expires_at REAL NOT NULL, expired INTEGER NOT NULL DEFAULT 0)")
            db.execute("CREATE TABLE IF NOT EXISTS event_requests (request_key TEXT PRIMARY KEY, event_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('manual','automatic','legacy')), fingerprint TEXT, policy_revision INTEGER, payload TEXT, state TEXT NOT NULL CHECK(state IN ('pending','delivered','cancelled','expired')), reason TEXT, job_id TEXT UNIQUE, created_at REAL)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_event_auto ON event_requests(event_id) WHERE kind IN ('automatic','legacy')")
            db.execute("CREATE INDEX IF NOT EXISTS pending_event_requests ON event_requests(state,created_at)")
            db.execute("CREATE TABLE IF NOT EXISTS event_legacy (episode_key TEXT PRIMARY KEY, event_id TEXT UNIQUE NOT NULL)")
            # Older workers must not consume event jobs without the new policy fence.
            db.execute('PRAGMA user_version=3')

    def set_policy(self, *, expected_revision, automatic_rules=(), delivery_ready=False):
        """CAS update; no retroactive auto work. delivery_ready is server configuration."""
        if type(expected_revision) is not int or expected_revision < 0 or type(delivery_ready) is not bool:
            raise ValueError('invalid policy revision/readiness')
        if not isinstance(automatic_rules, (list, tuple)) or len(automatic_rules) > 100:
            raise ValueError('invalid policy rules')
        rules = []
        for rule in automatic_rules:
            if not isinstance(rule, dict) or set(rule) != {'source_alias','target','alertname','channel_id','audience'}:
                raise ValueError('invalid automatic rule')
            if rule['source_alias'] not in self.sources or rule['alertname'] not in ALERT_NAMES:
                raise ValueError('unsupported automatic source/alert')
            binding = next((b for b in self.settings.bindings.bindings if b.alias == rule['target']), None)
            if binding is None or binding.target.cluster_id != self.sources[rule['source_alias']]['cluster_id']:
                raise ValueError('unsupported automatic target')
            if not re.fullmatch(r'C[A-Z0-9]{8,}', rule['channel_id']) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}', rule['audience']):
                raise ValueError('invalid result destination')
            key = tuple(rule[k] for k in ('source_alias','target','alertname'))
            if any(tuple(r[k] for k in ('source_alias','target','alertname')) == key for r in rules):
                raise ValueError('duplicate automatic rule')
            rules.append(dict(rule))
        if rules and not delivery_ready:
            raise ValueError('automatic result delivery is not ready')
        with self.queue.transaction() as db:
            current = db.execute('SELECT revision FROM event_policy WHERE singleton=1').fetchone()[0]
            if current != expected_revision:
                raise ValueError('stale policy revision')
            db.execute('UPDATE event_policy SET revision=?,activated_at=?,config_hash=?,rules=? WHERE singleton=1',
                       (current+1,self.queue.now(),self.settings.config_hash,json.dumps(rules)))
            return current+1

    def _prepare(self, source_alias, item):
        if source_alias not in self.sources:
            raise ValueError('unregistered authenticated source')
        source = self.sources[source_alias]
        if not isinstance(item, dict) or item.get('status') not in ('firing','resolved'):
            raise ValueError('invalid event status')
        labels = _labels(item.get('labels'))
        name = _name(labels.get('alertname'))
        if name == 'Watchdog':
            raise ValueError('heartbeat is not an incident')
        if labels.get('cluster_id', source['cluster_id']) != source['cluster_id']:
            raise ValueError('source cluster mismatch')
        fp = item.get('fingerprint')
        if not isinstance(fp,str) or not re.fullmatch('[0-9a-f]{16}',fp):
            raise ValueError('invalid fingerprint')
        starts, number = alert_time(item.get('startsAt'))
        if number < 0 or number > self.queue.now()+300:
            raise ValueError('invalid event time')
        ends = None
        if item['status'] == 'resolved':
            ends, end_number = alert_time(item.get('endsAt'))
            if ends < starts or end_number > self.queue.now()+300:
                raise ValueError('invalid resolution time')
        elif 'endsAt' in item:
            alert_time(item['endsAt'])
        binding = None
        try:
            target = Target.model_validate({key:labels.get(key) for key in TARGET_LABELS})
            binding = self.settings.bindings.resolve(target)
        except ValueError:
            pass
        route, operation = labels.get('route'), labels.get('operation')
        if binding and ((route is not None and route not in binding.routes) or
                        (operation is not None and operation not in binding.operations)):
            binding = None
        if binding:
            self.settings.metrics.render('api_latency',binding,dimension=route)
            self.settings.metrics.render('db_latency',binding,dimension=operation)
        identity = {'schema_version':1, **source, 'fingerprint':fp, 'starts_at':starts}
        event_id = 'evt-'+sha256_json(identity)
        legacy_key = sha256_json({'source':'alertmanager','cluster_id':source['cluster_id'],'fingerprint':fp,'starts_at':starts})
        metadata = {**identity,'source_alias':source_alias,'alertname':name,
                    'target':binding.alias if binding else None,'target_hash':sha256_json(binding.target.model_dump(mode='json')) if binding else None,
                    'config_hash':self.settings.config_hash,'route':route if binding else None,
                    'operation':operation if binding else None, 'automatic_label':labels.get('ai_ops') == 'enabled'}
        # Config revision is a separate freshness check, not an immutable event label.
        identity_hash = self.queue.engine.request_fingerprint({'labels':labels,'target':metadata['target'],'target_hash':metadata['target_hash']})
        return event_id, identity_hash, metadata, number, ends, legacy_key

    def _record(self, db, prepared, status):
        event_id, identity_hash, meta, starts, ends, _ = prepared
        row = db.execute('SELECT * FROM events WHERE event_id=?',(event_id,)).fetchone()
        if row:
            if row['expired'] or row['expires_at'] <= self.queue.now():
                raise ValueError('event expired')
            if row['identity_hash'] != identity_hash:
                raise ValueError('event identity conflict')
            if status == 'resolved':
                db.execute("UPDATE events SET status='resolved',ends_at=? WHERE event_id=?",
                           (max(filter(None,(row['ends_at'],ends))),event_id))
            return False
        if starts + RETENTION_SECONDS <= self.queue.now():
            raise ValueError('event expired')
        if db.execute('SELECT count(*) FROM events').fetchone()[0] >= MAX_EVENTS:
            raise QueueFull('event ledger capacity reached; archive required')
        db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,0)',
                   (event_id,identity_hash,json.dumps(meta),status,ends,self.queue.now(),starts+RETENTION_SECONDS))
        return True

    def ingest(self, source_alias, item):
        """One authenticated alert; legacy overlaps require verified migration first."""
        prepared = self._prepare(source_alias,item)
        with self.queue.transaction() as db:
            return self._ingest(db,prepared,item['status'])

    def accept(self, source_alias, raw):
        """Bounded v4 envelope for a future authenticated ai-events receiver."""
        if not isinstance(raw,bytes) or not raw or len(raw)>MAX_BODY:
            raise ValueError('invalid event body size')
        doc = _json_object(raw)
        if (doc.get('version')!='4' or doc.get('receiver')!='ai-events'
                or doc.get('status') not in ('firing','resolved')
                or type(doc.get('truncatedAlerts',0)) is not int or doc.get('truncatedAlerts',0)!=0
                or not isinstance(doc.get('alerts'),list) or not 1<=len(doc['alerts'])<=MAX_ALERTS):
            raise ValueError('invalid event envelope')
        prepared=[(self._prepare(source_alias,item),item['status']) for item in doc['alerts']]
        prepared.sort(key=lambda value:value[1]!='resolved')
        with self.queue.transaction() as db:
            return [self._ingest(db,item,status) for item,status in prepared]

    def _ingest(self, db, prepared, status):
        event_id, _, meta, starts, _, legacy_key = prepared
        legacy = db.execute("SELECT 1 FROM sqlite_master WHERE name='alert_episodes' AND type='table'").fetchone()
        if legacy and db.execute('SELECT 1 FROM alert_episodes WHERE episode_key=?',(legacy_key,)).fetchone():
            if not db.execute('SELECT 1 FROM event_legacy WHERE episode_key=? AND event_id=?',(legacy_key,event_id)).fetchone():
                raise ValueError('verified legacy migration required')
        created = self._record(db,prepared,status)
        policy = db.execute('SELECT * FROM event_policy WHERE singleton=1').fetchone()
        if (created and status=='firing' and meta['automatic_label'] and meta['target']
                and starts >= policy['activated_at'] and policy['config_hash']==self.settings.config_hash):
            for rule in json.loads(policy['rules']):
                if all(meta[k]==rule[k] for k in ('source_alias','target','alertname')):
                    self._reserve(db,event_id,'automatic',sha256_json({'event':event_id,'purpose':'automatic'}),
                                  '경보와 관련된 현재 관측 사실과 원인 후보를 읽기 전용으로 확인한다.', policy['revision'], 'policy')
                    break
        return event_id

    def _event(self, db, event_id):
        row = db.execute('SELECT * FROM events WHERE event_id=?',(event_id,)).fetchone()
        if row is None:
            raise ValueError('event not registered')
        if row['expired'] or row['expires_at'] <= self.queue.now():
            raise ValueError('event expired')
        return row

    def reference_id(self, reference):
        if isinstance(reference,str) and re.fullmatch(r'evt-[0-9a-f]{64}',reference):
            event_id = reference
        else:
            parts = reference.split('/',3) if isinstance(reference,str) else []
            if len(parts)!=4 or parts[0]!='al1' or parts[1] not in self.sources or not re.fullmatch('[0-9a-f]{16}',parts[2]):
                raise ValueError('invalid event reference')
            starts,_ = alert_time(parts[3])
            event_id = 'evt-'+sha256_json({'schema_version':1,**self.sources[parts[1]],'fingerprint':parts[2],'starts_at':starts})
        return event_id

    def resolve_reference(self, reference):
        event_id = self.reference_id(reference)
        with self.queue.transaction() as db:
            self._event(db,event_id)
        return event_id

    def reference_state(self, reference):
        """Internal lookup, not user authorization or proof of Slack delivery."""
        event_id = self.reference_id(reference)
        with self.queue.transaction() as db:
            row = db.execute('SELECT status,expired,expires_at FROM events WHERE event_id=?',(event_id,)).fetchone()
            state = 'pending' if row is None else 'expired' if row['expired'] or row['expires_at']<=self.queue.now() else 'registered'
            return {'event_id':event_id,'registration':state,'notification':'unknown',
                    'alert_status':row['status'] if state=='registered' else None}

    def _reserve(self, db, event_id, kind, key, question, revision, subject):
        event = self._event(db,event_id)
        meta = json.loads(event['metadata'])
        if not meta['target'] or meta['config_hash'] != self.settings.config_hash:
            raise ValueError('unbound or changed event target')
        cleaned,_ = sanitize_question(question)
        payload = {k:meta[k] for k in ('target','config_hash','route','operation')}
        payload.update(question=cleaned,mode='rules')
        fp = self.queue.engine.request_fingerprint({'event':event_id,'kind':kind,'payload':payload,'original_question':question,'subject':subject})
        row = db.execute('SELECT * FROM event_requests WHERE request_key=?',(key,)).fetchone()
        if row:
            if row['fingerprint'] != fp:
                raise ValueError('event request key conflict')
            return key
        if db.execute('SELECT count(*) FROM event_requests').fetchone()[0]>=MAX_REQUESTS:
            raise QueueFull('event request ledger capacity reached; archive required')
        db.execute("INSERT INTO event_requests VALUES (?,?,?,?,?,?,'pending',NULL,NULL,?)",
                   (key,event_id,kind,fp,revision,json.dumps(payload),self.queue.now()))
        return key

    def request(self, event_id, *, request_id, question, subject, allowed_targets):
        """Called only after user/channel/signature checks (WF-04); never trusts text ACLs."""
        _name(request_id); _name(subject)
        if (not isinstance(allowed_targets, (list, tuple, set, frozenset))
                or any(not isinstance(target, str) for target in allowed_targets)):
            raise ValueError('invalid allowed target collection')
        with self.queue.transaction() as db:
            meta = json.loads(self._event(db,event_id)['metadata'])
            if meta['target'] is None or meta['target'] not in allowed_targets:
                raise ValueError('event request not authorized')
            key = sha256_json({'source':'event-manual','request_id':request_id})
            return self._reserve(db,event_id,'manual',key,question,None,subject)

    def drain(self, *, limit=100):
        if type(limit) is not int or not 1<=limit<=100:
            raise ValueError('invalid drain limit')
        delivered = []
        with self.queue.transaction() as db:
            policy = db.execute('SELECT * FROM event_policy WHERE singleton=1').fetchone()
            rows = db.execute("SELECT r.*,e.status,e.expires_at,e.expired FROM event_requests r JOIN events e ON e.event_id=r.event_id WHERE r.state='pending' ORDER BY r.created_at,r.request_key LIMIT ?",(limit,)).fetchall()
            for row in rows:
                reason = None
                payload = json.loads(row['payload'])
                if row['expired'] or row['expires_at']<=self.queue.now(): reason='event_expired'
                elif payload['config_hash'] != self.settings.config_hash or policy['config_hash'] != self.settings.config_hash: reason='config_changed'
                elif row['kind']=='automatic' and (row['status']=='resolved' or row['policy_revision']!=policy['revision']): reason='event_policy_or_status_changed'
                if reason:
                    db.execute("UPDATE event_requests SET state='cancelled',reason=?,payload=NULL WHERE request_key=?",(reason,row['request_key']))
                    continue
                try:
                    job,_ = self.queue._submit(db,payload=payload,fingerprint=row['fingerprint'],episode_key=sha256_json({'source':'event-outbox','key':row['request_key']}),now=self.queue.now())
                except QueueFull:
                    # _submit checks admission before writing. Commit the delivered
                    # prefix; retain the rest as pending with their original keys.
                    if not delivered:
                        raise
                    break
                db.execute("UPDATE event_requests SET state='delivered',payload=NULL,job_id=? WHERE request_key=?",(job,row['request_key']))
                delivered.append(job)
        return delivered

    def show(self, event_id):
        with self.queue.transaction() as db:
            row = self._event(db,event_id)
            meta = json.loads(row['metadata'])
            requests = [dict(r) for r in db.execute('SELECT request_key,kind,state,reason,job_id FROM event_requests WHERE event_id=? ORDER BY request_key',(event_id,))]
        return {'event_id':event_id,'status':row['status'],'starts_at':meta['starts_at'],'ends_at':row['ends_at'],
                'target':meta['target'],'alertname':meta['alertname'],'notification':'unknown','requests':requests}

    def migrate_legacy(self, source_alias, item):
        """Authenticated replay proves legacy hashes; no guessing or new queue jobs."""
        prepared = self._prepare(source_alias,item)
        event_id = prepared[0]
        # Existing parser remains the authority for the legacy five-alert scope.
        inbox = AlertInbox(self.queue,self.settings,cluster_id=self.sources[source_alias]['cluster_id'])
        old = inbox._prepare_alert(item,self.queue.now())
        with self.queue.transaction() as db:
            row = db.execute('SELECT * FROM alert_episodes WHERE episode_key=?',(old['key'],)).fetchone()
            if row is None or any(row[k]!=old[v] for k,v in (('identity_hash','identity'),('target_hash','target_hash'),('starts_at','starts'),('alertname','name'))):
                raise ValueError('legacy identity not verified')
            resolved = row['status']=='resolved' or item['status']=='resolved'
            status = 'resolved' if resolved else 'firing'
            ends = max(filter(None,(row['ends_at'],prepared[4])), default=None)
            prepared = (*prepared[:4], ends, prepared[5])
            mapping = db.execute('SELECT event_id FROM event_legacy WHERE episode_key=?',(old['key'],)).fetchone()
            if mapping:
                if mapping['event_id']!=event_id: raise ValueError('legacy source conflict')
                self._record(db,prepared,status)
                return event_id
            if db.execute('SELECT 1 FROM events WHERE event_id=?',(event_id,)).fetchone():
                raise ValueError('event already exists; reconcile explicitly')
            if row['job_id']:
                job = db.execute('SELECT * FROM jobs WHERE job_id=?',(row['job_id'],)).fetchone()
                purge = db.execute('SELECT 1 FROM purges WHERE job_id=?',(row['job_id'],)).fetchone()
                if job is None or purge or job['episode_key']!=old['key'] or json.loads(job['payload'])!=old['payload']:
                    raise ValueError('legacy job/config cannot be verified')
            self._record(db,prepared,status)
            db.execute('UPDATE events SET status=?,ends_at=? WHERE event_id=?',(status,ends,event_id))
            db.execute('INSERT INTO event_legacy VALUES (?,?)',(old['key'],event_id))
            # Marker also covers resolved-first legacy entries with no job.
            if db.execute('SELECT count(*) FROM event_requests').fetchone()[0]>=MAX_REQUESTS:
                raise QueueFull('event request ledger capacity reached')
            db.execute("INSERT INTO event_requests VALUES (?,?,'legacy',NULL,NULL,NULL,'delivered',NULL,?,?)",
                       (sha256_json({'legacy':old['key']}),event_id,row['job_id'],row['received_at']))
        return event_id

    def expire(self, *, apply=False):
        """7 days from occurrence; keep hash tombstones. Never delete active evidence."""
        result = {'mode':'apply' if apply else 'preview','expired':[],'deferred':[]}
        with self.queue.transaction() as db:
            for row in db.execute('SELECT event_id FROM events WHERE expired=0 AND expires_at<=?',(self.queue.now(),)).fetchall():
                event_id=row['event_id']
                active=db.execute("SELECT 1 FROM event_requests r JOIN jobs j ON j.job_id=r.job_id WHERE r.event_id=? AND j.state IN ('collecting','analyzing')",(event_id,)).fetchone()
                if active:
                    result['deferred'].append(event_id); continue
                result['expired'].append(event_id)
                if apply:
                    guard_jobs(db,self.queue.now())
                    db.execute("UPDATE event_requests SET state='expired',payload=NULL,fingerprint=NULL,policy_revision=NULL,created_at=NULL,reason='event_expired' WHERE event_id=?",(event_id,))
                    db.execute('UPDATE events SET expired=1,metadata=NULL,identity_hash=NULL,ends_at=NULL,received_at=NULL WHERE event_id=?',(event_id,))
        return result
