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
| 1 | Alertmanager 라우팅을 `/v1/events`(ai-events)로 전환 | 플랫폼 [PR #164](https://github.com/f-lab-edu/F-Lab-DevOps/pull/164). 필수 CI 모두 통과(WF03 amtool 실제 설정 검사 포함). **승인 리뷰 1건 필요(보호 규칙)** |
| 2 | relay IRSA 역할(`ecr:DescribeImages`) | AI Terraform `wf11-recovery.tf`. 기본 비활성. 이 PR |
| 3 | 복구 읽기 신원 RBAC(SA·Role·RoleBinding·토큰)과 WF10 SA(IRSA annotation) manifest 도구 | 다음 |
| 4 | `c03-execution-lab` GitHub 읽기 토큰 | 사용자 발급 |
| 5 | `aws login --profile urlshortener-new` | 사용자, 설치 직전 |

### 2. IRSA 역할 상세

- **신뢰 조건.** 클러스터 OIDC provider는 운영 root가 만들고, 여기서는 조회만 한다. 신뢰 대상은 `system:serviceaccount:ai-ops:ai-ops`이고, aud는 `sts.amazonaws.com`이다. 와일드카드와 `StringLike`는 쓰지 않는다.
- **권한.** `ecr:DescribeImages` 하나다. 대상은 `repository/urlshortener` 하나다. 세션 시간은 최대 1시간이다.
- **코드와의 대응.** AI 코드(`irsa_clients`)는 `AWS_ROLE_ARN`이 `arn:aws:iam::764428911194:role/<aws_role_name>`과 같을 때만 동작한다. 그래서 output의 이름과 ARN을 각각 renderer 입력과 SA annotation에 넣는다.
- **검증.**
  - `terraform fmt -check`, `init -backend=false -lockfile=readonly`(provider는 lockfile 해시로 검증), `validate`를 통과했다.
  - 정적 계약 시험 4개를 추가했다. 기본 비활성, 정확한 SA 신뢰, 단일 action·저장소, 배치 결정 기본값을 확인한다.

실제 클러스터·AWS 호출(읽기 전용 계정 확인 제외)·Slack은 하지 않았다.
