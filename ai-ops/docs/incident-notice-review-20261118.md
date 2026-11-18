# WF-03 경보 공지·사건 참조 코드 검토

2026-11-18 KST. review-bugbot 스킬의 직접 검토 경로를 사용했다. 별도 Bugbot 도구가 없어 서브에이전트는 실행하지 않았다. AI 기준 main9c9f3fc, 플랫폼 기준 main01ca82b. 사용자 요청 범위는 코드 검토·보완·CI 통과 후 양쪽 main 반영이며 실제 배포/Slack/장애 실습은 제외한다.

## 발견과 보완

| Severity | Location (file:line) | Finding |
| --- | --- | --- |
| P2 | cluster-addons/prometheus/wf03/prepare.py | custom title/text를 넣어도 기본 Slack fallback은 전체 CommonLabels와 외부 URL을 렌더링한다. 실제 고정 amtool로 합성 비공개 라벨 노출을 재현하고 fallback 고정·title_link 비활성으로 보완했다. |
| P2 | cluster-addons/prometheus/wf03/prepare.py | tplConfig/stringConfig/useExistingSecret이 있으면 검토한 후보 config가 무시되거나 다른 템플릿 처리에 들어갈 수 있었다. 이런 대체 소스와 추가 Slack 템플릿 필드를 거절한다. |
| P2 | .github/workflows/ai-ops-ci.yml (양쪽) | 실제 Go template 검사는 환경변수가 없으면 skip되고 플랫폼 검사는 기존 CI 대상이 아니었다. 체크섬 고정 amtool·해시 잠금 PyYAML·정확 플랫폼 SHA/파일 해시를 연결한 검사를 필수 C03 gate의 의존성에 추가했다. |

공식 기본값 근거: [Slack 기본 template](https://github.com/prometheus/alertmanager/blob/v0.32.0/template/default.tmpl), [config defaults](https://github.com/prometheus/alertmanager/blob/v0.32.0/config/notifiers.go). 본문만 검증하지 않고 후보의 text/title/fallback 전체를 실제 renderer로 검사한다. 추가 검토에서 AI 인증·기본 경로 유지·commit 후 ACK·참조 정밀도·만료/통지 unknown 경계의 새 오류는 발견하지 않았다.

## 검증

- 플랫폼 실제 renderer/라우팅 10개 통과. 기본 fallback의 노출 재현, 보완 후 비출력, 대체 config 거절 회귀를 포함한다.
- AI 기존 1,334개 전체 시험에 사용한 코드/시험 4개는 해시가 동일하다. 보완된 플랫폼과 새 필수 CI wrapper로 9개 TLS·참조 시험을 다시 실행해 skip 없이 통과했다. 전체 시험을 의미 없이 반복하지 않았다. 기존 집중50개 결과도 보존한다.
- AST·양쪽 diff·AI 저장소 경계·workflow YAML/gate/pin 검증을 수행한다. 소스/로그 해시는 [검토 검증 JSON](incident-notice-review-validation-20261118.json)에 기록한다.
- 원래 `incident-notice-local-validation-20261118.json`은 수정 전 로컬 구현 기록이며 플랫폼 파일 해시는 이 검토 검증 JSON이 최신이다.

## 반영 순서와 실행 경계

플랫폼 [PR #163](https://github.com/f-lab-edu/F-Lab-DevOps/pull/163)의 head `d6c2c8fca50feb8453948be284e7b9b7cccb5284`를 AI `integration/wf03-platform-source.json`에 고정했다. 읽기 전용 교차 시험용이며 기존 platform-source.json의 배포/실행 pin은 바꾸지 않았다. AI CI는 다른 revision/파일이나 누락된 도구로 검사를 생략하지 않는다. 플랫폼 필수 리뷰1명·관리자 적용을 유지하고 새 보호 변경은 별도 사용자 결정 없이 실행하지 않는다.

이 기록 작성 시 양쪽 원격 CI/main 반영은 진행 중이며 최종 PR·main 상태는 최신 HANDOFF/작업표에 기록한다. CI가 통과하고 해당 저장소 리뷰 조건이 충족된 head만 병합한다. 기존 배포 values/Application·worker·실제 수신 설정은 변경하지 않는다. CI는 이미지 빌드 검증만 하고 등록/배포하지 않는다.

기존 3/15/120/210/240/300/120초·실행별 승인·정확 head 사람 리뷰/CI·잠금/ACK·원복/회수는 유지한다. 실제 배포·Slack 전송·장애 실습·Terraform 적용·유료 모델 호출 없음. C04/C05 미완료. 다음은 WF-04의 인증된 사건 지정 조사·동일 thread 결과·보관 호출 로컬 연결이다. 추천 GPT-6 SOL · High.
