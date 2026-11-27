# WF07 PR 발행: C03 writer 앱 writer (A2 핵심, PR-c) · 2026-11-27

상태: 로컬 구현과 시험만 했다. 기본 동작은 바뀌지 않는다(`prepare.publish_token_file`이 없으면 이전과 같이 준비만 한다). renderer 입력과 단기 토큰 도구는 다음 PR에서 한다.

## 배경
- WF11 실제 실행에서 복구 PR 경로를 검증하지 못했다.
  - `c03-execution-lab` main 보호 규칙(승인 1, 관리자 우회 금지) 때문에 본인 계정 PR은 병합할 수 없다.
  - WF07 설정의 작성자(`author_id`)는 C03 writer 앱 봇(354870297)이다.
- 사용자는 처음에 "writer 앱, 운영자 수동 실행"(A)을 골랐다.
- 그런데 승인 뒤 재검증이 정하는 **신선도 기한은 관측 시각 + 30초**이고, bridge는 이 안에서 준비와 발행을 끝내도록 설계돼 있다. 그래서 수동 실행은 맞출 수 없다.
- 사용자 결정(2026-11-26): **A2.** relay가 준비 직후 같은 단계에서 writer 앱으로 PR을 연다. 단기 설치 토큰만 클러스터에 넣는다. 병합과 sync는 사람이 한다.

## 변경
1. **`recovery_pr_writer.GitHubAppPRWriter`(신규, `mode='github_app'`)**
   - `create`: blob → 기준 커밋 읽기 → tree(파일 `ai-ops/lab/values.yaml` 하나) → commit(부모 = 기준 SHA) → branch `recovery/wf07-<plan hash>` → PR(`draft: false`, `maintainer_can_modify: false`)
   - PR 본문에 "자동으로 병합·배포되지 않음, head 리뷰·CI 필요, Argo sync는 별도 승인"을 적는다.
   - `lookup`: 그 branch의 PR만 조회한다. 2건 이상이면 오류다.
   - **요청 허용 목록**은 위 7개뿐이다. merge, review, 삭제, 수정(PATCH), 다른 저장소, 보호 설정은 보낼 수 없다.
   - **payload 검사**
     - 키 집합이 정확해야 한다.
     - 저장소는 c03, base는 `main`, branch는 plan hash와 일치해야 한다.
     - 경로는 `values.yaml`, `write_enabled`는 False여야 한다.
     - 전후 내용이 달라야 한다.
   - **토큰**
     - `ghs_…`(설치 토큰)만 받는다.
     - 파일은 일반 파일이고 그룹·기타 권한이 없어야 하며 4KB 이하여야 한다.
     - 요청마다 읽는다.
   - **네트워크**
     - 지정한 proxy만 쓰고, 환경 변수 proxy는 쓰지 않는다.
     - 리다이렉트를 거부한다.
     - HTTP 오류는 고정 코드(`pr_writer_http_<상태>`)로만 내고, 응답 본문은 쓰지 않는다.
2. **bridge(`recovery_pr`)**: 발행 writer 허용 조건을 "fixture 또는 **정확히 이 클래스**(`type` 비교)"로 바꿨다. 하위 클래스나 흉내 객체는 거부한다. 발행 뒤 PR 작성자·Bot 여부·base·branch·diff 재검증(`_accept`)은 그대로다.
3. **설정(`recovery_sources.PrepareConfig`)**: 선택 항목 `publish_token_file`을 추가했다. 절대 경로여야 하고 egress proxy가 지정돼 있어야 한다. 아니면 `prepare_publish_rejected`다.
4. **relay(`recovery_approval_wiring`·`slack_server`)**
   - writer가 있으면 한 tick에서 준비(`prepare`)만 한다.
   - **다음 tick**에서, 기록이 `prepared`이고 승인 신선도가 남아 있으면 `publish_local`을 부른다. bridge가 발행 직전에 근거를 두 번 다시 읽는다.
   - 한 번 발행됐거나 불확실해진 기록은 다시 발행하지 않는다.
   - writer만 있고 bridge가 없으면 거부한다.
   - **구현 중 발견하고 고친 설계 결함:** 처음에는 준비와 발행을 같은 tick에 넣었다. 그런데 relay는 tick당 차단 예산(`RELAY_BLOCK_SECONDS=15`)과 진행 나이 한도(30초)가 있어서, 준비(최대 15초)와 발행을 합치면 넘을 수 있었다. 그래서 tick을 나눴다.
5. **GitHub 쓰기 시간 예산**: 한 번의 발행에 쓰는 요청 6개가 **합계 10초**를 공유한다. 요청마다 제한은 min(5초, 남은 예산)이다. 예산을 넘으면 `pr_writer_budget_exceeded`로 멈추고, bridge가 "발행 결과 불확실 → 사람 확인"으로 기록하며 재시도하지 않는다.
6. **알림(`recovery_outbox`)**: `awaiting_review`(PR 열림) 고정 문구와 검증된 PR 번호만 붙인다. URL·head·diff는 넣지 않는 기존 원칙을 지킨다.

## 검증
- 새 시험 11개(`test_recovery_pr_writer.py`)
  - 정확한 요청 순서와 본문(파일 하나, 부모, branch, PR 옵션)
  - 허용 목록 밖 요청 거부(merge, review, 삭제, 수정, 다른 저장소, 보호, 다른 branch 조회)
  - payload 범위 위반은 요청 전에 거부
  - 토큰 형식·권한, proxy 형식, 환경 변수 proxy 무시
  - HTTP 오류 고정 코드(본문 비노출)
  - lookup
  - **실제 WF07 fixture에서 relay가 한 tick에 준비하고 다음 tick에 발행해 `awaiting_review`와 `PR #123` 알림이 나가며, 다시 발행하지 않는다**
  - 시간 예산: 남은 예산만큼 요청 제한이 줄어든다(5초 → 2초). 예산을 넘으면 중단하고 deadline을 초기화한다
  - `_call`이 허용 목록 밖 요청을 보내기 전에 거부한다
  - relay 시작 코드가 bridge와 writer를 서비스에 넘긴다
  - writer가 없으면 준비만 한다
  - 하위 클래스·흉내 writer는 거부한다
  - bridge 없는 writer는 거부한다
  - 설정 규칙
- 관련 기존 시험 121개 통과.
