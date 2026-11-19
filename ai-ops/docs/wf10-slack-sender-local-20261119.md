# WF-10 Slack sender 분리(B′) 로컬 구현 · 2026-11-19

상태: **로컬 구현·검증 완료 / 미커밋 / 실클러스터 미검증.** 설계 근거는 [Slack 송신 경로 설계](wf10-slack-egress-design-20261119.md). 기준 main `1f20442e2a70eb66a6228275add9f564cdb37c06`.

## 변경

| 파일 | 내용 |
| --- | --- |
| `ai_ops/slack_sender.py`(신규) | `SenderApp`(인증·라우트·payload 재검증·멱등·분당 60건), `SenderServer`(단일 스레드 HTTP 9446, 요청 로그 없음), `SlackSenderClient`(메인 Pod transport, bot 토큰 없음, 실패는 503), `main --enable-web` 명시 게이트 |
| `ai_ops/slack_web.py` | 응답 payload 계약을 `post_payload_valid`로 분리해 relay와 sender가 같은 검사를 쓰게 함. 허용 메서드 집합 `METHODS` 공유. 동작 변경 없음 |
| `ai_ops/slack_runtime.py` | `SlackWebConfig`에 `sender`(url·client_secret_file) 추가. `bot_token_file`과 `sender` 중 정확히 하나만 허용. sender 모드에서는 relay가 bot 토큰을 읽지 않음. 기존 설정 파일은 그대로 유효 |
| `ai_ops/wf10_deployment.py` | 메인 Pod에서 bot 토큰·인터넷 송신 제거, relay에 sender 클라이언트 비밀. sender Deployment(1 replica/Recreate)·ClusterIP Service·NetworkPolicy 추가(리소스 5→8개). 입력 `network.slack_cidrs` 삭제, `secrets.sender_client` 추가(6개 모두 서로 달라야 함). 실행 조건에 `egress_enforcement_negative_tests`·`slack_token_ip_restriction`, 플랫폼 선행 조건과 잔여 위험 목록 추가 |
| 시험 | 신규 `test_slack_sender.py` 10개, `test_wf10_deployment.py` 2개 추가·기존 단언 갱신 |

`wf10-deployment.unresolved.json`은 network/secrets가 null이므로 변경 없음. 실제 값은 계속 미확정이다.

## 검증한 동작

- relay가 bot 토큰 파일 없이 sender를 거쳐 실제 adapter 흐름으로 1회 전달(delivered). 두 Pod 사이 요청에 bot 토큰이 없음.
- 같은 응답 재시도는 재전송 없이 첫 결과를 반환. 429는 다시 전달하고, 모호한 503은 기억해 재전송하지 않음.
- 인증 실패·미허용 경로·비허용 채널·플래그 변경·추가 필드·멱등 키 누락/불일치·중복 JSON 키·깨진 JSON은 Slack에 닿기 전에 거부되고 요청 내용을 응답에 담지 않음.
- 클라이언트는 비정상 응답(500·과대·형식 위반·연결 예외)을 모두 503으로 돌리고 예외 문자열을 남기지 않음.
- loopback 실제 HTTP: 405·Content-Length 누락·크기 초과 처리.
- renderer: 메인 Pod에 bot 토큰·`0.0.0.0/0` 없음, sender만 인터넷 443(사설·IMDS 제외)·메인 Pod 수신만 허용, sender Pod에 PVC·K8s 토큰·signing 없음.

합성 후보 `candidate.json`은 54,482바이트로 64KiB 한도의 약 83%다. 실제 설정이 더 크면 `wf10_package_too_large`로 실패하며 잘라내지 않는다.

## 범위 밖

플랫폼(strict 모드·IMDS hop limit·EIP 보존)과 Slack 앱 IP 제한은 인프라 단계 작업이다. 실클러스터 음성 시험, proxy 감사 계층, 클러스터 내부 TLS, HA는 후속이다. 인프라 설치·실제 배포·Slack 전송·장애 실습·Terraform·유료 모델 호출은 하지 않았다. 기존 시간·승인·원복 조건은 변경하지 않았다.

검증 수치는 [검증 기록](wf10-slack-sender-local-validation-20261119.json)에 있다.
