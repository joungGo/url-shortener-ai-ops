# AI 저장소 이관 인계

## 현재 우선순위

사용자는 실습을 중단하고 인프라 정리 전 재개 자료를 보존한 뒤 AI 코드의 별도 저장소 관리를 요청했다. 이관 중 인프라 변경·실습·Terraform·모델 호출은 하지 않았다. 코드 기준은 이 저장소, 현재 live 실행 연결은 기존 플랫폼이다. [연결 전환 계획](integration/README.md)을 따른다.

새 main은 원본 main9573(PR155) 기반이다. 원본 PR156/e327의 시각 순서 보완은 별도 feature PR로 보존하며 이관이 해당 변경의 승인/병합을 대신하지 않는다. 추출 과정에서 commit SHA가 변경됐고 docs/repository-extraction.json에 원본/초기 분리 SHA가 있다. archive/pr156-filtered-head는 이관 전 PR의 추출 head, archive/root-working-copy-20261112는 구형 root의 AI 미커밋 작업 사본이다. archive 브랜치는 현재 코드에 덮어쓰지 않는다.

C04 실환경 성공·C05 최종 완료는 아직 미완료다. 마지막012는 시각 순서 거부 후 정상 원복·자격 회수가 확인됐고 새 복구 PR은 없었다. 다음에는 PR156 이관 변경의 로컬 검사부터 재개한다. 새 실행에는 새 설치 identity/정상CI·worker 로드와 별도 실행 승인·새 복구 PR 실제 리뷰가 필요하다.

## 로컬 보존 위치 (Git에 포함하지 않음)

- /Users/jounghyeon/.local/share/url-shortener-ai-ops/continuation-20261112/: Git bundle3개, 요청SQLite10개, AI PVC3개 파일125/SQLite4개, 오프라인 wheelhouse/venv, 원본 HANDOFF와 검증 기록. verify-checkpoint.py로 무결성 확인 가능.
- /Users/jounghyeon/.local/share/url-shortener-ai-ops/c04-20261112/: 실제 실습/원복/PR156 로컬880개·CI 근거.
- /Users/jounghyeon/.local/share/url-shortener-ai-ops/install-20261112/: 설치 원본. 새 배포에는 old plan·UID·토큰을 재사용하지 않는다.
- /Users/jounghyeon/.local/share/url-shortener-install/credentials/: 기존 자격 원본7개. c03-app-keys와 c2-projection-identity-20261028도 기존 개인 경로/0600으로 유지한다.

운영 state/RDS 현재 조회는 operator 권한 거부로 미확인, AI state만 현재 버전/ETag 대조 백업했다. 실제 정리 전 최신 권한/state/AWS 실물을 다시 확인한다. Terraform apply/destroy는 사용자 터미널 전용이다. Route53·RDS 최종 스냅샷·S3 state/backend·operator·기존 로그인 자격은 기존 정책대로 보존한다. 아직 인프라 삭제 완료를 뜻하지 않는다.

다음 추천: GPT-6 SOL · High.
