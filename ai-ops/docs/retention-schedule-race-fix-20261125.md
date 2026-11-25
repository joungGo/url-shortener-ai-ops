# 예약 보관 정리 시험 경합 수정 · 2026-11-25

상태: **로컬 검증 완료, PR CI/main 대기.** 기준 main `eb435540533da3d7583f40288c1ab6f30ac26165`(PR65). 시험만 바꿨고, 제품 코드는 바꾸지 않았다.

## 문제

[main CI45537162151](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/45537162151)(PR63 병합)의 첫 실행이 실패했다. PR CI에서는 같은 tree가 통과했고, 실패한 작업만 다시 실행하자 성공했다.

```
test_retention_schedule.RetentionScheduleTest.test_runtime_flag_automatically_purges_and_restart_keeps_due_time
AssertionError: 0 != 1   (state['runs'])
```

**원인: 시험 쪽 경합이다(PR42부터 있었다).**

- 시험은 worker 프로세스가 오래된 작업을 `purged`로 만드는 순간 대기를 끝낸다.
- 그런데 `RetentionSchedule.tick`은 정리 실행이 끝난 **뒤** 별도 트랜잭션에서 `runs=runs+1`과 `status`를 기록한다(`retention_schedule.py`).
- 그래서 시험이 그 사이에 상태를 읽으면 `runs`가 0으로 보인다.

제품 동작은 설계대로다. 기록 전에 중단되면 due 상태를 유지하고, 다음 worker가 재개한다.

## 수정

대기 조건에 "실행 기록 완료"를 더했다. `runs ≥ 1`이고 `status`가 `running`이 아니어야 대기를 끝낸다. 상태 표가 아직 없을 때 나는 오류(`TypeError`, `sqlite3.Error`)도 기존 오류처럼 재시도로 처리한다. 두 번째 반복(재시작 후 due 시각 유지)의 확인 내용은 그대로다.

## 검증

- **결정적 재현.** 제품 코드의 최종 기록 직전에 1초 지연을 임시로 넣었다(검사 스크립트가 `finally`에서 원복했고, 원복을 확인했다).
  - 기존 시험: CI와 같은 `0 != 1`로 실패
  - 수정한 시험: 통과
  - 지연이 없을 때도 통과
- 시험 모듈 20회 반복, 로컬 전체 시험, 경계 검사, diff 검사 결과는 [검증 기록](retention-schedule-race-fix-validation-20261125.json)에 있다.

실제 클러스터·배포·Slack 없음.
