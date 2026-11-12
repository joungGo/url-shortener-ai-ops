# C-02 실제 장애와 인증된 해결 요청·v2 초안 검증

2026-11-08, 계정 `764428911194`, 대상 `ai-ops-lab/lab-api` 한 개에서 실행했다. 실행별 검토안 SHA-256 `87d555765cd4f950ac6cc7bbd5b3f60587cd6409222e6d72d353d7921c90ade8`에 대해 사용자가 명시적으로 승인했다.

## 결과

- 정상 기준선 `9383e2df7f0850ea5e492a57b902f449d190fe06`과 digest 한 값만 다른 실패 소스 `a675bb297405a09dbf287ab374038ae8a2384b4e`를 사용했다. 실패 브랜치는 main에 병합하지 않았다.
- GitHub 독립 원복 실행 [42483879493](https://github.com/f-lab-edu/F-Lab-DevOps/actions/runs/42483879493)과 별도 로컬 감시의 준비 완료를 확인한 뒤 주입했다. 실행기는 170초에 조사 중단·원복을 시작하도록 제한했다.
- 22:14:59 KST에 실패 소스 전환을 시작하고 현재 활성 revision·Pod 이미지 취득 실패·Ready 0·빈 Lease를 검증했다.
- 일회성 `c2-source-capture-1794143697` Job이 기존 암호화 큐에 접수했다. worker가 사건 `inc-37dbbd1bd9d3489788f9bb660823be76`, 실행 `run-371be15a8cfc4a45b82120c6ca185928`을 저장했다.
- 사건은 `partial`이다. 자료 누락이 남은 부분 보고서이며 완전한 조사나 확정 원인으로 표시하지 않는다. 필요한 이미지 장애 근거 결합·정상 CI·ECR·현재 소스·인증 사용자와 해결 요청 대조는 통과했다.
- 인증된 해결 요청 `creq-e050bc4dd5ff4507ad916a9f48468e40`을 생성하고 초안 직전 재조회까지 통과해 v2 초안 `cprop-2d22d58019da460e9830a607fecdb853`을 저장했다.
- 감사 결과는 `proof_ready=true`, `incident_ready=true`, `proposal_ready=true`, 차단 사유는 `draft_only_c3_approval_required`다. 초안 상태 조회는 이벤트 1건·상태 `draft`·버전 1을 반환했다. 만료는 2026-11-09 22:15:40 KST다.
- 22:15:40 KST에 원복을 시작했다. 주입 시작 약 41초 후이며 승인된 3분 이내다. 22:15:51 KST에 정상 revision·원래 digest·Synced/Healthy·Ready 1 복귀를 확인했다.
- 운영 API는 HTTPS 200·TLS 검증 0, 모델 gateway/proxy는 0/0이었다. 유료 모델 호출·운영 앱 변경·복구 PR·병합·AI의 복구 변경 실행은 없었다. 조사는 현재 규칙 엔진을 사용했다.

## 근거와 한계

비공개 근거는 `/private/tmp/urlshortener-runtime-20261108/`의 `c2-approved-result.json`, `c2-v2-audit.json`, `c2-v2-proposal.json`과 `c2-operator/`의 개인 DB에 보관한다. 초안 파일 SHA-256은 `1d0aa14756362d32f08867239bf1a0347b95d3f301243108dc8158c9cc3a783e`다. 원문 로그·질문·토큰·자격은 이 문서에 포함하지 않는다.

이번 결과는 C-02의 실제 장애 → 인증 요청 → 검증된 v2 draft 연결을 확인한 것이다. C-03의 특정 변경 승인·PR review·commit/diff 검증·실제 복구 변경 실행을 검증한 결과는 아니다. 초안이 남아 있어도 현재 장애는 이미 원복됐으므로 향후 실행 전에는 원본 서비스 상태와 승인 조건을 다시 확인해야 한다.

후속 정리도 완료했다. 새 실습 Pod `lab-api-676558b8db-kdkbz`로 정상 CI 결합값·RBAC·GitHub 변수·공유 ConfigMap을 갱신하고 이전 Pod 이름 권한을 제거했다. 전달기 중지 → worker 중지/설정 반영 → worker Ready → 전달기 Ready 순서로 같은 PVC를 유지했다. 현재 worker `ci-worker-588d67cb5-6fzpj`는 2/2 Ready, relay는 1/1 Ready·재시작 0회다.

C-02 Role도 새 worker 한 개로 회전하고 새 1시간 자격을 발급했다. 새 Pod 조회 yes, 이전 Pod·Secret 조회 no를 확인했다. 제한된 TLS 경로에서 위 사건과 이미지 장애 결합값을 재조회해 PVC 보존을 확인한 뒤 포트 전달을 종료했다. 로컬 운영자 서비스도 종료했다. GitHub 독립 원복 실행과 로컬 감시 모두 정상 기준선 검증 성공으로 종료됐다. 새 Pod의 정상 CI binding 준비 검사만 수행했으며 정상 CI를 불필요하게 반복 실행하지 않았다.
