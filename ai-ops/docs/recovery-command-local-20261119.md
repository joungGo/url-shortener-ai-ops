# WF-08 고정 재시작 명령 경로 · 2026-11-19

## 이유와 완료 범위

일회성 재시작은 지속 설정을 바꾸는 이미지 PR과 승인 대상이 다르다. 기존 사건/조사·복구 후보에서 **실제로 실행할 고정 요청 자체를 봉인한 계획**을 만들고 별도 실행 승인·현재 상태·권한·공유 잠금을 확인해야 한다. PR 준비 동의로 명령을 실행하지 않으며, 조사 worker에 쓰기 권한을 추가하지 않는다.

AI main `b49672e95536dbc56f11a39626e300af2071d7da`(WF07 PR35)에서 분기한 `codex/wf08-command-execution-20261119`의 로컬 구현이다. `recovery_commands.py`와 `test_recovery_commands.py`를 추가하고 `recovery_approvals.py`의 기존 수명주기를 확장 지점으로 재사용했다. WF07 main 반영 후 메타데이터도 이어받았다. 코드 미커밋·원격 CI/main 미반영이며 실제 연결/실행은 하지 않았다.

## D5 로컬 계약: 최초 명령 한 종

대상은 기존 격리 `urlshortener / ai-ops-lab / lab-api` Deployment의 **replica 1개 재시작**이다. 운영 Deployment·다른 namespace·DB·replica 변경·임의 shell은 지원하지 않는다. 지금 구현한 것은 조건부 로컬 계약이며 실제 대상에서 이 조건들이 충족된다는 확인이나 새 RBAC 부여가 아니다.

전제 조건:

- 기존 큐/worker의 검증된 수집 결과에 같은 Deployment→ReplicaSet→Pod 소유 관계, Running이지만 Ready=false인 Pod, 같은 Pod의 Warning/Unhealthy 이벤트, 동일한 고정 ECR digest가 있다. 세 자료의 시각·감사 상태·발생 사건 범위를 확인한다.
- 정제 정책으로 생략된 이벤트 자유 메시지를 복구하거나 명령으로 사용하지 않는다. `Unhealthy`만으로 프로세스 정지를 확정하지 않는다. 이 근거는 **추가 진단이 필요한 재시작 후보**이며 아래 현재 조건과 정확한 계획의 사람 승인이 있어야 다음으로 진행한다.
- 신뢰된 현재 조회 포트가 이미지 사용 가능, 의존 서비스 정상, 설정 검증, 교체 용량 확보를 확인한다. UID·resourceVersion·revision/image·Pod 식별·generation이 계획과 같다.
- 현재 한 Pod가 비준비 상태이고 probe 실패가 지속된다. 이미 정상인 경우 변경하지 않는다. 진행 중 rollout·HPA·GitOps 자동 동기화·기존 restart marker가 있거나 진단 자료가 부족하면 거절한다.
- 기대 영향은 한 Pod 교체와 가용성 저하다. 이것을 무중단 작업이나 확정된 원인 해결로 표현하지 않는다.

실제 조회 어댑터는 위 진단을 인증된 데이터에서 도출해야 한다. 현재 `RestartSnapshot`은 내부 포트 계약과 합성 시험 자료이며 사용자 JSON/Slack 텍스트를 신뢰된 진단으로 받는 API가 아니다. 실환경 원인 조사·장애 주입으로 이 조건을 확인하지 않았다.

## 기존 경로 재사용과 승인 분리

1. 기존 WF05 `plan-recovery 사건 job deployment-restart`의 저장된 후보를 선택한다. `slack_restart_source`가 원래 사용자/현재 인가, 사건 firing 상태·발생 회차, 실제 job 결과·bundle/report/audit·대상/시각을 다시 확인한다. 기존 후보 스키마와 원문 장부는 재작성하지 않는다.
2. `RestartCoordinator.prepare`가 현재 조회를 받아 `RestartPlan`을 만든다. 사건/job/후보 hash/근거 hash, UID·resourceVersion·revision/image·Pod·generation, 정확한 PATCH, 실행자 ID·예산·영향·검증·실패 행동·보관 기한을 hash로 결합한다. 새 조사 큐는 만들지 않는다.
3. `RestartApprovalLedger`는 WF06의 대기/승인/취소/중복/만료/재검증 수명주기를 재사용한다. 같은 개인 JobQueue DB의 **별도 `restart_approval*` 테이블과 정책**을 사용한다. 기존 `recovery_approval*` 이름·자료·prepare_proposal 의미는 유지한다.
4. 명령 승인 scope는 `execute_command`다. `prepare_proposal` 정책을 명령 장부에 주거나 이미지 PR 승인 장부를 실행기에 전달하면 거절한다. 승인 대상은 정확한 RestartPlan hash와 현재 identity/정책/승인자다. 자연어 approve나 과거 실습 승인은 입력이 아니다.
5. 내부 수명주기의 legacy ready 상태명은 `proposal_ready`지만 명령 API는 `proposal_ready=false`, `command_ready=true`, `approval_scope=execute_command`를 반환한다. 모든 공개 결과의 `execution_allowed=false`는 생산 실행 연결이 비활성임을 유지한다.
6. 사람 대기 중 잠금·쓰기 토큰을 만들지 않는다. 합성 시험은 1시간 이상 기다린 뒤 새 관측으로 승인 재검증을 수행한다. 대상/현재 조건이 바뀌면 이전 계획으로 진행하지 않는다.

실제 Slack 승인 endpoint/서명된 interactivity·결과 outbox 연결은 아직 없다. 원래 WF05의 명령 후보 응답도 자동으로 실제 실행으로 바뀌지 않는다. `result_text`는 같은 사건/job/계획과 적용·잠금 회수 여부를 **문자열로만 생성**한다.

## 정확한 실행 요청과 별도 실행기

명령은 kubectl 문자열 실행이 아니라 고정 Kubernetes JSON PATCH 계약이다.

- `PATCH /apis/apps/v1/namespaces/ai-ops-lab/deployments/lab-api`
- Content-Type: `application/json-patch+json`
- `test /metadata/uid`, `test /metadata/resourceVersion` 이후 Pod template의 소유 annotation `ai-ops.bidservice.store/restart-id` 한 항목만 추가한다.
- annotations map이 없는 관측에 한해 빈 map을 먼저 만든다. 기존 map이 있으면 덮어쓰지 않는다. 동시 변경은 resourceVersion CAS가 차단한다.
- marker는 후보/UID/resourceVersion에서 도출하며 같은 승인 전달이 다른 marker로 재실행되지 않는다. 자유 인자·URL·shell·모델 생성 명령·DELETE는 계약 검증에서 거절한다.

`execute_local`은 명시적으로 주입한 `local_fixture` 포트만 허용한다. 서버/HTTP/Kubernetes 클라이언트·kubectl subprocess·토큰 발급을 기본 제공하지 않는다. mode 문자열은 신뢰된 로컬 테스트 포트 선택용이며 악성 플러그인을 격리하는 sandbox가 아니다.

후속 실제 실행 권한 후보는 Deployment `lab-api`의 get/patch, 기존 Lease `b6-scenario-lock`의 get/update로 제한한다. 리소스 생성·삭제·다른 이름 수정 권한은 요구하지 않는다. 현재의 역할/RoleBinding/ServiceAccount/조사 worker chart는 **변경하지 않았다**. 진단용 기존 읽기 포트와 쓰기 실행자의 신원/자격 연결은 활성화 전 별도 검토 대상이다.

## 잠금·시간·한 번만 실행하는 장부

`prepared → checking → executing → applied_unverified`, 별도 `cancelled / needs_review / expired` 상태를 사용한다.

- 실행 직전 기존 장부의 승인자·정책·신선도·보관 종료를 같은 DB 트랜잭션에서 확인하고 `(사건, 대상, UID, action)` 해시 슬롯을 원자적으로 소비한다. 동시 실행·다른 계획으로 재시도해도 같은 사건의 같은 대상에 자동 재시작을 반복하지 않는다.
- 슬롯을 소비한 뒤 읽기/잠금 단계에서 실패해도 자동 재시도하지 않는다. 이번 최초 구현은 확실히 쓰기 전 실패한 경우도 수동 인계하는 보수적 정책이다. 재시도 UX/정책을 몰래 추가하지 않는다.
- 대기 중 잠금 없음. 승인 뒤 현재 상태 확인 → `manual-<plan hash 일부>` holder로 기존 공유 Lease 획득 → 현재 상태/잠금 재조회 → 취소/승인/원래 기한 재검사 → executing 영속 기록 → PATCH 순서다. 기존 LabLease의 holder 형식·잠금 이름을 유지한다.
- 명령 준비/실행 예산은 필수 설정이며 최대15초, 실제 승인/관측/자료 만료보다 길게 쓰지 않는다. Lease 포트에 넘기는 조회/획득/회수 deadline은 남은 예산과3초 중 작은 값이다. 임의 callback을 강제 종료하지는 않으므로 실제 포트가 deadline을 준수해야 한다.
- `verified_until`은 로컬 포트의 검증 유효시각이며 Kubernetes Lease가 자동 만료되거나 다른 holder를 강제로 회수해도 된다는 뜻이 아니다. namespace/name/UID/holder·유한한 시각을 검사한다.
- 응답 유실·예외·프로세스 종료 뒤에는 `needs_review` 또는 executing 기록을 보존한다. `reconcile_local`은 읽기만 수행하며 쓰기를 재전송하지 않는다. marker·동일 UID/image/revision·새 resourceVersion·generation을 확인해야 적용으로 기록한다.
- 취소 직후 이미 변경이 적용됐으면 취소 상태와 applied=true를 함께 기록한다. 재시작을 되돌렸다고 표시하지 않는다. 자연 회복이나 부족한 진단은 쓰기 없이 중지한다.
- 정상 경로는 확인한 자기 Lease만 회수하고 반환 확인을 기록한다. 불명확한 적용/획득·회수 실패·프로세스 종료는 잠금 회수를 확인했다고 표시하지 않는다. 읽기 조정 후 남은 잠금은 별도 소유자 정리 대상이며 자동 강제 해제하지 않는다.

명령 PATCH의 적용 확인은 서비스 회복이 아니다. `applied_unverified`/`service_resolved=false`를 유지하고 새 Pod/readyz/업무 경로·관찰·공통 인계는 WF09로 남긴다. 재시작은 이전 프로세스를 되살릴 수 없어 자동 역재시작·무한 반복·승인되지 않은 이미지 원복을 하지 않는다.

## 보관과 회귀

기존 자료 보관 종료 이내에 계획을 저장하고, `prune`이 원문 명령 계획과 Lease 식별을 지운다. 최소 hash 기반 소비/종료 표식은 남긴다. 종료 표식을 포함한 실행 장부 상한은1만개다. 주기 prune·보관 용량 운영 연결은 실배포 전 작업이며 기존 개인 자료나 키를 삭제하지 않았다.

집중30개·전체1501개(152.834초/skip0), AST3개·diff·저장소 경계 검사가 통과했다. 최종 전체 시험은 마지막 장부 상한 보완까지 포함한다. 검증 수·시각·소스 hash는 [검증 JSON](recovery-command-local-validation-20261119.json)을 따른다. 실제 로컬 사건/큐/worker·SQLite 승인 장부와 가짜 시계·상태를 가진 실행 포트로 시험했다. 정상 적용, 미승인/다른 scope/사용자·정책 변경, 1시간+대기, 대상/CAS 변경, 부족한 진단/HPA/GitOps/rollout, 취소 경쟁, 동시 실행·동일 사건 두 계획, 응답 유실·프로세스 중단·읽기 조정, timeout·잠금 유실/회수 실패·보관 만료를 포함한다.

기존 C04 Lease3초·시작15초·안내후 최소120초/시작후210초 리뷰마감·소유원복240초·독립원복 시작300초·관찰120초와 실행별 승인·정확head 리뷰/CI·잠금/ACK·원복/회수는 그대로다. PR 경로의 사람 리뷰 조건을 명령 승인으로 대신하지 않는다. C단계 소스/스크립트75개는 변경하지 않았다. C04/C05 최종 실환경 검증은 미완료다.

실제 배포·Slack 전송·GitHub PR·인프라·장애·Terraform·유료 모델을 실행하지 않았다. 다음은 WF08 코드 검토·CI/main, 이후 WF09 공통 효과검증·원복/수동 인계와 WF10/11 실제 연결 검증이다. 추천 **GPT-6 SOL · High**.

## 검토 후 보완

취소/동시 조회 경계의 수정과 최신 검증 결과는 [WF08 검토 기록](recovery-command-review-20261119.md) 및 [검증 JSON](recovery-command-review-validation-20261119.json)을 따른다. 위 초기 로컬 검증 수·해시는 당시 자료로 유지한다.
