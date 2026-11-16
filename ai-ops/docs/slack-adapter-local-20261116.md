# 인증된 Slack 요청·결과 회신 어댑터 로컬 구현

2026-11-16. AI main `a66822ba1abeb12e411eda8423f5753fb13ad8dc`를 기준으로 구현했다. 실제 Slack 앱·서명 키·bot token·API·공개 수신기·실습은 연결하지 않았다. 모든 서명/신원은 합성 fixture이며 회신 transport는 `local_fixture`만 허용한다. 이 문서는 구현·검토·로컬 검증 기록이며 최종 PR/CI/main 반영 상태는 HANDOFF를 따른다. 실환경에는 배포하지 않았다.

## 요청·처리·회신 흐름

```text
합성 Slack app_mention HTTP 요청 (loopback)
  → 원본 bytes의 v0 HMAC·요청 시각 검증
  → workspace/app/user/channel/target 권한 검증
  → 별도 영속 inbox에 접수하고 빠른 ACK
  → drain_one: 기존 TaskService → JobQueue 전달
  → 별도로 실행한 기존 Worker가 규칙 조사·보고서 저장
  → 기존 IncidentStore를 통해 결과 재검증
  → 허용된 원래 channel/thread로 fixture 회신
```

지원 명령은 `<@봇사용자ID> investigate 등록대상별칭 [질문]`이다. `approve`, `recover`, `cancel`, PR 병합·동기화·장애 명령은 연결하지 않았다. slash command는 이번 입력 형식이 아니다. 실제 Slack의 slash command는 메시지 스레드에서 실행할 수 없으므로 원래 메시지/스레드에 회신하는 초기 경로에는 app_mention을 사용했다. [Slack slash command 설명](https://docs.slack.dev/interactivity/implementing-slash-commands/)

## 재사용과 추가 구성

| 구성 | 책임 |
|---|---|
| 기존 TaskService/JobQueue | 대상·입력·중복 검증, 실제 작업 ID, 조사 상태/보고서 참조. 변경하지 않음 |
| 기존 Worker/IncidentStore | 수집·규칙 분석·보고서 재검증. 새 분석 worker를 만들지 않음 |
| `slack_adapter.py` | Slack 서명/인가·설치 신원 고정·영속 inbox·큐 전달·결과 회신/조정 장부 |
| `slack_local.py` | `/slack/events` POST를 127.0.0.1 임의 포트에서만 검사하는 로컬 HTTP harness |
| `slack-local.example.json` | 가짜 workspace/app/bot/user/channel과 기존 practice 대상의 허용 목록. 키/토큰 없음 |
| `check_slack_adapter_local.py` | 합성 서명→실제 로컬 HTTP→기존 큐/fixture worker→가짜 스레드 회신·중복/재시작·회수 검사 |

inbox는 Slack 이벤트 접수의 기준이고 JobQueue는 조사 작업의 기준이다. inbox ACK는 큐 접수나 분석 완료를 의미하지 않는다. 큐 전달은 ACK 처리 안에서 실행하지 않아 큐가 막혀도 Slack 수신 처리를 분리한다. 기존 worker가 실제 조사를 수행하며 relay는 추가 조사를 실행하지 않는다. 로컬 mailbox와 Slack 회신 장부도 구분하며 Slack 회신에 `TaskService.deliver`를 사용하지 않는다.

## 인증·권한 경계

- JSON 파싱 전에 원본 body bytes로 `v0:timestamp:body`의 HMAC-SHA256을 검증하고 `compare_digest`로 대조한다. 요청 시각과 로컬 시각 차이가 300초를 넘으면 거절한다. 서명/시각/Content-Type 헤더의 이름은 대소문자를 구분하지 않고 중복은 거절한다. [Slack 서명 규약](https://docs.slack.dev/authentication/verifying-requests-from-slack/)
- 서명은 Slack 출처 확인이고 사용자 권한과는 별개다. workspace/app이 고정 설정과 같고 허용된 user/channel/target 조합이어야 접수한다. bot 메시지·subtype·다른 team context·외부 공유 채널은 거절한다.
- 허용 명령과 등록된 대상만 읽기 조사한다. 요청의 `response_url`을 회신 목적지로 받지 않는다. 회신 channel/thread는 서명된 원래 이벤트에서 고정한다. thread가 없으면 해당 메시지 ts를 부모로 사용한다. [Slack thread_ts](https://docs.slack.dev/reference/methods/chat.postMessage/)
- 한 저장소에 workspace/app/bot 설치 신원을 고정한다. 다른 설치로 같은 저장소를 재사용할 수 없다. 신뢰 설정을 사용하는 새 인스턴스의 정책/config/서명 키 버전이 활성화되면 이전 인스턴스는 접수·큐 전달·회신·정리를 수행할 수 없다. 실제 키 원문을 장부에 저장하지 않는다.
- 정책·config·대상 권한·기한을 큐 전달/회신 직전 다시 검사한다. 같은 event ID의 본문·사용자·채널·스레드 변경은 충돌로 거절한다. 헤더의 retry 번호는 인증/중복 우회 근거가 아니다.

## 빠른 ACK와 재시작 처리

Slack Events API의 3초 응답 규약에 맞춰 분석이나 회신은 수신 처리에서 수행하지 않는다. ACK에는 request ID와 `intake_state`, `queue_accepted`를 구분한다. 2.5초 ACK 예산을 넘기면 성공으로 위장하지 않고 로컬 HTTP는 503을 반환한다. 이미 영속 저장됐으면 같은 이벤트 재전송으로 기존 영수증을 찾는다. inbox SQLite 잠금 대기는 250ms로 제한한다. 디스크/OS까지 포함한 실환경 3초 보장은 아직 측정하지 않았다. [Slack 이벤트 응답·재시도](https://docs.slack.dev/apis/events-api/#responding-to-events)

한 사용자당 60초에 새 요청5개·전체 미전달 inbox100개를 상한으로 두며 동일 이벤트 재시도는 새 요청으로 계산하지 않는다. queue commit 이후 relay 응답이 유실돼도 기존 TaskService의 안정된 요청 키로 같은 job에 연결한다. 일시적인 큐 오류는 sanitized inbox를 유지하고, 영구 입력 거절은 failed로 격리해 다른 요청을 막지 않는다.

검토 보완: 접수 상한/잠금/ACK 지연은 HTTP503으로 재시도를 허용하고, 서명/인가/입력의 영구 거절 HTTP403에는 `X-Slack-No-Retry: 1`을 붙인다. 거절을 접수 성공으로 바꾸거나 인가를 완화하지 않는다. [Slack 재시도 중단 규약](https://docs.slack.dev/apis/events-api/#turning-retries-off)

## 결과 회신·실패 처리

결과는 재검증된 규칙 보고서의 상태·고정 진단 요약·사건/실행 참조와 task/request ID만 사용한다. raw evidence·원래 질문·자유 보고서 문장을 회신에 복사하지 않는다. partial 결과는 partial로 표시한다. 링크 펼침·broadcast·markdown 처리를 끄고 권한이 있는 원래 스레드로만 회신한다.

HTTP 성공뿐 아니라 `ok=true`·channel·thread·message ts·text 결합을 확인한다. `ok=false`를 성공으로 기록하지 않는다. 실제 호출은 없으며 이 응답은 fixture transport의 Slack 형식을 검증한 것이다. [chat.postMessage 응답](https://docs.slack.dev/reference/methods/chat.postMessage/)

| 상황 | 처리 |
|---|---|
| HTTP429 + 유효 Retry-After | 응답을 받은 시점부터 대기 후 회신만 재시도. 전체 최대3번 |
| 전송 시도 전임이 확실한 실패 | 회신만 제한 재시도. 분석을 다시 실행하지 않음 |
| HTTP5xx·timeout·응답 유실·불명확한 응답 | needs_review, 자동 재전송 금지 |
| post 중 프로세스 중단 | sending 유지, 재시작 후 자동 재전송 금지 |
| 조정 | 같은 채널/스레드·text·bot 작성자의 기존 메시지1건을 확인할 때만 전달 성공으로 기록 |
| 권한 철회·기한 만료·설치 신원 불일치 | 회신 거절. 다른 목적지/사용자로 바꾸지 않음 |

회신 재시도는 분석/모델/복구 재실행을 뜻하지 않는다. fixture의 `delivered`는 실제 Slack 전달이 아니다. 실제 연결 후에도 API 수락은 사람이 읽었다는 증거가 아니므로 `human_read_confirmed=false`이며 승인 영수증이나 GitHub 리뷰로 사용하지 않는다.

검토 보완: 한 번 확인한 message ts는 조정 실패 후에도 유지해 다른 동일 내용의 메시지로 바꾸지 않는다. 메시지 조회 뒤 현재 권한/config/기한을 다시 검사하며 조건이 달라졌으면 delivered로 기록하지 않는다. 실제 전송을 다시 실행하지 않는다.

inbox/회신 장부는 사용자 전용0700폴더·0600SQLite/WAL로 보호한다. 질문은 정제한 사본만 큐 전달 전까지 보관하고 이후 제거한다. 7일 만료 정리는 질문·사용자/채널/스레드·메시지 ts를 회수하고 중복 방지 tombstone만 유지한다. 실제 Slack 메시지를 삭제한 것으로 주장하지 않는다.

## 로컬 검증

```sh
PYTHONPATH=ai-ops/src .venv/bin/python ai-ops/scripts/check_slack_adapter_local.py
PYTHONPATH=ai-ops/src:ai-ops/tests .venv/bin/python -m unittest test_slack_adapter -v
```

기존 requirements.lock을 설치한 Python3.11 이상 venv를 사용한다. 추가 패키지나 실제 signing secret/bot token은 필요하지 않다. self-check는 합성 키로 서명하고 loopback 이외의 접속과 모델 키 조회를 차단한다. 종료 시 임시 DB·HTTP 서버 소켓을 정리한다.

최초 구현의 새35개·전체986개 시험/skip0 이후 검토에서 회귀5개를 추가했다. 수정 전 조정 실패3건을 재현했다. 검토 보완 후 집중40개·전체991개 시험/skip0과 로컬 전경로 검사를 통과했다. 정적 검증과 최종 CI/반영 상태는 같은 JSON과 HANDOFF를 따른다. 신원/권한/키 버전 변경·다른 허용 사용자로 event 재결합·응답 유실·429·영구 실패 격리·상한·보관 기한·민감값 비출력을 확인한다. self-check의 ACK 지연은 localhost 표본이며 실환경 SLO나 기존15초 실습 성공 근거가 아니다. GitHub CI에도 합성 Slack 전경로 self-check를 추가했다. 기존 SQLite ResourceWarning1건은 남았다.

기존 승인/기한/잠금/원복 제어9파일과 큐·worker·공통 TaskService는 main과 byte동일하다. 시작15초·실행별 새readiness승인·정확head GitHub 실제사람리뷰/CI·리뷰최소120초/시작후210초마감·소유원복240초/독립원복시작300초·관찰120초를 유지한다. Slack 메시지·회신 상태는 이 조건을 대신하지 않는다.

다음은 이 로컬 소스의 코드검토·CI 반영이다. 실제 연결에는 별도로 검토한 Slack 앱·권한·키/토큰 보호·TLS/수신 배포·운영 응답 정책·실제 회신 transport가 필요하며 현재 live transport는 비활성이다. 이를 자동 설치/연결하거나 새 실습을 시작하지 않는다. 기존 C04/C05·A5/A7·실행 저장소 전환·AI포트폴리오 보류 판정은 유지한다. 추천: GPT-6 SOL · High.
