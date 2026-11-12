# AI 작업 인계 · 2026-11-13

## 현재 결정과 재개 지점

**여기서 작업을 마치고, 사용자가 인프라 재설치를 요청하면 설치와 실제 연결 설정을 함께 진행한다.** 재설치 때 다시 확인해야 하는 OIDC·이미지 발행·private chart·실행기/CI/원복/collector 연결 설정을 지금 먼저 실행하지 않는다. 클라우드 조회·자격 발급·workflow 재활성화·실습도 현재 대기한다.

코드 검증과 PR #1 병합은 인프라 없이 완료했고 재설치 후에도 유효하다. 같은 변경을 다시 병합할 필요는 없다. 재개 시 main 변경 여부와 보존 자료 무결성을 먼저 확인하고, 변경되거나 필요한 부분만 추가 검증한다. 아래 현재 상태가 과거 checkpoint/HANDOFF의 “PR1 미병합·다음은 로컬 검증·인프라 삭제 전” 안내보다 우선한다.

## 완료한 작업

### 인프라 정리

- 계정 `764428911194`·서울 리전에서 운영/AI Terraform의 managed resource 각각0개와 잠금 없음, EKS·RDS Primary/Replica·프로젝트 EC2/NAT/VPC/LB/ECR/SQS·기록 EBS10개/EIP1개의 실제 부재를 확인했다. 이는 **2026-11-13 정리 검증 시점의 상태**이며 이번 문서 갱신에서 AWS를 재조회하지 않았다.
- 관리자 세션 만료로 중단된 state 저장은 본인 잠금·lineage/serial·실제 DB 삭제를 확인한 뒤 정상 state push와 리소스 변경0개 새 계획으로 복구했고 운영 잔여100개를 정리했다. 옛 plan/state 복구 helper를 재실행하지 않는다.
- DNS zone/레코드·S3 backend/state/버전·기존 RDS 스냅샷6개와 새 최종 스냅샷·operator/로그인/정책·개인 자료를 보존했다. 새 최종 스냅샷은 `urlshortener-postgres-final-snapshot-urlshortener-postgres-6962a7a0`이며 마지막 확인 시 available/encrypted, 연결 KMS는 Enabled다.
- EKS 전용 KMS는 예약 삭제 상태(마지막 확인 삭제 예정: 2026-12-13 00:55 KST)이며 보존한 RDS 스냅샷 키와 다르다. 공유 `RDSOSMetrics` 그룹은 빈 상태로 보존했다. DNS·스냅샷·S3·보존 KMS 등은 남아 있으므로 비용0을 뜻하지 않는다.
- 배포/실습 workflow6개는 동결했고 폐기한 Argo 서버 전용 Secret/변수·임시 원복 Secret을 제거했다. GitHub 저장소/App/나머지 설정은 유지했다.

### AI 저장소 분리와 코드 반영

- AI 구현·시험·전용 chart/Terraform의 기준: 비공개 [joungGo/url-shortener-ai-ops](https://github.com/joungGo/url-shortener-ai-ops), 로컬 `/Users/jounghyeon/dev/url-shortener-ai-ops`.
- 운영 앱/EKS/RDS/VPC와 운영 GitOps: `f-lab-edu/F-Lab-DevOps`. 격리 복구 PR: `joungGo/c03-execution-lab`. repository literal·workflow ID·binding을 일괄 치환하지 않는다.
- [PR #1](https://github.com/joungGo/url-shortener-ai-ops/pull/1)의 검증 head `0997a96142ca37e8195b9574c7766b095ac81753`를 일반 squash 병합했다. 구현 반영 기준 커밋은 **`69f99a08d25242b5d7774aacdc2844bb471f0997`**이다. 이후 문서 전용 로컬 커밋은 이 구현 기준과 구분한다.
- 초안 해제 후 [PR CI 43257307388](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/43257307388)의5개 검사가 모두 성공했다. [main CI 43257778832](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/43257778832)도 성공했다(4성공, PR 전용 gate는 push에서 skip). 병합 트리/검증 파일 해시 일치와 기존 feature 브랜치·미커밋 문서 보존을 확인했다.
- 마지막 조회 시 비공개 요금제의 보호 기능 미지원으로 main은 protected=false, 제출 리뷰0건이었다. 필수 별도 리뷰 규칙 없이 사용자의 반영 요청으로 일반 병합했으며 관리자 우회·보호 변경·공개 전환은 하지 않았다. 격리 복구 PR의 실제 사람 리뷰 요건을 대신하지 않는다.
- 원본 플랫폼 PR #156은 이번 작업에서 변경하지 않았다. 같은 보완은 AI main에 반영됐으므로 과거 PR156 병합/보호 완화 helper를 다시 실행하지 않는다. archive 브랜치는 현재 코드 위에 덮어쓰지 않는다.

### C-04 로컬 재검증

- 전체880개·집중15개 테스트/skip0, 구문287파일·schema14개·fixture4종·저장소 경계·고정 운영 chart Helm 검사를 통과했다. Python3.13.12/Pydantic2.13.5를 포함한 잠금 의존성을 사용했다.
- 마지막012의 관측 시각이 요청보다 **2.009996초 뒤**였고 다른 기록된 결합 조건은 일치했음을 재확인했다. 어느 호스트의 시계가 원인인지는 미측정이다.
- 미래 관측 요청의 저장 전 거절, 최대5초/기존 마감 내 실제 대기, 동일 인증 투영 재조회, 시계 변경·조회 실패·투영 변경·마감 초과 시 중지를 검증했다. 구현 코드는 추가 수정하지 않았다.
- [로컬 검증](ai-ops/docs/C4-local-validation-20261113.md)·[PR1 반영](ai-ops/docs/C4-pr1-integration-20261113.md)과 각 assets JSON에 결과가 있다. **C-04 실환경 복구 성공·C-05 최종 완료는 여전히 미완료**다. A5 HolmesGPT 비교·A7 독립 사람 평가/수동 기준선은 미측정이며 기존 B 범위와 AI 포트폴리오 보류를 유지한다.

## 보존 자료 위치

아래 개인 폴더는 Git에 넣지 않는다. 과거 UID·짧은 토큰·승인·큐/사건·plan은 역사 자료이며 새 실행의 현재 근거로 재사용하지 않는다.

| 위치 | 보존 내용 |
| --- | --- |
| `/Users/jounghyeon/.local/share/url-shortener-ai-ops/cleanup-20261112-after-ai-split/` | 정리 최종 실물/state/잠금 복구 근거·CHECKSUMS·현재 이미지 `images.oci/`·최신 AI 자료 `volumes/`·RDS 로그 백업·정리 시점 인계 |
| `/Users/jounghyeon/.local/share/url-shortener-ai-ops/continuation-20261112/` | Git bundle3개·요청SQLite10개·AI PVC3개 파일125/SQLite4개·wheelhouse/venv·verify-checkpoint.py |
| `/Users/jounghyeon/.local/share/url-shortener-ai-ops/repository-separation-20261112/` | 분리 저장소 Git bundle와 이관 무결성 자료 |
| `/Users/jounghyeon/.local/share/url-shortener-ai-ops/c04-20261112/` | 과거 실습·정상 원복·자격 회수·012 진단·원본 PR156 근거 |
| `/Users/jounghyeon/.local/share/url-shortener-ai-ops/local-c04-validation-20261113/` | 새 로컬 테스트/정적 검사 로그·고정 chart 사본·결과/해시 목록 |
| `/Users/jounghyeon/.local/share/url-shortener-ai-ops/pr1-integration-20261113/` | PR1 전후·CI·병합·로컬 main 검증·문서 사본 |
| `/Users/jounghyeon/.local/share/url-shortener-ai-ops/install-20261112/` | 설치 원본·기존 입력(옛 plan은 재사용 금지) |
| `/Users/jounghyeon/.local/share/url-shortener-install/credentials/` | 기존 원본 자격7개(0600); App 키2개는 `~/.local/share/url-shortener-ai-ops/c03-app-keys/`, C02 원본3개는 `c2-projection-identity-20261028/` |
| `/Users/jounghyeon/.local/share/url-shortener-ai-ops/handoff-20261113-reinstallation/` | 이번 정리 전/후 양쪽 HANDOFF 사본과 SHA256 목록 |

인프라 상세 기록은 `/Users/jounghyeon/dev/Url-Shortener-EKS-Platform/docs/runbooks/infrastructure-cleanup-2026-11-13.md`와 같은 이름 JSON을 따른다. 원본 자격은 보존하되 새 세션·짧은 실행 토큰은 유효성을 다시 확인하거나 새로 발급한다. 과거 활성 큐/승인 DB를 새 worker의 활성 상태로 복원하지 않는다.

## 재설치 요청 후 이어갈 순서

1. **현재 상태와 새 계획 확인:** 양쪽 AGENTS/HANDOFF와 [연결 계약](integration/README.md)을 읽는다. AI main 변경 여부·보존 자료 무결성, 계정764428911194/서울 리전·기존 세션/권한·현재 backend·DNS·최종 스냅샷/KMS를 확인한다. 새 AWS 계정/프로필을 만들지 않고 일상 기본은 기존 operator를 사용한다. 필요한 권한 부족만 구체화한다. 삭제 완료 후 정상적으로 빈 backend를 사용하며 옛 state/오류state를 덮어쓰지 않는다.
2. **운영 기반 재설치:** 플랫폼 설치 런북에 따라 새 Terraform plan·대상/보존 대조·사용자 실행 명령을 준비한다. **terraform apply/destroy는 사용자 터미널에서 직접 실행**한다. 완료된 이번 정리/과거 설치의 Codex 직접 실행 예외는 새 설치에 재사용하지 않는다. 최신 보존 RDS 스냅샷 복원과 새 비밀번호 회전·operator/기존 로그인 보존을 반영하고 사용자 실행 후 state/실물을 확인한다.
3. **AI 연결을 이번 설치와 함께 준비:** 새 저장소의 이미지 발행 환경·정확한 repository/environment OIDC 신뢰·private chart 읽기/검토 SHA를 구체화한다. control repository·workflow ID·정상 CI source revision/artifact 조회·collector binding·원복 감시를 함께 변경·검증한다. 새 저장소로의 권한/자격 확대를 과거 예외로 자동 승인된 것으로 보지 않는다. 현재 `integration/platform-source.json`의 `pending_reinstallation_review`와 기존 플랫폼 control identity는 아직 전환 전 상태다.
4. **이미지/AI 리소스 복원:** 새 ECR의 이미지 존재·digest/scan을 검사한다. 보존 OCI 사본의 재사용 가능 여부를 먼저 확인하고 필요하면 검토된 코드와 승인된 workflow로 빌드·등록한다. 새 암호화 PVC(AI5Gi, ci-queue1Gi, collector1Gi)와 RBAC·worker/relay·조회 경로를 준비한다. 유료 모델 호출/모델 활성화는 추가하지 않는다. 격리 `lab-target`은 새로 검증한 정상 revision에 수동 고정한다(과거 실습 Git main과 정상 revision이 달랐으므로 무조건 동기화하지 않는다).
5. **새 정상 기준선 확보:** 현재 Pod/Deployment/Application/Lease/PVC UID·endpoint·인증·binding·ConfigMap·GitHub 환경·이름 제한 권한을 재대조한다. 새 정상 CI artifact 출처와 worker의 실제 새 설정 로드/처리를 확인하고 기존5분 지표 보호 구간을 지킨다. 연결 확인 후 필요한 workflow만 재활성화한다. 단순 코드 반영/CI 통과를 실행 연결 완료로 간주하지 않는다.
6. **새 C-04 실습:** 양쪽 독립 원복 감시·새 실습 ID/intent/readiness를 준비한다. 준비 완료 후 새 실행별 승인을 받으면 고정 시각 대기 없이 현재 조건을 재검증해 시작한다. 새 복구 PR 링크·정확한 head·마감을 즉시 안내하고 실제 GitHub 사람 리뷰/CI 후에만 병합·동기화한다. 시작 예산15초·실제 안내 후 리뷰 최소120초·시작 후 리뷰 마감210초·소유 원복240초·독립 원복 시작300초·회복 관찰120초와 임시 자격/Secret 회수를 유지한다. 이전 실습 승인·PR 리뷰·ready hash는 재사용하지 않는다.
7. **C-05/최종 정리:** 실제 병합·동기화·정상 응답·관찰·취소/중단/독립 원복·회수 근거를 종합해 완료 여부를 보고한다. A5/A7 미측정과 한계를 구분한다. 연결 전환을 확인한 뒤에만 플랫폼의 중복 AI 코드를 별도 PR로 정리한다. 사용자의 AI 구현 완료 통보 전까지 포트폴리오 AI 내용은 보류한다.

## 로컬 Git 상태와 다음 담당자 주의

현재 main의 구현 반영은 PR1로 끝났다. 사용자의 후속 요청에 따라 **HANDOFF 및 `ai-ops/docs/C4-local-validation-20261113.md`, `C4-pr1-integration-20261113.md`와 각 assets JSON만 한글 메시지의 문서 전용 로컬 커밋으로 보존**한다. 플랫폼 저장소도 HANDOFF와 2026-11-13 정리 결과 문서/JSON만 별도로 커밋한다. 원격 push는 하지 않으며 다른 기존 미커밋 자료는 포함하거나 삭제/초기화하지 않는다. 새로 변경된 코드가 없다면 PR1 병합과 이미 완료한 전체 로컬 검증을 처음부터 반복하지 않는다.

다음 작업 추천: **GPT-6 SOL · High**. 다음 실작업은 사용자의 재설치 요청 이후다.
