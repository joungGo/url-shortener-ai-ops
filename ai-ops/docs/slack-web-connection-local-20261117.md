# Slack 실제 연결용 코드 · 로컬 검증 (2026-11-17)

> 이 문서는 최초 로컬 구현 시점의 기록이다. 후속 검토에서 보완한 종료 취소 처리와 검증 결과는 [Slack 코드 검토](slack-web-review-20261117.md)를 따른다. 아래 미커밋/다음 작업 표기는 최초 검증 당시 상태다.

현재 범위는 **실제 연결에 사용할 코드 구현과 로컬 대역 검증**이다. Slack App 설치·권한 부여·자격 발급·실제 API 호출/메시지 전송·공개 수신 주소 등록·배포는 하지 않았다. 원인 조사·C-04 실습·Terraform·유료 모델 호출도 하지 않았다. main `5a6298e389fae0defc1e025537e19081ab0e229b`에서 분리한 사본으로 검증하고 필요한 파일만 원본에 반영했다. 기존 Lease 계측 WIP는 포함하지 않는다.

## 구현

| 파일 | 역할 |
|---|---|
| `src/ai_ops/slack_web.py` | 고정 `https://slack.com/api/` Web API, 기본 TLS 인증, bot 자격 파일 읽기, workspace/app/bot 일치 확인, 원래 channel/thread에 회신 |
| `src/ai_ops/slack_ingress.py` | HTTPS ASGI `/slack/events`, raw body 서명 검증을 기존 adapter에 위임, durable inbox ACK만 수행 |
| `src/ai_ops/slack_runtime.py` | 기존 TaskService와 큐를 그대로 받아 접수 relay·완료 결과 회신 연결; Worker는 기존 별도 프로세스 재사용 |
| `src/ai_ops/slack_adapter.py` | 기본 fixture 유지, 명시적 web 모드만 허용, fixture/web 장부와 요청 ID 분리 |
| `config/slack-web.disabled.example.json` | `enabled:false`인 자격 **파일 참조** 예시. 실제 자격값 없음 |
| `tests/test_slack_web.py` | 외부 socket/process/model 접근을 막고 ASGI·HTTP 응답 대역으로 검증 |

수신 요청은 기존 allowlist의 workspace/app/user/channel/target과 `<@BOT> investigate target ...` 명령만 허용한다. 기존 큐/worker·공통 작업 ID·보고서 검증·런북 문서 ID/버전/해시/절 인용을 재사용한다. 실제 전송 장부는 같은 큐 root의 `slack-web/`, 대역 장부는 `slack/`이며 요청 ID namespace도 분리한다. 로컬 성공 장부가 실제 전달 성공으로 재사용되지 않는다.

`auth.test`의 team/user/bot과 `bots.info`의 app/user/bot/deleted를 대조한다. 5분이 지난 신원 확인은 재사용하지 않는다. 확인은 명시적 `prepare()` 또는 별도 relay에서만 수행하고 접수 ACK 경로에서는 네트워크·worker를 실행하지 않는다. 토큰은 소유자 전용 일반 파일(0600/0400, symlink·hardlink 거절)에서 읽고 환경변수/URL 대체 경로를 제공하지 않는다. 요청/응답 원문·자격·상위 예외를 로그에 남기지 않는다.

전송은 자동 재시도·redirect를 사용하지 않는다. 성공 응답의 channel/thread/text/ts와 관측된 user 또는 bot_id를 확인한다. 없는 user를 만들어 넣지 않는다. 429는 검증된 Retry-After 1~300초와 기존 장부의 최대3회 정책만 사용한다. timeout·5xx·잘림·알 수 없는 Slack 오류는 `needs_review`로 남기고 재시작/재호출해도 재전송하지 않는다. Slack의 `internal_error`/`fatal_error`는 일부 처리가 성공했을 가능성이 있으므로 확정 실패로 취급하지 않는다. [Slack chat.postMessage](https://docs.slack.dev/reference/methods/chat.postMessage/)

## 실행 경계와 시간 조건

- ASGI는 요청 body 수신부터 durable 접수까지 기존2.5초 ACK 예산을 적용한다. body64KiB·동시처리4개/프로세스로 제한하고, timeout·호출 취소 후에도 실제 처리 thread가 끝날 때까지 자리를 점유한다. 늦게 저장된 요청은 Slack 재전송의 동일 event ID로 중복 처리한다.
- HTTP 서버/TLS 종료·프록시의 헤더/연결 제한은 **배포 시 따로 설정/검증해야 한다**. `scope.scheme=https`만 허용하며 임의 X-Forwarded-Proto를 직접 신뢰하지 않는다. ASGI 서버의 신뢰 proxy 설정으로 우회되지 않게 실제 연결 전에 확인한다.
- outbound socket timeout3초는 새 Slack 전송기의 I/O 제한이다. OS DNS 해석 전체나 여러 I/O의 총 wall-clock 3초 보장을 뜻하지 않는다. ACK와 복구 경로에는 이 호출이 없다. 외부 지연/Slack 실제 수신은 이번에 측정하지 않았다.
- relay는 tick당 접수1개·조회 최대100개·회신 최대1개이며 cursor로 미완료 작업을 건너뛴다. 큐 backpressure가 완료된 결과의 회신을 막지 않는다. 호출자는 tick 사이 대기·종료·주기적 기존 `adapter.prune()`를 배치한다. busy loop를 만들지 않는다.
- 기존 Lease3초·시작15초·안내 후 리뷰 최소120초/시작 후210초 마감·소유 원복240초·독립 원복300초·회복 관찰120초, 실행별 승인·정확 head 실제 사람 리뷰/CI·UID/Lease/CAS/ACK·임시 자격 회수는 변경하지 않았다. Slack 접수/전송 성공은 복구 승인이나 사람 열람 확인이 아니다.

## 로컬 검사

```sh
cd /Users/jounghyeon/dev/url-shortener-ai-ops
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=ai-ops/src:ai-ops/tests \
  /Users/jounghyeon/.local/share/url-shortener-ai-ops/continuation-20261112/local-venv/bin/python \
  -m unittest test_slack_web -v
```

새28개 시험으로 다음을 확인했다: disabled 시 자격 읽기/접속0, private 파일/다른 app/만료 거절, TLS검증 설정/고정 host/path, 서명·allowlist·명령 거절, ACK 이후 기존 worker 조사, 문서 출처가 있는 회신, 중복·재시작, 대역/실제 장부 분리, response 유실 후 재전송0, 429 한도, unknown/5xx/redirect/잘린 응답, 비밀 마스킹, timeout/취소 중 inflight 자리 유지, 큐 backpressure.

전체 로컬 시험 결과와 코드/불변 파일 해시는 같은 이름 JSON 및 개인 검증 폴더에 기록한다. 기존 CI의 unittest discover가 새 시험을 자동 수집한다. 이번에는 원격 CI·commit/push·병합을 실행하지 않았다. 신규 외부 의존성은 없다.

## 추후 실제 연결 시 사용할 조립 경로 (이번에는 실행하지 않음)

기존 방식으로 읽은 **같은 BSettings·SlackPolicy·JobQueue root**를 사용한다. `TaskService(queue, settings)`와 기존 `Worker(queue, settings)`는 같은 연결/런북 설정을 공유해야 한다. fixture 전용 `load_local_workflow()`의 외부 연결 금지는 완화하지 않았으며 실제 B 설정은 기존 loader로 별도 검증한다.

```python
# 앞으로 실제 연결을 승인받은 배포의 조립 예시. 이 문서 작성 때 실행하지 않았다.
from ai_ops.tasks import TaskService
from ai_ops.slack_runtime import SlackRuntime, SlackWebConfig

runtime = SlackRuntime(TaskService(queue, settings), slack_policy,
                       SlackWebConfig.load(private_config_path))
app = runtime.app  # 검토한 TLS ASGI 서버에 연결; 여기서 조사/회신하지 않음
# 별도 relay 프로세스에서 runtime.prepare(), runtime.relay_once()를 호출한다.
# 기존 Worker가 같은 queue/settings로 조사를 처리한다.
```

1. 코드 검토·CI·main 반영. 아직 로컬 변경이며 검토된 이미지/배포 묶음에는 들어가지 않았다.
2. 실제 연결 승인 후 workspace/app/bot 및 허용 user/channel/target을 확정한다. 필요한 bot scopes 후보는 `app_mentions:read`, `chat:write`, 신원 대조용 `users:read`이며 실제 부여는 아직 하지 않았다. `bots.info`는 `users:read`를 요구한다. [Slack bots.info](https://docs.slack.dev/reference/methods/bots.info/)
3. 실제 자격 파일·공개 TLS 수신 주소·ASGI 서버/신뢰 proxy·프로세스/PVC 권한·worker와 relay의 공유 설정/보존 정책을 준비한다. Kubernetes projected Secret symlink는 그대로 읽지 않으므로 소유자 전용 일반 파일로 안전하게 제공하는 배포 방식이 필요하다. 예시의 `enabled:false`는 그대로 보존한다.
4. 명시적 신원 확인 후 Slack URL challenge·허용 요청1건·실제 채널/thread 회신·재전송/재시작·오류/회수를 확인한다. 실제 전송/사람 수신은 이번 결과로 주장하지 않는다.

현재 **불명확한 전송의 자동 원격 재조회/재조정은 지원하지 않는다**. `find_message()`는 실패로 반환하고 `needs_review`를 보존한다. 이를 위해 사용자 토큰/추가 history 권한을 요구하거나 자동 재전송하지 않는다. 수동 확인 후 장부를 안전하게 조정하는 경로는 후속 설계 대상이며, SQLite를 임의로 수정하거나 장부를 지워 재전송하지 않는다.

C04/C05 실환경 최종 검증, A5/A7 평가, 실행 저장소 전환과 AI 포트폴리오 보류는 그대로다. 원인 조사·실습은 자동 재개하지 않는다. 다음 추천: **GPT-6 SOL · High**.
