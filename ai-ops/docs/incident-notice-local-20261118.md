# WF-03 경보 공지·사건 참조 로컬 구현

후속 [검토·CI 기록](incident-notice-review-20261118.md): fallback·대체 설정·필수 CI를 보완했으며 최신 플랫폼 소스 해시는 검토 검증 JSON을 따른다. 아래는 최초 로컬 구현 기록이다.

2026-11-18 KST. AI 기준main9c9f3fc / 플랫폼 기준main01ca82b. 양쪽 브랜치 `codex/wf03-alert-reference-20261118`.

[전체 계획](incident-workflow-and-improvement-plan-20261118.md) · [작업표](incident-workflow-implementation-tracker.md) · [사건 계약](incident-identity-channel-policy-contract-20261118.md)

## 변경 이유와 구현 범위

경보 채널의 사실 공지와 조사 채널 #ai-ops를 사건 참조로 연결한다. Alertmanager가 새 서버 ID나 Slack message ts를 조회해야 공지할 수 있는 구조를 피하고, 기존 수신 경로와 별도로 사건을 접수한다. 두 경로 도착 순서는 보장하지 않는다.

플랫폼 `cluster-addons/prometheus/wf03/notice.tmpl`·`prepare.py`는 **검토용 values 복사본만** 만든다. 기존 values.yaml/Application은 변경하지 않았다. AI `event_notices.py`는 WF-02 저장소를 인증 수신기에 연결하고 `event_store.py`는 pending/registered/expired 참조 조회를 추가한다. `alert_receiver.py`는 명시 설정일 때만 새 경로를 선택한다.

## 공지 템플릿과 정확한 사건 참조

공지에 경보명·심각도·제한된 대상 label5개·발생/해제 시각·`al1/source_alias/fingerprint/startsAt`·조사 채널을 표시한다. 원인 확정이나 복구 성공으로 표현하지 않는다. 임의 summary/description·generatorURL·원문 로그는 표시하지 않고 label도 허용 문자/길이 이외는 `미확인`으로 대체한다. Slack mrkdwn/link_names를 꺼 임의 멘션·링크 해석을 막는다. 알려진 label의 문자열이 항상 비밀이 아니라고 보장하는 DLP는 아니므로 배포 시 라벨 소유/사용 범위 검토는 필요하다.

`StartsAt.UTC.Format`으로9자리 나노초를 보존한다. JSON payload의 +09:00/Z 표기는 같은 canonical evt로 연결된다. 새로운 startsAt은 별도 발생이다. 동일 공지의 firing/resolved는 같은 발생 참조를 유지한다. 잘못된 fingerprint는 참조 생성 불가로 표시한다.

기존 group_by=['...']를 보존한다. 예상 밖 다중 경보 입력에도 공지는 최대3개만 표시하고 나머지 생략을 명시한다. 사건 접수의 최대32개/64KiB와는 별도 한도다. 최악 label 길이를 포함해 텍스트3000자 미만을 검증했다.

기존 Incoming Webhook Secret 파일 경로·목적지·send_resolved는 보존하고 채널명/ID를 덮어쓰지 않는다. 조사 채널명은 표시용일 뿐 요청 인가가 아니다. Slack ts/permalink 조회나 history scope 추가는 없다. **사건 지정 Slack 명령은 아직 구현하지 않았으므로 현재 봇이 새 참조를 조사 명령으로 처리한다고 주장하지 않는다.**

## 라우팅 후보와 전환 차이

원래 Watchdog 분기·heartbeat 수신기·기존 억제 규칙·group/repeat 간격·인증/CA Secret을 보존한다. 후보에서는 Watchdog 외 경보를 아래 두 receiver로 각각 전달한다.

1. slack-notifications, continue=true: 기존 직접 공지.
2. ai-events: 인증된 사건 메타데이터 접수 `/v1/events`.

두 전달은 서로의 응답을 기다려 성공으로 판정하는 연결이 아니다. 기존 ai_ops=enabled→ai-investigation 자동 조사 분기를 **후보에서만** 교체한다. 이에 따라 일반 경보도 사건을 등록할 수 있지만, 기본 manual 정책에서는 요청 전 작업0개다. 이것은 실물 정책 변경이 아니며 기존 운영 설정에는 적용하지 않았다. 전환은 기존 큐/장부 보존·verified migration·worker 종료/교체 계획과 함께 별도 승인해야 한다.

새 후보는 검토한 기존3분기와 기존TLS/인증 설정을 확인하고, 임의 분기/보안 옵션 변경이나 중복 적용을 거절한다. CLI는 지정 output을 배타적으로 생성하며 Helm/kubectl/Slack을 호출하지 않는다.

## 인증된 사건 접수와 상태 장부

기존 TLS1.2 이상·Bearer 파일/권한·상수시간 비교·토큰 갱신·연결당2초·64KiB·분당60회·503/Retry-After 규칙을 재사용한다. 새 `EventInbox`는 서버 설정의 source_alias를 고정하며 HTTP 본문/헤더가 다른 발신원/클러스터를 고를 수 없다. 인증 뒤 `EventStore.accept`를 호출해 commit 완료 후에만202를 반환한다. 사건 접수는 worker 실행을 기다리지 않고 outbox를 drain하지 않는다.

CLI는 기존 --cluster-id에 **--event-source-alias와 --event-source-id를 둘 다 명시한 경우** 사건 모드가 된다. 둘 다 없으면 기존 `/v1/alerts`다. 한 가지만 지정하면 중지한다. 새 모드는 `/v1/events`만 허용하고 옛 경로를 열어 자동조사를 함께 실행하지 않는다. 현재 배포 args는 바꾸지 않았다.

수신 결과에는 event_ids·notification=unknown·investigation_started=false·execution_allowed=false를 담는다. API202는 사건 접수일 뿐 Slack 전달·사용자 읽음·조사 완료·실행 승인 증거가 아니다. 실제 자동 예외/결과 전달은 WF-02의 별도 준비조건을 따른다.

`reference_state`는 기존 사건 장부를 조회한다. 별도 통지 완료 장부를 만들거나 이미 보냈다고 기록하지 않는다.

| 순서/상태 | 내부 조회 결과 | 동작 |
| --- | --- | --- |
| 공지가 먼저 도착 | pending / notification unknown | 새 사건/작업 생성0, 접수 성공으로 표시하지 않음 |
| 사건 접수가 먼저 도착 | registered / notification unknown | 공지 전달은 미확정, 기존 evt로만 연결 |
| resolved 도착 | registered / alert_status resolved | 발생 참조 유지, 원인/복구 성공/승인과 구분 |
| 발생7일 경과 | expired | 옛 사건 재생성/조사 성공 위장 없음 |
| 다른 source/잘못된 URL | 거절 | URL 조회·추측 바인딩 없음 |

이 조회는 **내부 API**다. WF-04의 Slack 서명·workspace/channel/user/target 인가 후에만 사용자에게 반환해야 한다. 현재 이 함수를 공개하는 Slack/HTTP 조회 endpoint는 없다.

## 로컬 검증과 한계

- 플랫폼8개: 실제 Alertmanager config 검사/라우팅·template render, Watchdog/일반/기존 조사 허용 경보 예시 분기, 원본 불변·TLS 변경 거절, 나노초/offset/resolved, 위험 annotation/label 차단, 다중 표시 상한.
- AI 집중50개: 새9개와 기존 사건/TLS/routing 시험. 합성토큰·임시SQLite·loopback TLS만 사용했다. 인증 실패·토큰 교체·경로 격리·잘림·rate limit·저장/포화503, 두 경로 도착 순서·중복·만료·해제·동일 template→evt 매핑 포함.
- 전체 AI 회귀 **1,334개**가 81.827초에 통과했다. 이 실행의 새 교차 검증을 포함해 skip은 0개다. AST 6개·양쪽 diff·AI 저장소 경계 검사와 실제 후보의 inline template 렌더도 통과했다. 결과/소스·도구 해시는 [검증 JSON](incident-notice-local-validation-20261118.json)에 기록했다. 플랫폼 actual renderer와 AI 연결 검사는 환경변수로 정확한 소스를 지정해야 실행된다. 단순 테스트 skip을 완료로 세지 않는다.
- 기존 SQLite ResourceWarning은 전체 시험에서 관찰되는 별도 한계다. 운영 장애로 해석하지 않는다.

실제 Slack 서버의 실패/재시도 SLA, Alertmanager 프로세스의 실제 외부 전송, live IAM/클러스터·실물 채널 ID·실제 수신 Secret은 검증하지 않았다. 로컬 검증은 분리된 라우팅 설정과 저장 실패 응답이 공지 전달 완료로 오판되지 않는 경계까지다. 실제 전송 성공으로 표현하지 않는다.

## 재현 도구와 출처

플랫폼 고정 Helm chart83.7.0의 기본 Alertmanager는v0.32.0이다. [공식 chart values](https://github.com/prometheus-community/helm-charts/blob/kube-prometheus-stack-83.7.0/charts/kube-prometheus-stack/values.yaml)와 [공식 Alertmanager 릴리스](https://github.com/prometheus/alertmanager/releases/tag/v0.32.0)를 대조했다. 해당 darwin-arm64 압축파일의 공식sha256sums를 검증하고 amtool만 `/tmp/wf03-tools/amtool`에 준비했다. Docker daemon은 사용할 수 없어 native 도구를 썼으며 인프라/서비스를 설치하지 않았다.

```sh
# 기존 검증 venv를 사용한다. 두 경로는 각 로컬 작업트리로 지정한다.
export WF03_PLATFORM_ROOT=/Users/jounghyeon/dev/Url-Shortener-EKS-Platform/.worktrees/wf03-alert-reference-20261118
export WF03_AMTOOL=/tmp/wf03-tools/amtool
# 플랫폼 작업트리
PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m unittest discover -s cluster-addons/prometheus/wf03 -p 'test_*.py' -v
# AI 작업트리 (위 두 환경변수 유지)
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=ai-ops/src <venv>/bin/python -m unittest discover -s ai-ops/tests -p 'test_*.py' -q
```

## 다음 작업

양쪽 WF-03 코드 검토·CI/main 반영을 먼저 진행한다. 플랫폼 템플릿이 반영된 commit/hash를 AI 연결 검증에 고정하고 이후 WF-04에서 인증된 Slack 사건 지정 요청·동일thread 결과·보관 정기 호출·설정 배포 경로를 연결한다. 기존 integration/platform-source.json의 실행 소유/고정 revision은 이번에 임의 변경하지 않았다. 현재 교차 검증은 명시한 로컬 플랫폼 작업트리 해시 기준이다.

실제 연결은 source_alias/source_id/cluster_id·경보 Webhook 실제 채널·조사 채널/사용자·현재 state/장부·전환/원복 diff를 다시 확인해야 한다. 원본 토큰/기존 큐/DB를 이번 시험에 사용하지 않았다.

기존3/15/120/210/240/300/120초·실행별승인·정확head리뷰/CI·잠금/ACK·원복/회수 유지. 실제 배포·Slack 전송·장애 실습·Terraform·유료모델 호출·CI/main 반영 없음. C04/C05 미완료 유지. 추천 GPT-6 SOL · High.
