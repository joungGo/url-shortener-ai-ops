# WF10 CI relay 네트워크: 고정 목적지 proxy 경유 (PR-a) · 2026-11-26

상태: 로컬 구현과 시험만 했다. 기본 동작은 바뀌지 않는다(flag와 환경 변수가 없으면 이전과 같음). renderer 연결은 다음 PR-b에서 한다.

## 배경
- WF11 실제 실행에서 WF07이 `normal_ci_history_unavailable`로 안전하게 멈췄다. WF10에는 정상 CI 영수증을 store에 넣는 **CI relay가 없다.**
- 사용자 결정(2026-11-26): **P1 + N1.**
  - P1: relay를 WF10 메인 Pod의 컨테이너로 둔다.
  - N1: 메인 Pod는 인터넷 직접 송신 없이 기존 고정 목적지 egress proxy만 거친다.
- relay가 읽는 곳은 두 종류다.
  - GitHub API `api.github.com`: 기존 proxy가 이미 허용한다.
  - 정상 CI 영수증 artifact의 **서명 다운로드 위치**
    - 2026-11-26 실측: `302 Location: productionresultssa9.blob.core.windows.net`
    - 코드가 받는 다른 위치: `results-receiver.actions.githubusercontent.com`

## 변경
1. **`ci_token_reader.TokenGitHubReader(token_file, proxy=None)`**
   - 지정한 proxy 주소만 쓴다. 형식은 `http://<이름>:<포트>`, 기존 WF07 proxy 검사와 같다.
   - 환경 변수 proxy(`HTTPS_PROXY` 등)는 계속 쓰지 않는다.
   - API 요청과 서명 다운로드가 모두 그 proxy를 거친다. 리다이렉트 위치 검사와 "토큰은 API에만"은 그대로다.
2. **`ci_relay` main**
   - `AI_CI_EGRESS_PROXY`가 있으면 토큰 읽기 코드에 넘긴다.
   - proxy가 있는데 토큰 파일(`AI_CI_GITHUB_TOKEN_FILE`)이 없으면 고정 오류로 중단한다. 그 경우 `gh` CLI 경로로 새어 나갈 수 있기 때문이다.
3. **`recovery_egress_proxy`**
   - `GITHUB_ARTIFACTS` 정확한 패턴을 추가했다.
     - `productionresultssa(0|[1-9][0-9]?).blob.core.windows.net:443`
     - `results-receiver.actions.githubusercontent.com:443`
   - **`--github-artifacts`일 때만** 켜진다. WF07 전용 배포의 허용 목적지는 그대로다.
   - 패턴도 443만 허용한다. Host 헤더 일치, 공인 IPv4 확인, 터널 제한, 무기록은 그대로다.

## 남는 위험
- relay가 장악되면 GitHub 소유 artifact 저장소까지는 연결할 수 있다. 다른 Azure 저장 계정이나 임의 호스트는 패턴으로 막힌다.
- 서명 URL에는 토큰을 보내지 않는다.

## 검증
- 새 시험 10개(`test_ci_relay_proxy.py`)
  - 지정 proxy만 쓰고 환경 변수는 무시
  - proxy 형식 위반 거부
  - relay가 proxy를 읽기 코드에 전달
  - 토큰 없는 proxy와 잘못된 proxy는 고정 오류로 중단(값 비출력)
  - 패턴 기본 꺼짐
  - 켜졌을 때 GitHub 저장소만 허용(번호 범위·접미사·포트·Host 불일치 거부)
  - flag가 패턴을 제어
  - 443 아닌 패턴 거부
  - **실제 `ProxyServer`가 켜졌을 때만 터널을 연다(200/403)**
- 관련 시험 57개 통과. 결함 변형 12/12 검출. 처음 실행했을 때 살아남은 2개는 접미사 포트와 서버 경로 시험을 추가해 잡았다.
