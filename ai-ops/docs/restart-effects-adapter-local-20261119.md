# A7-4 재시작 효과 관찰·봉인·Lease 회수 확인 로컬 구현 · 2026-11-19

상태: **로컬 구현·검증 완료, 이 커밋 시점 PR CI/main 대기.** 기준 main `13d94e9a3efc8432480291ef9bbc1461e6899c60`. 실제 읽기와 회수는 계속 막혀 있다. 관찰기·봉인 리더·회수 포트는 모두 `local_fixture`가 아닌 mode를 쓰고, WF09 조정기는 `local_fixture`만 받는다.

## 결정 경위 (llm-council)

처음 제안한 혼합 리더(Prometheus 구간 조회 + Kubernetes 1회 조회)는 **철회**했다. 이유는 네 가지다.

- Kubernetes를 한 번 조회한 결과를 구간 전체의 표본처럼 쓰면 증거 위조가 된다.
- `query_range`의 step 값은 실제 관측값이 아니라 lookback으로 보간된 값이다. 그래서 45초 간격 검사를 거짓으로 통과시킬 수 있다.
- HTTP 응답 형식(계약) 유효성과 경보 평가 상태는 과거로 되돌려 재구성할 수 없다.
- 15초는 읽기 호출 1회의 제한이지 판정 전체의 제한이 아니다. 처음에는 이를 잘못 읽었다.

위원회 결론은 "실행 1회에 한정된 관찰 수집 + 봉인 묶음 판정"이다. 여기에 두 가지를 조정했다.

1. 관찰 구간 시작 시각을 **적용 시각 + 기동 허용 시간**으로 고정했다. rollout 완료를 기다리는 방식은 Pod가 끝내 Ready가 되지 않으면 구간이 열리지 않는다.
2. 봉인 묶음은 A7-3a와 같은 증거 저장 방식(0700/0600 파일 저장, 장부에는 hash만 기록)으로 남긴다.

## 구현

### `ai_ops.restart_effects` (신규)

**`RestartEffectObserver`** (mode `kubernetes`)

- **실행 전 검증.** 아래 조건을 수집 전에 확인하고, 하나라도 어긋나면 거절한다.
  - 영수증이 command 경로이고, 적용됐고, 취소되지 않았는지
  - run_id 형식
  - 설정한 지표·경보 정의 hash가 정책과 일치하는지
  - **간격 + 읽기 예산 ≤ 최대 간격**: 최악의 경우에도 표본 간격이 판정 기준 안에 든다.
  - **구간 길이 ≥ 최소 관찰 시간 + 간격 + 읽기 2회**: 실제로 수집된 표본의 폭이 최소 관찰 시간에 도달한다.
  - 표본 수 상한 100 이하
  - 보고 지연을 더해도 보관 기한을 넘지 않는지
  - Deployment UID가 영수증과 일치하는지
  - **구간 시작 ≥ 적용 시각 + `probe_window`(startup/readiness 허용 시간)**
  - 구간이 아직 시작되지 않았는지
- **잠금을 잡지 않는다.** WF08 실행기는 쓰기를 확인한 직후, 검증 전에 Lease를 반납한다.
- **tick마다 읽는 것.** 간격은 5~20초이고, tick마다 deadline이 적용된다.
  - **runtime**: Deployment와 Pod를 GET한다. 종료 중인 Pod도 UID 목록에 넣으므로, 옛 Pod가 남아 있으면 복구로 보지 않는다. 이미지가 spec과 다르게 실행 중이면 실행 중인 digest로 판정한다. restart marker, revision hash(restart annotation 제외), generation도 읽는다.
  - **HTTP**: `/readyz`와 `/items`를 **API 서버 Pod proxy의 고정 GET 경로**로 읽는다. 새 네트워크 경로는 만들지 않는다. 200이면 C-04의 `valid_body`로 응답 형식을 검사한다. 200이 아니면 앱 오류인지 proxy 오류인지 구분할 수 없으므로 unknown으로 둔다(통과 아님).
  - **지표**: 설정한 PromQL만 C-04의 `metric_value`(timestamp 신선도, 적용 이후 자료)로 읽는다. 보간된 오래된 값은 거절한다.
  - **경보**: 설정한 규칙을 `/api/v1/rules`로 읽는다. 모호하거나 일부만 온 응답은 거절한다. health는 ok/error/no_data로 매핑한다. 범위 라벨이 맞는 pending/firing 경보 수를 세고, 실제 규칙 정의 hash를 기록한다. 구간 중 정의가 바뀌면 수집 실패로 처리한다.
- **실패 처리.** 읽기가 실패한 series만 `collection_failed`로 바꾼다(통과로 보지 않는다).
  - 대상 Deployment UID가 바뀌거나 취소되면 모든 series를 실패로 처리하고 중단한다.
  - Pod가 정확히 1개 Ready가 아니면 HTTP series를 실패로 처리한다.
- **표본에 남기는 것.** 각 표본에 실제 수집 시각과 **원시 응답의 sha256**을 기록한다. 응답 본문은 저장하지 않는다(`/items`의 사용자 자료가 남지 않는다). 묶음 전체에는 `provenance=captured`, `run_id`, `collector`를 붙인다.

**`EffectCaptureStore.seal`**

- 정규화한 JSON 바이트를 0700 폴더 / 0600 파일에 원자적으로 저장한다. 파일 이름은 바이트의 sha256이다.
- 반환값은 hash·run_id·개수뿐이다. 장부에는 이 hash만 남긴다.

**`SealedEffectReader`** (mode `sealed_capture`)

- symlink를 따라가지 않는다(O_NOFOLLOW). 일반 파일, 본인 소유, 0600, 크기 상한을 확인한다.
- 바이트를 재계산한 hash가 기대값과 일치해야 한다.
- `captured`만 받고, 모든 series가 이 영수증의 실행 hash에 결합되어 있어야 한다. deadline도 확인한다.

**`LeaseCleanupPort`** (mode `kubernetes`)와 `restart_lease_item`

- WF08의 Lease 기록에서 정체성 hash(이름·namespace·uid)와 소유자 hash를 만든다.
- **반납됐다고 보는 경우**: holder가 비었거나, 404이거나, UID가 바뀌었을 때(객체가 다시 만들어졌을 때).
- **비우는 경우**: 소유자 hash가 일치할 때만 비우고, resourceVersion으로 동시 변경을 막는다.
- **다른 holder**는 거절하고 PUT하지 않는다.
- **오류 처리**:
  - 409 → 충돌(확정 미적용)
  - 그 밖의 오류 → 결과 불명
  - 자격·임시 자원 → 미지원(unknown이 되어 사람에게 인계)

### `ai_ops.recovery_effects` (WF09 계약 확장)

- `EffectBatch.provenance`에 `captured`를 추가했다.
  - `captured`이면 `run_id`·`collector`와 모든 표본의 `source_sha256`이 반드시 있어야 한다.
  - `synthetic`이면 이 값들을 가질 수 없다.
- `EffectSample.source_sha256`을 추가했다(선택 항목, 본문 미저장).
- 판정 결과의 `provenance`는 이제 묶음의 값을 그대로 따른다.
- **판정 규칙은 바꾸지 않았다.** 기존 합성 시험도 그대로 통과한다.

## 권한·연결 관련 기록 (실제 연결 단계에서 결정)

- **읽기 신원**: `deployments/lab-api` get, `pods` list, **`pods/proxy` get**(이번에 새로 필요해진 권한, namespace `ai-ops-lab`).
  - lab 차트에는 Service가 없다. 그래서 C-04는 사람 권한의 port-forward를 썼다.
  - Pod proxy는 새 네트워크 경로를 열지 않는 대신 RBAC가 늘어난다. 실제로 연결할 때 RBAC와 VAP 검토가 필요하다.
- **Prometheus 신원**: `/api/v1/query`, `/api/v1/rules`의 GET만 허용한다. 클라이언트는 A7-3a와 같은 HTTPS 전용이다.
- **실제 PromQL과 경보 규칙**: 플랫폼에 고정된 Prometheus 규칙과 대조해 운영자가 정한다. 미구성이면 정책을 만들 수 없다.
- **경보 0건의 한계**: 대상 지표가 없어도 경보는 0건이 될 수 있다. 그래서 정책에서 지표 series(신선도 검사 포함)를 반드시 함께 통과해야 성공이 된다.

## 검증

신규 `test_restart_effects.py` 17개(가짜 API·가짜 시계)를 추가했다.

- **완전 관찰**: 봉인 파일만 읽어서 `succeeded`, `captured`가 된다. 모든 표본이 구간 안에 있고 원시 hash를 가진다. 파일은 0600, 폴더는 0700이다. `/items` 본문은 남지 않는다. GET만 하고, proxy 경로는 고정된 2개뿐이다.
- **성공이 되면 안 되는 경우**
  - 허용 시간이 지나도 Pod가 Ready가 아님 → 미해결
  - 옛 Pod 잔존, 다른 marker, 실행 중인 이미지 불일치 → 미해결
  - tick 지연으로 간격 초과 → 검증 불가
  - 빈 지표, lookback으로 보간된 오래된 지표 → 검증 불가
  - 임계값 초과 → 미해결
  - `/readyz` 응답 형식 불일치 → 미해결
  - proxy 503 → 검증 불가
  - 규칙 err, 정의 불일치, 구간 중 정의 변경 → 검증 불가
  - firing 경보 → 미해결
  - 대상 UID 변경, 취소 → 모든 series 검증 불가
- **실행 전 거절 8종**: 수집 요청 0개로 끝난다.
- **경로 허용 목록**: 로그·secret·다른 포트·다른 경로·다른 namespace·POST·query_range·admin을 거절한다.
- **봉인 리더**: 다른 영수증, deadline 경과, 변조, symlink를 거절한다. 합성 묶음은 봉인을 거절한다.
- **`captured` 계약**: run_id·collector·표본 hash가 빠지거나, `synthetic`과 섞이면 거절한다.
- **Lease 회수**: 자기 것만 비우고 resourceVersion을 유지한다. 반납 확인 3종(holder 없음·404·UID 변경), 타 holder 미해제, 409, 결과 불명, 미지원 종류를 시험했다.
- **WF09 조정기가 계속 막는지**: 봉인 리더·관찰기·Lease 포트를 모두 `live_*_disabled`로 거절하는지 확인했다.

전체 결과는 [검증 기록](restart-effects-adapter-local-validation-20261119.json)에 있다.

## 범위 밖 (이후)

- PR 경로의 실제 원복 관측(GitHub·Argo 어댑터)
- 증거와 봉인 파일의 주기 보관 정리(A7-5)
- 프로세스 연결: 관찰 시작 시점, 영수증·정책 구성, 장부에 봉인 hash 기록, Slack 결과
- RBAC(`pods/proxy` 포함)·VAP·토큰, 실제 PromQL·규칙 고정, 실클러스터 shadow 검증, WF11 live 판단

실제 클러스터·배포·Slack·장애 실습 없음. 기존 시간·실행별 승인·정확 head 리뷰/CI·잠금·원복 조건 변경 없음.
