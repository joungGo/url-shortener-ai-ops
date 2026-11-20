# 원복 실행 조각 1: GitOps 드리프트 잠금 로컬 구현 · 2026-11-20

상태: **PR52로 main 반영 완료.** 기준 main `e16b61494fa8d5644d82670a08e23ded43ab34c6`. 실행·전송·클러스터·GitHub 쓰기는 없다.

## 배경 (llm-council 결정, 2026-11-20)

원복 실행 방식은 **기준선 고정**(Argo targetRevision을 승인된 정상 커밋에 고정하고 sync 1회)만 쓰기로 결정했다. revert PR은 원복이 아니라 새 변경으로 보고 별도 승인 대상으로 둔다.

기준선 고정 원복 뒤에도 GitHub `main`에는 실패한 변경이 그대로 남는다. 이 상태에서 다음 WF07 PR을 오염된 `main` 위에서 만들면 "main head == 배포 SHA" 동기화 검사가 **오히려 통과**해서 문제 이미지가 다시 배포된다. 그래서 위원회는 두 가지를 정했다.

- 원복 **전에** 잠금을 걸고, 잠금이 걸린 동안에는 WF07 진행을 fail-closed로 막는다.
- 잠금 해제 조건은 `main`의 `ai-ops/lab` 경로 트리가 기준선 커밋의 트리와 같을 때로 둔다.

## 구현

### `ai_ops.gitops_drift_lock` (신규)

- **`GitOpsDriftLock.engage(plan_hash, scope, repository)`**
  - 대상(lab-api)별로 잠금을 하나만 둔다.
  - 같은 계획·승인 범위·저장소로 다시 호출하면 기존 잠금을 그대로 돌려준다(멱등).
  - 다른 원복이 잠금을 쥐고 있으면 `gitops_drift_lock_conflict`로 거절한다.
  - 허용되지 않은 저장소나 잘못된 계획 hash는 거절한다.
  - 해제된 뒤에는 새 원복이 다시 잠글 수 있다. 이때 generation이 증가한다.
- **`release_if_aligned(reader)`**
  - 기존 GitHub 읽기 helper(`_main_sha`, `_lab_tree`)로 `main` head와 두 커밋의 `ai-ops/lab` Git 트리 SHA를 읽는다. Git 트리 SHA는 내용 hash이므로, 값이 같으면 경로 아래 파일이 모두 같다.
  - 트리가 같을 때만 해제하고, 해제 시점의 `main` SHA와 트리 SHA를 기록한다.
  - 해제하지 않는 경우:

    | 상황 | 결과 |
    |---|---|
    | 트리가 다르다 (fix-forward 포함) | `main_lab_tree_differs` |
    | 읽기 실패 | `alignment_unverified` |
    | 확인 도중 `main`이 바뀌었다 | `alignment_unverified` |

  - 예외 문구는 남기지 않는다.
- **`assert_unlocked(db)`**: WF07이 상태를 바꾸는 바로 그 DB 트랜잭션 안에서 호출한다. 잠금 테이블이 없으면 잠금이 없는 것으로 본다.
- **만료와 수동 해제**: 만료는 없다. 인증되지 않은 수동 해제도 없다. fix-forward로 트리가 달라진 경우를 해제하려면 이후 별도로 인가된 해제 경로가 필요하고, 그 전까지는 잠긴 상태가 유지된다(fail-closed).

### `ai_ops.recovery_pr` (WF07) 진입부

잠금 검사를 넣은 위치는 다음과 같다.

| 위치 | 해당 단계 |
|---|---|
| `_ready_in_db` | 준비·게시 트랜잭션 |
| `_prepared_authority` | preflight·병합·동기화 계획·동기화 기록의 시작 확인 |
| preflight·`plan_merge`·`plan_sync`·`record_sync`의 최종 트랜잭션 | 권한 재확인 직후 |

- 잠금이 걸린 동안에는 증거 읽기, GitHub 쓰기, 병합·동기화 계획을 모두 시작하지 않는다.
- 처리 도중에 잠금이 걸리면 상태를 바꾸는 트랜잭션에서 거절한다.
- 준비 단계는 기존 계약대로 오류를 `recovery_pr_preparation_failed`로 감싸고 기록을 `needs_review`로 남긴다.
- 읽기 전용 동작(조회, 결과 문장, 중단, 재조정, prune)과 이미 만들어진 PR을 기록하는 동작은 막지 않는다.

## 범위 밖 (이후)

- **조각 2 원복 실행기**: 실행 직전 재검증(승인 재조회, 조상·digest-only diff, source 밖 override, 진행 중 Argo 작업, Lease·UID/source)을 하고 → 잠금을 건 뒤 → 기준선 고정 sync를 1회 한다. 실패하면 시도 소진으로 보고 수동 인계한다.
- 정렬 PR은 봇 경로가 아니라 별도 승인을 받은 사람의 변경으로 처리하고, 잠금 해제 확인을 주기적으로 호출하도록 연결한다.
- fix-forward를 해제할 인가된 경로를 만든다.

## 검증

신규 `test_gitops_drift_lock.py` 9개. 실제 WF07 브리지와, 로컬 Git 저장소를 GitHub 읽기 인터페이스로 감싼 fixture를 썼다.

- **잠금 상태**: 같은 요청은 멱등, 다른 원복은 충돌, 잘못된 입력은 거절한다.
- **잠금 중 차단**: 준비는 증거 읽기 0회로 차단된다. 게시는 GitHub 쓰기 0회로 차단된다. preflight·병합 계획·동기화 계획도 각각 차단된다.
- **처리 도중 잠금(3종)**:
  - 준비 중 증거를 읽는 사이 → 커밋 트랜잭션에서 거절, 기록은 `needs_review`, 게시 쓰기 0회
  - preflight 실시간 읽기 중 → 거절
  - preflight의 마지막 시작 확인 뒤 → 트랜잭션에서 거절
- **해제**:
  - 실패 변경 커밋 → 유지
  - `ai-ops/lab` 밖 변경 → 유지
  - 정렬 커밋 → 해제(`main` SHA·트리 기록)
  - 해제 뒤 새 원복 → 다시 잠금
- **해제 거절**: fix-forward → 유지, GitHub 읽기 실패 → 유지(토큰 문구 미노출), 확인 중 `main` 변경 → 유지.
- **결함 변형 7종 모두 검출**:
  - 준비·게시 검사 제거
  - 시작 검사 제거
  - 트리 비교를 SHA 비교로 바꿈
  - 충돌 검사 제거
  - `main` 재확인 제거
  - 해제된 잠금을 잠긴 것으로 취급
  - 트랜잭션 안 검사 4곳 제거 (마지막 시작 확인 뒤 경쟁 시험으로 검출)
- **기존 WF07 시험**: 그대로 통과한다.

전체 결과는 [검증 기록](gitops-drift-lock-local-validation-20261120.json)에 있다.

실제 클러스터·GitHub·배포·Slack·장애 실습 없음. 기존 시간·실행별 승인·정확 head 리뷰/CI·잠금·원복 조건 변경 없음.

## 최종 반영 결과

[PR #52](https://github.com/joungGo/url-shortener-ai-ops/pull/52) head `d7fd6c82296ec25bd1c1136a6d117bc56cac9750`를 head 고정 일반 squash 병합해 main `564571836e33c71540313645308c258a99d3e126`에 반영했다. [PR CI44665694359](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44665694359) 7종(1696개, skip5 기준선 동일)·[main CI44666355792](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44666355792) 성공(PR전용 gate skip은 설계대로). tree `98eab6fb4a8c4bd56dc13e7d4e9fd9e73c46d262` 일치. 보호 변경/우회 없음. 병합 후 branch를 정리했고 최종 메타데이터는 로컬 갱신했다.
