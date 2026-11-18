# WF-04 사건 지정 Slack 조사·상태·결과 로컬 구현

2026-11-19 · **로컬검증완료, 미커밋·CI/main/배포 미실행**. 기준 AI main `ca63fcf47f17a0e02e1b1e91eb1e01bcdd2e2feb`, 작업 브랜치 `codex/wf04-slack-events-20261119`.

[전체 계획](incident-workflow-and-improvement-plan-20261118.md) · [사건 계약](incident-identity-channel-policy-contract-20261118.md) · [작업표](incident-workflow-implementation-tracker.md) · [검증 JSON](incident-slack-local-validation-20261119.json)

## 변경 이유와 완료 범위

WF-02 사건 장부와 WF-03 경보 참조는 연결됐지만 Slack의 기존 `investigate 대상`은 어느 경보 회차를 조사하는지 알지 못했다. WF-04는 별도 조사 큐/worker를 만들지 않고 **인증된 사용자 요청 → 사건 인가 → 기존 outbox/JobQueue → 기존 Worker → TaskService 결과 → 원래 요청 스레드**를 연결한다. 경보 발생 사실, 조사 접수, 조사 결과, Slack API 수락, 사람 승인/복구 성공을 구분한다.

현재 배포는 없으며 아래 명령은 이번 로컬 소스에서만 검증했다. 실제 Slack 봇에 반영됐다고 안내하지 않는다. 인프라는 정리 상태이고 C-04/C-05는 계속 미완료다.

## 입력과 회신

| 입력 | 동작 |
| --- | --- |
| `@봇 investigate-event <alert_ref 또는 evt-ID> [질문]` | 서버 등록 사건·현재 사용자/채널/대상 인가 후 기존 큐에 새 읽기 조사. 완료 결과를 요청한 같은 thread에 회신 |
| `@봇 status-event <alert_ref 또는 evt-ID>` | 현재 경보 상태와 같은 사용자·채널이 요청한 최근 작업 최대10건의 조사/결과 상태. 새 조사/worker 실행 없음 |
| `@봇 investigate-event this [질문]`, `status-event this` | 같은 사용자·채널·thread의 유효한 연결 사건이 정확히 하나일 때만 해석. `이장애`도 같은 토큰으로 지원. 모호하면 명시 ID 요청, 조사0 |
| 기존 `@봇 investigate <등록별칭> [질문]` | 그대로 지원. 새 요청에만 ‘사건 미지정 일반 조사’ 표시. 이전 전달 장부의 내용 해시는 유지 |
| 복구/승인/실행 명령 | 지원하지 않음. 조사 질문에 복구 문장이 있어도 읽기 조사만 수행 |

서명 확인 뒤 ACK는 **영속 수신함 접수만** 뜻한다. 사건 해석·작업 생성은 relay에서 수행하며 ACK 중 수집/Slack API 호출은 없다. 조사 큐 접수 전 성공한 조사라고 표시하지 않는다. 대상별 현재 권한은 제출·회신 및 outbox/worker 수집 직전에 재검사한다.

결과에는 사건 ID, 경보 발생/해제 시각, 상태를 확인한 시각, 실제 자료의 시간 구간·수집 시각, 기존 판정/부족자료/런북 ID·버전·해시·절, job/inc/run을 함께 표시한다. 오래된 resolved 사건의 명시 조사도 가능하지만 현재 수집을 발생 당시 증거로 표시하지 않고 UID 동일성도 추정하지 않는다. 현재 자료만으로 과거 원인이 확정되거나 partial이 자동 해소되지는 않는다.

## 정보 노출과 대기 처리

- 인증되지 않은 사용자/다른 워크스페이스/경보 채널 등 미허용 채널은 기존 서명·인가 단계에서 거절한다. 임의 링크 조회나 Slack history scope 추가 없음.
- 등록되지 않은 참조, 권한 없는 대상, unbound 사건, 상세가 지워진 tombstone은 동일한 비공개 응답을 쓴다: ‘사건 접수 확인 중이거나 조회할 수 없는 참조; 새 조사 없음’. ID·대상·경보 상태를 반사하지 않는다. 이 응답은 ‘사건이 실제로 없다’는 증명이 아니다.
- 상세가 남아 현재 대상 인가를 확인할 수 있는 만료 사건은 만료 응답을 준다. 상세 삭제 뒤에는 존재 여부 보호가 우선하므로 만료와 미등록을 구분해 노출하지 않는다. WF-01의 pending/expired 내부 상태와 외부 표시를 이 기준으로 구체화했다.
- 경보 공지와 접수 순서가 뒤바뀌면 자동 재조회/조사하지 않는다. 사용자가 새 메시지로 다시 요청한다. 같은 Slack event_id 재전송은 원래 접수를 유지한다.
- 상태회신은 준비 시각의 고정 snapshot이며 다른 사용자의 job ID/질문/결과를 포함하지 않는다. 새로운 상태가 필요하면 새 `status-event`를 보낸다. Slack delivered는 API 수락이며 사용자 읽음/사람 승인 아님.

## 재사용과 추가 코드

| 구성 | 변경 |
| --- | --- |
| `slack_adapter.py` | 기존 서명/워크스페이스/사용자·채널/시간·용량/영속 inbox·전송 장부 재사용, 사건 명령과 ready 상태 추가. 기존 요청 형식 유지용 부가 표 |
| `slack_events.py` | 사건 참조·현재 ACL·같은 thread 해석, 사건 outbox와 기존 job 연결, 상태/결과 설명 |
| `event_store.py` | 대상 인가 조회, 지정 request_key만 drain, 요청 authority/세대 fence. 기존 manual 기본·정확 자동예외 정책 유지 |
| `tasks.py` | 기존 결과 API에 사건용 시간 구간을 명시 옵션으로 제공. 기존 호출/전달 payload 형식 유지 |
| `slack_runtime.py`, `slack_server.py` | ingress/relay 동일 opt-in source registry, job 없는 상태회신도 기존 전송기 사용. 기존 60초 maintenance 경로에서 사건 expire 호출 |
| `slack-events.disabled.example.json` | 합성 source/cluster만 있는 비활성 예시. 실제 설정·chart/Secret/권한을 변경하지 않음 |

`event_sources`는 기본 빈 값이어서 기존 배포 설정은 사건 명령을 자동 활성화하지 않는다. 접수기의 source_alias/source_id/cluster와 동일한 서버 설정을 사용하며 사용자 텍스트로 선택/확장하지 못한다. 이번 어댑터는 자동 outbox를 배출하거나 자동조사 결과 전송을 활성화하지 않는다. 자동예외 결과 전달은 별도 대상/audience 설계·검증이 필요하다.

## 중단·재시작·보관·전환

- Slack 요청과 사건 연결은 같은 inbox 트랜잭션에 기록한다. outbox 예약/큐 commit/Slack job 연결 사이에서 중단돼도 같은 request_id로 기존 예약/job을 연결한다. 완료한 과거 작업을 새 요청의 현재 결과로 조용히 재사용하지 않는다.
- 전달 장부는 기존 pending/sending/needs_review/delivered 정책을 유지한다. 응답 유실이나 sending 상태는 재전송하지 않고 기존 메시지 조정 경로를 쓴다. 네트워크 전달의 exactly-once를 보장한다고 주장하지 않는다.
- 정책/설정/서명 키/연결 활성 상태가 바뀌면 이전 authority의 pending/queued 작업은 outbox/claim/수집 전 fence에서 취소한다. 이미 진행 중인 읽기 수집을 되돌리는 기능은 아니며, 결과 전송은 현재 인가를 재확인한다. 웹/로컬 fixture authority는 서로 분리된다.
- 기존 사건 발생 기준7일·장부 상한·active 근거 보존을 유지한다. relay의 기존60초 정리가 event expire를 호출하고 사건 설명의 Slack 복사본도 지운다. 요청이 나중에 생성돼도 사건 상세 보관 기간을 연장하지 않는다. 해시 tombstone과 기존 job/시도는 유지한다.
- `event_meta.version`은 1→2, 큐 `PRAGMA user_version`은 기존3 유지. 변경은 트랜잭션 안에서 부가 표와 함께 이루어진다. 이전 WF-02 worker는 v2를 거절해 새 인가 검사를 우회하지 못한다. v2의 인가 표가 누락되면 자동 복구하지 않고 거절한다.
- **재설치/배포 때** 기존 접수기·worker·relay 중지 → 개인 DB 백업/무결성 검증 → 같은 검토 버전의 세 프로세스 전환 → source/수동정책/채널·대상 diff 검토 → 새 기준선·회신 확인 순서가 필요하다. 예전 worker로 이미 전환한 DB를 그대로 여는 rollback은 지원하지 않는다. 코드 rollback에는 검증한 전환 전 DB와 신규 요청 대조가 필요하다. 실물 DB 전환은 이번에 수행하지 않았다.

## 로컬 검증

신규 집중 **36개**, 전체 **1370개(83.71초, skip0)** 통과. Python AST8개·diff·저장소 소유 경계 통과. 전체 로그 해시와 변경 파일 해시는 검증 JSON에 있다. 신규 ASGI 통합시험은 실제 소켓 연결과 모델 키 로딩을 차단하고 합성 HTTP 전송기를 주입했다. 기존 전체 시험의 loopback/TLS fixture도 외부 실환경 검증이 아니다.

주요 사례: manual 요청 전 job0; 서명/다른 사용자·채널·대상 거절; 없는/미허용/unbound 동일 응답; 공지 선도착; 상태조회 job0; 같은 thread·evt·런북 출처; 오래된 해제 사건과 현재 수집 구분; 요청 중복·예약/큐 commit 후 중단·재시작; 응답 유실 재전송0; 정책 변경 후 outbox·worker 차단; 다른 사용자 job 비노출; 같은 이름 대상 설정 변경 거절; 사건 만료/젊은 Slack 복사본 정리; 구버전/불완전 스키마 거절; 기존 회신/Task 결과 형식 보존.

초기 시험에서 fixture 대상 개수가1개인데 둘째 대상을 참조한 시험 오류1건을 고쳤다. 검토 중 권한 변경 후 남은 예약, 사건 만료 뒤 복사본 보관, 이전 전달 payload 호환성을 보완한 뒤 최종 시험을 실행했다. 기존 SQLite ResourceWarning 등은 전체 로그에서 별도 보존하며 외부 장애로 해석하지 않는다.

재실행(비밀값·인프라 불필요):

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=ai-ops/src python -m unittest discover -s ai-ops/tests -p 'test_slack_event*.py' -v
```

잠금 의존성을 설치한 Python 환경을 사용한다. 전체 시험은 WF-03 고정 플랫폼 source와 amtool을 명시해야 해당 교차시험이 skip되지 않는다.

## 2026-11-19 검토 보완

[코드 검토 기록](incident-slack-review-20261119.md)의 정책 전환 중단·연결 비활성화 후 보관2건을 보완했다. 최초 로컬 결과/해시는 역사 기록으로 보존하고 최종 소스는 검토 검증 JSON을 따른다. 인프라/실제 Slack/장애 실행 없음.

## 유지한 조건과 다음 작업

Lease GET3초·시작15초·안내 후 최소120초/시작 후210초 리뷰 마감·소유원복240초/독립원복300초·회복관찰120초, 실행별 승인·정확 head 사람 리뷰/CI·잠금/ACK·원복·자격 회수를 변경하지 않았다. 해당 실행기 소스의 기준 commit 대비 동일 해시를 기록했다.

이번에는 인프라 설치/배포·Slack 전송·장애 실습·Terraform·모델 호출·원격 CI·커밋/push를 하지 않았다. 다음은 **WF-04 코드 검토 → CI → main**이며 실제 연결은 WF-10의 새 계획/대상/자료 검증 이후다. 이후 WF-05 복구 계획/지원 조치 계약을 로컬로 구현할 수 있다. 추천 GPT-6 SOL · High.
