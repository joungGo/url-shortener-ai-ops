# A7 정상(healthy) 판정 · 2026-11-25

상태: **로컬 구현·검증 완료, PR CI/main 대기.** 기준 main `e5fcea4548bd70d1c30700adefff8cbcc3123432`(PR66).

## 결정 (사용자 2026-11-25, 모두 추천안)

이번 결정은 2026-11-21의 "관측기는 healthy를 절대 보고하지 않는다"를 대체한다.

| 질문 | 결정 |
|---|---|
| 안정 조건 | Ready가 **60초 이상** 지속돼야 하고, 컨테이너는 실행 중, 롤아웃은 완료 상태여야 한다 |
| readyz·업무 요청 경로 | **API 서버 Pod proxy.** 기존 효과 관찰기와 같은 방식이다. 새 네트워크 경로는 없고, 고정 GET 경로만 허용한다 |
| 승인 대기를 열 때 이미 정상이면 | **전용 회신을 보내고 기록은 남기지 않는다** |

## 판정 규칙 (`recovery_observe`)

이미지 가져오기 실패 판정은 그대로이며 항상 우선한다. 실패가 아니고 아래 **다섯 가지를 모두** 한 번의 읽기 예산 안에서 통과하면 `healthy`다. 이 다섯 가지는 이미지 복구 계획의 `verification` 이름과 같다. 하나라도 통과하지 못하거나 확인할 수 없으면 지금처럼 `unknown`이다.

| 검사 | 조건 |
|---|---|
| `argo_synced_healthy` | Argo `status.sync.status=Synced`, `status.health.status=Healthy`, revision 일치(기존 조건) |
| `pod_image_matches_desired_digest` | Deployment 이미지가 등록 저장소의 `@sha256:` digest 형식이다. Pod와 ReplicaSet 이미지가 같고, 컨테이너 `imageID`가 같은 digest로 끝난다 |
| `pod_ready` | 단일 replica다. `observedGeneration`이 최신이고, replicas·ready·updated·available이 모두 1이며 unavailable이 없다. 소유된 살아 있는 Pod가 하나이고 `phase=Running`이다. 컨테이너 `api` 하나가 ready이면서 running이다. Ready 조건이 True로 바뀐 지 **60초 이상**이다(미래 시각이면 불인정) |
| `readyz_success` | Pod proxy `GET /readyz`가 200이고 본문이 `{"status":"ready"}`이다 |
| `items_flow_success` | Pod proxy `GET /items`가 200이고 앱 응답 계약(C-04 `valid_body`)을 만족한다 |

- HTTP는 컨테이너가 ready인 단일 Pod일 때만 읽는다. 이미지 실패 경로의 읽기 수는 4회 그대로다.
- proxy 오류는 앱 오류와 구분할 수 없으므로 `unknown`으로 둔다. 본문이 깨졌거나 NaN 같은 비정상 상수가 있으면 계약 불일치로 본다.
- HTTP 응답은 hash로만 `proof_hash`에 반영한다. Pod 이름·digest·본문은 저장하거나 반환하지 않는다.
- 허용 경로는 `pods/<이름>:8000/proxy/(readyz|items)` GET뿐이다. 다른 포트·경로·메서드는 client가 거부한다.

## 장부·Slack 동작

- **열 때:** 같은 Deployment UID와 같은 이미지가 이미 정상이면 `current_state_already_healthy`로 거절한다. 회신은 "이미 정상으로 확인되어 승인 대기를 열지 않았습니다. 변경은 없습니다…"이고, 장부 기록은 없다. 이미지가 달라진 정상 상태는 기존처럼 "새 계획 필요"다.
- **재검증:** 기존 WF06 규칙대로 정상이면 `already_recovered`가 된다. "재확인 결과 이미 정상으로 확인되어 변경하지 않습니다" 알림이 가고, PR 준비로 넘어가지 않는다. 일부만 정상이면(`unknown`) 지금처럼 `needs_review`다.
- 재시작 경로는 영향이 없다. 재시작 게이트가 Ready인 Pod를 이미 거절한다.

기존 시험 `test_changed_initial_target_requires_new_draft`의 기대값을 바꿨다. revision이 바뀐 상태에서 같은 이미지가 정상이면, 이제 "새 계획 필요" 대신 "이미 정상"으로 거절한다. 계획에는 revision이 기록되지 않으므로 원래도 revision을 비교한 것이 아니었다. 승인 대기가 열리지 않는 것은 같다. 이미지가 다른 정상 상태가 여전히 "새 계획 필요"인 경우도 시험에 추가했다.

## 연결 요구 (WF12 체크리스트)

- 관측기 읽기 신원에 `ai-ops-lab` **`pods/proxy` get**을 더해야 한다. 이 권한이 없으면 proxy 읽기가 실패해 `unknown`이 된다. 동작은 안전하지만 정상 판정은 나오지 않는다.

## 검증

신규 `test_recovery_healthy.py` 11개를 추가했다.

- **판정:**
  - 다섯 가지 통과 시 healthy이고, 원값은 남지 않는다.
  - 안정 구간은 60초에 인정하고, 59초와 미래 시각은 불인정한다.
  - 단일 실패 25종은 모두 `unknown`이다.
  - 이미지 실패가 우선한다.
- **관측기:**
  - 고정 GET 6회가 나간다.
  - 실패 Pod에는 HTTP를 읽지 않는다.
  - HTTP 오류와 잘못된 본문 4종은 `unknown`이고 proof가 바뀐다.
  - proxy 경로·포트·메서드 고정을 확인했다.
- **흐름:**
  - 열 때 이미 정상이면 전용 회신을 보내고 기록하지 않는다.
  - 재검증에서 정상이면 `already_recovered`이고, 알림이 가며, 그 뒤로는 아무것도 하지 않는다.
  - 일부만 정상이면 `needs_review`다.

변형 검사와 전체 시험 결과는 [검증 기록](recovery-healthy-judgment-local-validation-20261125.json)에 있다.

실제 클러스터·배포·Slack·장애 실습은 하지 않았다.
