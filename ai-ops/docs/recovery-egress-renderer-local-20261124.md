# relay 외부 송신 proxy 조각 2: WF10 renderer의 선택적 recovery 렌더링 · 2026-11-24

상태: **로컬 구현·검증 완료, PR CI/main 대기.** 기준 main `65a32a14202973dd9fdfb3853071a8a4dfa6ec38`(PR61).

## 결정

renderer에는 복구(WF07) 설정이 전혀 없었다. proxy만 추가하면 이를 쓰는 쪽이 없으므로, 사용자 결정(2026-11-24, 추천안)에 따라 **선택적 `recovery` 입력 전체**를 렌더링한다. 입력에 `recovery` 절이 없거나 `null`이면 후보는 이전과 **바이트 단위로 같다**. main 코드와 비교해 manifest hash와 후보 전체 hash가 모두 같음을 확인했다.

## 입력 (`recovery` 절)

```json
"recovery": {
  "approvers": ["<slack.user_id>"],
  "approval": {"wait_seconds": 3600, "approval_seconds": 600, "observation_seconds": 30, "revalidation_seconds": 10},
  "principals": {"<slack.user_id>": 1234567},
  "author_id": 7654321,
  "aws_role_name": "ai-ops-recovery-reader",
  "secrets": {"reader": "<기존 Secret 이름>", "github": "<기존 Secret 이름>"},
  "normal_binding": { ... NormalCIBinding ... }
}
```

**입력 검사**

- 필드 집합이 정확해야 한다.
- 승인자는 렌더링되는 Slack 정책의 사용자(`slack.user_id`)와 같아야 한다. 정책에는 grant가 하나뿐이다.
- Secret 이름은 서로 달라야 하고, 기존 Secret 6개와도 겹치면 안 된다.
- binding은 `NormalCIBinding`으로 검증한다.
- 나머지는 런타임 모델(`RecoveryApprovalConfig`)로 그대로 검증한다. 대상은 승인자 매핑, 작성자 ≠ 리뷰어, 역할 이름, 재검증 시간 15초 이하다.
- 검사가 실패하면 아무 파일도 쓰지 않는다.

## 렌더링 결과 (`recovery`가 있을 때만)

**ConfigMap**

- `recovery-approval.json`을 생성한다.
  - `observe.api_url`은 기존 Kubernetes collector와 같은 API 서버를 쓴다. 기존 송신 규칙 안에 있고, 읽기 신원은 별도다.
  - 토큰과 CA는 `/run/recovery-reader`에서 읽는다.
  - Argo 대상은 `argocd/lab-target`이다.
  - 초안은 `/data/recovery-drafts`(PVC)에 저장한다.
  - GitHub 토큰은 `/run/ai-github/token`에서 읽는다.
  - binding은 `/etc/ai-ops/normal-ci-binding.json`에서 읽는다.
  - `egress_proxy`는 `http://<name>-egress.<ns>.svc:8080`이다.
- `normal-ci-binding.json`을 함께 넣는다.

**메인 Pod**

- **relay 컨테이너에만** reader Secret(`token`, `ca.crt`)과 GitHub Secret(`token`)을 마운트한다. 마운트 방식은 기존과 같다(init 복사, 0600).
- worker, receiver, ingress는 이 Secret을 받지 않는다.

**proxy Pod** (`<name>-egress` Deployment·Service)

- `ai_ops.recovery_egress_proxy`만 실행한다. 포트는 8080이다.
- Secret, Kubernetes 토큰(`automountServiceAccountToken: false`), PVC를 모두 쓰지 않는다.
- 파일 시스템은 읽기 전용이고, 권한 상승이 없으며, 기능(capability)을 모두 제거했다.

**NetworkPolicy**

| 대상 | 수신 | 송신 |
|---|---|---|
| 메인 Pod | 기존과 같음 | 기존 규칙 + proxy Pod 8080. 인터넷으로 직접 나가는 규칙은 없다. |
| proxy Pod | 메인 Pod에서만 8080 | DNS, 그리고 공개 IPv4 443. 사설·CGNAT·loopback·link-local·IMDS 대역은 제외한다. |

**후보 결과에 추가되는 것**

- `recovery` 요약: 활성화 여부, proxy 주소, 발행 미연결(`publication: not_wired`)
- 외부 선행 조건 5개: reader RBAC, GitHub 읽기 토큰, IRSA 주석과 역할, CI relay, Slack Interactivity URL
- 잔여 위험 2개:
  - relay가 침해되면 세 호스트(GitHub·STS·ECR)에는 접근할 수 있다.
  - IRSA 환경 변수가 메인 Pod의 모든 컨테이너에 주입된다.

## 변경 계획 (`wf10_change_plan`)

- **적용 순서:** `recovery`가 있으면 proxy NetworkPolicy·Service·Deployment를 메인 Pod **앞에** 적용한다. sender와 같은 규칙이다. 원복은 그 역순 11단계다.
- **사후 확인:** `egress_proxy_only_fixed_destinations`와 `main_pod_internet_egress_only_via_proxy`를 추가한다.
- **메인 Deployment 판별:** 이름이 `-sender`, `-egress`로 끝나는 것을 제외한다.

## 패키지 크기 상한

`recovery`가 있으면 후보가 약 69KB가 되어 기존 64KiB 상한을 넘는다.

- **후보 패키지 파일**(`inputs.json`, `candidate.json`)만 상한을 128KiB(`PACKAGE_LIMIT`)로 올렸다.
- `read_regular`는 64KiB와 128KiB 두 값만 허용한다.
- 그 밖의 파일(config 등)은 64KiB 그대로다.

## 남은 일

- **실제 값 결정(연결 단계):** reader 신원과 RBAC, GitHub 토큰, IRSA 역할과 주석, Secret 이름.
- **서버 측 dry-run:** 기존과 마찬가지로 live gate다.

## 검증

**시험**

- 신규 `test_wf10_recovery_render.py` 8개.
- 기존 renderer·변경 계획·준비 시험 51개도 그대로 통과했다.
- 시험 항목:
  - `recovery`가 없거나 `null`이면 결과가 같다.
  - 있으면 리소스 11개가 나오고, 구성 값과 요약이 맞다.
  - Secret은 relay에만 마운트된다.
  - proxy Pod에는 비밀이 없고, 두 NetworkPolicy가 경로를 고정한다.
  - 렌더링된 설정으로 relay가 `prepare`를 읽어 bridge를 구성한다(오프라인).
  - 잘못된 입력 10가지를 쓰기 없이 거절한다.
  - 변경 계획의 적용 순서와 사후 확인이 맞다.
  - 패키지 왕복 검증이 되고, 변조를 감지한다.

**변형 검사:** 결함 변형 15종을 모두 검출했다(기준선 선확인 실행기).

- 처음 결과는 14/15였다.
- 승인자 검사를 지우는 변형이 살아남았다. 시험이 그 검사가 아니라 다른 사유(GitHub ID 매핑 누락)로 거절되고 있었기 때문이다. 시험을 고쳐 해당 검사만 잡도록 했다.

전체 결과는 [검증 기록](recovery-egress-renderer-local-validation-20261124.json)에 있다.

실제 클러스터·배포·Slack·AWS·GitHub 쓰기 없음. renderer는 적용하지 않는 후보만 만든다(`execution_allowed=false`).
