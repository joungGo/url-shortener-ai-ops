"""A7 restart (command) path wiring up to just before execution (user decisions 2026-11-25).

* Started by the same ``@bot request-approval <event> <plan hash>`` command: when
  the thread's plan is the WF05 ``deployment-restart`` command candidate, the WF08
  coordinator seals an exact restart plan from a fresh read (restart gates run
  here) and the ``execute_command`` approval wait opens: separate ledger, buttons
  and policy from the PR path.
* After approval the relay revalidates (read-only) until ``command_ready``.
  **Execution stays disabled**: nothing here calls ``execute_local``; the WF08
  executor accepts only ``local_fixture`` ports and enabling live execution is a
  separate WF11 decision. The command-ready notice says so.
* Reads use the observer's Kubernetes read identity (PR57) extended with the
  restart reads (events, HPAs, Argo application) plus configured Prometheus
  queries. No writer identity is configured or created here.
"""
from __future__ import annotations

import json
import re

from pydantic import Field, StrictInt, StrictStr, model_validator

from ai_ops.contracts import StrictModel
from ai_ops.recovery_approvals import ApprovalPolicy
from ai_ops.recovery_commands import RestartApprovalLedger, RestartCoordinator, slack_restart_source
from ai_ops.restart_gates import RestartGateRejected, rejected as rejected_gates
from ai_ops.restart_k8s import (PROMETHEUS_ROUTES, READER_ROUTES, ApiClient, KubernetesRestartPort,
                                PrometheusSignals)

# Evidence capture runs only at execution, which this wiring never reaches.
DISABLED_EVIDENCE_DIR = '/nonexistent/restart-evidence-disabled'
GATE_NAME = re.compile(r'[a-z_]{1,64}\Z')

RESTART_DISABLED = ('재시작 경로가 켜져 있지 않아 재시작 승인 대기를 열지 않았습니다. '
                    '관리자 설정이 필요하며, 변경은 없습니다.')
RESTART_UNAVAILABLE = ('재시작 승인 대기를 열 수 없습니다. 이 스레드에서 본인이 요청한 재시작 후보인지, '
                       '경보가 아직 발생 중인지와 보관 기간을 확인하세요. 승인 대기를 만들지 않았습니다.')
RESTART_STALE = ('조사 근거가 오래되었거나(조사 관측 후 2분 이내 필요) 현재 상태가 조사 당시와 달라 '
                 '재시작 계획을 만들지 않았습니다. 필요하면 새 조사와 복구 계획을 요청하세요.')
RESTART_GATES = ('현재 상태가 재시작 조건을 통과하지 못해 승인 대기를 열지 않았습니다'
                 '(통과하지 못한 조건: {names}). 자동 재시도나 변경은 하지 않습니다.')
RESTART_OPENED = ('재시작 승인 대기를 열었습니다(응답 마감 {until}, 재시작 계획 {plan}). '
                  '승인·거절 버튼이 있는 메시지를 이 스레드에 곧 보냅니다. 승인 뒤에는 현재 상태를 다시 확인만 하며, '
                  '실제 재시작은 실행하지 않습니다(실행 비활성).')
RESTART_EXISTING = ('이 재시작 계획의 승인 요청은 이미 처리되었습니다(상태: {state}). 같은 계획으로 다시 열지 않으며, '
                    '필요하면 새 조사와 복구 계획을 요청하세요.')

STALE_CODES = frozenset({'restart_source_stale_or_changed', 'restart_source_changed',
                         'new_command_plan_required'})
NOT_OBSERVED_CODES = frozenset({'restart_current_state_rejected', 'restart_prepare_expired',
                                'revalidation_observation_rejected', 'revalidation_timed_out'})
FULL_CODES = frozenset({'restart_ledger_full', 'approval_ledger_full'})


class PrometheusConfig(StrictModel):
    api_url: StrictStr = Field(pattern=r'^https://[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:[1-9][0-9]{0,4})?$')
    token_file: StrictStr
    ca_file: StrictStr
    # PromQL from configuration only; `$TEMPLATE_HASH` is replaced literally.
    # Absent/empty -> the signal is 'unknown' and the restart gates block.
    prior_ready_query: StrictStr | None = Field(default=None, min_length=1, max_length=1000)
    dependency_queries: list[StrictStr] = Field(default_factory=list, max_length=8)

    @model_validator(mode='after')
    def valid(self):
        if not self.token_file.startswith('/') or not self.ca_file.startswith('/'):
            raise ValueError('prometheus_credential_path_rejected')
        if any(not q or len(q) > 1000 for q in self.dependency_queries):
            raise ValueError('prometheus_query_rejected')
        return self


class RestartConfig(StrictModel):
    """Optional ``restart`` section of recovery-approval.json."""
    approval: ApprovalPolicy
    executor_id: StrictStr = Field(pattern=r'^local-restart-[a-z0-9-]{1,40}$')
    execution_seconds: StrictInt = Field(ge=1, le=15)
    prometheus: PrometheusConfig

    @model_validator(mode='after')
    def scope(self):
        if self.approval.scope != 'execute_command':
            raise ValueError('restart_approval_scope_rejected')
        return self


def restart_source_index(adapter):
    """Process-wide coordinator ``source``: candidate hash -> its request -> WF08 source."""
    from ai_ops.recovery_approval_wiring import plan_request_id

    def read(candidate_hash, actor, *, require_firing):
        request_id = plan_request_id(adapter, candidate_hash, actor=actor)
        if request_id is None:
            raise ValueError('command_source_unavailable')
        source = slack_restart_source(adapter, request_id)
        return source(candidate_hash, actor) if require_firing else source.for_effects(candidate_hash, actor)

    def load(candidate_hash, actor):
        return read(candidate_hash, actor, require_firing=True)
    load.for_effects = lambda candidate_hash, actor: read(candidate_hash, actor, require_firing=False)
    return load


def make_restart_reader(observe, restart, *, now, connection_factory=None):
    """Read-only restart port: ``writer`` is None, so it cannot patch or take a lock."""
    reader = ApiClient(observe.api_url, observe.token_file, observe.ca_file, READER_ROUTES, now=now,
                       connection_factory=connection_factory)
    prom = ApiClient(restart.prometheus.api_url, restart.prometheus.token_file, restart.prometheus.ca_file,
                     PROMETHEUS_ROUTES, now=now, connection_factory=connection_factory)
    signals = PrometheusSignals(prom, prior_ready=restart.prometheus.prior_ready_query,
                                dependencies=restart.prometheus.dependency_queries)
    return KubernetesRestartPort(executor_id=restart.executor_id, reader=reader, writer=None, signals=signals,
                                 evidence_dir=DISABLED_EVIDENCE_DIR, now=now,
                                 argo_application=(observe.argo_namespace, observe.argo_application))


def is_restart_candidate(entry):
    """The thread's frozen WF05 plan is the restart command candidate (contract unchanged)."""
    if entry is None or not entry.get('context'):
        return False
    plan = json.loads(entry['context']).get('recovery_plan') or {}
    return (plan.get('action_id') == 'deployment-restart' and plan.get('method') == 'command'
            and plan.get('assessment') == 'unsupported' and plan.get('reason') == 'action_not_implemented')


def _code(error):
    # Never echo exception text; only exact ValueError/OSError codes are compared.
    return str(error) if type(error) in (ValueError, OSError, TimeoutError) else ''


def _gates(error):
    names = [n for n in rejected_gates(error.results) if GATE_NAME.fullmatch(n)]
    return RESTART_GATES.format(names=', '.join(names) or '확인 불가')


class RestartWiring:
    def __init__(self, adapter, config, *, read=None, connection_factory=None):
        restart, observe = config.restart, config.observe
        if restart is None or observe is None:
            raise ValueError('restart_wiring_not_configured')
        self.port = None  # read-only port (None when a test supplies ``read``)
        if read is None:
            port = self.port = make_restart_reader(observe, restart, now=adapter.tasks.queue.now,
                                                   connection_factory=connection_factory)

            def read(plan, *, deadline):
                return port.read(deadline=deadline)
        self.adapter = adapter
        queue = adapter.tasks.queue
        self.coordinator = RestartCoordinator(queue, source=restart_source_index(adapter), read_current=read,
                                              executor_id=restart.executor_id,
                                              execution_seconds=restart.execution_seconds)
        self.ledger = RestartApprovalLedger(queue, restart.approval, load_plan=self.coordinator.load, observe=read)
        self._cursor = ''

    def candidate_of(self, restart_plan_hash):
        with self.adapter.tasks.queue.transaction() as db:
            row = db.execute('SELECT candidate_hash FROM restart_plans WHERE plan_hash=?',
                             (restart_plan_hash,)).fetchone()
        return row['candidate_hash'] if row else None

    def request(self, candidate_hash, actor, *, fixed, until):
        """Reply text only. ``fixed`` are the PR path's shared texts (not observed / full)."""
        try:
            plan = self.coordinator.prepare(candidate_hash, actor=actor)
            view = self.ledger.open(plan.plan_hash, actor=actor)
        except RestartGateRejected as error:
            return _gates(error)
        except (ValueError, OSError, TimeoutError) as error:
            code = _code(error)
            if code in STALE_CODES:
                return RESTART_STALE
            if code in FULL_CODES:
                return fixed['full']
            if code in NOT_OBSERVED_CODES or isinstance(error, (OSError, TimeoutError)):
                return fixed['not_observed']
            return RESTART_UNAVAILABLE
        except (KeyError, TypeError, IndexError):
            return fixed['not_observed']  # malformed API response: never trusted, never echoed
        if view['state'] == 'awaiting_approval':
            return RESTART_OPENED.format(until=until(view['wait_until']), plan=plan.plan_hash[:12])
        return RESTART_EXISTING.format(state=view['state'])

    def page(self, limit):
        views, self._cursor = self.ledger.page(after=self._cursor, limit=limit)
        return views
