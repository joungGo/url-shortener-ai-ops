# WF11 실제 실행 기록 · 2026-11-26

상태: **S1(범위 B) 실행 완료, S2a 미실시, 철거 완료.** 시각은 모두 한국 시간(KST)이다. 증거 원본은 개인 경로에만 있고 저장소에는 넣지 않았다.

- 설치: `~/.local/share/url-shortener-ai-ops/install-20261126`
- 철거: `cleanup-20261126-wf11`
- 증거: `install-20261126/s1/evidence`

## 1. 결정과 범위

| 항목 | 결정 |
|---|---|
| 위임 | Claude가 Terraform 설치·철거를 수행한다(사용자 "진행해"). 장애 주입 sync, Slack 공지 연결, 복구 방식은 단계마다 사용자가 승인했다 |
| 범위 | **B.** WF10 renderer에 CI relay가 없어 정상 CI 근거가 공급되지 않는다. 그래서 WF07은 안전 중지를 기대 결과로 삼았다 |
| 시간 상한 | 3시간 + 사용자 연장 1시간 30분. 사용량 한도로 약 65분 정지한 탓이다. 철거 시작 기한은 19:11, 실제 시작은 18:17 |
| AI 저장소 | 사용자 결정으로 PUBLIC 전환했다. 비공개 저장소 Actions 결제 차단을 피하려는 것이다. 전환 전에 이력·PR·실행 로그 비밀 검사를 했고 실제 비밀은 0건이었다 |

## 2. 설치 (14:41 시작)

1. **사전 확인(읽기):** 상태가 깨끗했다(state 비어 있음, 잠금 0, 잔여 리소스 0). 최신 최종 스냅샷 `…a1a986fd`로 복원했다.
2. **production Terraform:** 102개 생성 계획(생성만)을 검토한 뒤 적용했다.
   - **실패 1건.** 코어 설치를 EKS ACTIVE 직후 병렬로 시작했더니, LB Controller의 Service webhook이 아직 endpoint 없이 등록돼 있었다. 그 상태에서 CoreDNS 애드온의 Service 생성이 거부됐다(`CREATE_FAILED`). DNS가 없으니 EBS CSI도 DEGRADED로 시간 초과됐다.
   - **처리.** EKS는 `CREATE_FAILED` 애드온의 갱신을 거부한다. 그래서 실패 애드온만 삭제하고, 두 애드온만 다루는 검토 plan(EBS CSI 교체, CoreDNS 생성)으로 복구했다.
3. AI root를 적용했다(6개: 레지스트리 4, 복구 IRSA 2).
4. 이미지
   - AI 이미지: 공개 저장소에서 `ba620c2`로 빌드했다. 취약점 0건. ECR에 반입했다.
   - 앱 이미지: `sha-d195fcb`. CRITICAL 0건.
   - lab 기준선 `dbd367…`: 보존된 OCI 보관본에서 복원했다.
5. RDS 비밀번호를 회전했다(검토 plan, 수정 1건). 코어, 모니터링(외부 Slack 수신기는 끈 상태), cert-manager, Traefik, Karpenter, 앱(2 Pod, DB·Redis 확인)을 설치했다. HTTPS·DNS는 `api`·`argocd`·`slack-ops`를 새 NLB로 UPSERT했다.
6. 격리 lab을 설치했다.
   - 기존 설치 도구의 운영 불변 확인이 B 방식 AI worker(StatefulSet·PVC)를 전제하고 있었다. 저장소 코드는 바꾸지 않고, 설치 쪽 wrapper에서 그 두 항목만 뺐다.
   - lab GitOps는 정상 revision `0cfef83`에 수동 sync로 고정했다.
7. WF10
   - 입력을 작성했다. binding·runtime은 lab collector ConfigMap에서 가져왔고, 정상 CI binding은 실제 Pod로 계산했다. PR 작성자는 C03 writer 봇 `354870297`이다.
   - `wf11_edge` 13개, PVC, Secret 4개를 적용했다.
   - **첫 기동 실패.** ConfigMap 볼륨 파일은 심볼릭 링크인데, 읽기 코드가 `O_NOFOLLOW`로 거부했다. [PR #77](https://github.com/joungGo/url-shortener-ai-ops/pull/77)로 init 복사 방식으로 고쳐 병합했다(`1f7bbd1`). 병합 전 branch 렌더링으로 실습 클러스터 기동을 먼저 확인했고, main 렌더링도 같은 manifest(`3195a002…`)였다.
8. **S0:** 아래 항목을 모두 확인했다.
   - `check-config` valid(recovery 세 플래그 true)
   - `LabDeploymentUnavailable` 로드, kube-state-metrics 수집
   - Alertmanager → ai-events 전송 성공(TLS·토큰)
   - Slack 인증서 Ready, `slack-ops` TLS 검증 통과
9. 사용자 승인 후 Alertmanager Slack 공지와 Watchdog을 연결했다.

## 3. S1 (범위 B)

| 시각 | 단계 | 결과 |
|---|---|---|
| 17:40:56 | 주입. `lab-target`을 기존 main 실패 seed(`0668a1c`)로 sync했다. 정상 revision과 `values.yaml` 1줄 차이이며, 사용자가 이 방식을 승인했다 | `ErrImagePull`. Argo targetRevision = sync revision |
| 17:43:12 | `LabDeploymentUnavailable` 발생(주입 후 약 2분 16초) | Slack 알림 채널 공지(전송 실패 0), WF10 사건 등록. 라벨이 lab 대상과 일치 |
| 17:52 | 조사 1차 | 증거 부족. **수집 경로 차단:** NetworkPolicy가 API 서버·Prometheus의 Pod/endpoint IP만 허용하고 Service IP(`172.20.0.1`, Prometheus Service)는 막았다. VPC CNI 정책은 DNAT 전 주소로 판정한다 |
| ~17:55 | 입력 `collector_cidrs`에 두 Service IP `/32`를 추가하고 다시 렌더링했다 | diff는 NetworkPolicy만. 연결 확인 |
| 17:56 | 조사 2차 | **원인 후보 있음**(Pod 상태·배포 정보·이벤트 확보) |
| 17:59 | `plan-recovery … image-rollback` | **지원 조치와 근거 일치**, 계획 `8ed968d3…` |
| 18:01 | 승인 요청(다른 스레드) | 거절. 계획 스레드와 달라서이며 설계대로다. 거절 문구는 사유를 구분하지 않는다 |
| 18:04 | 승인 요청(계획 스레드) → 승인 | 대기 열림 → **relay 재검증 통과(`proposal_ready`)** |
| 직후 | WF07 | **`preparation_failed`**. 원인 `normal_ci_history_unavailable`을 확인했다(기대와 일치) |
| 이후 | 승인 관측 | `observation_expired` → `needs_review`. 더 진행하지 않았다 |
| 18:10:20 | 복구. 사용자 A안에 따라 Argo로 정상 revision `0cfef83`에 sync했다 | Ready 60초 이상(재시작 0, 이미지 B), `/readyz` ready, `/items` 200, 경보 해제 |

복구 PR 경로는 검증하지 못했다. `c03-execution-lab` main 보호 규칙(리뷰 1, `enforce_admins`)상 본인 계정 PR은 병합할 수 없고, writer 앱 경로는 C-04 코드라 쓰지 않았다. S2a는 시간 상한 때문에 하지 않았다.

## 4. 철거 (18:17 시작, 18:50 완료)

1. inventory(읽기) → 클러스터 정리. 기본 namespace 4개만 남았고 PVC·EBS·LB 의존성을 제거했다.
2. ECR 이미지를 로컬 OCI로 백업했다(7 ref, 62 blob, 240MB, SHA-256 검증). 그다음 ECR을 비웠다.
3. AI root 6개를 삭제했다. 검토 기록의 고정값 10은 6으로 정정했다.
4. RDS
   - 보호 해제: 검토 plan, 수정 1건
   - replica 삭제
   - primary 삭제와 **최종 스냅샷 `urlshortener-postgres-final-snapshot-urlshortener-postgres-b6f690c5` available**
5. production 100개를 삭제했다(추가·변경 0). 두 root 모두 0이다.
6. 최종 검증: EBS 9·EIP 1 부재, CloudWatch 고아 1개는 보관 후 삭제했다. DNS·state·operator·스냅샷 KMS는 보존됐다.
7. 자격 회수: 내부 CA 키, 사건·sender 토큰, 로컬 GitHub 토큰 파일을 삭제했다. GitHub 토큰 자체의 폐기는 사용자가 한다.

## 5. 발견과 후속 후보 (모두 인프라 없이 가능, 허락 후)

1. **CI relay 연결:** WF10에 CI relay가 없어 WF07이 정상 CI 근거를 얻을 수 없다(이번 범위 B의 원인).
2. **정상 판정 경로**(후속 PR에서 수정, [변경 문서](lab-apiserver-proxy-ingress-20261126.md)): API 서버 `pods/proxy` → `lab-api:8000`이 lab NetworkPolicy에 막힌다. 실제 환경에서 복구 효과 정상 판정이 `unknown`이 될 것이다. 이번 회복 확인은 Pod 안에서 직접 요청해 대신했다.
3. **WF10 입력:** `collector_cidrs`에 Service ClusterIP가 필요하다는 점을 도구나 문서로 고정한다.
4. **설치 도구**
   - CoreDNS 등 EKS 애드온 ACTIVE를 기다린 뒤 LB Controller를 설치한다.
   - lab 설치 도구의 B worker 전제를 정리한다.
5. **승인 거절 안내:** 요청자 본인 계획의 스레드 불일치를 구체적으로 안내한다(사용자 보류).
6. **복구 PR 경로·S2a:** 다음 실행에서 검증한다. 작성자 신원과 보호 규칙 정리가 먼저 필요하다.

## 6. 다음 재설치 준비 (2026-11-26 철거 후)

- **잔여 확인(읽기):** EKS·RDS·NAT·EIP·LB·EC2·EBS·VPC·ECR 0개. 보존한 것은 RDS 수동 스냅샷 11개(최신 `…b6f690c5`), Terraform state 2개, DNS, operator다.
- **재사용 키트(개인 경로):** `~/.local/share/url-shortener-ai-ops/wf11-reinstall-kit`. 이번에 실제로 쓴 설치·철거 도구를 모았고, 저장소에는 넣지 않는다.
  - 이번 실행에 고정됐던 값(소스 SHA, 실행 ID, 복원 스냅샷)은 실행마다 `sources.json`·preflight 결과에서 읽도록 바꿨다.
  - 이번 실행에서 고친 내용을 미리 반영했다.
    - CoreDNS 등 애드온 ACTIVE 대기
    - `collector_cidrs`에 Service IP 포함
    - `slack-ops` DNS UPSERT
    - 사건 수신 SAN `ai-ops-events.ai-ops.svc`
    - lab 설치 wrapper
  - 쓰지 않은 B 방식 도구(CI 서비스, AI worker 등)는 별도 폴더로 분리했다.
  - `new-run.py YYYYMMDD`가 새 실행 디렉터리를 만든다(소스 내보내기, 경로 치환, 설정 준비, AWS 호출 없음). 임시 날짜로 시험한 뒤 삭제했다.
  - 순서와 사용자 작업은 키트 `README.md`에 있다.
- **다음 설치 전 사용자 작업:**
  - 설치 시작 위임
  - GitHub 읽기 토큰을 다시 발급해 편집기로 저장
  - `aws login` 2회(operator, root)
  - AI 저장소 PUBLIC 확인
