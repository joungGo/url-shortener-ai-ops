# C-04 안내 전달 지연 · 로컬 재현·우선 중계 · 2026-11-17

> 후속 코드 검토(2026-11-17): 계측 저장 실패 시 signal 복원 누락, ACK 뒤 bridge 실패 종료코드 은폐, 비객체 이벤트 처리를 수정 전 재현하고 보완했다. 전체 로컬1111개/48.015초·집중40개·fixture/schema/고정 chart/소유권 검사를 통과했다. 아래1108개·로컬 미커밋·30초 측정은 최초 로컬 단계의 역사 기록이다. 이번 최종 코드의1초 합성 대기는 기존순서1.029530초·우선순서0.000947초였다. 실제 native 성능/새 owner 연결은 여전히 미검증이고 실습은 실행하지 않았다. 원격 CI/병합 결과는 후속 통합 기록에서 확인한다.

인프라 정리 뒤 AI main `e9b3751db6b0e8c658a36d36019a113b09db3274`의 격리 작업 트리에서 구현·검증했다. 실제 AWS/GitHub/Kubernetes·native 안내 도구·장애/복구 실습·Slack·유료 모델·Terraform은 실행하지 않았다. 코드/문서는 로컬 미커밋이며 CI/원격 반영이나 C04/C05 완료를 뜻하지 않는다.

## 확인한 범위

마지막008 기록의 PR 생성→안내 영수증은 **30.647779초**다. 당시 자료에는 PR 생성과 영수증 시각만 있어, 그 30초를 bridge 대기/호스트 스케줄/실제 도구 호출로 나눌 수 없다. 과거 원인을 특정 구간으로 확정하지 않는다.

기존 실행은 owner/ready 실행기와 안내 bridge가 별도 세션이었다. bridge의 flush된 요청이 있어도 호스트가 다른 느린 조회를 먼저 기다린 뒤 bridge를 읽으면 그만큼 안내가 지연되는 순서를 로컬에서 재현했다. 실행기 내부에 30초 sleep이 있었다는 뜻은 아니다.

실제 로컬 Unix 소켓·bridge CLI·합성 owner·합성 ACK를 사용한 최종 측정:

| 로컬 경로 | bridge 출력→안내 영수증 |
|---|---:|
| 다른 조회를30초 기다린 뒤 안내를 읽는 순서 | 30.038886초 |
| 안내/실행기 출력을 동시에 기다리는 새 경로 | 0.000954초 |

새 경로는 owner가 안내 영수증을 기다려 아직 끝나지 않은 동안 질문을 전달했다. **실제 Codex 도구 전송 성능을 측정한 결과가 아니다.** 호스트 자체가 질문을 읽지 않거나 native 도구 호출이 느린 경우까지 이 코드가 제거한다고 주장하지 않는다.

## 변경

- `host_c4_ready_trial.py`: 기존 ready 실행기와 bridge를 한 foreground 세션으로 연결한다. 자식을 시작하기 전에 기존 intent/runtime hash를 검증한다. 실행 인자와 새 승인 파일은 기존 ready CLI와 같다.
- `c_trial_notice_host.py`: 하나의 selector에서 bridge를 우선 처리한다. 실행기 진행 로그는 호스트 출력에서 제외하고 정확한 준비 이벤트만 전달해 로그가 안내를 막지 않게 한다. runner는 native 도구 요청을 가장할 수 없다. 입력의 실제 도구 응답은 기존 `delivery_evidence`로 검사해 현재 bridge에만 전달한다.
- `bridge_c4_review_notice.py --host-events`: socket listen 뒤 준비 이벤트, 질문 출력 시각, 호스트 ACK 수신 시각을 제공한다. 기본 별도 bridge 모드의 출력 계약은 유지한다. SIGTERM도 socket finally 정리를 거친다.
- `review-host-timing.jsonl`: 새 실행 폴더의 **상위 폴더**에 O_EXCL/0600으로 만들고 소유 FD에 단계별로 append·fsync한다. 기존 파일/다른 실행 디렉터리를 덮어쓰지 않는다. 각 줄의 완성 JSON을 읽으며 마지막 줄이 최신 집계다. 질문/응답 본문은 기록하지 않고 `execution_allowed=false`·전달/리뷰 증명 아님을 표시한다.
- host 입력 EOF·불명확/중복 ACK·채널 오류는 성공을 만들지 않는다. 소유 runner에 SIGTERM을 한 번 전달해 **기존** 원복/finally 경로를 사용하고 완료를 기다린다. owner를 새 timeout/강제 kill로 잘라내지 않으며 독립 감시 코드는 바꾸지 않는다.

계측은 bridge 출력→호스트 중계의 벽시계 차이, 중계→호스트 응답 대기(호스트 스케줄+실제 도구 처리 포함), 응답 전달→bridge ACK 관측을 나눈다. 벽시계 차이는 비권위적 진단이며 승인·영수증 시각을 대신하지 않는다. 실제 영수증 게시 시각은 기존 `deliver_review_notice`가 실제 응답 후 관측해 저장한다. host의 종료코드0이나 계측 파일은 기존 `result.json`의 C04 판정을 대신하지 않는다.

## 유지한 조건과 검증

실행·준비·승인·Lease·SIGTERM 원복·원격 감시 등 **17파일 byte 동일**, `deliver_review_notice`·`delivery_evidence`·`validate_delivery`·`require_bridge`·`notification_question` **5함수 AST 동일**을 확인했다. 시작15초·실제 안내 후 최소120초·시작 후210초 리뷰 마감·소유원복240초·독립원복 시작300초·회복관찰120초를 유지한다. 새 실행 승인, 정확-head 실제 GitHub 사람 리뷰/CI, UID/Lease/ACK와 회수 조건은 그대로다.

전체 **1108개/42.494초**, 새 host 시험14개 포함 통과. 최종 runtime 사전 검증 추가 전의 집중36개도 통과했다. 실제 bridge/pipe/자식 프로세스, owner 완료 전 전달, EOF/잘못된 ACK/중복/선행 응답 거절, runner 위장/로그 차단, 기존 파일 보존, 자식 실패 전파, SIGTERM socket 정리를 확인했다. AST317개·fixture4종·schema14종 변경0·저장소 경계·고정 플랫폼 chart 계약 검사도 통과했다. 기존 SQLite ResourceWarning1건은 이번 범위 밖에 남았다. 기본 sandbox의 Unix bind 제한 때문에 로컬 소켓 시험만 승인된 로컬 실행 환경에서 수행했다.

재현 명령은 실제 인프라를 실행하지 않는다:

```sh
PYTHONPATH=ai-ops/src:ai-ops/tests python ai-ops/scripts/check_c4_notice_host_local.py
PYTHONPATH=ai-ops/src:ai-ops/tests python -m unittest discover -s ai-ops/tests -p 'test_*.py' -v
```

## 재설치 뒤 연결 조건 — 지금 실행하지 않음

1. 먼저 코드 검토·CI·정상 반영을 완료하고 그 **정확한 새 commit**으로 runtime을 새로 만든다. 지금 HEAD e9b3751의 기존 bundle이나 continuation-20261116의 옛 소스에는 이번 미커밋 변경이 포함되지 않는다. 과거 bundle/manifest/승인을 수정해 실행하지 않는다.
2. 새 준비 owner는 자격/양쪽 감시/회수만 소유하고, ready runner 시작은 **한 곳**에 맡긴다. 이전 개인 `standby-owner.py`가 내부에서 runner를 Popen하는 형태를 그대로 이 host와 함께 실행하면 중복되므로 사용하지 않는다. 새 owner 연결은 아직 실환경 검증하지 않았다.
3. 새 승인 대상 준비를 시작할 때 기존 `run_c4_ready_trial.py`의 인자를 그대로 **`host_c4_ready_trial.py` 한 프로세스**에 전달한다. 별도 bridge 세션을 동시에 띄우지 않는다. 기존 intent·readiness·정상 CI·worker·권한·양쪽 감시와 새 승인/리뷰 조건은 모두 먼저 충족해야 한다.
4. 호스트는 이 foreground 세션을 우선 대기한다. `review_notice_requested`가 나오면 그 `arguments`를 실제 `functions.request_user_input_async`에 전달하고 **실제 성공 응답만** 같은 세션 stdin에 돌려준다. 실제 도구 호출 전후에 다른 긴 조회를 동기 실행하지 않는다. 질문 stdout이나 `accepted`를 수기로 만들어 전달 성공/사람 리뷰로 기록하지 않는다.
5. 먼저 별도로 승인된 실제 호스트 전달 리허설/현재 owner 연결을 확인한다. 타이밍 trace·실제 도구 응답·영수증을 대조하기 전 실습 성공을 주장하지 않는다. 이 로컬 검증은 새 감시 기동/장애 주입/새 PR 리뷰의 승인이 아니다. 재설치나 실습은 이번 작업에서 시작하지 않았다.

## 보존과 다음 작업

개인 근거/수정 소스 사본/patch/체크섬은 `/Users/jounghyeon/.local/share/url-shortener-ai-ops/c04-notice-host-local-20261117/`에 둔다. 기존 정리·코드 bundle·키/로그인·스냅샷 보존 자료를 변경하지 않았다. AI 원본 저장소의 다른 미커밋 문서는 유지하며 플랫폼에 AI 코드를 복제하지 않는다.

다음은 이 로컬 코드의 검토·CI·반영이다. 실제 native 전달 지연과 새 owner 연결은 별도 검증 범위이고 C04/C05·A5/A7 미완료/미측정·AI 포트폴리오 보류를 유지한다. 다음 추천 **GPT-6 SOL · High**.
