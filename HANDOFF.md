# 작업 인계 · 2026-11-17 최신 정리

## 2026-11-26 WF11 설치 준비: manifest 병합, Prometheus 평문 HTTP 수용, 비밀 자료 절차 (Claude)

- **3b 조사 중 문제 세 가지를 찾았다.** 고치기 전에 보고했고, 사용자가 "진행해"로 승인했다.
  1. **`wf11_edge`의 worker 조사 읽기 권한 누락 → 수정.** 기존 chart `rbac.yaml`과 같은 규칙을 WF10 SA에 묶었다.
  2. **IRSA가 공용 SA의 모든 Pod(메인·송신·egress)에 주입됨 → 수용·기록.**
  3. **renderer가 Prometheus에 HTTPS를 강제함.** 실제 Prometheus는 평문 HTTP·무인증이라 연결할 수 없었다 → (a)안(이 PR #75).
- **PR #74 병합:** 사용자 "병합해" 후 head `d18d5a1`로 고정해 squash 병합했다.
  - main `514a37c`, tree `7d0e24a`가 PR head와 같다.
  - branch를 정리했다.
- **PR #75(이 branch):** 평문 HTTP 9090·무자격 Prometheus를 수용한다.
  - collector Secret은 쓰지 않고, restart 경로는 HTTPS만 허용한다.
  - 시험 5개, 결함 변형 12/14(나머지 2개는 동치), 전체 1884 OK.
- **PR #76:** 비밀 자료 절차 문서. 가짜 자료로 연습하다가 LibreSSL에서 `x509 -ext`가 지원되지 않는 것을 찾아 고쳤다.

## 2026-11-26 WF11 설치 준비: 라우팅·IRSA 반영, 설치 전 manifest 생성기 (Claude)

- **플랫폼 PR #164·#165 병합(사용자, GitHub 화면 우회)**
  - #164: Alertmanager를 ai-events(/v1/events)로 전환했다(main `f9b1b72`).
  - 병합 뒤 main CI가 실패했다. 원인은 레거시 `check_alert_routing.py`가 예전 구조만 가정한 것이다. #164 작성 때 놓쳤다.
  - #165로 고쳤다(main `d195fcb`). 내용은 ai-events 분기와 openssl 고정값 동기화다. main CI 45628950483이 성공했다.
  - `enforce_admins` 해제 상태는 사용자 결정으로 유지한다. Claude는 바꾸지 않는다.
- **AI PR #73(IRSA) 병합:** "진행해" 후 병합했다(main `12622a1`). main CI도 성공했다.
- **3a `ai_ops.wf11_edge`(이 branch `claude/wf11-edge-manifests-20261126`)**
  - WF10과 같은 입력으로 manifest를 만든다: WF10 SA(IRSA), 복구 읽기 신원(SA·토큰 Secret·`ai-ops-lab`/`argocd` 읽기 Role), Slack Certificate·IngressRouteTCP.
  - 시험 4개, 결함 변형 14/14.
- **다음:** 3b 비밀 자료 절차. 그 뒤 사용자가 c03 토큰을 발급하고 aws login을 하면 설치를 시작한다.

## 2026-11-25 WF11 설치 준비(인프라 없이): 플랫폼 라우팅 PR·IRSA (Claude)

사용자가 "인프라 없이 끝낼 수 있는 작업 먼저"를 택했다. 사전 점검(읽기 전용) 결과는 다음과 같다.

- 새 계정 `764428911194`의 세션이 만료됐다. 기본 CLI는 옛 계정이다.
- `c03-execution-lab` 읽기 토큰이 없다.
- WF10은 실설치 이력이 없다.

배치 결정: WF10만 `ai-ops/ai-ops`에 설치한다. Slack 주소는 `slack-ops.bidservice.store`를 재사용한다. 발신원은 `eks-seoul`/`am-urlshortener`이고, 경보 채널은 기존 Webhook을 쓴다.

진행 상황:

1. **플랫폼 [PR #164](https://github.com/f-lab-edu/F-Lab-DevOps/pull/164)**
   - `legacy-values.yaml`을 보존하고, prepare.py로 만든 전환 values를 반영했다.
   - `--event-service`를 추가했다.
   - 실제 values 일치 시험과 amtool 시험을 추가했다.
   - 필수 CI는 모두 통과했다. 보호 규칙상 **승인 리뷰 1건이 필요**하다. 우회하지 않는다.
2. **AI Terraform `wf11-recovery.tf`**
   - IRSA 역할이며 기본 비활성이다.
   - `ai-ops:ai-ops` SA만 신뢰하고, 권한은 `ecr:DescribeImages` 하나다.
   - 정적 계약 시험 4개를 넣었고 validate를 통과했다.
3. **다음 작업:** 신원 manifest 도구(WF10 SA·복구 읽기 신원).

[문서](ai-ops/docs/wf11-install-prep-20261125.md)

## 2026-11-25 WF11 lab 경보 규칙 CI/main 반영 완료 (Claude)

[PR #72](https://github.com/joungGo/url-shortener-ai-ops/pull/72)를 사용자 "진행해" 후 병합했다(head `548afa4` 고정 squash → main `1b6ca4554e675761068cd24f800a6c41f7fb4d3b`). PR CI는 2회 모두 7종 성공했고 tree `c956549`가 일치한다. main CI45619959166도 성공했다. branch는 정리했다.

**다음: WF11 인프라 재설치.** Claude가 Terraform을 실행한다(새 위임). 순서는 다음과 같다.

1. 설치 가이드와 최신 정리 기록을 다시 읽는다.
2. 계정, state, plan을 대조한다.
3. 설치 시작부터 철거 시작까지 3시간 상한을 지킨다.
4. WF11 기준표와 준비 절차를 따른다.

## 2026-11-25 WF11 결정·lab 경보 규칙·CI 반영 준비 (Claude)

branch `claude/wf11-lab-alert-20261125`(기준 main `aaa8528`).

**WF11 결정(사용자)**

- 인프라를 재설치하고 Terraform은 Claude가 실행한다. 새 위임이며, 준비가 끝난 뒤 시작한다.
- 리허설은 생략한다.
- 상한은 3시간이다.
- 주입·복구 PR은 Claude가 작성한다. 병합과 sync는 "진행해" 후에 한다.
- S1 다음 S2a를 한다.
- 재생 자료는 개인 경로에 둔다.
- PR 자동 발행은 하지 않는다.

**공백과 해결.** lab에는 경보 규칙이 없고, 앱 지표 경보는 Pod가 0개인 장애를 볼 수 없다. llm-council 2차 결론("진행해")에 따라 renderer가 `LabDeploymentUnavailable` PrometheusRule을 렌더링하도록 했다.

- 규칙은 lab chart 밖에 둔다. lab AppProject는 Deployment만 허용하기 때문이다.
- 변경 계획의 첫 단계로 적용한다.
- 수신기 허용 목록은 바꾸지 않았다. `/v1/events` 경로는 경보 이름을 거르지 않기 때문이다.
- 준비 절차에 사전 규칙 로드 확인과 8분 합성 비상 절차를 추가했다.

**검증.** 시험 9개, 결함 변형 13/13. promtool은 사용자가 "Claude 실행"을 택한 뒤 공식 3.5.0을 sha256 검증해 돌렸고, check와 test 5종을 통과했다. 반대 확인으로 규칙을 일부러 망가뜨린 사본 3종은 모두 검출했고, 나머지 1종은 동작이 같은 변형이었다.

PR70·71 병합 메타데이터를 함께 이관했다. [문서](ai-ops/docs/wf11-lab-alert-local-20261125.md)

## 2026-11-25 조회 명령·README·WF11 준비 절차 CI/main 반영 완료 (Claude)

사용자 "진행해" 후 두 PR을 병합하고 branch를 정리했다.

- **[PR #70](https://github.com/joungGo/url-shortener-ai-ops/pull/70)**(`ai_ops.recovery_status`)
  - main `c2cc32e44861f58daab48f13599ae02283a6b4a7`
  - PR CI45578594470 7종 성공, tree `246e253` 일치
  - main CI45580280550 성공
  - 처음에는 "모두 진행해"로 병합하려 했으나 자동 분류기가 막았다. 명시적 "진행해"를 받은 뒤 병합했다.
- **[PR #71](https://github.com/joungGo/url-shortener-ai-ops/pull/71)**(README 흐름 요약과 1인 운영 명시, WF11 준비 절차 초안)
  - main `aaa85287c4d99805bb83da175fbe195abca9e5eb`
  - PR CI45579685052 성공
  - 이전 main 기반 PR이라 tree 대신 diff를 비교했고, 동일했다.
  - main CI45580525860 성공

**인프라 없이 남은 작업은 없다.** 다음은 WF11 기준표 9절의 사용자 결정이다.

- 인프라 재설치와 Terraform 실행 주체
- 로컬 리허설 여부
- 시간·비용 상한
- 장애 주입 승인
- 경보 규칙 확인
- S1/S2 순서
- 재생 자료 보관
- PR 자동 발행 여부

## 2026-11-25 장부 읽기 전용 조회 명령·CI 반영 준비 (Claude)

PR69(WF11 기준표 초안)는 사용자 "진행해" 후 병합해 main `60faf1f4fb9a618c49ab2d6e9e323569ada21a47`에 반영했다. PR CI45576610850과 main CI45577440373이 성공했고 tree `b827d8d`가 일치한다.

새 branch는 `claude/recovery-status-20261125`다. 사용자 결정은 "추천대로 장부 조회 명령부터 모두 진행"이다.

- `ai_ops.recovery_status`를 추가했다. 읽기 전용 `mode=ro`로만 열고, 승인·WF07·재시작·outbox 상태를 hash·상태·시각·건수로만 출력한다.
- WF11 기준표 9절 9번 항목을 해결했다.
- 다음 작업: README(1인 운영 명시와 흐름 그림), WF11 준비물(부하 생성 스크립트와 장애 주입 PR 절차).

## 2026-11-25 정책 council·WF11 합격 기준표 초안·CI 반영 준비 (Claude)

branch `claude/wf11-acceptance-draft-20261125`(기준 main `9928fc8`). 문서만 바꿨다.

**llm-council 결론.** 정책 결정 3건을 검토한 뒤 작업 순서를 바꿨다.

1. 합격 기준표
2. 로컬 kind/k3d 리허설
3. WF11(사람이 PR을 올리고, 사람이 원복)
4. 원복 승인 흐름

함께 정한 것:

- PR 자동 발행은 선택 과제로 둔다(2~3일, 두 번째 버튼 없음).
- 승인자 제한은 유지하고, 1인 운영이라는 점을 명시한다.

**[WF11 합격 기준표 초안](ai-ops/docs/wf11-acceptance-sheet-draft-20261125.md) 작성.**

- 시나리오: S0~S4(S1·S2 필수)
- 단계별 기대 판정과 증거
- 재생 자료
- 상한·중단 조건
- 자격 회수
- 사용자 결정 항목 9개

이 초안은 실행 승인이 아니다. PR68 메타데이터(main CI45574073257 성공)도 함께 이관했다.

## 2026-11-25 WF10 renderer restart 절 CI/main 반영 완료 (Claude)

[PR #68](https://github.com/joungGo/url-shortener-ai-ops/pull/68) head `2a900669dbbd529e8b03d5d69d9623280fd071e2` → main `9928fc8fea69528993456982bcbc21de0b19b299`(head 고정 squash, 사용자 "진행해" 후).

- PR CI45573120978 7종 성공(1858개)
- tree `54f49ff` 일치
- branch 정리
- main CI45574073257 성공

인프라 없이 남은 큰 항목은 없다. 다음은 사용자 결정이 필요하다.

1. 정책 결정
   - PR 발행 경로(GitHub 쓰기, 별도 승인)
   - 승인자=요청자 제한 완화
   - 원복 승인 흐름(`RollbackScope` 발급자)
2. 인프라 재설치 결정과 Terraform 실행 주체 → WF12 체크리스트 실제 값 → WF11

## 2026-11-25 원복 연결 보류·WF10 renderer restart 절·CI 반영 준비 (Claude)

branch `claude/wf10-restart-render-20261125`(기준 main `ba2bda0`).

**원복 프로세스 연결은 보류했다.** 사용자가 추천안을 택했다. 원복을 시작시킬 상위 계기가 없기 때문이다.

- PR 발행·재시작 실행이 모두 비활성이라 WF09 기록이 생기지 않는다.
- 원복 승인 흐름(`RollbackScope` 발급자)과 Argo 쓰기 포트가 없다.

**renderer에 `recovery.restart` 절을 추가했다.**

- `restart` 설정을 렌더링한다(`execute_command`, executor `local-restart-wf10`).
- Prometheus 주소는 수집기 연결과 같다. relay에만 별도 Secret을 마운트하고, NetworkPolicy는 바뀌지 않는다.
- 외부 선행 조건에 `pods/proxy` get(모든 recovery 후보)과 events·HPA list(restart가 있을 때)를 추가했다.
- PR67 메타데이터를 이관했다(main CI45571034647 성공).
- [문서](ai-ops/docs/wf10-restart-render-local-20261125.md)

## 2026-11-25 A7 정상(healthy) 판정 CI/main 반영 완료 (Claude)

[PR #67](https://github.com/joungGo/url-shortener-ai-ops/pull/67)의 head `efad7ffd2d9a794d0c8801961e52d0fdecd25cfa`를 사용자 "진행해" 후 head 고정 squash로 병합했다. main은 `ba2bda030609d81c4260f57bf3a58f5bde44c9a0`이다.

- PR CI45569981964 7종 성공(1852개)
- tree `97ea810` 일치
- branch 정리
- main CI45571034647 성공

다음 후보:

- 인프라 불필요: 원복 프로세스 연결, renderer `restart` 절(+ `pods/proxy`·events·HPA RBAC 반영 검토)
- 정책 결정이 먼저 필요: PR 발행, 승인자 제한 완화
- WF11 결정 대기: 실제 연결과 실행

## 2026-11-25 A7 정상(healthy) 판정·CI 반영 준비 (Claude)

branch `claude/a7-healthy-judgment-20261125`(기준 main `e5fcea4`).

사용자 결정(모두 추천, 2026-11-21 "healthy 미보고" 대체):

- Ready 60초 지속
- Pod proxy로 readyz·items 확인
- 열 때 이미 정상이면 전용 회신, 기록 없음

동작:

- 다섯 가지 확인을 모두 통과해야 healthy다.
- 재검증에서 정상이면 `already_recovered`다.
- 기존 시험 1개의 기대값을 바꿨다(revision 변경 + 같은 이미지 정상 → "이미 정상" 거절).
- RBAC에 `pods/proxy` get이 필요하다.
- PR66 메타데이터를 이관했다(main CI45546805843 성공).
- [문서](ai-ops/docs/recovery-healthy-judgment-local-20261125.md)

## 2026-11-25 예약 보관 정리 시험 경합 수정 CI/main 반영 완료 (Claude)

[PR #66](https://github.com/joungGo/url-shortener-ai-ops/pull/66) head `b90cda6f7a2d69bd1eb4f166f015b5dc4549370d` → main `e5fcea4548bd70d1c30700adefff8cbcc3123432`(head 고정 squash, 사용자 "진행해" 후).

- PR CI45546158652 7종 성공(1841개)
- tree `9b725dd` 일치
- branch 정리
- main CI45546805843 성공

다음 후보(인프라 불필요):

- 원복 프로세스 연결
- healthy 판정
- renderer `restart` 절

정책 결정이 먼저 필요한 것: PR 발행, 승인자 제한 완화.

## 2026-11-25 예약 보관 정리 시험 경합 수정·CI 반영 준비 (Claude)

branch `claude/retention-schedule-race-20261125`(기준 main `eb43554`). PR63 main CI 첫 실행을 실패시킨 `test_retention_schedule`의 경합을 고쳤다. 원인은 시험이 정리 완료 직후 `runs`를 읽는데, `runs`는 실행 뒤 따로 기록되는 것이었다. 시험만 수정했다.

- 1초 지연 주입 재현: 기존 시험 실패, 수정 시험 통과
- PR65 병합 메타데이터(main CI45543789806 성공 포함) 이관
- [문서](ai-ops/docs/retention-schedule-race-fix-20261125.md)

## 2026-11-25 A7 재시작 경로 CI/main 반영 완료 (Claude)

[PR #65](https://github.com/joungGo/url-shortener-ai-ops/pull/65) head `63372f4f59facb826fea70efbaa48b06d29cc736` → main `eb435540533da3d7583f40288c1ab6f30ac26165`(head 고정 squash, 사용자 "진행해" 후).

- PR CI45541408028 7종 성공(1841개)
- tree `138cdd9` 일치, branch 정리 완료
- main CI45543789806 성공

다음 후보:

- `test_retention_schedule` 경합 시험 수정(작음)
- 원복 프로세스 연결
- healthy 판정
- renderer `restart` 절

정책 결정이 먼저 필요한 것: PR 발행, 승인자 제한 완화. 실제 실행·연결은 WF11 결정을 기다린다.

## 2026-11-25 A7 재시작 경로 실행 직전까지 연결·CI 반영 준비 (Claude)

branch `claude/a7-restart-wiring-20261125`(기준 main `8db0878`). 사용자 결정(모두 추천):

- 실행 직전까지 연결한다.
- `request-approval`을 재사용한다.
- 관측기 읽기 신원을 확장한다.
- WF05는 회신 문구만 바꾼다.

재시작 후보에 `request-approval`을 보내면 다음 순서로 간다. WF08 봉인(읽기와 게이트) → 명령 승인 대기 → 버튼 → 재검증 → `command_ready` 알림("실제 재시작 실행은 실환경 실행 결정 전까지 비활성"). 클러스터 쓰기는 0회다.

함께 바꾼 것:

- 정책 전환 명령이 범위별로 동작한다.
- 재시작 기록 누적 결함을 고쳤다(30일 뒤 삭제).
- WF12 문서 3·5·6·7·10절을 갱신했다.

PR63 main CI 45537162151의 첫 실행은 `test_retention_schedule`의 경합 시험으로 실패했다. 실패한 작업만 다시 실행해 성공했다(attempt 2). 시험 수정은 별도 조각으로 제안한다. 상세는 [조각 문서](ai-ops/docs/recovery-restart-wiring-local-20261125.md)에 있다.

## 2026-11-25 A7 작은 항목 CI/main 반영 완료 (Claude)

[PR #63](https://github.com/joungGo/url-shortener-ai-ops/pull/63) head `36d8c1468805616e37809b354f1d67720bf0e6b3` → main `8db08787f34a460c251e2fb6ed36fd88c9341be4`(head 고정 squash, 사용자 "진행해" 후). 첫 PR CI(45536393470 이전 실행 45535553355)는 CLI 시험이 Linux `/tmp`를 루트로 잡아 실패 → 시험 수정(제품 변경 없음) 후 PR CI45536393470 7종(1817개) 성공, tree `3679977` 일치, branch 정리. main CI45537162151은 기존 경합 시험으로 첫 실행 실패, 실패 작업 재실행 성공(attempt 2). 다음 후보(인프라 불필요): execute_command·재시작 조정기 연결, 원복 프로세스 연결, healthy 판정; 정책 결정 선행: PR 발행, 승인자 제한 완화; 실제 연결은 인프라 재설치·실행 주체 결정 대기.

## 2026-11-25 A7 작은 항목·CI 반영 준비 (Claude)

사용자 결정(모두 추천): 정책 전환=운영자 명령+이전 설정, 알림=사유별 전용 문구, tombstone=보관 종료 30일 뒤 삭제. branch `claude/a7-small-fixes-20261125`(기준 main `0b91169`). `slack_server transition-approval-policy --config --store --previous`: relay·ingress 프로세스 잠금 확보(실행 중이면 거절), 이전 정책 hash 정확 일치·버전 증가 필수, 진행 중 승인 superseded(재시작 relay가 알림), 장부 없으면 `nothing_to_transition`, 출력은 hash·건수만. `RecoveryApprovalLedger.prune`·`ImageRecoveryPRBridge.prune`: 보관 종료+30일(`TOMBSTONE_SECONDS`) 지난 tombstone·receipt·WF07 기록 삭제(상한 도달 결함 해소, 계획 만료로 재개·재결정 불가 확인). outbox `REASON_NOTICES`로 `needs_review`/`observation_expired` 전용 문구. 시험11·변형 13/13. WF12 7절 3·5항 갱신. PR62 병합·main CI 결과 메타데이터 이관 포함.

## 2026-11-24 relay 외부 송신 proxy 조각 2 renderer CI/main 반영 완료 (Claude)

[PR #62](https://github.com/joungGo/url-shortener-ai-ops/pull/62) head `d4486a61e0c608d562ef4b8b49c97c3f13eb5b73` → main `0b91169c7a5f19ad947c02270ef43224d3cc741b`(head 고정 squash, 사용자 "진행해" 후). PR CI45390608450 7종(1806개) 성공, tree `d13d57f` 일치, 보호 변경/우회 없음, branch 정리. **relay 외부 송신 문제 코드상 해결(PR61·62).** 남은 것: 인프라 재설치 요청·Terraform 실행 주체 결정 → WF12 5절 체크리스트 실값(reader 신원·RBAC, GitHub 읽기 토큰, IRSA 역할·주석, Secret 이름, 정상 CI binding, Slack Interactivity URL) → renderer recovery 입력 확정 → WF11 실환경 검증. 그 뒤 PR 발행·원복·재시작 프로세스 연결.

## 2026-11-24 relay 외부 송신 proxy 조각 2 renderer·CI 반영 준비 (Claude)

사용자 결정(추천): 선택적 recovery 입력 전체. branch `claude/a7-egress-proxy-renderer-20261124`(기준 main `65a32a1`). WF10 renderer에 선택적 `recovery` 절(approvers=렌더링 grant 사용자, approval 시간, principals, author_id, aws_role_name, secrets{reader,github}, normal_binding) → 있을 때만 `recovery-approval.json`(런타임 모델 검증, observe api_url=기존 collector API·reader `/run/recovery-reader`, prepare drafts `/data/recovery-drafts`·GitHub `/run/ai-github`·egress_proxy `http://<name>-egress.<ns>.svc:8080`)·`normal-ci-binding.json`, relay 전용 Secret 마운트, `-egress` proxy Deployment(비밀·토큰·PVC 없음)·Service·NetworkPolicy, 메인 Pod 송신에 proxy 8080 추가(직접 인터넷 없음), 후보 `recovery` 요약·선행 조건 5·잔여 위험 2. 없으면 main과 manifest/후보 hash 동일. 변경 계획: proxy NP·Service·Deployment를 메인 Pod 앞에, 사후 확인 2개 추가. 후보 패키지만 128KiB 상한. WF12 7절 1항 해결로 갱신. 시험8·변형 15/15. PR61 병합·main CI 결과 메타데이터 이관 포함.

## 2026-11-24 relay 외부 송신 proxy 조각 1 CI/main 반영 완료 (Claude)

[PR #61](https://github.com/joungGo/url-shortener-ai-ops/pull/61) head `33614a74b1547e6c0eb545acf9a4ab227850e971` → main `65a32a14202973dd9fdfb3853071a8a4dfa6ec38`(head 고정 squash, 사용자 "진행해" 후). PR CI45359117834 7종(1798개) 성공, tree `22a87e5` 일치, 보호 변경/우회 없음, branch 정리. 다음: 조각 2 — WF10 renderer에 proxy Deployment·Service·NetworkPolicy(메인 Pod 송신은 proxy로만, proxy는 메인 Pod 수신·인터넷 443 송신) 추가, WF12 체크리스트 갱신.

## 2026-11-24 relay 외부 송신 proxy 조각 1·CI 반영 준비 (Claude)

사용자 결정(추천 3번): 고정 목적지 송신 proxy. branch `claude/a7-egress-proxy-20261124`(기준 main `4a62309`). 신규 `ai_ops.recovery_egress_proxy`(`api.github.com`·`sts.ap-northeast-2.amazonaws.com`·`api.ecr.ap-northeast-2.amazonaws.com` :443만, CONNECT 한 줄+일치 Host 하나, DNS 전부 공개 IPv4, 동시 8·유휴 35초·수명 120초, 고정 응답·무기록; `model_egress_proxy`는 불변). `PrepareConfig.egress_proxy`, `TokenRepoReader`·`irsa_clients`/`EcrImageChecker`가 설정 proxy만 사용 — botocore 세션 단계에 리전·지역 STS·proxy·dualstack/FIPS 끔·설정 endpoint URL 무시 → web-identity 자격 교환도 proxy 경유(로컬 기록 proxy로 CONNECT 대상 확인). **시험에서 제품 결함 2건 발견·수정:** dualstack 환경 변수 시 자격 교환이 `sts.ap-northeast-2.api.aws`로, `AWS_ENDPOINT_URL[_STS]` 시 projected 토큰을 실은 자격 교환이 임의 주소로. 시험12·변형 19/21(동등 2)·전체1798(skip1). 다음: 조각 2 renderer proxy Pod·NetworkPolicy·WF12 갱신. PR60 main CI45356948426 성공 기록 포함.

## 2026-11-24 A8 WF12 인계 초안 main 반영 완료 (Claude)

[PR #60](https://github.com/joungGo/url-shortener-ai-ops/pull/60) head `87f8d05d155e93b94026b0b31aab8a5904d81ad6` → main `4a623096fdf17dea464ae100fd45ec388395d8e1`(head 고정 squash, 사용자 "진행해" 후). PR CI45355975980 7종 성공, tree `9f282f4` 일치, branch 정리. 다음: (1) relay 외부 송신 방식 결정(메인 Pod 송신 추가/별도 송신 Pod/송신 proxy), (2) 인프라 재설치 요청·Terraform 실행 주체 결정 후 WF12 5절 체크리스트를 실제 값으로, (3) WF11 실환경 검증, (4) PR 발행 경로·원복/재시작 프로세스 연결.

## 2026-11-24 A8 WF12 인계 문서 초안 (Claude)

사용자 허락(추천 3번). branch `claude/a8-wf12-handoff-20261124`(기준 main `5d0c439`), 문서만. [WF12 인계 초안](ai-ops/docs/wf12-recovery-handoff-20261124.md): 흐름표·근거·설정 예시(모델 검증)·프로세스 동작·연결 체크리스트·지원 범위·미결정·안전 경계. **새 미결정 발견:** WF10 renderer에서 `slack-relay`는 메인 Pod에 있고 메인 Pod 송신은 DNS·수집 CIDR만(B′: 인터넷 송신은 sender Pod만) → WF07의 `api.github.com`·AWS STS/ECR 읽기가 막힘(그 경우 준비는 `needs_review`로 안전 중단). 선택지: 메인 Pod 송신 추가 / 별도 송신 Pod / 송신 proxy. PR59 main CI45354867715 성공 기록 포함.

## 2026-11-24 WF07 실제 읽기 포트 조각 B CI/main 반영 완료 (Claude)

[PR #59](https://github.com/joungGo/url-shortener-ai-ops/pull/59) head `72fbc518e2999c9c574b45896a2136e60d2709e0` → main `5d0c43984c27754921c2d5b7f30cc1e883b3542d`(head 고정 squash, 사용자 "진행해" 후). 첫 PR CI 실패(IMDS 탐색) → 수정 후 PR CI45353120230 7종 성공(1786개, skip1 — boto3 설치로 `test_c_trial_aws` 시험 실행, image 빌드 통과), tree `0ba1390` 일치, 보호 변경/우회 없음, branch 정리. **설정이 갖춰지면 실제 배포에서 요청→관측→승인→재검증→PR 준비까지 코드 연결 완료(발행 없음).** 다음 후보: 실제 연결 준비(IRSA·GitHub 토큰·RBAC·renderer — 인프라 재설치·실행 주체 결정 선행), PR 발행 경로, A8 WF12 문서.

## 2026-11-24 WF07 실제 읽기 포트 조각 B·CI 반영 준비 (Claude)

branch `claude/a7-wf07-proof-20261124`(기준 main `f219d04`). 사용자 결정(모두 추천): 정상 CI 근거=`NormalCIInbox` 재사용, binding 일치 최신 `ready`, ECR=boto3+IRSA. `requirements.lock`에 boto3 1.44.0·botocore 1.44.0·s3transfer 0.19.3·jmespath 1.1.0·python-dateutil 2.9.0.post0·six 1.17.0·urllib3 2.8.0(PyPI 공개 wheel sha256, 순수 Python, `--require-hashes` 설치 확인), `pyproject` dependencies에 boto3. 신규 `ai_ops.recovery_proof_live`: `EcrImageChecker`(IRSA 기본 체인·고정 리전·STS 호출자=고정 계정 assumed-role/<설정 역할>, 증거 필드 3개만, `ImageNotFoundException`만 부재, SDK 오류 고정 코드 — 정상 이미지 조회 오류 원문 노출 결함을 시험에서 발견해 수정), `LiveRecoveryProof`(Kubernetes 객체→SHA revision→values→inbox→ECR→`assess_recovery_proof`), `live_pr_bridge`. `NormalCIInbox.latest_ready_history`. `PrepareConfig`에 `normal_binding_file`·`aws_role_name`. relay는 `prepare` 있을 때만 WF07 bridge(ingress 제외) → 설정이 갖춰지면 실제 배포에서 PR 준비까지 연결, 발행(GitHub 쓰기) 없음. 연결 단계: IRSA(ecr:DescribeImages)·GitHub 토큰·RBAC·renderer·CI relay 실행. 시험16·변형 21/21(고친 실행기, 처음 15/18 → 빈틈 시험 3개 추가). 첫 PR CI(run 45351079366) 실패: boto3가 client 생성 시 자격을 찾다 CI 러너에서 IMDS까지 내려감 → `irsa_clients`로 IRSA web identity 제공자만 남기고 역할 ARN 일치 확인, ECR client는 첫 사용 때 생성(IRSA 누락 시 relay가 아닌 WF07 준비만 멈춤). PR58 병합 결과 메타데이터 이관 포함.

## 2026-11-24 WF07 실제 읽기 포트 조각 A CI/main 반영 완료 (Claude)

[PR #58](https://github.com/joungGo/url-shortener-ai-ops/pull/58) head `dcfa034a5e39d2127ae60849c24274df9da57672` → main `f219d046013ca131930568c59dcad01ae6937d93`(head 고정 squash, 사용자 "진행해" 후). PR CI45347993050 7종(1770개) 성공, tree `2ed22aa` 일치, 보호 변경/우회 없음, branch 정리. 다음: 조각 B — 사용자 결정(2026-11-24, 모두 추천): 정상 CI 근거=CI relay가 검증한 `NormalCIInbox` 재사용(HMAC 확인), 실행 선택=설정 binding 일치 최신 `ready`, ECR=boto3+IRSA(실행 lock hash 고정 추가).

## 2026-11-24 WF07 실제 읽기 포트 조각 A·CI 반영 준비 (Claude)

사용자 결정(추천안): ECR=boto3 추가+IRSA(조각 B), GitHub=토큰 파일 마운트, 2조각 분할. branch `claude/a7-wf07-sources-20261124`(기준 main `de0a291`). 신규 `ai_ops.recovery_sources`: `TokenRepoReader`(격리 저장소 `git/ref/heads/main`·`git/{commits,trees,blobs}/<sha>`만, 토큰 파일 요청마다 재읽기·권한/형식 검사, proxy 무시·redirect 미추적·8초·2MiB, 본문 없는 고정 오류), `values_at`, `ProofObjectsReader`(Argo argocd/lab-target·Deployment·Lease·ReplicaSets·Pods 고정 GET 5개, selector Pod·소유 RS 선택만), `PrepareConfig`(principals·author·drafts_dir·github_token_file·prepare_seconds≤15, observe 필수·승인자 매핑 필수). check-config `recovery_prepare_configured`. 진입점 연결·read_proof 조립은 조각 B. **정정:** 조각 3(PR56) 변형 검사 근거 무효(실행기 최소 PATH에서 helm 부재로 기준선 실패) → 병합 커밋 임시 worktree 전체 환경 재검사 13/13 실제 검출, 실행기는 전체 환경 상속·기준선 선확인으로 수정. 조각 1·관측기는 최소 환경 기준선 통과, 조각 2는 생존 변형 존재로 유효. 시험13·변형 19/19. PR57 병합 결과 메타데이터 이관 포함.

## 2026-11-24 A7 PR 경로 현재 상태 관측기 CI/main 반영 완료 (Claude)

[PR #57](https://github.com/joungGo/url-shortener-ai-ops/pull/57) head `cd0fedec51bc4e92c0631a70225b696437e05718` → main `de0a2919f36682147b09abe1ac1219b602c71515`(head 고정 squash, 사용자 "진행해" 후). PR CI44883281788 7종(1757개) 성공, tree `5d7a235` 일치, 보호 변경/우회 없음, branch 정리. A7 로컬 연결: 요청→관측→승인→재검증→PR 준비. 다음 후보: execute_command·재시작 조정기 연결, WF07 실제 읽기 포트(GitHub·CI·ECR·ID 매핑), A8 WF12 문서.

## 2026-11-21 A7 PR 경로 현재 상태 관측기·CI 반영 준비 (Claude)

사용자 결정: 실패 판정=현재 컨테이너 상태(WF07 재확인과 동일, 이벤트 시각 미사용), healthy 판정 안 함(실패 아니면 unknown), 설정 있을 때만 연결. branch `claude/a7-recovery-observer-20261121`(기준 main `83b8e39`). 신규 `ai_ops.recovery_observe`: PR48 `ApiClient` 재사용, 고정 GET 4개(Deployment·ReplicaSets·Pods·Argo application), 대상 외 계획은 읽기 0. `image_failure`=selector Pod 1개·Deployment→RS→Pod controller 소유·Pod/RS/상태 이미지=Deployment 이미지·`api` 단일 컨테이너 ready=false·waiting `ErrImagePull|ImagePullBackOff`·최신 generation·readyReplicas 0, 그 외 `unknown`. revision=targetRevision==sync.revision(WF07 base_sha와 동일) 아니면 예외. proof_hash=원시 응답 hash의 hash. `RecoveryApprovalConfig.observe`(https·절대 경로·Argo 앱) 있을 때만 사용, check-config는 `recovery_observe` 여부만. 연결 단계: 읽기 RBAC(replicasets list 신규)·renderer 마운트·실응답 형태 확인. 시험15·변형 16/16. PR56 병합 결과 메타데이터 이관 포함.

## 2026-11-21 A7 프로세스 연결 조각3 CI/main 반영 완료 (Claude)

[PR #56](https://github.com/joungGo/url-shortener-ai-ops/pull/56) head `e61c4427821e5a4ea9b0be218c28c4d5213067f5` → main `83b8e39efe3ce02405b1e1e3bb73a49b41471d3f`(head 고정 squash, 사용자 "진행해" 후). PR CI44881773956 7종(1742개) 성공, tree `3f2a4bf` 일치, 보호 변경/우회 없음, branch 정리. 다음(사용자 허락): PR 경로 현재 상태 관측기(observe 포트 실구현, 가짜 K8s API 서버 검증).

## 2026-11-21 A7 프로세스 연결 조각3 승인 뒤 자동 재검증·WF07 PR 준비·CI 반영 준비 (Claude)

사용자 결정: 재검증=승인 후 자동, WF07=자동 준비까지(GitHub 쓰기 제외). branch `claude/a7-revalidate-prepare-20261121`(기준 main `a5f59dc`). `RecoveryApprovalService.advance_once`(relay 5초 주기, 1단계): `approved`→요청자 신원 `ledger.revalidate`, `proposal_ready`·WF07 기록 없음→`ImageRecoveryPRBridge.prepare`(로컬 diff·PR 내용, publish 없음), 반복 없음. 재검증 시간 ≤15초 설정 검증(relay 상태 확인 30초 이내). `RecoveryOutbox.enqueue_pr` WF07 결과 고정 문구(URL·head·diff 없음). 장부 `page(states=)`. 보관 정리에 WF07 기록 포함. 진입점은 WF07 포트를 넘기지 않음(실 GitHub/CI/ECR 읽기·ID 매핑 미구현) → 운영 미연결, 관측 포트도 미연결 유지. 한계: 준비 뒤 `observation_expired` 알림 혼동 가능, 매핑 없는 요청자 반복 거절. 시험11(모듈35)·변형 13/13(변형 검사 중 타 모듈 일시 실패로 1건 거짓 검출 → 상태 필터 시험 추가로 실제 검출). PR55 병합 결과 메타데이터 이관 포함.

## 2026-11-21 A7 프로세스 연결 조각2 CI/main 반영 완료 (Claude)

[PR #55](https://github.com/joungGo/url-shortener-ai-ops/pull/55) head `b973505b066cf364ab068d1a087cb816179fd243` → main `a5f59dc085b08114baeb44d950ab616e709dcace`(head 고정 squash, 사용자 "진행해" 후). PR CI44730619628 7종(1731개) 성공, tree `60ae06e` 일치, main CI44879917750, 보호 변경/우회 없음, branch 정리. 다음 A7 후보: 승인 뒤 재검증·WF07 연결, PR 경로 현재 상태 관측기(실제 연결 필수), execute_command 승인, renderer 마운트.

## 2026-11-20 A7 프로세스 연결 조각2 복구 보관 정리 tick·CI 반영 준비 (Claude)

branch `claude/a7-retention-tick-20261120`(기준 main `46789b1`). relay가 기존 Slack 요청·작업 정리(60초) 뒤 10분마다 A7-5 `RecoveryRetention.tick`: 승인 기능 켜짐→`RecoveryApprovalService.retention`(승인 장부 tombstone·outbox), 꺼짐→`OrphanOutboxRetention`(남은 outbox만, 테이블 생성·정책 읽기 없음). `recovery_outbox.prune_existing` 추가(`RecoveryOutbox.prune` 동작 동일). 종료 중 미실행, 실패는 이름만·다음 주기. 보관 정리 객체 없으면 기존 루프 호출 그대로. 증거·봉인 디렉터리 정리는 재시작 경로 연결 때. 시험7(모듈24)·변형 10/10. PR54 병합 결과 메타데이터 이관 포함. 실제 배포·Slack·장애·인프라 없음.

## 2026-11-20 A7 프로세스 연결 조각1 CI/main 반영 완료 (Claude)

[PR #54](https://github.com/joungGo/url-shortener-ai-ops/pull/54) head `73b8a5315bfaeacfd21bf5b30394a8b51eee8b03` → main `46789b11434736aad2531a22f40605dad44fe57c`(head 고정 squash). PR CI44724810023 7종(1724개)·main CI44728486857 성공, tree `43bd1aa` 일치, 보호 변경/우회 없음, branch 정리. 같은 날 사용자 지시로 AI 저장소 병합 branch 29개·플랫폼 F-Lab-DevOps 병합/닫힘 branch 원격61·로컬80개 삭제(복구 SHA는 `~/.local/share/url-shortener-ai-ops/logs-20261120/`). 다음: A7 조각2 보관 정리 tick 연결.

## 2026-11-20 A7 프로세스 연결 조각1 Slack 복구 승인 경로·CI 반영 준비 (Claude)

사용자 허락으로 A7 프로세스 연결 시작, 결정: 승인 대기=명시 명령, 첫 조각=승인 경로. branch `claude/a7-process-wiring-20261120`(기준 main `9e70a3d`). 신규 `ai_ops.recovery_approval_wiring`: `@bot request-approval <사건|this> <계획hash>`(ACK는 정확 64hex만) → 계획 요청자·같은 채널/스레드/사건 확인 → WF06 `ledger.open` → 고정 문구 회신(예외 문구 없음). 계획 hash→요청 색인(`json_extract`, 정확 1건). 장부 `page`(시간 전이만, 열기·결정·재검증 없음)→A7-2 outbox 동기화(relay 5초)·틱당 전송1(실패 격리, 종료 중 전송 없음). ingress `SlackRouter`가 `/slack/interactivity` 장착(서명 키만, bot 토큰 없음). `recovery-approval.json` 없거나 `enabled:false`면 기존 동작, 잘못되거나 grants 밖 승인자면 시작·check-config 실패. 관측 포트는 `observation_not_connected`(항상 실패)라 실제 배포에선 승인 대기가 열리지 않음(fail-closed). 후속: PR 경로 현재 상태 관측기, 승인 뒤 재검증·WF07, execute_command, 보관 tick, renderer 마운트·Slack Interactivity URL, 정책 전환. 시험17·변형 12/12. 실제 배포·Slack·장애·인프라 없음.

## 2026-11-20 원복 실행 조각2 기준선 고정 원복 실행기 CI/main 반영 완료 (Claude)

[PR #53](https://github.com/joungGo/url-shortener-ai-ops/pull/53) head `f9267cfe4a9c1a4e7313da30dbb2c324aad5b072`를 head 고정 일반 squash 병합해 main `9e70a3dd96fd1d51453b3b61afba64797c5791da`에 반영했다. PR CI44669209443 7종(1707개)·main CI44669722156 성공, tree `b621d1a1bda0fe657ac036fe9fef8e4e96ac182a` 일치. 보호 변경/우회 없음. 병합 후 branch 정리(복구용 SHA는 inventory). 최종 메타데이터(이 기록·작업표·구현 문서 최종 절·검증 JSON)는 병합 후 로컬 갱신이다.

원복 경로(관측 PR51·드리프트 잠금 PR52·실행기 PR53) 로컬 계약 완료. 다음 후보: A7 프로세스 연결(승인 정책·Slack 수신 경로·outbox 전송·관찰 시작/봉인 hash 기록·보관 정리 tick·잠금 해제 확인·실제 PromQL/규칙·RBAC 설계: pods/proxy·applications patch 포함), 실제 Argo 쓰기 포트·인증 승인 조회기, A8 WF12 문서. **"뭐야" 질문은 답만, 구현은 명시 허락 후.** 실제 배포·Slack·장애·인프라 없음.

## 2026-11-20 원복 실행 조각2 기준선 고정 원복 실행기·CI 반영 준비 (Claude)

llm-council 결정(원복=기준선 고정 sync 1회, 쓰기 전 드리프트 잠금, 실패=시도 소진→수동 인계)의 두 번째 조각. 신규 `ai_ops.rollback_executor.RollbackExecutor.execute_local`(local_fixture 포트·Argo `applications/lab-target` get/patch 신원만): 예약 전 WF09가 이 정확한 `rollback_candidate`(범위 hash·change 일치, 취소·결과·관측 시도 없음)이고 범위가 미철회·만료 전·미사용·실행 hash/대상/UID 결합인지 확인 → 계획당 1회 예약(이후 `rollback_already_attempted`) → 배포/기준선 `ai-ops/lab` 트리가 values.yaml만 다르고 정규 실패→기준선 digest뿐인지, Argo source 정확 일치(override 없음)·수동 sync·진행 중 operation 없음·실패 digest 실행 중, Lease 비어 있을 때만 → 드리프트 잠금 → 실시간·Lease·범위·결정 재확인 → pin 1회 → 확정 충돌 `not_applied`/그 외·미확인 `needs_review` → 고정 확인·Lease 반환 `pinned_unverified`(sync·회복은 WF09 원복 관측). 쓰기 전 실패 시 Lease 미반환(WF08 동일), 잠금 후 중단 시 잠금 유지(fail-closed).

신규 시험11(실제 WF07/WF09·로컬 Git), 결함 변형 9/9, 전체1707 통과. 상세 `ai-ops/docs/rollback-executor-local-20261120.md`·검증 JSON. 실제 Argo 쓰기 포트·RBAC·인증 승인 조회기는 이후. 이 커밋 시점 PR CI/main 대기. 실제 배포·Slack·장애·인프라 없음.

## 2026-11-20 원복 실행 조각1 GitOps 드리프트 잠금 CI/main 반영 완료 (Claude)

[PR #52](https://github.com/joungGo/url-shortener-ai-ops/pull/52) head `d7fd6c82296ec25bd1c1136a6d117bc56cac9750`를 head 고정 일반 squash 병합해 main `564571836e33c71540313645308c258a99d3e126`에 반영했다. PR CI44665694359 7종(1696개)·main CI44666355792 성공, tree `98eab6fb4a8c4bd56dc13e7d4e9fd9e73c46d262` 일치. 보호 변경/우회 없음. 병합 후 branch 정리(복구용 SHA는 inventory). 최종 메타데이터(이 기록·작업표·구현 문서 최종 절·검증 JSON)는 병합 후 로컬 갱신이다.

다음 후보: 원복 실행 조각2 원복 실행기(실행 직전 재검증: 승인 재조회·조상/digest-only diff·source 밖 override·진행 중 Argo 작업·Lease/UID/source → 드리프트 잠금 → 기준선 고정 sync 1회 → 실패=시도 소진→수동 인계, 실제 실행 차단), A7 프로세스 연결, A8 WF12 문서. **"뭐야" 질문은 답만, 구현은 명시 허락 후.** 실제 배포·Slack·장애·인프라 없음.

## 2026-11-20 원복 실행 조각1 GitOps 드리프트 잠금·CI 반영 준비 (Claude)

llm-council 결정(원복=기준선 고정만, 원복 전 잠금, main 정렬은 별도 승인)의 첫 조각. 기준선 고정 원복 뒤 main에는 실패 변경이 남아, 다음 WF07이 오염된 main 위에서 만들어지면 "main head==배포 SHA" 검사를 오히려 통과해 재배포한다. 신규 `ai_ops.gitops_drift_lock`: `engage`(대상별 1개, 같은 계획·범위·저장소면 멱등, 다른 원복은 충돌, 해제 후 재잠금), `release_if_aligned`(기존 `_main_sha`/`_lab_tree`로 main `ai-ops/lab` Git 트리==기준선 트리일 때만 해제·main SHA/트리 기록; 다름·읽기 실패·확인 중 main 변경이면 유지, 예외 문구 없음), `assert_unlocked(db)`. 만료·비인증 수동 해제 없음(fix-forward 해제는 이후 인가 경로). WF07 `recovery_pr`: `_ready_in_db`·`_prepared_authority`·preflight/병합/동기화 계획·기록 최종 트랜잭션 4곳에서 잠금 중 fail-closed(읽기 전용·생성된 PR 기록은 미차단).

신규 시험9(실제 WF07·로컬 Git), 결함 변형 7/7, 전체1696 통과. 상세 `ai-ops/docs/gitops-drift-lock-local-20261120.md`·검증 JSON. 다음 조각2 원복 실행기(직전 재검증·잠금·기준선 고정 sync 1회·실패=시도 소진→수동 인계, 실제 실행 차단). 이 커밋 시점 PR CI/main 대기. 실제 배포·Slack·장애·인프라 없음.

## 2026-11-20 PR 경로 원복 관측 CI/main 반영 완료 (Claude)

[PR #51](https://github.com/joungGo/url-shortener-ai-ops/pull/51) head `d375e4bf35203e567f438a33a2a2b38b8b78d75a`를 head 고정 일반 squash 병합해 main `e16b61494fa8d5644d82670a08e23ded43ab34c6`에 반영했다. PR CI44657902367 7종(1687개)·main CI44658449019 성공, tree `f8bca149d56c87c537740fefcb3bc7faecc63179` 일치. 보호 변경/우회 없음. 병합 후 branch 정리(복구용 SHA는 inventory). 최종 메타데이터(이 기록·작업표·구현 문서 최종 절·검증 JSON)는 병합 후 로컬 갱신이다.

다음 후보: 원복 실행 조각(llm-council 결정 — 기준선 고정만, 조각1 gitops_drift_lock: 원복 전 잠금·잠금 중 WF07 PR 생성/sync fail-closed·해제=main ai-ops/lab 경로 트리 hash==scope.revision 트리; 조각2 원복 실행기: 실행 직전 재검증·sync 1회·실패=시도 소진→수동 인계, 실제 실행 차단), A7 프로세스 연결, A8 WF12 문서. **"뭐야" 질문은 답만, 구현은 명시 허락 후.** 실제 배포·Slack·장애·인프라 없음.

## 2026-11-20 PR 경로 원복 관측·CI 반영 준비 (Claude)

WF09 `record_rollback_local`에 실제 원복 영수증·관찰 표본을 공급한다. 사용자 결정(권장안 1): 원복 적용은 클러스터 관측으로 판정하고 GitHub main==원복 revision은 판정이 아닌 인계 정보로 기록(승인 범위 revision은 승인 시 고정된 기존 커밋이라 WF07의 main==배포 SHA를 그대로 쓰면 기준선 고정 원복이 항상 검증불가). `restart_effects` 관찰기 PR 영수증 확장(Argo 앱 tick별 GET, `argo_state`: targetRevision·동기화·마지막 성공 작업 revision 일치+Healthy+삭제 중 아님, 앱 UID 변경 시 중단). 신규 `ai_ops.rollback_observe`: `RollbackAppliedReader.applied`(mode=kubernetes; 같은 앱·승인 revision 고정·동기화·성공·Healthy·수동 sync·같은 Deployment·generation 반영·`api` 단일 컨테이너가 승인 digest일 때만 영수증, 반영 시각=finishedAt·미래 거절, 안정 사실 hash로 재조회 동일), `alignment`(허용 저장소만 기존 `_main_sha`로 True/False/None), `RollbackObservationReader`(mode=sealed_capture). WF09: `rollback_note` 저장(판정 아님, prune 시 삭제), result_text 한 줄. 판정 규칙 불변, 조정기는 계속 local_fixture만.

신규 시험8(실제 WF07/WF09 fixture), 결함 변형 7/7, 전체1687 통과. 상세 `ai-ops/docs/rollback-observe-local-20261119.md`·검증 JSON. 원복 실행 방식은 llm-council로 A(기준선 고정)만·원복 전 gitops_drift_lock·main 정렬은 별도 승인으로 결정(구현 전, 다음 조각). 이 커밋 시점 PR CI/main 대기. 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 A7-5 복구 장부·증거·봉인 파일 주기 보관 정리 CI/main 반영 완료 (Claude)

[PR #50](https://github.com/joungGo/url-shortener-ai-ops/pull/50) head `21b060c3ada218c0d69818386e22c9c825bd5f99`를 head 고정 일반 squash 병합해 main `ce68c5bd761db02ca1941f1aa79f9612e1d8b62d`에 반영했다. PR CI44536959534 7종(1679개)·main CI44537829472 성공, tree `3a1f5902a1e5f6e4eb204177c05b33e5720e4271` 일치. 보호 변경/우회 없음. 병합 후 branch 정리(복구용 SHA는 inventory). 최종 메타데이터(이 기록·작업표·구현 문서 최종 절·검증 JSON)는 병합 후 로컬 갱신이다.

A7 남은 조각: PR 경로 원복 관측(GitHub/Argo), 프로세스 연결(승인 정책·수신기·outbox·관찰 시작·봉인 hash 장부 기록·보관 정리 tick·실제 PromQL/규칙·`pods/proxy` RBAC). 그다음 A8 WF12 문서 초안. 조각별 PR. **"뭐야" 질문은 답만, 구현은 명시 허락 후.** 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 A7-5 복구 장부·증거·봉인 파일 주기 보관 정리·CI 반영 준비 (Claude)

조사: 복구 장부 4종의 `prune()`을 주기 호출하는 곳이 없고, 재시작 증거(Pod 로그 포함)·효과 봉인 파일은 삭제 경로가 없었다. `recovery_outbox`는 Slack 요청 만료 후에도 메시지 본문을 남기고 행을 지우지 않아 10000행 상한에서 enqueue가 영구 거절될 수 있었다. 신규 `ai_ops.recovery_retention.RecoveryRetention.tick()`: 장부 prune(재시작 장부 먼저·중복 호출 없음, tombstone 의미 불변) → outbox prune → 증거(live 실행 참조는 유지, 만료 실행만 참조하면 삭제, 미참조·`.tmp-*`는 7일 후) → 봉인(7일 후). 본인 0700 디렉터리 바로 아래 본인 일반 `<sha256>.json`/`.tmp-*`만, symlink·하위 디렉터리·다른 이름은 개수만, 없는 디렉터리 미생성, 7일 미만 기간 거절, 실패는 이름만(다음 주기 재시도). `RecoveryOutbox.prune()`: Slack 처리 잠금에서 만료·부재 요청 행 삭제(전송은 요청 재확인이라 재전송 없음).

신규 시험8, 결함 변형 5/5(+동등1), 전체1679 통과. 상세 `ai-ops/docs/recovery-retention-local-20261119.md`·검증 JSON. tick 호출 프로세스·간격·경로/PVC, 봉인 hash 장부 기록, tombstone 장기 정리는 이후. 이 커밋 시점 PR CI/main 대기. 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 A7-4 재시작 효과 관찰·봉인·Lease 회수 확인 CI/main 반영 완료 (Claude)

[PR #49](https://github.com/joungGo/url-shortener-ai-ops/pull/49) head `6e4f5ef147ca2d0f3a32781700ae0bdcd846224d`를 head 고정 일반 squash 병합해 main `3c00eeecc97a2b273b713bfd247f997c94461a93`에 반영했다. PR CI44532528034 7종(1671개)·main CI44533398664 성공, tree `0e976753b4ff9ed92d9669b161748de0dca7ca95` 일치. 보호 변경/우회 없음. 병합 후 branch 정리(복구용 SHA는 inventory). 최종 메타데이터(이 기록·작업표·구현 문서 최종 절·검증 JSON)는 병합 후 로컬 갱신이다.

A7 남은 조각: 5 주기 보관 정리(증거·봉인 파일 포함), PR 경로 원복 관측(GitHub/Argo), 프로세스 연결(승인 정책·수신기·outbox·관찰 시작·실제 PromQL/규칙·`pods/proxy` RBAC). 조각별 PR. **"뭐야" 질문은 답만, 구현은 명시 허락 후.** 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 A7-4 재시작 효과 관찰·봉인·Lease 회수 확인·CI 반영 준비 (Claude)

llm-council로 최초 혼합 리더안(Prometheus range query + K8s 1회 조회)을 철회했다: 1회 스냅샷 복제는 증거 위조, query_range step은 lookback 보간이라 45초 간격 검사를 거짓 통과, HTTP 응답계약·경보 평가상태는 과거 재구성 불가, 15초는 읽기 1회 제한. 신규 `ai_ops.restart_effects`: `RestartEffectObserver`(mode=kubernetes, 잠금 없음 — WF08이 검증 전 Lease 반납)가 적용+`probe_window` 이후 구간에서 간격(5~20초)마다 K8s runtime(종료 중 Pod 포함·실행 이미지 digest·marker)·API 서버 Pod proxy `/readyz`·`/items`(C-04 `valid_body`, 비200은 unknown)·설정 PromQL(C-04 `metric_value` 신선도)·설정 경보 규칙(`/api/v1/rules`, 정의 hash 기록)을 읽는다. 간격+읽기≤gap·span 도달·보관 기한·창 시작 조건은 수집 전에 거절하고, 실패한 series만 collection_failed, 대상 UID 변경·취소는 전부 unknown. 원시 응답은 sha256만(본문 미저장). `EffectCaptureStore`(0700/0600 sha256 봉인)·`SealedEffectReader`(mode=sealed_capture, O_NOFOLLOW·hash·영수증 결합)·`LeaseCleanupPort`/`restart_lease_item`(반납 확인, 자기 것만 resourceVersion으로 비움). WF09 `EffectBatch.provenance`에 `captured`(run_id·collector·표본 hash 필수) 추가, 판정 규칙 불변. WF09 조정기는 여전히 local_fixture만 받아 실제 읽기/회수 차단 유지.

신규 시험17, 결함 변형 6/6 검출, 전체1671 통과(skip1). 상세 `ai-ops/docs/restart-effects-adapter-local-20261119.md`·검증 JSON. 연결 단계 기록: 읽기 신원에 `pods/proxy` get 필요(lab 차트에 Service 없음). PR 원복 관측·A7-5 보관 정리·프로세스 연결·RBAC/VAP·실제 PromQL·shadow·live는 이후. 이 커밋 시점 PR CI/main 대기. 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 A7-3a 재시작 Kubernetes 어댑터 CI/main 반영 완료 (Claude)

[PR #48](https://github.com/joungGo/url-shortener-ai-ops/pull/48) head `9124203650e859cdb18de4793ba800e904179f3a`를 head 고정 일반 squash 병합해 main `13d94e9a3efc8432480291ef9bbc1461e6899c60`에 반영했다. PR CI44525442506 7종(1654개)·main CI44526171563 성공, tree `d075338c571214f4a4fa8fdfdf7191620501cb59` 일치. 보호 변경/우회 없음. 병합 후 branch 정리(복구용 SHA는 inventory). 최종 메타데이터(이 기록·작업표·구현 문서 최종 절·검증 JSON)는 병합 후 로컬 갱신이다.

A7 남은 조각: 4 효과·원복·회수 어댑터, 5 주기 보관 정리(증거 파일 포함), 프로세스 연결(승인 정책·수신기·outbox·실제 PromQL). 조각별 PR. **"뭐야" 질문은 답만, 구현은 명시 허락 후.** 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 A7-3a 재시작 Kubernetes 어댑터·CI 반영 준비 (Claude)

신규 `ai_ops.restart_k8s`: 고정 경로 `ApiClient`(토큰 요청별 재읽기·CA TLS·리다이렉트 미추종·2MiB·deadline)로 읽기(Deployment·Pod·로그·이벤트·HPA·Argo GET)/쓰기(lab-api PATCH·Lease GET/PUT)/Prometheus(query) 신원을 분리했다. `derive_snapshot`이 3b 원시 관측값(restartCount·Ready 실패 시작·probe 창·전략·restart annotation 제외 revision hash 등)을 만들고, Argo를 못 읽으면 자동 동기화로 간주하는 등 불확실하면 차단한다. `PrometheusSignals`는 설정 PromQL만(미구성·빈 결과=unknown), 증거는 0700/0600 파일에 저장하고 바이트 hash만 반환, PATCH는 봉인 형태만, Lease는 비었을 때만 획득·자기 것만 반환. mode=`kubernetes`라 WF08 실행기가 거절해 실제 쓰기는 계속 차단.

진행 중 PromQL `str.format` 중괄호 결함(과거 Ready 항상 unknown) 발견·수정. 대상 사실: lab-api livenessProbe 없음·컨테이너 api·readiness 5초·Recreate·DB/Redis 의존. 신규 시험15, 전체1654 통과. 상세 `ai-ops/docs/restart-k8s-adapter-local-20261119.md`·검증 JSON. 실제 PromQL·토큰/RBAC/VAP·shadow·live 모드는 이후. 이 커밋 시점 PR CI/main 대기. 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 A7-3b 재시작 사전 게이트 CI/main 반영 완료 (Claude)

[PR #47](https://github.com/joungGo/url-shortener-ai-ops/pull/47) head `8c86507b45c5221c15a0a5e0501cf27ee2ef508f`를 head 고정 일반 squash 병합해 main `6a6605d11b86a7b438286f8664d3341ae39e678a`에 반영했다. PR CI44522308075 7종(1639개)·main CI44523032690 성공, tree `713fd3e069565d693054f1cb049d4fda0d74ae17` 일치. 보호 변경/우회 없음. 병합 후 branch 정리(복구용 SHA는 inventory). 최종 메타데이터(이 기록·작업표·구현 문서 최종 절·검증 JSON)는 병합 후 로컬 갱신이다.

A7 남은 조각: 3a 실제 K8s 어댑터(원시 관측 도출·증거 수집·PATCH·Lease, 가짜 API 서버), 4 효과·원복·회수, 5 주기 보관 정리, 프로세스 연결. 조각별 PR. **"뭐야" 질문은 답만, 구현은 명시 허락 후.** 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 A7-3b 재시작 사전 게이트 재설계·CI 반영 준비 (Claude)

WF08 실행 전 진단 4종은 실제 도출 정의가 없고 fixture가 통과값을 넣고 있었다. llm-council로 재설계를 결정했고, 사실 확인으로 대상 lab-api가 `Recreate`라 재시작 시 기존 Pod가 먼저 종료되어 증거가 반드시 사라짐을 확인했다. 신규 `ai_ops.restart_gates`(순수·결정적, pass/fail/unknown, unknown은 통과 아님)로 계획 이후 변경 없음(배포 전략 포함)·Ready 0·경쟁 컨트롤러 없음·readiness 실패 지속·liveness 정상(restartCount 불변)·과거 Ready 이력·의존성(실패만 차단)을 판정한다. image_available·capacity_verified 삭제(표시만), configuration_verified는 지문 게이트로 흡수. 스냅샷은 원시 관측값만 받고 계획에 strategy·restart_count를 봉인한다. PATCH 직전 증거 보존(describe·events·logs current/previous·metrics·status) 필수 게이트, 게이트 거절은 `gate_rejected` 인계 기록(값 없음)·재시도 없음.

시험39(거절16종·증거5종), 의도적 결함 변형 2종 검출, 효과 포함73·전체1639 통과. 상세 `ai-ops/docs/restart-gates-local-20261119.md`·검증 JSON. 실제 쓰기 차단 유지. 이 커밋 시점 PR CI/main 대기. 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 A7-2 복구 결과 outbox CI/main 반영 완료 (Claude)

[PR #46](https://github.com/joungGo/url-shortener-ai-ops/pull/46) head `b2a97a5153f94505ccb5d69d9099aee97abce80b`를 head 고정 일반 squash 병합해 main `8fa3ad2b38b88cbe31496ad81c1ede3644cb7bdb`에 반영했다. PR CI44504060937 7종(1634개)·main CI44504869626 성공, tree `534bae66adb832065fa4e839560019e93d63d932` 일치. 보호 변경/우회 없음. 최종 메타데이터(이 기록·작업표·구현 문서 최종 절·검증 JSON)는 병합 후 로컬 갱신이다.

A7 남은 조각: 3 진단·쓰기·잠금 어댑터(고정 명령 1종의 실제 Kubernetes 읽기·PATCH·Lease, 정확 대상 권한), 4 효과·원복·회수, 5 주기 보관 정리. 조각별 PR. **"뭐야" 질문은 답만, 구현은 명시 허락 후.** 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 A7-2 복구 결과 outbox·CI 반영 준비 (Claude)

신규 `ai_ops.recovery_outbox`: 승인 장부 view만으로 원래 계획 요청 thread에 고정 템플릿 메시지를 만든다(승인 대기 → [승인][거절] 버튼, 승인 → [취소] 버튼, 거절·취소·만료·대체·이미 정상·사람 확인·준비 완료 → 텍스트). 대상·generation·상태 기반 결정적 키로 한 번만 저장한다. 전송은 기존 회신 규칙·처리 잠금을 따르며 429만 최대 3회 재시도, 확정 거절 failed, 모호·중단 needs_review(재전송 없음), 인가 철회 withdrawn. 버튼 블록은 `approval_blocks`로만 만들고 `blocks_valid`가 재생성 일치만 허용하며 `post_payload_valid`(sender 동일)가 정규 블록만 통과시킨다.

신규 시험8(변조 블록 8종 거절), 관련224·전체1634 통과. 상세 `ai-ops/docs/recovery-outbox-local-20261119.md`·검증 JSON. 프로세스 연결과 WF08/09 알림은 이후 조각. 이 커밋 시점 PR CI/main 대기. 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 A7-1 복구 승인 Slack 버튼 수신 CI/main 반영 완료 (Claude)

[PR #45](https://github.com/joungGo/url-shortener-ai-ops/pull/45) head `56e0d309e333f81dd49ad2136f6ba3eadfbc7d75`를 head 고정 일반 squash 병합해 main `0619651393b5792da03f9a86d561e502560d5e81`에 반영했다. PR CI44500818121 7종(1626개)·main CI44501492963 성공, tree `5b9d01a938163d7b0cbe279a2a2308b71521b9d6` 일치. 보호 변경/우회 없음. 최종 메타데이터(이 기록·작업표·구현 문서 최종 절·검증 JSON)는 병합 후 로컬 갱신이다.

A7 남은 조각: 2 결과 outbox(버튼 있는 승인 요청·진행/결과 회신, sender 경로 재사용), 3 진단·쓰기·잠금 어댑터, 4 효과·원복·회수, 5 주기 보관 정리. 조각별 PR. **"뭐야" 질문은 답만, 구현은 명시 허락 후.** 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 A7-1 복구 승인 Slack 버튼 수신·CI 반영 준비 (Claude)

A7(WF07~09 실제 어댑터)을 5조각으로 나눠 하나씩 진행한다(사용자 결정). 1조각: 신규 `ai_ops.recovery_slack_approval`이 서명된 Slack interactivity(`block_actions`)를 서명·시각·form·팀/App/채널·정책 (사용자, 채널) 쌍으로 검증한 뒤 WF06 장부 `decide`만 호출한다. 버튼 값에 정확한 장부 대상(plan/approval hash·generation)과 범위(준비/명령 실행)를 담고, request_id는 Slack 식별자 hash로 멱등이다. 재검증·실행·전송 없음, execution_allowed=false. `SlackInteractivityApp`(/slack/interactivity)은 기존 ACK 제약을 재사용하고 서명 검증은 `verify_slack_request`로 공용화했다(동작 동일).

진행 중 회귀(adapter.receive를 생성 시 바인딩해 기존 시험이 멈춤)를 발견·수정했다. 신규 시험11, 전체1626 통과. 상세 `ai-ops/docs/recovery-slack-approval-local-20261119.md`·검증 JSON. 승인 요청 메시지 전송은 A7-2, 프로세스·Slack App 연결은 이후 조각. 이 커밋 시점 PR CI/main 대기. 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 WF-10 전환·원복 변경 계획 형식(A6) CI/main 반영 완료 (Claude)

[PR #44](https://github.com/joungGo/url-shortener-ai-ops/pull/44) head `5b699bc5356cd2f66f51a1efef9e3b2c47100196`를 head 고정 일반 squash 병합해 main `7b6dcb19caca9156c492cb51c19a5a3a35cfd76a`에 반영했다. PR CI44491923061 7종(1615개)·main CI44492763462 성공, tree `d8a0beb9eb36336bf920b6503b393cb51e79245f` 일치. 보호 변경/우회 없음. 최종 메타데이터(이 기록·작업표·계획 문서 최종 절·검증 JSON)는 병합 후 로컬 갱신이다.

남은 인프라 불필요 후보: A7 WF07~09 실제 어댑터 로컬 구현, A8 WF12 문서 초안. **"뭐야" 질문은 답만, 구현은 명시 허락 후.** 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 WF-10 전환·원복 변경 계획 형식(A6)·CI 반영 준비 (Claude)

신규 `ai_ops.wf10_change_plan`: 검증된 후보 패키지 1개에서 적용 순서(ConfigMap→NetworkPolicy→Service→Deployment sender→main)·리소스별 hash·probe 기반 시작 허용 시간·사전/사후 확인과 역순 원복 묶음(`part-of=wf10`+기록 uid 일치 시에만 삭제, PVC·Secret·namespace·SA·RBAC·Ingress 금지, 삭제 전 DB 보존·자동 복원 없음)을 데이터로 생성한다. `rollback_bundle_sha256`·`change_plan_sha256`으로 이후 별도 실제 승인이 묶일 형식을 제공하며 명령 문자열·실행기 없음, execution_allowed=false.

신규 시험7, 전체1615 통과. 상세 `ai-ops/docs/wf10-change-plan-20261119.md`·검증 JSON. 이 커밋 시점 PR CI/main 대기. 실제 hash·적용·uid 기록은 인프라 단계. 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 WF-10 manifest 오프라인 schema 검증(A5) CI/main 반영 완료 (Claude)

[PR #43](https://github.com/joungGo/url-shortener-ai-ops/pull/43) head `552bb869b960eb60320c1ba66579c194708f9460`를 head 고정 일반 squash 병합해 main `5c74985973b71400000d408ae07e1cd69c410176`에 반영했다. PR CI44489431925 7종(1608개)·main CI44490014251 성공, tree `3c3fd0d427984b4b82750c8fdeb2dc4c2c3f41fc` 일치. 보호 변경/우회 없음. 최종 메타데이터(이 기록·작업표·검증 문서 최종 절·검증 JSON)는 병합 후 로컬 갱신이다.

남은 인프라 불필요 후보: A6 전환·원복 적용안 형식, A7 WF07~09 실제 어댑터 로컬 구현, A8 WF12 문서 초안. **"뭐야" 질문은 답만, 구현은 명시 허락 후.** 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 WF-10 manifest 오프라인 schema 검증(A5)·CI 반영 준비 (Claude)

renderer가 만드는 리소스 8개를 클러스터 없이 Kubernetes v1.35.8 구조 기준으로 검증한다. 새 바이너리·라이브러리 없이 공식 OpenAPI v3에서 4종 전이 스키마 138개만 추출해 `ai_ops/k8s_openapi_v1_35_8.json`으로 두고(원본 sha256 고정, `scripts/build_k8s_schema_subset.py --check`), `ai_ops.k8s_schema`가 strict unknown 필드·타입·필수·범위·int-or-string을 검사한다. renderer는 출력 전 전부 검증하고 실패 시 `wf10_schema_rejected`로 거절하며, live gate에 `server_side_dry_run`을 추가했다(서버 dry-run 대체 아님).

신규 시험6(위반 12종 거절), manifest hash 불변, 전체1608 통과. 상세 `ai-ops/docs/wf10-manifest-schema-validation-20261119.md`·검증 JSON. 이 커밋 시점 PR CI/main 대기. 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 SQLite 보조 파일 삭제 경쟁 수정 CI/main 반영 완료 (Claude)

[PR #42](https://github.com/joungGo/url-shortener-ai-ops/pull/42) head `a45c053b15b5141ea7dfba0539119135194e7d29`를 head 고정 일반 squash 병합해 main `f6e04337239a6f981745674ff390ce8b89e37f81`에 반영했다. PR CI44486424872 7종(1602개)·main CI44487170865 성공, tree `15e36202953b3f5364325833e8baa4261d2aead3` 일치. 보호 변경/우회 없음. 최종 메타데이터(이 기록·작업표·수정 문서 최종 절·검증 JSON)는 병합 후 로컬 갱신이다.

남은 인프라 불필요 후보: A5 오프라인 schema 검증, A6 전환·원복 적용안 형식, A7 WF07~09 실제 어댑터 로컬 구현, A8 WF12 문서 초안. **"뭐야" 질문은 답만, 구현은 명시 허락 후.** 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 SQLite 보조 파일 삭제 경쟁 수정·CI 반영 준비 (Claude)

간헐 실패하던 `test_recovery_commands` 동시 실행 시험을 부하 재현·예외 추적한 결과 **제품 경쟁 조건**이었다. SQLite가 마지막 연결 종료 때 `-wal`/`-shm`을 지우는 순간 다른 스레드·프로세스의 `lstat`이 `nlink 0`을 보고, 안전 검사가 위험 파일로 오판해 정상 작업을 거절했다(fail-safe였지만 무작위 거절). 사용자에게 보고·허락 후 4곳(`job_queue`, `slack_adapter`, `observability`, `continuous_model`)에서 보조 파일 `nlink 0`만 부재로 간주(하드링크·권한·소유자·형식 검사와 본 DB 엄격 유지), `continuous_model` exists/lstat 경쟁 수정.

신규 시험4(수정 전 코드에서 실패 확인), 부하 재현 160/160 정상(이전 80회 중 실패1·오판 거절3), 관련122·전체1602 통과. 상세 `ai-ops/docs/sqlite-sidecar-race-fix-20261119.md`·검증 JSON. 이 커밋 시점 PR CI/main 대기. 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 WF-10 Slack sender 분리(B′) CI/main 반영 완료 (Claude)

[PR #41](https://github.com/joungGo/url-shortener-ai-ops/pull/41) head `5ec3f9d50bd5dad1192ca644220e740d4842d326`를 head 고정 일반 squash 병합해 main `ceb6c277b6a968c5be046657dc04137695e1a836`에 반영했다. PR CI44482224455 7종(1598개)·main CI44482913902 성공, tree `24a2ea8c937da307ac3a2b4fc307249198cca7cd` 일치. 보호 변경/우회 없음. 최신 main에서 새 branch `claude/wf10-slack-sender-20261119`로 두 커밋을 옮겨 PR40 이력 중복을 피했다(이전 branch는 원격에 보존). 최종 메타데이터(이 기록·작업표·구현 문서 최종 절·검증 JSON)는 병합 후 로컬 갱신이다.

남은 인프라 불필요 후보: A2 inventory 기존 경로 정정, A5 오프라인 schema 검증, A6 전환·원복 적용안 형식, A7 WF07~09 실제 어댑터 로컬 구현, A8 WF12 문서 초안, 기존 flaky `test_recovery_commands` 동시성 시험 안정화(신규 발견). **"뭐야" 질문은 답만, 구현은 명시 허락 후.** 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 WF-10 Slack sender 분리(B′) 구현·CI 반영 준비 (Claude)

Slack 송신 경로를 운영 기준으로 재설계했다. Slack은 고정 IP가 없고, 플랫폼은 관리형 노드그룹이라 AWS 도메인 정책(Auto Mode 전용)을 쓸 수 없다. llm-council 결과 **B′(무상태 sender 분리)**로 결정했다. 신규 `ai_ops.slack_sender`가 bot 토큰을 가진 유일한 프로세스이며 메서드3·허용 채널·멱등·분당60건을 집행한다. relay는 bot 토큰 없이 `SlackSenderClient`로 보낸다. renderer는 메인 Pod 인터넷 송신0, sender만 443(사설·IMDS 제외)으로 리소스8개를 생성하고 입력 `slack_cidrs` 삭제·`secrets.sender_client` 추가.

신규 시험12개, 전체1598개 2회(1회차 기존 flaky `test_recovery_commands` 동시성 1건 — 변경 무관, 2회차 전부 통과). 상세 `ai-ops/docs/wf10-slack-egress-design-20261119.md`, `wf10-slack-sender-local-20261119.md`, 검증 JSON. 이 커밋 시점 PR CI/main 대기. 플랫폼 strict 모드·IMDS hop limit·EIP 보존·Slack 토큰 IP 제한·실클러스터 음성 시험은 인프라 단계. 잔여 위험(slack.com·DNS 유출, 내부 평문, 메모리 멱등, HA 없음) 문서화. 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 WF-10 후보 renderer 보강 CI/main 반영 완료 (Claude)

[PR #40](https://github.com/joungGo/url-shortener-ai-ops/pull/40) head `6920a0703736b896b74fe3b643bc7d8033d91b3c`를 head 고정 일반 squash 병합해 main `1f20442e2a70eb66a6228275add9f564cdb37c06`에 반영했다. PR CI44474606794 7종(1586개)·main CI44475106909 성공, tree `ac77ac84b546f48904dea0a9f98f4c24ccc571b5` 일치. 보호 변경/우회 없음. 최종 메타데이터(이 기록·작업표·보강 문서 최종 절·검증 JSON)는 병합 후 로컬 갱신이다.

남은 인프라 불필요 작업: A2(inventory 기존 경로 정정), A4(Slack egress 경로 설계), A5(오프라인 schema 검증), A6(전환·원복 적용안 형식), A7(WF07~09 실제 어댑터 로컬 구현), A8(WF12 문서 초안). **사용자 지침: "뭐야" 질문은 답만, 구현은 명시 허락 후 시작.** 실제 배포·Slack·장애·인프라·Terraform·유료 모델 실행 없음.

## 2026-11-19 WF-10 후보 renderer 보강·CI 반영 준비 (Claude)

main4b54777 기반 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf10-renderer-hardening-20261119` / `claude/wf10-renderer-hardening-20261119`. 네 프로세스 livenessProbe(30/5/3, readiness와 같은 probe)·init resources(10m/32Mi~100m/64Mi) 추가, 시험1개. 집중83·전체1586(212.6초, skip1=WF03 환경 게이트) 통과. PR39 병합 후 메타데이터도 이 작업트리로 이관했다. 이 커밋 시점 PR CI/main 대기. 사용자 지침: "뭐야" 질문은 답만, 구현은 명시 허락 후 시작. 실제 배포·Slack·장애·인프라 없음.

## 2026-11-19 WF-10 활성 후보 검토·CI/main 반영 완료 (Claude)

[PR #39](https://github.com/joungGo/url-shortener-ai-ops/pull/39) head `0ff46cd19abdf346abf1205d14a5da8b15b01312`를 head 고정 일반 squash 병합해 main `4b54777a20880614403d19ee98b54bec9cc45263`에 반영했다. [PR CI44471952438](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44471952438) 7종(전체1585개, skip5=PR38과 동일)·push CI44471944816·[main CI44472709490](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44472709490) 성공. 검토/병합 tree `19a045da24eeb71e6082598e27f8ee33d02cb319` 일치. 보호 변경/우회 없음.

검토 P1(공유 PVC `fsGroupChangePolicy: OnRootMismatch` 누락) 수정 포함. 상세 `ai-ops/docs/wf10-active-config-review-20261119.md`·검증 JSON. 작업트리 `CLAUDE.md`는 로컬 전용으로 미커밋 보존. 최종 메타데이터(이 기록·검토 문서 최종 절·검증 JSON·작업표)는 병합 후 로컬 갱신이다. 실제 입력12개·연결 승인·실물 검증, WF10 실제 Slack 연결·WF11/C04/C05·WF12는 미완료. 배포·Slack·장애·인프라·Terraform·유료 모델 실행 없음.

다음 한 작업: **WF10 실제 입력12개 확정 준비**(재설치 요청·실행 주체 결정 후). 남은 검토 위험(liveness 부재·init resources·Slack egress 경로)은 실제 배치 결정 때 판단.

## 2026-11-19 WF-10 활성 후보 코드 검토·CI 반영 준비 (Claude)

Claude Code가 Codex 로컬 구현을 이어받아 검토했다. 실제 실행 모듈(worker/receiver/Slack ingress·relay)의 인자·경로·자격 권한 검사와 기존 chart를 대조했다. **P1 1건 수정:** 후보 Pod에 `fsGroupChangePolicy: OnRootMismatch`가 없어 공유 기존 PVC의 큐 0600/0700이 기동마다 0660/2770으로 넓어져 큐 검사가 실패할 수 있었다(B6 과거 사고와 같은 경로). 회귀 단언 추가. receiver 지연 import 정리(동작 동일).

집중82개·전체1585개(210.376초, skip1=WF03 환경변수 게이트 시험으로 CI 별도 job에서 실행)·경계/diff/컴파일·CLI build/verify/미확정 exit2 통과. 상세 `ai-ops/docs/wf10-active-config-review-20261119.md`와 검증 JSON. 이 커밋 시점 PR CI/main 대기. 이 저장소는 비공개 무료 플랜이라 브랜치 보호 API가 403(미제공)이며 보호 변경·우회 없이 정확 head CI 성공 후 일반 병합한다. 기존 시간·승인·원복 조건 유지, 실제 배포·Slack·장애·인프라·Terraform·유료 모델 없음.

## 2026-11-19 WF-10 활성 배포 후보 설정 로컬 완료

AI main50d0c1a 기반 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf10-active-config-20261119` / `codex/wf10-active-config-20261119`에서 wf10_deployment renderer·합성 시험20개·실제값 null 입력을 추가했다. 누락 입력은 리소스0/exit2/출력폴더 없음. 합성 완성 입력은 ConfigMap·단일 Pod Deployment(Recreate/공유 기존PVC)·내부 Service2·제한 NetworkPolicy 후보5개를 생성한다. 자격값 없이 역할별 Secret 참조/0600 복사, 기존 read-only SA, manifest 정합성 검증만 제공한다. 실제적용 실행기는 없고 execution_allowed=false다.

접수기만 런북 정책을 config hash에 누락하던 호환 오류를 수정했다. 기존 방식 사건 요청 거절·수정 방식 요청 성공을 로컬 SQLite로 재현/검증했다. 관련80개 선행·최종전체1585개(207.001초/skip0)·AST3·경계/diff 통과. 기존 시간·승인·원복·C단계 실행기 변경 없음.

상세 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf10-active-config-20261119/ai-ops/docs/wf10-active-config-local-20261119.md` 및 검증 JSON/작업표. 원래 변경과 WF10 최종 메타데이터는 보존했다. 새 코드/문서는 미커밋·push/원격CI/main 미반영. 실제 입력12개·이미지출처·물리PVC/권한/네트워크/TLS·새 정상CI/원복·연결 승인은 미확정이다. 설치·실제 배포·Slack·장애·Terraform·유료 모델 실행 없음.

다음은 **활성 후보 코드 검토·CI/main 반영**이다. 실제 연결·WF11/C04/C05는 후속이다. Claude 최신 체크포인트는 플랫폼 `docs/handoff/claude/CURRENT.md`. 추천 GPT-6 SOL · High.

## 2026-11-19 상시 Claude 인계 규칙 반영

사용자 요청에 따라 이후 Codex 작업의 시작·의미 있는 단계 완료·승인/로그인 대기·외부 장시간 작업 전후·최종 응답 전에 Claude 인계 체크포인트를 별도 요청 없이 갱신한다. 최신 진입점은 `/Users/jounghyeon/dev/Url-Shortener-EKS-Platform/docs/handoff/claude/CURRENT.md`, 절차는 `UPDATE-PROTOCOL.md`다. 양쪽 AGENTS와 CLAUDE에 연결했고 비밀값 복사/자동 commit·push·배경 예약 없이 기존 승인·원복 조건을 유지한다. 이번 변경은 로컬 문서만이며 실제 설치/삭제/배포/전송/실습 없음.

## 2026-11-19 Claude 인계 준비

같은 Mac의 Claude Code에서 전체 구현·검토/CI·설치/정리 작업을 이어갈 진입 자료를 `/Users/jounghyeon/dev/Url-Shortener-EKS-Platform/docs/handoff/claude/README.md`, `TASKS.md`, `inventory.json`에 작성했다. 양쪽 루트와 최신 WF10 작업트리에 `CLAUDE.md`를 추가했다. 루트의 오래된 HEAD/미커밋 자료와 최신 검토 코드 위치를 구분했으며 원본 변경/자격을 보존했다.

이번 작업은 로컬 문서 준비만 수행했다. 클라우드/실제 Slack/실습/배포/설치/삭제·원격 CI·commit/push 없음. 최신 WF10 main50d0c1a 및 최종 로컬 기록이 기준이며, 실제 연결/WF11/C04/C05 미완료를 유지한다. 일반 Terraform 사용자 터미널 원칙과 새 건별 위임, 기존 시간·승인·원복/보존 조건도 유지한다. Claude에서 먼저 인계 자료/도구/현재 상태를 확인하고 다음 한 작업을 정한다.

## 2026-11-19 WF-10 비활성 준비 검토·CI/main 완료

[PR #38](https://github.com/joungGo/url-shortener-ai-ops/pull/38) head `c0fa27311f9127eb856db2bf5f348d03e8f6f182`를 일반 squash 병합해 main `50d0c1a309698320bc958eafc3d294f38b023f01`에 반영했다. [PR CI44465091658](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44465091658) 7종·push CI44465054234·[main CI44465684513](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44465684513) 성공. main의 PR전용 gate skip은 설계대로이며 실제 PR gate는 통과했다. 검토/병합 전체 tree `50452341417945dc2a13e9ab17130d4be9dee228` 및 소스 hash 일치. 보호 변경/관리자 우회 없음.

검토상 수정 필요 오류 없음. 집중21개 재실행, 전체1565개 로컬 기존 결과/소스 해시 확인 및 PR CI 전체1565개 통과. Helm리소스0·Slack자격읽기전 거절·큐/Slack 두DB 보존·새 요청 미대조 원복차단을 검증했다. 기존 시간·승인·원복·잠금/ACK·회수 조건은 유지한다.

상세 AI 작업트리 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf10-connection-plan-20261119/ai-ops/docs/wf10-preparation-review-20261119.md`·검증 JSON·작업표. 코드·준비자료·검토문서는 main 반영했고 최종 병합/CI 메타데이터·인계는 로컬 갱신했다. 기존 작업트리 수정은 보존했다.

다음은 실제 입력12개와 활성 배포 renderer·전환/원복 적용안을 확정하는 단계다. 현재 자료는 실행기/실제 연결 승인이 아니며 WF10 실제 통지·조사·회신 검증과 WF11/C04/C05는 미완료다. 인프라 설치·실제 배포·Slack 전송·장애 실습·Terraform·유료 모델 호출 없음. 추천 **GPT-6 SOL · High**.

## 2026-11-19 WF-10 준비 코드 검토·CI 반영 준비

비활성 설정/전환·원복 자료를 검토했고 수정이 필요한 오류는 발견하지 못했다. 집중21개 재실행 통과, 전체1565개 기존 통과 기록과 현재 소스·산출물/원본 로그 hash 일치 확인. 상세 `ai-ops/docs/wf10-preparation-review-20261119.md` 및 검증 JSON. 이 커밋 시점 CI/main 대기이며 실제 배포·Slack·실습·설치 없이 진행한다. 기존 시간·승인·원복 조건 유지. 실제 입력12개/활성 배포 연결은 후속이다. GPT-6 SOL · High.

## 2026-11-19 WF-10 비활성 배포·전환/원복 자료 로컬 준비 완료

AI main1bea7d6 기반 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf10-connection-plan-20261119`에서 `ai_ops.wf10_preparation`·시험21개와 `ai-ops/config/wf10-disabled-review/`8파일을 준비했다. Helm enabled=false 렌더 리소스0, Slack disabled/빈sources로 자격 읽기 전에 거절. deployment/transition/rollback JSON은 검토 명세이며 Kubernetes 적용본·installer·복원 실행기가 아니다. inventory/파일 변조·enable 변경/미확정 삭제를 거절하고 외부 클라이언트/자격 사용은 없다.

실제 로컬 합성 큐/Slack 별도DB의 일관 backup/quick_check·새 빈 활성큐 jobs0/manual·registry대조·복제본schema전환·전환후 요청 보존을 시험했다. 실제 보존DB/키는 읽지 않았다. 새요청/모호한전송이 미대조면 원복 후보를 막고 수동 인계하며 모든 로컬 판정 execution_allowed=false다. 기존 경보 공지와 과거자료 보존 유지.

집중21개(0.197초)·전체1565개(209.958초/skip0)·AST2개·diff·경계/링크 검증 통과. 기존 추적 구현/설정/시험/워크플로429개·C단계75개/공유Lease 동일. 상세 `ai-ops/docs/wf10-disabled-preparation-20261119.md`, `wf10-disabled-preparation-validation-20261119.json`, 작업표.

실제 입력12개·활성 배포 renderer/원복 실물·이미지 빌드/등록·새 UID/TLS/CI는 미확정이다. 이 변경은 미커밋/원격CI/main 미반영. 다음은 **WF10 준비 도구·자료 검토·CI/main**이며 이후 확정안 승인으로 실제 연결한다. 기존 시간·승인·원복 유지. 인프라 설치·배포·Slack 전송·장애·Terraform·유료모델 실행 없음. 추천 **GPT-6 SOL · High**.

## 2026-11-19 WF-10 연결 계획 문서 준비 완료

AI main1bea7d6 기반 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf10-connection-plan-20261119`에서 `ai-ops/docs/wf10-connection-plan-20261119.md`·`wf10-connection-inputs-20261119.json`과 작업표/전체 계획을 갱신했다. 첫 범위는 기존 경보 채널 사실 공지→#ai-ops 사건 지정 조사→원래 요청 thread의 같은 사건 결과다. 공지와 사건 접수 독립·manual기본/자동예외0개·새 빈 활성 큐·과거DB 읽기 보존·호환 DB 원복을 전환안으로 정리했다.

정확 경보 channel ID·현재 bot 신원·source registry·새 digest/render/원복 hash·UID/TLS/정상CI·적용 승인은 미확정이다. 계획 문서만 완료됐으며 실행 준비/배포 완료가 아니다. 새 설정/이미지·installer는 생성하지 않았고 코드/자격/정책을 변경하지 않았다. WF09 완료 메타데이터를 이어받았고 이전 작업트리 수정은 보존했다.

다음은 인프라 없는 disabled 설정·전환/원복 자료 준비 및 검토다. 재설치와 실제 Slack 합성 경보/사용자 요청·회신 검증은 별도 요청/확정안 승인 후 진행한다. WF07~09 실제 쓰기/승인/효과·회수 연결·WF11/C04/C05는 별도 미완료다. 기존 시간·승인·원복 유지, 실제 설치·배포·Slack·장애·Terraform·유료모델·커밋/push 없음. 추천 GPT-6 SOL · High.

## 2026-11-19 WF-09 검토·CI/main 반영 완료

[PR #37](https://github.com/joungGo/url-shortener-ai-ops/pull/37) head `11ff946064388a090cecadb770d4278c7ca91201`를 일반 squash 병합해 main `1bea7d6f83e60b12ddb2fe04ce181cfecf5a17ff`에 반영했다. [PR CI44461799743](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44461799743) 7종·push CI44461794113·[main CI44462377953](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44462377953) 성공. main의 PR전용 gate skip은 설계대로이며 실제 PR gate는 통과했다. 검토/병합 tree `424ccbea3ca4b0b521b4e138c9d60867c508ac99`와 소스 hash 일치. 보호 변경/관리자 우회 없음.

원복 관찰 뒤 최초 실패로 재제안하던 문제와 취소/만료 우선순위 P2 두 건을 회귀3개로 재현·수정했다. 집중39개·전체1544개(205.843초/skip0), AST4개·diff·저장소 경계 통과. 동시 회수 시험의 SQLite 연결 수명을 고정해 회수 예약 경쟁을 검증했으며 제품 파일 안전 검사는 유지했다. 기존 C단계75개/공유 Lease 동일.

상세 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf09-effect-verification-20261119/ai-ops/docs/recovery-effect-review-20261119.md`, `recovery-effect-review-validation-20261119.json`, 작업표. 소스·시험·구현/검토 문서는 main 반영됐으며 최종 CI/main 메타데이터·인계는 로컬 갱신했다. 원래 작업트리의 다른 수정은 보존했고 main checkout/reset하지 않았다.

기존 시간·실행별 승인·정확head 사람리뷰/CI·잠금/ACK·원복/회수 조건 유지. 실제 배포·Slack 전송·복구 PR 생성·감시/장애·인프라·Terraform apply/destroy·유료 모델은 실행하지 않았다. local_fixture만 연결하며 원복 후보는 실행 승인이 아니다. cleanup_complete는 선언된 자기 manifest의 합성 회수 범위다.

다음은 WF10 공지·조사 첫 완료단위의 배포/Slack 검증 계획과 재설치 입력 준비다. 복구 쪽 실제 인증된 관측·승인/결과 outbox·원복/소유 회수 어댑터 연결은 별도 후속 범위로 남는다. 실제 연결/실습을 자동 시작하지 않는다. C04/C05 최종 실환경 검증은 미완료다. 추천 **GPT-6 SOL · High**.

## 2026-11-19 WF-09 코드 검토·CI 반영 준비

후속 판정의 원복 결과/소비된 시도 누락과 취소·만료 우선순위 P2 두 건을 회귀3개로 재현·수정했다. 집중39개/전체1544개·AST4개·diff·저장소 경계 통과. 기존 C단계75개·공유 Lease 동일. `ai-ops/docs/recovery-effect-review-20261119.md`와 `recovery-effect-review-validation-20261119.json` 참조. 이 커밋 시점 원격 CI/main 대기이며 완료 후 최신 기록으로 확정한다. 실제 배포·Slack·장애·인프라는 실행하지 않는다. 기존 시간·승인·원복 조건 유지. 추천 GPT-6 SOL · High.

## 2026-11-19 WF-09 효과검증·원복/수동 인계 로컬 검증 완료

AI main `6891144c6c31e29a856d701191c9eb177764204a` 기반 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf09-effect-verification-20261119` (`codex/wf09-effect-verification-20261119`)에서 기존 WF07/08 적용 기록·사건/job/계획·인가와 같은 JobQueue DB를 효과·후속·회수 장부에 연결했다. PR/명령 적용 시각을 보존하고, 경보 해제 뒤 결과 읽기와 새 실행 승인을 분리했다.

같은 UID/정확 실행의 runtime·HTTP·지표·경보 관찰로 성공/미해결/검증불가를 나누고, 취소·원복 결과·최초 실패를 별도 보존한다. 명령 실패는 재시작 반복 없이 수동 인계하며, PR 후속 원복은 사전 승인 범위/기준선/기한이 맞는 후보만 연결한다. 후보는 execution_allowed=false이고 기존 실행 직전 승인·권한·잠금 검사가 별도로 필요하다. 자기 소유의 봉인된 manifest만 로컬 회수하며 불명확한 쓰기는 재전송하지 않는다.

신규 집중36개·전체1541개(198.711초/skip0)·AST4개·diff·저장소 경계 통과. 기존 C단계75개와 공유 Lease 구현은 동일. 승인 만료 뒤로 잘못 잡힌 취소 fixture1건은 합성 시각을 수정해 최종 전체 시험으로 확인했다. 실제 로컬 큐/worker·장부와 로컬 Git/Helm·합성 포트만 사용했다.

**코드 미커밋·원격 CI/main 미반영.** 상세는 위 작업트리의 `ai-ops/docs/recovery-effect-local-20261119.md`, `recovery-effect-local-validation-20261119.json`, 작업표/전체 계획에 있다. WF08 main 완료 후 메타데이터도 이어받았고 원래 작업트리의 다른 수정은 보존했다.

실제 인증된 관측/Slack 승인·결과 outbox·원복 실행·소유 자원 재고/회수 어댑터·주기 prune은 미연결이다. cleanup_complete는 선언된 자기 manifest 범위이며 실제 외부 자원 전체 회수 주장이 아니다. 기존 기록에 적용 시각이 없으면 추정하지 않고 검증불가다. C04/C05 최종 실환경 검증은 여전히 미완료다.

기존3/15/120/210/240/300/120초·실행별 새승인·정확head 사람리뷰/CI·잠금/ACK·원복/회수 유지. 실제 배포·Slack 전송·복구 PR·인프라·장애·Terraform·유료 모델 실행 없음. 다음은 **WF09 코드 검토·CI/main**이다. 추천 **GPT-6 SOL · High**.

## 2026-11-19 WF-08 검토·CI/main 반영 완료

[AI PR #36](https://github.com/joungGo/url-shortener-ai-ops/pull/36)의 head `b571d34fcb41ef37a0e404acd46964a1b6cfb684`를 일반 squash 병합해 main `6891144c6c31e29a856d701191c9eb177764204a`에 반영했다. 응답 유실 뒤 WF06 취소 누락과 동시 조회의 완료/취소 상태 덮어쓰기 P2 두 건을 재현·수정했다.

로컬 집중34개·전체1505개(147.607초/skip0), AST3개·diff·저장소 경계 통과. 기존 C단계75개와 공유 Lease 구현은 동일. [PR CI44457885630](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44457885630) 7종·push CI44457825395·[main CI44458405951](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44458405951) 성공. main의 PR전용 gate는 예상 skip이고 실제 PR gate는 통과했다. 검토/병합 전체 tree·소스 hash 일치, 보호 변경/관리자 우회 없음.

상세 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf08-command-execution-20261119/ai-ops/docs/recovery-command-review-20261119.md`, `recovery-command-review-validation-20261119.json`, 작업표. 소스·시험·구현/검토 문서는 main 반영됐고 최종 CI/main 메타데이터·인계는 로컬 갱신했다. 원래 작업트리의 다른 변경을 보존했으며 main checkout/reset하지 않았다.

기존 시간·승인·원복·정확head 사람리뷰/CI·잠금/ACK·회수 조건 유지. 실제 배포·Slack 전송·복구 PR·감시/장애·인프라·Terraform apply/destroy·유료 모델은 실행하지 않았다. 명령은 local_fixture만 허용하며 적용 확인은 서비스 회복이 아니다. 실제 인증된 Slack 승인/결과 outbox·진단/쓰기/잠금 어댑터·RBAC/자격·주기 보관 정리와 C04/C05 최종 실환경 검증은 후속이다.

다음은 **WF09 공통 효과검증·원복/수동 인계 로컬 구현·검증**이다. 실제 연결/실습을 자동 시작하지 않는다. 추천 **GPT-6 SOL · High**.

## 2026-11-19 WF-08 검토 보완·CI 반영 준비

명령 응답 유실 후 취소 누락·동시 조회 상태 덮어쓰기 P2 두 건을 재현·수정했다. 집중34개·전체1505개·AST3개·diff·저장소 경계 통과, 기존 C단계75개/공유 Lease 동일. `ai-ops/docs/recovery-command-review-20261119.md`, `recovery-command-review-validation-20261119.json`을 따른다. 이 커밋 시점 CI/main은 대기이며 완료 후 최신 기록으로 확정한다. 기존 시간·승인·원복 조건 유지, 실제 배포·Slack·장애·인프라 실행 없음. GPT-6 SOL · High.

## 2026-11-19 WF-08 명령 실행 연결 코어 로컬 검증 완료

AI main `b49672e95536dbc56f11a39626e300af2071d7da` 기반 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf08-command-execution-20261119` (`codex/wf08-command-execution-20261119`)에서 WF05 사건/조사 후보·기존 큐와 WF06 승인 수명주기를 고정 명령 계획에 연결했다. 최초 명령은 격리 ai-ops-lab/lab-api 한 Pod 재시작이며, 정확 UID/resourceVersion 검사 뒤 소유 annotation 하나를 PATCH하는 계약이다. 별도 execute_command 승인/테이블로 PR 준비 동의와 분리하고 현재 진단·실행자·공유 Lease·취소/만료를 확인한다.

집중30개·전체1501개(152.834초/skip0)·AST3개·diff·저장소 경계 통과. 같은 사건의 동시/다른 계획 중복 실행, 1시간 이상 사람 대기, 정책/대상 변경, CAS 충돌, 취소 경쟁, 응답 유실·프로세스 중단·읽기 조정·잠금 실패를 검증했다. 기존 C단계75개와 공유 Lease 구현은 기준 main과 동일하다.

**코드 미커밋·원격 CI/main 미반영. 실행은 합성 local_fixture 포트만 허용한다.** 실제 진단/쓰기/잠금 어댑터·RBAC/자격, 인증된 Slack 승인 endpoint/결과 outbox·주기 보관 정리는 후속 구현이다. 적용 확인은 서비스 회복이 아니며 WF09 효과검증/수동 인계를 남겼다. 불명확한 쓰기를 자동 재전송하거나 재시작을 원복했다고 표시하지 않는다.

상세와 검증 해시는 위 작업트리의 `ai-ops/docs/recovery-command-local-20261119.md`, `recovery-command-local-validation-20261119.json`, 작업표/개선 계획에 기록했다. WF07 main 완료 후 메타데이터도 이어받았으며 원래 작업트리의 다른 변경은 보존했다.

기존3/15/120/210/240/300/120초·실행별 승인·정확head 사람리뷰/CI·잠금/ACK·원복/회수 유지. C04/C05 최종 실환경 검증은 미완료다. 실제 배포·Slack 전송·복구 PR·인프라·장애·Terraform·유료 모델 실행 없음.

다음은 **WF08 코드 검토·CI/main**, 이후 WF09 공통 효과검증·원복/수동 인계다. 추천 **GPT-6 SOL · High**.

## 2026-11-19 WF-07 검토·CI/main 반영 완료

[AI PR #35](https://github.com/joungGo/url-shortener-ai-ops/pull/35)의 head `bba9c335c4589bb72903de1dea0986e8d6e5db96`를 일반 squash 병합해 main `b49672e95536dbc56f11a39626e300af2071d7da`에 반영했다. 최종 확인 사이의 WF06 취소·원본 보관 만료·관측 지연 P2 3건을 재현·수정했다. 로컬 집중31개·전체1471개(134.944초/skip0)·AST2개·diff·저장소 경계 통과, 기존 C단계75개 동일.

[PR CI44454087061](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44454087061) 7종과 [main CI44454506379](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44454506379) 성공. main의 PR전용 gate skip은 설계대로이며 실제 PR gate는 통과했다. 검토 head와 병합 tree·소스 hash 일치. 보호 변경/관리자 우회 없음.

상세 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf07-image-pr-20261119/ai-ops/docs/recovery-pr-review-20261119.md` 및 `recovery-pr-review-validation-20261119.json`, 작업표를 따른다. 코드·구현/검토 문서는 main 반영됐고 완료 후 메타데이터·인계는 로컬 갱신했다. 원래 작업트리의 다른 변경은 보존했다.

기존 시간·실행별 승인·정확head 리뷰/CI·잠금/ACK·원복/회수 유지. 실제 복구 PR·Slack전송·배포·인프라·장애·Terraform·유료모델은 실행하지 않았다. PR bridge는 로컬 fixture/쓰기 비활성 계획·관측 검증이며 실제 승인 endpoint/outbox/쓰기·잠금·회수 adapter와 draft 감사 DB 보관 정리, WF09 효과검증·C04/C05 실환경 완료는 후속이다.

다음은 **WF08 명령1종의 고정 실행계약·승인·별도 실행기를 로컬 구현·검증**하는 단계다. 실제 실행은 별도 범위/승인 확인 후이며 자동 기동하지 않는다. 추천 **GPT-6 SOL · High**.

## 2026-11-19 WF-07 검토 보완·CI 반영 준비

사용자 요청으로 WF07을 직접 검토해 마지막 확인 사이의 WF06 취소·자료 보관 종료·관측 지연 오류를 로컬 재현·수정했다. 기존 한계/승인/원복을 유지하며 최종 결과는 `ai-ops/docs/recovery-pr-review-validation-20261119.json`, 상세는 `recovery-pr-review-20261119.md`를 따른다. 이 커밋 시점 CI/main은 대기다. 실제 배포·Slack·장애·인프라는 실행하지 않는다. 추천 GPT-6 SOL · High.

## 2026-11-19 WF-07 이미지 복구 PR 연결 코어 로컬 검증 완료

AI main59c33a9(WF06 PR34) 기반 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf07-image-pr-20261119`(`codex/wf07-image-pr-20261119`)에서 기존 WF05 계획·WF06 준비 동의/SQLite 장부를 이미지 proof·정확 diff/PR head·사람 리뷰/CI·잠금·merge/sync 검증 경로에 연결했다. 새 `recovery_pr.py`와 로컬 통합 시험을 추가했다.

같은 사건/job/계획/승인/PR hash 연결, 중복 방지, 유실/중단 후 읽기 조정, 취소 경쟁·정책/대상 변경·신선도·보관 만료·잠금 유실을 검증했다. 로컬 Git/Helm와 합성 API만 사용했다. merge/sync는 쓰기 비활성 계획/관측 검증이며 서비스 회복 판정은 하지 않는다.

집중28개(38.806초)·전체1468개(131.644초/skip0), AST2개·diff·저장소 경계 통과. 전체 시험 후 관측 시각 형식 호환 수정은 WF07 집중28개로 재검증했다. 기존 C단계75개 기준 main과 동일. 상세 작업트리 `ai-ops/docs/recovery-pr-local-20261119.md`, `recovery-pr-local-validation-20261119.json`, 작업표. WF06 완료 후 메타데이터도 이어받았다.

**코드 미커밋·원격 CI/main 미반영.** 실제 인증된 Slack 승인/outbox와 생산용 작성/병합/동기화·잠금 획득/회수 포트 및 기존 draft 감사 DB 보관 정리는 후속 연결 작업이다. 로컬 코어 완료를 실제 운영 복구 완료로 보지 않는다. 기존3/15/120/210/240/300/120초·실행별승인·정확head리뷰/CI·잠금/ACK·원복/회수·C04/C05미완료 유지. 실제 PR/배포·Slack·인프라·장애·Terraform·모델 실행 없음.

다음은 **WF07 코드 검토·CI/main**, 이후 WF08 명령1종·WF09 공통 효과검증/인계와 WF10/11 실제 연결 검증이다. 추천 **GPT-6 SOL · High**.

## 2026-11-19 WF-06 검토·CI/main 반영 완료

[AI PR #34](https://github.com/joungGo/url-shortener-ai-ops/pull/34)의 최종 head `18f3eb41bcebedf0a57d2e971a8c5bebe25b88a9`를 일반 squash 병합해 main `59c33a9d4e4dfd8f524161ce2cc2d590e1ca1133`에 반영했다. DB 대기 뒤 예산 재확인·시계 역행 시 종료 상태 보존·원본 조사 결과/해시 재확인 P2 세 건을 로컬 재현·수정했다.

집중39개·전체1440개(90.613초/skip0)·AST2개·diff·저장소 경계 통과. [PR CI44450455429](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44450455429) 7종과 [main CI44450888910](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44450888910) 성공. main PR전용 gate는 설계상 skip이며 실제 PR gate는 통과했다. 검토/병합 전체 tree와 파일 해시 일치, C단계68개 기준 main과 동일, 보호 변경/관리자 우회 없음.

상세 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf06-approval-lifecycle-20261119/ai-ops/docs/recovery-approval-review-20261119.md`와 `recovery-approval-review-validation-20261119.json`, 작업표를 따른다. 코드·검증 문서는 main 반영됐고 완료 후 인계/최종 결과 메타데이터는 로컬 갱신했다. 원래 작업트리의 다른 수정/개인자료는 보존했다.

이번 장부는 prepare_proposal 동의와 재검증이며 모든 결과 execution_allowed=false다. 실제 Slack 승인 endpoint·실환경 조회·정확 diff/PR 실행·명령 실행은 미연결. 기존3/15/120/210/240/300/120초·실행별승인·정확head리뷰/CI·잠금/ACK·원복/회수·C04/C05미완료 유지. 배포·Slack 전송·인프라 설치·장애 실습·Terraform·유료 모델은 실행하지 않았다.

다음은 **WF-07 기존 이미지 복구 PR 경로에 계획/승인 장부 연결을 로컬에서 구현·검증**하는 단계다. 정확 diff/정상 이미지·서명된 실제 승인 입력/조회 어댑터 계약을 먼저 연결하고 새 실행권한이나 실습은 자동 활성화하지 않는다. 추천 **GPT-6 SOL · High**.

## 2026-11-19 WF-06 검토 보완·CI 반영 준비

사용자 후속 요청에 따라 WF06을 직접 검토해 DB 대기 후 예산 확인 누락·시계 역행의 종료 상태 덮어쓰기·원본 조사 결과 재확인 누락을 재현·수정했다. 회귀4개 포함 집중39개 통과. 최종 전체 시험과 해시는 `ai-ops/docs/recovery-approval-review-validation-20261119.json`, 상세 `recovery-approval-review-20261119.md`를 따른다. 정확 PR head의 CI 확인 후 일반 main 반영한다. 이 커밋 시점 CI/main은 대기이며 후속 완료 기록으로 확정한다. 기존 시간·승인·원복·C04/C05미완료 유지. 실제 배포·Slack·인프라·장애 실행 없음. GPT-6 SOL · High.

## 2026-11-19 WF-06 승인 대기 핵심 로컬 구현·검증 완료

WF05 PR33/main17093a7 기반 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf06-approval-lifecycle-20261119`(`codex/wf06-approval-lifecycle-20261119`)에서 승인 대기/만료/취소/중복/재시작/현재 관측 재검증 장부를 구현했다. 기존 WF05 Slack 저장 계획·요청자/정책/자료 인가와 JobQueue의 개인 DB를 연결한다. 대기 중 쓰기 자격·클러스터 잠금·새 조사 작업을 만들지 않는다.

승인 범위는 **prepare_proposal(PR 준비)**이며 정확 diff/정상 이미지 없는 초안을 실행 승인으로 바꾸지 않는다. 모든 결과 execution_allowed=false. 한 시간 이상 대기, 자연 회복, UID/revision 변경, 승인/취소 경쟁, 오래된/미래 관측, 기한·정책 변경·재시작·보관 만료를 검증했다. 실제 Slack 승인 endpoint/현재 조회 adapter는 미연결이며 WF07/08에서 인증과 완성된 PR/명령 실행계약에 연결한다. 합성 시간 설정은 운영 기본값이 아니다.

집중35개·전체1436개(89.924초, skip0)·AST2개·diff·저장소 경계 통과, C단계68개 기준 main과 동일. 문서: 작업트리 `ai-ops/docs/recovery-approval-local-20261119.md`, `recovery-approval-local-validation-20261119.json`, 개선 계획·작업표. WF05 완료 후 로컬 메타데이터도 이어받았다.

**로컬 미커밋·원격CI/main 미반영.** 인프라 정리 상태를 유지했고 설치·실제 Slack/배포·장애·Terraform·유료 모델은 실행하지 않았다. 기존3/15/120/210/240/300/120초·실행별승인·정확head리뷰/CI·잠금/ACK·원복/회수·C04/C05미완료 유지. 다음은 WF06 코드 검토·CI/main, 이어 WF07 PR 준비/실제 승인 연결이다. 추천 **GPT-6 SOL · High**.

## 2026-11-19 WF-05 검토·CI/main 반영 완료

[AI PR #33](https://github.com/joungGo/url-shortener-ai-ops/pull/33)의 최종 head `33a9a19cb832af106af65d94621547ba43aa75f7`를 일반 squash 병합해 main `17093a7684c885574388d81f3168033d526a6115`에 반영했다. 직접 검토에서 사건 시각 나노초 손실로 직전 관측이 지원 근거가 되는 P2 한 건을 재현·수정했다. 이미지 CI의 OpenSSL 간접 갱신/검사 불일치도 공식 Alpine 양 아키텍처 목록 확인 후 설치·검사를3.5.9-r1로 함께 고정했다.

WF05 집중29개 통과. 로컬 전체1401개 중1400개 통과 후 임시 amtool 부재의1개는 고정 notice9개 검사에서 통과해 고유1401개 검증 완료(skip0). [최종 PR CI44446823328](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44446823328)의 gate 포함7종·pushCI44446816507·[main CI44447269162](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44447269162) 성공. main PR전용 gate skip은 설계대로이며 실제 PR gate는 통과했다. 검토/병합 전체tree·소스해시 일치, 기존 C단계47개 소스 동일, 보호변경/관리자우회 없음.

상세 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf05-recovery-plans-20261119/ai-ops/docs/recovery-plan-review-20261119.md`와 `recovery-plan-review-validation-20261119.json`, 구현 문서·작업표를 따른다. 코드/검증 문서는 main 반영됐으며 완료 후 인계·작업표·검증 메타데이터는 로컬 갱신했다. 원래 작업트리의 다른 코드/개인자료는 보존했다.

계획 후보는 여전히 draft/미승인/proposal_ready=false/execution_allowed=false다. 기존 시간·승인·원복·C04/C05미완료 유지. 실제 배포·Slack전송·인프라·장애실습·ECR검사·Terraform/모델 실행 없음. 다음은 **WF-06 승인 대기·만료·취소·늦은 승인·재검증 로컬 구현**이다. 추천 GPT-6 SOL · High.

## 2026-11-19 WF-05 검토·CI 반영 준비

사용자 후속 요청으로 WF-05를 직접 검토했고 사건 발생 시각의 나노초 손실로 직전 관측이 지원 근거가 되는 오류를 재현·수정했다. 최종 결과·해시는 `ai-ops/docs/recovery-plan-review-validation-20261119.json`, 설명은 `recovery-plan-review-20261119.md`를 따른다. 정확한 PR head의 CI 확인 뒤 일반 main 반영을 진행한다. 이 커밋 시점 원격 CI/main은 대기이며 최종 상태는 후속 인계 기록으로 확정한다. 기존 시간·승인·원복 조건과 C04/C05 미완료 유지. 실제 배포·Slack·인프라·장애·모델 실행 없음. 추천 GPT-6 SOL · High.

## 2026-11-19 WF-05 복구 계획 요청·계약 로컬 검증 완료

AI main e3a4efa(WF-04 PR32 반영) 기반 작업트리 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf05-recovery-plans-20261119`에서 명시적 `plan-recovery 사건 job [action]`·고정 조치 registry·해시 결합 RecoveryPlan을 구현했다. 기존 사건/조사 큐/worker/TaskService·Slack 회신 장부를 재사용한다. 같은 사용자/채널의 사건 조사만 참조하며 지원 조치/미지원/근거부족·중복/응답유실·인가변경/자료만료를 검증했다.

지원은 이미지 장애의 저장 근거와 계획 템플릿 일치를 뜻한다. 모든 초안은 draft/미승인/proposal_ready=false/execution_allowed=false다. 정상 이미지·현재 UID/revision·정확 diff·승인은 후속 WF06/07이며 명령 재시작은 WF08 미구현 후보로 표시한다. 요청만으로 새 조사·PR·명령·쓰기 자격을 만들지 않았다.

신규27개·전체1399개(83.307초/skip0)·AST4개·diff·소유경계 통과. C단계 소스47개 main과 동일; 기존 시간·승인·원복·C04/C05미완료 유지. 상세 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf05-recovery-plans-20261119/ai-ops/docs/recovery-plan-local-20261119.md`, `recovery-plan-local-validation-20261119.json`, 작업표. WF04 반영 후 로컬 완료 메타데이터도 새 작업트리에 이어받았다.

이번 WF05는 로컬 미커밋·CI/main 미반영. 인프라 설치·실제 배포/Slack·장애 실습·Terraform·유료 모델 없음. 원래 작업트리의 다른 수정/개인자료는 보존했다. 다음은 WF05 검토·CI/main 후 WF06 승인 대기·만료·취소·재검증 로컬 구현. 추천 GPT-6 SOL · High.

## 2026-11-19 WF-04 코드 검토·CI 반영 준비

사건 지정 Slack 조사·상태/결과 연결을 직접 검토해 정책 변경 중단 뒤 이전 큐 권한 잔존과 연결 비활성화 뒤 사건 복사본 만료 지연2건을 재현·수정했다. Slack125개·전체1372개(84.572초/skip0)·AST8개·diff·저장소 경계 통과. 검토 기록 `ai-ops/docs/incident-slack-review-20261119.md`, 최종 소스 해시 `incident-slack-review-validation-20261119.json`을 따른다. 최초 로컬1370개/해시는 이전 시점 기록이다.

사용자 요청에 따라 이 검토 코드의 PR CI 통과 후 일반 main 반영을 진행한다. 이 커밋 시점의 원격 CI/main은 아직 미확정이며 최종 상태는 최신 인계 기록을 따른다. 기존 시간·승인·원복 조건 유지. 실제 배포·DB전환·Slack·장애·인프라·Terraform·모델 실행 없음. 추천 GPT-6 SOL · High.

## 2026-11-19 WF-04 사건 지정 Slack 연결 로컬 검증 완료

AI mainca63fcf 기반 작업트리 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf04-slack-events-20261119`에서 사건 지정 `investigate-event`·`status-event`·유일한 thread 참조를 기존 사건 outbox/JobQueue/Worker/TaskService·Slack 회신 장부에 연결했다. 사용자/채널/대상 인가, 정책 변경 후 예약·수집 전 fence, 요청/응답 중복·재시작, 과거 발생 시각/현재 수집 구분, 사건 만료와 복사본 정리를 포함한다. 기존 일반 조사/이전 전달 payload 호환을 유지했다.

신규36개·전체1370개(83.71초/skip0), AST8개·diff·저장소 경계 통과. 상세: [WF-04 구현·검증](/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf04-slack-events-20261119/ai-ops/docs/incident-slack-local-20261119.md), 같은 폴더 작업표/검증 JSON. 인프라 정리 완료 상태와 개인 복원 자료는 그대로 보존했다.

로컬 미커밋이며 원격CI/main·실제배포·Slack전송·장애/모델/Terraform 실행 없음. 사건 메타데이터v2 전환은 실제 적용하지 않았으며 배포 시 기존 수신기/worker/relay 중지·DB 백업과 같은 검토 버전 적용 필요. 기존3/15/120/210/240/300/120초·실행별승인·정확head리뷰/CI·잠금/ACK·원복/회수, C04/C05미완료 유지. 다음은 WF-04 코드 검토·CI/main, 이후 WF-05 로컬 계약. 추천 GPT-6 SOL · High.

## 2026-11-18 WF-03 검토·CI 반영 준비

Slack 기본 fallback 라벨 노출·대체 Helm/Slack 설정·실제 template CI 누락3건을 보완했다. 플랫폼10개·AI교차9개 통과, 기존 AI1334개 검증 코드4개 해시 동일. 양쪽 C03 gate에 검사를 연결하며 AI는 플랫폼 PR163 head d6c2c8f를 고정한다. 상세 `ai-ops/docs/incident-notice-review-20261118.md/json`(검증은 review-validation 파일). 정확 head·CI·현재 리뷰 조건을 확인한 뒤 main 반영하며 보호 변경은 새 사용자 결정 없이 실행하지 않는다. 실제 배포·Slack·장애 실습 없음. 기존 시간·승인·원복 유지.


## 2026-11-18 WF-03 경보 공지·사건 참조 로컬 구현 완료

양쪽 `codex/wf03-alert-reference-20261118`에서 로컬 구현했다. AI 작업트리는 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf03-alert-reference-20261118`(기준 main9c9f3fc), 플랫폼은 `/Users/jounghyeon/dev/Url-Shortener-EKS-Platform/.worktrees/wf03-alert-reference-20261118`(기준 main01ca82b)다. 원래 작업트리의 다른 수정은 보존했다. WF-02는 PR30/main9c9f3fc·CI44318734083 완료 상태를 이어받았다.

플랫폼 `cluster-addons/prometheus/wf03/`에 제한된 경보 사실·나노초 사건 참조·조사 채널 template와 후보 생성기를 추가했다. 기존 Watchdog/억제/간격/Secret은 보존하며 실제 values.yaml/Application은 변경하지 않았다. 후보는 기존 자동조사 분기를 별도 사건 접수로 전환하므로 후속 배포 계획에서 정책/DB 전환을 함께 검토해야 한다.

AI는 기존 EventStore와 TLS 수신기를 재사용해 명시 옵션의 `/v1/events`·고정 발신원·pending/registered/expired 참조 상태를 연결했다. 기본 `/v1/alerts` 유지. 사건 접수 ACK는 Slack 전달 성공이 아니며 notification은 unknown이다. 새 사건 Slack 명령·결과 회신은 WF-04에 남아 있다.

플랫폼8개·AI집중50개·전체1334개(81.827초, skip0), AST6개·양쪽 diff·저장소 경계·실제 inline template 렌더 통과. 상세/해시는 AI 작업트리 `ai-ops/docs/incident-notice-local-20261118.md`와 `incident-notice-local-validation-20261118.json`, 상태는 `incident-workflow-implementation-tracker.md`에 있다. 실제 Slack 전송/실물 실패·재시도 SLA·채널/DB 전환은 미검증이며 기존 SQLite ResourceWarning과 C04/C05 미완료를 보존한다.

WF-03 로컬 미커밋. 원격 CI/main·실제 배포·Slack 전송·장애 실습·Terraform·유료 모델 호출 없음. 기존3/15/120/210/240/300/120초·실행별 승인·정확 head 리뷰/CI·잠금/ACK·원복/회수 조건은 유지했다. 다음은 WF-03 양쪽 코드 검토·CI/main 반영 후 WF-04 인증된 사건 조사 요청·결과·보관 정리의 로컬 연결이다. 추천 GPT-6 SOL · High.


## 2026-11-18 WF-02 코드 검토·CI 반영 준비

`codex/incident-workflow-plan-20261118`에서 WF-02 전체를 직접 검토하고 정확 대상 인가·큐 부분 용량·반복 장부 전환의 해제/만료 처리3건을 재현·수정했다. 집중32개/큐 포함48개/전체1325개와 AST4개·diff·저장소 경계 검사 통과. 상세 `ai-ops/docs/incident-store-review-20261118.md`, 소스 해시는 `incident-store-local-validation-20261118.json`이다.

사용자가 CI 통과 시 main 반영까지 요청했다. 해당 PR의 정확head/CI를 확인해 일반 squash 병합하며 최종 결과는 PR/main 기록으로 확인한다. 이 문서 작성 시 원격 결과는 미확정이다. 보호 완화·관리자 우회·실제 배포·Slack·장애 실습 없음. 기존 시간·승인·원복 및 C04/C05 미완료 유지. 다음은 WF-03 공지 참조/템플릿, 이후 WF-04 인증 사건요청/결과·정기정리 연결이다. 추천 GPT-6 SOL · High.


## 2026-11-18 WF-02 사건 저장·큐·보관·장부 전환 로컬 검증 완료

AI 작업트리 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/incident-workflow-plan-20261118` / `codex/incident-workflow-plan-20261118`(기준main36a55fd)에서 event_store 신규와 기존 queue/worker 수집 전 검사를 구현했다. canonical evt·발신원 registry·manual 기본/정확 자동 예외·원자 outbox/기존 job 연결·정책/해제 검사·발생부터7일 상세 보관/해시 tombstone·verified legacy 전환을 포함한다. 새 스키마 사용 DB만 version3이며 이전 worker를 먼저 종료해야 실물 전환할 수 있다. 원래 job/시도 보존, 전환된 legacy 대기는 새 명시 조사 필요.

집중29개·사건/큐45개·전체1322개 로컬시험과 Python4개 AST/diff 검사 통과. 동시접수·프로세스 강제종료·기존 보관정리/worker 실사용 포함. 초기 bare version3 허용 회귀를 수정한 뒤 전체통과. 기존 SQLite ResourceWarning은 남아 있다. 상세 `ai-ops/docs/incident-store-local-implementation-20261118.md`와 `incident-store-local-validation-20261118.json`, 상태는 `incident-workflow-implementation-tracker.md`.

WF-02 로컬검증완료, 코드/문서 미커밋·CI/main/실제DB전환/배포 없음. HTTP/Slack 새 사건 입력·결과·공지 템플릿·expire 주기 호출은 WF-03/04에서 연결한다. 실제 Slack/장애/모델/Terraform 미실행. 기존 시간·승인·원복·C04/C05 미완료 유지. 다음은 코드검토·CI/main 반영 후 WF-03/04. 추천 GPT-6 SOL · High.


## 2026-11-18 WF-01 사건·채널·조사 정책 상세 계약 완료

문서 worktree `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/incident-workflow-plan-20261118/ai-ops/docs/incident-identity-channel-policy-contract-20261118.md`에 evt(경보회차)와 기존inc/job 분리, 발신원/cluster/fingerprint/나노초startsAt canonical ID, 중복/재발/해제/충돌 규칙, 두채널 alert_ref 연결, manual기본/정확자동예외 정책을 정의했다. D1/D2는 후속로컬 설계로 채택했으며 현행 자동조사/채널설정은 미변경. 기존Webhook 공지는 독립유지·Slack message ts/history 권한을 필수로 하지 않는다. template 시각정밀도/참조일치는 WF-03 구현시험 필요.

WF-01 문서완료(18개 시험명세는 아직 미실행), WF-02~12 예정. 다음은 WF-02 계약/장부/outbox/보관·기존큐 매핑 로컬구현. 작업표/전체계획 링크 갱신. 기존시간/승인/원복·C04/C05미완료 유지. 코드/실제연결/배포/Slack/장애/CI/main 변경 없음. 로컬문서 미커밋. 추천 GPT-6 SOL · High.

## 2026-11-18 장애 워크플로우 문서 기반 관리 시작

사용자 요청으로 개선 이유R1~R7·설계 선택D1~D5·구현 작업WF-00~12를 연결했다. 기준 문서는 AI 문서 worktree `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/incident-workflow-plan-20261118/ai-ops/docs/`의 `incident-workflow-and-improvement-plan-20261118.md`(왜/무엇을/어떻게)와 `incident-workflow-implementation-tracker.md`(상태/완료기준/증거/다음행동)다. 향후 작업마다 해당행을 갱신하고 이 HANDOFF는 진입점으로 사용한다.

WF-00 문서완료, WF-01~12 예정. 다음은 WF-01 사건/채널/조사정책 계약. 자동조사 비활성화·채널통합·일반운영 시간수치·명령1종 대상/UI는 제안 또는 미정으로 구분했다. 공지→요청조사→결과의 첫 완료단위를 우선하고 복구는 PR→명령1종으로 확장한다. 기존 실습시간/승인/원복과C04/C05미완료는 보존. 문서만 변경했으며 코드/CI/main/배포/실제연결/실습 없음. 문서 변경은 현재 로컬 미커밋 상태다. 추천 GPT-6 SOL · High.

## 2026-11-18 장애 공지·조사·복구 전체 워크플로우 개선 계획

사용자 요청에 따라 현행/목표·재사용/변경/추가 구현·PR/명령 방식·승인/장시간 대기/실패/원복·단계별 완료 기준을 문서화했다. 문서 전용 worktree `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/incident-workflow-plan-20261118` / `codex/incident-workflow-plan-20261118`(기준main36a55fd)의 `ai-ops/docs/incident-workflow-and-improvement-plan-20261118.md`를 따른다.

현재 경보 채널과 조사 #ai-ops는 다르다는 사용자 확인을 기록. 목표 권고는 먼저 관측사실 공지→사용자 요청 조사→근거 결과→별도 복구 계획 요청→PR 또는 고정 명령 작업계획→방식별 사람승인→현재상태 재검증→제한 실행/효과검증·결과회신이다. 기존5종 자동조사와 현행Slack investigate만 지원하는 사실을 구분했다. 자동조사 비활성화·채널통합·명령실행 구현/권한변경은 하지 않았다.

다음 추천 P1 사건/채널/조사정책 계약→P2 공지/조사/결과 연계→P3 공통복구 장부/승인/대기→P4 Slack PR복구→P5 명령1종→P6 통합검증. 한시간 이상 사람대기와 실행기한은 분리 설계하되 숫자는 후속확정, 현행C04 3/15/120/210/240/300/120초·실행별승인/정확head리뷰/CI/잠금/ACK/원복 유지. C04/C05미완료·全경로시간부족 미해결. 계획만 작성했으며 실제배포/연결/Slack/복구/장애/Terraform/유료모델 없음. 추천 GPT-6 SOL · High.

## 2026-11-18 C04 안내 응답·왕복 예산 검토 완료

직접 기능·회귀 검토 추가 결함0건. 로컬1293개 시험 당시 소스6개·로그 해시 일치. 상세 `ai-ops/docs/C4-notice-tool-roundtrip-review-20261118.md`. 사용자 요청으로 PR CI 성공 후 일반 main 병합을 진행하며 원격 최종 상태는 PR 기록으로 확인한다. 실제 배포·실습 없음. 전체 경로14.331초 부족은 별도 작업2이며 기존 시간·승인·원복 유지. C04/C05 미완료.

## 2026-11-18 C04 안내 응답·왕복 예산 로컬 보완 완료

007의 native accepted 응답 포장 누락과 도구 왕복21.156초를 로컬에서 재현했다. 전용 worktree `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/c04-notice-tool-roundtrip` / `codex/c04-notice-tool-roundtrip-20261118`(기준main272a1b2)에 정확한 native 응답 자동포장·거짓/추가/중복키 거절·응답 도착/검증 분리계측·안내25초 예약을 구현했다. 집중61개·전체1293개 시험, 변경Python6개 AST·diff·저장소 경계 통과. 기존SQLite ResourceWarning은 별도 한계로 기록했다. 기본sandbox의 소켓제한 후 로컬 소켓권한으로 시험했으며 실제 외부연결/배포/감시/실습/PR/main반영은 없다.

**형식 오류는 해결했지만 실제 전체 경로 시간 부족은 아직 해결하지 않았다.** 당시 재검증37.099+작성5.844+안내21.156초는 남은49.768초보다14.331초 길다. 새25초는 보장값이 아니라 조기중단용 예약이며, 같은 실측은 PR생성 전에 중단된다. 시간여유를 장애 없이 입증하기 전에는 실습반복 금지. 다음은 코드검토·CI 반영 후 실제 안내 왕복/전체예산의 무장애 검증이다. 현재 고정실행경로는 기존main 유지. Lease3초·시작15초·리뷰120/210초·원복240/300초·관찰120초·새승인/정확head리뷰·잠금/ACK/회수 유지. C04/C05미완료, 종료007/PR23승인 재사용 금지. 상세 worktree `ai-ops/docs/C4-notice-tool-roundtrip-local-20261118.md/json`. 추천 GPT-6 SOL · High.

## 2026-11-18 C04 PR 생성·안내 예산 검토 완료

직접 Bugbot 방식 검토 추가 결함0건. 로컬1287시험 통과 당시 소스9개 해시 일치, 최신main680568b 동일. 상세 `ai-ops/docs/C4-pr-notice-budget-review-20261118.md`. 기존 시간·승인·원복 유지. 일반 PR의 CI 성공 후 main 반영을 진행하며 최종 상태는 원격 PR/CI 기록으로 확인한다. 실제 배포·실습 없음.


## 2026-11-18 C04 PR 생성·안내 예산 · 로컬 수정 완료

006은 복구안43.424초 후 작성 여유6.385초에 작성7.416초가 걸려 안내 마감을 약1.03초 넘겼다. 새 worktree `.worktrees/c04-pr-notice-budget`(기준main680568b, 브랜치`codex/c04-pr-notice-budget-20261118`)에서 PR작성8초·실제안내4초 예약, 동일 monotonic 마감 인계, 독립 Pod/ReplicaSet/사건 및 작성App GET 병렬화, 작성 단계계측·늦은 PR 정리 기록·예산 실패 후 토큰회수를 구현했다. 세번 최신수집·전후비교·권한/원본/정확head리뷰·기존시간/승인/원복 유지. 전체1287시험(신규11) 통과, 합성API13회 동일·직렬382→병렬272ms. 실환경 개선/완료 수치가 아니며 실제배포/CI/main/새PR/실습 없음. 상세 작업트리 `ai-ops/docs/C4-pr-notice-budget-local-20261118.md/json`. 다음은 코드검토·CI·main반영 후 장애없는 연결검증; 해결 확인 전 실습반복 금지. C04/C05미완료. 추천 GPT-6 SOL · High.


## 2026-11-18 C04 재검증 지연 · 검토 및 CI 반영 준비

직접 Bugbot 방식 검토 추가 오류0건. 로컬1276시험 소스8개 해시 일치, 기존 시간·승인·원복 조건 유지. 상세 `ai-ops/docs/C4-proposal-latency-review-20261118.md`. 원격CI/main 반영은 해당 PR 기록으로 확인한다. 실제배포·감시·실습은 수행하지 않는다.

## 2026-11-18 C04 복구안 재검증 지연 · 로컬 개선 완료

005의50.740초 구간은 세번 원본수집+초안+Git/Helm연결을 포함하며 기존에는 내부 timing context가 비활성이라 실제 서비스별 비중은 미확정이다. 전용 `codex/c04-proposal-latency-local-20261118` worktree(기준 AI651cdc6)에서 PR전 단계계측/잔여리뷰예산 적용, 독립 CI·신원/ECR·Pod 조회 병렬화, full-SHA Git객체만 재사용하도록 수정했다. 세번 신선수집·전후원본비교·main두번재조회 유지. 합성 로컬 조회947→634ms, Git연결 GET14→8회; 실환경50.7초 개선수치로 해석하지 않는다. 전체1276시험(신규9)·문법·diff 통과. 기존시간/승인/원복 유지, 실제연결/배포/원격CI/실습 없음. 다음은 코드검토·CI·main반영→장애없는 연결검증이며 새실습 자동반복 금지. 코드/결과: `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/c04-proposal-latency/ai-ops/docs/C4-proposal-latency-local-20261118.md` 및 같은이름JSON. C04/C05미완료. 추천 GPT-6 SOL · High.


## 2026-11-18 Lease401 수정 검토·CI 반영 준비

직접 코드 검토 추가 발견0건, 로컬 검증 파일 hash 동일. GET의401만 1회·총3초 재조회, PUT/오류/취소는 중단하며 기존 시간·승인·원복 유지. 상세 `ai-ops/docs/C4-lease-auth-refresh-review-20261118.md`. 원격CI/main 반영 여부는 이 문서를 포함한 PR 기록으로 확인한다. 실제 배포·연결·실습 없음. 첫401 원인은 미확정, 실제 연결 검증 전 실습 금지.

## 2026-11-18 Lease401 인증 갱신 · 로컬 수정 완료

client-go가401 후 자격을 갱신해도 원래401을 반환해 감시가 중단되는 경로를 실제 kubectl+로컬TLS/가짜exec로 재현했다. Unauthorized GET만 최대1회·최초부터 합산3초 안에서 재조회하며 PUT/다른오류/지속401은 중단한다. 응답 핸들 즉시 회수·진단 허용목록을 보완했고 기존15초·리뷰·원복·승인 조건 유지. AI1267/플랫폼925 로컬 시험 및 일반/계측kubectl 각각7개 TLS사례 통과. 첫 실환경401 발생 원인은 미확정이며 실환경 해결 완료로 보지 않는다. 배포/원격CI/실제연결/실습 없음. 수정은 양쪽 `.worktrees/lease-auth-refresh`의 `codex/lease-auth-refresh-local-20261118`에 보존, 원래 미커밋 파일 유지. 상세는 해당 worktree `ai-ops/docs/C4-lease-auth-refresh-local-20261118.md`, 원시 검증은 `~/.local/share/url-shortener-ai-ops/lease-auth-refresh-20261118/verification.json`. 다음은 코드검토/CI/main반영→장애 없는 실제 인증 검증이며 성공 전 실습 반복 금지. 추천 GPT-6 SOL · High.


## 2026-11-18 Lease 계측 연결·오류 전달 검토

AI 1,257개·플랫폼 915개 로컬 시험 통과. 초대형 정수 진단 오류 1건 수정, 감시 옵션·정제 실패 전달과 플랫폼 의존성 반영을 준비했다. 상세 `ai-ops/docs/C4-lease-integration-review-20261118.md`. 원격 CI·main 반영 여부는 해당 PR 기록으로 확인한다. 실제 배포·실습 없음. 기존 시간·승인·원복 조건 유지, 실제 timeout 하위 원인 미확정이며 원인 해결 전 실습 반복 금지. 추천 GPT-6 SOL · High.

## 2026-11-18 로컬 감시 Lease timeout 재현·진단 연결 보완

AI main d9be347 기준 작업트리 `.worktrees/c04-lease-timeout-20261118` / `codex/c04-lease-timeout-20261118`에서 로컬 수정했다. 실제 Unix 대역으로 정상 GET 뒤 응답 헤더 지연→3초 감시 종료를 재현했다. 가짜 인증/TLS/응답 지연은 모두 같은 외부 response_headers/timeout으로 보이며 상세 단계 계측으로만 구분됨을 검증했다. 실제 002의 하위 지연 원인은 아직 미확정이고 실습을 반복하지 않았다.

감시 CLI의 고정 바이너리/해시 옵션 전달, 같은 intent/guard/role의 실패 sidecar, 부모 캐시 폐기·최종 실패 진단 전달을 구현했다. 원문 비밀/주소/헤더/본문 미출력·조회3초/시작15초/리뷰120·210초/원복240·300초/관찰120초·승인/UID/CAS/ACK 유지. 시간 연장·재시도·실패 무시 없음. 전체1256개(신규9)·로컬 상세단계3종·저장소 경계·diff check 통과. 증거 `~/.local/share/url-shortener-ai-ops/c04-lease-timeout-local-20261118/`.

상세 문서는 작업트리 `ai-ops/docs/C4-lease-timeout-local-20261118.md/json`. 변경은 미커밋이며 CI/main/실제 배포·플랫폼 감시 반영은 아직 하지 않았다. 다음은 코드 검토·CI·AI 및 플랫폼 감시 의존성 반영, 그 후 별도 허용 범위의 읽기 전용 실제 계측이다. 실제 원인 해결 근거 전에는 장애 실습을 반복하지 않는다. C04/C05 미완료. 추천 GPT-6 SOL · High.

## 2026-11-18 조사 Job 설정 호환 검토 완료 · CI/main 반영 단계

현재 변경의 기능·회귀 및 자격 전달 경계를 직접 검토했고 추가 결함은 발견하지 못했다. 코드/로그 해시가 로컬 1,247개 시험 통과 시점과 일치한다. 상세 `ai-ops/docs/capture-job-config-review-20261118.md`. 사용자 요청으로 이번 PR의 CI 성공을 확인한 후 일반 main 병합을 진행하며 실제 결과는 PR 기록으로 확인한다. 기존 시간·승인·원복 유지, 실제 배포·실습 없음. C04/C05는 미완료이며 다음 실제 실행 전 검증은 별도다. 추천 GPT-6 SOL · High.

## 2026-11-18 조사 Job의 Slack worker 설정 호환 · 로컬 수정 완료

현재 common-materialize + Memory config 구조를 엄격히 검증해 조사 Job을 생성하고, runbook-policy를 접수 CLI에 전달해 worker와 config_hash를 맞췄다. 기존 ci-config 직접/projected 구성은 유지한다. 허용되지 않은 init/Secret/토큰/환경변수/추가 권한·중복 source는 거절하며 시간·승인·UID/CAS/Lease/ACK·원복 조건은 변경하지 않았다.

실제 worker의 저장 사본에서 구버전 거절→수정 생성 성공을 확인했다. 검증 수치·범위는 작업트리의 `ai-ops/docs/capture-job-config-local-20261118.md/json`을 따른다. 실제 배포·server dry-run·원격 CI·Slack·감시·장애·Terraform·모델 호출 없음. 실제 init 기동 시간은 미검증이며 C04/C05는 미완료다.

코드 기준 AI main5931e245f01b55ce4d74c0e34aa8763ca8cd83b1, 브랜치 `codex/c04-capture-config-20261118`, 작업트리 `/Users/jounghyeon/dev/Url-Shortener-EKS-Platform/.worktrees/c04-capture-config-20261118`. 원래 두 체크아웃의 미커밋 작업은 보존했다. 다음은 이 브랜치 검토·CI·main 반영 후 새로운 실행 전 준비 검증이며, 현재 배포 코드는 변경하지 않았다. 새 준비가 끝나면 실행 승인을 별도로 받는다. 추천 **GPT-6 SOL · High**.

## 2026-11-18 남은 partial 처리 로컬 구현·검증 완료

로그 상한을 유지하며 정제 표본의 줄 수·probe 외 HTTP·진단·시각 범위를 표시하고, 이벤트 전체 UID 조회 성공 후 빈 목록과 조회 실패를 구분했다. 현재 배포와 범위 밖 이력의 원래 시각을 분리하고, CI 실패/원복 이력 미확보를 장애 발생으로 단정하지 않는다. missing/partial/실행 거부 및 기존 시간·승인·원복 조건은 유지했다.

현재 설치 기준 AI main bc398ff의 로컬 bundle에서 `codex/partial-evidence-treatment-20261118` 작업트리를 준비했다. 기존 기본 체크아웃 main ce96d46와 미커밋 작업은 보존했다. 작업 위치: `/Users/jounghyeon/dev/Url-Shortener-EKS-Platform/.worktrees/partial-evidence-treatment-20261118`. 상세 `ai-ops/docs/partial-treatment-local-20261118.md/json`, 코드4개와 새 시험1개를 준비했다. 최초 로컬 전체1239개·신규경계9개·합성 Slack loopback·기존 배포 판독기 호환·스키마 불변 검증을 통과했고, 후속 검토에서 로그 출처 중복 집계1건을 수정했다. 최종 검증 수치는 partial-treatment-local-20261118.json과 partial-treatment-review-20261118.md를 참조한다. 실서비스/Slack/장애/모델 실행0.

현재 해당 작업트리의 코드 검토·수정은 완료했고 정확 head CI 후 일반 PR 병합을 진행한다. 실제 반영 결과는 원격 PR 및 CI 기록으로 확인한다. 이후 승인된 실제 배포에서 새 Slack 요청으로 검증한다. C04/C05는 미완료이고 이번 로컬 개선은 새 실습 승인에 해당하지 않는다. 추천 GPT-6 SOL · High.

아래는 2026-11-17 정리 시점의 기록이다. 2026-11-18에는 재설치·정상 Slack 관측 검증까지 진행했으며, 이번 partial 코드 검토 작업에서는 실제 환경을 변경하지 않았다.

- 완료: 지표 초기화(플랫폼 PR159/main867ea23), 검증된 이력 연결(AI PR20), Lease 계측/CI(AI PR21/main9482120).
- 후속 코드: AI `codex/lease-version-resume-20261117`의 v0.35.8 정렬·잠금 검증·준비 비용 표본. 재설치 전에 해당 변경의 main 포함과 CI를 확인한다.
- 소스 관리는 AI 저장소, 실제 CI/원복 실행은 기존 플랫폼 계약이다. 저장소 literal·OIDC·workflow·binding을 일괄 변경하지 않는다.
- 기존 시간 제한·실행별 승인·정확 head 사람 리뷰·UID/CAS/ACK·원복/회수 조건 불변. C04/C05/AI 전체 완료를 주장하지 않는다.
- Terraform apply/destroy는 일반 사용자 터미널 원칙으로 복귀했다. 이번에는 설치·실제 연결·실습을 실행하지 않았다.

재설치 순서·최신 소스·보존 자료·남은 작업은 [재설치 인계](ai-ops/docs/offline-resume-20261117.md)를 기준으로 한다. 오래된 설치/삭제 대기 기록의 명령을 재실행하지 않는다. 원래 상세 HANDOFF와 미커밋 검증 문서는 사용자 전용 `~/.local/share/url-shortener-ai-ops/offline-resume-20261117/before/`에 보존했다. 현재 Git 문서에는 비밀값/개인 사건/DB/state 원문을 포함하지 않는다.

다음은 재설치 후 최신 이미지/새 큐/정상 CI/사용자 새 Slack 요청 검증이다. 선택형 계측 실제 연결과 C04/C05 실습 재개는 별도 범위를 확인한다. 다음 추천 **GPT-6 SOL · High**.
