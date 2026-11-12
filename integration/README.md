# 플랫폼과 AI의 연결 계약

AI 구현·테스트·모델/규칙 조사·전용 chart와 AI Terraform root의 코드 기준은 이 저장소다. 운영 앱·RDS/EKS/VPC·운영 GitOps는 `f-lab-edu/F-Lab-DevOps`, 장애/복구 PR 대상은 기존 `joungGo/c03-execution-lab`이 소유한다.

- 이미지: 이 저장소의 검토된 commit → AI 이미지 빌드/검사 → 검증된 digest → 플랫폼 Helm 입력. 앱 이미지는 플랫폼 소유다.
- 관측: 플랫폼의 정제 CI artifact·현재 binding/Pod identity → AI 조사 worker. 기존 repository literal은 신뢰 경계로 유지하며 새 코드 저장소 이름으로 일괄 치환하지 않는다.
- GitOps: AI chart 경로는 `ai-ops/chart`, 실습 chart는 `ai-ops/lab`. chart source 전환은 private repo 읽기 자격과 검토한 전체 SHA가 필요하다. 기존 클러스터나 AppProject를 이번 분리에서 변경하지 않는다.
- Terraform: `terraform/ai-ops-registry`는 AI root 소스다. 기존 backend key와 리소스 주소를 유지하며 state를 이동/복원하지 않는다. production OIDC는 외부 의존성이다. apply/destroy는 사용자 터미널 전용이다.
- 플랫폼 chart 통합 검사: `platform-source.json`의 원본 commit만 읽는다. CI는 공개 플랫폼의 chart/Prometheus 파일만 sparse checkout한다. 운영 앱 구현·배포 workflow는 새 저장소에서 실행하지 않는다.
- 테스트 fixture: `ai-ops/tests/fixtures/platform/.github/`는 고정된 운영 CI·검증 wrapper 계약이다. 원본 구현 변경은 원본 저장소에서 하고 AI 통합 의존성을 검토해 갱신한다.

## 재설치 때 연결 전환

새 저장소에는 GitHub Secret·배포 환경 승인·AWS OIDC 권한·App 설치를 복제하지 않았다. 현재 실제 환경은 기존 실행기와 그대로 연결돼 있다. 복사한 live workflow는 기존 repository identity에서만 실행하도록 잠겨 있으며 새 저장소에서는 skip된다. `ai-ops-image`도 publish=false 빌드만 가능하다. 자동 CI는 클라우드 인증·유료 모델 호출 없이 수행한다.

1. PR156 이관 PR의 정확한 head/CI/리뷰를 확인하고 정상 반영한다. 기존 PR 예외 승인은 없다.
2. 새 저장소의 이미지 등록 environment/main 제한/별도 승인과 정확한 repository/environment OIDC 신뢰를 계획한다. 사용자 apply 후 현재 역할을 확인한다. 검토 없이 trust 대상을 늘리지 않는다.
3. 실제 CI/원복을 옮길 때 control repository, workflow ID, 정상 CI source revision, artifact 조회자·collector binding을 함께 새 계약으로 변경·시험한다. `supervise_c4_trial.py`와 c_trial_guard 계열은 아직 기존 실행 저장소를 고정하고 있다. 단순 repo명 치환이나 workflow 복사만으로 새 실행을 시작하지 않는다.
4. private AI chart 읽기 자격과 검토 commit을 등록하고 새 PVC/UID/정상 CI/worker 설정 로드를 확인한다. 과거 토큰·승인·큐를 재사용하지 않는다.
5. 기존 새 실습 승인/새 PR 정확 head 사람 리뷰·독립 원복·기한을 유지해 C04를 재검증한다. 전환 확인 뒤에만 플랫폼 저장소의 AI 레거시 복사본을 별도 PR로 제거한다.

이관은 코드 관리 분리다. 실습 성공·인프라 설치/정리·운영 실행기 전환 완료를 의미하지 않는다.
