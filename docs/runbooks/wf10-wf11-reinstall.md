# AI 운영 재설치와 실습 절차 (WF10 경로 · WF11 시나리오)

기준: AI main `1ee4192` 이후(PR #77·#79~#86 포함). 작성 2026-11-27. 근거는 [WF11 실행 기록](../../ai-ops/docs/wf11-execution-20261126.md)과 이후 후속 PR 문서다.

이 문서는 **현재 쓰는 AI 설치 경로**다. 예전 B 경로(수동 큐 worker StatefulSet·PVC)는 쓰지 않는다. 예전 절차는 [AI 클러스터 설치·정리](ai-ops-cluster-lifecycle.md)에 역사 기록으로 남아 있다.

## 0. 원칙

- **실물 작업은 건별 명시 위임 후에만** 한다. 설치·Slack 연결·장애 주입·복구 sync·철거는 각각 사용자 승인이 필요하다.
- **GitHub 환경 승인은 사용자가 UI에서 클릭한다.** API로 우회하지 않는다.
- **비밀 값은 출력하지 않는다.**
  - Secret은 stdin 또는 파일 참조로만 만들고, 끝 줄바꿈을 제거한다.
  - 로그와 기록에는 이름·hash·상태만 남긴다.
- **각 단계는 앞 단계 기록이 있어야 진행한다.** 실패하면 멈춘다. 같은 단계가 2회 실패하면 멈추고 보고한다.
- 플랫폼(인프라·앱·모니터링·Traefik·Argo CD) 설치는 플랫폼 저장소의 [인프라 재설치 가이드](https://github.com/f-lab-edu/F-Lab-DevOps/blob/main/docs/runbooks/infrastructure-installation-and-validation.md)를 따른다. 이 문서는 그 뒤의 AI 부분만 다룬다.

## 1. 시작 전

1. 설치 시작 위임과 **시간 상한**을 확인한다. 철거는 상한 안에 시작해야 한다.
2. AI 저장소 `joungGo/url-shortener-ai-ops`가 PUBLIC인지 확인한다. 비공개면 Actions 결제 한도 때문에 이미지 빌드가 막힐 수 있다.
3. 다음 자격을 준비한다(값은 운영자 개인 경로에만 둔다).

   | 용도 | 범위 | 들어가는 Secret |
   |---|---|---|
   | c03 읽기 | fine-grained 토큰, `joungGo/c03-execution-lab` Contents: Read 하나, 짧은 만료 | `ai-ops-recovery-github` |
   | F-Lab-DevOps Actions 읽기(CI relay) | 정상 CI run·artifact 읽기 | `ai-ops-ci-github` |
   | C03 writer 앱 개인키 | 운영자 Mac에만 보관. 단기 설치 토큰 발급용 | (S1 직전) `ai-ops-pr-writer` |
   | Slack 서명·봇 토큰 | Slack 앱 | `ai-ops-slack-signing`, `ai-ops-slack-bot` |

4. AWS 신원은 STS ARN으로 확인한다. Terraform과 Kubernetes에 쓰는 신원은 플랫폼 가이드를 따른다.

## 2. AI 설치 순서

플랫폼 앱·모니터링(외부 Slack 수신기는 끈 상태)·cert-manager·Traefik·HTTPS·DNS(`slack-ops` 포함)까지 끝난 뒤 진행한다.

| # | 단계 | 확인 |
|---|---|---|
| 1 | **AI Terraform root**(`terraform/ai-ops-registry`)를 검토 plan 후 적용한다. `enable_wf11_recovery_irsa=true`, `enable_b6_normal_ci=true` | 생성 **9개**: 레지스트리 4, 복구 IRSA 2, 정상 CI 3(역할·정책·EKS 접근 항목). 출력 `wf11_recovery_role_arn`, `normal_ci_role_arn` |
| 2 | **AI 이미지**: `ai-ops-oci-bundle.yml`(main) 산출물을 받아 SHA256SUMS를 확인하고 ECR에 반입한다 | 스캔 결과 0건. 반입 증명(`registry_digest`, `source_revision`, `scan_findings`)을 남긴다 |
| 3 | **lab 기준선 이미지**를 보존 OCI에서 복원한다 | digest 일치 |
| 4 | **격리 lab 설치**: `b6_lab.build_lab`으로 manifest를 만들고 `ai-ops/scripts/deploy_b6_lab.py --manifest … --output … --apply`를 실행한다 | Pod 4개 Ready. 운영 앱은 불변, AI 서비스 객체는 있을 때만 불변 확인(PR #86). `api_server_cidrs`에는 kubernetes endpoint IP와 Service IP를 넣는다 |
| 5 | **lab GitOps 고정**: `lab-target` Application을 c03 정상 revision에 고정하고 수동 sync 1회 | Synced·Healthy, `automated` 없음, lab-api UID 불변 |
| 6 | **정상 CI 준비**: `ai-ops/scripts/prepare_b6_normal_ci.py --output <prep>.json` → 결과 `rbac`를 server dry-run 후 적용 → F-Lab-DevOps 환경 `ai-ops-normal-ci`에 `NORMAL_CI_BINDING`(binding JSON, stdin)과 `NORMAL_CI_ROLE_ARN` 설정 | binding은 현재 lab Pod UID를 고정한다. lab Pod가 바뀌면 다시 계산한다 |
| 7 | **WF10 입력**(PR #85): `python -m ai_ops.wf10_inputs collect --output facts.json` → `build --facts facts.json --normal-binding <prep>.json --image-proof <반입 증명> --bot-user-id <봇> --alert-channel-id <알림 채널> --output inputs.json` | 수집은 `kubectl get` 5개만. `collector_cidrs`에 Service IP 2개 자동 포함. renderer 검증 통과 후에만 새 파일로 저장 |
| 8 | **edge·신원**: `python -m ai_ops.wf11_edge --inputs inputs.json --role-arn <wf11_recovery_role_arn> --slack-host <slack 호스트> --output edge.json` → server dry-run → 적용. PVC `ai-ops-data` 적용 | |
| 9 | **Secret**: `ai-ops-slack-signing`, `ai-ops-slack-bot`, `ai-ops-slack-sender-client`, `ai-ops-recovery-github`, `ai-ops-ci-github` | Actions 읽기 토큰은 읽기 전용 호출 1회로 유효성 확인(값 비출력). 사건 수신 TLS·identity, sender client는 플랫폼 기반 설치에서 만든다 |
| 10 | **WF10**: `python -m ai_ops.wf10_deployment build --input inputs.json --output <dir>` → `verify --directory <dir>` → server dry-run → 적용 | 렌더링 결과에 `ci-relay` 컨테이너와 egress `--github-artifacts`가 있어야 한다 |

## 3. S0 점검

- WF10 Pod Ready, relay `check-config` valid(recovery 플래그 3개 true)
- `LabDeploymentUnavailable` 규칙 로드, kube-state-metrics 수집
- Alertmanager → 사건 수신기(`/v1/events`) 전송 성공(TLS·토큰)
- Slack 인증서 Ready, Slack 호스트 TLS 검증 통과
- **API 서버 proxy로 lab-api `/readyz` 200**: `kubectl get --raw /api/v1/namespaces/ai-ops-lab/pods/<pod>:8000/proxy/readyz`(PR #79)
- (사용자 승인 후) Alertmanager Slack 공지와 heartbeat를 연결한다

## 4. 정상 CI 근거

1. `gh workflow run ai-ops-normal-ci.yml -R f-lab-edu/F-Lab-DevOps --ref main`
2. **사용자가 GitHub 환경 `ai-ops-normal-ci` 승인을 클릭한다.**
3. 완료 후 15분 안에 WF10 `ci-relay`가 영수증을 받는지 확인한다(로그에 `"normal":{"submitted":1}` 등).
4. 정상 CI inbox가 `ready`가 되어야 WF07이 복구 diff를 준비할 수 있다. 아니면 WF07은 `normal_ci_history_unavailable`로 안전 중지한다.

## 5. 시나리오 S1 (A2: 복구 PR 자동 발행, 병합은 사람)

Slack 명령은 조사 채널(`C0CK82SD8NL`)에서 등록된 사용자만 할 수 있다.

1. **발행 토큰 발급(실습 직전).** 토큰 수명은 1시간이다. 필요하면 relay를 재시작해 다시 읽게 한다.
   ```
   python -m ai_ops.recovery_pr_token issue --namespace ai-ops --secret ai-ops-pr-writer [--kubeconfig …]
   ```
   출력은 `issued`와 만료 시각뿐이다. c03 한 저장소, `contents`·`pull_requests` 쓰기만 허용된다(PR #84).
2. **주입(사용자 승인).** `lab-target`을 c03 main의 실패 seed로 sync한다.
3. 경보 공지 → `investigate-event <사건 참조>` → `plan-recovery` → **계획 회신이 달린 같은 스레드에서** `request-approval` → 승인 버튼
4. relay가 진행한다: 재검증(`proposal_ready`) → WF07 준비(`prepared`) → **다음 tick에 PR 발행**(`awaiting_review`, Slack에 `PR #N`). 승인 신선도 30초 안에 진행돼야 한다.
5. **사용자:** `c03-execution-lab-writer[bot]`의 PR(`recovery/wf07-<plan>`)을 정확한 head로 리뷰·승인하고, CI 통과 후 병합한다.
6. **사용자 승인 후** `lab-target`을 병합된 main으로 sync한다(targetRevision = 병합 커밋).
7. 회복 확인: Ready 60초 이상, API 서버 proxy `/readyz`·`/items`, 경보 해제.
8. **토큰 즉시 폐기**(철거 전에도 반드시):
   ```
   python -m ai_ops.recovery_pr_token revoke --namespace ai-ops --secret ai-ops-pr-writer [--kubeconfig …]
   ```
   결과는 `revoked` 또는 `already_invalid`여야 한다.

S2a(이미 회복 → `already_recovered`)는 PR 없이 진행한다. 토큰이 필요하면 다시 발급하고 끝나면 폐기한다.

## 6. 철거

1. 발행 토큰이 남아 있으면 폐기한다(5절 8).
2. 증거(WF10 `/data`)를 개인 경로에 보관하고 sha256을 남긴다.
3. 클러스터 정리 → ECR 이미지 OCI 백업 → ECR 비우기 → AI root destroy(검토 plan, 9개).
4. RDS 보호 해제 → replica → primary 삭제와 최종 스냅샷 → production destroy(플랫폼 가이드).
5. 최종 확인: EBS·EIP·LB·잔여 리소스 0. DNS·state·operator·스냅샷은 보존한다.
6. 자격 회수: 내부 CA 키, 사건·sender 토큰, 로컬 GitHub 토큰 파일을 삭제한다. GitHub 토큰 자체의 폐기는 사용자가 한다.

## 7. 알려진 함정

| 증상 | 원인 | 대응 |
|---|---|---|
| 조사가 증거 부족으로 끝남 | VPC CNI 네트워크 정책은 DNAT 전 Service IP로 판정한다. `collector_cidrs`에 Service IP가 없으면 수집이 막힌다 | `wf10_inputs`가 API 서버 endpoint, Service IP 2개, Prometheus Pod IP를 모두 넣는다 |
| 승인 요청이 거절됨 | 계획 스레드와 다른 스레드에서 보냈다(설계대로). 본인 계획이면 "다른 스레드" 또는 "사건 참조가 다름"이라고 안내한다(2026-11-28) | `plan-recovery` 회신이 달린 스레드에서 다시 보낸다 |
| WF07이 `preparation_failed`(`normal_ci_history_unavailable`) | 정상 CI 영수증이 없다 | 4절을 먼저 끝낸다 |
| 승인 관측이 `observation_expired` | 승인 신선도 30초 안에 진행되지 못했다 | relay가 자동으로 진행해야 한다(A2). 수동 단계로 바꾸지 않는다 |
| 복구 효과 정상 판정이 `unknown` | API 서버 `pods/proxy` → lab-api:8000이 막혔다 | lab NetworkPolicy의 `api_server_cidrs`를 확인한다(PR #79) |
| WF10 첫 기동 실패 | ConfigMap 볼륨 파일이 심볼릭 링크다 | main에 PR #77(init 복사)이 포함돼야 한다 |
| lab 설치가 운영 불변 확인에서 실패 | 예전 도구가 B worker를 전제했다 | main에 PR #86이 포함돼야 한다 |
| 코어 설치 중 CoreDNS 애드온 `CREATE_FAILED` | EKS ACTIVE 직후 LB Controller webhook이 먼저 등록됐다 | CoreDNS·kube-proxy·vpc-cni 애드온 ACTIVE를 기다린 뒤 LB Controller를 설치한다 |
