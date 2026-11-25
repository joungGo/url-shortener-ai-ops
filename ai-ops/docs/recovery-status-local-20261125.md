# 복구 장부 읽기 전용 조회 명령 · 2026-11-25

상태: **로컬 구현·검증 완료, PR CI/main 대기.** 기준 main `60faf1f4fb9a618c49ab2d6e9e323569ada21a47`(PR69).

## 배경

[WF11 합격 기준표](wf11-acceptance-sheet-draft-20261125.md) 9절 9번 항목이다. 지금까지는 승인·PR 준비·재시작·알림 장부의 상태를 사람이 확인할 수단이 없었다. 그래서 WF11 증거를 남기기 어려웠다. 사용자가 추천대로 진행하기로 했다(2026-11-25).

## 사용법

```
python -m ai_ops.recovery_status --store /data/incidents [--plan <plan hash>] [--limit 50]
```

WF11에서는 relay 컨테이너 안에서 실행한다. 예: `kubectl exec … -c slack-relay -- python -m ai_ops.recovery_status --store /data/incidents`.

## 동작

- **읽기 전용**
  - 기존 SQLite 파일만 `mode=ro`로 연다(큐 DB, Slack outbox DB).
  - 파일·DB·표·행을 만들지 않는다. WAL `-shm` 색인은 SQLite가 열 수 있다.
  - 시간에 따른 상태 전환(만료 등)을 적용하지 않는다. 전환은 relay가 다음 tick에 한다.
  - relay·ingress의 프로세스 잠금을 잡지 않는다. 따라서 두 프로세스가 실행 중이어도 쓸 수 있다.
- **출력(JSON)**
  - `approvals.{prepare_proposal,execute_command}`: 계획마다 plan hash, 저장된 상태, 사유, generation, tombstone 여부, 시각(UTC)을 낸다. 시각은 생성·응답 마감·승인 유효·관측 유효·보관 종료다.
  - `deadlines_passed`: 이미 지났지만 아직 전환되지 않은 시한을 보여 준다.
  - `policies`: 범위별 기록된 정책 hash.
  - `recovery_pr`: WF07 상태, 사유, PR 번호, payload hash.
  - `restart_runs`: 상태, 사유, 적용 여부, Lease 반환 여부, 통과하지 못한 차단 게이트 이름.
  - `outbox`: 종류·상태별 건수.
- **싣지 않는 것**
  - Slack 사용자·채널·스레드 ID, 메시지 본문·payload
  - 승인자 hash, 승인 hash, PR URL, 파일 경로
  - 형식이 맞지 않는 값은 `null`로 둔다. JSON이 깨진 기록은 `unreadable`로 표시한다.
- **실패:** 고정 코드 `{"status": "recovery_status_rejected"}`만 낸다. store가 없으면 `no_store`이고, 아무것도 만들지 않는다.

## 검증

신규 `test_recovery_status.py` 8개. 실제 WF05/06/08 Slack·큐 fixture(재시작 경로 요청 → 버튼 메시지 전달)를 사용했다.

- **상태·시각·건수:** 상태, 시각, 정책 hash, 재시작 기록, outbox 건수가 정확하다.
- **쓰기 없음:** 조회 전후로 DB 논리 내용(WAL 포함 dump)과 표 목록이 같다. Slack ID, 스레드, 경로, 승인자·승인 hash가 출력되지 않는다.
- **연결 방식:** 모든 연결이 `file:…?mode=ro` URI다.
- **시한 처리:** 지난 시한은 표시만 하고 상태는 그대로다. 저장된 기록도 바뀌지 않는다.
- **입력 검증:** plan 필터, 잘못된 hash, limit 범위를 확인했다.
- **비정상 기록:** 깨진 기록은 `unreadable`, 게이트 이름은 형식 검사·차단 게이트만, PR URL은 미출력, tombstone은 표시한다.
- **CLI:** 성공하면 JSON을 낸다. 허용되지 않은 경로나 잘못된 인자에는 고정 코드만 내고 경로를 노출하지 않는다.
- **빈 store:** `no_store`를 내고 아무것도 만들지 않는다.

**결함 변형 15/15 검출.** 첫 실행에서 "rw 모드로 연결" 변형이 살아남았다. 이 명령은 원래 쓰지 않으므로 겉으로 차이가 드러나지 않았기 때문이다. 연결 URI를 직접 확인하는 시험을 추가한 뒤 검출했다. 전체 결과는 [검증 기록](recovery-status-local-validation-20261125.json)에 있다.

실제 클러스터·Slack 없음.
