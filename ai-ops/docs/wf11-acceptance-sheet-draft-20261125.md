# WF11 합격 기준표 (초안) · 2026-11-25

상태: **초안이며 실행 승인이 아니다.** 아래 작업은 모두 사용자의 건별 승인이 있어야 한다.

- 인프라 재설치·Terraform
- Slack 연결
- 장애 주입
- 로컬 클러스터 리허설

기존 C04/C05 실습 기한·실행별 승인·정확 head 리뷰/CI·잠금·원복·자격 회수 규칙은 바꾸지 않는다. 이 문서는 시간 연장이나 새 실습 승인이 아니다.

근거:

- [WF12 인계 문서](wf12-recovery-handoff-20261124.md) 5절 연결 체크리스트
- 2026-11-25 llm-council 결론: 순서를 "기준표 → 로컬 리허설 → WF11(수동 PR·수동 원복) → 원복 승인 흐름"으로 하고, PR 자동 발행은 선택 과제로 둔다.

## 1. 완료 정의

다음 세 가지가 실환경에서 증거와 함께 확인되면 WF11을 완료로 본다.

1. **정방향:** 실제 장애 경보 1건이 Slack 조사 → 복구 계획 → 승인 → 재검증 → 정확한 diff 준비까지 간다. 사람이 그 diff로 PR을 만들어 리뷰·CI·병합하고 sync하면 서비스가 정상으로 돌아온다.
2. **음성 경로:** 바꾸면 안 되는 상황에서 시스템이 변경을 제안하지 않는다(아래 S2).
3. **정리:** 쓰기 자격 회수와 철거가 끝나고, 그 증거가 남는다.

다음은 완료 조건이 아니며, 하지 않았으면 "미검증"으로 기록한다.

- 원복
- 재시작 경로
- 자동 PR 발행

## 2. 범위

| 포함 | 제외(이번 WF11에서 하지 않음) |
|---|---|
| 공지·`investigate-event`·`plan-recovery`·`request-approval`·승인 버튼·재검증·WF07 diff 준비 | 봇의 GitHub PR 발행(선택 과제, 아래 7절) |
| 정상 판정(`already_recovered`, 열 때 "이미 정상" 회신) | 재시작 실제 실행(Deployment patch) |
| 사람이 하는 PR 작성·리뷰·CI·병합·sync, 사람이 하는 원복 | 원복 실행기·원복 승인 흐름(`RollbackScope`) |
| 재시작 경로의 `command_ready` 알림(선택 S4, 실행 없음) | C04/C05 완료 주장 |

## 3. 사전 조건

WF12 5절 체크리스트를 모두 끝낸 뒤 아래를 추가로 확인한다.

- **이미지:** main `9928fc8` 이후 이미지를 쓴다(정상 판정·재시작 연결·renderer `restart` 절 포함). CI image 작업이 성공한 commit이어야 한다.
- **장애 주입 재료:**
  - registry에 실제로 있는 정상 digest(기준선)
  - registry에 **없는** digest(이미지를 가져오지 못하는 장애용). digest 형식은 정상이어야 한다.
  - 주입은 사람이 격리 저장소(`joungGo/c03-execution-lab`)에 PR을 만들고 병합하는 방식으로만 한다.
- **신호원:** 새 클러스터에는 트래픽이 없다. 경보 규칙이 평가 창 안에 실제로 발화하도록 부하 생성 방법을 정하고, 규칙 이름과 평가 시간을 확인한다. 실제 규칙 이름은 재설치 시 확인하며 추측하지 않는다.
- **재연결 점검:** 철거로 끊긴 것을 다시 확인한다.
  - ECR 이미지
  - Slack 앱의 `/slack/events`·`/slack/interactivity` URL
  - GitHub 읽기 토큰
  - CI relay와 정상 CI binding(15분 이내 수신)
  - IRSA 역할
  - Argo `lab-target` 자동 동기화 꺼짐
- **읽기 RBAC(PR65·67·68 반영):** reader 신원에 다음 권한을 준다. 쓰기 권한은 주지 않는다.
  - `ai-ops-lab`: `deployments` get, `replicasets`·`pods` list, `leases` get, `pods/proxy` get
  - (S4를 할 때만) `events`·`horizontalpodautoscalers` list
  - `argocd`: `applications` get
- **`check-config` 플래그:** `recovery_approval`, `recovery_observe`, `recovery_prepare_configured`(S4를 하면 `recovery_restart_configured`도)가 모두 `true`여야 한다.

## 4. 시나리오와 단계별 기대 판정

### S0. 연결 점검 (장애 없음)

| 단계 | 기대 | 증거 |
|---|---|---|
| ingress·relay 기동 | 진행 신호가 정상이고 재시작 반복이 없다 | Pod 상태, 진행 시각 |
| `check-config` | 3절의 플래그가 모두 `true` | 출력 JSON(경로·비밀 없음) |
| 서비스 확인 | `/readyz`가 `{"status":"ready"}`, `/items` 응답 계약이 맞다 | 사람이 확인한 결과 |

### S1. 이미지를 가져오지 못하는 장애 → 사람 PR로 복구 (필수)

| # | 단계 | 기대 판정 | 증거 |
|---|---|---|---|
| 1 | 사람이 없는 digest를 병합하고 sync | Recreate로 기존 Pod가 내려가고 새 Pod는 `ImagePullBackOff` | Argo 상태, Pod 상태 |
| 2 | 경보 발화 → Slack 공지 | 사건이 등록되고 공지가 간다 | Slack 메시지 시각, 사건 ID |
| 3 | `investigate-event` | 조사가 완료되고 이미지 실패 근거가 결합된다 | 작업 ID, 근거 묶음 hash |
| 4 | `plan-recovery <job>` | `image-rollback`이 "지원 조치와 근거 일치"로 나오고 계획 hash가 표시된다 | 계획 회신 |
| 5 | `request-approval this <hash>` | 승인 대기가 열리고 버튼 메시지가 간다 | 회신, 버튼 메시지 |
| 6 | 승인 버튼 | `approved` 알림이 간다(취소 버튼만 있음) | 알림 |
| 7 | relay 재검증 | `proposal_ready` 후 WF07 `prepared`. 바꾸는 줄은 `values.yaml` digest 1줄(없는 digest → 정상 digest)뿐이다 | 준비 알림, diff |
| 8 | 사람이 그 diff로 PR 작성·리뷰·CI·병합 | 정확 head 리뷰와 CI를 통과한다(환경 승인은 UI 클릭) | PR 번호, head, CI |
| 9 | sync 후 회복 | Pod가 Ready로 60초 이상 지속되고, `/readyz`·`/items`가 정상이며, 경보가 해제된다 | Argo·Pod·응답·경보 기록 |

- 이 9단계 중 하나라도 기대와 다르면 6절 중단 조건을 따른다.
- **WF09 효과 검증 프로세스는 아직 연결되지 않았다.** 9번은 사람이 확인하고, 자동 효과 판정은 "미검증"으로 기록한다.

### S2. 음성 경로: 이미 회복된 상태에서는 변경하지 않음 (필수, 하나 이상)

- **S2a:** S1의 5~6번까지 진행한 뒤, 재검증 전에 사람이 먼저 정상 digest로 복구하고 Ready가 60초 이상 지속되기를 기다린다.
  - **기대:** 재검증이 `already_recovered`가 된다. "재확인 결과 이미 정상으로 확인되어 변경하지 않습니다" 알림이 가고, WF07 준비는 없다.
  - 이 방식은 서로 다른 장애 두 번이 필요할 수 있으니, S1과 S2a 중 무엇을 먼저 할지 사전에 정한다.
- **S2b:** 다른 사람의 스레드, 또는 다른 사건에서 `request-approval`을 보낸다.
  - **기대:** 고정 거절 문구만 오고 장부 기록이 없다. grant가 1명이면 다른 스레드로만 시험한다.

### S3. 사람이 하는 원복 기록 (선택)

S1의 복구가 효과를 내지 못했을 때만 한다. 사람이 Argo를 사전 승인된 기준선 커밋에 다시 고정하고 1회 sync한다. 2026-11-20 결정 방식이며, revert PR은 원복으로 보지 않는다. 원복 코드(실행기·승인 흐름)는 쓰지 않고, 수행 기록만 남긴다.

### S4. 재시작 후보의 `command_ready` 알림 (선택, 실행 없음)

readiness 실패를 일으킨 뒤 다음 순서로 진행한다: `plan-recovery <job> deployment-restart` → `request-approval` → 게이트 판정 → 승인 → `command_ready` 알림("실제 재시작 실행은 … 비활성").

- **기대:** 쓰기가 0회다(Deployment·Lease 변경 없음).
- readiness 실패를 일으키는 방법은 별도로 승인받는다.

## 5. 반드시 남길 증거와 재생 자료

- 단계별 시각(UTC). 다음 소요 시간을 기록한다.
  - 경보부터 공지까지
  - 요청부터 승인 대기 열림까지
  - 승인부터 `proposal_ready`까지
  - 병합부터 회복까지
- Slack 메시지는 내용 요약과 ts만 남긴다(토큰·서명 없음).
- 장부 상태(승인·WF07·재시작·outbox): relay 컨테이너에서 `python -m ai_ops.recovery_status --store /data/incidents [--plan <hash>]`로 조회한다. 읽기 전용이고 hash·상태·시각·건수만 낸다([조회 명령](recovery-status-local-20261125.md)).
- PR 번호·head·CI 실행 ID, Argo revision.
- **재생 자료:** 철거 뒤에도 회귀 시험에 쓸 수 있도록 다음을 비밀을 제외하고 저장한다.
  - 경보 payload
  - S1 시점의 Deployment/ReplicaSet/Pod/Argo 객체(`kubectl get -o json`, managedFields·토큰 제외)
  - `/readyz`·`/items` 응답 모양
  - 저장 위치는 개인 자료 경로이며 저장소에 커밋하지 않는다. 비식별 fixture로 만들지는 따로 결정한다.

## 6. 상한과 중단 조건 (사용자 결정 필요)

- **시간 상한:** 인프라 창 1회당 상한을 사용자가 정한다. 제안은 재설치 완료부터 철거 시작까지 4시간이다.
- **비용 상한:** 사용자가 정한다.
- **중단 조건:** 아래 중 하나라도 해당하면 진행을 멈추고, 증거를 보존한 뒤 정리한다. 같은 단계를 자동으로 다시 시도하지 않는다.
  - 기대와 다른 판정이 나옴
  - 같은 단계가 2회 실패함
  - 의도하지 않은 쓰기를 발견함
  - 자격·비밀이 노출됐을 가능성이 있음
  - 시간 상한을 초과함
- C04 실습 기한(Lease 3초, 시작 15초 등)과 상관없는 일반 진행이지만, 실습 코드를 쓴다면 그 규칙을 그대로 따른다.

## 7. PR 자동 발행 (선택 과제, WF11 필수 아님)

하려면 WF11과 분리해 인프라 없이 진행한다. 기한은 2~3일이고, 넘기면 버린다.

- **범위:** 격리 저장소에 실제 PR을 1회 열고 닫는 것까지만.
- **필요한 것:**
  - 별도 쓰기 자격(저장소 1개, Contents/Pull requests write)
  - 저장소 ruleset(브랜치 접두사 `ai-ops/recovery-*`, main 직접 push 금지)
- **명시할 사항:** 소유자 관리자는 main 보호를 우회할 수 있다.
- **버튼:** 두 번째 "PR 생성" 버튼은 넣지 않는다. 1인 운영에서는 통제 효과가 없다(council 결론).
- 발행 직전 재확인은 WF07에 이미 있다.

## 8. 정리와 자격 회수

- WF11용으로 만든 자격을 폐기하고, 폐기 시각을 기록한다.
  - GitHub 읽기 토큰(7절을 했다면 쓰기 자격 포함)
  - reader 신원 토큰 Secret
  - Prometheus 읽기 토큰(S4를 한 경우)
- IRSA 역할을 분리하거나 정책을 제거한다.
- Slack 앱 요청 URL을 비활성화한다.
- 철거는 사용자 터미널에서 한다(Terraform). 철거 뒤 잔여 리소스 점검 결과를 남긴다.

## 9. 사용자 결정이 필요한 항목

1. 인프라 재설치 여부, 시기, Terraform 실행 주체
2. 로컬 kind/k3d 리허설 여부(사용자 Mac에서 실행)
3. 시간·비용 상한(6절)
4. 장애 주입 승인: 없는 digest 병합(S1), 필요 시 readiness 실패(S4)
5. 부하 생성 방식과 경보 규칙 확인
6. S1과 S2a의 순서(장애 2회 여부)
7. 재생 자료의 보관 위치와 비식별화 여부
8. 7절 PR 자동 발행을 할지
9. ~~장부 상태 읽기 전용 조회 절차~~ → `ai_ops.recovery_status`로 해결(2026-11-25)

## 반영 기록

[PR #69](https://github.com/joungGo/url-shortener-ai-ops/pull/69)의 head `cc2e138e700f299b3c36bfb0ce765dbcb1bdd0c5`를 사용자 "진행해" 후 head 고정 squash로 병합해 main `60faf1f4fb9a618c49ab2d6e9e323569ada21a47`에 반영했다. 문서만 바꾼 PR이다.

- PR CI: [45576610850](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/45576610850) 7종 성공
- tree: `b827d8df3082b55c8a8c1a4c140725e31a09a723` 일치
- 병합 후 branch를 정리했다.
- main CI: [45577440373](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/45577440373) 성공
