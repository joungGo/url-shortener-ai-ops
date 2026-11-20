# A7 프로세스 연결 조각 1: Slack 복구 승인 경로 · 2026-11-20

상태: **로컬 구현·검증 완료, PR CI/main 대기.** 기준 main `9e70a3dd96fd1d51453b3b61afba64797c5791da`. 기본 비활성이며, 현재 상태 관측이 연결되지 않아 실제 배포에서는 승인 대기가 열리지 않는다(fail-closed).

## 배경

A7-1(Slack 승인 버튼 수신)·A7-2(결과 outbox)·WF06(승인 장부)은 시험에서만 조립되고, 실제 프로세스(`slack_server` ingress/relay)에는 연결되지 않았다. 조사 중 확인한 공백은 세 가지다.

- **승인 시작 경로가 없다.** `plan-recovery` 회신 뒤 `RecoveryApprovalLedger.open`을 부르는 코드가 없다.
- **장부의 `load_plan`이 요청 단위다.** `slack_plan_source(adapter, request_id)`는 요청 하나에 묶여 있어, 프로세스 전체에서 쓰려면 계획 hash → 요청 색인이 필요하다.
- **`observe`는 실클러스터 읽기가 필요하다.** PR 경로의 현재 상태 관측기는 아직 없다.

사용자 결정(2026-11-20):

- 승인 대기는 **명시 명령**으로만 연다. 계획 회신만으로 자동으로 열지 않는다.
- 첫 조각은 승인 경로(시작 명령·색인, ingress의 interactivity 장착, relay의 outbox 전송)로 한다.

## 구현

### 1. `request-approval` 명령 (`slack_adapter`·`slack_events`·`recovery_approval_wiring`)

형식은 `@bot request-approval <사건 참조|this> <계획 hash 64자>`다. 기존 `plan-recovery <사건 참조> <job> [조치]` 문법과 같은 모양이다.

- **ACK 시점:** 인자는 정확한 64자 소문자 hex만 받는다. 추가 문구, 짧은 hash, 대문자, `approve` 같은 단어는 거절한다. 사건 참조는 기존 bridge 해석(this 포함)을 그대로 쓴다.
- **relay 시점** (`RecoveryApprovalService.request`):
  1. 계획 hash로 살아 있는 `plan-recovery` 요청을 정확히 하나 찾는다. 요청자는 명령을 보낸 사람이어야 한다.
  2. 그 계획 요청과 같은 채널·스레드·사건이어야 한다. 아니면 같은 문구(`UNAVAILABLE`)로 거절한다. 존재 여부를 드러내지 않는다.
  3. `ledger.open(plan_hash, actor)`를 부른다. 승인자 목록·보관 기간·원본 결과 확인은 WF06 장부와 `slack_plan_source`가 그대로 한다.
  4. 결과는 고정 문구로만 회신한다. 예외 문구는 내보내지 않는다.
     - 대기 열림 → 마감 시각과 "버튼 메시지를 곧 보냄"
     - 이미 결정된 계획 → 상태만 알리고 다시 열지 않음
     - 관측 실패 → "현재 상태를 확인하지 못해 열지 않음"
     - 현재 상태가 근거와 다름 → "새 계획 필요"
     - 그 밖 → `UNAVAILABLE`
- **기능이 꺼져 있으면** "이 배포에서는 활성화되어 있지 않음"으로 회신하고 장부를 만들지 않는다.
- 승인 버튼 메시지는 이 명령이 직접 보내지 않는다. 아래 동기화가 보낸다. 중간에 중단돼도 다음 동기화가 다시 넣는다.

### 2. 계획 색인 (`plan_request_id`, `slack_plan_index`)

Slack DB에서 `plan-recovery` 요청 중 고정된 계획의 `plan_hash`가 일치하는 만료 전 요청을 찾는다(`json_extract`). 정확히 1건일 때만 쓴다. 이후 검증은 기존 `slack_plan_source`가 한다.

### 3. 장부 → outbox 동기화 (`RecoveryApprovalLedger.page`, `sync_once`)

- `page(after, limit)`: 보관 기간 안의 기록을 plan_hash 순서로 최대 50건 읽는다. `show`와 같은 시간 전이(만료·시간 초과)를 적용한다. 열기·결정·재검증은 하지 않는다.
- `sync_once`: 각 기록의 현재 상태를 A7-2 outbox에 넣는다. outbox 키가 (계획, 생성 번호, 상태)라 같은 상태는 한 번만 들어간다. 요청이 더 이상 인가되지 않으면 건너뛴다.

### 4. 프로세스 연결 (`slack_server`)

- **설정:** `/etc/ai-ops/recovery-approval.json`(`schema_version` 1, `enabled`, `prepare_proposal` 승인 정책).
  - 파일이 없거나 `enabled: false`면 기존 동작 그대로다.
  - 내용이 잘못됐거나, 승인자가 Slack grants에 없거나, scope가 다르면 시작과 `check-config`가 실패한다.
  - `check-config`는 `recovery_approval` 활성 여부를 출력한다.
- **ingress:** 활성화되면 `SlackRouter`가 `/slack/events`와 `/slack/interactivity`를 같은 TLS 리스너에서 나눠 받는다.
  - 버튼 수신기는 ingress가 이미 가진 서명 키만 쓴다. bot 토큰은 읽지 않는다.
  - 경로를 모르면 기본 앱이 403으로 거절한다.
  - 종료할 때는 두 앱의 진행 중인 처리 스레드를 모두 기다린다.
- **relay:** 활성화되면 어댑터에 서비스를 붙여 `request-approval`을 처리한다. 매 틱마다 기존 회신 처리 뒤에 다음을 한다.
  - 5초마다 `sync_once`
  - 전송은 틱당 최대 1건(`deliver_once`)
  - 실패는 삼키고 다음 틱에 다시 한다. 결과가 불명확한 전송은 outbox 규칙대로 재전송하지 않는다.
  - 종료 신호가 오면 전송하지 않는다.
- **관측 포트:** 기본값은 `observation_not_connected`(항상 실패)다. 프로세스 진입점은 다른 포트를 넘기지 않는다.

## 알려진 동작과 한계

- **실제 배포에서는 아직 승인 대기가 열리지 않는다.** `open`에는 새 관측이 필요한데 관측 포트가 연결되지 않았다. 검증된 PR 경로 현재 상태 관측기를 연결하는 것은 후속 조각이다.
- **승인 뒤 재검증·PR 준비 호출이 없다.** 승인되면 `approved_until`이 지나 만료되고, 만료 알림이 간다. 재검증과 WF07 연결은 후속 조각이다.
- **승인자는 계획 요청자 본인이어야 한다.** 기존 WF06 `slack_plan_source` 제한이다.
- **명령 실행 승인(`execute_command`)은 연결하지 않았다.** 재시작 조정기와 함께 연결한다. 그 scope의 버튼은 `approval_scope_unavailable`로 거절된다.
- **정책을 바꾸면 명시적 전환이 필요하다.** 승인 정책 버전이나 내용이 바뀌면 장부가 `approval_policy_requires_explicit_transition`으로 시작을 거부한다(기존 WF06 동작). `replace_policy` 전환 절차는 연결하지 않았다.
- **ingress·relay 동시 접근.** 버튼 처리 중 Slack DB 잠금이 겹치면 503을 돌려준다. Slack은 interactivity를 재전송하지 않으므로 사용자가 다시 누른다. 같은 누름은 결정적 request_id로 멱등이다.
- **상태가 바뀐 뒤 전송.** 승인 요청이 아직 전송 대기 중일 때 상태가 바뀌면(예: 만료), 버튼 메시지와 상태 알림이 차례로 갈 수 있다. 늦게 누른 버튼은 장부가 만료로 처리한다.
- **보관 정리 tick**(`RecoveryRetention`, A7-5)은 아직 relay에 연결하지 않았다. 다음 조각이다.
- **renderer와 Slack 앱 설정.** WF10 renderer는 `recovery-approval.json`을 아직 마운트하지 않는다(기본 꺼짐 유지). Slack 앱의 Interactivity Request URL 설정은 실제 연결 입력이다.

## 검증

신규 `test_recovery_approval_wiring.py` 17개. 실제 WF05/06 Slack·큐·worker fixture와 가짜 시계, 가짜 Slack wire로 검증했다. 네트워크는 쓰지 않는다.

- **정상 경로:** 명령 → 대기 열림 → 동기화 1건 → 계획 스레드에 승인·거절 버튼 1회 전송. 이후 동기화와 전송은 0건이다. 관측은 열 때 1회뿐이고 동기화는 관측하지 않는다. 조사 job은 1개다.
- **반복과 결정:** 같은 명령을 반복해도 다시 열지 않고 마감도 늘지 않는다. 결정된 계획은 다시 열리지 않는다.
- **fail-closed:** 기본 관측 포트면 대기가 열리지 않고 메시지도 없다. 현재 이미지가 다르면 새 계획을 요구한다. 기능이 꺼져 있으면 장부를 만들지 않는다.
- **거절:** 모르는 hash와 다른 스레드는 같은 문구로 거절되고 관측도 0회다. Slack grants 밖 승인자는 설정 단계에서 거절된다. 인자는 정확한 hash만 받는다(ACK 거절).
- **버튼 이후:** 승인 버튼 → 결정 기록만 → 동기화 → 취소 버튼만 있는 승인 알림. `approved_until`이 지나면 만료 알림이 간다.
- **라우터:** `/slack/interactivity`는 결정을 기록하고, 모르는 경로는 403이고, 같은 경로를 중복 등록하면 거절한다.
- **relay tick:** 5초 간격 동기화, 틱당 전송 1회, 실패 격리, 종료 중 전송 없음.
- **장부 page:** cursor 동작, 보관 경계에서 제외, 잘못된 인자 거절.
- **서버:** 설정이 없거나 꺼져 있으면 기존 ingress 그대로다. 켜져 있으면 bot 토큰과 네트워크 없이 라우터가 뜬다. 잘못된 설정과 grants 밖 승인자는 `check-config`가 실패하고 사용자 ID를 출력하지 않는다.
- **결함 변형 12종 모두 검출:**
  - 스레드 확인 제거
  - 기본 관측 포트가 성공하도록 변경
  - 관측 실패 문구 혼동
  - 승인자 grants 확인 제거
  - 색인 요청자 확인 제거
  - page 보관 경계 완화
  - 종료 중 전송
  - 동기화 간격 제거
  - ACK 인자 검사 제거
  - 비활성 문구 혼동
  - ingress 라우터 미장착
  - `check-config` grants 확인 제거

전체 결과는 [검증 기록](recovery-approval-wiring-local-validation-20261120.json)에 있다.

실제 클러스터·GitHub 쓰기·배포·Slack·장애 실습 없음. 기존 시간·실행별 승인·정확 head 리뷰/CI·잠금·원복 조건 변경 없음.
