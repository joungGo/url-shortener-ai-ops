# WF07 실제 읽기 포트 조각 A: GitHub Git 읽기·Kubernetes 증거 읽기·설정 · 2026-11-24

상태: **로컬 구현·검증 완료, PR CI/main 대기.** 기준 main `de0a2919f36682147b09abe1ac1219b602c71515`(PR57).

## 배경

WF07 PR 준비(`ImageRecoveryPRBridge.prepare`)는 로컬 fixture로만 완성돼 있었다. 실제 relay Pod 안에서 쓸 읽기 연결이 없었고, C04 실습 때의 읽기는 모두 운영자 PC용이었다(`gh`·`kubectl`·`aws` 명령과 운영자 자격).

사용자 결정(2026-11-24, 추천안):

| 항목 | 결정 |
|---|---|
| ECR | boto3를 추가하고 IRSA로 읽는다(조각 B). |
| GitHub 자격 | 토큰 파일을 마운트한다. |
| 분할 | 두 조각으로 나눈다. A는 GitHub Git 읽기·Kubernetes 증거·설정, B는 정상 CI·ECR·조립·연결. |

## 구현 (`ai_ops.recovery_sources`)

### 1. `TokenRepoReader`: WF07의 GitHub `reader` 포트

- **허용 경로:** 격리 저장소(`joungGo/c03-execution-lab`)의 Git 데이터 경로만 허용한다. 대상은 `git/ref/heads/main`과 `git/{commits,trees,blobs}/<40자 SHA>`이고, WF07 준비가 쓰는 경로와 같다. `user`, PR, 다른 저장소, 쿼리 문자열은 요청하기 전에 거절한다.
- **토큰:** 마운트된 파일을 **요청마다** 다시 읽는다. 그래서 토큰을 교체해도 안전하다. 일반 파일이어야 하고, group·other 쓰기 권한이 없어야 하며, 4096바이트 이하이고, `[A-Za-z0-9_]{20,}`(fine-grained PAT와 App 토큰 형식) 형식이어야 한다. 토큰은 헤더에만 넣고 URL에는 넣지 않는다.
- **연결:** 환경 변수의 proxy를 무시하고, redirect를 따르지 않는다(`NoRedirect`). 요청 하나당 8초로 제한하고, 응답은 2MiB 이하만 받는다.
- **오류:** 응답이 JSON 객체가 아니면 거절한다. 오류는 고정 코드만 쓰고, 응답 본문·사유·주소는 담지 않는다.
- **검증 범위:** 실제 Git fixture 위에서, 기존 fixture reader 대신 이 reader로 **같은 정확한 PR 준비(`prepared`)**가 되는 것을 확인했다.

### 2. `values_at(reader, revision)`

배포 revision에서 lab `values.yaml` 바이트를 읽는다. WF07 준비와 같은 helper(`_lab_tree`·`_tree`·`_blob`)를 쓴다.

### 3. `ProofObjectsReader`: `assess_recovery_proof`에 넘길 객체 읽기

- **읽기:** 고정 GET 5개다. Argo `argocd/lab-target`, Deployment, Lease(`b6-scenario-lock`), ReplicaSet 목록, Pod 목록. 관측기(PR57)와 같은 `ApiClient`·읽기 신원을 쓰고, Lease 경로만 추가했다. 읽는 도중 마감을 넘기면 거절한다.
- **선택만 하고 판정은 하지 않는다.**
  - Pod는 Deployment selector에 맞고 삭제 중이 아닌 것만 고른다.
  - `replica_set`은 그 Pod가 정확히 1개이고, Pod를 controller로 소유한 ReplicaSet이 Deployment 소유일 때만 채운다. 그 외에는 `{}`다.
  - `{}`이면 기존 증거 함수가 `current_pod_binding_unverified`로 막는다(fail-closed).
- **검증 범위:** 기존 C03 증거 fixture를 가짜 API로 내보내고, 읽은 객체를 기존 `assess_recovery_proof`에 그대로 넣어 `proof_ready`가 되는 것을 확인했다.

### 4. `PrepareConfig`: `recovery-approval.json`의 선택적 `prepare` 절

- **항목:** Slack→GitHub ID 매핑(`principals`), PR 작성자 ID, 초안 저장 경로, GitHub 토큰 파일, 준비 시간(15초 이하).
- **검사:**
  - Slack ID 형식이 맞아야 한다.
  - GitHub ID가 중복되면 안 된다.
  - 작성자 ID가 리뷰어 ID와 같으면 안 된다.
  - 경로는 절대 경로여야 한다.
  - `prepare`가 있으면 `observe`도 있어야 한다(같은 Kubernetes 읽기 신원을 쓰므로).
  - 승인자는 모두 매핑에 있어야 한다.
- **`check-config`:** `recovery_prepare_configured` 여부만 출력한다. 토큰을 읽지 않고, 경로도 노출하지 않는다.

**연결 경계:** 이번 조각은 부품과 설정만 만든다. `read_proof` 조립(정상 CI·ECR)과 진입점에서 WF07 bridge를 만드는 연결은 조각 B에서 한다. 그때까지 운영 동작은 바뀌지 않는다.

## 연결 단계에서 할 일 (범위 밖)

- **GitHub 토큰:** 읽기 전용 토큰을 발급하고 마운트한다. 권한은 격리 저장소 Contents: Read다.
- **RBAC:** 관측기에서 정리한 RBAC에 Lease `get`(`ai-ops-lab/b6-scenario-lock`)을 추가한다.
- **renderer:** 설정과 토큰을 마운트한다.

## 검증

신규 `test_recovery_sources.py` 13개. 가짜 HTTP opener를 실제 로컬 Git 저장소 위에 두고, 가짜 Kubernetes API를 썼다. 네트워크와 자격 증명은 쓰지 않았다.

- **GitHub 경로:** Git 데이터 4종만 허용한다. 그 외 8가지 경로는 요청 없이 거절한다.
- **토큰:** 헤더에만 들어가고, 교체 즉시 반영된다. 권한·형식·크기가 맞지 않으면 요청 없이 거절한다.
- **오류:** 404, 302(redirect 미추적), 네트워크 오류, 200이 아닌 응답은 모두 같은 고정 오류다. 본문은 노출되지 않는다. 크기 초과, 배열 응답, JSON이 아닌 응답은 거절한다.
- **기본 opener:** 환경 변수 proxy가 있어도 proxy를 쓰지 않고, redirect를 따르지 않는다.
- **Git 내용:** `values_at`이 커밋된 lab values와 일치한다. 이 reader로 WF07이 정확한 PR을 준비한다(`prepared`).
- **Kubernetes 객체:** 고정 GET 5개를 읽고, 그 결과로 기존 증거 함수가 `proof_ready`가 된다. Pod 2개, 다른 ReplicaSet 소유, 다른 Deployment의 ReplicaSet이면 `replica_set={}`이 되어 증거가 막힌다. 읽는 도중 마감 초과, selector 없음, 다른 Argo 앱 설정은 거절한다.
- **설정:** 잘못된 형식 9가지를 거절한다. `observe` 없는 `prepare`와 매핑 없는 승인자를 거절한다. `check-config`는 토큰을 읽지 않고 경로를 노출하지 않는다.
- **결함 변형 19종 모두 검출.** 고친 실행기를 썼다. 전체 환경을 상속하고, 기준선 통과를 먼저 확인했다.
  - 처음 결과는 17/19였다.
  - `token_cached`는 동작을 바꾸지 않는 잘못 만든 변형이어서, 실제로 토큰을 캐시하는 변형으로 바꿨다.
  - `deadline_removed`는 시험의 빈틈이어서, 읽는 도중 마감을 넘기는 시험을 추가했다.

## 같은 PR의 정정

- 조각 3(PR56)의 변형 검사 근거가 무효였던 점을 [재검증 기록](recovery-advance-wiring-local-20261121.md#정정-2026-11-24-변형-검사-근거)에 정정했다. 재검사 결과는 13/13 실제 검출이다.
- PR57 병합 결과 메타데이터도 이관했다.

실제 클러스터·GitHub 쓰기·배포·Slack·장애 실습·토큰 발급 없음. 기존 시간·실행별 승인·정확 head 리뷰/CI·잠금·원복 조건 변경 없음.

## 최종 반영 결과

[PR #58](https://github.com/joungGo/url-shortener-ai-ops/pull/58) head `dcfa034a5e39d2127ae60849c24274df9da57672`를 head 고정 일반 squash 병합해 main `f219d046013ca131930568c59dcad01ae6937d93`에 반영했다(사용자 "진행해" 후). [PR CI45347993050](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/45347993050) 7종(1770개, skip5 기준선 동일) 성공, tree `2ed22aad98e9997a312fcfcd3cb597de952ee40f` 일치, [main CI45348807139](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/45348807139) 성공(PR전용 gate skip은 설계대로). 보호 변경/우회 없음. 병합 후 branch를 정리했다.
