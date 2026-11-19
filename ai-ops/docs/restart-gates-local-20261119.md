# A7-3b 재시작 사전 게이트 재설계 로컬 구현 · 2026-11-19

상태: **PR47로 main 반영 완료.** 기준 main `8fa3ad2b38b88cbe31496ad81c1ede3644cb7bdb`. 실제 쓰기 차단(`local_fixture` 전용)은 그대로다.

**이 명령이 있는 이유:** kubelet은 liveness 실패에만 컨테이너를 재시작하고, readiness 실패만으로는 재시작하지 않는다. Running이지만 계속 NotReady인 Pod는 사람 승인으로만 재시작할 수 있는 후보다.

## 배경과 결정

WF08의 실행 전 진단 4종(`image_available`·`dependencies_healthy`·`configuration_verified`·`capacity_verified`)은 실제 데이터에서 도출하는 정의가 없었고, 시험 fixture가 통과값을 넣고 있었다. 사용자 요청으로 llm-council(자문 5·익명 상호검토 5·의장)을 거쳐 **재설계**로 결정했다. 사실 확인으로 대상 `lab-api`의 배포 전략이 **`Recreate`**임을 확인했다(`ai-ops/lab/templates/deployment.yaml`). 재시작하면 기존 Pod가 먼저 종료되므로 실패 증거가 반드시 사라진다.

- Readiness 실패 중인 Pod는 이미 이미지를 받아 실행 중이라 `image_available`은 늘 참이다.
- `configuration_verified`는 실제로는 "계획 이후 바뀌지 않음"만 확인했다(이름과 동작 불일치).
- 4종은 "다른 원인이 없음"을 증명하려는 구조라 닫히지 않는다. 막아야 할 것은 해로운 쓰기다. Ready 0에서 재시작의 실제 피해는 증거 소실과 무의미한 재시작이다.

## 구현

- 신규 `ai_ops.restart_gates`(순수 함수, 결정적)
  - `evaluate(plan, snapshot, now)` → 게이트별 `pass|fail|unknown`. `unknown`은 통과가 아니다. 근거에는 값이 아닌 필드 이름만 남긴다. 규칙 버전은 `restart-gates-v1`이다.

  | 게이트 | 판정 | 차단 |
  | --- | --- | --- |
  | `spec_unchanged_since_plan` | UID·resourceVersion·revision·이미지·Pod UID·generation·observed generation·**배포 전략**·어노테이션이 계획과 같음 | 예 |
  | `not_ready_single_replica` | replica 1, Ready 0 | 예 |
  | `no_competing_controller` | rollout·HPA·GitOps 자동 동기화·기존 restart marker 없음 | 예 |
  | `persistent_readiness_failure` | readiness 실패 지속 시간과 Pod 나이가 모두 2 × (initialDelay + failureThreshold × period) 이상. 시각이 모순이면 `unknown` | 예 |
  | `liveness_stable` | 컨테이너 실행 중이고 restartCount가 계획 시점과 같음(kubelet이 이미 재시작 중이면 봇이 할 일이 아님) | 예 |
  | `prior_ready_evidence` | 같은 spec Pod의 과거 Ready 이력 observed=통과, absent=실패, unknown=거절 | 예 |
  | `dependencies` | 명확한 실패면 거절("재시작으로 해결 안 됨"), `unknown`이면 표시만 | 실패일 때만 |

  - 삭제: `image_available`(imageID hash는 표시만), `capacity_verified`(스케줄 실패·quota 신호는 표시만, 새 Pod Pending은 사후 검증에서 인계), `configuration_verified`(위 지문 게이트로 흡수)
- `RestartSnapshot`: 판정 결과(bool)를 받지 않고 원시 관측값만 받는다(strategy, container_running, restart_count, pod_started_at, readiness_failing_since, readiness_window_seconds, prior_ready, dependencies, 표시용 image_id_hash·scheduling_signals).
- `RestartPlan`: strategy와 restart_count 기준값을 봉인한다. 계획 hash에 포함된다.
- **증거 보존 필수 게이트:** 두 번째 조회와 Lease 확인 뒤, `executing` 기록과 PATCH 전에 `port.capture_evidence`로 describe·events·logs_current·logs_previous·metrics·status 묶음을 저장한다. 묶음 hash·항목·시각을 실행 기록에 남긴다. 항목 누락, 형식 오류, 추가 필드, 시각 범위 밖, 수집 실패가 있으면 쓰지 않고 `needs_review`로 둔다.
- 게이트 거절은 `needs_review`, `reason=gate_rejected`이며 게이트별 이름·판정·차단 여부·규칙 버전만 기록한다(수동 인계). 사건당 1회 소비 규칙은 그대로라 재시도하지 않는다.
- 유지: 승인 후 재검증·15초 신선도·두 번 조회·test op·Lease 확인·사건당 1회·사후 효과 검증·"재시작은 되돌릴 수 없으므로 실패하면 인계" 원칙.

## 인프라 단계로 남긴 것

- 실제 조회 어댑터가 위 원시 관측값을 도출: Pod 조건·restartCount·readiness probe spec, 같은 spec의 과거 Ready 이력(Prometheus), 의존성 지표, 이벤트. collector의 events·pods/log·Prometheus 읽기 범위 확인과 필요 시 추가.
- 증거 수집 어댑터(describe·로그 current/previous·이벤트·지표 저장소)
- 실습 클러스터에서 읽기 전용 shadow 판정
- 서버 쪽 쓰기 범위 강제: RBAC resourceNames=lab-api, ValidatingAdmissionPolicy로 restart annotation 외 변경 거부
- 배포 전략을 surge로 바꿀지 검토(바꿔도 증거 게이트는 유지)
- 이후 WF11에서 live 모드 판단

## 검증

- 신규·변경 시험(`test_recovery_commands.py`)
  - 거절 사례 16종: 새 게이트(전략 변경, 실패 미지속, 짧은 지속, 짧은 Pod 나이, 컨테이너 정지, restartCount 변화, 과거 Ready 없음/판정 불가, 의존성 실패, 시각 모순) 포함
  - 의존성 `unknown`은 표시만 하고 실행됨
  - 증거 게이트 5종(수집 실패, 항목 누락, 잘못된 hash, 시각 범위 밖, 추가 필드)은 쓰기 0회
  - 증거는 PATCH 전에 수집되고 기록됨
  - 승인 뒤 상태가 변하면 `gate_rejected`로 인계 기록하고 재시도하지 않음
  - 지속 시간 경계(정확히 2배 통과, 1초 부족 실패)
- 의도적 결함 확인: 증거 게이트를 제거한 변형과 `unknown`을 통과로 바꾼 변형을 새 시험이 모두 실패로 잡았다.
- 기존 복구 명령·효과 시험 73개 통과(효과 검증 fixture 재사용). 전체 결과는 [검증 기록](restart-gates-local-validation-20261119.json)에 있다.

실제 클러스터·배포·Slack·장애 실습 없음. 기존 시간·실행별 승인·정확 head 리뷰/CI·잠금·원복 조건 변경 없음.

## 최종 반영 결과

[PR #47](https://github.com/joungGo/url-shortener-ai-ops/pull/47) head `8c86507b45c5221c15a0a5e0501cf27ee2ef508f`를 head 고정 일반 squash 병합해 main `6a6605d11b86a7b438286f8664d3341ae39e678a`에 반영했다. [PR CI44522308075](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44522308075) 7종(1639개, skip5 기준선 동일)·[main CI44523032690](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44523032690) 성공(PR전용 gate skip은 설계대로). tree `713fd3e069565d693054f1cb049d4fda0d74ae17` 일치. 보호 변경/우회 없음. 병합 후 branch를 정리했고 최종 메타데이터는 로컬 갱신했다.
