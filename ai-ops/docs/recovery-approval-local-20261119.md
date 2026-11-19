# WF-06 승인 대기·재검증 로컬 구현 · 2026-11-19

## 이유와 완료 범위

사람이 한 시간 이상 뒤에 응답해도 안전하게 이어갈 수 있어야 한다. C-04 실습의 짧은 실행 기한을 일반 운영의 사람 대기 시간으로 사용하지 않는다. 기다리는 동안 쓰기 자격이나 클러스터 잠금을 보유하지 않고, 승인 뒤 현재 대상·근거를 새로 확인한다.

main `17093a7684c885574388d81f3168033d526a6115`(WF05 PR33)에서 분기한 `codex/wf06-approval-lifecycle-20261119`의 로컬 구현이다. 기존 WF05의 서명된 Slack 계획 요청 → 사건·job·worker 증거 → 저장된 RecoveryPlan을 입력으로 사용하고 기존 JobQueue의 개인 SQLite DB에 별도 장부를 추가했다. 사건/조사 데이터를 복사하거나 새로운 실행 큐를 만들지 않는다.

**현재 초안에는 정확한 diff와 정상 이미지가 없다. 이번 승인은 `prepare_proposal` 범위, 즉 재검증 후 상세 PR 준비로 넘기는 동의다. 클러스터 실행 승인이 아니다.** `proposal_ready`는 이 준비 인계 가능 상태이며 WF05의 `RecoveryPlan.proposal_ready=false`와 별도 장부 필드다. 원래 초안·Slack 회신은 수정하지 않고 `execution_allowed=false`를 모든 결과에서 유지한다. 정확한 PR head의 사람 리뷰·CI와 실행 직전 검사는 WF07에서 계속 필요하다. 명령 실행 승인은 WF08의 완성된 실행계약을 대상으로 별도 구현한다.

## 코드와 연결

- `src/ai_ops/recovery_approvals.py`: ApprovalPolicy, CurrentRecoveryState, RecoveryApprovalLedger, slack_plan_source.
- `slack_plan_source`: 기존 Slack 요청 ID를 고정해 원래 요청자·현재 정책/설정·사건/조사 연결·자료 보관 기한을 다시 검증한다. 정책에 이름만 추가한 다른 사람은 원래 요청의 자료를 얻지 못한다.
- `load_plan(plan_hash, actor)`: 인증된 호출자에 대한 읽기 전용 내부 포트다. 사용자 문자열의 actor를 신뢰하는 HTTP API가 아니다.
- `observe(plan, deadline=...)`: 검증된 현재 관측을 읽는 내부 포트다. 대상/사건/계획 해시·Deployment UID·revision·이미지·증거 해시·원래 시각을 전달한다. 이미지 실패는 결합 근거를 요구하며, 자연 회복은 모든 등록 검증 항목의 실제 확인을 요구한다. 경보 resolved 하나로 성공 처리하지 않는다.
- 이번에는 **실제 조회 어댑터와 Slack 승인 endpoint를 설치하지 않았다.** 내부 승인 API·실제 WF05 자료 연결·합성 관측을 로컬에서 검증했다. 재검증 포트는 전달받은 기한을 지켜야 하며, 장부도 반환 시 기한/신선도를 다시 검사해 늦은 결과를 버린다. 장부 자체가 임의의 블로킹 callback을 강제 종료하지는 않는다. 실제 포트는 WF07의 기존 제한 조회 실행기에 연결한다.

## 상태와 경계

`awaiting_approval → approved → revalidating → proposal_ready`

실행 전 종료/중단은 `rejected`, `cancelled`, `expired`, `superseded`, `already_recovered`, `needs_review`다. 실제 executing/verifying/succeeded 상태와 쓰기 실행기는 이번 범위에 없다.

| 상황 | 동작 |
| --- | --- |
| 대기 후 승인 | 정확한 approval_hash·generation·현재 승인자 권한 확인 후 새 관측 필수 |
| 해시에 결합하는 내용 | 원래 초안 hash, 정책 hash, 대상/UID/revision/이미지, 생성/대기/자료 보관 종료 |
| 중복 승인 | 인증 주체와 전달 ID의 해시로 중복 제거. 같은 이벤트가 기한을 연장하지 않음 |
| 동일 전달 ID의 내용 변경 | 거절. 다른 ID로 두 번째 승인도 기한을 갱신하지 않음 |
| 승인/취소 경쟁 | SQLite 원자 처리와 generation으로 하나만 수락. 조회 중 취소는 늦은 성공보다 우선 |
| 대상/UID/revision/이미지 변경 | 새 계획 필요. 이전 승인으로 준비를 계속하지 않음 |
| 정책 변경 | 명시적 버전 전환으로 미완료 장부를 superseded 처리하고 옛 프로세스 차단 |
| 자연 회복 | 같은 대상의 검증된 건강 항목 확인 시 already_recovered, 변경 없음 |
| 조회 실패/불명확/중단 | needs_review, 자동 쓰기·무조건 재시도 없음. 예외 원문은 저장하지 않음 |
| 시계 역행/미래 관측/조회 전 자료 | 중단. 관측 시각과 승인/대기 기한을 분리 검사 |
| 재시작 | 원래 장부·절대 기한 유지. 도중 중단된 조회는 기한 후 needs_review |
| 보관 만료 | 원본 접근 차단; prune이 대상 식별/증거/승인자 해시를 지우고 해시 기반 중복 표식 유지 |

대기/조회 동안 클러스터 Lease나 worker lock을 점유하지 않는다. DB 트랜잭션은 짧은 상태 변경에만 사용하고 관측 호출 전에 해제한다. 장부 상한 10,000개·전달 영수증 상한 30,000개를 넘으면 수락하지 않는다. 해시 표식의 추가 보관/정리 정책은 실배포 전에 용량 계획에 포함한다.

## D3 / D4 결정과 활성화 조건

**D3 로컬 계약:** 준비 승인과 실행 승인을 분리하고 정확한 subject hash·승인자 allowlist·권한 재검증·재전송 ID를 결합한다. 자연어 `approve` 메시지는 승인으로 해석하지 않는다. 후속 UI는 서명된 Slack interactivity를 우선 구현안으로 관리한다. Slack 서명/시각/팀·App·채널 검증 후 principal을 도출해야 하며 이는 아직 연결되지 않았다. 실제 명령 승인은 완성된 명령계약·실행자 권한을 포함해 WF08에서 확정한다.

**D4 로컬 계약:** wait_seconds·approval_seconds·observation_seconds·revalidation_seconds를 반드시 명시한다. 운영 기본값은 없다. 테스트의 7,200/300/30/3초는 1시간 이상 기다림·시간 경계를 검증하는 합성 입력이며 운영 SLA나 기존 실습 기한 변경이 아니다. 보관 종료는 WF05 원본 자료 기한 이내, 대기/승인/관측 유효기간은 각각 독립이다. 실배포용 시간과 실행/효과 관찰 상한은 조치별로 WF07~09에서 계획·승인한다.

C-04의 Lease 조회3초·시작15초·안내후 최소120초/시작후210초 리뷰마감·소유원복240초·독립원복시작300초·관찰120초, 새 실행 승인·정확head 리뷰/CI·잠금/ACK·자격 회수는 변경하지 않았다. C04/C05 최종 실환경 검증은 미완료다.

## 검증과 다음 단계

집중35개·전체1436개(89.924초, skip0)·AST2개·diff·저장소 경계 검사가 통과했다. C단계 소스/스크립트68개가 기준 main과 같다. 실제 WF05 서명 요청·조사/worker 결과를 만드는 fixture에서 장부 연결, 1시간+대기, 만료/취소/재시작/중복, 다른 사람·정책 변경·자연 회복·UID/revision 변경·동시 승인/취소·자료 신선도·기한을 검증했다. 전체 회귀/정적 검사와 소스 해시는 `recovery-approval-local-validation-20261119.json`을 따른다.

위 로컬35개/1436개 결과는 최초 구현 시점이다. 후속 [검토 기록](recovery-approval-review-20261119.md)에서 세 경계 오류를 수정했고 최종 검증 JSON을 따르며 원격 CI/main을 진행한다. 이후 WF07에서 검증된 정상 이미지·정확 diff의 PR 준비와 실제 승인/조회 어댑터를 연결한다. 실제 Slack 승인 연결·배포·장애 실습·Terraform·유료 모델은 실행하지 않았다. 추천 모델/추론 깊이: GPT-6 SOL · High.
