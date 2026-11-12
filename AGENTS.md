# AI 저장소 작업 지침

- AI 구현·시험·전용 chart/Terraform 코드는 이 저장소에서 관리한다. 운영 앱/EKS/RDS/VPC는 f-lab-edu/F-Lab-DevOps, 격리 복구 PR은 joungGo/c03-execution-lab 소유다.
- integration/README.md와 HANDOFF.md를 먼저 읽는다. 실제 실행 저장소와 코드 저장소가 다르므로 repository literal·workflow ID·승인·binding을 일괄 치환하지 않는다.
- terraform apply/destroy는 사용자가 자기 터미널에서 실행한다. Codex는 새 plan·대상/state/보존 대조·명령을 준비하고 결과를 검증한다.
- 클라우드/모델 호출 없이 로컬 검사부터 수행한다. 장애 주입은 매 실행 새 준비 결과의 사용자 승인이 필요하고 복구 PR의 정확한 head에 대한 실제 사람 리뷰/CI는 별도다. 원복·잠금·기한을 완화하지 않는다.
- 개인 사건·DB·키·토큰·Terraform state/입력은 Git에 넣지 않는다. 기존 개인 보존 경로와 urlshortener-operator 및 로그인 자격을 유지한다. 새 AWS 계정/프로필을 만들지 않는다.
- 기존 범위의 격리 자격 예외는 사용자에게 AI 구현 완료 통보를 받을 때까지 원래 승인 대상/권한/짧은 기간/회수 범위에만 적용한다. 새 저장소로의 자격/권한 확대를 이미 승인됐다고 간주하지 않는다.
- C04/C05 미완료·A5/A7 미측정·기존 B 범위를 사실대로 기록한다. AI 포트폴리오는 사용자의 구현 완료 통보 전까지 보류한다.
- 다음 작업 추천 모델/추론 깊이: GPT-6 SOL · High.
