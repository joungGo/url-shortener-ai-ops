# WF10 renderer: WF07 PR 발행 토큰 입력 (A2, PR-d) · 2026-11-27

상태: 로컬 구현과 시험만 했다. 입력이 없으면 출력이 현재 main과 같다.

## 배경
- PR-c(#82)로 relay가 준비 다음 tick에 C03 writer 앱으로 PR을 여는 코드가 들어갔다.
- 이 코드는 설정 `prepare.publish_token_file`이 있을 때만 켜진다.
- 이번 PR은 WF10 renderer가 그 설정과 토큰 마운트를 만들게 한다.

## 변경
- 새 선택 입력 **`recovery.publish = {"secret": "<단기 설치 토큰 Secret 이름>"}`**
  - 다른 키는 거부한다.
  - Secret 이름은 다른 모든 Secret과 달라야 한다. CI relay 토큰과도 다르다.
  - 알 수 없는 recovery 키는 계속 거부한다.
- 입력이 있으면 아래를 렌더링한다.
  - **`slack-relay` 컨테이너에만** 토큰을 `/run/ai-pr-writer/token`에 init 복사로 넣는다. relay가 발행하는 프로세스다.
  - `recovery-approval.json`의 `prepare.publish_token_file`을 `/run/ai-pr-writer/token`으로 둔다. 런타임 모델 검증을 통과한다.
  - NetworkPolicy, 송신 Pod, egress Pod는 바꾸지 않는다. GitHub 요청은 기존 egress proxy의 `api.github.com`으로 나간다.
  - `external_prerequisites`에 단기 설치 토큰을 적는다. 범위는 c03만, 권한은 contents·pull_requests 쓰기이고, 실습 전에 Mac에서 발급하고 실습 뒤 폐기한다.
  - `residual_risks`에 "토큰 유효 중 relay가 장악되면 c03에 branch·PR을 열 수 있음(병합은 보호된 사람 리뷰·CI 필요)"을 적는다.

## 검증
- 새 시험 4개(`test_wf10_publish_render.py`)
  - relay 하나만 토큰을 갖고 경로와 설정이 맞으며, 런타임 모델이 받는다.
  - 입력이 없으면 준비만 한다.
  - 네트워크와 다른 Pod가 그대로다. 문구가 들어간다.
  - 입력 검증: 추가 키, 누락, 이름, Secret 재사용, 형식, 알 수 없는 recovery 키, CI relay와 같은 Secret 거부. 두 입력을 함께 쓰는 경우도 확인한다.
- **입력이 없을 때 출력 동일성:** main 렌더러와 이 branch 렌더러의 manifest hash가 기본, recovery, restart, CI relay 네 fixture 모두 같다.
  - recovery fixture의 hash가 이전 기록과 다른 이유는 PR #82가 설정에 `publish_token_file: null`을 추가했기 때문이다. main도 같은 값이다.
- 기존 WF10·WF11 시험 64개 통과.
- 결함 변형 8/8 검출. 처음 살아남은 1개(알 수 없는 recovery 키 허용)는 시험을 추가해 잡았다.
