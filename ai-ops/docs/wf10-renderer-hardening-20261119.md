# WF-10 후보 renderer 보강 · 2026-11-19

[PR39 검토](wf10-active-config-review-20261119.md)에서 남긴 위험 중 인프라 없이 코드로 해결할 수 있는 두 건을 반영했다. 기준 main `4b54777a20880614403d19ee98b54bec9cc45263`. 실제 입력·배포·연결과는 무관한 offline 후보 생성기 변경이다.

## 변경

1. **livenessProbe 추가.** worker·event-receiver·slack-ingress·slack-relay 네 컨테이너에 readiness와 같은 probe(worker `--probe`, Slack `probe-ingress`/`probe-relay` exec, receiver 9443 tcpSocket)로 30초 주기·5초 제한·3회 실패를 넣었다. 기존 chart `statefulset.yaml`과 `ci_service`의 worker liveness 계약과 같다. heartbeat 한도는 worker 120초·Slack 30초이며, 약 90초 연속 실패 시에만 해당 컨테이너가 재시작된다. startupProbe(최대 120초)가 끝난 뒤에만 적용된다.
2. **init 컨테이너 resources 지정.** Secret 복사 init 5개에 requests 10m/32Mi, limits 100m/64Mi를 넣어 LimitRange/ResourceQuota가 강제되는 namespace에서도 Pod가 거절되지 않게 했다. 작업은 고정 key 몇 개를 메모리 emptyDir로 복사하는 것뿐이다.

회귀 시험 `test_every_process_has_liveness_and_init_has_bounded_resources`가 네 프로세스의 liveness 값·probe 동일성과 init resources를 확인한다. 합성 후보 `candidate.json`은 41,870바이트로 64KiB 한도 안이다.

## 범위 밖

- Slack egress 경로(고정 `/32` 대 proxy/NAT)는 설계 결정이 필요한 별도 작업(A4)이다.
- liveness가 실제 kubelet/CNI에서 기대대로 동작하는지, 장시간 조사 job 중 worker heartbeat가 120초 안에 유지되는지는 실물 없이 확인하지 않았다(기존 chart와 같은 가정).
- 실제 입력12개·연결 승인·실물 검증은 미확정이다. 인프라 설치·실제 배포·Slack 전송·장애 실습·Terraform·유료 모델 호출은 하지 않았다. 기존 시간·승인·원복 조건은 변경하지 않았다.

검증 수치는 [검증 기록](wf10-renderer-hardening-validation-20261119.json)에 있다.

## 최종 반영 결과

[PR #40](https://github.com/joungGo/url-shortener-ai-ops/pull/40) head `6920a0703736b896b74fe3b643bc7d8033d91b3c`를 head 고정 일반 squash 병합해 main `1f20442e2a70eb66a6228275add9f564cdb37c06`에 반영했다. [PR CI44474606794](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44474606794) 7종(1586개, skip5 기준선 동일)·[main CI44475106909](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44475106909) 성공(PR전용 gate skip은 설계대로). tree `ac77ac84b546f48904dea0a9f98f4c24ccc571b5` 일치. 보호 변경/우회 없음. 최종 메타데이터는 병합 후 로컬 갱신했다.
