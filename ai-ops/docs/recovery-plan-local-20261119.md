# WF-05 복구 계획 요청·지원 조치 계약

2026-11-19 · 로컬 구현 및 검증. 기반 AI main `e3a4efab193bac143d598044e5d011329263d484`(WF-04 PR32 반영), 브랜치 `codex/wf05-recovery-plans-20261119`.

[전체 개선 계획](incident-workflow-and-improvement-plan-20261118.md) · [작업표](incident-workflow-implementation-tracker.md) · [검증 기록](recovery-plan-local-validation-20261119.json)

## 왜 변경했는가

WF-04는 사건 지정 조사와 결과 조회를 연결했지만, 조사 결과를 본 사용자가 복구안을 요청할 창구가 없었다. 조사 질문의 “복구해줘”를 실행 승인으로 해석하면 조사 권한이 변경 권한으로 확대된다. 따라서 **기존 조사 결과를 명시한 별도 복구안 요청**을 추가하고, 등록된 조치·근거·대상·지원 범위를 먼저 판단한다.

이번 완료 단위는 **지원 조치 판단과 실행 불가능한 계획 초안**이다. 정상 이미지 출처·현재 UID/revision·정확한 diff를 확인한 PR 제안서는 WF-07에서 연결한다. 승인 대기·유효기간·취소는 WF-06, 명령 실행은 WF-08이다. 계획의 `supported`는 저장된 근거와 조치 템플릿이 일치한다는 뜻이며 실제 실행 조건 충족이나 복구 성공이 아니다.

## 사용자 입력과 응답

```text
@봇 investigate-event <alert_ref 또는 evt-ID> 현재 상태를 조사해줘
@봇 plan-recovery <같은 alert_ref 또는 evt-ID> <결과의 job-ID> [image-rollback]
@봇 plan-recovery <evt-ID> <job-ID> deployment-restart
```

- 사건 명령을 활성화한 기존 `event_sources` 설정에서만 처리한다. 기본 비활성 설정과 일반 `investigate`·사건 조사·상태조회는 유지한다. 이 소스는 아직 실제 Slack에 배포하지 않았다.
- 서명·워크스페이스·App·사용자·채널·현재 대상 권한을 검증한다. 같은 사용자·채널에서 해당 사건에 요청한 명시적 job만 선택한다. 다른 사람/다른 사건/일반 조사 job, 추측한 ID는 동일한 조회 불가 응답이다. 자동 조사 결과를 임의로 공유하지 않는다.
- `this`/`이장애`는 WF-04의 같은 사용자·채널·thread 단일 사건 규칙을 따른다. job은 항상 명시한다. 어느 조사 결과를 선택했는지 숨기지 않는다.
- ACK는 기존 영속 inbox 저장까지만 수행한다. 자료 조회·조치 판단은 relay가 수행한다. 복구안 요청으로 새 조사 job·worker·PR·클러스터 명령을 만들지 않는다.
- action은 제한된 ID이며 자유 shell·URL·digest·승인 문자열·추가 인자를 받지 않는다. 등록되지 않은 ID는 미지원 수동 대응으로 분류한다. 기존 조사 질문에 “복구해줘”가 있어도 읽기 조사다.
- 같은 요청 스레드에 지원 여부·사유·대상·방식·job/evt·근거 해시/시각/인용 ID·계획 해시·다음 조건을 표시한다. 기존 전송 장부의 응답 유실/중복 정책을 재사용한다.

## 지원 조치와 판단

| action v1 | 방식 | WF-05 판단 | 실행 전 남은 조건 |
| --- | --- | --- | --- |
| `image-rollback` | PR | 정확한 `urlshortener/ai-ops-lab/lab-api/practice` 대상의 검증된 Pod·Warning 이벤트·Deployment 이미지/소유/시각 결합만 계획 후보로 지원 | 현재 UID/revision·정상 이미지 출처·단일 digest diff·계획 승인·정확 head 사람 리뷰/CI·제한 실행기·원복/효과 검증 |
| `deployment-restart` | 명령 | 명령 방식임을 표시하되 **미지원·미구현** 반환 | D5 대상/전제 결정과 WF-08 실행기/인가/실패 처리 검증 |
| 그 외 | 수동 대응 | 미등록 조치 | 별도 지원 계약·구현·검증 필요 |

이미지 후보는 기존 `c_recovery._image_failure_binding`의 v3 규칙/실물 수집 감사 연결을 재사용한다. 저장된 JobQueue/TaskService 결과의 report/bundle/audit를 검증하고, 사건→job 결합·Target 일치·등록 ECR 이미지 형식·관측 시각을 추가 검사한다. 질문·경보 설명·모델이 만든 문자열로 조치를 선택하지 않는다.

완료되지 않은 조사, 이미지 장애 근거 부족/다른 이미지 저장소, 사건 발생 전/미래 관측, 해제된 경보는 근거 부족으로 기록한다. 다른 대상은 미지원이다. 로그 등 일부 자료가 빠져 전체 조사가 partial이어도 특정 이미지 장애 결합 근거가 검증됐다면 **계획 후보**로 분류할 수 있으나 partial을 수정하거나 정상 이미지·현재 상태를 추정하지 않는다.

## 공통 계획 계약과 재사용

`recovery_plans.py`의 고정 ActionSpec registry와 `RecoveryPlan`은 다음을 포함한다.

- 요청·사건·job·Target, 경보 상태/발생 시각, 생성/자료 보관 종료 시각.
- action ID/버전·PR/명령/수동 방식·지원/미지원/근거 부족 및 고정 사유.
- inc/run·bundle/report/audit 해시·수집 시각·이미지 장애 인용 ID·Pod/Deployment UID 및 이미지 해시. 원문 로그·키·자격·임의 명령은 복사하지 않는다.
- 현재 설정/인가/사용자·채널·thread 해시와 전체 계획 해시.
- 고정 변경 범위, 예상 영향, 효과 검증 항목, 실패 시 행동, 해결해야 할 조건.

이미지 변경 범위는 격리 `joungGo/c03-execution-lab`, `ai-ops/lab/values.yaml`, `api.image.digest`만이다. 현재/정상 digest를 임의로 채우지 않으며 실제 diff는 아직 없다. 명령에는 PR을 강제하지 않고 별도 정확 계획 승인이 필요함을 설명한다.

모든 계획은 `state=draft`, `approval=not_requested`, `proposal_ready=false`, `execution_allowed=false`다. 해시/조치/대상 범위를 검증하며 승인·실행 상태로 바꾼 문서를 거절한다. 해시는 내용 결합이지 사용자 승인이나 외부 증거 서명이 아니다. 향후 실행기가 이 초안만으로 실행하도록 연결해서는 안 된다.

별도 큐·worker·DB를 만들지 않았다. 기존 개인 Slack SQLite의 `slack_event_requests.context.recovery_plan`과 고정 reply_text를 같은 트랜잭션에 저장한다. 기존 EventStore와 TaskService는 읽는다. commit 전 중단은 같은 요청을 다시 계산할 수 있고, commit 후에는 같은 계획/회신을 사용한다. 새 조사나 외부 부작용이 없으며 전달 불명확이면 기존 `needs_review`를 유지한다.

현재 인가·설정·사건/대상·자료 보관을 작성 완료 및 회신 전에 대조한다. 사건 발생 기준7일과 job 보관 종료 중 이른 시각을 적용하며, 이후 새 Slack 요청으로 자료 보관을 연장하지 않는다. 원본 결과 만료 시 전달을 차단하고 기존 정리 주기에 계획 복사본도 지운다. 사건 연결을 비활성화해도 정리는 계속된다. **이 보관 기한은 실행 승인 TTL이 아니다.** 일반 운영의 사람 대기 시간은 WF-06에서 별도로 구현한다.

## 배포 및 재설치 시 주의

이번에는 실제 배포·DB 전환·Slack 전송·GitHub PR 생성·명령 실행·인프라/Terraform·장애/유료 모델을 실행하지 않았다. 새 명령은 이전 relay가 해석할 수 없으므로 실물 전환 시 WF-04 절차대로 접수기/worker/relay를 중지하고 DB를 백업한 뒤 동일 검토 버전을 함께 적용한다. 구버전 relay와 새 요청 장부를 혼용하지 않는다. 역전환은 신규 요청과 DB 자료를 대조해야 한다.

## 검증 및 남은 범위

신규 집중27개·전체1399개(83.307초/skip0), AST4개·diff·저장소 경계 검사가 통과했다. 기존 C단계 소스47개가 main 기준과 동일하다. 소스/보존 조건 해시는 [검증 JSON](recovery-plan-local-validation-20261119.json)을 따른다. 합성 서명 요청·실제 로컬 사건/큐/worker/규칙 보고서·가짜 Slack 전송기를 사용했다. 지원 후보·누락 근거·미구현 명령·미지원 조치·다른 사건/사용자·중복/재시작/응답 유실·작성 중 인가 변경·보관 정리·명령 삽입 거절·빠른 ACK를 검증했다. 지원 경로 시험 중 socket 연결·subprocess 실행·모델 키 조회를 금지했다.

Lease GET3초·시작15초·안내 뒤 최소120초/시작 뒤210초 리뷰 마감·소유원복240초/독립원복300초·회복관찰120초와 실행별 승인·정확 head 리뷰/CI·잠금/ACK·원복/회수를 변경하지 않았다. 초안의 ‘추가 승인 없는 원복 금지’는 기존 승인된 C-04 소유/독립 원복을 끄는 변경이 아니다. 해당 실행기 파일은 그대로다. C-04/C-05 실환경 완료 여부도 바꾸지 않았다.

다음은 **WF-05 코드 검토·CI/main 반영**, 이후 WF-06의 사람 대기·만료·취소·재검증 로컬 구현이다. WF-07 PR 연결과 WF-08 명령 실행기를 이번 완료로 간주하지 않는다. 추천 모델/추론 깊이: GPT-6 SOL · High.

## 후속 코드 검토

[검토 기록](recovery-plan-review-20261119.md)의 사건 시각 정밀도 한 건을 보완했다. 최초 구현 검증 JSON은 당시 해시이며 최종 코드 해시/결과는 [검토 검증 JSON](recovery-plan-review-validation-20261119.json)을 따른다. 실제 연결/배포는 수행하지 않는다.
