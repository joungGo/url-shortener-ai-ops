# A7-3a 재시작 Kubernetes 어댑터 로컬 구현 · 2026-11-19

상태: **PR48로 main 반영 완료.** 기준 main `6a6605d11b86a7b438286f8664d3341ae39e678a`. 실제 쓰기는 계속 막혀 있다. 어댑터의 mode는 `kubernetes`이고, WF08 실행기는 `local_fixture` 포트만 받는다.

## 목적

A7-3b(PR47)에서 재설계한 게이트가 요구하는 **원시 관측값**을 실제 Kubernetes·Prometheus에서 만들어 낸다. 또 증거 수집, 봉인한 PATCH, Lease 처리를 실제 API 형태로 구현한다. 검증은 가짜 API로 하며, 실제 클러스터 연결과 live 모드 활성화는 인프라 단계(WF11)에서 별도 승인을 받는다.

## 구현 (`ai_ops.restart_k8s`)

- `ApiClient`: 고정 경로 허용 목록, 요청마다 토큰 파일 재읽기(회전 반영), CA 검증 TLS, 리다이렉트 미추종(3xx는 오류), 응답 2MiB 상한, 쿼리 시계 기준 deadline(남은 시간이 없으면 연결하지 않음). 오류에는 응답 본문과 토큰을 담지 않는다.
- **신원 분리**
  - 읽기(`READER_ROUTES`): Deployment `lab-api`, Pod 목록, Pod 로그, 이벤트, HPA 목록, Argo Application. **GET만** 허용한다.
  - 쓰기(`WRITER_ROUTES`): Deployment `lab-api` PATCH, Lease `b6-scenario-lock` GET/PUT만 허용한다.
  - Prometheus: `/api/v1/query`만 허용한다.
  - Secret이나 다른 Deployment 경로, `..` 경로는 거절한다.
- `derive_snapshot`(순수 함수) → `RestartSnapshot`
  - UID·resourceVersion·generation·observedGeneration·배포 전략을 읽는다.
  - revision hash는 template에서 **우리 restart annotation만 빼고** 계산한다.
  - Pod: 삭제 중이 아닌 Pod가 정확히 1개여야 한다. `api` 컨테이너의 실행 여부, restartCount, imageID hash(표시용), startTime, Ready 조건의 마지막 전환 시각(실패 시작 시각)을 읽는다.
  - 판정 창: readiness probe와 startup probe 중 큰 값(initialDelay + failureThreshold × period)이다. lab-api는 readiness 5초 주기, startup 24×5초라 120초다. readiness probe가 없으면 거절한다.
  - rollout 진행: observedGeneration 불일치이거나 replicas·updatedReplicas가 1이 아니면 진행 중으로 본다.
  - HPA가 lab-api를 대상으로 하면 autoscaling이다.
  - Argo Application에 `automated`가 있거나 **읽을 수 없으면** 자동 동기화로 간주한다(차단).
  - 이벤트의 `FailedScheduling`은 표시만 한다. 이벤트를 읽지 못해도 표시만 비고 판정에는 영향이 없다.
  - Pod 2개, readiness probe 없음, 태그 이미지, replicas 2, startTime 없음이면 모두 예외다(실행기는 쓰지 않고 중지).
- `PrometheusSignals`: **설정으로 받은 PromQL만** 실행한다. 지표 이름을 추측해 코드에 넣지 않는다.
  - `prior_ready`: 결과가 비면 `unknown`(=거절), 값이 1 이상이면 observed, 모두 0이면 absent다.
  - `dependencies`: 식마다 1이면 pass, 0이 하나라도 있으면 fail, 비거나 오류면 unknown이다.
  - placeholder는 `$TEMPLATE_HASH` 문자열 치환이다. PromQL 라벨 선택자는 중괄호를 쓰므로 `str.format`을 쓰지 않는다. pod-template-hash는 `[a-z0-9]{1,16}`만 허용해 PromQL 주입을 막는다.
- `KubernetesRestartPort.capture_evidence`: describe(Deployment·Pod, managedFields 제외), status, events, 현재 로그와 이전 로그(이전 컨테이너가 없으면 `absent`로 기록)를 모으고, 지표 판정과 함께 **0700 폴더 / 0600 파일**에 원자적으로 저장한다. 반환값은 저장된 바이트의 sha256, 항목, 시각뿐이며 로그는 장부와 Slack에 나가지 않는다. 하나라도 실패하면 예외를 낸다(3b 필수 게이트로 쓰기 중지).
- `patch`: `patch_request`가 만든 **봉인된 JSON Patch 형태와 정확히 같을 때만** writer로 보낸다. 409·422(test 실패)는 확정된 미적용, 그 밖의 응답은 결과 불명(재전송 없음)이다.
- Lease
  - `acquire`: holder가 비어 있을 때만 resourceVersion 낙관적 동시성으로 PUT한다. **다른 holder가 있으면 만료로 보여도 빼앗지 않는다.** 409는 충돌이다.
  - `inspect_lease`: holder·uid·만료 시각(renewTime + duration)을 반환한다.
  - `release`: uid와 holder가 **자기 것일 때만** 비운다. 아니면 False이며 남의 Lease를 지우지 않는다.

## 확인된 대상 사실

lab-api(`ai-ops/lab/templates/deployment.yaml`): 컨테이너 `api`, readiness 5초 주기, startup probe 있음, **livenessProbe 없음**(kubelet이 멈춘 프로세스를 재시작하지 않으므로 이 명령이 필요한 이유), DATABASE·REDIS 의존성 있음, 배포 전략 `Recreate`.

## 진행 중 발견해 고친 것

처음에는 PromQL placeholder를 `str.format`으로 치환해서, 라벨 선택자의 중괄호 때문에 과거 Ready 판정이 늘 `unknown`이 됐다. fail-closed라 위험하지는 않았지만 판정을 쓸 수 없게 만드는 결함이라 문자열 치환으로 고치고 회귀 시험을 추가했다.

## 범위 밖(이후)

- 실제 PromQL(과거 Ready·DB·Redis)은 플랫폼에 고정된 Prometheus·kube-state-metrics 설정과 대조해 프로세스 연결 때 설정한다. 그 전에는 unknown(=거절)이다.
- reader·writer 토큰과 RBAC(Deployment lab-api get/patch, Lease get/update, 읽기 범위), VAP, 증거 보관 정리(A7-5), 실클러스터 shadow 판정, WF11 live 모드 판단.

## 검증

- 신규 `test_restart_k8s.py` 15개(가짜 API)
  - 관측값 도출, revision hash가 restart annotation만 무시하는지
  - fail-closed 신호 10종과 미구성 시 unknown, PromQL 중괄호·주입 방지
  - 불일치 객체 5종 예외, 도출 스냅샷이 3b 게이트를 모두 통과하는지
  - 증거 묶음(0600·0700, 바이트 hash, 항목, 이전 로그 absent, managedFields 제외, 로그 비노출)과 수집 실패 3종
  - 봉인 PATCH만 writer로 전송(변조 4종은 HTTP 호출 없이 거절), PATCH 결과 분류
  - Lease 획득·점검·자기 것만 반환, 타인 holder 미탈취·미해제, 409 충돌
  - 경로 허용 목록과 신원 분리, deadline·토큰 회전·리다이렉트·크기 상한·URL 검사
  - **실행기가 이 포트를 `live_command_executor_disabled`로 거절하고 쓰기 요청이 0개인지**
- 전체 결과는 [검증 기록](restart-k8s-adapter-local-validation-20261119.json)에 있다.

실제 클러스터·배포·Slack·장애 실습 없음. 기존 시간·실행별 승인·정확 head 리뷰/CI·잠금·원복 조건 변경 없음.

## 최종 반영 결과

[PR #48](https://github.com/joungGo/url-shortener-ai-ops/pull/48) head `9124203650e859cdb18de4793ba800e904179f3a`를 head 고정 일반 squash 병합해 main `13d94e9a3efc8432480291ef9bbc1461e6899c60`에 반영했다. [PR CI44525442506](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44525442506) 7종(1654개, skip5 기준선 동일)·[main CI44526171563](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44526171563) 성공(PR전용 gate skip은 설계대로). tree `d075338c571214f4a4fa8fdfdf7191620501cb59` 일치. 보호 변경/우회 없음. 병합 후 branch를 정리했고 최종 메타데이터는 로컬 갱신했다.
