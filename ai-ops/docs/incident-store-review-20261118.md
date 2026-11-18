# WF-02 기능·회귀 코드 검토

2026-11-18 KST · 기준main `36a55fdb59ec63c34cd7bcb339cada39ec0b515b` · branch `codex/incident-workflow-plan-20261118`.

review-bugbot 절차의 직접 검토를 수행했다(전용 Bugbot subagent 도구 없음). 사건 스키마·큐/worker·보관·migration·32개 집중시험과 관련 문서를 확인했다. 사용자 요청의 main 반영을 위해 아래3개를 로컬 재현·수정했다. 별도 사람 승인이나 실환경 완료를 의미하지 않는다.

| Severity | Location (file:line) | Finding |
| --- | --- | --- |
| P2 · 수정완료 | ai-ops/src/ai_ops/event_store.py:288 | allowed_targets에 문자열이 들어오면 부분 문자열 membership으로 다른 별칭을 허용할 수 있었다. 문자열/사전/혼합형을 거절하고 문자열 컬렉션의 정확 일치만 허용한다. |
| P2 · 수정완료 | ai-ops/src/ai_ops/event_store.py:314 | 일부 큐 슬롯만 비면 중간 QueueFull로 이미 전달한 예약까지 전부 rollback됐다. 용량에 맞춰 전달한 앞부분만 원자 commit하고 나머지는 원래 중복키로 pending 유지한다. 처음부터 가득 찬 경우 기존 QueueFull을 유지한다. |
| P2 · 수정완료 | ai-ops/src/ai_ops/event_store.py:345 | 이미 전환한 장부의 재시도에서 새 resolved/endsAt을 무시하고 만료 사건도 성공으로 반환했다. 불변 사건 검사와 단방향 해제 병합·만료 검사를 다시 수행하며 기존 job/시도는 보존한다. |

수정 전 신규3개 시험의 실패를 확인했다. 수정 후 사건32개/사건+큐48개/전체1325개 시험이 통과했다. 코드4개 AST·git diff --check·저장소 경계 검사도 통과했다. [검증 JSON](incident-store-local-validation-20261118.json)에 소스/시험 로그 SHA256을 기록했다. 추가 미해결 발견0건이며 실제 인증/Slack/배포 완료를 주장하지 않는다.

기존3/15/120/210/240/300/120초·실행별 승인·정확head 사람리뷰·CI·잠금·ACK·원복·자격 회수 경로는 수정하지 않았다. Lease/worker 시간 한도·대기100개·7일보관도 유지한다. 변경은 WF-02 코어와 해당 큐 검사뿐이다. HTTP/Slack·정기expire·실물장부 전환·C04/C05는 다음 단계다.

원격 반영은 이 브랜치 PR의 정확head와 전체 CI 성공을 확인한 뒤 일반 squash 병합한다. AI private 저장소의 보호 설정 조회는 요금제 제한403이며 보호 약화/관리자 우회는 사용하지 않는다. 최종 PR/main 상태는 병합 기록과 HANDOFF 최신 기록을 따른다. 원격CI는 로컬 계약·이미지 검사만 수행하고 실제 배포 workflow를 기동하지 않는다.
