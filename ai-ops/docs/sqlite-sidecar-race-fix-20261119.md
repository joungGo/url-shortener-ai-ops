# SQLite 보조 파일 삭제 경쟁 수정 · 2026-11-19

상태: **PR42로 main 반영 완료.** 기준 main `ceb6c277b6a968c5be046657dc04137695e1a836`.

## 발견 경위

PR41 작업 중 전체 시험에서 `test_recovery_commands.RestartCommandTest.test_two_concurrent_executors_submit_at_most_once`가 간헐 실패했다(쓰기 0회). 시험만의 문제로 보고 안정화하려 했으나, 부하 재현(CPU 부하 + 80~160회)과 예외 추적으로 **제품 코드의 경쟁 조건**을 확인했다. 사용자에게 보고하고 허락을 받은 뒤 수정했다.

## 원인

SQLite는 마지막 연결을 닫을 때 `-wal`/`-shm`(필요 시 `-journal`)을 삭제한다. 그 순간 다른 스레드나 프로세스의 파일 안전 검사가 `lstat`하면, 경로로 찾은 파일이 이미 unlink된 상태라 `st_nlink == 0`이 보인다(macOS에서 관찰). 기존 검사는 `st_nlink != 1`을 하드링크·위험 파일로 보고 거절했다.

- 재현: 수정 전 80회 중 쓰기 0회 실패 1회, 엉뚱한 사유("큐 파일 권한 또는 링크가 안전하지 않습니다", `slack_storage_rejected`)로 거절된 경우 3회. `jobs.sqlite3-wal`/`-shm`에서 `nlink 0` 관찰.
- 결과는 fail-safe였다(needs_review, 쓰기 최대 1회 유지). 그러나 정상 작업이 무작위로 거절되고, B′ 구조처럼 한 Pod의 여러 프로세스가 같은 SQLite를 쓰면 운영에서 간헐 장애가 될 수 있었다.

## 수정

같은 패턴 4곳(`job_queue._safe_files`, `slack_adapter._db`, `observability.Telemetry._validate_files`, `continuous_model` 저장소)에서 **보조 파일의 `nlink == 0`만 "삭제 중 = 없음"으로 허용**했다.

- 보조 파일의 `nlink > 1`(하드링크), 비정규 파일·심볼릭 링크, 다른 소유자, 그룹/기타 권한 비트는 계속 거절한다. `nlink 0`이어도 이 조건들은 그대로 검사한다.
- 본 DB 파일은 계속 `nlink == 1`만 허용한다.
- `continuous_model`은 `exists()` 뒤 `lstat()`을 호출해 그 사이 삭제되면 처리되지 않은 `FileNotFoundError`가 나던 경쟁도 단일 `lstat`으로 고쳤다.

동시 실행 시험 자체는 바꾸지 않았다(최대 1회 안전 단언과 정확히 1회 성공 기대 유지).

## 검증

- 신규 `test_sqlite_sidecar_race.py` 4개: 4개 저장소 × 보조 파일 3종의 `nlink 0` 허용, 하드링크·권한·소유자·심볼릭 링크 거절, 본 DB 파일 엄격, 모델 저장소 사라짐 경쟁. **수정 전 코드에서는 실패**함을 확인했다.
- 부하 재현 160회: 매번 쓰기 정확히 1회 + 다른 쪽 `restart_not_executable` 거절(엉뚱한 사유 0건).
- 관련 모듈 122개, 전체 1602개(205.063초, skip1=WF03 환경 게이트) 통과. 저장소 경계·diff 통과.

검증 수치는 [검증 기록](sqlite-sidecar-race-fix-validation-20261119.json)에 있다. 인프라·배포·Slack·실습 실행 없음. 기존 시간·승인·원복 조건 변경 없음.

## 최종 반영 결과

[PR #42](https://github.com/joungGo/url-shortener-ai-ops/pull/42) head `a45c053b15b5141ea7dfba0539119135194e7d29`를 head 고정 일반 squash 병합해 main `f6e04337239a6f981745674ff390ce8b89e37f81`에 반영했다. [PR CI44486424872](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44486424872) 7종(1602개, skip5 기준선 동일)·[main CI44487170865](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44487170865) 성공(PR전용 gate skip은 설계대로). tree `15e36202953b3f5364325833e8baa4261d2aead3` 일치. 보호 변경/우회 없음. 최종 메타데이터는 병합 후 로컬 갱신했다.
