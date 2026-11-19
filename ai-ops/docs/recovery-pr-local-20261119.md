# WF-07 이미지 복구 PR 연결 · 2026-11-19

## 구현 이유와 범위

WF05의 사건/조사에서 만든 계획과 WF06의 `prepare_proposal` 동의가 기존 이미지 PR 검증 경로와 분리돼 있었다. 이번에는 같은 plan hash를 기준으로 검증된 정상 이미지·정확 diff·PR head·리뷰/CI·잠금 검증 결과를 이어 기록한다. 단순한 복구 요청이나 준비 동의를 실행 승인으로 바꾸지 않는다.

기준 AI main `59c33a9d4e4dfd8f524161ce2cc2d590e1ca1133`(WF06 PR34), 작업 브랜치 `codex/wf07-image-pr-20261119`. 새 `recovery_pr.py`와 `test_recovery_pr.py`의 로컬 구현이다. WF06 main 반영 후의 완료 메타데이터도 이어받았다. 이번 변경은 미커밋이며 원격 CI/main·실제 연결은 별도 단계다.

## 연결한 흐름

1. 기존 서명 Slack 계획 요청 → 사건/조사 job → 기존 queue/worker가 저장한 증거 → WF05 RecoveryPlan을 읽는다. 원래 요청자·현재 정책·보관 기한을 다시 확인한다.
2. WF06의 최신 `proposal_ready`, 준비 동의 hash/generation/policy, 같은 Deployment UID·revision·이미지를 확인한다. 장부 예약은 기존 JobQueue SQLite에서 원자적으로 수행한다.
3. 기존 `assess_recovery_proof`/사건 결합/두 번의 재검증으로 정상 CI 영수증·정상 이미지 존재·실패 이미지·Pod 소유 관계를 확인한다. 정상 이미지가 없거나 대상/근거가 다르면 중단한다.
4. 기존 `draft_verified_proposal`/`prepare_pr_link`로 격리 저장소 `joungGo/c03-execution-lab`의 `ai-ops/lab/values.yaml` digest 한 항목만 바꾸는 변경안을 준비한다. 실제 로컬 Git object/tree와 Helm 렌더 비교를 사용한다. main이 이미 정상 digest이면 PR을 만들지 않고, 서비스 회복으로도 표시하지 않는다.
5. PR 준비 payload는 사건/job/계획·변경안·PR 연결 hash, 예상 영향·검증·실패 행동을 포함한다. 결정적 요청/브랜치 키와 장부로 같은 계획의 중복 생성을 막는다. 이번 `publish_local`은 테스트 fixture writer만 허용한다. 실제 GitHub 쓰기 adapter나 토큰은 연결하지 않는다.
6. PR 번호를 받은 뒤 실제 읽기 계약으로 저장소·base·branch·작성 Bot·단일 commit/diff·head를 재검증한다. `result_text`가 같은 사건·조사 ID, 검토할 정확한 head와 PR 링크를 반환한다. 문자열 생성이며 Slack 전송/outbox 활성화는 아니다.
7. 기존 `verify_execution_preflight`로 정확한 head의 사람 리뷰, 필수 CI, 보호 규칙, 작성/병합 App 분리, 현재 리소스 두 조회를 확인한다. 준비 동의는 GitHub 리뷰를 대신하지 않는다.
8. 소유 Lease의 검증된 locked preflight와 현재 검증 결과가 일치해야 기존 merge guard로 **쓰기 비활성 명령 계획**을 만든다. 실제 병합 tree/actor/main을 읽어 검증한 뒤에만 고정 merge SHA의 sync 계획을 만들며, 동기화 관측도 별도로 검증한다. 이 모듈은 merge/sync/잠금 획득 명령을 실행하지 않는다. 서비스 회복은 항상 별도 WF09 검증 대상이다.

## 내부 포트와 실제 활성화 경계

- `ImageRecoveryPRBridge(approvals, drafts, principals, reader, read_proof, author_id, prepare_seconds)`의 포트는 신뢰된 내부 어댑터다. 임의 JSON의 actor/proof/locked 값을 인증된 자료로 받는 공개 API가 아니다.
- `principals`는 검증된 Slack→GitHub 사용자 ID 매핑, 작성 Bot은 별도 ID다. 매핑·준비 예산 변경은 기존 기록을 재사용하지 못하게 한다.
- `read_proof`는 기존 제한 조회 경로에서 실제 CI/ECR/Kubernetes 근거를 검증한 결과와 canonical values를 반환해야 한다. `reader`는 기존 GitHub 조회 계약이다. 로컬 시험은 합성 응답을 쓰며 실제 AWS 조회 성공으로 해석하지 않는다.
- 준비 예산은 1~15초 내 명시 설정이며 원래 승인·관측 만료보다 길게 사용하지 않는다. DB 대기 뒤에도 확인한다. 콜백 자체의 블로킹을 강제 종료하는 기능은 없으므로 실제 어댑터가 전달받은 deadline을 지켜야 한다.
- `publish_local`의 mode 표시는 신뢰된 테스트 포트 선택용이며 악성 플러그인을 격리하는 보안 sandbox가 아니다. 제품 경로·HTTP·CLI에 writer를 노출하지 않았다.
- 실제 Slack 승인 endpoint, 승인된 principal 도출, Slack 결과 outbox 연결, 생산용 작성/병합/동기화 실행기와 토큰 수명·잠금 획득/회수는 후속 연결 단계에서 구체적인 활성화 계획이 필요하다. 이번 로컬 구현으로 실운영 복구 완료를 선언하지 않는다.

## 장부·중단·재시작·보관

`preparing → prepared → publishing → awaiting_review → merge_verified → sync_verified`

별도 종료/정지: `no_pr`, `needs_review`, `cancelled`, `expired`. 기존 queue의 `recovery_pr_links`에 최대 10,000개, 원래 계획 보관 기한 이내로 저장한다. 새 작업 큐나 Slack 수신 장부를 복제하지 않는다.

| 조건 | 처리 |
| --- | --- |
| 같은 계획 재요청 | 기존 기록 반환, 새 PR lineage를 만들지 않음 |
| 준비 중 취소/정책 변경/대상 변경 | 늦게 돌아온 성공을 수락하지 않음 |
| 작성 응답 유실·프로세스 종료 | publishing/needs_review 보존, 자동 재작성 금지 |
| 재시작 후 조정 | 결정적 키로 읽기 조회 → 정확한 PR 내용 검증; 없거나 모호하면 정지 유지 |
| 작성 중 취소 | PR이 이미 생겼을 가능성 표시; 확인된 PR 영수증을 기록해도 자동 재개하지 않음 |
| 리뷰 head 변경·거절·CI 실패·보호 누락·권한 불일치 | preflight 거절 |
| 잠금 유실·병합 SHA 변경·다른 리소스 | merge/sync 계획 거절 |
| 원본 보관 만료 | 접근 차단; prune이 복제된 사건/job/PR/proof/payload/실행 관측을 비우고 최소 해시 기록 유지 |

기존 개인 `RecoveryDraftStore`의 요청/초안은 그 저장소의 24시간 실행 만료 규칙을 그대로 따른다. 새 장부 prune은 기존 감사 DB의 원문을 삭제하지 않는다. **실제 배포 전 기존 draft DB의 별도 보관·정리 주기를 연결해야 한다.** 이번 시험의 DB는 임시 폴더이고 종료 시 제거된다. 보관 만료·명령 유효기간·실제 삭제 완료를 혼동하지 않는다.

## 시간·승인·원복 조건

준비 동의는 상세 PR 작성에 소비된다. 작성 후 사람의 GitHub 리뷰를 기다릴 때 WF06의 짧은 준비 관측 만료를 그대로 리뷰 마감으로 적용하지 않는다. 그렇다고 취소·정책 변경을 무시하거나 동의 TTL을 연장하지도 않는다. 정확한 승인 subject/policy/원래 승인자와 취소 여부를 계속 검증한다.

기존 PR 24시간 유효·리뷰 30분 신선도·현재 근거/관측 신선도는 유지한다. 오래 기다려 사건 근거가 낡으면 기존 검사에서 거절되며 새 조사·계획이 필요하다. 한 시간 전 자료를 현재 장애로 간주해 실행하지 않는다. 제품 운영의 대기/재조사 UX는 실제 연결 전 별도 확정한다.

C04의 Lease 조회3초·시작15초·실제 안내 뒤 최소리뷰120초/시작 뒤210초 리뷰마감·소유원복240초·독립원복 시작300초·회복관찰120초, 매 실행 새 승인·정확head 리뷰/CI·잠금/ACK·원복/자격 회수는 변경하지 않았다. 기존 C단계 소스/스크립트 변경도 없다. C04/C05 최종 실환경 검증은 미완료다.

## 검증 결과와 한계

집중28개(38.806초)·전체1468개(131.644초/skip0) 통과. 전체 시험 뒤 관측 시각 datetime/문자열 호환 수정은 영향 범위인 WF07 집중28개를 다시 실행해 통과했다. AST2개·저장소 경계·diff 검사 통과, 기존 C단계 소스/스크립트75개 기준 main과 동일. 최종 소스/로그 hash·기존 C단계 동일성은 [검증 JSON](recovery-pr-local-validation-20261119.json)에 기록한다. 실제 WF05 계획/큐/worker와 WF06 장부, 로컬 Git/Helm, 합성 GitHub·Kubernetes·CI/ECR 응답으로 검증했다. 정상 경로뿐 아니라 중복·응답 유실·중단/재시작·취소 경쟁·인가/정책 변경·정상 이미지 없음·main 변화·리뷰/CI 실패·신선도·보관 만료·잠금 유실을 포함한다.

검증 중 고정 now를 조회 전에 넘기면 나중 관측을 미래 자료로 거절하는 연결 오류를 재현했다. 두 조회 후의 시각으로 기존 sync guard를 호출하고 저장 직전 신선도를 다시 확인하도록 수정했다. 기존 guard의 신선도/동일성/권한 조건은 그대로다.

병합·동기화 성공 사례는 **로컬 fixture**이며 실제 클러스터가 회복됐다는 증거가 아니다. 실제 GitHub PR 생성·Slack·배포·AWS·인프라·장애 실습·Terraform·유료 모델 호출은 하지 않았다. 다음 단계는 코드 검토·CI/main 반영이다. 후속 WF08 명령1종, WF09 효과검증/원복·인계, WF10/11 실제 연결 검증은 작업표로 관리한다. 추천 **GPT-6 SOL · High**.

## 후속 코드 검토

위 최초 로컬 검증 후 [검토 기록](recovery-pr-review-20261119.md)에서 취소·보관 종료·최종 조회 지연 3건을 재현·수정했다. 최신 소스 해시와 최종 검증은 [검토 JSON](recovery-pr-review-validation-20261119.json)을 따른다. 최초 검증 JSON은 당시 구현 기록으로 보존한다.
