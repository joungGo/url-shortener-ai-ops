# A7 프로세스 연결 조각 3: 승인 뒤 자동 재검증과 WF07 PR 준비 연결 · 2026-11-21

상태: **로컬 구현·검증 완료, PR CI/main 대기.** 기준 main `a5f59dc085b08114baeb44d950ab616e709dcace`(PR55).

## 배경

조각 1(PR54)에서는 승인 버튼이 결정만 기록했다. 재검증(`ledger.revalidate`)을 부르는 곳이 없어서, 승인된 기록은 `approved_until`이 지나면 만료됐다. WF07 `ImageRecoveryPRBridge.prepare`도 프로세스에 연결되지 않았다.

사용자 결정(2026-11-21):

- 재검증은 **승인 후 자동**으로 한다. 승인 알림 문구가 이미 "실행 전 현재 상태를 다시 확인한다"고 안내한다.
- WF07은 **자동 준비까지** 한다. GitHub에 실제 PR을 만드는 것은 범위 밖이다.

## 구현

### 1. `RecoveryApprovalService.advance_once` (relay, 5초 주기마다 최대 1단계)

- **`approved` 기록:** 승인자(=계획 요청자) 신원으로 `ledger.revalidate`를 1회 호출한다. 읽기 전용이다.
  - 결과는 `proposal_ready`, `already_recovered`, `superseded`, `needs_review` 중 하나다(기존 WF06 규칙).
- **`proposal_ready` 기록이고 WF07 기록이 아직 없을 때:** `pr_bridge.prepare`를 1회 호출해 정확한 diff와 PR 내용을 로컬에 준비한다.
  - 결과는 `prepared`, `no_pr`, `needs_review`다.
  - `publish_local`(GitHub 쓰기)은 호출하지 않는다.
- **같은 단계를 반복하지 않는다.**
  - 재검증 중 중단되면 기록이 `probe_until` 뒤 `needs_review`가 된다.
  - WF07 기록이 한 번 생기면 다시 준비하지 않는다. 실패하면 WF07이 스스로 `needs_review`로 남긴다.
- **한 번 호출에 한 단계만 한다.** 재검증 시간(`revalidation_seconds`)은 15초 이하로 제한한다(`RELAY_BLOCK_SECONDS`, 설정 검증 단계에서 거절). relay 상태 확인 기준(30초) 안에 한 틱이 끝나게 하기 위해서다. WF07 준비 시간도 기존 상한이 15초다.
- **대상만 고른다.** 장부 `page(states=...)`로 `approved`와 `proposal_ready`만 읽는다. 대기 중이거나 끝난 기록은 건드리지 않는다.

### 2. WF07 결과 알림 (`RecoveryOutbox.enqueue_pr`)

- `prepared`, `no_pr`, `needs_review`, `cancelled`만 고정 문구로 알린다.
- PR URL, head, diff, 자유 문구는 넣지 않는다. 사유 코드는 `[a-z_]` 형식일 때만 붙인다.
- 키는 (`pr_<상태>`, 계획, 생성 번호, 상태)라서, 같은 상태는 한 번만 저장한다.
- `sync_once`는 장부 상태 알림 뒤에 WF07 기록이 있으면 그 상태도 넣는다.

### 3. relay 순서 (`slack_server.recovery_tick`)

5초마다 `advance_once`(종료 중이면 생략)를 하고 `sync_once`를 한다. 그 뒤 틱마다 전송을 1건 한다. 진행 결과는 같은 주기에 알림으로 들어간다.

### 4. 보관 정리

WF07이 붙어 있으면 보관 정리 tick이 WF07 기록의 `prune`도 같이 부른다. 보관 기간이 지나면 복사해 둔 제안·PR 내용을 지운다.

### 5. 연결 경계

- `pr_bridge`는 `ledger -> ImageRecoveryPRBridge`를 만드는 factory다. 같은 장부를 감싼 bridge만 받는다.
- **프로세스 진입점은 넘기지 않는다.** GitHub 읽기, CI·ECR·리소스 재확인, Slack→GitHub ID 매핑 포트의 실제 구현이 아직 없기 때문이다. 그래서 실제 배포에서는 WF07 준비가 연결되지 않는다.
- 관측 포트도 여전히 미연결이다. 실제 배포에서는 승인 대기 자체가 열리지 않는다.

## 알려진 동작과 한계

- **준비 뒤 관측 만료 알림.** 준비가 끝난 뒤 장부의 관측 유효 시간(`fresh_until`)이 지나면 장부가 `needs_review`(`observation_expired`)가 되고, 그 알림이 이어서 간다. 발행하려면 새 재검증이 필요하다는 실제 상태다. 다만 "준비 완료" 뒤에 "사람 확인 필요"가 와서 혼동을 줄 수 있다. 문구 정리는 발행 경로를 연결할 때 함께 한다.
- **GitHub ID 매핑이 없는 요청자.** 준비가 WF07 기록 없이 거절된다. `fresh_until`까지 5초마다 같은 거절이 반복될 수 있다(읽기 없이 즉시 거절).
- **WF07 `preparing`에서 중단된 경우.** 기록이 그대로 남는다. 기존 `interrupt` 경로가 정리한다(프로세스 미연결).
- **승인자는 계획 요청자 본인이어야 한다.** 기존 WF06 제한이다.

## 검증

신규 시험 11개, 모듈 전체 35개. 실제 WF07 fixture(로컬 Git·증거 재확인·초안 저장소)를 썼고, 네트워크는 쓰지 않았다.

- **끝까지 1회:** 승인 → 재검증 `proposal_ready` → 준비 `prepared`(1회, 이후 증거 읽기 0) → 같은 스레드에 재검증 통과·PR 준비 알림. URL은 없고 GitHub 쓰기는 0회다. 다시 동기화해도 메시지는 늘지 않는다.
- **연결 안 된 관측:** 재검증이 `needs_review`(`read_only_revalidation_failed`)가 되고, 준비는 하지 않는다.
- **WF07 없음:** `proposal_ready` 알림만 가고 준비는 하지 않는다.
- **준비 실패:** 1회만 알리고 재시도하지 않는다. 예외 문구는 노출되지 않는다.
- **매핑 없는 요청자:** WF07 기록과 증거 읽기가 없다.
- **승인 전 기록:** 진행하지 않는다.
- **구성 검사:** 다른 장부를 감싼 bridge는 거절한다. 재검증 시간 16초는 거절한다.
- **PR 알림 형식:** 잘못된 view는 거절하고, 다른 상태는 보내지 않는다. URL과 형식 밖 사유는 빠진다.
- **relay 순서:** 진행 → 동기화 → 전송. 종료 중에는 진행하지 않는다.
- **장부 page 상태 필터.**
- **보관 정리:** WF07 기록이 만료되면 제안·PR 내용이 지워진다.
- **결함 변형 13종을 모두 검출했다.** 새 시험 모듈만으로 다시 확인했다.
  - 재준비 방지 제거
  - 대기 중 기록 진행
  - 한 번에 두 단계
  - bridge 장부 동일성 확인 제거
  - 재검증 시간 한도 제거
  - PR 알림 누락
  - URL 노출
  - 사유 필터 제거
  - PR view 검사 제거
  - relay 순서 뒤바꿈
  - 종료 중 진행
  - page 상태 필터 무시
  - WF07 보관 정리 누락
- **변형 검사 중 일시 실패 1회.** 첫 검사 실행에서 "page 상태 필터 무시" 변형이 검출된 것으로 나왔지만, 실제로는 함께 돌린 다른 시험 모듈의 일시 실패 때문이었다. 확인 결과 그 변형은 살아 있었다. 상태 필터 전용 시험을 추가했고, 이후 실제로 검출된다.

전체 결과는 [검증 기록](recovery-advance-wiring-local-validation-20261121.json)에 있다.

실제 클러스터·GitHub 쓰기·배포·Slack·장애 실습 없음. 기존 시간·실행별 승인·정확 head 리뷰/CI·잠금·원복 조건 변경 없음.

## 최종 반영 결과

[PR #56](https://github.com/joungGo/url-shortener-ai-ops/pull/56) head `e61c4427821e5a4ea9b0be218c28c4d5213067f5`를 head 고정 일반 squash 병합해 main `83b8e39efe3ce02405b1e1e3bb73a49b41471d3f`에 반영했다(사용자 "진행해" 후). [PR CI44881773956](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44881773956) 7종(1742개, skip5 기준선 동일) 성공, tree `3f2a4bfd2a10b17f6f44a15f757ac93dbd4820a7` 일치, [main CI44882341093](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44882341093) 성공(PR전용 gate skip은 설계대로). 보호 변경/우회 없음. 병합 후 branch를 정리했다.

## 정정 (2026-11-24): 변형 검사 근거

처음 기록한 변형 검사 13/13은 **유효한 근거가 아니었다.** 변형 실행기가 최소 환경(`PATH=/usr/bin:/bin`)을 써서 `helm`을 찾지 못했고, 그 결과 WF07 시험 2개(끝까지 흐름, WF07 보관 정리)가 기준선에서부터 실패했다. 기준선이 실패하면 어떤 변형이든 "검출"로 나온다.

병합된 커밋 `83b8e39`를 임시 worktree로 꺼내 다시 검사했다. 이번에는 전체 환경을 상속했고, 기준선 통과를 먼저 확인한 뒤 같은 13종을 돌렸다. 결과는 **13/13 실제 검출**이다. 제품 코드와 결론은 바뀌지 않았다.

실행기는 이후 전체 환경을 상속하고, 기준선이 실패하면 변형 검사를 시작하지 않도록 고쳤다.

다른 조각의 같은 검사는 영향이 없다.

- 조각 1(PR54)과 관측기(PR57): 같은 최소 환경에서도 기준선이 통과했다.
- 조각 2(PR55): 처음 실행에서 변형 3종이 살아남았다. 기준선이 통과했다는 뜻이다.
