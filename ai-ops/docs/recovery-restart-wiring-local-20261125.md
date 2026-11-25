# A7 재시작(명령) 경로 연결: 실행 직전까지 · 2026-11-25

상태: **로컬 구현·검증 완료, PR CI/main 대기.** 기준 main `8db08787f34a460c251e2fb6ed36fd88c9341be4`(PR63).

## 결정 (사용자 2026-11-25, 모두 추천안)

| 질문 | 결정 |
|---|---|
| 어디까지 연결하나 | **실행 직전까지.** 요청 → 재시작 계획 봉인 → 명령 승인 → 재검증 → "실행 조건 충족"까지 연결한다. 실제 Deployment PATCH는 계속 비활성이다. |
| 무엇으로 시작하나 | 기존 **`request-approval` 재사용.** 스레드의 계획이 재시작 후보면 재시작 경로로 간다. |
| 읽기 신원 | **관측기 읽기 신원 확장.** 같은 Kubernetes 읽기 신원에 events·HPA 읽기를 더하고, Prometheus 읽기 설정을 추가한다. |
| 계획 회신 문구 | **회신 문구만 수정.** WF05 계약(판정 `unsupported`, 사유 `action_not_implemented`, 계획 hash)은 그대로 둔다. |

## 흐름

```
plan-recovery <job> deployment-restart   → WF05 명령 후보 (회신: "명령 후보 · 조건은 승인 요청 시 판정 · 실행 비활성")
@봇 request-approval this <후보 hash>     → (relay) 후보 확인 → WF08 coordinator.prepare
                                             · 현재 상태 1회 읽기 + 재시작 게이트 → 재시작 계획 봉인
                                           → 명령 승인 장부 open (현재 상태 1회 더 읽기)
                                           → "재시작 승인 대기를 열었습니다(… 재시작 계획 <12자>)"
relay sync                                → [고정 복구 명령 실행] 승인·거절 버튼 (recovery_execute_*)
버튼                                       → ingress가 명령 승인 장부에 결정만 기록
relay advance                             → 승인된 건 재검증(읽기 1회, 게이트 재평가)
                                           → command_ready → "실행 조건이 충족되었습니다. 실제 재시작 실행은
                                             실환경 실행 결정 전까지 비활성이며, 아무것도 변경하지 않았습니다."
```

- **실행은 하지 않는다.** 아무 코드도 `execute_local`을 부르지 않는다. 연결된 포트는 쓰기 신원이 없는 읽기 전용(`writer=None`, `mode='kubernetes'`)이다. WF08 실행기는 원래 `local_fixture` 포트만 받으므로, 실수로 불려도 거부된다.
- **PR 경로와 분리된다.** 장부(`restart_approval*`), 정책, 버튼 접두사(`recovery_execute_`), 알림 머리말(`[고정 복구 명령 실행]`)이 모두 따로다. 재시작 후보는 PR 장부로 가지 않고, PR 후보는 재시작 경로로 가지 않는다(`is_restart_candidate`는 WF05 계약 네 값이 모두 맞을 때만 참).
- **재시작 경로가 꺼져 있으면**(설정에 `restart` 절 없음) 재시작 후보에 "재시작 경로가 켜져 있지 않아…"로만 답하고, 읽기도 기록도 하지 않는다.

## 회신 문구

| 상황 | 회신 |
|---|---|
| 열림 | 재시작 승인 대기를 열었습니다(응답 마감, 재시작 계획 hash 앞 12자). 승인 뒤에도 재확인만 하고 실행하지 않음 |
| 게이트 거절 | 현재 상태가 재시작 조건을 통과하지 못함 + **통과하지 못한 조건 이름**(예: `no_competing_controller`). 값은 싣지 않음 |
| 조사 근거가 오래됨·작업 대상이 바뀜 | 조사 관측 후 2분 이내여야 함(WF08 기존 규칙) 또는 UID/Pod/이미지가 달라짐 → 새 조사·계획 요청 |
| 읽기 실패·응답 모양 이상 | 기존 "현재 상태를 확인하지 못해…" |
| 장부 가득 참 | 기존 문구 |
| 그 밖의 코드 | 고정 "재시작 승인 대기를 열 수 없습니다…". 예외 문구는 내보내지 않음 |
| 이미 처리됨 | 상태와 함께 "같은 계획으로 다시 열지 않음" |

알림은 명령 범위 전용 문구를 쓴다(`COMMAND_NOTICES`). 실행 조건 충족(`proposal_ready` 상태)과 준비 뒤 관측 만료에서 "PR" 문구가 나가지 않는다.

## 설정 (`recovery-approval.json`의 선택 `restart` 절)

```json
"restart": {
  "approval": {"version": 1, "scope": "execute_command", "approvers": ["U…"],
               "wait_seconds": 7200, "approval_seconds": 300,
               "observation_seconds": 30, "revalidation_seconds": 3},
  "executor_id": "local-restart-lab",
  "execution_seconds": 10,
  "prometheus": {"api_url": "https://prometheus.monitoring.svc:9090",
                 "token_file": "/run/…/token", "ca_file": "/run/…/ca.crt",
                 "prior_ready_query": "…$TEMPLATE_HASH…",
                 "dependency_queries": ["…"]}
}
```

- `observe` 절이 있어야 한다(같은 Kubernetes 읽기 신원).
- `execution_seconds + approval.revalidation_seconds ≤ 15`. `request-approval` 한 번에 relay가 읽기를 두 번(prepare·open) 하므로, 한 tick이 relay 진행 기준(30초) 안에 머물게 한다.
- 승인자는 Slack grants에 있어야 한다(서비스와 `check-config`에서 확인).
- Prometheus 질의가 없거나 결과가 없으면 신호가 `unknown`이 되어 게이트가 막는다.
- `check-config`에 `recovery_restart_configured`가 추가됐다.

## 함께 바꾼 것

- **정책 전환 명령이 범위별로 동작한다.** `transition-approval-policy`가 PR 준비와 재시작 명령 두 장부를 모두 다룬다.
  - 장부가 있고 정책이 바뀐 범위만 전환한다. 그대로인 범위는 `unchanged`로 둔다.
  - 모든 범위를 먼저 검사한 뒤에 바꾼다. 하나라도 이전 hash가 맞지 않으면 아무것도 바꾸지 않는다.
  - 바뀐 범위가 하나도 없으면 `nothing_to_transition`이다.
- **결함 수정: 재시작 기록 장기 정리.** WF08 재시작 기록(`restart_runs`, 상한 10,000)이 보관 종료 뒤 "expired"로만 남아 영구히 쌓였다. 이번 조각부터 실제 프로세스에서 쓰이므로, 승인 장부와 같은 규칙으로 정리한다. 보관 종료 30일(`TOMBSTONE_SECONDS`) 뒤에 삭제한다.
  - 실행 중복 방지 기록(`restart_consumed`)은 지우지 않는다. 실행 때만 생기며, 이번 조각에서는 생기지 않는다.
  - 참조가 사라진 증거 파일은 기존 규칙대로 보관 기간이 지났을 때만 지운다.
- **보관 정리 대상 추가:** relay 보관 정리 대상에 명령 승인 장부와 재시작 coordinator를 넣었다. 증거 폴더는 넣지 않았다(실행하지 않아 증거 파일이 생기지 않는다).

## 남은 것

1. **실제 실행(쓰기)**: Deployment patch·Lease 쓰기 신원, 증거 저장 폴더, 실행기 프로세스, 효과 관찰 연결. 실환경 실행 결정(WF11) 대상이다.
2. **renderer**: WF10 renderer가 아직 `restart` 절과 Prometheus 읽기 Secret을 렌더링하지 않는다. 메인 Pod 송신 규칙은 이미 수집기 연결(Prometheus 포함) 목적지를 허용한다.
3. **RBAC**: 관측기 읽기 신원에 `events` list, `horizontalpodautoscalers` list(autoscaling/v2)를 더해야 한다. Argo `applications` get은 기존 그대로 쓴다.
4. 정상 판정, 승인자=요청자 제한, HA는 기존 한계 그대로다.

## 검증

신규 `test_recovery_restart_wiring.py` 24개. 실제 WF05/06/08 Slack·큐·worker fixture, 가짜 시계, 가짜 Slack wire를 쓰고, 현재 상태 읽기만 가짜로 넣었다. 실제 읽기 포트는 기존 가짜 Kubernetes API(`FakeApi`)로 따로 확인했다.

- **계획 회신:** 명령 후보 표시. 계약 값은 그대로이고, `request-approval this <hash>`를 안내한다. "미지원"과 "새 복구안 요청"은 나오지 않는다.
- **요청**
  - 봉인과 open으로 읽기 2회, 명령 버튼 2개가 계획 스레드에 간다. PR 장부에는 기록이 없다. 반복 요청은 다시 봉인하거나 다시 열지 않는다. 거절된 건은 다시 열리지 않는다.
  - 게이트 거절 시 조건 이름이 나간다. 오래된 근거·작업 대상 변경은 새 조사 안내로 간다.
  - 읽기 오류 4종(OSError·Timeout·KeyError·TypeError)은 같은 고정 문구로 닫히고, 오류 문구가 새지 않는다. 장부 가득 참, 알 수 없는 코드, 재시작 경로 꺼짐도 확인했다.
- **실행 조건 충족:** 서명된 버튼 → 재검증 → `command_ready`. 그 뒤 알림에 "PR"이 없고, 더 진행하지 않는다. 재시작 기록은 `prepared`, `applied=false`, 잠금 없음 그대로다. 재검증 중 게이트가 실패하면 `needs_review`가 된다. 관측 만료 문구를 확인했고, 명령 버튼은 재시작 장부가 없는 프로세스에서 거부된다.
- **보관:** 보관 종료에 expired, 30일에서 1초 모자란 시점에는 유지, 30일에 기록·계획·승인 tombstone이 삭제된다.
- **설정:** observe 필수, relay 예산, 명령 scope, Prometheus 경로·https, executor_id, 승인자 grant를 확인했다.
- **서버 진입점:** `check-config` 플래그, relay 보관 정리 대상, ingress router, 읽기 전용 포트(`writer=None`)를 확인했다.
- **읽기 포트:** 가짜 API에 GET만 간다. patch는 쓰기 client가 없어 실패한다.
- **정책 전환:** 재시작 정책만 바뀌면 그 범위만 전환되고 PR 범위는 `unchanged`다. 이전 hash가 틀리면 아무것도 바뀌지 않는다. 정책이 그대로면 `nothing_to_transition`이다.

**변형 검사:** [검증 기록](recovery-restart-wiring-local-validation-20261125.json)에 있다.

실제 클러스터·배포·Slack·장애 실습 없음.
