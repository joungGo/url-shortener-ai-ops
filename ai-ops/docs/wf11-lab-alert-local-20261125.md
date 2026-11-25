# WF11 lab 경보 규칙 `LabDeploymentUnavailable` · 2026-11-25

상태: **로컬 구현·검증 완료(promtool 실행은 아래 참고), PR CI/main 대기.** 기준 main `aaa85287c4d99805bb83da175fbe195abca9e5eb`(PR71).

## 배경과 결정

WF11 준비 중에 S1을 막는 공백을 찾았다. S1은 존재하지 않는 이미지 digest로 장애를 만든 뒤 경보에서 복구 계획까지 이어지는 시나리오다.

- AI 쪽 경보 규칙 5개는 운영 앱의 요청 지표에만 걸려 있다. lab에는 경보 규칙이 없다.
- Pod가 0개인 장애에서는 요청 지표가 나오지 않는다. 그래서 경보도 나오지 않는다.
- 복구 계획은 경보 사건에 묶여 있다. 경보가 없으면 S1을 진행할 수 없다.

사용자는 "진행해"로 llm-council 2차 결론을 받아들였다. 결론은 다음과 같다.

- 상태 기반 lab 경보를 기본 경로로 쓴다.
- 합성 경보는 비상 절차로만 쓴다.
- 규칙은 lab chart 밖에 둔다.
- 경보 라벨 계약을 먼저 고정한다.

### 조사하면서 바로잡은 것

1. **규칙 위치.** 처음에는 격리 저장소의 lab chart에 넣자고 제안했다. 하지만 lab AppProject가 `apps/Deployment`만 허용한다(`b6_gitops.py`). 그리고 원복 실행기는 "lab 트리가 values.yaml 외에는 동일"할 것을 요구한다. 그래서 규칙을 **WF10 renderer**(AI namespace)에 두기로 했다.
2. **수신기 허용 목록은 바꾸지 않았다.** council은 허용 목록에 새 이름을 추가하라고 권고했다. 그런데 코드를 확인해 보니 그럴 필요가 없었다.
   - WF10 수신기는 `/v1/events` 경로(EventInbox)로 받는다. 이 경로는 경보 이름을 허용 목록으로 거르지 않고, Watchdog만 제외한다. 대상은 라벨로 판정한다.
   - 5개 허용 목록은 예전 `/v1/alerts` 경로에만 적용된다.
   - WF10 연결 계획에는 이미 Alertmanager를 `/v1/events`(ai-events)로 전환하는 단계가 있다.
3. **조사·복구 계획·재검증은 경보 이름에 묶여 있지 않다.** 모두 수집 증거와 현재 상태로 판정한다. 또 PR 경로의 계획 확인은 경보가 firing인지 요구하지 않는다. 그래서 경보가 해제된 뒤에도 S2a를 진행할 수 있다.

## 라벨 계약 (고정)

| 항목 | 값 |
|---|---|
| 리소스 | `monitoring.coreos.com/v1` `PrometheusRule` `<이름>-lab-alerts`(AI namespace). recovery 절이 있을 때만 만든다 |
| 리소스 라벨 | `release: kube-prometheus-stack`(Operator가 규칙을 고르는 라벨. 운영 앱 규칙과 같다), `app.kubernetes.io/part-of: wf10` |
| 경보 이름 | `LabDeploymentUnavailable` |
| 식 | `(max by (namespace, deployment) (kube_deployment_spec_replicas{ns,deploy}) > 0) and on (namespace, deployment) (max by (namespace, deployment) (kube_deployment_status_replicas_available{ns,deploy}) == 0)` |
| 지속 | `for: 2m`, 그룹 평가 주기 30초 |
| 경보 라벨 | `severity=warning`, `ai_ops=enabled`, binding 대상 5개(`cluster_id`·`namespace`·`workload`·`service`·`environment`) |
| annotation | 고정 문구 하나. 템플릿(`{{`)은 쓰지 않는다 |

식과 라벨에 대한 판단은 다음과 같다.

- `max by`로 묶어 kube-state-metrics의 scrape 라벨(`service`·`pod`·`job` 등)을 없앤다. 대상 라벨은 규칙에서만 붙는다.
- 원하는 replica가 1 이상인 조건을 함께 건다. 의도적으로 0으로 줄인 경우를 장애로 보지 않기 위해서다.
- 대기 사유(ImagePullBackOff/ErrImagePull)는 식에 넣지 않는다. 두 사유가 번갈아 나오면 `for` 타이머가 다시 시작될 수 있다. 원인 판정은 조사 단계가 증거로 한다.
- 대상 이름(`namespace`·`workload`)은 PromQL에 넣기 전에 DNS 이름 형식을 검사한다.

## 구현

- `wf10_deployment.lab_alert_rule(binding)`
- `check_lab_rule(resource)`: 고정 형식을 엄격하게 검사한다. 모르는 필드가 있거나 필드가 빠졌거나, 템플릿·잘못된 기간이 있으면 거부한다.
  - vendored Kubernetes 스키마에는 이 CRD가 없다. 그래서 형식은 렌더 단계에서 이 함수로 검사하고, 실제 클러스터에서는 server dry-run이 계속 관문 역할을 한다.
- 변경 계획(`wf10_change_plan`)
  - 규칙을 **첫 단계**로 적용한다. CRD가 없으면 워크로드를 만들기 전에 실패한다.
  - 롤백 단계는 12개가 된다.
  - recovery 사후 점검에 `lab_alert_rule_loaded_in_prometheus`를 추가했다.
- `ai-ops/scripts/check_lab_alert_promtool.py`: 렌더된 규칙과 `promtool test rules` 시나리오 5종을 파일로 만든다. `--promtool`을 주면 실행한다.
  - 시나리오: 실패 뒤 2분 pending → firing(계약 라벨), kube-state-metrics 2중 수집 시 1건, 의도적 0으로 축소 시 미발화, 2분 안에 회복 시 미발화, 다른 namespace·deployment 미발화.

## 검증

신규 `test_wf11_lab_alert.py` 9개다. 실제 renderer와 실제 EventStore를 썼다.

- 라벨 계약은 binding에서 온다. release 라벨, 이름, 기간은 상수가 아니라 리터럴로 고정해 검사했다.
- recovery 절이 없으면 규칙도 없다.
- 형식 변형 9종을 거부한다.
- 형식이 잘못된 규칙이 나오면 renderer가 쓰기 전에 거부한다.
- 대상 이름 주입을 거부한다.
- **사건 계약**
  - 규칙 라벨에 시계열 라벨을 더한 경보가 firing이면 lab 대상 사건 1건이 된다. 수동 정책이라 조사가 자동으로 시작되지 않는다.
  - resolved 경보는 같은 사건이 해제로 바뀐다.
  - 대상 라벨이 틀리면 대상 없음으로 등록한다.
  - 예전 `/v1/alerts`는 이 경보를 거부한다. 그래서 라우팅 전환이 필요하다.
- promtool 문서가 렌더된 규칙과 같은지 확인했다.
- 기존 recovery 렌더 시험 2개를 갱신했다(리소스 목록, 롤백 12단계, 적용 순서 첫 단계, 사후 점검).

결함 변형은 13/13을 검출했다. 첫 실행에서는 3종이 살아남았다.

- release 라벨과 `for` 변형은 시험이 모듈 상수를 쓰고 있었다. 리터럴로 바꿨다.
- 렌더 단계 형식 검사를 생략하는 변형은 잡지 못했다. renderer 경로 시험을 추가했다.

**promtool 실행:** 공식 Prometheus 3.5.0 바이너리를 내려받아 실행하려 했으나 자동 권한 분류기가 막았다. 우회하지 않았다. 시나리오 파일은 생성했다. 실행은 사용자 승인이나 사용자 직접 실행을 기다린다([검증 기록](wf11-lab-alert-local-validation-20261125.json)).

실제 클러스터·Slack 없음.

## WF11 운영 연결

- [준비 절차](wf11-preparation-procedure-draft-20261125.md) 1-1절: 장애 주입 전에 규칙 로드, kube-state-metrics 수집, ai-events 라우팅을 확인한다.
- 같은 문서 1-2절: 주입 후 약 8분 안에 경보가 없으면 합성 경보 비상 절차로 바꾸고 "탐지 미검증"으로 기록한다.
- 규칙은 WF10 리소스와 함께 적용되고, 철거 때 함께 회수된다.
