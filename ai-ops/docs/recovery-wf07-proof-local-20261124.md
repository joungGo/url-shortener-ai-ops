# WF07 실제 읽기 포트 조각 B: 정상 CI 근거·ECR·read_proof 조립·진입점 연결 · 2026-11-24

상태: **로컬 구현·검증 완료, PR CI/main 대기.** 기준 main `f219d046013ca131930568c59dcad01ae6937d93`(PR58).

## 배경과 결정

조각 A(PR58)에서 GitHub Git 읽기, Kubernetes 증거 읽기, 설정을 만들었다. 이번 조각은 남은 부품을 채우고 relay에 연결한다.

사용자 결정(2026-11-24, 모두 추천안):

- **정상 CI 근거:** 기존 CI relay가 GitHub에서 검증해 큐 DB에 넣어 둔 `NormalCIInbox` 기록을 재사용한다(HMAC 확인). relay Pod에는 GitHub Actions 읽기 권한이 필요 없다.
- **사용할 실행:** 설정된 정상 CI binding(`normal-ci-binding.json`)과 일치하는 가장 최근 `ready` 기록을 쓴다.
- **ECR:** boto3와 IRSA로 읽는다. 실행 lock에 hash를 고정해 추가한다.

## 구현

### 1. 의존성 (`requirements.lock`, `pyproject.toml`)

- boto3 1.44.0, botocore 1.44.0, s3transfer 0.19.3, jmespath 1.1.0, python-dateutil 2.9.0.post0, six 1.17.0, urllib3 2.8.0을 추가했다. 모두 PyPI 공개 순수 Python wheel이고 sha256을 고정했다.
- 버전은 기존 실습용 lock과 같다. 게시일은 모두 2026-11-10 이전이다.
- `--require-hashes --only-binary=:all:`로 설치되는 것을 확인했다.
- 실행 이미지가 커진다. 정확한 크기는 CI image 작업에서 확인한다.

### 2. `NormalCIInbox.latest_ready_history(before=)`

- `ready` 기록 가운데 아래를 모두 만족하는 것만 남긴다.
  - 키와 HMAC 지문이 맞아야 한다(저장 무결성).
  - 현재 binding과 결합돼 있어야 한다(`_validate`).
  - `before` 이전에 끝난 실행이어야 한다.
- 그중 **가장 늦게 끝난 1건**을 `VerifiedNormalHistory`로 돌려준다. 남는 것이 없으면 `normal_ci_history_unavailable`로 거절한다.
- `queued`, `needs_review`, 다른 binding의 기록, 변조된 기록은 쓰지 않는다.

### 3. `EcrImageChecker` (`ai_ops.recovery_proof_live`)

- 자격은 IRSA web identity만 쓴다(3-1). 리전은 `ap-northeast-2`로 고정한다. 연결 2초, 읽기 3초, 최대 2회 시도다. boto3는 실제로 쓸 때만 import한다.
- **호출자 확인:** STS 호출자가 고정 계정(`764428911194`)의 `assumed-role/<설정된 역할>/`이 아니면 ECR을 읽기 전에 거절한다.
- **정상 이미지:** `describe_images`로 정상 digest를 조회한다. 증거 함수에 넘기는 값은 `registryId`·`repositoryName`·`imageDigest` 세 필드뿐이다(태그·크기는 넘기지 않음).
- **실패 이미지가 없는지:** `ImageNotFoundException`(예외 이름 또는 오류 코드)만 "없음"으로 인정한다. 권한 오류 같은 다른 오류는 모두 `ecr_read_failed`로 거절한다. **"없음"으로 잘못 판정하지 않는다.**
- **오류 처리:** SDK 오류는 ARN·요청 ID·메시지를 담을 수 있어 고정 코드로만 바꾼다. 처음에는 정상 이미지 조회 오류가 원래 예외 그대로 밖으로 나가는 결함이 있었고, 시험에서 발견해 고쳤다.
- 각 단계 사이에 마감을 확인하고, digest 형식이 다르면 거절한다.

### 3-1. 자격 증명은 IRSA만 사용 (PR CI 실패에서 발견해 수정)

첫 PR CI([run 45351079366](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/45351079366))에서 contracts 작업이 실패했다.

**원인.** boto3는 client를 만드는 순간 자격 증명을 찾는다. 로컬에는 AWS 프로필이 있어서 거기서 끝났지만, CI 러너에는 프로필이 없다. 그래서 기본 자격 체인이 인스턴스 메타데이터(IMDS)까지 내려가 네트워크를 시도했고, 네트워크를 금지한 시험이 이를 잡았다.

**운영 위험.** Pod에서 IRSA 설정이 빠지면 같은 경로로 EKS 노드 역할 등 다른 자격으로 조용히 넘어갈 수 있다. 호출자 역할 확인이 막기는 하지만, 그 경로 자체를 타면 안 된다.

**수정.**

- `irsa_clients`는 아래 조건을 모두 만족할 때만 client를 만든다. 어긋나면 `ecr_irsa_not_configured`로 거절한다.
  - `AWS_ROLE_ARN`이 `arn:aws:iam::764428911194:role/<설정 역할>`이다.
  - `AWS_WEB_IDENTITY_TOKEN_FILE`이 절대 경로다.
  - botocore 자격 해석기에서 web identity 제공자 하나만 남긴다. 환경 키·프로필·IMDS는 쓰지 않는다.
- 자격은 지연(`DeferredRefreshableCredentials`) 상태라, 첫 호출 전에는 네트워크를 쓰지 않는다.
- ECR client는 **처음 쓸 때** 만든다. IRSA가 빠졌을 때 relay 전체(Slack 회신 포함)가 시작에 실패하지 않고, WF07 준비만 `needs_review`로 멈춘다.

**시험.**

- AWS 프로필이 없는 CI 조건에서 시험을 다시 했다.
- 환경 변수에 접근 키가 있어도 무시하고 web identity만 쓰는 것을 확인했다.
- IRSA가 없거나 역할이 다르면 첫 사용 때 거절되고, 네트워크는 0회다.
- 변형 3종(대체 경로 허용, 역할 확인 제거, 즉시 생성)을 추가해 21/21을 검출했다.

### 4. `LiveRecoveryProof`: WF07 `read_proof` 포트

순서는 다음과 같다.

1. Kubernetes 객체를 읽는다(조각 A).
2. Argo `targetRevision`이 40자 SHA인지 확인한다. 아니면 GitHub를 읽기 전에 거절한다.
3. 그 revision의 `values.yaml`을 읽는다(조각 A). 실패 digest는 여기서 얻는다.
4. inbox에서 정상 CI 기록을 가져온다.
5. ECR을 확인한다. 정상 digest는 binding의 이미지에서 얻는다.
6. 마감을 확인한다.
7. 기존 `assess_recovery_proof`를 그대로 호출한다(`repository`는 격리 저장소).

반환값은 WF07이 기대하는 `(결과, values 바이트)`다.

### 5. 연결 (`live_pr_bridge`, `slack_server`)

- **`live_pr_bridge(config, queue)`:** `config.prepare`로부터 `ledger -> ImageRecoveryPRBridge` factory를 만든다.
  - 시작할 때 binding 파일(비밀 아님)을 읽는다.
  - 토큰은 reader가 요청마다 읽는다.
  - principals, 작성자, 준비 시간은 설정값을 그대로 쓴다.
- **relay:** `recovery-approval.json`에 `prepare` 절이 있을 때만 bridge를 만든다. 보관 정리에 WF07 기록도 포함된다.
- **ingress:** 만들지 않는다. 버튼 수신만 한다.
- **결과:** 설정이 갖춰지면 실제 배포에서 요청 → 관측 → 승인 → 재검증 → **PR 준비**까지 이어진다. PR 발행(GitHub 쓰기)은 여전히 연결하지 않는다(`publish_local`은 fixture 전용).

## 연결 단계에서 할 일 (범위 밖)

- **IRSA 역할:** `ecr:DescribeImages` 하나만 허용한다(repository `urlshortener`). relay ServiceAccount에 연결한다.
- **GitHub 토큰:** 격리 저장소 Contents: Read 토큰을 마운트한다.
- **RBAC:** 관측기·Lease 읽기를 준다(조각 A·관측기 문서 참고).
- **renderer:** `recovery-approval.json`, `normal-ci-binding.json`, 토큰을 마운트하고 초안 경로를 PVC에 둔다.
- **CI relay:** 같은 큐에 정상 CI 기록을 넣도록 실행한다.

## 알려진 동작과 한계

- **inbox에 기록이 있어야 준비할 수 있다.** CI relay가 정상 CI를 접수하지 않았으면 준비는 `needs_review`로 멈춘다(fail-closed).
- **binding이 바뀌면 relay를 다시 시작해야 한다.** binding은 시작할 때 읽는다.
- **준비 한 번에 증거 읽기가 두 번이다(WF07 설계).** 그래서 ECR STS 1회 + DescribeImages 2회가 두 번 일어난다.

## 검증

시험 기준:

- 신규 `test_recovery_proof_live.py` 16개.
- 기존 `test_recovery_sources.py`의 설정 시험을 새 필수 항목(`normal_binding_file`·`aws_role_name`)에 맞게 갱신했다.
- 실제 WF07 fixture(로컬 Git·증거·초안·승인 장부) 위에 가짜 Kubernetes·GitHub·AWS를 붙였다. 네트워크는 쓰지 않았다.

시험 항목:

- **끝까지 준비:** 실제 읽기 부품만으로 같은 정확한 PR이 `prepared`가 된다. URL과 PR 번호는 없고 GitHub 쓰기는 0회다. Kubernetes는 GET만, GitHub는 `/git/` 경로만 쓴다. AWS 호출 순서는 [STS, 정상 이미지, 실패 이미지]가 2회다.
- **준비가 막히는 경우:** 실패 이미지가 레지스트리에 남아 있을 때, 다른 역할·다른 계정·IAM 사용자일 때(ECR 호출 0회), inbox가 비었거나 HMAC이 변조됐을 때.
- **inbox 선택:** binding이 맞는 최신 `ready` 기록을 고른다. `before` 경계를 지킨다. `queued`·`needs_review`·다른 binding 기록은 쓰지 않는다.
- **ECR:** 증거 필드 3개만 넘긴다. 오류 코드 `ImageNotFoundException`은 "없음"으로 본다. 실패 digest 조회의 권한 오류는 "없음"이 아니라 거절이다. digest 형식·마감·역할 이름을 검사한다. 기본 client는 고정 리전으로, 네트워크 없이 만들어진다.
- **진입점:** `prepare`가 있을 때만 relay에 bridge를 만들고, 보관 정리에 WF07을 포함한다. ingress는 bridge를 만들지 않는다. `prepare`가 없으면 bridge가 없다. bridge에는 설정된 principals, 작성자, 준비 시간이 그대로 들어간다.
- **SHA가 아닌 Argo revision:** GitHub와 AWS 호출 0회로 거절한다.

변형 검사:

- **결과:** 결함 변형 21종을 모두 검출했다(IRSA 3종 포함). 전체 환경을 상속하고 기준선을 먼저 확인하는 실행기를 썼다.
- **처음 결과는 15/18이었다.** 남은 3종은 다음과 같이 처리했다.
  - 실패 digest 조회 오류를 "없음"으로 보는 변형: 진짜 빈틈이어서 시험을 추가했다.
  - revision 검사 제거: GitHub reader도 막지만, 오류 코드와 읽기 0회를 시험으로 고정했다.
  - 작성자 값 변경: 설정 전달을 확인하는 시험을 추가했다.

시험 작성 중 확인한 것:

- **정상 CI 영수증:** PR fixture의 영수증은 완료 시각만 바꿔 둔 것이라 영수증 불변식(시작 ≤ 수집 ≤ 완료)을 어긴다. 시험에서는 영수증의 모든 시각을 같은 만큼 옮겨 유효한 영수증을 만들었다. 제품 코드는 그대로다.
- **가짜 시계:** WF07의 두 번째 증거 확인은 첫 번째보다 엄격히 늦어야 한다. 그래서 가짜 Kubernetes 응답마다 가짜 시계를 조금씩 진행시켰다. 실제 운영에서는 시간이 흐르므로 해당하지 않는다.

전체 결과는 [검증 기록](recovery-wf07-proof-local-validation-20261124.json)에 있다.

실제 클러스터·AWS·GitHub 쓰기·배포·Slack·장애 실습·토큰/역할 발급 없음. 기존 시간·실행별 승인·정확 head 리뷰/CI·잠금·원복 조건 변경 없음.
