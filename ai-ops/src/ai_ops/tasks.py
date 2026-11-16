"""로컬 요청·기존 조사 작업·결과·로컬 알림을 연결한다.

작업의 기준 상태는 JobQueue/IncidentStore다. 이 모듈은 별도 실행 권한이나
worker를 만들지 않으며 복구·PR·승인·모델·외부 알림 API를 호출하지 않는다.
"""

from pathlib import Path
from contextlib import contextmanager
import fcntl
import json
import os
import re
import stat
import tempfile

from pydantic import Field, StrictStr

from ai_ops.contracts import StrictModel, _utc_datetime, canonical_json_bytes, sha256_json
from ai_ops.evidence import sanitize_question
from ai_ops.incidents import _private_directory
from ai_ops.job_queue import JobQueue, RETENTION_SECONDS, utc
from ai_ops.reports import CURRENT_STATE, DIAGNOSIS


class TaskRequest(StrictModel):
    request_id: StrictStr = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
    conversation_id: StrictStr = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
    target: StrictStr = Field(min_length=1, max_length=80)
    question: StrictStr = Field(min_length=1, max_length=4096)


class DeliveryNotSent(OSError):
    """로컬 publish를 시도하기 전의 확정 실패다. 전달 불명확에는 사용하지 않는다."""


class TaskService:
    """현재 OS 사용자 전용 로컬 창구다. 다중 사용자 인증 어댑터는 별도다."""

    def __init__(self, queue: JobQueue, settings):
        self.queue, self.settings = queue, settings
        self.directory = queue.root / 'tasks'
        self.mailbox = self.directory / 'mailbox'
        _private_directory(self.directory)
        _private_directory(self.mailbox)
        with queue.transaction() as db:
            # 기존 큐의 schema/state/worker 잠금은 변경하지 않는 부가 장부다.
            db.execute('CREATE TABLE IF NOT EXISTS task_requests ('
                       'request_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, '
                       'conversation_id TEXT NOT NULL, job_id TEXT UNIQUE, created_at REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS task_deliveries ('
                       'job_id TEXT PRIMARY KEY, delivery_id TEXT UNIQUE NOT NULL, '
                       'payload_hash TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, '
                       'updated_at REAL NOT NULL)')

    def submit(self, raw):
        request = TaskRequest.model_validate(raw)
        binding = next((b for b in self.settings.bindings.bindings if b.alias == request.target), None)
        if binding is None:
            raise ValueError('task_target_rejected')
        sanitize_question(request.question)
        # 원문 질문/요청 키를 새 장부에 저장하지 않는다. 회신 목적지도 중복 계약에 포함한다.
        key = sha256_json({'source': 'local-task-v1', 'request_id': request.request_id})
        fingerprint = self.queue.engine.request_fingerprint(request.model_dump())
        with self.queue.transaction() as db:
            existing = db.execute('SELECT * FROM task_requests WHERE request_key=?', (key,)).fetchone()
            if existing is not None and existing['fingerprint'] != fingerprint:
                raise ValueError('task_request_conflict')
            if (existing is not None and existing['job_id'] is None
                    and self.queue.now() >= existing['created_at'] + RETENTION_SECONDS):
                raise ValueError('task_reservation_expired')
            if existing is None:
                db.execute('INSERT INTO task_requests VALUES (?,?,?,?,?)',
                           (key, fingerprint, request.conversation_id, None, self.queue.now()))
        # 예약→큐 접수→연결의 어느 지점에서 중단돼도 동일 요청 재전송으로 같은 job에 연결한다.
        # 연결 저장까지 끝나기 전에는 accepted 응답을 보내지 않는다.
        job, duplicate = self.queue.submit(self.settings, target=request.target,
                                          question=request.question, request_id='task:' + key)
        with self.queue.transaction() as db:
            db.execute('UPDATE task_requests SET job_id=? WHERE request_key=? AND fingerprint=?',
                       (job, key, fingerprint))
        view = self.show(job)
        return {'accepted': True, 'task_id': job, 'duplicate': duplicate,
                'analysis_completed': view['result']['state'] == 'available',
                'result_state': view['result']['state'],
                'investigation_state': view['investigation']['state'], 'execution_allowed': False}

    def _request(self, job, *, required=True):
        self.queue._id(job)
        with self.queue.transaction() as db:
            row = db.execute('SELECT * FROM task_requests WHERE job_id=?', (job,)).fetchone()
        if row is None and required:
            raise ValueError('task_not_registered')
        return dict(row) if row is not None else None

    def _expired(self, request, job):
        return (job['state'] in ('purging', 'purged')
                or self.queue.now() >= _utc_datetime(job['retention_deadline']).timestamp()
                or (request is not None and self.queue.now() >= request['created_at'] + RETENTION_SECONDS))

    def _delivery(self, job):
        with self.queue.transaction() as db:
            row = db.execute('SELECT delivery_id,state,attempts,updated_at FROM task_deliveries WHERE job_id=?',
                             (job,)).fetchone()
        return ({'state': 'not_prepared', 'transport': 'local_mailbox'} if row is None else
                {**dict(row), 'updated_at': utc(row['updated_at']), 'transport': 'local_mailbox'})

    @contextmanager
    def _delivery_lock(self):
        _private_directory(self.directory)
        fd = os.open(self.directory / 'delivery.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError('task_delivery_lock_rejected')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('task_delivery_busy') from None
            yield
        finally:
            os.close(fd)

    def show(self, job):
        request = self._request(job, required=False)
        investigation = self.queue.show(job)
        expired = self._expired(request, investigation)
        state = investigation['state']
        available = state in ('completed', 'partial') and not expired
        return {'task_id': job, 'source': 'local' if request else 'existing_queue',
                'conversation_id': request['conversation_id'] if request else None,
                'investigation': investigation,
                'result': {'state': 'expired' if expired else 'available' if available else
                           'unavailable' if state in ('failed', 'cancelled', 'needs_review') else 'pending',
                           'reference': {'incident_id': investigation['incident_id'],
                                         'run_id': investigation['run_id']} if available else None},
                'delivery': {'state': 'expired', 'transport': 'local_mailbox'} if expired else self._delivery(job),
                'recovery': {'state': 'not_connected', 'execution_allowed': False}}

    def result(self, job):
        view = self.show(job)
        if view['result']['state'] != 'available':
            return {'task_id': job, **view['result'], 'execution_allowed': False}
        investigation = view['investigation']
        record = self.queue.engine.show(investigation['incident_id'], investigation['run_id'])
        if (record.report is None or record.bundle is None
                or record.metadata['status'] != investigation['state']
                or record.bundle.bundle_hash != investigation['bundle_hash']
                or record.report.execution.mode != 'rules'):
            raise ValueError('task_report_binding_rejected')
        if self.show(job) != view:
            raise ValueError('task_changed_during_result_read')
        # 관측 요약과 마스킹한 런북 참고 자료를 분리하며 원문 관측/개인 경로는 복사하지 않는다.
        report = record.report
        result = {'task_id': job, 'state': 'available', 'investigation_state': investigation['state'],
                'reference': view['result']['reference'], 'bundle_hash': record.bundle.bundle_hash,
                'summary': DIAGNOSIS[report.diagnosis_status],
                'observed_state': CURRENT_STATE[report.current_state],
                'evidence_ids': sorted({eid for fact in report.facts for eid in fact.evidence_ids}),
                'limitation_count': len(report.limitations), 'execution_allowed': False}
        if record.runbook_context is not None:
            from ai_ops.runbook_context import withheld
            result['runbook_context'] = (withheld() if self.settings.runbooks is None else
                self.settings.runbooks.expose(record.runbook_context, record.request, record.bundle, report, self.settings))
        if self.show(job) != view:
            raise ValueError('task_changed_during_reference_read')
        return result

    def _envelope(self, job):
        self._request(job)  # 기존 큐 작업의 조회는 허용하되 회신 목적지를 추정하지 않는다.
        view = self.show(job)
        if view['result']['state'] in ('pending', 'expired'):
            raise ValueError('task_result_not_deliverable')
        envelope = {'schema_version': 1, 'transport': 'local_mailbox', 'task_id': job,
                'conversation_id': view['conversation_id'],
                'investigation_state': view['investigation']['state'], 'result': self.result(job),
                'execution_allowed': False, 'human_read_confirmed': False}
        # 최초 조회 이후 만료/상태 변경이 생겼다면 앞선 view로 알림을 만들지 않는다.
        if self.show(job) != view or envelope['result']['state'] not in ('available', 'unavailable'):
            raise ValueError('task_changed_during_notice_read')
        if len(canonical_json_bytes(envelope)) > 60000:
            raise ValueError('task_notice_too_large')
        return envelope

    def _mailbox_path(self, identifier):
        if not re.fullmatch(r'notice-[0-9a-f]{64}', identifier):
            raise ValueError('task_delivery_id_rejected')
        _private_directory(self.directory)
        _private_directory(self.mailbox)
        return self.mailbox / (identifier + '.json')

    def _read_delivery(self, identifier):
        path = self._mailbox_path(identifier)
        if not path.exists() and not path.is_symlink():
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536):
                raise ValueError('task_mailbox_file_rejected')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                return json.loads(stream.read(65537))
        finally:
            os.close(fd)

    def _publish(self, identifier, envelope):
        path = self._mailbox_path(identifier)
        temporary = None
        try:
            try:
                with tempfile.NamedTemporaryFile(dir=self.mailbox, prefix='.notice-', delete=False) as stream:
                    temporary = Path(stream.name)
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(canonical_json_bytes(envelope))
                    stream.flush(); os.fsync(stream.fileno())
            except OSError as exc:
                # 최종 경로에 대한 publish를 아직 시도하지 않은 경우만 재시도 가능하다.
                raise DeliveryNotSent('task_local_prepare_failed') from exc
            # 완성한 파일을 배타적으로 게시한다. 이미 있으면 내용을 대조한다.
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        if self._read_delivery(identifier) != envelope:
            raise ValueError('task_mailbox_binding_rejected')

    def _finish_delivery(self, job, state):
        with self.queue.transaction() as db:
            db.execute('UPDATE task_deliveries SET state=?,updated_at=? WHERE job_id=? AND state="sending"',
                       (state, self.queue.now(), job))

    def _check_reference_envelope(self, job, envelope):
        if (envelope['result'].get('runbook_context') is not None
                and self.result(job) != envelope['result']):
            raise ValueError('task_reference_scope_changed')

    def deliver(self, job):
        with self._delivery_lock():
            return self._deliver(job)

    def _deliver(self, job):
        envelope = self._envelope(job)
        digest = sha256_json(envelope)
        identifier = 'notice-' + sha256_json({'job': job, 'payload': digest})
        with self.queue.transaction() as db:
            row = db.execute('SELECT * FROM task_deliveries WHERE job_id=?', (job,)).fetchone()
            if row is None:
                db.execute('INSERT INTO task_deliveries VALUES (?,?,?,?,?,?)',
                           (job, identifier, digest, 'pending', 0, self.queue.now()))
                state, attempts = 'pending', 0
            else:
                if row['delivery_id'] != identifier or row['payload_hash'] != digest:
                    raise ValueError('task_delivery_payload_changed')
                state, attempts = row['state'], row['attempts']
            if state == 'pending' and attempts < 3:
                db.execute('UPDATE task_deliveries SET state="sending",attempts=attempts+1,updated_at=? WHERE job_id=?',
                           (self.queue.now(), job))
        if state != 'pending' or attempts >= 3:
            if state == 'delivered':
                # 장부의 과거 성공만으로 현재 파일 부재·변조를 성공 처리하지 않는다.
                return self._reconcile_delivery(job)
            # 전달 중/불명확 결과는 새 전송하지 않는다. reconcile은 별도 명령이다.
            return self._delivery_from_row(identifier, state, attempts)
        try:
            self._check_reference_envelope(job, envelope)
            self._publish(identifier, envelope)
        except DeliveryNotSent:
            self._finish_delivery(job, 'pending' if attempts + 1 < 3 else 'failed')
            return self._delivery(job)
        except (OSError, ValueError):
            # 로컬 파일도 결과가 불명확하면 자동 재전송하지 않고 확인 대기로 둔다.
            self._finish_delivery(job, 'needs_review')
            return self._delivery(job)
        self._finish_delivery(job, 'delivered')
        return self._delivery(job)

    @staticmethod
    def _delivery_from_row(identifier, state, attempts):
        return {'delivery_id': identifier, 'state': state, 'attempts': attempts, 'transport': 'local_mailbox'}

    def reconcile_delivery(self, job):
        """응답 유실 후 같은 로컬 파일만 확인한다. 없거나 다르면 재전송하지 않는다."""
        with self._delivery_lock():
            return self._reconcile_delivery(job)

    def _reconcile_delivery(self, job):
        envelope = self._envelope(job)
        with self.queue.transaction() as db:
            row = db.execute('SELECT * FROM task_deliveries WHERE job_id=?', (job,)).fetchone()
        if row is None or row['payload_hash'] != sha256_json(envelope):
            raise ValueError('task_delivery_not_prepared')
        try:
            valid = self._read_delivery(row['delivery_id']) == envelope
            if valid:
                self._check_reference_envelope(job, envelope)
        except (OSError, ValueError):
            valid = False
        with self.queue.transaction() as db:
            db.execute('UPDATE task_deliveries SET state=?,updated_at=? WHERE job_id=? AND state IN ("sending","needs_review","delivered")',
                       ('delivered' if valid else 'needs_review', self.queue.now(), job))
        return self._delivery(job)

    def prune(self):
        """만료된 알림만 회수한다. 큐/원본 사건의 보관 정책은 기존 정리기가 담당한다."""
        with self._delivery_lock():
            return self._prune()

    def _prune(self):
        with self.queue.transaction() as db:
            rows = db.execute('SELECT r.job_id,d.delivery_id FROM task_requests r '
                              'JOIN task_deliveries d ON d.job_id=r.job_id WHERE d.state!="expired"').fetchall()
        removed = 0
        for row in rows:
            request = self._request(row['job_id'])
            if not self._expired(request, self.queue.show(row['job_id'])):
                continue
            path = self._mailbox_path(row['delivery_id'])
            if path.is_symlink():
                raise ValueError('task_mailbox_link_rejected')
            path.unlink(missing_ok=True)
            with self.queue.transaction() as db:
                db.execute('UPDATE task_deliveries SET state="expired",updated_at=? WHERE job_id=?',
                           (self.queue.now(), row['job_id']))
            removed += 1
        return {'expired_deliveries': removed}
