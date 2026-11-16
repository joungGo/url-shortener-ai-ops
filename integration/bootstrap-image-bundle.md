# 설치용 이미지 산출과 등록 경계

AI 코드 저장소에서 AWS 자격이나 OIDC 권한 없이 검증한 이미지를 만들기 위한 수동 workflow다. 이 경로는 기존 공개 플랫폼의 배포 승인/OIDC 경로를 변경하거나 새 저장소에 배포 권한을 부여하지 않는다.

`ai-ops-oci-bundle.yml`은 이 저장소의 main에서만 실행되며 contents:read만 사용한다. 전체 계약 시험·비root/읽기전용/통신차단 실행을 통과한 linux/amd64 이미지를 docker-archive와 출처 JSON·SHA256 목록으로 비공개 artifact에7일 보관한다. 등록 기록의 published=false는 그대로 유지한다. 키·토큰·사건 원문은 산출물에 넣지 않는다.

설치 담당자는 현재 검토한 main SHA·실제 성공 run/jobs·workflow_dispatch·동일 저장소/branch와 artifact 귀속·체크섬·image ID/소스 label/파일 해시를 대조한 뒤 기존 설치 관리자 자격으로 목표 ECR 하나에만 digest 보존 import한다. ECR manifest config ID와 현재 스캔 결과를 확인하고 별도 설치 증거에 등록 사실을 기록한다. CI가 ECR에 등록한 것처럼 원본 artifact를 고쳐 쓰지 않는다.

새 저장소의 AWS Secret/environment·OIDC trust는 추가하지 않는다. 실제 정상 관측/원복의 control repository는 현재 integration 계약대로 기존 플랫폼을 유지한다. 전체 control 이전과 private chart GitOps 연결이 완료된 것으로 표시하지 않는다. 이 이미지 산출은 장애 주입·복구 PR 리뷰·실습 실행 승인이 아니다.
