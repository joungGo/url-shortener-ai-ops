# WF-10 전환·원복 변경 계획 형식(A6) · 2026-11-19

상태: **로컬 구현·검증 완료 / 미커밋.** 기준 main `5c74985973b71400000d408ae07e1cd69c410176`.

## 목적

기존 비활성 준비 자료(`wf10-disabled-review/transition·rollback.review.json`)는 전환·원복을 **일반 절차 문장**으로만 담고 있었고, 실제 후보 manifest에 묶이지 않았다. 실제 입력 12개 중 `rendered_manifest_hash`와 `rollback_bundle_hash`도 만들 형식이 없었다. 신규 `ai_ops.wf10_change_plan`은 **검증된 후보 패키지 하나**에서 적용 순서와 그것을 되돌리는 원복 묶음을 결정적으로 만들어, 이후 별도 실제 승인이 그 hash에 묶일 수 있게 한다.

## 계획 내용(데이터만)

- **입력 검증:** 먼저 `wf10_deployment.verify`로 후보를 다시 렌더링해 대조한다. 바뀌었거나 미완성인 후보는 거절한다. 리소스는 정확히 계획된 8개여야 하고 같은 namespace여야 하며, 보호 대상 종류는 포함될 수 없다.
- **적용 순서:** ConfigMap → NetworkPolicy(sender, main) → Service(sender, events, slack) → Deployment(sender → main). 정책을 workload보다 먼저 둬서 무제한 시작 창이 없게 하고, 호출받는 sender가 먼저 뜨게 했다.
  - 각 단계에는 리소스별 manifest hash, 선행 조건(`server_side_dry_run_passed`, `name_absent_before_apply`), 적용 후 기록할 항목(uid, resourceVersion)이 있다.
  - Deployment 단계에는 시작 허용 시간을 probe 설정에서 계산해 넣는다(main 120초, sender 60초). 임의 숫자는 쓰지 않는다.
- **사전 확인:** 기존 전환 조건(생산자 중지, 백업 quick_check·hash, 공유 DB, 같은 코드·설정, manual 정책, 빈 큐, 지원 schema)과 함께 계획 hash 별도 승인, 같은 이름 리소스 부재, 전 리소스 서버 dry-run을 요구한다.
- **사후 확인:** 모든 리소스의 uid 기록, 두 Deployment가 시작 허용 시간 안에 Ready, worker 송신 차단·sender만 인터넷, sender 경유 Slack 신원, manual 정책, 첫 요청 전 job 없음.
- **원복 묶음:** 적용의 역순으로 8개만 `delete_if_recorded_uid_matches`. `part-of=wf10` 라벨과 기록된 uid가 모두 일치할 때만 삭제하고, 다르면 중지하고 수동으로 인계한다.
  - Namespace·PVC·PV·Secret·ServiceAccount·RBAC·Ingress·StorageClass는 절대 삭제하거나 바꾸지 않는다.
  - 삭제 전에 쓰기를 멈추고 큐·Slack DB와 근거를 보존한다. 데이터 복원은 자동으로 하지 않고 기존 원복 검토 절차를 따른다.
  - 원복 trigger: dry-run·적용 거절, 시작 허용 시간 초과, 송신 음성 시험 실패, Slack 신원 거절, 예상 밖 소유자·uid, 운영자 중단.
- **hash 묶음:** `rollback_bundle_sha256`(원복 묶음), 후보 `manifest_sha256`, 계획 파일 전체 `change_plan_sha256`. `approval_binding.granted=false`, 과거 승인 재사용 금지.
- shell·kubectl 명령 문자열이나 자격 값을 담지 않는다. `execution_allowed=false`, `live_verified=false`. 적용·삭제 실행기는 없다.

## CLI

```bash
PYTHONPATH=ai-ops/src python -m ai_ops.wf10_change_plan build --candidate <후보 폴더> --output <새 폴더>
PYTHONPATH=ai-ops/src python -m ai_ops.wf10_change_plan verify --candidate <후보 폴더> --directory <계획 폴더>
```

출력 폴더는 0700, `change-plan.json`은 0600으로 배타 생성한다. `verify`는 후보에서 계획을 다시 만들어 바이트를 비교하므로, 원복 단계를 하나 빼고 hash를 다시 계산한 변조도 거절한다.

## 검증

- 신규 `test_wf10_change_plan.py` 7개: 순서·리소스별 hash·시작 허용 시간 계산, 원복 역순·보호 대상 제외·hash 결합, 데이터 전용(명령·자격 문자열 없음)·실행 불가, 결정성·파일 권한·덮어쓰기 거절·변조 탐지, 바뀐·미완성 후보 거절, 일반화된 CLI 거절.
- 합성 후보 CLI build/verify 성공(계획 9,183바이트). 전체 1615개(204.526초, skip1=WF03 환경 게이트) 통과. 저장소 경계·diff·compileall 통과.

실제 입력과 실클러스터 값이 없으므로 실제 hash는 아직 없다. 실제 적용·삭제, 서버 dry-run, uid 기록은 인프라 단계에서 한다. 인프라·배포·Slack·실습 실행 없음. 기존 시간·승인·원복 조건 변경 없음. 검증 수치는 [검증 기록](wf10-change-plan-validation-20261119.json)에 있다.
