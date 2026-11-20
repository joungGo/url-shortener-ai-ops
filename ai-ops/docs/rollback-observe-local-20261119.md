# PR 경로 원복 관측 로컬 구현 · 2026-11-19

상태: **로컬 구현·검증 완료, 이 커밋 시점 PR CI/main 대기.** 기준 main `ce68c5bd761db02ca1941f1aa79f9612e1d8b62d`. 원복 실행 경로는 없다. 실제 읽기도 계속 막혀 있다(모든 리더 mode가 `local_fixture`가 아님).

## 조사 결과와 결정

- WF09 `record_rollback_local`은 원복 관측에 두 가지를 요구한다.
  - 원복이 적용됐다는 영수증(`reader.applied`). revision과 digest가 승인 범위(`RollbackScope`)와 정확히 같아야 한다.
  - 원복 뒤 새 관찰 구간의 표본(`reader.read`).
- A7-4 관찰기는 재시작(command) 영수증만 받았다.
- **원복을 실제로 실행하는 방법은 아직 정의되지 않았다.**
  - 승인 범위의 revision은 승인 시점에 이미 정해진 커밋이다. 그래서 과거 정상 커밋(기준선)에 Argo를 고정하는 방식으로 보인다.
  - 반면 WF07의 기존 동기화 확인은 **GitHub main의 최신 커밋(head) == 배포된 SHA**를 필수로 요구한다. 이 확인을 그대로 쓰면 과거 커밋에 고정하는 원복은 항상 검증불가가 된다.
- **사용자 결정(권장안 1)**
  - 원복 적용 여부는 **클러스터 관측으로 판정**한다.
  - GitHub main이 원복 revision과 같은지는 **판정 조건이 아닌 인계 정보로 기록**한다.

## 구현

### `ai_ops.restart_effects` 확장 (PR 영수증 관찰)

- `RestartEffectObserver(argo_application=(ns, name))`가 PR 영수증도 관찰한다.
  - 매 tick마다 Argo Application을 고정 GET 경로로 읽는다. 이 경로는 `EFFECT_READER_ROUTES`에 추가했다.
  - 앱 UID가 영수증과 다르면 관찰을 멈추고 모든 series를 unknown으로 둔다.
- `argo_state()`: 아래가 모두 같은 40자리 커밋을 가리킬 때만 정상(synced+healthy)으로 본다.
  - 고정 대상(`targetRevision`)
  - 동기화 revision
  - 마지막 성공 작업의 revision

  여기에 health가 `Healthy`이고 삭제 중이 아니어야 한다.
- 판정 규칙(`verify_effect`의 PR 경로)은 바꾸지 않았다. 앱 UID 일치·Argo 정상·revision/digest·replica 조건을 그대로 쓴다.

### `ai_ops.rollback_observe` (신규)

**`RollbackAppliedReader.applied`** (mode `kubernetes`)는 아래 조건을 모두 만족할 때만 원복 영수증을 만든다. 하나라도 어긋나면 `rollback_not_observed`로 거절한다.
- 원래 실행이 PR 경로일 것
- Argo 앱 UID가 같을 것
- 앱이 승인 revision에 고정·동기화되어 있고, 마지막 작업이 성공했고, 상태가 Healthy일 것
- 자동 동기화가 꺼져 있을 것(WF07과 같은 조건)
- Deployment UID·이름·namespace가 같고, generation 반영이 끝났을 것
- 컨테이너가 `api` 하나이고, 이미지가 `IMAGE_REPOSITORY@승인 digest`일 것

반영 시각은 Argo 작업의 `finishedAt`을 쓴다. 이 시각이 현재보다 미래면 거절한다.

영수증의 `source_hash`는 바뀌지 않는 사실(앱/Deployment UID, revision, digest, 반영 시각)만으로 계산한다.
- 클러스터가 그대로이면 다시 읽어도 같은 영수증이 나온다. 봉인 묶음이 결합된 실행 hash와도 같다.
- 그 사이 다시 동기화가 일어나면 영수증이 달라진다. 그러면 봉인 묶음과 맞지 않아 검증불가(인계)로 끝난다.

**`RollbackAppliedReader.alignment`**는 Argo `repoURL`이 허용된 저장소일 때만 기존 GitHub 읽기(`_main_sha`)로 main head를 확인한다.
- 결과는 `{'gitops_main_matches': True | False | None}`이다.
- GitHub가 없거나, 오류가 났거나, 다른 저장소이거나, deadline을 넘기면 None(미확인)이다. 예외 문구는 남기지 않는다.

**`RollbackObservationReader`** (mode `sealed_capture`)는 실제 영수증 리더와 A7-4 봉인 리더를 묶어, `record_rollback_local`이 받는 리더 하나로 제공한다.

### `ai_ops.recovery_effects` (WF09)

- 리더에 `alignment`가 있으면 `record_rollback_local`이 그 결과를 `rollback_note`로 저장한다.
  - **판정 조건이 아니다.** 실패하거나 값이 이상하면 None으로 둔다.
  - 보관 정리(prune) 때 함께 지운다.
- `result_text`는 기록이 있을 때만 한 줄을 덧붙인다: "원복 revision과 Git main 일치: 예 / 아니오(Git과 클러스터 불일치, 사람 확인 필요) / 미확인".
- 기존 판정·후속 결정 규칙은 바꾸지 않았다.

## 범위 밖 (이후)

- 원복 실행 경로: 기준선 커밋 고정 동기화로 할지 revert PR로 할지, 그리고 그 승인·잠금·재검증.
- 원복 관찰을 시작하는 프로세스 연결, `RollbackScope`를 인증된 승인 장부에서 읽는 연결.
- GitHub 읽기 신원, 실제 Argo 앱 이름 확정.
- 실클러스터 shadow 검증과 live 판단.

## 검증

신규 `test_rollback_observe.py` 8개. 실제 WF07 PR 경로의 동기화 확인과 WF09 장부 위에서, 가짜 Argo/Kubernetes/GitHub API와 가짜 시계로 실행했다.

- **전체 흐름**: 최초 판정 미해결 → 원복 후보 → 원복 영수증 → 원복 후 구간 관찰·봉인 → 실제 리더는 `live_rollback_reader_disabled`로 거절된다. 실제 어댑터를 시험용으로 허용하면 `rollback_verified`가 되고, 최초 실패가 보존되며, provenance는 `captured`다. Git main 불일치는 결과 문장에 표시되지만 판정에는 영향이 없다. 모든 요청은 GET이다.
- **GitHub 일치 여부**: 일치·불일치·GitHub 오류·GitHub 미설정·다른 저장소 → True/False/None/None/None. 토큰 문구는 남지 않는다.
- **영수증 안정성**: resourceVersion이 바뀌어도 같은 영수증이 나온다. 관찰 후 다시 동기화되면 수동 인계(검증불가)가 된다.
- **관찰 구간 중 변화**
  - 앱이 잠깐 Degraded였다가 회복 → 미해결
  - 기록 시점에 Degraded → 검증불가
  - 앱 UID가 바뀜 → 모든 series 수집 실패
- **원복 영수증 거절 11종**: 다른 revision, 일관되게 다른 revision에 동기화, 고정 안 됨, Degraded, 작업 실패, 자동 동기화, 다른 앱, 다른 Deployment, 다른 이미지, generation 미반영, 미래 시각. command 경로도 거절한다.
- **설정 누락**: Argo 앱 설정 없이 PR 관찰 → 거절. 잘못된 앱 이름 → 거절.
- **결함 변형 7종 모두 검출**: health 무시, 고정 대상 무시, 승인 revision 비교 제거, 이미지 digest 비교 제거, GitHub 일치 여부를 판정 조건으로 바꿈, 영수증 hash에 원시 응답 포함, 앱 UID 확인 제거.
  - 승인 revision 비교 제거는 처음에 검출되지 않았다. "일관되게 다른 revision에 동기화" 경우를 시험에 추가해 검출되도록 했다.

전체 결과는 [검증 기록](rollback-observe-local-validation-20261119.json)에 있다.

실제 클러스터·GitHub·배포·Slack·장애 실습 없음. 기존 시간·실행별 승인·정확 head 리뷰/CI·잠금·원복 조건 변경 없음.
