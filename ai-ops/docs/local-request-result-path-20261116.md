# 공통 작업 조회·로컬 요청·결과 전달 구현

기준: 2026-11-16. Application 비교 수정은 AI PR #6으로 main `656e6b957d95988b69bc7d95cfda0279d2432532`에 반영했다. 이 문서는 새 요청·결과 경로의 구현·검토·검증 기록이며 최종 PR/CI/반영 상태는 HANDOFF를 따른다. 실환경에는 배포하지 않았다.

## 요청부터 결과까지

```text
로컬 CLI/Unix socket 요청
  → 등록 대상·입력·요청 키·회신 위치 검증
  → 기존 JobQueue에 접수하고 기존 job ID 반환
  → 별도로 실행한 기존 Worker가 수집·규칙 보고서 저장
  → 같은 task ID로 조사 상태·검증된 결과 참조 조회
  → 별도 전달 장부를 거쳐 사용자 전용 로컬 mailbox 게시
```

새 task ID는 기존 `job-...` ID 자체다. 큐와 사건 저장소가 조사 결과의 기준이며 새 장부는 요청·회신 위치와 전달 상태만 관리한다. 기존 수동·CI 등 큐 작업도 같은 조회 경로로 읽을 수 있다. 회신 위치가 등록되지 않은 기존 작업에는 알림을 보내지 않는다.

| 구성 | 구현 범위 |
|---|---|
| `tasks.py` | 엄격한 요청 계약, 중복/충돌 확인, 기존 작업/검증된 보고서 조회, 전달 장부·로컬 mailbox·만료 알림 회수 |
| `task_service.py` | 현재 OS 사용자 전용 Unix socket API·CLI, 메시지 크기/시간 제한, 고정 action 허용 목록, SIGTERM 시 자기 socket 회수 |
| 기존 큐·worker·보고서 | 수정 없이 재사용. 새 API 자체는 worker를 시작하지 않음 |
| 결과 요약 | 조사 상태, 저장 증거 기준의 진단/관측 상태, 사건·실행 참조, bundle hash·증거 ID·한계 수 |
| 미연결 | 실제 Slack·FastAPI HTTP 계층·n8n·LangGraph·Vector DB·모델·복구 실행 |

## 동작과 상태 구분

- `submit` 응답의 accepted는 접수 성공이다. 분석 완료·변경 실행 성공이 아니다.
- 중복 접수는 현재 investigation_state/result_state를 함께 반환한다. analysis_completed는 partial을 포함한 보고서 이용 가능 상태이며 완전한 조사 성공·복구 성공을 뜻하지 않는다.
- `show`는 조사·결과·전달·복구 상태를 분리한다. partial 보고서는 partial로 유지한다.
- `result`는 기존 IncidentStore의 재검증을 통과하고 큐의 상태·사건·실행·bundle hash와 일치한 규칙 보고서만 요약한다. 원문 질문·원시 관측·비밀값·로컬 파일 경로를 알림에 복사하지 않는다. 상세 보고서는 기존 `aiops show`로 조회한다.
- `deliver`는 로컬 mailbox 저장만 뜻한다. Slack 수신·사람이 읽음·PR 안내 영수증·실제 사람 리뷰·복구 성공을 뜻하지 않는다. `human_read_confirmed=false`, `execution_allowed=false`를 유지한다.
- `reconcile-delivery`는 응답 유실 후 같은 ID/내용의 기존 로컬 알림 파일을 대조한다. 없거나 다르면 확인 대기로 두고 자동 재전송하지 않는다.
- `prune`은 기존 7일 보관 기한이 지났거나 원본 큐가 purging/purged인 알림만 회수한다. 기존 사건/큐 정리는 기존 정리기가 담당한다. 중복 키의 tombstone은 유지한다.

요청 키는 source와 결합해 해시하고 원문 질문을 새 장부에 저장하지 않는다. HMAC fingerprint에는 요청·대상·회신 위치가 포함된다. 동일 키의 내용이나 회신 위치가 바뀌면 거절한다. 예약→큐 접수→작업 연결 중단 후 같은 요청 재전송으로 기존 job에 연결한다. 최종 연결을 저장하기 전에는 accepted를 반환하지 않는다. 연결 전에 프로세스가 중단되면 같은 원래 요청을 재전송해야 하며 임의 목적지로 복원하지 않는다.

알림은 안정된 ID·payload hash로 결합한다. 최종 publish 전의 확정 실패만 알림 자체를 최대 3번 재시도한다. 보내는 중 중단되거나 전달 여부가 불명확하면 sending/needs_review로 유지해 자동 중복 전송을 막는다. 분석을 다시 실행하지 않는다. 전달/조정/회수는 별도 OS 잠금으로 직렬화한다. 반복 deliver의 기존 성공도 현재 파일과 다시 대조해 삭제·변조를 성공으로 반환하지 않는다. 알림 구성 중 보관 기한/상태가 바뀌면 게시를 거절한다. 미연결 접수 예약이 7일 만료되면 재접수로 새 작업을 만들지 않는다.

## 로컬 실행

저장소 루트에서 기존 검증 venv 또는 requirements.lock을 설치한 venv를 사용한다.

```sh
PYTHONPATH=ai-ops/src .venv/bin/python ai-ops/scripts/check_task_service_local.py
PYTHONPATH=ai-ops/src:ai-ops/tests .venv/bin/python -m unittest test_tasks -v
```

첫 명령은 가짜 수집 API와 실제 Unix socket/JobQueue/Worker/IncidentStore를 연결해 접수→대기→조사→결과 조회→알림 게시→중복 재접수→재시작 대조를 수행한다. 임시 개인 저장소와 socket은 끝에 정리한다. 외부 통신·모델 키 조회는 차단한다.

서비스를 별도로 확인할 때 사용할 기본 형태는 다음과 같다. serve만으로 조사나 클라우드 조회가 실행되지는 않는다.

```sh
PYTHONPATH=ai-ops/src .venv/bin/python -m ai_ops.task_service serve \
  --store "$HOME/.local/share/url-shortener-ai-ops/task-local" \
  --bindings ai-ops/config/targets.practice.example.json \
  --runtime ai-ops/config/b-runtime.example.json \
  --metric-queries ai-ops/config/metric-queries.json
```

다른 터미널의 같은 코드/venv에서 submit한다. 반환된 task ID를 show/result/deliver/reconcile-delivery의 `--task-id`로 사용한다.

```sh
PYTHONPATH=ai-ops/src .venv/bin/python -m ai_ops.task_service submit \
  --store "$HOME/.local/share/url-shortener-ai-ops/task-local" \
  --target url-shortener-practice --request-id local-request-1 \
  --conversation-id local-thread-1 --question '현재 상태를 확인해줘'
```

예제 설정은 실제 클러스터 설정이 아니다. 외부 연결 없이 전 과정을 확인하려면 위 self-check를 사용한다. serve에는 다른 사용자의 접수 권한을 판정하는 HTTP/Slack 인증이 없으므로 공개 포트나 외부 사용자에 연결하지 않는다. 현재 로컬 사용자는 기존 큐 저장소의 OS 소유자다. 강제 SIGKILL 후 남은 socket은 자동 삭제하지 않고 소유 프로세스 종료를 확인한 뒤 수동 정리한다.

## 검증 결과와 기존 제어

최초 구현의 새24개·전체946개 시험을 통과한 뒤 코드 검토에서 회귀5개를 추가했다. 수정 전 실패6건(파일 삭제/변조 subtest 포함)을 재현하고 새29개 회귀시험을 통과했다. 검토 보완 후 전체951개 시험/skip0과 로컬 전경로 self-check도 통과했다. 최종 CI/반영 상태는 HANDOFF를 따른다. 실제 로컬 socket 요청, 기존 worker의 partial 보고서, 같은 thread의 단일 로컬 알림, 큐/서비스 재시작, 중복/충돌, 응답 유실, 확정 실패 알림만 재시도, 불명확 전송 중지, 보고서 hash 불일치, 조회 중 만료, 다른 회신 위치/링크 우회 거절, 알림 잠금, 만료 회수, 실제 SIGTERM 회수를 확인했다. 성공 알림 파일 재검증·만료 예약/알림 경쟁·기존 결과 상태 표시·socket 부모 링크 거절과 깊게 중첩된 입력도 확인했다. 기존 SQLite ResourceWarning1건은 남았다. GitHub CI에도 로컬 전경로 self-check를 추가했다.

15초 시작·실행별 새 준비 승인·정확 PR head의 실제 GitHub 사람 리뷰/CI·리뷰 최소 120초/시작 후 210초 마감·소유 원복 목표 240초/독립 원복 시작 300초·관찰 120초와 잠금/ACK/대상 검증을 유지한다. 관련 제어 9파일, 기존 JobQueue/Worker/의존성은 main과 byte 동일하다. 새 API에는 approve/recover/work/model/cancel action이 없다. 기존 취소·실제 원복은 기존 제어 경로로만 처리한다. 새 로컬 알림을 기존 `codex-host-bridge` 영수증으로 변환하지 않는다.

## 남은 연결

1. 검토한 로컬 구현의 PR/CI/main 반영 상태는 HANDOFF를 따른다. 반영해도 실환경 실행이나 외부 권한을 자동 연결하지 않는다.
2. Slack 요청자·대상 권한·서명·이벤트 결합과 회신 정책을 정한 뒤 외부 어댑터를 구현한다. FastAPI는 그 접수 계층 후보이며 현재 의존성에는 추가하지 않았다.
3. 실제 Slack 전달 상태를 로컬 mailbox와 별도 transport로 검증한다. 현재 파일 저장을 Slack 전달 성공으로 재사용하지 않는다.
4. 이후 런북 검색과 복구 안내 연결을 별도 단계로 진행한다. 실습은 새 준비·실행 승인과 새 PR의 실제 리뷰가 필요하다.

C04/C05 최종·A5/A7 미측정·실제 실행 저장소 전환의 기존 미완료 판정은 변경하지 않는다. 추천: GPT-6 SOL · High.
