# 작업 인계 · 2026-11-17 최신 정리

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
