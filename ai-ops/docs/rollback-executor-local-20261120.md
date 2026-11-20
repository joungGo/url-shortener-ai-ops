# 원복 실행 조각 2: 기준선 고정 원복 실행기 로컬 구현 · 2026-11-20

상태: **로컬 구현·검증 완료, 이 커밋 시점 PR CI/main 대기.** 기준 main `564571836e33c71540313645308c258a99d3e126`. 실제 Argo 쓰기는 없다. 실행기는 `local_fixture` 포트만 받는다.

## 배경

2026-11-20 llm-council에서 원복 실행 방식을 정했다.

- 원복은 **Argo Application을 사전 승인된 기준선 커밋에 다시 고정하고 sync를 1회 하는 것**만 허용한다.
- revert PR은 원복이 아니라 새 변경이므로 이 실행기 범위에 넣지 않는다.
- 쓰기 전에 GitOps 드리프트 잠금(PR52)을 건다.
- 실패하면 시도를 소진한 것으로 보고 사람에게 인계한다. 재시도하지 않는다.

## 구현 (`ai_ops.rollback_executor.RollbackExecutor.execute_local`)

시도는 한 번뿐이다. 순서는 아래와 같다.

1. **예약 전 확인.** 아래 조건이 하나라도 어긋나면 예약 없이 거절한다.
   - **포트:** `local_fixture`이고 신원이 일치해야 한다(Argo `applications/lab-target`의 get/patch만).
   - **WF09 결정:** 지금도 이 정확한 `rollback_candidate`여야 한다. 승인 범위 hash와 바꿀 revision·digest가 같아야 하고, 취소·원복 결과·원복 관측 시도가 없어야 한다.
   - **승인 범위:** 철회되지 않았고, 만료 전이고, 사용 횟수가 0이고, 이 실행 hash·대상·Deployment·앱 UID에 묶여 있어야 한다.
2. **한 번만 예약.** 이 계획에 이미 실행 기록이 있으면 `rollback_already_attempted`로 거절한다. 중단됐거나 결과가 불명확한 시도도 다시 실행하지 않는다.
3. **쓰기 전 재검증.** 어긋나면 `needs_review`(`precondition_failed_no_write`)로 남기고 쓰지 않는다.
   - **Git 내용:** 배포 중인 revision과 기준선 커밋의 `ai-ops/lab` 트리는 `values.yaml` 외에는 모두 같아야 한다. 두 `values.yaml`은 각각 "실패 digest의 정규 표현"과 "기준선 digest의 정규 표현"이어야 한다. 이미지 외의 변경이 같이 되감기는 경우를 막는 조건이다. 내용 비교가 조상 관계 확인보다 강한 조건이다.
   - **실시간 상태:**
     - Argo source가 정확히 `{repoURL, path: ai-ops/lab, targetRevision: 배포 revision}`이어야 한다. helm 파라미터 같은 override가 있으면 거절한다.
     - 자동 동기화가 꺼져 있어야 한다.
     - 진행 중인 operation이 없어야 한다.
     - Deployment는 실패 digest를 실행 중이어야 한다.
   - **Lease:** 비어 있을 때만 잡는다. 다른 보유자가 있으면 빼앗지 않는다.
4. **드리프트 잠금.** Lease를 잡은 뒤, 쓰기 **전에** `GitOpsDriftLock.engage`를 한다. 이후 실시간 상태, Lease, 승인 범위·WF09 결정을 한 번 더 확인한다. 범위가 바뀌었거나 취소됐으면 쓰지 않는다. 이 경우 잠금은 걸린 채로 두고 사람이 확인한다(fail-closed).
5. **쓰기 1회.** `port.pin({application_uid, from_revision, to_revision})`을 한 번 호출한다.
   - 결과가 확정 충돌이면 `not_applied`로 둔다.
   - 그 밖의 예외나, 다시 읽은 고정 대상이 기준선과 다르면 `needs_review`(`pin_outcome_unknown_no_retry`)로 둔다.
6. **고정 확인 뒤 Lease 반환.** 고정이 확인되면 Lease 소유를 확인한 뒤 반환하고 `pinned_unverified`로 둔다. sync가 끝났는지와 서비스가 회복됐는지는 여기서 판정하지 않는다. WF09 원복 관측(PR51)이 새 관찰 구간으로 판정한다.

예외를 처리할 때도 예외 문구는 기록하지 않는다.

## 알려진 동작과 한계

- **Lease 처리.** 쓰기 전에 실패하면 WF08과 같은 이유로 Lease를 자동 반환하지 않는다. 보유자를 확인하고 정리하는 일은 WF09 회수(Lease 항목)와 사람이 맡는다.
- **잠금 후 중단.** 잠금을 건 뒤 쓰기 전에 중단되면 잠금이 남는다. main 트리가 기준선과 같아져야 풀리며, 그 전까지 WF07은 막힌다(fail-closed). 사람이 확인해야 한다.
- **시도 횟수 합산.** 승인 장부의 `attempts_used`는 이후 인증된 승인 조회가 담당한다. 지금은 실행기 장부가 계획당 1회를 보장한다.
- **C04 독립 원복과의 관계.** C04 실습의 독립 원복(300초)은 별개의 실습 코드다. C 파일은 동결 정책이라 통합하지 않았다.
- **미구현.** 실제 Argo 쓰기 포트, RBAC(`applications` patch), 레지스트리 이미지 존재 확인(선택), DB 마이그레이션 같은 비가역 부수효과 확인은 아직 없다. 비가역 부수효과는 승인 단계에서 사람이 판단할 항목이다.

## 검증

신규 `test_rollback_executor.py` 11개. 실제 WF07 PR 경로와 WF09 장부, 로컬 Git 저장소(기준선=병합 직전 커밋, 정상 digest=제안의 기존 digest), fixture Argo 포트로 검증했다.

- **정상 1회:** `pinned_unverified`가 되고, 고정 요청이 정확하고, 잠금이 걸리고 WF07이 차단되고, Lease가 반환된다. 다시 실행하면 거절되고 고정은 1회뿐이다. 순서는 잠금 → 쓰기다.
- **예약 없이 거절:** 다른 승인 범위, 만료, live 포트·신원 불일치, 취소된 효과 기록. 실행 기록이 남지 않고 쓰기도 0회다.
- **쓰기 없이 `needs_review`:**
  - 이미지 외 변경이 섞인 기준선
  - helm override
  - 자동 동기화
  - 진행 중 operation
  - 다른 이미지
  - 이미 기준선으로 고정된 상태
  - 다른 Lease 보유자(빼앗지 않음)
  - 잠금 뒤 승인 범위 변경 또는 취소(잠금 유지)
- **쓰기 결과:** 확정 충돌 → `not_applied`. 응답 유실 → 불명확, 토큰 문구 없음. 수락됐지만 반영되지 않음 → 불명확. 세 경우 모두 재실행이 거절된다.
- **결함 변형 9종 모두 검출:**
  - 1회 보장 제거
  - Git 트리 diff 확인 제거
  - source 정확 일치 완화
  - 잠금을 쓰기 뒤로 이동
  - 재확인 제거
  - 진행 중 operation 무시
  - 만료 무시
  - 충돌을 반영으로 취급
  - 고정 재확인 제거

전체 결과는 [검증 기록](rollback-executor-local-validation-20261120.json)에 있다.

실제 클러스터·GitHub 쓰기·배포·Slack·장애 실습 없음. 기존 시간·실행별 승인·정확 head 리뷰/CI·잠금·원복 조건 변경 없음.
