# WF-10 Slack 송신 경로 설계 · 2026-11-19

상태: **결정 B′(무상태 Slack sender 분리) · 로컬 구현 완료([구현 기록](wf10-slack-sender-local-20261119.md)) · 실클러스터 미검증.** 배포 변경 없음. 실제 환경 값은 추측하지 않았고 AWS/Slack 실물을 조회하지 않았다.

## 문제

WF-10 후보 renderer(main `1f20442`)는 Slack 송신을 입력 `network.slack_cidrs`(공인 `/32` 목록)로만 허용한다. 실제 연결에서 이 방식은 유지할 수 없다.

## 확인한 사실

| 항목 | 근거 | 의미 |
| --- | --- | --- |
| Slack 송신 코드 | `slack_web.py` `SlackWebTransport._request`: `http.client.HTTPSConnection('slack.com', 443)`, 기본 TLS 검증, 메서드 3종(auth.test·bots.info·chat.postMessage)만 허용 | 목적지 호스트가 코드에 고정돼 있다. proxy(`HTTPS_PROXY`) 지원은 없다. |
| Slack 송신 주체 | `slack_server`: ingress는 SlackRuntime/bot 토큰을 만들지 않음, relay만 송신 | 인터넷 송신이 필요한 프로세스는 **relay 하나**다. |
| Slack 주소 | Slack은 고정 송신 IP 목록을 공식 제공하지 않으며, AWS 기반 동적 주소로 알려져 있다(공식 문서 아닌 커뮤니티 근거 포함) | `/32` 고정 목록은 주소가 바뀌면 끊긴다. |
| 클러스터 CNI | 플랫폼 main `bf6bcbc` `terraform/main.tf`: VPC CNI `enableNetworkPolicy=true`, `eks_managed_node_groups`, 단일 NAT GW | 표준 NetworkPolicy는 IP 기반만 지원한다. |
| AWS 도메인 정책 | EKS 문서: `ApplicationNetworkPolicy`의 DNS 규칙은 **EKS Auto Mode 노드에서만** 적용 | 현재 관리형 노드그룹에서는 쓸 수 없다. |
| 선례 | `B6-ci-service.md`: ci-worker·ci-relay 두 Pod가 같은 RWO PVC 공유, `OnRootMismatch`, 노드 이동 시 relay 먼저 중지 | 같은 노드에서 PVC를 공유하는 Pod 분리 배치를 실제 운영한 기록이 있다. |

## 선택지

| 안 | 내용 | 장점 | 단점/위험 | 추가 인프라·비용 |
| --- | --- | --- | --- | --- |
| **A. 단일 Pod + 인터넷 443** | 현재 Pod 전체에 `0.0.0.0/0:443` 허용, 사설·CGNAT·링크로컬(IMDS 169.254.169.254 포함)·loopback 대역 제외 | 변경 최소, 코드가 목적지를 slack.com으로 고정 | worker의 K8s 토큰·collector 자격이 있는 Pod까지 인터넷 443이 열림(컨테이너 침해 시 유출 경로) | 없음 |
| **B. relay Pod 분리 + relay만 인터넷 443** (권장) | relay를 별도 Deployment로 분리해 같은 PVC를 같은 노드에서 공유(podAffinity). relay Pod에만 A의 제외 목록 적용, 본 Pod는 기존처럼 인터넷 송신 없음 | 인터넷 송신이 Slack 자격만 가진 Pod로 한정, B6 선례 재사용, 새 구성요소 없음 | renderer 구조 변경(리소스 5→7개 수준), 노드 이동 시 relay 먼저 중지 절차 필요, 같은 노드 강제 스케줄링 | 없음 |
| C. 도메인 허용 egress proxy | 별도 proxy(예: CONNECT를 slack.com:443만 허용)로만 송신, Pod는 proxy로만 송신 | 도메인 단위 통제, TLS는 종단 간 유지(proxy가 토큰 못 봄) | `SlackWebTransport`에 터널 지원 코드 추가, proxy 이미지 출처/digest·배포·운영 입력 추가, 실물 검증 필요 | proxy Pod 운영 |
| D. 플랫폼 수준 | AWS Network Firewall 도메인 규칙, EKS Auto Mode 전환, Cilium 교체 | 가장 강한 중앙 통제 | 플랫폼 저장소 소유 변경, 클러스터 재구성 또는 시간당 고정 요금 | 큼 |
| E. 현행 `/32` 유지 | 배포 직전 DNS 결과를 `/32`로 고정 | 변경 없음 | 주소 변경 시 송신 실패, 사실상 불가 | 없음 |

## 결정: B′ — 무상태 Slack sender 분리 (2026-11-19)

처음 권장안은 B였다. 사용자가 "최종 목적은 실제 운영 기준"이라고 정했고, llm-council(자문 5·익명 상호검토 5·의장)을 거쳐 **B′**로 확정했다. 상호검토 5건 모두 B′를 가장 강한 안으로 골랐다.

- 보안 경계는 "어느 도메인으로 나가느냐"가 아니라 "인터넷에 나갈 수 있는 프로세스가 어떤 비밀을 갖느냐"다. 한 Pod의 컨테이너는 네트워크 네임스페이스를 공유하므로, 단일 Pod에 A나 C를 적용하면 탈취된 worker가 자기 K8s 토큰을 같은 송신 경로로 내보낼 수 있다(Secret 파일은 컨테이너별로 분리돼 있지만 네트워크는 공유).
- slack.com 허용만으로는 유출을 막지 못한다(공격자 워크스페이스 토큰으로 chat.postMessage). 그래서 C(proxy)는 경계가 아니라 감사 계층이며, 필요해지면 선택적으로 붙인다.
- B′는 sender가 PVC를 쓰지 않으므로 B의 같은 노드 강제·RWO 공유·SQLite WAL 결합 장애가 없다. sender가 탈취돼도 DB에 접근할 수 없다.

### 구조

| 구성 | 보유 자격 | 네트워크 |
| --- | --- | --- |
| 메인 Pod(worker·receiver·ingress·relay) | K8s 토큰(worker), collector, 사건 인증/TLS, signing, sender 클라이언트 비밀(relay). **bot 토큰 없음** | DNS, collector `/32`, sender Service 9446만. **인터넷 송신 없음** |
| sender Pod(`ai_ops.slack_sender`) | bot 토큰, sender 클라이언트 비밀. PVC·K8s 토큰·signing 없음, ConfigMap은 `slack-policy.json`만 | 수신: 메인 Pod 라벨→9446만. 송신: DNS + `0.0.0.0/0:443`(사설·CGNAT·loopback·link-local/IMDS 제외) |

- relay의 outbox·SQLite 처리는 메인 Pod에 그대로 둔다. `SlackSenderClient`는 `SlackWebTransport` 하위 클래스이므로 신원 확인·payload·응답 대조를 메인 Pod에서 계속 수행한다.
- sender는 클라이언트 비밀(Bearer)을 확인하고, 메서드 3종·허용 채널·고정 플래그·크기를 다시 검사한다. 분당 60건으로 제한한다. chat.postMessage는 payload에 묶인 멱등 키로 1시간 안의 같은 응답 재시도를 재전송하지 않는다(429만 재시도 허용, 200/503은 기억).
- 실패는 503으로 돌려주므로 기존 adapter 규칙에 따라 needs_review로 남고 재전송하지 않는다.

### 플랫폼·Slack 쪽 필요 작업(인프라 단계, 사용자 적용)

1. Slack 앱의 토큰 사용 IP 제한을 NAT 출구 EIP로 설정(EIP 보존 결정 필요). bot scope 최소화와 토큰 교체 절차.
2. VPC CNI network policy **strict** 모드(기본 standard 모드는 Pod 시작 직후 정책 적용 전 틈이 있음).
3. 노드 IMDSv2 hop limit 1.
4. VPC CIDR이 sender 송신 제외 대역 안에 있는지 확인.

### 잔여 위험(문서화)

- 탈취된 sender는 slack.com 경유로 유출할 수 있다(토큰 IP 제한은 탈취 토큰의 외부 사용만 막음).
- 클러스터 DNS 질의는 유출 채널로 남는다.
- 메인 Pod↔sender는 클러스터 내부 평문 HTTP + Bearer 비밀이다.
- 멱등 기록은 메모리에만 있어 sender 재시작 시 사라진다(adapter의 needs_review 규칙이 1차 방어).
- 메인 Pod 단일 replica·RWO 위 SQLite로 HA가 없다(별도 결정 과제).

### 실클러스터 완료 기준(인프라 단계)

worker 컨테이너에서 외부 443 연결 실패, sender의 비허용 메서드·채널·인증 실패 요청 거부, NAT 외부에서 토큰 사용 거부, sender 외 Pod의 9446 접근 거부.
