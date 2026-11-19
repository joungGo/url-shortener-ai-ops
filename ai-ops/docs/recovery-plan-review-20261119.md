# WF-05 코드 검토·CI 반영 기록

2026-11-19 · 기반 main e3a4efab193bac143d598044e5d011329263d484 · 브랜치 codex/wf05-recovery-plans-20261119.

review-bugbot 스킬의 직접 검토 절차를 사용했다. 전용 Bugbot 도구가 없어 자동 에이전트 검토로 표시하지 않는다. 신규 RecoveryPlan/registry와 Slack 접수·사건/job 인가·TaskService 결과·회신/정리 호출 경로, 이전 전달 형식과 기존 C단계 파일을 대조했다.

| Severity | Location (file:line) | Finding |
| --- | --- | --- |
| P2 | ai-ops/src/ai_ops/recovery_plans.py:178 | 사건 시각을 datetime으로 변환해 나노초를 잃으면 사건 직전 관측이 지원 근거로 통과한다. UTC 9자리 canonical 문자열 보관·비교와 계약 재검증으로 수정했다. |

재현: 관측 `2026-10-16T01:00:00Z`, 사건 `2026-10-16T01:00:00.000000001Z`를 합성 로컬 장부에 연결했을 때 수정 전 supported/image_failure_bound였다. 수정 후 insufficient_evidence/observation_outside_event이며 정확한 사건 시각이 계획 해시에 포함된다. 해시를 재계산한 잘못된 supported 계획도 거절한다. 사건의 기존 ID·보관 계산·어떠한 시간 예산도 바꾸지 않는다.

회귀: `test_review_nanosecond_event_boundary_rejects_earlier_observation`, `test_review_rehashed_plan_cannot_claim_pre_event_evidence_supported`. 최초 구현 기록의 1399개와 소스 해시는 이전 시점으로 보존하며 최종 결과는 `recovery-plan-review-validation-20261119.json`을 따른다.

현재 범위는 실행 불가능한 복구 계획이다. supported는 저장 근거와 템플릿 일치이며 실제 PR 제안/실행 준비가 아니다. 정상 이미지·현재 UID/revision·정확 diff·새 승인·CI/원복 조건은 후속 단계다. 명령 재시작은 미구현 후보다. 실제 배포·Slack·인프라·장애 실습·유료 모델 없음.

원격 CI와 main 결과는 아직 대기하며, 정확한 head의 검사 완료 뒤 보호 변경·관리자 우회 없이 일반 squash 병합한다. 새 보호 요건이 발견되면 기존 승인으로 임의 우회하지 않는다. Lease GET3초·시작15초·안내 최소120초/리뷰210초·소유원복240초/독립원복300초·관찰120초와 실행별 승인·정확 head 리뷰/CI·잠금/ACK·회수를 유지한다.

로컬 최종 검증: WF05 집중29개 통과. 전체1401개(82.817초) 중1400개 통과·임시 amtool 부재로1개 미실행 오류. 공식0.32.0 darwin-arm64 아카이브 해시 검증 후 고정 플랫폼 source로 notice9개를 실행해 누락 사례까지 통과했다. 소스 변경 없이 전체1401개 고유 사례 검증을 완료했으며 skip0이다. AST4개·diff·저장소 경계 통과, C단계47개 소스 기준main과 동일. 원격 CI는 별도로 정확head에서 확인한다.


원격 첫 CI44446235573의 계약·Lease·notice·Terraform 검사는 통과했으나 이미지가 기존 OpenSSL 검증에서 중단됐다. APKINDEX 실물은 libssl3/libcrypto3 3.5.9-r1이고 설치가 간접 업그레이드를 허용하면서 검사만r0를 요구했다. 공식 양 아키텍처 목록 확인 뒤 설치/검사를r1로 함께 고정했다. Docker daemon이 로컬에서 연결되지 않아 실제 이미지 재빌드는 새 head CI로 확인한다. WF05 Python 검증 소스는 이 보완에서 바꾸지 않았다. ECR 검사/배포 없음.
