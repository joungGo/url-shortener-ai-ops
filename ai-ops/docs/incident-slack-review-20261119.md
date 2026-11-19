# WF-04 코드 검토·CI 반영 기록

**최종 상태: PR32 일반 squash 병합·main e3a4efa·PR7종/main CI 성공.** 아래 대기 표기는 검토 당시 이력이다. 완료 후 메타데이터는 로컬 갱신.

2026-11-19 · 기준 main ca63fcf · `codex/wf04-slack-events-20261119`.

review-bugbot 스킬의 직접 검토 경로로 변경 코드·서명/인가·두 SQLite 장부·worker·retention·회신 호출 경로를 검토했다. 전용 Bugbot 도구는 없으므로 자동 Bugbot 실행으로 표현하지 않는다. 인프라/Slack/장애 실습은 실행하지 않는다.

| Severity | Location (file:line) | Finding |
| --- | --- | --- |
| P1 | ai-ops/src/ai_ops/slack_adapter.py:94 | 새 Slack 정책 commit 뒤 큐 authority 갱신 전에 종료되면 이전 권한의 queued 작업을 worker가 수집할 수 있었다. 이전 authority를 먼저 영속 회수하고 새 정책 노출·authority 활성화까지 같은 처리 잠금을 유지하도록 수정. 합성 중단 시험에서 수정 전 실패·수정 후 차단 확인 |
| P2 | ai-ops/src/ai_ops/slack_adapter.py:438 | 사건 연결 비활성화 뒤 expire 호출이 빠져 나중에 생성된 Slack 사건 복사본이 사건 상세의7일 기한을 넘겨 남았다. 기존 장부의 maintenance를 새 명령 활성 상태와 분리하여 만료·복사본 정리를 계속 수행하도록 수정. 수정 전 잔존·수정 후 삭제 확인 |

원래 WF-04 신규36개에 회귀2개를 추가했다. 기존125개 Slack 시험 통과. 동일 설정 재시작·외부 App 설치 거절·outbox/claim/수집 전 차단도 계속 통과했다. 소유권·diff·AST와 전체 회귀 결과는 아래 검증 JSON으로 확정한다.

[최종 소스 검증](incident-slack-review-validation-20261119.json), [최초 구현 기록](incident-slack-local-20261119.md). 최초 구현 검증 JSON의 해시는 최초 로컬 완료 시점이며 이번 수정 소스 해시로 사용하지 않는다.

기존3/15/120/210/240/300/120초·실행별 승인·정확head 사람리뷰/CI·잠금/ACK·원복/회수 유지. 실제배포·DB 전환·Slack전송·장애·Terraform·모델 없음. 원격CI/main 결과는 완료 후 최신 HANDOFF/작업표로 확정한다.

최종 로컬 결과: 전체1372개(84.572초, skip0), Slack125개·변경Python AST8개·diff·저장소 경계 통과. 기존 안전 실행기5개 해시 일치. 수정 후 남은 재현 결함0건. 원격 CI/main은 아직 대기.

## 2026-11-19 WF-04 코드 검토·CI/main 반영 완료

AI [PR #32](https://github.com/joungGo/url-shortener-ai-ops/pull/32)의 head `2a1aefcaf05f97fd006b4dfc7f12fa133079ce1e`를 직접 검토했다. 정책 전환 중단 뒤 이전 큐 권한 잔존(P1), 사건 연결 비활성화 뒤 사건 복사본 만료 지연(P2)을 로컬 재현·수정했다. Slack125개·전체1372개(84.572초/skip0)·AST8개·diff·소유 경계 통과. 수정 후 남은 재현 결함0건.

[PR CI44338728798](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44338728798)의 필수 C03 gate 포함7종과 pushCI44338554478 성공 후 보호 변경/우회 없이 일반 squash 병합했다. main `e3a4efab193bac143d598044e5d011329263d484`, [main CI44339438355](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44339438355) 성공. main의 PR전용 gate skip은 설계대로이며 실제 PR gate는 통과했다. 검토 소스와 병합 소스의 해시/전체 tree 일치 확인.

코드/기존 검증 문서는 main 반영 완료. 완료 후 이 인계·작업표·최종 검증 메타데이터는 로컬에 갱신했다. AI 작업트리 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf04-slack-events-20261119`, 상세 `ai-ops/docs/incident-slack-review-20261119.md`, `incident-slack-review-validation-20261119.json`. 원래 작업트리의 다른 변경을 보존했으며 강제 checkout/pull하지 않았다.

기존 시간·승인·원복·C04/C05미완료 유지. 인프라 설치·실제 배포/DB전환·Slack전송·장애/모델/Terraform 실행 없음. 다음은 WF-05 복구 계획 요청·지원 조치 계약의 로컬 구현이며, WF-10 실제 연결/전환은 별도 계획·준비 후 진행한다. 추천 GPT-6 SOL · High.
