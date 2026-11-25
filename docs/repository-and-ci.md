# 저장소 구조와 CI 검사 안내 · 2026-11-25

기준 main은 `8db0878`(PR63)이다. 이 문서는 `joungGo/url-shortener-ai-ops` 저장소의 **디렉터리 구성**과 **GitHub Actions 검사**를 설명한다. 각 기능의 근거와 결과는 `ai-ops/docs/`의 조각별 문서에 있고, 흐름 전체는 [WF12 인계 초안](../ai-ops/docs/wf12-recovery-handoff-20261124.md)에 있다.

## 1. 저장소 개요

- **무엇을 담는가:** URL 단축 플랫폼의 운영 증거 검증, 규칙·AI 조사, 제한된 복구 제안과 검증 코드를 관리하는 **독립 비공개 저장소**다.
- **어디서 왔는가:** 플랫폼 저장소 `f-lab-edu/F-Lab-DevOps`의 AI 경로 이력을 보존한 채 분리했다. 대응 관계는 [`docs/repository-extraction.json`](repository-extraction.json)에 있다.
- **무엇을 담지 않는가:** 원문 비밀값, 개인 사건 백업, Terraform state는 저장소에 넣지 않는다. 운영 배포 workflow도 플랫폼 저장소에 남아 있다.
- **어떻게 변경하는가:** PR 단위로만 한다. 정확한 head에서 PR CI가 성공한 뒤 head를 고정해 squash 병합하고, 병합 후 main CI를 확인한다.
  - 비공개 무료 요금제라 branch protection을 쓸 수 없다. 그래서 이 절차는 강제 보호가 아니라 **운영 규칙**으로 지킨다.

## 2. 최상위 구조

| 경로 | 추적 파일 | 내용 |
|---|---|---|
| `ai-ops/` | 961 | AI 운영 코드·시험·설정·문서 전부(아래 3절) |
| `terraform/ai-ops-registry/` | 14 | AI 전용 ECR 등 독립 레지스트리 Terraform root. CI는 `fmt`·`validate`만 실행하고 `plan`·`apply`는 하지 않는다. |
| `integration/` | 4 | 플랫폼 연결·재설치 전환 절차(`README.md`), 고정 플랫폼 소스 SHA(`platform-source.json`, `wf03-platform-source.json`), 초기 이미지 묶음 |
| `cluster-addons/ai-ops/` | 2 | AI 서비스용 Argo CD `Application`(`argocd/ai-ops`, chart `ai-ops/chart`)과 `AppProject` 정의. 자동 동기화는 꺼져 있다. |
| `docs/` | 5 | 저장소 분리 기록(`repository-extraction.json`), 운영 runbook 3개(`runbooks/`: 클러스터·레지스트리 수명주기, 로컬 자격 보존), 이 문서 |
| `.github/workflows/` | 7 | CI 1개와 수동 workflow 6개(5·6절) |
| `HANDOFF.md`, `AGENTS.md`, `README.md` | — | 작업 인계(최신이 위), 작업 지침, 저장소 소개 |

## 3. `ai-ops/` 하위 구조

| 경로 | 추적 파일 | 내용 |
|---|---|---|
| `src/ai_ops/` | 171 | 파이썬 패키지(모듈 163개, 아래 4절) |
| `tests/` | 167 | 단위·통합 시험 164개 파일. 표준 `unittest`만 쓰고, 네트워크·클러스터 대신 가짜 API·loopback·fixture를 쓴다. |
| `scripts/` | 77 | CI 검사 스크립트(`check_*`), fixture 생성·대조(`build_*`), 실습 준비·감사 도구 |
| `config/` | 29 | 예시·로컬 설정(대상 binding, runtime, metric 질의, runbook 정책, WF10 비활성 검토 묶음 등) |
| `schemas/` | 14 | Pydantic 계약에서 만든 공개 JSON 스키마. CI가 다시 만들어 바뀌지 않았는지 확인한다. |
| `evals/` | 175 | 조사 평가 fixture·채점 기준·템플릿 |
| `runbooks/` | 5 | 조사 인용용 runbook(cache·database·deployment), 색인·검색 메타데이터 |
| `chart/` | 12 | AI 서비스 Helm chart(`check_deployment.py`가 렌더링해 권한·통신·저장을 검사) |
| `lab/` | 6 | 격리 실습 앱 chart(`values.yaml`의 이미지 digest가 WF07 복구 PR의 변경 대상) |
| `kubernetes/` | 2 | namespace·StorageClass 정의 |
| `tools/lease-trace-kubectl/` | 7 | C04 Lease 진단용 kubectl 빌드 도구(Go 모듈 잠금) |
| `docs/` | 289 | 조각별 구현·검토·검증 기록과 작업표(`incident-workflow-implementation-tracker.md`) |
| `Dockerfile`, `requirements.lock`, `requirements-trial.lock`, `pyproject.toml` | — | 실행 이미지와 의존성 잠금 |

**의존성 규칙**

- `requirements.lock`은 실행 이미지와 CI가 쓴다. 모든 패키지를 sha256으로 고정하고 `--require-hashes --only-binary=:all:`로 설치한다.
- 2026-11-24부터 boto3 계열 7개가 들어 있다(PR59, ECR 읽기).
- `requirements-trial.lock`은 운영자 PC에서 실습할 때만 쓴다.

## 4. 소스 모듈 묶음 (`ai-ops/src/ai_ops/`)

| 묶음(접두어) | 수 | 역할 |
|---|---|---|
| 기본 계약·조사 | — | `contracts`, `evidence`, `rules`, `collection`, `tasks`/`task_service`, `job_queue`, `worker`/`worker_runtime`, `reports`, `incidents`, `event_store`, `alerts`/`alert_receiver`, `runbook_*` |
| `slack_*` | 9 | Slack 수신(`slack_ingress`·`slack_server`), 회신 relay, 서명 검증 adapter, 무상태 송신기(`slack_sender`) |
| `recovery_*` | 13 | 복구 계획·승인 장부(WF05/06), 버튼 수신·outbox, 프로세스 연결(`recovery_approval_wiring`), 관측기(`recovery_observe`), WF07 읽기 포트(`recovery_sources`·`recovery_proof_live`), 송신 proxy(`recovery_egress_proxy`), 보관 정리 |
| `restart_*` | 3 | 재시작 사전 게이트, Kubernetes adapter, 효과 관찰(로컬 계약, 실제 쓰기 차단) |
| `rollback_*`, `gitops_drift_lock` | 3 | 기준선 고정 원복 실행기·원복 관측·드리프트 잠금(local_fixture 한정) |
| `wf10_*` | 3 | 오프라인 배포 후보 renderer·변경 계획·준비 묶음(적용하지 않음) |
| `c_*`, `c2_*`, `c_trial_*`, `c_verification_*` | 51 | C02~C05 격리 실습 계약: 복구 증거, 승인, PR 연결, Lease, 검증 수집 |
| `b6_*` | 6 | B6 실습 시나리오·GitOps·CI·조정 |
| `ci_*`, `normal_ci_*` | 13 | 실습 CI 이력 검증·relay, 정상 CI 영수증(inbox) |
| `model_*`, `projected_*` | 20 | 모델 gateway·투영·비교(기본 비활성) |
| `live_collectors/` | — | Kubernetes·Prometheus 읽기 수집기 |

**모든 묶음에 공통인 경계**

- 결과는 항상 `execution_allowed=false`다.
- 쓰기(클러스터·GitHub)는 local_fixture에 한정하거나 미연결 상태로 둔다.
- 오류에는 고정 코드만 남긴다.

## 5. 자동 CI: `ai-ops-ci.yml` ("AI 운영 조사 로컬 검사")

**실행 조건과 권한**

- PR 이벤트(`opened`, `reopened`, `synchronize`, `edited`, `ready_for_review`)와, `main` 및 `codex/**` branch push에서 돈다.
  - `claude/**` branch는 push만으로는 돌지 않고, PR을 열어야 실행된다.
- 권한은 `contents: read` 하나다.
- 클라우드 자격, 환경 승인, AWS OIDC, 실제 Slack·클러스터·모델을 쓰지 않는다.

**작업과 의존 관계**

```
scope ──▶ contracts ──▶ image
terraform        (독립)
Lease trace      (독립)
WF03 notice      (독립)
                 └──────▶ C03 Main PR Gate (PR→main일 때만, 위 6개 결과 확인)
```

| 작업 | 검사 내용 | 소요(최근) |
|---|---|---|
| **scope** | 변경 범위 표시. 지금은 항상 `ai_changes=true`다(분리 저장소라 모든 변경이 AI 대상). | 수 초 |
| **contracts** | ① 잠금 의존성 설치(hash 고정), Helm v4.1.3 준비. ② `compileall`, CLI `--help`, **전체 단위 시험**(`unittest discover`, PR63 기준 1817개·skip1). ③ 로컬 통합 검사 스크립트: task service, Slack adapter, runbook 검색·인용, 요청 workflow 설정과 요청 흐름. ④ fixture 결정성(`build_dev/holdout*_fixtures --check`)과 스키마 재생성 후 `git diff --exit-code ai-ops/schemas`. ⑤ sha256으로 확인한 kubectl v1.35.8로 loopback TLS·Lease proxy·인증 검사. ⑥ 고정 SHA의 플랫폼 chart를 읽기 전용 sparse checkout. ⑦ `check_repository_boundary.py`(소유권·실행 경계, 실제 전환 차단). ⑧ `check_deployment.py`(Helm 렌더링 결과의 권한·통신·저장 정적 검사, 클러스터 미접속). | 4~5분 |
| **image** | `docker build` 후, 네트워크 없음·읽기 전용 root·capability 전부 제거·`no-new-privileges` 상태로 실행해 확인: CLI 동작, uid 10001, pydantic 버전, runbook 읽기, runbook 검색 결과. | 약 25초 |
| **terraform** | `terraform/ai-ops-registry`에서 Terraform 1.14.7로 `fmt -check`, `init -backend=false -lockfile=readonly`, `validate`. state·자격 없이 형식만 본다. | 약 25초 |
| **Lease trace local contracts** | Go 1.27.4로 진단용 kubectl을 **오프라인**(`GOPROXY=off`)으로 빌드하고 `go test -race`(인증 exec)를 돌린다. 빌드한 바이너리의 sha256을 고정한 뒤, 합성 자격·loopback으로 Lease 단계·인증·proxy 계약을 검사한다. 15분 제한. | 4~6분 |
| **WF03 event notice integration** | 고정 SHA의 플랫폼 경보 소스(`cluster-addons/prometheus/wf03`)와 sha256을 확인한 amtool 0.32.0으로, TLS 접수와 실제 template의 사건 참조 연결을 검사한다(`check_notice_platform_local.py`). 10분 제한. | 약 20초 |
| **C03 Main PR Gate** | **main 대상 PR에서만** 돈다. 위 작업 결과를 모아, scope·Lease·notice 성공에 더해 contracts·image·terraform이 성공했는지 확인한다. `ai_changes=false`일 때는 contracts·image가 skip이어야 한다. main push에서는 설계대로 skip된다. | 수 초 |

**읽는 법**

- **PR이면 7개가 모두 성공해야 병합한다.**
- main push 뒤에는 6개가 성공하고 Gate는 skip이면 정상이다.
- contracts의 skip 수는 환경에 따라 달라진다.
  - boto3가 lock에 들어가기 전에는 5였다. 지금은 1이다(`test_c_trial_aws`가 이제 실행된다).
  - skip 수가 늘면 원인을 확인한다.
- 외부에서 내려받는 도구는 모두 버전과 sha256으로 고정했다: kubectl, amtool, Go 모듈, 플랫폼 소스 SHA, GitHub Action commit SHA.

## 6. 수동 workflow 6개 (`workflow_dispatch` 전용)

어느 것도 자동으로 실행되지 않는다. 모두 GitHub **환경(environment) 승인**을 받아야 실행되는 실물 작업이고, 승인은 GitHub UI에서 사람이 클릭한다. API로 우회하지 않는다. 같은 대상을 다루는 workflow는 동시 실행 그룹(concurrency)으로 겹치지 않게 막는다.

| workflow | 목적 | 환경 / 자격 | 동시 실행 그룹 |
|---|---|---|---|
| `ai-ops-image.yml` | AI 에이전트 이미지 검증. `publish=true`일 때만 독립 ECR에 등록하고 digest를 기록한다. | `ai-ops-image-build` / `ai-ops-image`, OIDC(`id-token: write`) | `ai-ops-image-<ref>` |
| `ai-ops-oci-bundle.yml` | 설치용 OCI 이미지 산출. 계약 시험과 이미지 검증 후 미등록 이미지와 출처를 보존한다. | 없음 | `ai-ops-oci-bundle-<ref>` |
| `ai-ops-lab-ci.yml` | S10 격리 실습: 실패 배포 → 최초 실패 기록 → (승인 시) C-02 관측 창 → 고정 revision으로 복구 → 복구 검증. | `ai-ops-lab-ci`, OIDC | `ai-ops-b6-lab` |
| `ai-ops-normal-ci.yml` | 정상 캐시 순서 관측. 자기 아이템을 네 번 조회해 정제 영수증을 만든다(WF07 정상 CI 근거의 출처). | `ai-ops-normal-ci`, OIDC | `ai-ops-b6-lab` |
| `ai-ops-c2-rollback.yml` | C-02 격리 실습 독립 기한 원복. 기본은 준비만 한다(`prepare_only=true`). | `ai-ops-lab-ci`, OIDC | `ai-ops-c2-rollback` |
| `ai-ops-c4-ready-rollback.yml` | C-04 준비 완료 승인 후 독립 원복. | `ai-ops-lab-ci`, OIDC | `ai-ops-c2-rollback` |

## 7. 로컬에서 CI와 같게 확인하기

```sh
# 의존성 (Python 3.11 이상, 실행 이미지는 3.11; CI와 같은 hash 고정 설치)
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r ai-ops/requirements.lock

# contracts의 핵심: 전체 단위 시험. AWS 프로필을 숨겨 CI 러너와 같은 조건으로 돌린다.
env -u AWS_PROFILE AWS_CONFIG_FILE=/nonexistent AWS_SHARED_CREDENTIALS_FILE=/nonexistent \
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=ai-ops/src \
  .venv/bin/python -m unittest discover -s ai-ops/tests -p 'test_*.py'

python3 ai-ops/scripts/check_repository_boundary.py
git diff --check
```

**주의할 점**

- **helm 필요:** WF07 관련 시험은 `helm`이 PATH에 있어야 한다. CI는 v4.1.3을 쓴다.
- **AWS 프로필:** 로컬 AWS 프로필이 있으면 boto3 자격 경로의 차이가 가려진다. PR59의 첫 CI 실패가 이 경우였다. 위처럼 프로필을 숨기고 돌린다.
- **배포 정의 검사:** `check_deployment.py`를 돌리려면 `integration/platform-source.json`에 적힌 정확한 commit의 플랫폼 checkout을 `AI_OPS_PLATFORM_SOURCE_ROOT`로 지정해야 한다.
- **변형 검사:** 결함을 일부러 넣어 시험이 잡는지 보는 검사를 직접 돌릴 때는, 반드시 **기준선이 먼저 통과하는지** 확인한다. 최소 PATH 환경에서 helm이 없어 기준선이 실패하면 모든 변형이 "검출"로 나온다. 2026-11-24에 이 문제로 PR56 기록을 정정했다.
- **실제 자원 금지:** 시험은 AWS 인프라·클러스터·Slack·GitHub 쓰기를 쓰지 않는다. 그런 것이 필요한 확인은 6절의 승인된 수동 workflow나 별도 연결 단계에서 한다.
