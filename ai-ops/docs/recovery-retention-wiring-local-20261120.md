# A7 프로세스 연결 조각 2: 복구 보관 정리 tick 연결 · 2026-11-20

상태: **로컬 구현·검증 완료, PR CI/main 대기.** 기준 main `46789b11434736aad2531a22f40605dad44fe57c`(PR54).

## 배경

A7-5(PR50)의 `RecoveryRetention.tick()`은 한 번의 정리 패스만 정의하고, 어느 프로세스도 호출하지 않았다. 조각 1(PR54)로 relay가 승인 장부와 outbox를 갖게 됐으므로, relay가 이 둘을 주기적으로 정리하도록 연결한다.

## 구현

- **relay 연결** (`slack_server.relay_loop`, `retention_tick`): 기존 Slack 요청·작업 정리(60초) **뒤에** 복구 보관 정리를 10분(`recovery_retention.INTERVAL_SECONDS`)마다 1회 실행한다.
  - 요청 정리가 먼저라서, 방금 만료된 요청의 outbox 메시지도 같은 패스에서 지워진다.
  - 종료 신호가 오면 실행하지 않는다.
  - 실패는 삼키고 다음 주기에 다시 한다. 보고에는 이름만 남는다(경로·본문·예외 문구 없음).
  - 보관 정리 객체가 없으면 기존 루프 동작(호출 순서·횟수)은 바뀌지 않는다.
- **기능이 켜져 있을 때** (`RecoveryApprovalService.retention`): `RecoveryRetention(ledgers={'prepare_proposal': 승인 장부}, outbox=outbox)`.
  - 장부: 보관 기간이 지난 기록에서 신원·승인자·증거 hash를 지우고, 중복 방지용 tombstone만 남긴다(기존 WF06 `prune`).
  - outbox: 만료된 요청의 메시지를 지운다.
- **기능이 꺼져 있을 때** (`OrphanOutboxRetention`): 예전에 켜져 있던 동안 남은 outbox 메시지만 지운다. 테이블이 없으면 아무것도 만들지 않고, 승인 정책도 읽지 않는다.
- **`recovery_outbox.prune_existing(adapter)`**: 테이블이 있을 때만, 기존과 같은 처리 잠금 아래에서 지운다. `RecoveryOutbox.prune`도 이 함수를 쓴다(동작 동일).

## 알려진 동작과 한계

- **꺼진 상태의 승인 장부는 정리하지 않는다.** 장부를 열려면 승인 정책(정책 hash 일치)이 필요하다. 기록은 hash와 시각뿐이고, 기능을 다시 켜면 정리된다.
- **증거·봉인 파일 정리는 이번 범위가 아니다.** 재시작 실행 증거와 봉인 캡처는 재시작 경로가 프로세스에 연결될 때 같은 tick에 디렉터리를 추가한다(경로/PVC 결정 포함).
- **Slack 처리 잠금과 겹치면** outbox 단계가 `outbox` 실패로 기록되고 다음 주기에 다시 한다.
- **보관 기간은 기존 정책(7일) 그대로다.** 이보다 짧게 설정할 수 없다.

## 검증

신규 시험 7개(`test_recovery_approval_wiring.py`, 모듈 전체 24개):

- **켜진 상태 패스:** 만료 전에는 아무것도 지우지 않는다. 계획 요청이 만료되면 outbox 1건이 지워지고, 장부의 신원·승인자·증거 hash가 비워진다. 정리는 메시지를 보내지 않는다.
- **꺼진 상태 패스:** 남은 메시지는 요청이 만료된 뒤에만 지운다. 테이블이 없으면 만들지 않는다.
- **처리 잠금 충돌:** 이름만 있는 실패(`outbox`)로 남는다.
- **relay 연결:** 10분 간격, 실패 격리, 없으면 호출 없음, 요청 정리 뒤 실행, 종료 중 미실행. 진입점이 꺼진 상태에선 `OrphanOutboxRetention`, 켜진 상태에선 자기 장부·outbox를 쓰는 `RecoveryRetention`을 넘긴다.
- **결함 변형 10종 모두 검출:**
  - 테이블 확인 대신 생성
  - 모든 행 삭제
  - 처리 잠금 제거
  - 장부 정리 누락
  - 켜진 상태에서 꺼진 상태용 정리 사용
  - 간격 제거
  - 실패 전파
  - 요청 정리 전에 실행
  - 종료 중 실행
  - 진입점 미연결

전체 결과는 [검증 기록](recovery-retention-wiring-local-validation-20261120.json)에 있다.

실제 클러스터·GitHub 쓰기·배포·Slack·장애 실습 없음. 기존 시간·실행별 승인·정확 head 리뷰/CI·잠금·원복 조건 변경 없음.

## 최종 반영 결과

[PR #55](https://github.com/joungGo/url-shortener-ai-ops/pull/55) head `b973505b066cf364ab068d1a087cb816179fd243`를 head 고정 일반 squash 병합해 main `a5f59dc085b08114baeb44d950ab616e709dcace`에 반영했다. [PR CI44730619628](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44730619628) 7종(1731개, skip5 기준선 동일) 성공, tree `60ae06e12f9fe20e668f5a9f7f5d63d34e31d02e` 일치. [main CI44879917750](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44879917750) 성공(PR전용 gate skip은 설계대로). 보호 변경/우회 없음. 병합 후 branch를 정리했다.
