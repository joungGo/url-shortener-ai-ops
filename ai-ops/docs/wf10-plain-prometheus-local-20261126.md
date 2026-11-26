# WF10 Prometheus 연결: 실제 구성(평문 HTTP, 인증 없음) 수용 · 2026-11-26

상태: **로컬 구현과 시험만 했다.** 클러스터·AWS·Slack 호출은 없다. WF11 설치 준비 3c 항목이다([설치 준비](wf11-install-prep-20261125.md)).

## 문제

WF10 renderer는 Prometheus 연결에 아래 세 가지를 요구했다.

- `https://`
- `/run/collector/token`·`ca.crt`
- `collector` Secret

하지만 실제 클러스터의 Prometheus는 `http://kube-prometheus-stack-prometheus.monitoring.svc:9090`이고 인증이 없다. 2026-11-18 설치와 B6 실습도 이 주소를 그대로 썼다. 연결 계약(`Connection`)은 주소에 경로를 붙이는 것을 금지하므로, Kubernetes API 서버의 service proxy로 돌아가는 방법도 쓸 수 없다. 그래서 실제 입력을 만들 수 없었다.

## 결정 (사용자 2026-11-26, (a)안)

Prometheus 연결에 아래 두 형태만 허용한다.

| 형태 | 조건 | collector Secret |
|---|---|---|
| HTTPS(기존) | port 443/6443/8443/9090, `/run/collector/token`·`ca.crt` | 필수, worker에 마운트 |
| **평문 HTTP(신규)** | port **9090만**, `token_file`·`ca_file` **없음** | **없어야 함**. 있으면 입력 거절, 마운트 없음 |

- 평문 HTTP로는 어떤 자격도 보내지 않는다. 토큰이나 CA가 있으면 거절한다.
- 이 경우 `secrets`에서 `collector` 키를 뺀다. 자격 분리 검사도 실제로 있는 Secret 수로 센다.
- 재시작 경로(`recovery.restart`)는 Prometheus에 읽기 토큰을 보내므로 HTTPS만 허용한다. 평문 HTTP와 함께 쓰면 `wf10_restart_prometheus_tls_required`로 거절한다. WF11은 이 경로를 쓰지 않는다.
- 송신 허용은 기존 `collector_cidrs`의 `/32` 규칙을 그대로 쓴다. 포트 9090은 연결 주소에서 자동으로 들어간다.
  - Prometheus는 Pod IP로 넣는다. NetworkPolicy는 Service IP가 아니라 Pod IP로 판정한다.
  - Pod가 다른 곳에 다시 뜨면 IP가 바뀌므로 다시 렌더링해야 한다. 이 점을 `external_prerequisites`에 적었다.
- `residual_risks`에 두 가지를 반영했다.
  - 평문 HTTP 무인증 읽기
  - IRSA 주입 범위 정정: 메인 Pod만이 아니라, 같은 ServiceAccount를 쓰는 메인·Slack 송신·egress Pod 전부

## 검증

- 시험 5개를 추가했다(`test_wf10_plain_prometheus.py`).
  - 평문 HTTP일 때 collector volume과 마운트가 없다.
  - runtime 설정에 자격 없는 연결이 들어간다.
  - NetworkPolicy가 9090을 허용한다.
  - 전제 조건과 위험 문구가 들어간다.
  - 다음 입력은 거절한다: 토큰, CA, 포트 80, 포트 8080, collector Secret 잔존, kubernetes 평문 HTTP.
  - HTTPS일 때는 collector가 여전히 필수다.
  - recovery는 렌더링되지만 restart는 거절된다.
  - 자격 분리 검사가 동작한다.
- 기존 WF10 시험(deployment, recovery, restart, lab 경보)은 그대로 통과한다.
- 결함 변형은 14개 중 12개를 검출했다. 살아남은 2개는 결과가 같은 변형이다.
  - 설정 모양이 잘못됐을 때 판정을 바꾸는 변형: 뒤의 BRuntime 검사가 어차피 거절한다.
  - kind 조건을 지우는 변형: `Connection` 모델이 kubernetes 연결을 HTTPS로만 받는다.
