# AI 저장소 작업 지침

- 2026-11-19 사용자 상시 인계 요청: **Codex 작업마다 언제든 Claude에서 이어갈 수 있게 별도 요청 없이 인계 자료를 갱신한다.** 구현·검토/CI·설치/정리·Slack 연결·실습에 공통 적용한다. 작업 시작 시 `/Users/jounghyeon/dev/Url-Shortener-EKS-Platform/docs/handoff/claude/CURRENT.md`와 `UPDATE-PROTOCOL.md`를 읽고, 의미 있는 단계 완료·승인/로그인 대기 전·장시간/외부 변경 작업 전후·최종 응답 전에 최신 상태를 저장한다. 현재 작업트리/branch/SHA·미커밋 경로·검증/CI·실제 실행과 미실행·다음 한 작업·차단 이유·유효 승인 범위/만료·자기 실행 프로세스/잠금/회수 상태·보존 자료 위치를 구분한다. CURRENT/HANDOFF/작업표를 우선 갱신하고 경로/절차 변경 시 CLAUDE/README/TASKS도 갱신, 마지막으로 inventory 해시/검증 시각을 맞춘다. 비밀값·원본 자격·state/DB 내용은 복사하지 않는다. 오래된 사실을 현재 조회로 표시하거나 과거 승인을 새 실행 권한으로 넘기지 않는다. 새 작업트리는 이 규칙과 최신 인계 위치를 먼저 연결한다. 인계만을 위한 클라우드 조회/설치·자동 commit/push·별도 예약/백그라운드 프로세스는 만들지 않는다. 변경 없는 설명만 한 경우 이력을 불필요하게 누적하지 않는다.

- AI 구현·시험·전용 chart/Terraform 코드는 이 저장소에서 관리한다. 운영 앱/EKS/RDS/VPC는 f-lab-edu/F-Lab-DevOps, 격리 복구 PR은 joungGo/c03-execution-lab 소유다.
- integration/README.md와 HANDOFF.md를 먼저 읽는다. 실제 실행 저장소와 코드 저장소가 다르므로 repository literal·workflow ID·승인·binding을 일괄 치환하지 않는다.
- terraform apply/destroy는 사용자가 자기 터미널에서 실행한다. Codex는 새 plan·대상/state/보존 대조·명령을 준비하고 결과를 검증한다.
- 클라우드/모델 호출 없이 로컬 검사부터 수행한다. 장애 주입은 매 실행 새 준비 결과의 사용자 승인이 필요하고 복구 PR의 정확한 head에 대한 실제 사람 리뷰/CI는 별도다. 원복·잠금·기한을 완화하지 않는다.
- 개인 사건·DB·키·토큰·Terraform state/입력은 Git에 넣지 않는다. 기존 개인 보존 경로와 urlshortener-operator 및 로그인 자격을 유지한다. 새 AWS 계정/프로필을 만들지 않는다.
- 기존 범위의 격리 자격 예외는 사용자에게 AI 구현 완료 통보를 받을 때까지 원래 승인 대상/권한/짧은 기간/회수 범위에만 적용한다. 새 저장소로의 자격/권한 확대를 이미 승인됐다고 간주하지 않는다.
- C04/C05 미완료·A5/A7 미측정·기존 B 범위를 사실대로 기록한다. AI 포트폴리오는 사용자의 구현 완료 통보 전까지 보류한다.
- 다음 작업 추천 모델/추론 깊이: GPT-6 SOL · High.
