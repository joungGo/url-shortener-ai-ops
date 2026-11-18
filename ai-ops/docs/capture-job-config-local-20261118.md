# 조사 Job의 Slack worker 설정 호환 · 2026-11-18

## 원인과 수정

기존 `build_live_capture_job`은 `ci-config` 직접 마운트 또는 `ci-config`와 `ci-normal-binding`의 projected 볼륨만 허용했다. 현재 Slack worker는 `ci-config-slack-current`와 `ci-normal-binding`을 projected 볼륨으로 받고, 고정 `common-materialize` init container가 2Mi Memory 볼륨에 권한 0600으로 복사한다. 조사 Job 생성 단계에서 이 구조가 거절돼 전체 C-04 실행 전 검증에 도달하지 못했다.

`c_capture_config.py`는 기존 두 구조와 현재의 정확한 설정 구조만 허용한다. 현재 구조에서는 ConfigMap 이름·init 명령·동일 digest 이미지·마운트·보안 설정·자원 제한을 검증한 뒤 고정 init 정의를 생성한다. 임의 init/env/Secret/서비스 계정 토큰·추가 권한을 복사하지 않는다. 중복 볼륨·중복 source·임의 ConfigMap·items/optional 재정의·무제한 메모리 볼륨도 거절한다.

현재 worker가 설정 해시에 포함하는 `runbook-policy.json`을 조사 접수 CLI에도 명시적으로 전달한다. 정책 누락·오류 시 접수를 거절한다. 기존 직접/projected 구성의 명령 호환성은 유지한다. 내용이 준비 이후 바뀌어 worker와 다른 해시가 되면 기존 worker의 설정 해시 검증이 계속 거절하며, 이번 수정이 그 검증을 우회하지 않는다.

## 보존한 경계

- 조사 Job: deadline 180초, 재시도 0, TTL 300초, 기존 큐/PVC·노드 affinity·비특권 실행·서비스 계정 토큰 자동 마운트 금지 유지.
- 파일당 65,536바이트, Memory 2Mi 및 init 자원 제한 유지.
- C-04: 시작 15초, 안내 뒤 최소 120초 리뷰/시작 뒤 210초 마감, 소유 원복 목표 240초/독립 원복 시작 300초, 회복 관찰 120초 유지.
- 실행별 새 준비 승인, 정확한 PR head의 사람 리뷰·CI, UID/CAS/Lease/ACK·원복·임시 자격 회수 로직 변경 없음.

## 로컬 검증

- 저장된 실제 worker Deployment 사본: 기존 main 생성기 거절 재현 → 수정 생성기 성공. 생성한 manifest는 적용하지 않았다.
- 새 회귀 시험 7개: 기존 구성 호환, 현재 구성과 최소 권한, 24종 금지 구성, legacy의 숨겨진 init/Secret 거절, 실제 고정 복사 스크립트의 symlink 처리·0600·크기 제한, 정책 누락/오류 접수 거절, 접수 CLI→큐→worker 설정 해시 일치 및 처리.
- 합성 조사 결과 `partial`은 fixture에 부족한 자료가 있기 때문이며, 설정 불일치로 실패하지 않고 실제 로컬 처리 경로를 완료했다. 이 결과를 실환경 정상 판정이나 C-04 통과로 해석하지 않는다.
- 대상 시험 17개와 전체 로컬 시험 **1,247개(53.417초)** 통과. 최초 sandbox 실행의 소켓 권한 오류 후 loopback 사용 권한으로 재실행했다. 구버전과 비교해 legacy Job 전체가 동일하며, 새 구성도 설정 볼륨·init·정책 인자 외에는 동일하다.
- 상세 실행 결과는 함께 있는 `capture-job-config-local-20261118.json` 참조.

실제 배포, server dry-run, 원격 CI, Slack 요청, 감시 기동, 장애 주입, Terraform, 유료 모델 호출은 실행하지 않았다. 실제 init 스케줄링·기동 시간은 미측정이다.

## 이어서 할 작업

1. 이 브랜치 코드 검토와 정확 head의 CI를 확인한 뒤 main 반영.
2. 후속 승인 범위에서 새 코드의 실행 runtime을 준비하고 실제 Job server dry-run·현재 worker/binding·정상 CI·양쪽 감시를 재검증. 준비 실패를 실행 승인으로 대체하지 않는다.
3. 새 intent/readiness·만료 시각으로 장애 실행 승인을 받는다. 과거 PR21 seed 리뷰는 새 복구 PR 리뷰나 장애 실행 승인이 아니다.

AI 기준 main `5931e245f01b55ce4d74c0e34aa8763ca8cd83b1`, 작업 브랜치 `codex/c04-capture-config-20261118`. C-04/C-05는 미완료다. 다음 추천: **GPT-6 SOL · High**.
