# B-06 S9 최근 쓰기 표식·목록 캐시 자연 만료 검증

상태: **구현·로컬 전체 299개 시험·실제 목록 표식/캐시 자연 만료·보고서 대조·자기 데이터 정리 통과**. 실제 Replica 전환이 아닌 단일 Primary 환경의 목록 경로 하위 사례다. [최종 실측](assets/B6-s9-list-consistency.json), [조사 전 관측](assets/B6-s9-list-consistency.ground-truth.json), [최종 검증](assets/B6-primary-read-validation.json)을 구분한다. B-06 전체·B 완료·C 진입은 보류한다.

## 구현 목적과 처리 흐름

아이템을 생성하면 앱은 목록용 `items:primary-read` 표식을 5초 동안 설정한다. 표식이 있는 동안 목록 조회는 캐시 내용을 사용하지 않고 write 세션을 선택하며 `consistency_primary`를 증가시킨다. 이 정상 일관성 처리를 Redis 장애나 Replica 장애로 오해하지 않는지 검증한다.

현재 앱 POST는 목록 표식만 설정하므로 단건 `/items/{item_id}`를 시험 대상으로 쓰지 않는다. 목록 `/items`와 `select_all`을 사용한다. Primary 우선 조회도 목록 캐시를 채우므로 표식 만료 직후는 miss가 아니라 hit일 수 있다. 이후 목록 캐시 자체의 60초 TTL이 자연 만료돼야 read 세션의 miss를 관측할 수 있다.

단일 실행 잠금·이전 305초 창 → 실습 소유/Pod Ready/읽기 인가/scrape 품질 확인 → 실제 DB 역할 검사 → 고유 아이템 생성 → 표식 유효 목록 조회 → 표식 자연 만료 뒤 hit → 목록 캐시 자연 만료 뒤 miss → 후속 hit → 두 번째 고유 아이템으로 같은 관측 → 조사 전 관측 기록 고정 → 마지막 scrape 대기 → 실제 수집·규칙 보고서·재조회 → finally에서 자기 데이터 삭제/표식 만료/운영·실습 식별자 확인 순서다.

두 묶음은 Prometheus가 처음 생긴 counter의 첫 값을 이미 증가량으로 관측했다고 오인하지 않도록 준비한 단일 실행이다. 첫 묶음의 자연 만료 대기 동안 기준 표본이 수집되고 다음 묶음의 실제 증가와 구분한다. 정확도 통계를 위한 반복 표본으로 쓰지 않는다.

## 파일과 동작

| 파일 | 역할 |
|---|---|
| `src/ai_ops/b6_consistency.py` | 실습 앱 안에서 목록 요청·Redis PTTL·counter·DB histogram을 관측하고, 실행 순서/판정/finally 정리를 구현한다. |
| `src/ai_ops/b6_scenarios.py` | 기존 고정 실습 클라이언트와 S9 진입점, `/items`·`select_all` 수집을 연결한다. |
| `scripts/run_b6_scenario.py` | 기존 잠금·복구 미완료 차단·이전 지표 창·새 파일 전용 기록에 S9를 추가한다. |
| `tests/test_b6_consistency.py` | 만료되지 않은 표식·가짜 hit·hit 중 DB 증가·추가 오류 counter·실제 Replica 역할·허위 원인·수집/기록 실패·응답 유실·정리 실패를 검사한다. |
| `evals/rubrics/b6-live/scenarios.json` | 사전 기대 관측과 허용할 해석·자기 자료 정리 기준이다. 분석기에 정답으로 넣지 않는다. |

운영 이미지나 앱 코드를 수정하지 않는다. 운영자 프로그램을 실습 Pod의 별도 Python 프로세스로 실행하며 수집기는 승인된 v3 이미지와 기존 읽기 권한을 유지한다. 외부 모델·쓰기 실행 권한을 추가하지 않는다.

## 단계별 관측과 판정

| 단계 | 표식/캐시 | 기대 counter | DB `select_all` 표본 |
|---|---|---|---|
| 쓰기 직후 목록 | 표식 PTTL 전후 모두 양수·최대 5000ms | `consistency_primary`만 +1 | +1 |
| 표식 자연 만료 후 | 표식 -2(없음), 캐시 PTTL 양수 | `hit`만 +1 | 증가 없음 |
| 목록 캐시 자연 만료 후 | 표식 -2, 캐시 조회 전 -2/후 양수 | `miss`만 +1 | +1 |
| 후속 목록 | 표식 없음, 캐시 PTTL 양수 | `hit`만 +1 | 증가 없음 |

각 응답은 200이어야 하며 해당 고유 아이템이 목록에 포함돼야 한다. 각 단계에서 error·unavailable·명시적 bypass 등 다른 결과가 증가하면 통과시키지 않는다. Redis 키를 삭제하거나 TTL을 줄여 만료를 연출하지 않는다. 캐시 세대가 중간에 바뀌면 다른 쓰기가 섞인 것으로 거부한다. 누락 counter는 0으로 채우지 않고 첫 출현 값 1을 구분한다.

역할 검사 `/items/_db`에서 write/read 두 세션 모두 `in_recovery=false`이고 연결 설정이 같음을 비출력 비교한다. 따라서 표식 만료 뒤 read 세션을 선택하는 코드 경로를 확인해도 실제 Replica에 연결했다고 주장하지 않는다. 서버 주소·사용자·DB URL·아이템 본문은 로컬 결과로 반출하지 않는다.

보고서에는 실제 수집한 `consistency_primary` 증가와 캐시 자료의 available 근거 인용이 있어야 하고 허위 원인 후보는 없어야 한다. 자료가 불완전한 현재 환경에서는 `current_state=unknown`을 유지한다. 규칙이 표식 의미를 완전히 설명하는지와 실제 운영자 관측의 경로 확인은 구분한다. 전체 정상 판정·라이브 모델 평가·실제 복제 지연 검증으로 확대하지 않는다.

## 실행과 정리

```bash
export AI_OPS_FILES_ROOT=/private/tmp/ai-b6-lab-20261021
export PYTHONPATH=ai-ops/src
python ai-ops/scripts/run_b6_scenario.py --scenario S9 --state-dir "$AI_OPS_FILES_ROOT/scenarios" --output "$AI_OPS_FILES_ROOT/s9-list-consistency.json"
```

기존 제어 폴더를 그대로 사용하고 출력은 새 파일이어야 한다. 총 수동 앱 HTTP는 역할 확인·metrics·생성/조회·삭제/확인·readyz를 포함해 최대 28회다. 기존 S1/S2/S4의 20회 제한을 바꾸지 않고 S9의 두 TTL 묶음에만 28회 상한을 둔다. Prometheus 정기 scrape는 별도다. 일부 실패로 묶음 응답 자체를 잃으면 출력된 요청 합계는 완료 응답의 합계일 수 있으며 성공 측정으로 사용하지 않는다.

아이템 이름은 실행 ID·묶음 번호로 고정하고 POST 전에 실습 Pod의 `/tmp/b6-consistency-<run_id>.json`에 소유 기록을 저장한다. 생성 응답을 잃어도 그 이름의 행만 찾아 확인·삭제한다. 삭제 전 자기 이름을 대조하고 삭제 후 HTTP 404를 확인한다. 목록·단건 표식은 자연 만료를 기다린 뒤 Ready·Redis PING·소유 기록 제거를 확인한다. 다른 자료를 일괄 삭제하거나 Redis를 비우지 않는다. 이전 세대 목록 캐시는 앱의 TTL로 만료되며 모든 Redis 키를 즉시 지웠다고 주장하지 않는다.

프로세스 강제 종료·컴퓨터 종료에서는 finally를 보장할 수 없다. 제어 파일의 active·실행 ID와 위 소유 기록을 유지하고 자기 데이터/표식·Ready를 확인한 뒤 복구 상태를 처리한다. 새 state-dir로 바꿔 차단을 우회하지 않는다. 앱 Pod도 재생성돼 기록을 잃었다면 고유 이름·실행 자료를 대조한 별도 복구가 필요하다.


## 실제 결과 — 2026-10-21

17:14:52~17:17:59 KST 실행에서 고유 아이템 두 개를 직렬로 생성했다. 전체 약 186.47초에는 두 번의 자연 캐시 만료·마지막 scrape 대기·수집·정리가 포함되며 MTTR이 아니다. 수동 앱 HTTP는 28회이며 Prometheus scrape는 이 합계에 포함하지 않는다. 코드/시나리오 경계 시험 10개를 추가했고 이 checkout의 전체 Python 299개가 통과했다. 최신 main 통합 CI를 이번에 다시 실행했다고 주장하지 않는다.

| 관측 | 첫 묶음 | 둘째 묶음 |
|---|---|---|
| 최근 쓰기 표식 PTTL, 조회 전→후 | 4941→4930ms | 4992→4986ms |
| `consistency_primary` | 첫 값 1 출현 | 1→2 |
| 표식 만료 직후 목록 hit | 캐시 잔여 약 55초, DB 1→1 | 캐시 잔여 약 55초, DB 3→3 |
| 목록 캐시 자연 만료 뒤 miss | cache PTTL -2, DB 1→2 | cache PTTL -2, DB 3→4 |
| 후속 hit | DB 2→2 | DB 4→4 |

각 목록 응답은 200이고 해당 고유 아이템이 포함됐다. 각 단계의 error·unavailable·bypass 결과 증가 없이 기대 counter만 증가했다. 원래 없던 시계열을 인위적인 0으로 생성하지 않았다. 실제 `/items/_db`의 write/read는 모두 `in_recovery=false`이고 같은 연결 설정이었다. Primary 우선 및 read 세션 선택의 코드 경로와 counter/DB 표본은 일치하지만 실제 Replica·복제 지연을 관측한 것은 아니다.

규칙 v3 보고서는 available 7·missing 5·stale 12, 사실 4·원인 후보 0·partial/insufficient_evidence/unknown이었다. 캐시 지표의 `consistency_primary` 추정 증가량 약 1.342와 `ev-metric-cache_outcomes` 근거 인용을 확인했다. 앱 counter에서 직접 확인한 2회와 Prometheus increase의 추정치는 구분한다. 로그는 truncated, Argo/CI 자료는 미확보이고 HTTP 오류 분자도 없는 시계열이었다. 따라서 표식 의미를 로그까지 연결해 전체 정상이라고 진단한 것은 아니며, 이번 통과 조건은 **관측된 일관성 처리를 허위 장애 원인으로 만들지 않고 유효 캐시 근거를 설명한 하위 조건**이다.

조사 후 고유 이름/소유 기록으로 자기 아이템 두 개만 삭제하고 HTTP 404·표식 자연 만료·Ready·Redis PING을 확인했다. 추가 읽기 검증에서도 해당 이름의 DB 행은 0개이고 소유 파일은 없었다. 운영 앱/AI/PVC 식별자와 실습 Deployment UID·generation·이미지를 유지했고 실습 Pod 네 개가 Ready다. 새 보고서의 저장·재조회·유효 근거 인용과 captured 외부 모델 차단을 확인했다. 외부 모델 호출·API 비용은 0이고 이미지 재배포·권한 변경은 없다.

다음은 S11의 캐시 미설정·연결 오류·JSON 오류·명시적 우회 의미를 분리하는 미검증 변형과 필요한 실습 설정/원복 범위 정의다. S9의 실제 Replica 검증은 별도 토폴로지가 필요하므로 미완료로 유지한다. GPT-6 SOL high를 추천한다.
