# 요청·조사·결과 흐름 로컬 완료 범위

2026-11-17 사용자가 과거 Lease 원인 조사를 보류하고 **‘요청 → 기존 큐·worker 조사 → 출처가 있는 결과 회신’의 코드·연결 설정·로컬 통합 검증**을 우선 완료하도록 선택했다. 실제 Slack 연결과 C-04 장애 실습은 별도 단계다. 이 문서는 그 범위의 완료 판정이며 AI 전체 운영 검증이나 포트폴리오 재개 선언이 아니다.

## 코드 검토 · PR 범위의 로컬 재검증

검토에서 통합 명령이 잘못된 Slack 정책을 받으면 Pydantic 예외의 입력값이 stderr에 노출되는 문제를 가짜 비밀 canary로 재현했다. CLI 실패는 고정 코드 `local_workflow_verification_failed`와 종료 코드2만 반환하도록 수정했다. 직접 호출하는 단위 시험의 진단은 유지하며, 성공/실패를 바꾸거나 예외를 성공으로 처리하지 않는다.

이번 PR은 요청·결과 흐름 10파일만 대상으로 한다. 별도 미커밋 Lease 단계 계측과 원인 조사 자료는 포함하지 않는다. 검토된 PR 트리의 전체 **1,128개/50.936초/skip0**, 집중30개가 통과했다. 앞선 1,134개는 당시 작업 사본에 포함된 별도 Lease 계측 시험7개를 포함한 수치이고, 이번에는 이를 제외한 뒤 검토 회귀1개를 추가했다. 승인/원복 C모듈45개와 기존 처리 흐름7개는 기준 main83d411b와 byte 동일하다. 원격 CI/병합의 최신 상태는 HANDOFF와 후속 integration 기록을 따른다.

## 구성과 실행

기존 TaskService, JobQueue, Worker, IncidentStore, SlackAdapter와 RunbookPolicy를 그대로 재사용한다. `request-workflow.local.example.json` 하나에서 대상·수집 설정·지표·런북 허용 정책·Slack 허용 정책을 연결한다. `workflow_config.py`는 동일 BSettings/config hash를 조합해 검증하며 서버·worker·큐를 시작하지 않는다. 외부 주소/CA/토큰 파일 참조, 모델 활성화, production 대상, live 모드, 알 수 없는 필드, 경로 이탈·symlink·중복 키·과대 설정을 거절한다. 예제에는 실제 자격을 넣지 않는다.

저장소 루트에서 기존 검증 venv 또는 `ai-ops/requirements.lock`을 설치한 Python 3.11+를 사용한다.

```sh
PYTHONPATH=ai-ops/src .venv/bin/python -m ai_ops.workflow_config \
  --config ai-ops/config/request-workflow.local.example.json

PYTHONPATH=ai-ops/src .venv/bin/python ai-ops/scripts/check_request_workflow_local.py
```

두 번째 명령은 테스트 모듈이 포함된 소스 체크아웃의 **로컬 통합 검증 진입점**이다. 상시 서비스나 배포 명령이 아니다. 합성 서명·수집 API·회신 transport를 사용하고, 실제 HTTP/Unix socket·SQLite 큐·기존 worker·보고서/런북 검증은 교체하지 않는다. 인증키/모델키를 읽지 않으며 연결 목적지는 이 명령이 만든 두 로컬 서버로 제한한다. Python `-O`로 검증을 생략한 실행은 거절한다. 임시 서버·스레드·소켓·저장소는 끝에 회수한다.

## 한 요청의 검증 흐름

1. 합성 Slack app_mention의 raw bytes 서명·사용자/채널/대상 검증 후 HTTP ACK를 반환한다. ACK 시점의 큐는 아직 비어 있어 접수와 조사를 구분한다.
2. 기존 inbox drain이 기존 JobQueue에 한 번 접수한다. 공통 Unix API에서 같은 task ID의 pending 상태를 조회한다.
3. 동일 설정 인스턴스의 기존 Worker가 한 번 수집·규칙 조사한다. fixture의 불완전 관측은 `partial`로 유지하며 정상·복구 성공으로 바꾸지 않는다.
4. 같은 task ID로 보고서를 재검증하고 문서 ID·버전·원문 SHA256·절 위치/절 SHA256을 실제 런북 사본과 다시 대조한다.
5. 서명된 원래 channel/thread에 가짜 Slack transport로 출처 요약을 한 번 회신한다. 같은 결과의 로컬 mailbox도 비교하되 두 전달 상태를 혼동하지 않는다.
6. 장부를 다시 열고 같은 서명 요청을 재접수한다. 기존 task/result를 재사용하고 조사/회신 횟수가 각각 1회인지 확인한다.
7. 잘못된 서명·허용되지 않은 사용자·approve 명령과 공통 API의 approve/recover/cancel/sync 요청을 거절한다. 큐/회신이 늘지 않고 `execution_allowed=false`, `human_read_confirmed=false`임을 확인한다.

## 완료 판정과 유지한 범위

| 항목 | 판정 |
| --- | --- |
| 로컬 요청·공통 상태/결과 조회 | 기존 구현 재사용, 통합 검증 완료 |
| 인증된 Slack 입력·원래 스레드 결과 | 합성 서명·fixture transport 범위 완료 |
| 기존 큐·worker·보고서 | 같은 설정·같은 task ID·조사 1회 확인 |
| 대상별 런북 허용·인용 | 문서/절 해시 재대조와 회신 연결 완료 |
| 재시작·중복·거절·비밀 제외 | 기존 제어를 통한 통합 검증 완료 |
| 로컬 연결 설정·재현 명령 | 새 설정 검사기와 단일 통합 진입점 제공 |
| 과거 Lease 원인 조사/추가 계측 연결 | 사용자 선택으로 보류, 해결됐다고 표시하지 않음 |
| 실제 Slack transport·앱 자격·공개 수신기 | 미연결. 이 로컬 완료로 실제 사용 준비 완료를 뜻하지 않음 |
| 새 원격 CI·main 반영 | 이번 변경은 로컬. CI 정의에는 설정/통합 검사 추가, 원격 실행·커밋·푸시는 미실행 |
| C-04/C-05 실환경 최종 검증 | 별도 준비/승인/사람 리뷰/회복 관찰이 남음 |
| A5/A7 비교 평가·실행 저장소 전체 전환·AI 포트폴리오 | 기존 미완료/보류 범위를 유지 |

시작15초·안내 후 리뷰최소120초·시작 후 리뷰마감210초·소유 원복240초·독립 원복300초·회복관찰120초, 실행별 승인·정확 head 실제 사람 리뷰/CI·UID/Lease/CAS/ACK·회수 조건은 변경하지 않았다. 승인/복구 관련 C 모듈44개와 기존 큐·worker·공통 작업·Slack·런북 코드는 작업 시작 시점과 byte 동일하다. 직전 Lease 계측 작업도 확장하지 않았다.

전체 회귀 중 기존 시험의 ZIP 생성 시각이 listing/download 사이에 달라져 해시가 깨지는 실패를 발견했다. 두 시각으로 재현한 뒤 `test_lab_ci_history.py`의 **합성 ZIP 메타데이터만** 고정했다. 실제 artifact digest 검증이나 승인 규칙은 변경하지 않았다. 최초 실패/수정 전 재현/최종 시험은 개인 증거에 구분해 보존한다.

최종 시험 수치·해시·통합 JSON은 같은 이름의 JSON 및 개인 `~/.local/share/url-shortener-ai-ops/local-workflow-completion-20261117/`을 따른다. 기존 SQLite ResourceWarning은 이 변경으로 해결했다고 주장하지 않는다.

다음 작업은 원인 조사 반복이 아니라 **이 로컬 변경 묶음의 코드 검토·원격 CI·정상 반영**이다. 실제 연결은 그 뒤 별도 범위로 선택한다. 추천 모델/추론 깊이: **GPT-6 SOL · High**.
