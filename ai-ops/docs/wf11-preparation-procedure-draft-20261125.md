# WF11 준비 절차 (초안) · 2026-11-25

상태: **초안이며 실행 승인이 아니다.** 이 문서는 [WF11 합격 기준표](wf11-acceptance-sheet-draft-20261125.md) 시나리오 S1·S2를 실제로 돌리는 순서와 명령을 적은 것이다. 아래 각 항목은 사용자의 건별 승인이 있어야 실행한다.

- 장애 주입
- Argo sync
- Slack 사용
- 인프라 작업

기존 C04/C05 실습 기한·승인·잠금 규칙은 바꾸지 않는다. **C04 실습 코드(`c_trial_*`, `run_c4_*`)와 B6 실습 도구(`run_b6_scenario.py`)는 쓰지 않는다.** 이유는 다음과 같다.

- C04 코드는 짧은 실습 기한과 잠금이 묶인 동결 코드다. 범용 실행기로 쓰지 않는다는 기존 원칙이 있다.
- B6 도구는 Deployment를 `kubectl patch`로 직접 바꾼다. 하지만 WF11 S1은 GitOps 경로여야 한다. 관측기는 Argo `targetRevision` = sync revision일 때만 판정하고, WF07은 격리 저장소의 `values.yaml`에 묶여 있다.

## 부하 생성에 대한 판단

이미지를 가져오지 못하는 장애(S1)에서는 lab-api Pod가 없다. Recreate 전략이라 기존 Pod가 먼저 내려가기 때문이다. 그래서 요청 부하가 경보 발화에 영향을 주지 못한다.

S1의 경보는 상태 기반 규칙에서 나와야 한다. 이를 위해 2026-11-25에 다음을 정하고 구현했다(사용자 결정, llm-council 2차 결론).

- renderer가 recovery 절이 있을 때 **`LabDeploymentUnavailable`** 규칙을 만든다([lab 경보 규칙](wf11-lab-alert-local-20261125.md)).
- 조건은 원하는 replica가 1 이상인데 사용 가능한 replica가 0인 상태가 2분 지속되는 것이다.
- 라벨은 `ai_ops=enabled`와 lab 대상 5개다. `release=kube-prometheus-stack`으로 Prometheus가 규칙을 선택한다.
- 사건은 `/v1/events` 경로로 등록된다. 이 경로는 경보 이름을 허용 목록으로 거르지 않는다. 따라서 재설치 때 Alertmanager 라우팅을 WF03 후보(ai-events, `/v1/events`)로 바꿔야 한다. 예전 `/v1/alerts` 경로는 이 이름을 거부한다.

정상 요청이 필요한 곳은 S0 기준선과 S1 9번(회복 확인) 두 곳이다. 둘 다 사람이 `/readyz`·`/items`를 몇 번 호출하는 정도면 충분하다. 2026-11-18 정상 요청 검증과 같은 범위다. 그래서 새 부하 생성 스크립트는 만들지 않았다.

## 고정 값

| 항목 | 값 |
|---|---|
| 격리 저장소 | `joungGo/c03-execution-lab` |
| 값 파일 | `ai-ops/lab/values.yaml`의 `api.image.digest` |
| 이미지 저장소 | `764428911194.dkr.ecr.ap-northeast-2.amazonaws.com/urlshortener` |
| Argo 앱 | `argocd/lab-target`(자동 sync 꺼짐) |
| 대상 | `ai-ops-lab/lab-api`(replica 1, Recreate) |

`values.yaml`은 **고정 JSON 형식**이어야 한다(`indent=2`, 끝 줄바꿈 1개).

```json
{
  "api": {
    "image": {
      "repository": "764428911194.dkr.ecr.ap-northeast-2.amazonaws.com/urlshortener",
      "digest": "sha256:<64자리>"
    }
  }
}
```

주입 PR도 이 형식을 정확히 지켜야 한다. 그래야 WF07 복구 diff가 정확히 1줄로 나온다. 형식이 다르면 WF07은 준비하지 않는다(fail-closed).

## 1. 사전 확인 (읽기만)

1. **기준선 digest(B)**: 현재 main의 `values.yaml` digest를 확인한다. ECR에 **있는지** 확인한다.
   ```
   aws ecr describe-images --region ap-northeast-2 --repository-name urlshortener \
     --image-ids imageDigest=<B>
   ```
2. **없는 digest(M)**: 무작위 `sha256:<64자리>`를 하나 만든다. ECR에 **없음**을 확인한다.
   ```
   aws ecr batch-get-image --region ap-northeast-2 --repository-name urlshortener \
     --image-ids imageDigest=<M>
   ```
   결과에서 `images=[]`이고 `failures[0].failureCode=ImageNotFound`여야 한다. 다른 오류(권한 등)면 "없음"으로 보지 않는다. WF07의 ECR 확인과 같은 원칙이다.
3. 아래 항목을 기록한다.
   - Argo `lab-target`: 자동 sync 꺼짐, `targetRevision` = sync revision, Healthy
   - lab-api: Ready, 기준선 이미지
4. 장부를 조회한다(빈 상태 확인).
   ```
   kubectl -n <ai-ops ns> exec <main pod> -c slack-relay -- \
     python -m ai_ops.recovery_status --store /data/incidents
   ```

### 1-1. 경보 경로 사전 확인 (장애 주입 전, 읽기만)

1. Prometheus `/api/v1/rules`에 `LabDeploymentUnavailable`이 로드돼 있는지 확인한다. 없으면 S1을 시작하지 않고 원인을 기록한다(release 라벨, CRD, 적용 순서).
2. `kube_deployment_spec_replicas{namespace="ai-ops-lab",deployment="lab-api"}`가 1을 반환하는지 확인한다. kube-state-metrics가 이 namespace를 수집하는지 보는 것이다.
3. Alertmanager 설정에 `ai-events`(`/v1/events`) 수신기가 있는지 확인한다.

### 1-2. 경보 미발화 시 비상 절차 (B, 사전 승인된 대체 경로)

장애 주입(sync) 뒤 **약 8분 안에** 경보가 공지·등록되지 않으면 아래 절차로 바꾼다. 8분은 scrape, `for 2m`, 그룹 대기, 여유 시간을 더한 값이다.

- Alertmanager API로 **같은 이름(`LabDeploymentUnavailable`)과 같은 라벨 계약**의 합성 경보를 1건 넣는다. `endsAt`을 충분히 뒤로 명시해 조사 도중 해제되지 않게 한다.
- 기존 이름(`HighErrorRate` 등)은 쓰지 않는다. 조사 근거가 오염되기 때문이다.
- 결과는 **"합성 경보 · 탐지 미검증 · S1 부분 성공"**으로 기록한다. 실제 탐지에 성공한 것으로 보고하지 않는다.
- 회복 확인은 합성 경보의 해제가 아니라 실제 Pod·응답 상태로 한다.

## 2. S1 주입 (사람, 승인 필요)

1. 격리 저장소에 브랜치를 만들고, `values.yaml`의 digest만 B에서 M으로 바꾼 커밋 1개로 PR을 연다. 형식은 위 고정 JSON 그대로 둔다.
2. 정확한 head 리뷰와 CI를 거쳐 병합한다. 환경 승인은 UI에서 한다.
3. 사람이 Argo `lab-target`을 병합된 revision으로 한 번 sync한다. 자동 sync는 켜지 않는다.
4. 아래를 기록한다.
   - Pod가 `ImagePullBackOff`/`ErrImagePull`이고 `readyReplicas=0`인지
   - 경보 발화 시각(`LabDeploymentUnavailable`, pending에서 firing으로 바뀌는 데 약 2분)
   - Slack 공지 시각

## 3. S1 진행 (Slack, 기준표 3~7번)

1. 사건 스레드에서 `@봇 investigate-event this`를 보내고 완료를 기다린다.
2. `@봇 plan-recovery <job-id>`를 보낸다. 회신이 "지원 조치와 근거 일치"인지, 계획 hash를 확인한다.
3. `@봇 request-approval this <계획 hash>`를 보내고, 버튼 메시지가 오면 승인한다.
4. relay가 재검증하면 `proposal_ready`가 된다. 이어 WF07 `prepared` 알림이 오는지 확인한다.
5. 장부를 다시 조회한다. 시각과 상태를 기록한다.
   ```
   … python -m ai_ops.recovery_status --store /data/incidents --plan <계획 hash>
   ```

## 4. S1 복구 (사람, 기준표 8~9번)

1. WF07이 준비한 diff(M → B, 1줄)를 그대로 써서 사람이 격리 저장소에 PR을 연다.
2. 정확한 head 리뷰와 CI를 거쳐 병합한 뒤, 사람이 Argo를 한 번 sync한다.
3. 회복을 확인하고 기록한다.
   - Pod Ready가 60초 이상 유지되는지
   - `/readyz` 결과가 `{"status":"ready"}`인지
   - `/items` 응답 계약이 맞는지
   - 경보가 해제됐는지

WF09 자동 효과 판정은 연결돼 있지 않다. 따라서 이 결과는 "미검증(사람 확인)"으로 기록한다.

## 5. S2a (이미 회복됨 → 변경 없음)

S1과 같은 주입을 한 번 더 하거나, 순서를 바꿔 진행한다. 둘 중 무엇으로 할지는 기준표 9절 6번에서 사용자가 정한다.

1. `request-approval`과 승인까지만 진행한다.
2. relay가 재검증하기 **전에** 사람이 4번 방법으로 먼저 복구한다.
3. Ready가 60초 넘게 유지되기를 기다린다. relay는 짧은 주기로 재검증하므로, 승인 직후 복구 PR을 병합하고 sync까지 마치기 어렵다. 그래서 승인 버튼을 누르는 시점을 복구 완료 뒤로 미룬다. 단, 승인 대기 마감 안이어야 한다.
4. 승인 버튼을 누른다.
5. **기대 결과:** `already_recovered`가 되고 "이미 정상으로 확인되어 변경하지 않습니다" 알림이 온다. WF07 기록은 생기지 않는다(조회로 확인).

## 6. 정리

1. 최종 상태를 확인한다: `values.yaml`이 B이고, Argo가 Healthy이고, lab-api가 Ready다.
2. 장부 조회 결과와 증거를 개인 경로에 저장한다. 저장소에는 커밋하지 않는다.
3. 기준표 8절 자격 회수와 철거로 넘어간다.

## 중단 조건

기준표 6절을 따른다. 다음 중 하나라도 해당하면 진행을 멈추고, 증거를 보존한 뒤 사람이 B로 복구하는 PR을 병합·sync한다.

- 판정이 기대와 다르다.
- 같은 단계가 2회 실패한다.
- 의도하지 않은 쓰기가 있었다.
- 자격 노출 가능성이 있다.
- 시간 상한을 넘었다.

같은 단계를 자동으로 다시 시도하지 않는다.

## 반영 기록

- [PR #71](https://github.com/joungGo/url-shortener-ai-ops/pull/71)의 head `95e159cc5fb0238062e91fa90b14b17a7b8b5581`를 PR70 다음에 squash 병합해 main `aaa85287c4d99805bb83da175fbe195abca9e5eb`에 반영했다. 사용자 "진행해" 후 병합했다.
- README와 이 문서만 바꿨다.
- PR CI45579685052 7종이 성공했다.
- PR이 PR70 이전 main에서 갈라졌으므로 tree 대신 변경 내용을 비교했다. `c2cc32e..aaa8528`의 diff가 PR 커밋 diff와 정확히 같다.
- 병합 후 branch를 정리했다. [main CI45580525860](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/45580525860)이 성공했다.
