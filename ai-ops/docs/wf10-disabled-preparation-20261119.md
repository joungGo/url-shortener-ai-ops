# WF-10 비활성 설정·전환/원복 로컬 준비 · 2026-11-19

상태: 로컬 패키지 준비·검증, 미커밋/원격CI 미실행. 실제 입력12개 미확정과 실행 미승인 상태는 유지한다. [연결 계획](wf10-connection-plan-20261119.md) · [입력 작업표](wf10-connection-inputs-20261119.json) · [구현 작업표](incident-workflow-implementation-tracker.md).

## 준비한 것

`ai_ops.wf10_preparation`은 외부 클라이언트·자격 로딩·apply/restore 기능이 없는 오프라인 도구다. 계획의 현재 미확정 inventory만 받으며 활성 플래그/자동예외/새 대상/새 필드/중복·누락 입력은 거절한다. 기존 코드나 설정을 수정해서 활성화하는 도구가 아니다.

| 파일 | 내용·검증 |
| --- | --- |
| [helm.disabled.json](../config/wf10-disabled-review/helm.disabled.json) | 기존 chart용 enabled=false, receiver/observability/retention=false. 기존 schema의 replicaCount=1은 유지하지만 Helm template 실제 출력은 리소스0개 |
| [slack-web.disabled.json](../config/wf10-disabled-review/slack-web.disabled.json) | enabled=false/event_sources={}와 파일 경로 참조만. 현재 SlackWebConfig 스키마로 검사하고 runtime 생성은 비활성 검사에서 자격 읽기 전에 거절 |
| [deployment.review.json](../config/wf10-disabled-review/deployment.review.json) | receiver/기존worker/ingress/relay의 프로세스·자격 분리·동일 물리 store·단일worker/교체 절차. namespace/PVC/digest/registry는 null이며 Kubernetes manifest가 아님 |
| [transition.review.json](../config/wf10-disabled-review/transition.review.json) | 생산자 중지·일관 백업·옛DB 읽기 보존·새 빈 큐·manual정책·schema/권한/버전 대조와 예외 migration 순서 |
| [rollback.review.json](../config/wf10-disabled-review/rollback.review.json) | 새 요청/모호한 회신 보존·호환 코드/DB·공지 경로 보존·조건 미충족 시 수동 인계. 자동 복원/재전송 명령 없음 |
| [inventory.json](../config/wf10-disabled-review/inventory.json), [CHECKSUMS.json](../config/wf10-disabled-review/CHECKSUMS.json), [README.md](../config/wf10-disabled-review/README.md) | 미확정 입력12개·과거 후보·소스 기준과 고정 파일별 hash. hash는 변경 검출이며 승인/서명/실물 준비 증명이 아님 |

패키지는8개 파일이다. 파일0600·폴더0700으로 배타 생성하며 기존 출력 경로를 덮어쓰지 않는다. 실패한 부분 출력도 자동 지우지 않으므로 재실행은 확인 후 새 경로로 한다. verifier는 정확한 파일 집합/내용을 재생성해 비교하므로 enable 변경 뒤 CHECKSUMS만 다시 써도 통과하지 않는다. 링크/큰 파일·예상외 파일을 거절한다. 일반 악성 파일시스템 경합을 격리하는 보안 샌드박스는 아니다.

현재 Helm chart는 기존 진단/worker 배포용이다. 이 비활성 렌더를 WF10 전체의 활성 배포 렌더라고 주장하지 않는다. 실제 source registry·마운트·서비스/route·네트워크/최소권한·이미지가 확정되면 별도의 검토된 배포 연결 구현/실물 원복 패키지가 필요하다. 빈 값을 추측하거나 이 패키지의 enabled만 바꾸면 되는 방식이 아니다.

## 저장소와 전환 검증

물리 저장소 기준은 하나의 공유 store이며 그 안의 **queue/jobs.sqlite3와 slack-web/inbox.sqlite3는 별도 DB**다. 로컬 fixture는 slack/inbox.sqlite3를 사용한다. 사건/정책·작업과 Slack 요청/전송 장부를 같은 DB라고 가정하지 않는다. 같은 `/data/incidents` 문자열이 다른 PVC를 가리키면 연결된 것이 아니다. namespace/PVC/프로세스 배치는 실제 입력 대기다. SQLite WAL에 적합하지 않은 네트워크 파일시스템이나 cross-namespace PVC 공유를 가정하지 않는다.

일관 보존 단위는 모든 writer 중지 후 두 DB 각각의 SQLite backup/quick_check + 연관 증거/설정/manifest다. DB 파일을 각각 backup한다고 여러 writer가 계속 쓰는 동안의 전체 원자성이 생기지 않는다. 실제 자료는 이번에 읽지 않았다.

새 합성 리허설에서 기존 사건 지정 요청/큐/worker fixture와 실제 SQLite를 사용했다.

- pending 작업이 있는 기존 큐와 Slack inbox를 readonly 연결로 backup하고 quick_check/파일 hash를 확인했다. 새 활성 큐의 jobs0·manual rules=[]·event_meta2를 확인했으며 과거 큐를 배출하지 않았다.
- 같은 store를 여는 두 EventStore가 동일 alert_ref/evt를 읽고, 다른 source registry는 거절하는지 확인했다.
- 복제본에서만 v1→v2 초기화를 시험해 기존 job을 보존하고 원본 backup 바이트가 그대로인지 확인했다. 이는 모든 옛버전/실물 DB migration 검증이 아니다.
- 전환 전/후 합성 자료를 따로 보존하고 신규 요청2건이 있는 Slack 장부를 확인했다. 신규 요청 또는 모호한 회신 대조가 끝나지 않으면 복원 후보 대신 수동 인계로 판정하며 DB를 덮어쓰지 않는다.

`transition_assessment`/`rollback_assessment`는 신뢰된 **로컬 리허설 사실**의 순수 판정 함수다. 항목별 미충족은 blocked/manual_handoff, 모두 충족해도 execution_allowed=false/live_verified=false다. 호출자가 True를 보냈다고 실물 백업이나 실행 승인으로 인정하는 API가 아니다. 실제 백업/restore·RBAC·Slack 인증 어댑터는 연결하지 않았다.

## 재현 명령 — 로컬만

잠금된 의존성의 Python과 설치된 Helm을 사용한다. 아래 명령은 외부 연결·자격/기존DB 읽기를 하지 않는다.

```sh
# AI 작업트리 루트. build 출력은 존재하지 않는 사용자 전용 임시 경로로 지정한다.
PYTHONPATH=ai-ops/src python -m ai_ops.wf10_preparation verify --directory ai-ops/config/wf10-disabled-review
PYTHONPATH=ai-ops/src python -m ai_ops.wf10_preparation build --inventory ai-ops/docs/wf10-connection-inputs-20261119.json --output /tmp/wf10-review-unique
helm template wf10-offline ai-ops/chart -f ai-ops/config/wf10-disabled-review/helm.disabled.json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=ai-ops/src:ai-ops/tests python -m unittest test_wf10_preparation -v
```

생성 단계에는 Helm 호출도 없다. Helm은 로컬 검증에서만 사용한다. 초기 시험의 실제 chart replicaCount=1 조건과 허용 store 경로, 별도 Slack DB 위치를 반영해 fixture를 바로잡았다. 최종 신규21개(0.197초)·전체1565개(209.958초/skip0)가 통과했다. 기존 추적 구현/설정/시험/워크플로 파일429개와 C단계75개·공유 Lease가 동일하다. 최종 수치·소스/산출물 hash는 [검증 기록](wf10-disabled-preparation-validation-20261119.json)에 기록한다. 원격 CI·이미지 빌드/등록·실환경 점검은 하지 않았다.

## 다음 단계와 유지 조건

다음은 이 준비 도구/자료의 코드 검토·CI/main 반영이다. 이어서 미확정 실제 경보 channel/bot/source registry·공유 물리 store·검토 image/연결 렌더·새 UID/TLS/정상CI를 확정한 패키지와 원복안을 별도 승인받아야 한다. 현재 패키지로 인프라 설치나 활성화는 할 수 없다.

기존3/15/120/210/240/300/120초·실행별 새승인·정확head 사람리뷰/CI·잠금/ACK·원복/회수 조건과 소스는 유지한다. WF10의 실제 공지/조사 검증, WF07~09 실제 복구 연결과 WF11/C04/C05 완료는 남아 있다. 실제 설치·배포·Slack 전송·장애·Terraform·유료 모델 실행 없음. 추천 **GPT-6 SOL · High**.
