# WF12 인계 초안: Slack 복구 승인·PR 준비 흐름 · 2026-11-24

상태: **초안 (A8).** 기준 AI main `5d0c43984c27754921c2d5b7f30cc1e883b3542d`(PR59).

- 코드 연결은 로컬 계약과 CI로만 검증했다.
- **실제 클러스터·Slack·GitHub·AWS에는 배포하거나 연결한 적이 없다.**
- WF11(실환경 검증) 결과에 맞춰 이 문서를 확정한다.
- 이 문서는 배포·연결 승인이 아니다.

## 1. 한눈에 보기

설정이 갖춰지면, 잘못된 이미지 배포로 lab-api가 장애일 때 아래 흐름이 Slack에서 사람 승인을 거쳐 **"정확한 복구 PR 내용 준비"**까지 이어진다.

PR 생성(GitHub 쓰기), 병합, 배포, 클러스터 쓰기는 **하지 않는다.**

| # | 단계 | 시작 | 하는 일 | 결과 | 막히면 |
|---|---|---|---|---|---|
| 1 | 조사 | `@bot investigate-event <사건>` | 기존 조사 worker 실행 | 같은 스레드에 조사 결과 | — |
| 2 | 계획 | `@bot plan-recovery <사건> <job> [image-rollback]` | 조사 근거로 계획 초안 고정(WF05) | 계획 hash가 담긴 회신 | 근거 부족·미지원 안내 |
| 3 | 승인 요청 | `@bot request-approval <사건\|this> <계획hash>` | 계획 요청자·같은 스레드 확인 → **현재 상태 관측** → 승인 대기 | 승인·거절 버튼 메시지 | 관측 실패 → "열지 않음", 상태가 다름 → "새 계획 필요" |
| 4 | 결정 | 버튼 클릭 | 서명 확인 → 결정만 기록 | 승인 알림(취소 버튼) | 마감·정책 변경 → 만료·대체 |
| 5 | 재검증 | 자동(승인 직후) | 현재 상태를 다시 관측(읽기 전용) | `proposal_ready` | 실패 아님 → `needs_review`, 신원 바뀜 → `superseded` |
| 6 | PR 준비 | 자동 | GitHub·Kubernetes·정상 CI·ECR을 두 번 읽고 대조 → 정확한 diff와 PR 내용을 로컬에 저장 | "정확한 변경을 준비했습니다" 알림 | 조건 불충족 → `no_pr`, 판단 불가 → `needs_review` |

- 모든 알림은 원래 계획 스레드에 고정 문구로 간다.
- 모든 결과는 `execution_allowed=false`다.

## 2. 구성 요소와 근거

| 조각 | PR | 핵심 모듈 | 문서 |
|---|---|---|---|
| 승인 경로(명령·버튼·알림) | #54 | `recovery_approval_wiring`, `slack_ingress.SlackRouter` | [recovery-approval-wiring](recovery-approval-wiring-local-20261120.md) |
| 보관 정리 tick | #55 | `RecoveryRetention` 연결 | [recovery-retention-wiring](recovery-retention-wiring-local-20261120.md) |
| 승인 뒤 재검증·WF07 준비 | #56 | `advance_once`, `enqueue_pr` | [recovery-advance-wiring](recovery-advance-wiring-local-20261121.md) |
| 현재 상태 관측기 | #57 | `recovery_observe` | [recovery-observer](recovery-observer-local-20261121.md) |
| WF07 읽기 A (GitHub·Kubernetes·설정) | #58 | `recovery_sources` | [recovery-wf07-sources](recovery-wf07-sources-local-20261124.md) |
| WF07 읽기 B (정상 CI·ECR·조립) | #59 | `recovery_proof_live`, `NormalCIInbox.latest_ready_history` | [recovery-wf07-proof](recovery-wf07-proof-local-20261124.md) |

그 이전의 바탕은 다음과 같다.

- WF05 계획
- WF06 승인 장부
- WF07 PR bridge
- A7-1 버튼 수신(PR45)
- A7-2 outbox(PR46)
- A7-5 보관 정리(PR50)

## 3. 설정: `/etc/ai-ops/recovery-approval.json`

- **기본은 꺼짐이다.** 파일이 없거나 `enabled: false`면 기존 동작 그대로다.
- **설정 절 조합에 따라 동작이 달라진다.**
  - 승인 경로만: `observe`·`prepare` 없이 둔다. 다만 관측이 연결되지 않아 승인 대기가 열리지 않는다.
  - 승인 대기까지: `observe`를 추가한다.
  - PR 준비까지: `prepare`를 추가한다. `prepare`는 `observe`가 있어야 한다.
  - 재시작 실행 조건 확인까지: `restart`를 추가한다. `restart`도 `observe`가 있어야 하며, 실제 재시작은 실행하지 않는다(2026-11-25, [재시작 연결](recovery-restart-wiring-local-20261125.md)). 아래 예시에는 없다.

아래 예시는 실제 설정 모델(`RecoveryApprovalConfig`)로 검증했다. 값은 자리표시자다.

```json
{
  "schema_version": 1,
  "enabled": true,
  "prepare_proposal": {
    "version": 1, "scope": "prepare_proposal", "approvers": ["U0123ABCD"],
    "wait_seconds": 3600, "approval_seconds": 600,
    "observation_seconds": 30, "revalidation_seconds": 10
  },
  "observe": {
    "schema_version": 1, "api_url": "https://kubernetes.default.svc",
    "token_file": "/var/run/secrets/ai-ops-reader/token",
    "ca_file": "/var/run/secrets/ai-ops-reader/ca.crt",
    "argo_namespace": "argocd", "argo_application": "lab-target"
  },
  "prepare": {
    "schema_version": 1, "principals": {"U0123ABCD": 1234567}, "author_id": 7654321,
    "drafts_dir": "/data/incidents/recovery-drafts",
    "github_token_file": "/run/ai-github/token", "prepare_seconds": 15,
    "normal_binding_file": "/etc/ai-ops/normal-ci-binding.json",
    "aws_role_name": "ai-ops-recovery-reader"
  }
}
```

| 항목 | 규칙 |
|---|---|
| `prepare_proposal.approvers` | Slack 사용자 ID. `slack-policy.json` grants에 있어야 한다. 승인자는 **계획 요청자 본인**이어야 한다(WF06 제한). |
| `revalidation_seconds` | 15 이하. 관측 한 번이 relay 한 틱 안에 끝나야 한다(relay 상태 확인 30초). |
| `wait_seconds` / `approval_seconds` | 승인 대기 마감 / 승인 후 재검증 유효 시간. |
| `observe.api_url` | https만. 토큰·CA는 절대 경로. Argo 앱은 PR 준비를 쓰면 `argocd/lab-target`이어야 한다. |
| `prepare.principals` | Slack ID → GitHub 사용자 ID. 모든 승인자가 있어야 하고, GitHub ID는 중복될 수 없다. |
| `prepare.author_id` | PR 작성자 GitHub ID. 리뷰어(principals)와 달라야 한다. |
| `prepare.drafts_dir` | 초안 저장 경로. 저장소 밖 PVC에 둔다. |
| `prepare.github_token_file` | 읽기 토큰 파일. 요청마다 다시 읽는다. 일반 파일이어야 하고, group·other 쓰기 권한이 없어야 하며, `[A-Za-z0-9_]{20,}` 형식이어야 한다. |
| `prepare.normal_binding_file` | 정상 CI binding. relay 시작 때 읽는다. 바뀌면 relay를 재시작해야 한다. |
| `prepare.aws_role_name` | IRSA 역할 이름. `AWS_ROLE_ARN`과 정확히 일치해야 한다. |

`check-config`가 출력하는 것은 `recovery_approval`, `recovery_observe`, `recovery_prepare_configured` 여부뿐이다. 토큰을 읽지 않고, 경로나 주소도 출력하지 않는다.

## 4. 프로세스 동작

**ingress** (`slack_server ingress`)

- 하나의 TLS 리스너에서 `/slack/events`와 `/slack/interactivity`를 받는다.
- 서명 키만 쓴다. bot 토큰은 쓰지 않고, WF07 bridge도 만들지 않는다.

**relay** (`slack_server relay`), 1초 틱

1. 기존 Slack 요청 접수와 조사 결과 회신을 처리한다.
2. 5초마다 `advance_once`를 실행한다.
   - 한 번에 1단계만 진행한다: approved → 재검증, 또는 proposal_ready → WF07 준비.
   - 같은 단계를 반복하지 않는다.
   - 그다음 장부 상태를 outbox에 동기화한다.
3. 틱마다 Slack 메시지를 최대 1건 보낸다. 결과가 불분명한 전송은 다시 보내지 않는다.
4. 60초마다 Slack 요청과 작업을 정리한다.
5. 그 직후 10분마다 복구 보관 정리를 한다. 대상은 승인 장부 tombstone, WF07 기록, 만료 요청의 outbox 메시지다.
6. 종료 신호를 받으면 새 단계와 전송을 시작하지 않는다.

**시간 상한**

- 관측: `revalidation_seconds` 이하(최대 15초)
- WF07 준비: `prepare_seconds` 이하(최대 15초)
- 읽기 한 번: 3~8초

## 5. 연결 단계 체크리스트 (WF11 전 필수, 별도 승인)

이 순서로 준비하고 확인한다. 하나라도 없으면 해당 단계가 **안전하게 멈춘다**(fail-closed). 그 단계가 실패해도 relay 자체는 멈추지 않는다.

1. **이미지:** main `5d0c439` 이후 이미지를 쓴다(boto3 계열 포함, CI image 작업에서 빌드 확인).
2. **Kubernetes 읽기 신원** (`observe.token_file`): 관측기와 증거 읽기가 같은 신원을 쓴다.
   - `ai-ops-lab`: `deployments` get(`lab-api`), `replicasets` list, `pods` list, `leases` get(`b6-scenario-lock`)
   - `argocd`: `applications` get(`lab-target`)
   - `replicasets` list는 재시작 읽기 신원(PR48)에는 없던 새 권한이다.
   - `pods/proxy` get(`ai-ops-lab`): 정상 판정의 `/readyz`·`/items` 읽기(2026-11-25, [정상 판정](recovery-healthy-judgment-local-20261125.md)). 없으면 정상 판정이 `unknown`이 된다.
   - 이 신원에는 쓰기 권한을 주지 않는다.
3. **GitHub 읽기 토큰:** 격리 저장소 `joungGo/c03-execution-lab`의 Contents: Read만 준다(Git 데이터 API: ref·commit·tree·blob).
4. **IRSA**
   - relay ServiceAccount에 역할(`aws_role_name`)을 연결한다.
   - 정책은 `ecr:DescribeImages` 하나로 한정한다(`arn:aws:ecr:ap-northeast-2:764428911194:repository/urlshortener`).
   - EKS가 주입하는 `AWS_ROLE_ARN`·`AWS_WEB_IDENTITY_TOKEN_FILE`만 쓴다. 프로필·환경 키·IMDS는 코드에서 막았다.
5. **정상 CI 근거**
   - CI relay(`ai_ops.ci_relay --watch --normal-binding ...`)가 **같은 큐 store**에서 실행 중이어야 한다.
   - worker가 inbox 기록을 `ready`로 만든다.
   - CI relay는 **완료 15분 이내** 실행만 자동으로 받는다. 놓친 실행은 `--normal-run-id`와 `--expected-head`로 명시해 넣는다.
   - inbox 기록은 7일 보관이다. 7일보다 오래된 근거만 있으면 준비가 멈춘다.
6. **binding 파일:** `normal_binding_file`에 현재 정상 CI binding(정상 이미지·Pod 고정)을 마운트한다.
7. **초안 경로:** `drafts_dir`를 PVC(0700)에 둔다.
8. **Slack 앱**
   - Interactivity Request URL을 `https://<Slack 수신 호스트>/slack/interactivity`로 설정한다. `/slack/events`와 같은 리스너다.
   - 승인자를 `slack-policy.json` grants와 `principals`에 모두 넣는다.
9. **renderer:** WF10 renderer 입력에 선택적 `recovery` 절을 넣으면 다음을 함께 렌더링한다(PR62, [조각 2 문서](recovery-egress-renderer-local-20261124.md)). `recovery` 절이 없으면 기존 후보와 바이트 단위로 같다.
   - `recovery-approval.json`과 binding
   - relay 전용 Secret 마운트
   - 송신 proxy Pod와 NetworkPolicy
10. **`check-config`:** 세 플래그를 확인한다.
11. **네트워크 송신:** 고정 목적지 송신 proxy를 쓴다(7절 1항, PR61·62). `prepare.egress_proxy`를 proxy Service로 지정한다. 메인 Pod의 송신은 proxy로만 나가고, proxy는 GitHub·STS·ECR :443으로만 나간다.
12. **재시작 경로(선택, `restart` 절):**
    - 2항의 읽기 신원에 `ai-ops-lab` `events` list, `horizontalpodautoscalers` list(autoscaling/v2)를 더한다. 쓰기 권한은 여전히 주지 않는다.
    - Prometheus 읽기 토큰·CA와 질의(`prior_ready_query`, `dependency_queries`)를 정한다. 질의가 없으면 게이트가 막는다.
    - 메인 Pod 송신은 기존 수집기 연결 목적지(Prometheus 포함)를 이미 허용한다.
    - renderer 입력 `recovery.restart`로 `restart` 절과 relay 전용 Prometheus Secret 마운트를 렌더링한다(2026-11-25, [renderer restart 절](wf10-restart-render-local-20261125.md)). Prometheus 주소는 수집기 연결과 같다.

## 6. 지원 범위

| 구분 | 상태 |
|---|---|
| 조치 `image-rollback`(PR 방식) 승인·재검증·PR 준비 | 코드 연결 완료, 실환경 미검증 |
| PR 발행(GitHub 쓰기)·병합·Argo sync | **미연결**(`publish_local`은 fixture 전용) |
| 원복(기준선 고정)·드리프트 잠금·원복 관측 | 로컬 계약만(PR51~53), 프로세스 미연결 |
| 조치 `deployment-restart`(명령 방식)·`execute_command` 승인 | **실행 직전까지 연결**(요청·봉인·승인·재검증·실행 조건 충족 알림). 실제 재시작 실행은 비활성(2026-11-25) |
| 재시작 실제 실행(Deployment patch·Lease·증거 저장·효과 관찰) | **미연결**(WF11 실환경 실행 결정 대상) |
| "이미 회복됨" 자동 판정(healthy) | **연결**(5가지 모두 통과 + Ready 60초 지속, 2026-11-25). 열 때 정상이면 전용 회신, 재검증 때 정상이면 `already_recovered` |
| 재시작 기록 보관 정리 | 연결됨(보관 종료 30일 뒤 삭제). 증거 파일은 실행 연결 때 |

## 7. 미결정·남은 한계

1. **relay의 외부 송신은 해결됐다 (2026-11-24, 사용자 결정: 고정 목적지 송신 proxy).**
   - [proxy 조각 1](recovery-egress-proxy-local-20261124.md)(PR61): proxy 본체를 만들고, GitHub reader와 IRSA client(자격 교환 STS 포함)가 설정된 proxy만 쓰게 했다. 이 과정에서 boto3 내부 STS의 dualstack·endpoint 재지정 결함을 고쳤다.
   - [proxy 조각 2](recovery-egress-renderer-local-20261124.md)(PR62): renderer에 proxy Pod와 NetworkPolicy를 추가했다.
   - 잔여 위험: relay가 침해되면 세 호스트에는 도달할 수 있다.
2. **정상 판정이 생겼다(2026-11-25).** 다섯 가지 확인을 모두 통과하고 Ready가 60초 이상 지속되면 정상이다. 하나라도 확인할 수 없으면 지금처럼 `needs_review`다. 실환경에서 `pods/proxy` 권한과 앱 응답 계약은 WF11에서 확인한다.
3. **준비 후 관측 만료 알림은 정리됐다.** PR 준비가 끝난 뒤 관측 유효 시간이 지나면, 이제 전용 문구가 간다(2026-11-25, [작은 항목](recovery-small-fixes-local-20261125.md)). 문구는 "준비된 내용이 있어도 발행·변경하지 않으며, 진행하려면 새 승인·재검증이 필요"라는 뜻이다.
4. **승인자는 계획 요청자 본인만 가능하다.** 재시작 명령 승인도 같다.
5. **승인 정책 전환 절차가 생겼다.** relay와 ingress를 멈춘 뒤 `slack_server transition-approval-policy --previous <이전 설정>`을 실행한다. 이전 정책 hash가 일치하고 버전이 올라가야 하며, 진행 중이던 승인은 superseded가 된다(2026-11-25). PR 준비·재시작 명령 두 범위를 함께 다루며, 바뀐 범위만 전환한다.
   - 승인 장부와 WF07 기록의 tombstone은 보관 종료 30일 뒤 삭제한다. 장부가 상한에 차서 막히던 결함을 해소했다.
6. **동시 접근 시 버튼 처리가 실패할 수 있다.** ingress와 relay가 Slack DB 잠금을 동시에 잡으면 버튼 처리가 503이 된다. Slack은 다시 보내지 않으므로 사용자가 다시 눌러야 한다. 같은 누름은 멱등이다.
7. **HA를 지원하지 않는다.** replica는 1개이고 SQLite(RWO)를 쓴다.
8. **이전 운영 미결정은 그대로다.** 비가역 부수효과 승인 항목, 정렬 PR 절차.

## 8. 안전 경계 요약

- **쓰기:** 클러스터 쓰기, GitHub 쓰기, 배포는 0회다. 모든 결과는 `execution_allowed=false`다.
- **fail-closed:** 관측·증거·자격·네트워크 중 하나라도 실패하면 열지 않거나 `needs_review`로 멈춘다. 확실한 실패 증거가 없으면 `unknown`이다.
- **"없음" 판정:** ECR에서 실패 이미지가 없다는 것은 `ImageNotFoundException`일 때만 인정한다.
- **오류 노출:** 오류 문구, 응답 본문, 토큰, 경로, ARN은 Slack·로그·장부에 남기지 않는다. 고정 코드만 쓴다.
- **반복:** 같은 단계는 반복하지 않는다. 불분명한 Slack 전송은 다시 보내지 않는다.
- **권한:** 자격은 고정 경로 읽기 신원, 격리 저장소 읽기 토큰, 지정된 IRSA 역할만 쓴다. 고정 경로·고정 저장소·고정 리전만 허용한다.

## 9. 검증 근거

- **조각별 시험과 변형 검사:** 각 조각 문서와 검증 JSON에 있다.
  - 조각 3의 변형 검사 근거는 2026-11-24에 정정했다. 병합된 커밋에서 다시 검사했고 13/13이다.
  - 그 뒤로 변형 실행기는 전체 환경을 상속하고, 기준선 통과를 먼저 확인한다.
- **main CI:** PR59 반영 main CI 45354867715 성공(1786개). boto3를 설치하면서 기존에 건너뛰던 `test_c_trial_aws` 시험이 실행되어 skip이 1개가 됐다.
- **실환경:** 아직 검증하지 않았다(WF11).

## 10. 다음 단계 (제안)

1. **7절 1항 결정:** relay 외부 송신 방식을 정한다.
2. **연결 준비:** 인프라 재설치 요청과 Terraform 실행 주체를 정한 뒤, 5절 체크리스트를 실제 값으로 채운다. 실제 입력은 추측하지 않는다.
3. **WF11:** 합성 경보 또는 승인된 장애 실습으로 단계 1~6을 실물에서 확인하고, 이 문서를 확정한다.
4. **그 뒤:** PR 발행 경로(쓰기, 별도 승인 설계), 재시작 실제 실행(쓰기 신원·실행기, WF11 결정), 그 다음 원복 프로세스 연결(원복 승인 흐름 `RollbackScope` 발급자 설계 포함; 상위 실행 경로가 있어야 계기가 생김).

## 반영 기록

[PR #60](https://github.com/joungGo/url-shortener-ai-ops/pull/60) head `87f8d05d155e93b94026b0b31aab8a5904d81ad6`를 head 고정 일반 squash 병합해 main `4a623096fdf17dea464ae100fd45ec388395d8e1`에 반영했다(사용자 "진행해" 후). [PR CI45355975980](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/45355975980) 7종 성공, tree `9f282f470649c4ba561d38cf2c2325f743d58df8` 일치, [main CI45356948426](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/45356948426) 성공. 문서만 바꿨고, 병합 후 branch를 정리했다.
