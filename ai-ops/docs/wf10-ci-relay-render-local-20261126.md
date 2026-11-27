# WF10 renderer: CI relay 컨테이너 연결 (PR-b) · 2026-11-26

상태: 로컬 구현과 시험만 했다. 실제 확인은 다음 설치에서 한다. 선택 입력이 없으면 출력이 이전과 바이트 단위로 같다.

## 배경
- WF11 실제 실행에서 WF07이 `normal_ci_history_unavailable`로 안전하게 멈췄다. 정상 CI 영수증을 store에 넣는 CI relay가 없었기 때문이다.
- 사용자 결정은 P1(메인 Pod 컨테이너) + N1(egress proxy 경유)이다. 네트워크 부분은 PR-a(#80)로 병합했다.

## 변경
- 새 선택 입력 **`recovery.ci_relay = {"secret": "<Actions 읽기 토큰 Secret 이름>"}`**. 다른 키는 거부한다.
  - Secret 이름은 다른 모든 Secret과 달라야 한다(자격 분리 검사에 포함).
  - recovery 절 밖에 두면 거부한다.
- 입력이 있으면 아래를 렌더링한다.
  - **메인 Pod에 `ci-relay` 컨테이너**
    - 실행: `python -m ai_ops.ci_relay --config /etc/ai-ops --store /data/incidents --watch --normal-binding /etc/ai-ops/normal-ci-binding.json`
    - store를 worker와 같이 쓴다. worker는 이미 정상 CI inbox를 처리해 `ready`로 바꾼다.
    - 설정과 binding은 init 복사본(PR77)을 쓴다.
    - 환경: `AI_CI_GITHUB_TOKEN_FILE=/run/ci-github/token`, `AI_CI_EGRESS_PROXY=http://<name>-egress.<ns>.svc:8080`
    - **토큰은 이 컨테이너에만** init 복사로 넣는다. 다른 컨테이너와 송신·egress Pod에는 없다.
    - probe는 없다. relay는 실행마다 오류를 삼키고 계속 조회하며, 시작 실패는 설정이 잘못됐을 때뿐이다.
  - **egress Pod 인자 `--github-artifacts`.** PR-a의 GitHub artifact 저장소 패턴을 켠다.
  - 메인 Pod NetworkPolicy는 바꾸지 않는다. 인터넷 직접 송신은 없고 egress proxy만 쓴다.
  - `external_prerequisites`에 세 가지를 추가했다.
    - F-Lab-DevOps Actions 읽기 토큰
    - 정상 CI 설치 단계(AI root `enable_b6_normal_ci`, RBAC·환경 변수, 실행마다 GitHub 환경 승인)
    - 정상 CI는 lab Pod가 확정된 뒤 실행하고, relay는 15분 이내 실행만 받는다는 점
  - `residual_risks`에 두 가지를 추가했다.
    - proxy가 GitHub artifact 저장소도 중계한다.
    - relay가 반복 실패하면 메인 Pod가 NotReady가 될 수 있다. 설정 오류일 때뿐이며, 그때는 다른 프로세스도 멈춘다.
- 경로 정책: `/etc/ai-ops`(입력)와 `/data/incidents`(store)는 relay CLI 경로 정책에서 허용된다.

## 검증
- 새 시험 5개(`test_wf10_ci_relay_render.py`)
  - 입력이 없으면 relay와 flag가 없다.
  - relay 컨테이너 계약: 명령, 인자, 환경, 마운트, store 공유, binding 존재
  - 토큰 보유자는 relay 하나이고, Secret 이름은 출력에 한 번만 나온다.
  - 메인 Pod와 egress Pod의 NetworkPolicy가 입력이 없을 때와 같고, egress 인자는 `--github-artifacts`다.
  - 입력 검증: 추가 키, 누락, 이름 형식, Secret 재사용, 형식, 알 수 없는 recovery 키, recovery 밖 배치
- **입력이 없을 때 바이트 동일성:** main 렌더러와 이 branch 렌더러의 manifest hash가 기본, recovery, restart 세 fixture 모두 같다(`9eaeca0b…`, `3d393230…`, `54a7c734…`).
- 기존 WF10·WF11 시험 59개 통과. 결함 변형 11/11 검출. 처음 살아남은 1개(알 수 없는 recovery 키 허용)는 시험을 추가해 잡았다.
