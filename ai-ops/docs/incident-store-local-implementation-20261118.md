# WF-02 사건 저장·큐 연결·보관·장부 전환 로컬 구현

2026-11-18 KST. 기준 AI main `36a55fdb59ec63c34cd7bcb339cada39ec0b515b`. 작업 브랜치 `codex/incident-workflow-plan-20261118`.

[WF-01 계약](incident-identity-channel-policy-contract-20261118.md) · [작업표](incident-workflow-implementation-tracker.md) · [전체 계획](incident-workflow-and-improvement-plan-20261118.md)

## 구현 범위와 현재 상태

`src/ai_ops/event_store.py`에 영속 사건 장부와 기존 JobQueue 연결을 구현했다. `job_queue.py`와 `worker.py`에는 사건 연결 작업의 수집 전 검사만 추가했다. 기존 IncidentStore 보고서·큐 시도/Lease·Collector·규칙 분석기를 재사용한다. 신규 사건 저장소가 없는 큐는 기존 동작을 유지한다.

**로컬 코어 구현이다. HTTP 인증 수신기·Slack 새 명령·공지 템플릿·결과 전송·자동 maintenance 호출은 아직 연결하지 않았다.** 현재 서비스의 자동 조사 정책이나 채널을 변경하지 않았다. 실제 배포·외부 연결·장애 주입·원격 CI·main 반영은 수행하지 않았다.

## 사건과 저장 계약

- 사건 ID는 `evt-`와 canonical JSON의 전체 SHA256이다. schema_version/source_id/cluster_id/fingerprint/나노초 starts_at을 사용한다. HA replica는 같은 논리 source를 사용하며 동등한 시간대 표기는 같은 ID다.
- 발신원 registry는 서버가 넘기는 `{alias: {source_id, cluster_id}}`다. 인증된 source_alias와 경보 cluster가 다르면 거절한다. registry 변경은 기존 저장소에 묵시 적용하지 않는다.
- 원래 labels는 저장하지 않고 로컬 비밀키 기반 fingerprint로 불변성을 검사한다. annotations·generatorURL·원문 질문·토큰은 새 장부에 저장하지 않는다. 대상은 등록 Target과 정확히 일치해야 수집 대상으로 연결된다.
- Watchdog는 사건에서 제외한다. 등록 대상이 없는 일반 경보는 unbound 메타데이터만 남긴다. 같은 발생키의 다른 labels/Target은 충돌로 거절한다.
- `al1/source_alias/fingerprint/startsAt`과 evt 참조를 기존 사건으로만 해석한다. 외부 URL 조회나 사용자 메시지에서 사건 생성은 없다. 없는 사건과 만료 사건은 각각 명확한 오류이며 성공으로 표시하지 않는다. 외부 권한별 오류 가림은 WF-04 어댑터 책임이다.
- `accept(source_alias, raw)`는 version4·receiver `ai-events`·64KiB·최대32개·truncatedAlerts=0을 검사한다. 전체 묶음을 검증하고 resolved 우선으로 하나의 트랜잭션에 기록한다. 현재 `ai-investigation` 수신기를 교체하거나 새 HTTP 라우트를 등록하지 않는다.
- resolved는 뒤늦은 firing으로 되돌리지 않는다. 사건 해제와 조사 완료·인간 승인·복구 성공은 별개다. 공지 전달 상태는 계속 `unknown`이다.

같은 사용자전용 `queue/jobs.sqlite3`에 다음 확장 테이블을 둔다. 기존 작업/보고서 장부를 다른 의미로 바꾸지 않는다.

| 테이블 | 역할 |
| --- | --- |
| event_meta | 확장 schema version1·발신원 registry hash |
| event_policy | revision·활성화 시각·설정 hash·정확 자동 예외 |
| events | evt 불변 identity·대상 해석·발생/해제·만료 상태 |
| event_requests | 요청 중복키·정제 payload·pending/delivered/cancelled/expired·job 연결; outbox와 연결 장부를 한 행으로 구현 |
| event_legacy | 검증한 기존 episode_key→evt 매핑 |

확장을 처음 생성할 때 queue user_version을3으로 올린다. 큐만 사용하는 새 저장소는2를 유지한다. 이전 바이너리는3을 열 수 없어 새 정책 검사를 건너뛰지 못한다. **실물 전환 시 실행 중인 이전 worker/접수기를 먼저 멈춰야 한다.** 이미 열린 이전 프로세스를 PRAGMA가 자동 종료하지는 않는다. 버전 숫자만2로 낮추는 다운그레이드는 금지한다.

## 조사 정책과 큐 전달

초기 policy는 revision0·manual·자동 예외0개다. `set_policy(expected_revision=..., automatic_rules=..., delivery_ready=...)`는 현재 revision과 일치할 때만 변경한다. 설정 저장 형식은 테이블의 canonical 입력 구조이며 운영 설정 파일/HTTP 관리 API는 추가하지 않았다.

자동 규칙 필드는 `source_alias`, `target`, `alertname`, `channel_id`, `audience`다. source_alias는 고정 source+cluster를 결정하고 target은 현재 등록별칭이다. wildcard·중복 규칙·허용5종 밖의 경보·다른 cluster·미등록 대상은 거절한다. 결과 전달 어댑터가 준비됐다는 **서버 측 설정**이 없으면 규칙 활성화를 거절한다. 시험의 delivery_ready=True는 합성 조건이며 실제 전달 준비의 증거가 아니다. WF-03/04 구현과 실제 연결 승인 전 live 활성화 금지.

자동 예약은 새 발생·firing·ai_ops=enabled·정확 규칙·현재 설정·정책 활성화 이후 startsAt을 모두 만족해야 한다. 기존 사건 재전송이나 뒤늦게 도착한 과거 발생은 소급 자동조사하지 않는다. 사건당 자동/legacy 연결은 unique 제약으로 최대1개다.

명시 조사 API는 이미 인증된 subject와 서버가 계산한 allowed_targets를 받는다. 같은 요청 ID/내용/subject는 같은 예약을 반환하고, 내용·사건·subject를 바꾸면 충돌이다. 허용5종 밖의 bound 사건과 resolved 사건도 명시적인 읽기 조사를 할 수 있다. 새 요청 ID는 현재 자료를 다시 수집하며 완료된 옛 보고서를 재활용하지 않는다. 일반 `investigate` 경로는 바꾸지 않았다.

사건 등록+자동 예약, 요청 예약, outbox의 기존 큐 전달+job 연결을 각각 동일 DB 트랜잭션으로 처리한다. 전달 후 예약 payload는 비우고 기존 큐에 정제 질문만 유지한다. 일부 큐 슬롯만 남으면 전달 가능한 앞부분을 commit하고 나머지는 미전달 상태로 남긴다. 처음부터 큐가 가득 차거나 다른 오류가 발생하면 rollback해 다시 전달할 수 있다. 작업 키는 안정된 해시이며 전송 직후 응답이 유실돼도 새 job을 만들지 않는다.

정책 revision·사건 해제·보관 만료·설정 변경을 전달 시 검사한다. `claim`과 worker 수집 진입 직전에도 자동 revision/해제/만료를 검사한다. 정책이 바뀐 자동 대기는 취소하고, 실행 중 읽기 수집은 기존 소유권/시간 조건 안에서 끝날 수 있다. 결과를 실행 승인으로 승격하지 않는다. 정책 변경 전후를 하나의 원자적 네트워크 동작으로 만들었다고 주장하지 않는다.

worker 보고서의 inc/run/bundle은 기존 job에서 조회한다. evt는 발생 사실, 보고서는 실제 수집 시점의 근거다. 향후 Slack 회신은 이 두 시점을 함께 표시해야 하며 이는 WF-04에서 구현한다.

## 보관과 용량

- 사건 상세 정보의 논리 유효기간은 **발생 startsAt부터7일**이다. 늦게 수신해도 기한을 연장하지 않는다. 사건이 firing이어도 기한이 지나면 새 조사를 거절한다. 장기 장애는 별도 운영 인계 대상이며 기존 회차를 몰래 다시 만들지 않는다.
- `expire()`는 미리보기, `expire(apply=True)`는 실제 로컬 상세 정리다. metadata·identity fingerprint·endsAt·수신 시각과 예약 질문/내용 fingerprint/정책revision을 지운다. queued 연결 작업은 만료 취소한다.
- collecting/analyzing 작업의 연결 정보는 해당 작업이 끝날 때까지 정리를 보류한다. 기존 Lease/재시도/운영자 취소로 종료한 뒤 다시 정리한다. 진행 중 근거 파일을 삭제하거나 시간을 늘리지 않는다.
- evt/요청의 해시 중복키·최소 상태·job 연결은 tombstone으로 남겨 재전송 부활을 막는다. 원래 경보 원문/subject/질문은 tombstone에 없다.
- 사건 최대10,000개·요청 최대10,000개에 tombstone도 포함한다. 용량이 차면 새 접수를 거절하고 기존 중복키는 유지한다. 원래 대기/진행 큐100개 한도도 유지한다. 자동 무한 확장·tombstone 삭제는 없다.
- 전체 장부의 장기 아카이브/교체 자동화는 구현하지 않았다. 한도 도달 시 운영자에게 이관하며, 중복키를 지워 재전송이 새 사건이 되도록 만들지 않는다.
- 기존 QueueRetention은 작업 생성부터7일의 보고서/큐 데이터 정리를 계속 소유한다. 사건 expire와 QueueRetention을 순서대로 실행한 호환 시험을 통과했다. **새 expire의 주기 호출은 WF-04 실행 연결 때 추가해야 하며, 현재 자동 물리 삭제를 보장하지 않는다.**

## 기존 alert_episodes 전환

기존 장부는 episode 해시만 보유하고 원본 fingerprint/labels가 없어 evt를 역산할 수 없다. `migrate_legacy(source_alias, alert)`에 인증된 재전송 자료를 제공해 기존 parser의 episode_key·labels fingerprint·Target hash·경보명·시각을 모두 확인한다. 연결 job의 key·payload·현재 설정도 확인한다. 삭제 중/이미 지워진 job이나 불일치 자료는 자동 전환하지 않는다.

검증되면 같은 트랜잭션에서 evt와 legacy→evt, 원래 job_id를 연결한다. job 상태·시도·보고서·기존 alert_episodes는 마이그레이션 자체에서 변경하지 않는다. 중복 전환은 같은 evt를 반환하되 새 해제 상태/시각과 보관 만료를 다시 확인한다. 기존 회차가 새 수신 경로로 먼저 오면 이중 조사를 막기 위해 검증 전환을 요구한다. 기존 resolved와 새로운 resolved 중 하나라도 있으면 해제를 보존한다.

전환된 **아직 시작하지 않은** legacy 작업은 다음 claim에서 `legacy_requires_manual_request`로 취소된다. 옛 자동조사 허용을 새 manual 정책의 승인으로 간주하지 않기 때문이다. 원래 job과 시도 이력은 보존하며 필요하면 현재 인가로 새 명시 요청을 만든다. 실행 중 작업은 전환 전 운영 절차에서 종료를 기다린다.

재전송 자료가 없는 행은 추측 변환하지 않고 기존 DB에 남긴다. 실제 전환은 백업→이전 접수/worker 중지→현재 작업 종료→verified replay 대조→매핑/미전환 목록 확인→새 worker/수신기 기동 순서로 별도 계획한다. 이번에는 임시 시험 DB만 사용했다. 기존 실물/개인 DB를 읽거나 전환하지 않았다.

## 검증 근거

`tests/test_event_store.py`의 초기 집중29개, 검토 보완 후32개가 통과했다. 실제 private SQLite, 기존 JobQueue, 규칙 worker와 QueueRetention을 사용했다. 주요 범위:

- 시각 오프셋/나노초/참조·HA 중복·불변 identity 충돌·발신원/cluster·unbound·resolved 우선 원자 묶음.
- manual 기본·정확 자동 예외·전달 준비 차단·정책 CAS·소급 금지·정책 변경/해제 후 수집0.
- claim 뒤 해제 경쟁·1시간 뒤 resolved 사건의 새 읽기 조사·실제 inc/run 생성·기존 일반 조사 유지.
- 원문 비출력·질문 정제·subject/내용 충돌·완료 보고서 재사용 금지·큐/장부 포화.
- 4개 동시 접수/전달·예약/전달 트랜잭션 rollback·별도 프로세스 os._exit(77) 직후 DB 재개·commit 후 재시작 중복0.
- 7일 만료 preview/apply·진행 작업 보류·tombstone 재생성 차단·기존 QueueRetention 근거 삭제 호환.
- 기존 job/id/시도 보존 전환·누락/충돌 거절·resolved-first 전환·확장 버전 오류의 실행 차단.

전체 회귀 결과는 같은 폴더의 `incident-store-local-validation-20261118.json`에 기록한다. WF-01 T09/T11/T12/T16의 외부 인증·실제 두 채널·템플릿·전송 장애, T17의 새 UID 실물 관찰은 이번 검증 완료로 세지 않는다. 외부 Slack/GitHub/AWS 요청은 없다.

## 다음 단계와 유지 조건

다음은 WF-02 코드 검토·CI/main 반영이다. 이후 WF-03의 공지 템플릿/발신원 계약과 WF-04의 인증된 사건 요청/결과/maintenance 연결을 구현한다. 기존 채널을 합치거나 새 history scope를 요구하지 않는다.

Lease3초·시작15초·리뷰 최소120초/마감210초·소유원복240초/독립원복300초·관찰120초, 실행별승인·정확head 사람리뷰·CI·잠금·ACK·회수 조건을 변경하지 않았다. 복구/명령 실행 코드를 추가하지 않았다. C04/C05 미완료와 기존 실측 시간 부족은 그대로 남는다. 추천 GPT-6 SOL · High.

검토 후 전체1325개 통과와 수정3건은 [코드 검토 기록](incident-store-review-20261118.md)에 있다.
