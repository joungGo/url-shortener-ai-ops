# A7-1 복구 승인 Slack 버튼 수신 로컬 구현 · 2026-11-19

상태: **로컬 구현·검증 완료 / 미커밋.** 기준 main `7b6dcb19caca9156c492cb51c19a5a3a35cfd76a`. A7(WF07~09 실제 어댑터)의 첫 조각이다.

## 목적

WF06 승인 장부(`RecoveryApprovalLedger`, `RestartApprovalLedger`)는 지금까지 로컬 호출로만 승인·거절·취소를 받았다. WF06 설계(D3)에서 정한 대로 **서명된 Slack interactivity(버튼)**를 장부 결정으로 연결한다. 자연어 "approve" 메시지는 승인 경로가 아니다.

## 구현

- `ai_ops.recovery_slack_approval`
  - `button_value(scope, plan_hash, approval_hash, generation)` / `parse_button`: 버튼 값에 정확한 장부 대상을 `v1:scope:plan:approval:generation` 형식으로 담고 엄격히 다시 읽는다. `action_id`는 `recovery_prepare_*`(준비 승인)·`recovery_execute_*`(명령 실행 승인)로 범위를 나눠, 한 범위의 버튼이 다른 범위의 장부를 건드릴 수 없게 했다.
  - `SlackApprovalReceiver.receive`: Slack v0 서명·300초 시각·정확한 form content type 검증 → `payload` 필드 하나만 있는 form(중복·추가 필드·중복 JSON 키 거절) → `block_actions`, 팀·App, enterprise 설치 아님, 사용자 팀, bot 아님, 메시지 container와 채널 일치, 버튼 1개, action_ts 형식 확인 → **Slack 정책에 등록된 (사용자, 채널) 쌍만** 허용 → 해당 범위 장부의 `decide`만 호출.
  - `request_id`는 팀·App·사용자·채널·message_ts·action_ts·action_id의 hash다. 같은 전달이 다시 와도 결과가 같고(멱등), 새로 누른 버튼은 새 영수증이지만 이미 결정된 대상을 갱신하지 못한다(장부 규칙).
  - 장부는 계속 자기 승인자 allowlist, 원래 요청 출처, 정확한 approval_hash·generation, 기한을 다시 검사한다. 수신기는 재검증·준비·실행·전송을 하지 않으며 결과는 항상 `execution_allowed=false`다. 오류에는 요청 내용을 담지 않는다.
- `ai_ops.slack_ingress.SlackInteractivityApp`: 기존 `SlackEventsApp`의 HTTPS 전용·POST·Content-Length 상한·2.5초 ACK·동시 처리 4개 제약을 그대로 쓰고 경로만 `/slack/interactivity`다. `SlackEventsApp`은 경로를 클래스 속성으로, 처리 함수를 요청마다 찾는 `_handle`로 바꿨다(동작 동일).
- `ai_ops.slack_adapter.verify_slack_request`: 기존 `_verify`의 서명·시각 검증을 content type만 인자로 받는 공용 함수로 분리했다(동작 동일). Events는 `application/json`, interactivity는 form을 요구한다.

## 범위 밖(다음 조각)

- 버튼이 있는 승인 요청 메시지 **전송**과 결과 회신은 A7-2(결과 outbox)에서 한다. 현재 sender 계약은 텍스트 회신만 허용하므로 그때 형식을 확장한다.
- 수신기를 실제 ingress 프로세스에 연결하는 것(승인 정책·계획 출처 설정 파일, `/slack/interactivity` 경로 mount)과 Slack App의 Interactivity URL 설정은 장부를 프로세스에 연결하는 조각에서 함께 한다. 지금은 클래스와 ASGI 앱만 있고 배포 설정은 바꾸지 않았다.

## 검증

- 신규 `test_recovery_slack_approval.py` 11개(실제 WF05/06 fixture 재사용)
  - 서명된 승인이 결정만 기록하고 재검증 관측을 일으키지 않음, 거절·취소
  - 재전달 멱등·두 번째 누름이 기한을 갱신하지 못함, 오래된 대상 거절
  - 서명·시각·content type, form·JSON 형식, 식별 결합 15종, 정책 쌍·승인자 allowlist
  - 미구성 범위와 생성자 검사, 버튼 값 왕복·엄격성, ASGI 경로·HTTPS·거절
- 진행 중 발견해 고친 것: 처음에 `SlackEventsApp`이 `adapter.receive`를 생성 시점에 묶도록 바꿨는데, 이 때문에 기존 시험(`adapter.receive`를 나중에 바꿔 끼우는 경우)이 끝나지 않았다. 요청마다 찾는 방식으로 되돌렸고 Slack 회귀가 모두 통과한다.
- 저장소 경계·diff·compileall·로컬 Slack 점검 통과. 전체 시험 결과는 [검증 기록](recovery-slack-approval-local-validation-20261119.json)에 있다.

실제 Slack 전송·배포·인프라·장애 실습 없음. 기존 시간·실행별 승인·정확 head 리뷰/CI·잠금·원복 조건 변경 없음.
