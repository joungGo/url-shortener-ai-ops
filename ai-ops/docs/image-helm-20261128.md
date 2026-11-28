# 실행 이미지에 Helm 포함 (WF07 이중 렌더 검증) · 2026-11-28

상태: 2026-11-28 재설치 중 실물에서 발견해 고쳤다.

## 문제
- S1에서 승인과 재검증은 통과했지만 WF07 준비가 `preparation_failed`로 끝났다.
- 읽기 전용으로 재현해 보니 실패 지점은 하나였다.
  - 4개 읽기(GitHub·Kubernetes·정상 CI·ECR), 식별자 대조, 제안 초안은 모두 통과했다.
  - `c_pr_link.check_rendered_image_change`가 `helm template`을 실행하는데, 실행 이미지에 helm이 없었다(`FileNotFoundError`).
- 이 검증은 C-04 시절 helm이 있는 Mac·CI 기준으로 만들어졌다. CI unittest는 setup-helm 호스트에서 돌고, 이미지 안에서는 `--help`만 확인해서 드러나지 않았다.
- relay 경로(prepare·publish·reconcile)에서 빠진 외부 프로그램은 helm 하나다. 다른 c_* 호출은 모두 HTTP reader를 받는다.

## 변경
- Dockerfile에 `helm` 단계를 추가했다.
  - Helm v4.1.3 공식 배포본(`linux-amd64`)을 받아 sha256 `02ce9722…a4700`을 대조한다.
  - 바이너리만 실행 이미지 `/usr/local/bin/helm`에 복사한다.
- `HELM_CACHE_HOME`·`HELM_CONFIG_HOME`·`HELM_DATA_HOME`을 `/tmp/helm/*`로 지정했다. relay는 루트가 읽기 전용이고 `/tmp`만 쓰기 가능(emptyDir, `HOME=/tmp`)하다.
- CI image 작업에 스모크 시험을 추가했다. 실제 Pod와 같은 조건(통신 없음·읽기 전용 루트·`/tmp` tmpfs)에서 다음을 확인한다.
  - `helm version`이 v4.1.3인지
  - 저장소 lab 차트로 `check_rendered_image_change`가 통과하는지
- 검사 로직은 바꾸지 않았다. helm이 없으면 지금처럼 준비가 실패로 막힌다(fail-closed, 실물에서 확인).

## 보안 메모
- 렌더 입력은 c03의 고정 커밋 파일 3개(64KB 이하)다. values는 로컬에서 만들고, 출력은 Deployment 1개인지 검증한다. `helm template`은 클러스터에 접속하지 않는다.
- relay 환경 변수에는 토큰이 없다(토큰은 파일로만 읽는다). 그래서 helm 자식 프로세스에 토큰이 넘어가지 않는다.
- ECR BASIC 스캔은 Go 바이너리를 보지 않는다. helm 버전·sha256·CVE 확인은 사람이 맡는다.

## 검증
- 로컬 helm v4.1.3으로 스모크와 같은 호출이 통과했다. 이미지 안 실행은 CI image 작업에서 확인한다.
- 저장소 경계 검사 통과.
