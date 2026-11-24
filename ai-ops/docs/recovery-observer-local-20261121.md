# A7 PR 경로 현재 상태 관측기 · 2026-11-21

상태: **로컬 구현·검증 완료, PR CI/main 대기.** 기준 main `83b8e39efe3ce02405b1e1e3bb73a49b41471d3f`(PR56).

## 배경

조각 1~3(PR54~56)으로 요청 → 승인 → 재검증 → PR 준비 흐름을 이었다. 하지만 승인 장부의 관측 포트(`observe`)가 항상 실패해서, 실제 배포에서는 승인 대기가 열리지 않았다. 이번 조각은 그 포트를 실제로 구현한다. 읽기 전용이며, 지원 대상은 하나(`ai-ops-lab/lab-api`)다.

사용자 결정(2026-11-21):

- **실패 판정:** 현재 컨테이너 상태로 판정한다. WF07 증거 재확인과 같은 규칙이고, 이벤트 시각은 쓰지 않는다. kubelet의 이미지 재시도 간격이 최대 5분이라, "이벤트 120초 이내" 규칙을 쓰면 실제로 실패 중인데도 `unknown`이 될 수 있다.
- **정상 판정:** 이번에는 하지 않는다. 실패가 증명되지 않으면 항상 `unknown`이다. "이미 회복됨"(`already_recovered`)은 자동으로 판정하지 않는다.
- **연결 범위:** 설정이 있을 때만 연결한다. `recovery-approval.json`의 선택적 `observe` 절이 있을 때만 쓰고, 없으면 지금처럼 미연결이다.

## 구현 (`ai_ops.recovery_observe`)

### 읽기: 고정 GET 4개

PR48의 `ApiClient`를 그대로 쓴다. 고정 경로만 허용하고, 토큰은 요청마다 다시 읽고, TLS를 검증하고, redirect는 따르지 않고, 본문 크기를 제한한다.

1. `GET /apis/apps/v1/namespaces/ai-ops-lab/deployments/lab-api`
2. `GET /apis/apps/v1/namespaces/ai-ops-lab/replicasets`
3. `GET /api/v1/namespaces/ai-ops-lab/pods`
4. `GET /apis/argoproj.io/v1alpha1/namespaces/<argo_namespace>/applications/<argo_application>`

계획 대상이 지원 대상이 아니면 한 번도 읽지 않고 거절한다. 읽기가 끝난 시각이 마감을 넘었으면 거절한다.

### 판정 (`derive_state`, 순수 함수)

- **신원:** Deployment 이름과 네임스페이스, uid, 단일 `api` 컨테이너의 이미지, selector가 있어야 한다. Argo `spec.source.targetRevision`이 `status.sync.revision`과 같아야 하고, 이 값이 WF07의 `base_sha`와 같은 값이다. 하나라도 어긋나면 **예외**가 나서 관측 없음으로 처리된다(fail-closed).
- **`image_failure` 조건:**
  - Deployment selector에 맞는 살아 있는 Pod가 정확히 1개다. 같은 네임스페이스의 다른 workload Pod는 세지 않는다.
  - 그 Pod가 Deployment 소유의 ReplicaSet 소유다(controller owner, Deployment→ReplicaSet→Pod).
  - Pod spec 이미지, ReplicaSet template 이미지, 컨테이너 상태 이미지가 모두 Deployment 이미지와 같다.
  - 컨테이너가 1개이고 이름이 `api`이며, `ready=false`이고, 대기 사유가 `ErrImagePull` 또는 `ImagePullBackOff`다.
  - Deployment가 최신 generation을 관측했고, `readyReplicas`가 0이다.
- **결과:** 위 조건을 모두 만족하면 `image_failure`이고, `ImageFailureBinding`(Pod·Deployment uid hash, 이미지 hash, 관측 시각)을 함께 반환한다. 그 밖에는 모두 `unknown`이다. **`healthy`는 반환하지 않는다.**
- **`proof_hash`:** 원시 응답 4개의 hash로 만든 hash만 둔다. 객체 본문은 저장하거나 반환하지 않는다.

### 장부 쪽 결과 (기존 WF06 규칙 그대로)

| 상황 | 결과 |
|---|---|
| 열 때 `image_failure`이고 계획 근거와 uid·이미지가 같음 | 승인 대기 열림 |
| 열 때 `unknown`이거나 근거와 다름 | "새 계획 필요" |
| 재검증 때 `image_failure`이고 신원이 같음 | `proposal_ready` |
| 재검증 때 `unknown` | `needs_review`(`cause_not_verified`) |
| 재검증 때 revision 등 신원이 바뀜 | `superseded` |
| 읽기 실패 | 열지 않음, 또는 `needs_review` |

### 연결

- `RecoveryApprovalConfig.observe`(`ObserveConfig`)에 다음을 둔다: API 주소(https만), 토큰·CA 파일 절대 경로, Argo 앱 네임스페이스·이름.
- 이 절이 있으면 `RecoveryApprovalService`가 `make_observer`로 관측 포트를 만든다. 생성할 때는 자격 증명을 읽지 않는다.
- `check-config`는 `recovery_observe` 여부만 출력한다. 주소나 경로는 출력하지 않는다.

## 연결 단계에서 할 일 (이번 범위 밖)

- **읽기 RBAC:** `deployments` get, `replicasets` list, `pods` list(`ai-ops-lab`), Argo `applications` get. `replicasets` list는 재시작 읽기 신원(PR48)에 없던 새 권한이다.
- **renderer:** `recovery-approval.json`과 토큰·CA 마운트. 기본은 꺼짐을 유지한다.
- **실클러스터 응답 형태 확인:** 실제 응답이 대기 사유·이미지 표기 형식에서 다르면 `unknown` 쪽으로 기울어 fail-closed가 된다.

## 알려진 동작과 한계

- **정상 판정을 하지 않는다.** 자연 회복된 경우에도 재검증은 `needs_review`(사람 확인)가 된다. 5종 정상 검사(Argo·Pod 이미지·Ready·readyz·업무 요청)는 후속 과제다.
- **이미지 hash는 이미지 문자열 그대로의 hash다.** 계획 근거의 이미지 표기와 형식이 다르면 열 때 "새 계획 필요"가 된다.
- **재시도 간격의 다른 상태.** Pod가 재시도 사이에 `ContainerCreating` 같은 다른 대기 사유를 보이는 순간에는 `unknown`이다. 다음 요청이나 재검증 때 다시 판정된다.

## 검증

신규 `test_recovery_observe.py` 15개. 메모리 안의 가짜 API를 쓰고, 클러스터와 네트워크는 쓰지 않았다.

- **실패 판정:** 대기 사유 2종, Deployment·이미지·revision 결합, 근거 ID, 본문 비노출.
- **실패가 아닌 13가지 경우가 모두 `unknown`이다:** 실행 중·Ready, CrashLoopBackOff, ready 값 없음, Pod 2개, 삭제 중인 Pod만 있음, 다른 ReplicaSet 소유, 다른 Deployment의 ReplicaSet, Pod 이미지 다름, 상태 이미지 다름, ReplicaSet 이미지 다름, Ready replica 있음, 오래된 generation, sidecar 컨테이너. 어느 경우도 `healthy`가 아니다.
- **다른 workload:** 같은 네임스페이스의 다른 workload는 무시한다.
- **신원 확인 실패는 예외로 처리한다:** revision 불일치·누락, Deployment 이름·uid·컨테이너·selector 이상.
- **관측기:** 고정 GET 4개 순서대로 읽는다. 응답이 바뀌면 `proof_hash`도 바뀐다. 지원하지 않는 대상은 읽기 0회로 거절한다. 403, 마감, 목록 형식 오류는 예외이고, 읽는 도중 마감이 지나도 거절한다. 설정은 http·상대 경로·잘못된 이름을 거절한다.
- **실제 WF05/06 fixture 연결:**
  - 설정된 관측기로 승인 대기가 열리고, 재검증하면 `proposal_ready`가 된다(GET 8회).
  - 회복된 상태는 `needs_review`(`cause_not_verified`)가 되고, `already_recovered`가 되지 않는다.
  - revision이 바뀌면 `superseded`가 된다.
  - `observe` 절이 없으면 읽기 0회다.
  - `check-config`는 자격 증명을 읽지 않고 주소를 노출하지 않는다.
- **결함 변형 16종 모두 검출(새 모듈만으로 확인):**
  - 대기 사유 무시
  - ready 무시
  - Pod 1개 조건 완화
  - 소유 체인 무시
  - Pod 이미지 무시
  - ReplicaSet 이미지 무시
  - readyReplicas 무시
  - generation 무시
  - sync revision 무시
  - selector 무시
  - healthy 보고
  - 대상 확인 제거
  - 읽기 후 마감 확인 제거
  - proof hash 고정
  - http 허용
  - 진입점 미연결

전체 결과는 [검증 기록](recovery-observer-local-validation-20261121.json)에 있다.

실제 클러스터·GitHub 쓰기·배포·Slack·장애 실습 없음. 기존 시간·실행별 승인·정확 head 리뷰/CI·잠금·원복 조건 변경 없음.
