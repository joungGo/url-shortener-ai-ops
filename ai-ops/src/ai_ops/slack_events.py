"""WF-04 signed Slack event references -> existing event outbox/queue/results.

This module has no transport, worker launcher or recovery action. Only the already
verified Slack adapter calls it. Unknown and unauthorized references are indistinguishable.
"""
import json

from ai_ops.contracts import sha256_json
from ai_ops.job_queue import RETENTION_SECONDS, utc

UNAVAILABLE = ('사건 접수 확인 중이거나 조회할 수 없는 참조입니다. 아직 조사 작업을 만들지 않았습니다. '
               '참조·대상 권한·보관 기간을 확인하고 새 메시지로 다시 요청하세요.')
AMBIGUOUS = '이 스레드의 사건을 하나로 정할 수 없습니다. alert_ref 또는 evt-ID를 명시해 주세요. 조사 작업은 없습니다.'


class SlackEventBridge:
    def __init__(self, adapter):
        self.adapter = adapter
        self.store = adapter.events

    def targets(self, user, channel):
        return {target for grant in self.adapter.policy.grants
                if grant.user_id == user and grant.channel_id == channel for target in grant.targets}

    def entry(self, request_id):
        with self.adapter._db() as db:
            row = db.execute('SELECT * FROM slack_event_requests WHERE request_id=?', (request_id,)).fetchone()
        return dict(row) if row else None

    def parse(self, command, reference, user, channel, thread):
        if self.store is None or not self.targets(user, channel):
            raise ValueError('slack_principal_target_rejected')
        if reference in ('this', '이장애'):
            # Never infer from arbitrary text, another channel, or another person's thread.
            with self.adapter._db() as db:
                rows = db.execute('SELECT DISTINCT e.event_id FROM slack_event_requests e '
                    'JOIN slack_requests r USING(request_id) WHERE r.user_id=? AND r.channel_id=? '
                    'AND r.thread_ts=? AND r.target!="" AND r.state!="expired" AND r.created_at>? '
                    'AND r.policy_hash=? AND r.config_hash=?',
                    (user,channel,thread,self.store.queue.now()-RETENTION_SECONDS,
                     self.adapter.policy_hash,self.adapter.config_hash)).fetchall()
            reference = rows[0]['event_id'] if len(rows)==1 else 'ambiguous'
            if reference == 'ambiguous':
                return command, reference
        return command, self.store.reference_id(reference)

    def view(self, row, entry):
        if self.store is None or entry['event_id'] in ('', 'ambiguous'):
            return None
        return self.store.authorized_view(entry['event_id'],
            allowed_targets=self.targets(row['user_id'],row['channel_id']))

    def authorized(self, row):
        entry = self.entry(row['request_id'])
        if entry is None:
            return self.adapter.policy.allows(row['user_id'],row['channel_id'],row['target'])
        if self.store is None or not self.targets(row['user_id'],row['channel_id']):
            return False
        if not row['target']:
            # Only a uniform non-disclosing response has no target binding.
            return entry['context'] is None
        view = self.view(row, entry)
        if view is None or view['target'] != row['target']:
            return False
        if view['registration']=='registered':
            context=json.loads(entry['context']) if entry['context'] else {}
            if 'recovery_plan' in context:
                from ai_ops.recovery_plans import RecoveryPlan
                plan=RecoveryPlan.model_validate(context['recovery_plan'])
                return (plan.event_id==entry['event_id'] and plan.request_id==row['request_id']
                        and plan.authorization_hash==self.adapter.authorization_hash
                        and plan.config_hash==self.adapter.config_hash
                        and plan.audience_hash==sha256_json({'team':self.adapter.policy.team_id,
                            'user':row['user_id'],'channel':row['channel_id'],'thread':row['thread_ts']})
                        and self.adapter.tasks.show(plan.job_id)['result']['state']!='expired'
                        and self.store.queue.now()<plan.retained_until.timestamp())
            return True
        context=json.loads(entry['context']) if entry['context'] else {}
        return row['state']=='ready' and context.get('registration')=='expired'

    def _ready(self, row, entry, text, context=None):
        with self.adapter._db() as db:
            db.execute('UPDATE slack_event_requests SET context=?,reply_text=? WHERE request_id=?',
                (json.dumps(context) if context else None,text,row['request_id']))
            db.execute('UPDATE slack_requests SET state="ready",target=?,question=NULL WHERE request_id=?',
                (context['target'] if context else '',row['request_id']))
        return {'request_id':row['request_id'],'state':'ready','queue_accepted':False,'execution_allowed':False}

    def drain(self, row):
        entry = self.entry(row['request_id'])
        view = self.view(row, entry)
        if view is None:
            return self._ready(row,entry,AMBIGUOUS if entry['event_id']=='ambiguous' else UNAVAILABLE)
        view['checked_at'] = utc(self.store.queue.now())
        if view['registration']=='expired':
            return self._ready(row,entry,'사건 상세의 보관 기간이 만료됐습니다. 새 조사 작업은 없습니다.',view)
        if entry['command']=='plan-recovery':
            return self._plan(row, entry, view)
        if entry['command']=='status-event':
            # Status sees only this principal's requests, never another audience's job IDs.
            with self.adapter._db() as db:
                jobs = db.execute('SELECT r.job_id FROM slack_requests r JOIN slack_event_requests e USING(request_id) '
                    'WHERE e.event_id=? AND r.user_id=? AND r.channel_id=? AND r.job_id IS NOT NULL '
                    'AND r.state!="expired" AND r.policy_hash=? AND r.config_hash=? '
                    'ORDER BY r.created_at DESC,r.request_id LIMIT 10',
                    (entry['event_id'],row['user_id'],row['channel_id'],self.adapter.policy_hash,
                     self.adapter.config_hash)).fetchall()
            states = []
            for job in jobs:
                shown = self.adapter.tasks.show(job['job_id'])
                states.append(f"{job['job_id']}: {shown['investigation']['state']} / 결과 {shown['result']['state']}")
            text = self._context_text(view) + '본인이 요청한 최근 조사(최대 10건):\n'
            text += '\n'.join(states) if states else '연결된 조사 작업 없음'
            text += '\n상태조회만 수행했으며 새 조사를 시작하지 않았습니다.'
            return self._ready(row,entry,text,view)
        key = self.store.request(entry['event_id'],request_id=row['request_id'],question=row['question'],
            subject='slack:'+sha256_json({'team':self.adapter.policy.team_id,'user':row['user_id'],
                                         'channel':row['channel_id']}),
            allowed_targets=self.targets(row['user_id'],row['channel_id']),
            authority=(self.adapter.event_owner,self.adapter.authorization_hash))
        # Scoped drain cannot activate an unrelated automatic outbox entry.
        self.store.drain(request_key=key)
        receipt = self.store.request_receipt(key)
        if receipt['state']!='delivered' or not receipt['job_id']:
            return self._ready(row,entry,'조사 요청을 전달하지 못했습니다. 현재 조건을 확인한 뒤 다시 요청하세요.')
        # Both DBs retain stable keys. A crash before this transaction reconnects to
        # the SAME event outbox/job, without another collection or result send.
        with self.adapter._db() as db:
            db.execute('UPDATE slack_event_requests SET context=? WHERE request_id=?',
                       (json.dumps(view),row['request_id']))
            db.execute('UPDATE slack_requests SET state="queued",target=?,job_id=?,question=NULL WHERE request_id=?',
                       (view['target'],receipt['job_id'],row['request_id']))
        return {'request_id':row['request_id'],'state':'queued','task_id':receipt['job_id'],
                'event_id':entry['event_id'],'execution_allowed':False}

    def _plan(self, row, entry, view):
        from ai_ops.recovery_plans import parse_plan_arguments, prepare_plan, plan_text
        job, action = parse_plan_arguments(row['question'])
        # An explicit job must belong to this event AND this signed audience.
        # Other users' jobs and arbitrary guessed IDs get the same response.
        with self.adapter._db() as db:
            linked = db.execute('SELECT 1 FROM slack_requests r JOIN slack_event_requests e USING(request_id) '
                'WHERE r.job_id=? AND e.event_id=? AND e.command="investigate-event" '
                'AND r.user_id=? AND r.channel_id=? AND r.target=? AND r.policy_hash=? '
                'AND r.config_hash=? AND r.state="queued" AND r.created_at>?',
                (job,entry['event_id'],row['user_id'],row['channel_id'],view['target'],
                 self.adapter.policy_hash,self.adapter.config_hash,
                 self.store.queue.now()-RETENTION_SECONDS)).fetchone()
        if linked is None:
            return self._ready(row,entry,UNAVAILABLE)
        plan = prepare_plan(self.adapter.tasks,self.store,view=view,job_id=job,action_id=action,
            request_id=row['request_id'],authorization_hash=self.adapter.authorization_hash,
            audience_hash=sha256_json({'team':self.adapter.policy.team_id,'user':row['user_id'],
                                      'channel':row['channel_id'],'thread':row['thread_ts']}))
        if not self.adapter._authorized(row) or self.view(row,entry) != {k:v for k,v in view.items() if k!='checked_at'}:
            raise ValueError('recovery_scope_changed')
        context = {**view,'recovery_plan':plan.model_dump(mode='json')}
        # Atomic frozen plan/reply. Crash before commit just rebuilds a local draft;
        # after commit no retry re-reads a different result or creates another job.
        return self._ready(row,entry,plan_text(plan),context)

    @staticmethod
    def _context_text(context):
        return (f"사건: {context['event_id']}\n경보 발생: {context['starts_at']}\n"
                f"경보 상태: {context['status']} (확인 시각 {context['checked_at']})\n"
                + (f"경보 해제: {context['ends_at']}\n" if context['ends_at'] else '')
                + '경보 공지 전달 여부: 확인되지 않음. 경보 해제는 복구 성공이나 승인이 아닙니다.\n')

    def prefix(self, row, result):
        entry = self.entry(row['request_id'])
        if entry is None:
            with self.adapter._db() as db:
                current=db.execute('SELECT 1 FROM slack_request_formats WHERE request_id=?',(row['request_id'],)).fetchone()
            # Existing delivered requests retain their exact historical payload hash.
            return '사건 미지정 일반 조사입니다.\n' if current else ''
        context = json.loads(entry['context'])
        text = self._context_text(context)
        observation = result.get('observation')
        if observation:
            text += (f"실제 자료 범위: {observation['window']['start']} ~ {observation['window']['end']}\n"
                     f"자료 수집 시각: {observation['collected_at']}\n")
        return text + ('사건 발생 당시 증거와 현재 관측은 같다고 가정하지 않습니다. '
                       '같은 이름의 대상도 당시 UID와 동일하다고 확정하지 않습니다.\n\n')

    def status_payload(self, row):
        entry = self.entry(row['request_id'])
        if entry is None or entry['reply_text'] is None:
            raise ValueError('slack_status_not_ready')
        return {'channel':row['channel_id'],'thread_ts':row['thread_ts'],
                'text':entry['reply_text']+'\n변경·복구를 실행하지 않았습니다. 사람 승인 결과가 아닙니다.',
                'mrkdwn':False,'parse':'none','unfurl_links':False,'unfurl_media':False,'reply_broadcast':False}
