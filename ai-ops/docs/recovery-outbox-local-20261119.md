# A7-2 복구 결과 outbox 로컬 구현 · 2026-11-19

상태: **로컬 구현·검증 완료 / 미커밋.** 기준 main `0619651393b5792da03f9a86d561e502560d5e81`. A7의 두 번째 조각이다.

## 목적

A7-1로 Slack 버튼 결정을 받을 수 있게 됐지만, 버튼이 달린 승인 요청 메시지와 이후 상태(승인·거절·취소·만료·사람 확인 필요·준비 완료)를 사용자에게 알리는 경로가 없었다. 이 조각은 승인 장부 상태를 **원래 계획 요청의 Slack thread**로 보내는 outbox를 만든다.

## 구현

- `ai_ops.recovery_outbox.RecoveryOutbox`(Slack DB의 `recovery_outbox` 테이블)
  - `message_for(request_id, view)`: 장부 view만으로 메시지를 만든다. 문구는 고정 템플릿이고 사용자 입력 텍스트는 넣지 않는다. 사유 코드는 `[a-z_]` 형식일 때만 덧붙인다.
    - `awaiting_approval` → 승인 요청과 [승인][거절] 버튼, 응답 가능 기한(UTC) 표시
    - `approved` → 기록 알림과 [취소] 버튼
    - `rejected`·`cancelled`·`expired`·`superseded`·`already_recovered`·`needs_review`·`proposal_ready` → 텍스트 알림만
    - `revalidating` 같은 중간 상태 → 보내지 않음
  - 수신처는 원래 Slack 요청 행의 채널·thread이며, 그 요청이 지금도 인가된 경우에만 사용한다. view는 `execution_allowed=false`, 범위·hash·generation 형식을 확인한다.
  - `enqueue`: 키 = hash(kind, 범위, plan, approval, generation, 상태). 같은 키는 한 번만 저장하고, 같은 키에 다른 내용이 오면 거절한다. 행 상한은 10,000개다.
  - `deliver_one(transport)`: 신원이 확인된 transport만 쓰고, 기존 회신과 같은 처리 잠금 안에서 한 건만 보낸다.
    - 429는 최대 3회까지 `retry_after` 뒤 재시도한다.
    - 4xx나 Slack의 확정 거절(`ok:false`)은 `failed`다.
    - 응답이 보낸 내용과 일치하면 `delivered`로 기록하고 `message_ts`를 남긴다.
    - 그 밖의 모호한 결과와 예외는 `needs_review`다.
    - 보내는 도중 중단돼 `sending`으로 남은 행은 다음 실행 때 `needs_review`로 바꾸고 **다시 보내지 않는다**.
    - 요청 인가가 철회됐거나 thread가 바뀌었으면 `withdrawn`으로 두고 보내지 않는다.
- 버튼 블록 계약(`recovery_slack_approval.approval_blocks`·`blocks_valid`)
  - 블록은 고정 라벨(승인/거절/취소)과 정확한 장부 대상 값으로만 만든다.
  - 검증할 때는 버튼 값으로 블록을 **다시 만들어 완전히 같을 때만** 허용한다. 문구 변경, 버튼 추가, 다른 대상 값, URL 버튼, 블록 추가, mrkdwn, fallback 텍스트 불일치는 거절한다.
- `slack_web.post_payload_valid`: 기존 텍스트 회신 계약에 위 정규 블록만 선택적으로 허용한다. B′ sender도 같은 검사를 쓰므로, 정규 블록만 Slack으로 나간다.

## 범위 밖(다음 조각)

- 장부가 열리거나 결정될 때 outbox에 넣고 relay가 `deliver_one`을 호출하도록 **프로세스에 연결하는 일**은 승인 정책·계획 출처 설정과 함께 이후 조각에서 한다. 지금은 클래스와 계약만 있고 런타임 설정은 바꾸지 않았다.
- 고정 명령 실행 결과(WF08)와 효과 검증(WF09) 알림은 A7-3·A7-4에서 같은 outbox 형식을 확장한다.

## 검증

- 신규 `test_recovery_outbox.py` 8개(실제 WF05/06 fixture + 가짜 Slack wire)
  - 원래 thread로 정규 버튼과 함께 전달, enqueue 멱등과 전달 후 재전송 없음
  - 상태 알림(승인→취소 버튼, 취소 텍스트, 중간 상태 미전송)
  - 429 재시도 후 3회 실패, 모호한 결과·중단 후 재전송 없음, 확정 거절·인가 철회
  - 잘못된 view·미검증 transport 거절, 정규 블록만 transport·sender 통과(변조 8종 거절)
- Slack·복구 관련 회귀 224개 통과. 저장소 경계·diff·compileall 통과. 전체 결과는 [검증 기록](recovery-outbox-local-validation-20261119.json)에 있다.

실제 Slack 전송·배포·인프라·장애 실습 없음. 기존 시간·실행별 승인·정확 head 리뷰/CI·잠금·원복 조건 변경 없음.
