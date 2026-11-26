# WF11 설치 준비 (인프라 없이) · 2026-11-25

상태: **진행 중.** 인프라 설치를 시작하기 전에, 3시간 상한 밖에서 끝낼 준비 작업이다. 사용자가 "인프라 없이 끝낼 수 있는 작업 먼저"를 택했다.

## 설치 전 점검 결과 (읽기 전용)

- **계정.** 프로젝트는 새 계정 `764428911194`에 있다([플랫폼 계정 이전 기록](https://github.com/f-lab-edu/F-Lab-DevOps/blob/main/docs/runbooks/aws-account-migration-2026-11.md)).
  - 사용 프로필은 `urlshortener-new`(root 로그인 세션)와 Terraform용 `urlshortener-terraform`이다. 직전 설치(2026-11-18)도 이 둘을 썼다.
  - 세션은 **만료**됐다. 설치 직전에 사용자가 `aws login`을 다시 해야 한다.
  - 기본 CLI 프로필과 원격 main 설치 가이드의 계정 번호는 옛 계정이다.
- **보존 자격.** 설치 자격 7종과 Slack bot·signing secret은 그대로 있다.
  - 격리 저장소 `c03-execution-lab`의 GitHub 읽기 토큰은 없다. 사용자가 새로 발급해야 한다.
- **WF10 활성 배포.** 실환경에 설치한 적이 없다.

## 사용자 결정 (2026-11-25)

| 항목 | 결정 |
|---|---|
| 배치 | WF10만 설치(레거시 ai-ops worker는 설치하지 않음). namespace·이름은 `ai-ops`, 새 빈 PVC |
| 사건 수신 | `ai-ops-events.ai-ops.svc:9443/v1/events` |
| Slack 수신 주소 | `slack-ops.bidservice.store` 재사용(`/slack/events`, `/slack/interactivity`) |
| 발신원 | source_alias `eks-seoul`, source_id `am-urlshortener`, cluster_id `urlshortener` |
| 경보 공지 | 기존 Webhook 채널 유지(채널 ID는 설치 때 화면에서 확인) |

## 준비 항목

| # | 항목 | 상태 |
|---|---|---|
| 1 | Alertmanager 라우팅을 `/v1/events`(ai-events)로 전환 | **완료.** 플랫폼 [PR #164](https://github.com/f-lab-edu/F-Lab-DevOps/pull/164) → main `f9b1b72`. 아래 1-1 참고 |
| 2 | relay IRSA 역할(`ecr:DescribeImages`) | **완료.** AI [PR #73](https://github.com/joungGo/url-shortener-ai-ops/pull/73) → main `12622a1`. 기본 비활성 |
| 3a | WF10 SA(IRSA annotation)·worker 조사 읽기 권한, 복구 읽기 신원, Slack 진입점 manifest 생성기 `ai_ops.wf11_edge` | **완료.** AI [PR #74](https://github.com/joungGo/url-shortener-ai-ops/pull/74) → main `514a37c` |
| 3c | WF10 Prometheus 연결을 실제 구성(http, 인증 없음)에 맞추는 renderer 수정 | 이 PR([3c 문서](wf10-plain-prometheus-local-20261126.md), 사용자 결정 2026-11-26, (a)안) |
| 3b | 사건 수신 TLS·토큰, Alertmanager 인증, sender client 비밀 자료 절차 | AI [PR #76](https://github.com/joungGo/url-shortener-ai-ops/pull/76)(문서) |
| 4 | `c03-execution-lab` GitHub 읽기 토큰 | 사용자 발급 |
| 5 | `aws login --profile urlshortener-new` | 사용자, 설치 직전 |

### 1-1. 플랫폼 라우팅 전환 결과

- **PR #164 병합.** main 보호 규칙(승인 1건, `enforce_admins` 켜짐) 때문에 사용자가 GitHub 화면에서 "관리자 우회 금지"를 끄고 우회 squash 병합했다. 병합 결과는 main `f9b1b72`, tree는 PR head와 같다.
- **main CI 실패와 수정.** 병합 뒤 main CI 45626777257의 contracts가 실패했다. main에서만 도는 플랫폼 레거시 `check_alert_routing.py`가 예전 `/v1/alerts` 구조만 가정했기 때문이다. PR 단계에서는 이 작업이 돌지 않아 드러나지 않았고, PR #164 작성 때 놓친 부분이다.
- **[PR #165](https://github.com/f-lab-edu/F-Lab-DevOps/pull/165)의 두 가지 수정.**
  - ai-events 상태 분기를 추가했다. legacy는 기존 검사를 통과하고, 실제 values는 정해진 차이만 허용한다.
  - 레거시 이미지의 openssl 고정값을 `3.5.9-r1`로 바꿔 AI 저장소와 맞췄다. 외부 패키지가 바뀌어 image 작업이 실패하고 있었다.
- **PR #165 병합.** 사용자가 화면에서 우회 병합했다. main은 `d195fcb`, tree는 PR head와 같고, main CI 45628950483이 성공했다.
- **보호 설정(사용자 결정).** F-Lab-DevOps main의 `enforce_admins` 해제 상태를 유지한다. 승인 1건과 필수 CI는 그대로다. Claude는 이 설정을 바꾸지 않는다.

### 2. IRSA 역할 상세

- **신뢰 조건.** 클러스터 OIDC provider는 운영 root가 만들고, 여기서는 조회만 한다. 신뢰 대상은 `system:serviceaccount:ai-ops:ai-ops`이고, aud는 `sts.amazonaws.com`이다. 와일드카드와 `StringLike`는 쓰지 않는다.
- **권한.** `ecr:DescribeImages` 하나다. 대상은 `repository/urlshortener` 하나다. 세션 시간은 최대 1시간이다.
- **코드와의 대응.** AI 코드(`irsa_clients`)는 `AWS_ROLE_ARN`이 `arn:aws:iam::764428911194:role/<aws_role_name>`과 같을 때만 동작한다. 그래서 output의 이름과 ARN을 각각 renderer 입력과 SA annotation에 넣는다.
- **검증.**
  - `terraform fmt -check`, `init -backend=false -lockfile=readonly`(provider는 lockfile 해시로 검증), `validate`를 통과했다.
  - 정적 계약 시험 4개를 추가했다. 기본 비활성, 정확한 SA 신뢰, 단일 action·저장소, 배치 결정 기본값을 확인한다.

### 3a. `ai_ops.wf11_edge` (설치 전 manifest, 비밀 값 없음)

```
python -m ai_ops.wf11_edge --inputs <WF10 입력 JSON> --role-arn <PR73 output wf11_recovery_role_arn> \
  --slack-host slack-ops.bidservice.store --output <새 파일>
```

renderer와 **같은 WF10 입력**에서 만든다. 그래서 이름이 renderer와 어긋날 수 없다. 출력은 아래 리소스 13개를 담은 `List`이며, 운영자가 검토한 뒤 server dry-run을 거쳐 적용한다.

- **WF10 ServiceAccount** `<service_account>`: IRSA annotation `eks.amazonaws.com/role-arn`을 달고, `automountServiceAccountToken: false`로 둔다.
- **worker 조사 읽기 권한**(2026-11-26 추가): worker는 이 ServiceAccount의 projected token으로 대상을 읽는다. 그래서 기존 B chart `rbac.yaml`과 같은 규칙을 이 계정에 묶는다.
  - 대상 namespace의 `<name>-investigate-read`: deployments get(workload), services get(service), pods·events get/list, replicasets get/list, pods/log get
  - Argo namespace의 `<name>-investigate-argo-read`: applications get(binding의 application)
  - 이름은 binding에서 가져온다. 형식은 `inputs()`의 BindingRegistry 모델이 이미 검사한다.
  - 처음 작성할 때 이 권한을 빠뜨렸다. 그대로였다면 S1 조사가 403으로 실패했을 것이다. 사용자 승인 후 수정했다.
- **복구 읽기 신원**
  - ServiceAccount `<name>-recovery-reader`(IRSA 없음)
  - `kubernetes.io/service-account-token` Secret: 이름은 renderer의 `recovery.secrets.reader`와 같고, token·ca.crt는 Kubernetes가 채운다.
  - `ai-ops-lab` Role과 RoleBinding: `deployments` get(lab-api), `replicasets`/`pods` list, `pods/proxy` get, `leases` get(b6-scenario-lock)
  - `argocd` Role과 RoleBinding: `applications` get(lab-target)
  - 쓰기 동사와 와일드카드는 없다.
- **Slack 진입점**(2026-11-17과 같은 형태)
  - cert-manager Certificate: ClusterIssuer `letsencrypt-traefik`, Secret은 renderer의 `secrets.slack_tls`
  - Traefik IngressRouteTCP: `HostSNI(slack-ops.bidservice.store)`에서 `<name>-slack:9444`로 TLS passthrough
- **IRSA 범위(사용자 결정 2026-11-26, 수용).** EKS webhook은 annotation이 달린 ServiceAccount를 쓰는 **모든 Pod**에 역할을 주입한다. renderer는 메인·Slack 송신·egress Pod에 같은 계정을 쓰므로 세 Pod 모두 역할을 받는다.
  - 받아들인 이유: 역할 권한이 `urlshortener` 저장소의 `ecr:DescribeImages` 하나뿐이다. 송신·egress Pod의 외부 목적지는 코드 수준에서 고정돼 있다(Slack, GitHub/STS/ECR). 단, NetworkPolicy는 공인 대역 443 전체를 허용한다.
  - Pod마다 계정을 나누려면 renderer를 바꿔야 한다. WF11 이후 과제로 남긴다.
- **입력 검사.** recovery 절이 없거나, 역할 ARN·호스트·이름 형식이 맞지 않으면 거절한다. 출력 파일이 이미 있으면 덮어쓰지 않는다. 실패하면 고정 코드만 낸다.

**검증.** 시험 5개를 추가했다.

- renderer 출력과 이름이 맞물린다: Deployment의 SA, relay 마운트 Secret, ingress TLS Secret, `-slack` Service와 포트
- 읽기 전용 규칙이 정확하다.
- 잘못된 입력은 거절한다.
- CLI는 한 번만 쓰고, 실패 시 고정 코드를 낸다.
- worker 조사 읽기 규칙이 chart와 같고, Deployment의 SA에 묶인다(추가).

결함 변형은 처음 14/14, 조사 권한 추가분 12/12를 검출했다. 처음 13개 중 1개가 살아남았는데, 원인은 상위 모델이 이미 검사하는 binding 이름을 다시 검사하던, 실행될 수 없는 코드였다. 이 코드를 지웠다.

실제 클러스터·AWS 호출(읽기 전용 계정 확인 제외)·Slack은 하지 않았다.
