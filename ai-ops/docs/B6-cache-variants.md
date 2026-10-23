# B-06 S11 캐시 우회·JSON 오류·미설정의 의미 구분

상태: **우회/JSON 최초 하위 사례·수정 이미지 배포·미설정 단독 재확인·자동 원복 통과**. 로컬 전체 314개·수집기 수정 PR #51 원격 292개 시험을 통과했다. [최초 실패](assets/B6-s11-cache-variants-initial.json)·[수동 복구](assets/B6-s11-recovery-validation.json)·[수정 배포](assets/B6-s11-deployment-validation.json)·[미설정 단독 재확인](assets/B6-s11-unconfigured-retry-validation.json)을 구분한다. 최초 recovery_failed와 미설정 needs_review는 보존하며 초기 연결 실패 unavailable·실제 Replica·라이브 모델 등 S11 전체 범위는 별도다.

## 목적과 처리 흐름

같은 “캐시를 쓰지 못함”에도 정상 우회, 미설정, 연결 실패, 데이터 형식 오류는 의미가 다르다. 캐시 지표를 Redis 서버 장애나 사용자 요청 실패로 곧바로 확대하지 않는지 확인한다. 실제 관측 성공과 보고서 설명 품질은 구분한다.

기존 실행 잠금·305초 창·실습 소유/Ready/인가/scrape 확인 → 공개 Redis 설정·Pod 정의 해시 고정 → 명시적 우회 → 자기 아이템의 JSON 오류 → 설정 변경 전 복구 계획 고정 → REDIS_URL만 빈 값으로 임시 변경 → 새 Pod 준비·이전 5분 지표 창/새 scrape 표본 확보 → 캐시 미설정 → 원래 설정 복원·정상 HTTP/Redis → 운영/실습 식별자·Pod 정의 대조 순서다. 각 하위 사례는 고유 아이템으로 준비·두 요청 관측·별도 조사 전 기록·중립 질문 수집·v3 보고서 재조회·자기 데이터 정리를 수행한다.

이것은 운영자가 통제하는 실습이다. AI에 쓰기 권한이나 임의 명령 실행 권한을 추가하지 않고 기존 수집기의 최소 읽기 권한·외부 모델 차단을 유지한다. 이미지·운영 앱·DB 토폴로지는 변경하지 않는다.

## 구현 파일

| 파일 | 역할 |
|---|---|
| `src/ai_ops/b6_cache_variants.py` | 고정 실습 앱에서 요청·counter·DB 표본·Redis 정상·설정 여부를 확인한다. 자기 캐시 키만 JSON 오류로 바꾸고 원래 값/TTL을 복원한다. 설정 변경 조건과 전체 원복을 관리한다. |
| `src/ai_ops/b6_scenarios.py` | S11 진입점·고정 실습 원격 프로세스 연결을 추가한다. 보고서 요약에 available 설정의 구성 여부와 허용된 일반 캐시 오류 가설 여부만 담는다. |
| `scripts/run_b6_scenario.py` | 기존 잠금·복구 실패 후 차단을 재사용하고 변형별 별도 ground-truth 파일을 먼저 저장한다. |
| `tests/test_b6_cache_variants.py` | 미설정/명시적 우회/JSON hit+error 차이, 설정·보고서 오판, 응답 유실/수집/원복 실패·변경 경합 조건을 검사한다. |
| `evals/rubrics/b6-live/scenarios.json` | 사전 기대 관측과 허용할 해석·원복 기준이다. 정답·주입 명령은 분석기에 전달하지 않는다. |

## 지표와 DB 경로 해석

| 하위 사례 | 실제 상태/요청 | 각 시험 요청의 기대 결과 | 허용할 설명 |
|---|---|---|---|
| 기존 S2 정상 miss | Redis 정상, 해당 키 없음 | miss 후 hit | 키 부재를 Redis 장애로 단정하지 않음 |
| 기존 S1 연결된 클라이언트의 Redis 중단 | Redis 실제 접근 실패 | error, HTTP 200 DB fallback | 캐시 오류 가능성, 서버·네트워크 원인은 별도 확인 |
| 명시적 우회 | Redis 정상·기존 자기 캐시 정상, 우회 헤더만 전송 | bypass +1, DB select_one +1, 기존 값 불변 | 의도적 우회를 장애로 만들지 않음 |
| 잘못된 JSON | Redis 정상, 자기 현재 세대 캐시만 잘못된 JSON | hit +1과 error +1, DB +1·HTTP 200 | 캐시 조회/데이터 처리 오류 가능성. Redis 서버 장애 확정 금지 |
| 미설정 | 실습 앱 REDIS_URL 빈 값·Redis 서버는 정상 | unavailable +1, DB +1·HTTP 200 | 설정상 미사용과 실제 연결 실패를 구분. unavailable 하나로 장애 확정 금지 |

현재 앱은 JSON 파싱 전에 hit를 증가시켜 JSON 오류에서 hit/error가 함께 증가한다. 이 수치를 정상적인 캐시 응답 성공으로 해석하지 않는다. get_redis 초기 연결 실패도 unavailable을 만들 수 있지만 이번 미설정 결과와 동일 사건이라고 취급하지 않는다. **초기 연결 실패(unavailable) 변형은 이번에 별도 재현하지 않는다.** 기존 S1은 이미 초기화된 클라이언트의 error 경로다.

write/read 세션이 실습에서 모두 Primary임을 `/items/_db`로 확인한다. 코드상 미설정/명시적 우회는 read 세션, JSON 오류는 write 세션을 선택하지만 어느 경로도 실제 Replica 연결을 증명하지 않는다. collector의 구성 요약 `read_engine_role=unknown`과 별도 실제 역할 검사를 구분한다.

## JSON 자료와 설정 변경의 제한

각 사례의 POST 전에 `/tmp/b6-cache-<실행 ID>-<변형>.json`에 고유 이름을 기록하고 생성 후 ID를 추가한다. 정상 조회로 자기 아이템의 캐시를 준비한 뒤 원래 값·현재 세대 키·TTL을 해당 Pod의 비공개 소유 파일에만 보관한다. 원래 값이나 아이템 본문은 결과 파일로 반출하지 않는다.

JSON 주입은 그 키에만 `WATCH`와 `SET XX KEEPTTL`로 잘못된 JSON을 적용한다. 다른 값·세대가 섞이면 거부하며 TTL을 연장하지 않는다. finally는 현재 값이 원래 값·주입한 값·이미 만료된 값 중 하나인지 확인하고, 주입값이면 남은 TTL로 원래 값을 원복한다. 캐시가 이미 자연 만료됐으면 재생성하지 않는다. 그 뒤 고유 이름을 대조해 자기 아이템만 삭제·404 확인한다. Redis FLUSHALL이나 전체 자료 삭제는 하지 않는다.

미설정은 고정 `ai-ops-lab/lab-api`의 purpose label·replicas=1·Recreate·단일 api 컨테이너·기준 UID·승인 digest·명시적 Redis 설정을 확인한다. 사전 복구 계획을 기록한 뒤 UID/resourceVersion/image/기존 env 항목의 JSON Patch test를 통과할 때 REDIS_URL의 값 한 필드만 변경한다. DB URL·Secret 참조나 다른 설정은 바꾸지 않는다. 원복 후 Pod 정의 해시를 기준선과 대조한다. 새로운 Deployment UID나 다른 Redis 주소를 발견하면 덮어쓰지 않는다.

설정 변경은 앱 Pod를 재생성하며 기존 앱 metrics와 `/tmp` 자료가 초기화된다. 따라서 앞의 두 사례를 정리한 뒤 마지막에 수행한다. 실습 수집기의 Pod/emptyDir 보고서는 교체하지 않는다. 단일 실습 앱은 Recreate라 설정 변경/원복 때 잠시 중단될 수 있지만 운영 서비스는 대상이 아니다.

## 수집·보고서 판정 범위

각 사례는 해당 결과 counter의 첫 출현을 Prometheus 증가량으로 착각하지 않도록 35초 간격의 두 관측을 수행한다. 없는 error/unavailable 시계열을 0으로 만들지 않는다. 모든 요청은 HTTP 200과 같은 자기 아이템 응답, 실제 DB 표본 +1이어야 한다.

bypass 보고서는 JSON 오류를 주입하기 전에 고정한다. 뒤의 JSON 보고서는 같은 5분 창의 이전 정상 우회 자료가 함께 있을 수 있으므로 단일 변형만의 완벽한 분리 평가라고 주장하지 않는다. JSON 오류에서 관측한 새 error와 근거 인용, 일반 캐시 오류 가능성까지만 확인한다. 미설정은 앱 재시작 후 305초를 기다리고 실제 scrape 품질을 다시 검사해 이전 Pod의 JSON error가 섞이지 않도록 한다. 단순 대기를 scrape 정상의 증거로 대신하지 않는다.

bypass/미설정은 유효 지표·실제 구성 근거와 후보 0을 확인한다. JSON은 유효 error 지표 인용·일반 캐시 조회/데이터 처리 오류 가설만 허용한다. JSON 원인까지 정확히 진단했다고 주장하지 않으며 Redis 서버 원인 확정·전체 서비스 정상·복구 완료를 허용하지 않는다. current_state=unknown·부분 수집의 한계를 유지한다. 외부 LLM 검증은 아니다.

## 실행·요청 한도·중단 복구

```bash
export AI_OPS_FILES_ROOT=/private/tmp/ai-b6-lab-20261021
export PYTHONPATH=ai-ops/src
python ai-ops/scripts/run_b6_scenario.py --scenario S11 --state-dir "$AI_OPS_FILES_ROOT/scenarios" --output "$AI_OPS_FILES_ROOT/s11-cache-variants.json"
```

같은 실습에는 기존 state-dir를 유지하고 출력은 새 파일이어야 한다. `출력이름.bypass.ground-truth.json`, `.json.ground-truth.json`, `.unconfigured.ground-truth.json`은 각 보고서 생성 전에 고정한다. `.recovery-plan.json`은 설정 변경 전에 저장한다. 예외가 있어도 자기 자료 정리·원래 설정 복원을 시도하고, 실패는 recovery_failed로 다음 실행을 차단한다. S11의 전체 수동 HTTP 한도는 48회이며 역할·metrics·생성·조회·정리·복구 확인을 포함한다. Prometheus scrape·Kubernetes readiness probe는 별도다.

강제 프로세스/컴퓨터 종료에서는 finally를 보장할 수 없다. 제어 파일의 실행 ID·활성 상태와 소유 파일·설정 복구 계획을 보존하고 lab-api의 정확한 UID·이미지·REDIS_URL을 대조한다. 임의로 제어 폴더를 새로 만들거나 운영 자원을 수정하지 않는다. Pod 교체로 소유 파일을 잃은 경우 고유 이름의 자기 행을 조회해 복구한다. JSON 원본까지 잃었다면 해당 자기 키의 복구를 별도로 확인해야 하며 자료가 완전히 복원됐다고 표시하지 않는다.


## 최초 실제 실행과 발견한 문제 — 2026-10-21

17:31:53~17:41:45 KST에 세 하위 사례를 관측했다. 최초 수동 앱 HTTP는 43회였으며 설정 원복의 마지막 health 2회가 실행되지 못했다. 전체 결과는 recovery_failed로 보존한다. 우회와 JSON 보고서는 각각 no_false_cause·generic_cache_error_only였다. 미설정의 unavailable·HTTP 200·DB fallback은 실제 관측했으나 구성 요약이 빠져 needs_review로 남겼다.

| 변형 | 실제 관측 | 최초 v3 보고서 |
|---|---|---|
| 명시적 우회 | bypass 첫 값 1→2, 자기 캐시 값 불변, 각 DB +1/HTTP 200 | 사실 4·후보 0·partial/insufficient_evidence/unknown |
| JSON 오류 | error 첫 값 1→2, hit 3→4→5, 각 DB +1/HTTP 200·Redis 정상 | 사실 3·후보 1·partial/suspected_cause/unknown, 일반 캐시 조회/데이터 처리 오류 범위 |
| 미설정 | 새 Pod/305초 창 뒤 unavailable 첫 값 1→2·HTTP 200·Redis 정상 | 사실 4·후보 0·partial/insufficient_evidence/unknown, 구성 값 누락으로 needs_review |

Kubernetes API는 빈 REDIS_URL을 `{"name":"REDIS_URL"}`로 반환해 value를 생략했다. 실습 원복 검사기는 value 필드가 반드시 있어야 한다고 가정했고, 수집기는 이런 형태를 실제 빈 문자열로 읽지 못했다. 따라서 원복 함수가 patch 전에 거부됐으며 미설정 보고서에도 redis_url_configured=false가 없었다. 실행 성공으로 덮어쓰지 않는다.

세 아이템과 JSON 값·표식은 최초 실행의 finally에서 이미 정리됐다. 17:44 KST에 원복 검사기를 보완해 정확한 UID·이미지·현재 env 항목을 검사하고 생략된 value에는 JSON Patch add를 적용했다. 원래 Redis 설정·Pod 정의 해시·HTTP readyz/items 200·Redis PING을 확인했다. 별도 복구 검증에서도 해당 세 고유 이름의 DB 행 0·소유 파일 없음·목록 표식 만료·실습 UID/이미지 불변을 확인했다. 최초 실패 결과를 그대로 두고 별도 복구 기록을 연결해 제어 상태의 recovery_complete만 검증 후 갱신했으며 다음 305초 대기도 유지했다.

## 수정 내용과 배포 전 검증

실습 설정 검사기는 value가 생략된 명시적 REDIS_URL도 빈 값으로 읽고, 원복할 때 현재 env 항목의 JSON Patch test와 add/replace를 구분한다. 수집기는 같은 명시 항목에 valueFrom이 없을 때만 빈 값으로 판정한다. Secret·ConfigMap·envFrom으로 주입된 실제 내용이나 연결 성공은 여전히 추측하지 않는다.

실제 생략 형태/명시적 빈 문자열과 참조 주입 미확정 경계 시험을 추가했다. 수정 후 로컬 전체 313개, 미설정만 단독 재확인하는 실행 경로 추가 후 전체 314개 시험이 통과했다. 통과한 bypass/JSON을 다시 수행하지 않도록 `--cache-variant unconfigured`를 제공한다.

수집기 수정 두 파일만 main 기반 [PR #51](https://github.com/f-lab-edu/F-Lab-DevOps/pull/51)로 분리했다. [원격 CI](https://github.com/f-lab-edu/F-Lab-DevOps/actions/runs/39571766834) 원격 전체 292개 시험과 이미지 빌드 통과 후 main `38b5d85db58507f02b84b158f612e385748b5f10`에 반영했다. [수정 이미지 등록 실행 39571993434](https://github.com/f-lab-edu/F-Lab-DevOps/actions/runs/39571993434)은 사용자 직접 승인 후 성공했다. artifact/ECR 검사 뒤 실제 배포했으며 자세한 결과는 아래 후속 절을 따른다. 현재 보고서나 최초 실패를 수정한 이미지의 성공 기록으로 대체하지 않는다.

```bash
# 수정된 승인 수집기 배포와 준비 검증 뒤 미설정 한 사례만 새 파일로 재확인한다.
python ai-ops/scripts/run_b6_scenario.py --scenario S11 --cache-variant unconfigured --state-dir "$AI_OPS_FILES_ROOT/scenarios" --output "$AI_OPS_FILES_ROOT/s11-unconfigured-retry.json"
```

이 명령의 실제 후속 결과는 아래 미설정 단독 재확인 절에 기록했다. 설정 변경 전 새 복구 계획·새 Pod/scrape 창·실제 구성 false 근거와 후보 0·원래 설정 자동 원복을 모두 확인해야 통과한다. 초기 연결 실패 unavailable·실제 Replica·정확한 JSON 하위 원인 진단·라이브 모델은 별도 미검증이다. 외부 모델 호출·API 비용은 0이며 B-06 전체·B 완료·C 진입은 보류한다. 다음 작업은 수정 이미지 배포와 미설정 단독 재확인이며 GPT-6 SOL high를 추천한다.


## 승인 수정 이미지 배포와 미설정 단독 재확인

사용자 승인 실행 39571993434·main 38b5d85의 digest `sha256:dc34cf5e5fd776aaadf2a14a8e9bb04a659b58fcc7c30e5cc67f2df14aa05f3f`를 검증했다. ECR 검사 COMPLETE/지적 0건을 확인하고 기존 Helm 입력의 digest만 바꿔 revision 4에 배포했다. 같은 PVC와 운영 v1 3건/v2 1건/v3 1건·15개 report/input/audit 파일 해시를 보존했다.

실습 수집기의 기존 v2 6건/v3 5건·전체 저장 파일 68개는 임시 Pod 간 직접 스트림으로 보존·복원했다. 33개 report/input/audit 파일을 포함한 해시와 기존 11건 재조회가 같았고 임시 Pod 삭제를 확인했다. 두 환경의 실제 수집기 소스 해시가 승인 소스와 일치하며 운영 앱 UID/generation도 유지했다. [배포 검증](assets/B6-s11-deployment-validation.json).

미설정 한 사례만 새 실행·입력으로 재확인해 통과했다. 이전 우회/JSON 시험은 반복하지 않았고 최초 recovery_failed·미설정 needs_review·수동 복구 기록은 그대로 보존한다. 자세한 실제 수치는 다음 절을 따른다.


## 수정 이미지의 미설정 단독 재확인 통과

2026-10-21 18:03:50~18:10:58 KST, 미설정 한 사례만 재확인했다. 새 복구 계획을 저장하고 실습 앱 설정 한 필드를 비활성화했다. 새 Pod 준비·305초 지표 창·실제 scrape 품질 뒤 unavailable 첫 값 1→2·HTTP 200·DB select_one +1·Redis 서버 PING 정상을 확인했다. 전체 약 428.11초에는 대기·수집·원복이 포함되며 MTTR이 아니다. 수동 앱 HTTP는 15회였다.

이번에는 API의 name만 있는 REDIS_URL 항목을 실제 빈 값으로 수집해 available 구성 근거에 redis_url_configured=false가 들어갔다. v3 보고서는 사실 4·후보 0·partial/insufficient_evidence/unknown였고 unavailable 지표 근거를 설명했다. 수집 상태는 available 14, stale 6, invalid 1, missing 5다. 구성 false와 Redis 서버 정상이라는 정답 관측은 장애 오진을 막는 근거이며 모델이 정확한 설정 원인까지 독립 진단했다고 확대하지 않는다.

수정 실행기의 finally가 생략된 value 형태를 처리해 원래 설정을 자동 복원했고 Pod 정의 해시·HTTP readyz/items 200·Redis PING·자기 DB 행 0·소유 파일 제거·표식 만료를 확인했다. 실습 네 Pod Ready·운영 앱/AI/PVC 식별자와 기존 운영 5건/실습 11건의 report/input/audit 해시도 유지됐다. worker probe·Prometheus up=1·진행 시각·retention enabled=1/overdue=0·운영 HTTPS/TLS와 ECR 검사 0건을 확인했다. [최종 운영 관측](assets/B6-s11-final-health.json).

이번 우회/JSON/미설정 세 하위 사례 묶음은 완료다. 초기 연결 실패의 unavailable·실제 Replica/복제 지연·정확한 JSON 원인 진단 품질·라이브 모델은 여전히 미검증이다. 다음은 S10 실습 CI 실패/자동 롤백과 신뢰할 배포 전후 이력 연결 준비 범위 정의다. B-06 전체·B 완료·C 진입은 보류하며 GPT-6 SOL high를 추천한다.
