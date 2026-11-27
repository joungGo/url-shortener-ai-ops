# lab NetworkPolicy: API 서버 Pod proxy 허용 (복구 정상 판정 경로) · 2026-11-26

상태: 로컬 구현과 시험만 했다. 실제 확인은 다음 설치의 S0 점검에서 한다.

## 문제
- 복구 정상 판정(`recovery_observe`)은 relay가 API 서버 `pods/proxy`로 `lab-api` Pod의 `:8000/readyz`·`/items`를 GET해서 확인한다.
  - 읽기 전용 RBAC를 쓴다(`pods/proxy get`).
  - 이 연결을 실제로 거는 쪽은 **EKS 제어 영역(API 서버)**이다.
- lab 정책(`b6_lab.build_lab`)은 `lab-deny`로 모두 막고, `lab-api` 수신은 **Prometheus Pod → 8000번만** 허용했다.
- 2026-11-26 WF11 실제 실행에서 API 서버 proxy 요청이 **시간 초과**됐다(`dial tcp <Pod IP>:8000: connect: connection timed out`). 회복 확인은 Pod 안에서 직접 요청해 대신했다.
- 이 상태로는 정상 판정이 `unknown`이 된다. 그러면 **S2a의 기대 결과 `already_recovered`가 나올 수 없다.**

## 변경 (사용자 결정 2026-11-26, A안)
- `lab-api` NetworkPolicy 수신에 **`api_server_cidrs`(입력, IPv4 `/32`만 허용)에서 오는 TCP 8000**을 하나 추가했다.
  - EKS API 서버가 Pod로 연결할 때 출발 주소는 `kubernetes` Service endpoint(제어 영역 ENI) IP다. 수집기 송신 허용에 이미 쓰는 그 입력이다.
  - 입력에 함께 들어가는 Service IP(`172.20.0.1/32`)는 출발지가 될 수 없지만, 넣어 둬도 해가 없다.
- 그대로 둔 것:
  - Prometheus 허용
  - DB·Redis·수집기 정책
  - `lab-deny`
  - 다른 포트
- 접근 통제는 지금처럼 RBAC가 맡는다. 이름이 제한된 읽기 전용 `pods/proxy get`, 고정 GET 경로 두 개다.
- 버린 대안(B): relay → lab-api Service로 직접 요청하는 방식. WF10 Pod에 앱 직접 접근 권한을 줘야 하고, 송신·수신 정책 두 개와 판정 코드를 바꿔야 해서 범위가 더 크다.
- 남는 제약: EKS가 제어 영역 ENI를 교체하면 IP가 바뀐다. 그때는 lab 정책을 다시 렌더링해야 한다. 수집기 송신 규칙과 같은 제약이다.

## 검증
- 시험 5개(`test_lab_apiserver_proxy.py`)
  - `lab-api` 수신은 정확히 두 규칙(Prometheus, API 서버 `/32`)이고 둘 다 8000번이다.
  - 다른 lab Pod는 API 서버에 계속 닫혀 있다.
  - 연 포트가 정상 판정이 읽는 포트(`recovery_observe.HTTP_PORT`, 컨테이너 포트)와 같다.
  - `validate_lab`이 넓힌 출발지·다른 포트·포트 제한 제거·추가 CIDR을 거부한다.
  - 입력은 여전히 대역(`/16`, `/0`)을 거부한다.
- 관련 시험(lab·GitOps·CI 서비스·정상 CI·이미지 시나리오) 64개 통과.
- 결함 변형 7/7 검출.
- **다음 설치 S0 점검에 추가한다:** API 서버 proxy로 `lab-api` `/readyz` 200(`kubectl get --raw /api/v1/namespaces/ai-ops-lab/pods/<pod>:8000/proxy/readyz`).
