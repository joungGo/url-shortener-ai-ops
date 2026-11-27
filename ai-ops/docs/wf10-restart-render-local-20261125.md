# WF10 renderer: 재시작 경로 `restart` 절 · 2026-11-25

상태: **로컬 구현·검증 완료, PR CI/main 대기.** 기준 main `ba2bda030609d81c4260f57bf3a58f5bde44c9a0`(PR67).

## 배경과 결정

원복 프로세스 연결을 먼저 조사했다. 그 결과 원복을 시작시킬 상위 흐름이 없었다.

- PR 발행과 재시작 실행이 모두 비활성이라 WF09 효과 기록이 생기지 않는다.
- 원복 승인 흐름도 없다(`RollbackScope` 발급자가 없다).

이 내용을 보고했고, 사용자는 추천안에 따라 원복 연결을 미루고 이 조각을 먼저 하기로 했다(2026-11-25).

PR65에서 재시작 경로를 실행 직전까지 연결했지만, renderer는 아직 그 설정을 만들지 못했다. 이번 조각에서 renderer가 그 설정을 만들게 하고, PR65·PR67에서 새로 필요해진 읽기 권한을 외부 선행 조건 문구에 반영한다.

## 입력 (`recovery.restart`, 선택)

```json
"restart": {
  "approval": {"wait_seconds": 3600, "approval_seconds": 600, "observation_seconds": 30, "revalidation_seconds": 3},
  "execution_seconds": 10,
  "prometheus_secret": "<relay 전용 Prometheus 읽기 Secret 이름>",
  "prior_ready_query": "…$TEMPLATE_HASH…",
  "dependency_queries": ["…"]
}
```

- 필드는 정확히 이 다섯 개다. `approval`도 기존 네 필드만 받는다. 승인자는 PR 준비 승인자와 같고, Slack grant 한 명이다.
- Secret 이름 규칙과 자격 분리 검사에 Prometheus Secret을 포함했다. 기존 6개 Secret, reader, github과 모두 달라야 한다.
- relay tick 예산(`execution_seconds + revalidation_seconds ≤ 15`), 질의 개수와 형식은 런타임 모델(`RecoveryApprovalConfig`)이 검증한다. 하나라도 틀리면 파일을 쓰지 않는다.

## 출력

- `recovery-approval.json`의 `restart` 절
  - 승인 범위는 `execute_command`, 버전은 1이다.
  - executor는 `local-restart-wf10`이다.
  - Prometheus 주소는 `runtime.json`의 수집기 Prometheus 연결과 **같은 주소**다. 그래서 메인 Pod 송신 규칙은 바뀌지 않는다(시험으로 NetworkPolicy 3개 동일 확인).
  - 자격 파일 경로는 `/run/recovery-prometheus/{token,ca.crt}`다.
- 그 Secret은 **relay 컨테이너에만** 마운트한다.
- `result.recovery.restart = "execution_disabled"`
- 외부 선행 조건
  - **모든 recovery 후보**: reader 신원에 `pods/proxy` get을 더한다(정상 판정, PR67).
  - **restart가 있을 때**: reader 신원에 events list와 `horizontalpodautoscalers` list(autoscaling/v2)를 더한다. Prometheus 읽기 토큰도 필요하다. 쓰기 권한은 여전히 없다.
- `restart`가 없으면 recovery 출력은 이전과 같다. 외부 선행 조건 문구만 바뀐다(`pods/proxy` 추가). recovery 절 자체가 없으면 결과가 이전과 바이트 단위로 같다(기존 시험).

## 검증

신규 `test_wf10_restart_render.py` 6개:

- `restart` 없음·`null`일 때 동일
- 렌더링된 절이 런타임 모델을 통과하고 값이 정확함
- relay만 Secret 마운트, NetworkPolicy 불변
- 렌더링된 설정을 `load_recovery`로 읽고 승인자가 grant에 있음
- 잘못된 입력 12종을 쓰기 없이 거절
- 패키지 왕복과 변경 계획 순서

결함 변형 11/11 검출. 전체 시험·경계 검사 결과는 [검증 기록](wf10-restart-render-local-validation-20261125.json)에 있다.

실제 클러스터·배포·Slack 없음.

## 최종 반영 결과

[PR #68](https://github.com/joungGo/url-shortener-ai-ops/pull/68)의 head `2a900669dbbd529e8b03d5d69d9623280fd071e2`를 head 고정 일반 squash로 병합했다(사용자 "진행해" 후). main은 `9928fc8fea69528993456982bcbc21de0b19b299`다.

- PR CI [45573120978](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/45573120978) 7종 성공(1858개, skip1)
- tree `54f49ff66c3cf7b763dc2e7dc2a559a118151a35` 일치
- 보호 변경·우회 없음
- 병합 후 branch 정리
- main CI [45574073257](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/45574073257) 성공
