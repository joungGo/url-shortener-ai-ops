# Slack 연결용 코드 검토 · 2026-11-17

대상은 main `5a6298e389fae0defc1e025537e19081ab0e229b` 위의 Slack 수신/전송/runtime·기존 adapter의 명시적 mode 추가·disabled 설정·시험·관련 문서다. 원본의 Lease 단계 계측, 실환경 기록, AGENTS/HANDOFF 등 다른 미커밋 변경은 PR 범위에서 제외했다. 실제 Slack 연결·자격 발급·원인 조사·장애 실습·배포는 수행하지 않는다.

## 검토 결과와 수정

접수 서명/allowlist, bot/team/app 신원, private 자격 파일, endpoint/TLS, 저장 전/전송 전 인가 확인, fixture/web 장부 분리, 중복/재시작과 불명확한 전송의 재전송 금지, ASGI 시간/동시처리 경계를 검토했다. 원격 재조회 미지원 및 socket timeout과 총 wall-clock 차이는 기존 명시된 제한으로 유지한다.

**종료 대기 취소가 실행 중인 접수 thread의 추적을 먼저 해제하는 결함을 수정했다.** lifespan shutdown에서 일반 `gather`를 취소하면 `to_thread`의 asyncio task도 취소되지만, 실제 thread는 접수를 계속할 수 있었다. 완료 callback이 동시처리 수를 먼저 줄여 '실제 처리가 끝날 때까지 자리 유지' 조건을 깨는 것을 회귀 시험으로 재현했다(수정 전 `0 != 1` 실패).

종료 대기에서 각 접수 task를 `asyncio.shield`로 보호해 shutdown 대기 취소가 실제 접수 추적을 취소하지 않도록 했다. 기존 HTTP 호출자 취소/ACK timeout과 동일하게 실제 완료 후 callback이 자리를 반환한다. 강제 프로세스 종료의 durable recovery를 보장한다고 주장하지 않는다. ACK2.5초와 MAX_INFLIGHT4는 변경하지 않았다.

수정 후 새 회귀를 포함한 집중 시험29개가 통과했다. 전체 회귀·저장소 경계·구문/disabled 설정·불변 소스 검사를 수행하고 그 결과를 아래 기록한다. 실제 Slack 응답 대신 대역을 사용했고 신규 시험에서 외부 socket/process/model 접근을 차단했다. 별도 하위 에이전트 검토는 사용하지 않았다.

전체 **1,157개 / 53.992초 / skip0**, 집중29개 / 1.695초 / skip0 통과. 기존 SQLite ResourceWarning은 남지만 실패는 없다. 저장소 경계·구문/설정·불변56소스 검사도 통과했다.

## 유지한 경계

- 기존 큐/worker·공통 TaskService·런북과 C 제어 관련56소스는 기준 main과 byte 동일하다.
- Lease3초·시작15초·리뷰 최소120초/마감210초·소유 원복240초·독립 원복300초·회복 관찰120초, 실행별 승인·정확 head 실제 사람 리뷰/CI·UID/Lease/CAS/ACK·자격 회수를 유지한다.
- Slack 성공 응답은 사람 승인/열람이나 복구 완료를 뜻하지 않는다. `enabled:false` 기본값을 유지한다.
- 원격 CI는 기존 소스/계약·이미지 build/smoke·Terraform backend 없는 validate 범위다. 실제 연결/실습 workflow는 기동하지 않는다. 실행 저장소 전환과 C04/C05 완료 상태도 바꾸지 않는다.

## 다음 단계와 근거

CI가 같은 PR head에서 통과한 후 기존 저장소 정책으로 main에 반영한다. 실제 Slack 준비/연결은 별도 승인 범위로 남긴다. 최초 로컬 구현의 원본 검증 JSON은 당시 파일 해시/시험 수를 보존하며, 이번 수정된 ingress/test 해시와 PR/CI/main 결과는 후속 통합 기록으로 구분한다.

개인 보존 폴더: `/Users/jounghyeon/.local/share/url-shortener-ai-ops/slack-web-integration-20261117/` (검토 전8파일·WIP/index 근거·수정 전 실패·수정 후 시험/CI·병합 증거).
