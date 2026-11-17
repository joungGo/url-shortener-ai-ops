# Slack 수신·회신 실행기 검토와 적용 범위

## 변경 동작

기존 Slack 어댑터는 라이브러리만 있어 배포 프로세스가 없었다. `python -m ai_ops.slack_server`의 TLS ingress와 relay가 기존 영속 inbox·TaskService·큐·별도 worker를 연결한다. 수신 프로세스는 Signing Secret만, 회신 프로세스는 Signing Secret과 Bot Token을 읽는다. 기본 비활성과 명시적인 `--enable-web` 조건을 모두 통과해야 실행한다.

`check-config`는 설정과 해시만 검증하고 자격/네트워크/큐를 열지 않는다. `probe-ingress`와 `probe-relay`는 역할별 잠금·PID·최근 진행 상태를 확인하며 큐나 자격을 초기화하지 않는다. 이 probe는 프로세스 상태 검사이며 실제 Slack/worker/실습 준비 완료 증명이 아니다.

공유 `runbook-policy.json`이 있으면 worker·CI relay·배포 로컬 검사가 같은 정책을 읽는다. 기존 배포에서 파일이 없을 때 동작은 유지하며, Slack 실행에는 정책이 필수다. 정상 CI inbox·보존·metrics·기존 worker의 단일 실행 잠금은 유지한다.

## 검토 중 재현·수정

상태 파일의 동기 fsync를 ASGI 이벤트 루프에서 실행하면 PVC 지연이 요청 ACK를 늦출 수 있었다. 합성 200ms 저장 지연이 event loop를 약205ms 막는 실패를 재현했다. 상태 저장은 별도 thread에서 실행하고, 취소 시 실제 저장을 기다린 뒤 health 파일을 제거하도록 수정했다. 종료 뒤 늦은 thread가 health를 다시 쓰는 경로도 회귀 시험으로 확인한다.

검증은 합성 토큰/서명·임시 저장소·loopback TLS와 대역 Slack API를 사용한다. 실제 TLS 수신→기존 큐/worker→문서 ID·버전·해시·절 위치가 있는 결과, 원래 스레드 회신, 중복/재시작, 응답 유실 뒤 needs_review/자동재전송0, SIGTERM·잠금·설정 거절을 포함한다. 집중54개 통과. 전체 결과는 아래 검증 결과에 기록한다.

CI의 unittest discovery에서는 시험 경로가 부모 sys.path에만 추가되어 회신 자식 프로세스의 대역 모듈 import가 실패했다. 시험 자식 프로세스에 fixture 경로를 명시하고 tests가 없는 PYTHONPATH로 재검증했다. 제품 코드나 시간 제한은 이 보완에서 바뀌지 않는다.

## 운영 계약과 한계

- Uvicorn0.54.0/h110.16.0/click8.5.1을 hash lock으로 고정한다. Python3.11 이상을 사용하며 기존 이미지 빌드가 requirements.lock을 설치한다.
- ingress는 TLS9444/최소TLS1.2, h11, worker1, WebSocket·전달헤더 신뢰·access 로그 비활성이다. TLS 키와 Slack 자격은 현재 UID의 전용 일반 파일로 제공한다. projected Secret/config symlink는 init에서 일반 파일로 materialize해야 한다.
- ACK2.5초/64KiB/접수4, 기존 socket IO3초/시작15초/안내 후 리뷰최소120초·시작 후210초/소유원복240초·독립원복300초/관찰120초와 승인·정확 head 사람 리뷰·CI·UID/Lease/CAS/ACK 조건을 유지한다.
- relay는1초 간격으로 최대100 조회/회신1건을 처리하고, 기존429 최대3회/기한을 유지한다. SIGTERM 후 새 작업을 시작하지 않고 현재 journal 처리를 마친다. 원문 예외·요청·자격을 로그에 남기지 않는다.
- DNS를 포함한 전체 전송 wall-clock 상한, 자동 토큰 회전, 불명확 전송의 원격 자동 재조회는 제공하지 않는다. 후자는 needs_review로 보존한다.
- Uvicorn은 graceful shutdown 후 원래 SIGTERM을 재발생시킨다. 로컬 시험은 -SIGTERM 반환과 lifespan 정리·health 제거를 함께 검증한다.
- 실제 적용에는 동일 공유 설정/hash와 새 정상 CI binding, 검토 이미지 digest, 현재 PVC/Pod/Traefik selector, backend TLS 인증서·DNS·제한 네트워크 규칙이 필요하다. TCP passthrough에서는 backend 인증서를 따로 공급해야 한다.

이번 변경은 코드·시험 반영이다. 실제 Slack 호출/이벤트 등록/메시지 전송, Secret/DNS/배포/권한 변경, 원인 조사, 장애 실습, Terraform, 유료 모델은 실행하지 않는다. 배포는 정확한 변경과 원복안을 확정한 뒤 별도 승인한다. C04/C05 실환경·A5/A7·실행 저장소 전환·AI 포트폴리오 완료를 뜻하지 않는다.

## 검증 결과

검토 최종 코드의 전체 로컬 시험 **1,175개 / 55.543초 / 실패0 / skip0**, 집중54개 / 5.064초를 통과했다. 기존125개 소스(ACK 및 복구 제어 포함)는 기준 main과 동일하다. CI는 같은 head의 계약·이미지·Terraform 정적 검사와 PR gate를 확인하고 일반 squash 병합한다. 개인 배포 초안·실제 Slack ID·자격 파일·과거 사건 기록은 이 PR에 포함하지 않는다.
