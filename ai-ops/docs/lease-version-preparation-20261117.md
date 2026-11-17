# Lease 버전 정합성과 준비 비용의 로컬 검증

## 변경과 범위

AI main `9482120`의 진단 도구를 실습의 공식 kubectl `v1.35.8`과 대응하는 Kubernetes `v0.35.8` 소스로 정렬했다. client-go/kubectl/component-base/api/apimachinery/cli-runtime 버전 혼합이나 사전 replace를 빌더가 거부한다. source SHA·module checksum·go.mod/go.sum을 검토해 갱신했고, 빌드 실행은 readonly/offline 잠금 확인을 유지한다. 업데이트 시의 명시적 go get/tidy와 실행 시의 자동 의존성 변경을 구분한다.

공식 출처: [client-go exec v0.35.8](https://github.com/kubernetes/client-go/blob/v0.35.8/plugin/pkg/client/auth/exec/exec.go), [kubectl v0.35.8](https://github.com/kubernetes/kubectl/tree/v0.35.8). 진단 바이너리는 관측 훅을 추가한 별도 빌드다. 버전 소스 정렬은 공식 바이너리와의 byte 동일성이나 실환경 성공을 뜻하지 않는다. Go 최소 요구는 1.25, 검증 도구는 고정 1.27.4다.

## 이번 Mac 로컬 표본

| 측정 | 결과 |
|---|---:|
| 의존성 다운로드 후 오프라인 빌드·인증 시험 | 46.60431초 |
| 진단 바이너리 소유권·해시 검사 | 0.062698초 |
| 진단 proxy 기동·해시 재검사·최초 가짜 인증/TLS/UID 조회 | 0.164016초 |
| 공식 kubectl 준비 | 0.084382초 |
| 진단 proxy 준비 후 GET 20회 합계 | 0.012425초 |

빌드 표본은 기존 Go cache를 사용할 수 있으며 cold build 기준이 아니다. 최초 인증은 가짜 exec plugin이며 실제 AWS 로그인/토큰 갱신·원격 DNS/TCP/TLS 비용은 측정하지 않았다. `first_request_trace`는 기존 best effort 규칙을 따르며 누락을 0초나 정상으로 바꾸지 않는다.

가짜 인증/TLS/응답 대기를 각각 지연시킨 시험은 약 3.002초에 중지됐다. 비밀 canary 비출력·요청 추가 재전송 없음·TLS/잘못된 CA 거절·고정 경로/메서드/Host·UID/CAS/양쪽 ACK·인증 갱신·SIGTERM/소켓 정리를 확인했다. Go exec 원본/추가 콜백 시험과 race 검사를 통과했다. 핵심 모듈 혼합·replace 거절 회귀 시험을 추가했다. Python 전체 1,230개가 55.808초에 통과했다(skip 0). 최초 샌드박스 실행은 로컬 소켓 생성 권한 때문에 실패했고, 로컬 소켓 허용 환경에서 재검증했다. 두 로그를 보존한다. 원격 CI 결과는 해당 PR 검사와 보존 기록을 따른다.

## 준비와 실행 경계

다운로드·빌드·바이너리 검증·최초 인증/TLS/UID 조회는 실행 승인을 받기 전 준비 단계에서 완료해야 한다. 실행기나 감시에 선택형 계측 옵션을 연결하지 않았으며 이번에는 그 경로도 변경하지 않았다. 승인 후 현재 상태·양쪽 ACK 검증을 생략하거나 15초 시작 예산 밖으로 옮기지 않는다. 필요하면 재설치 후 새 대상의 읽기 전용 준비 비용을 확인한다.

요청 3초, 시작 15초, 안내 후 최소 120초/시작 후 210초 리뷰 마감, 소유 원복 240초/독립 원복 시작 300초, 회복 관찰 120초와 실행별 승인·정확 head 사람 리뷰·UID/CAS/ACK·회수 조건은 그대로다. 과거 Lease 실패 원인은 미확정이다. 실제 연결·AWS·Slack·감시 기동·Terraform·장애 실습·유료 모델 호출은 수행하지 않았다.

기존 PR21 검토/CI 기록은 당시 v0.34.1 검증의 이력으로 보존한다. 이번 버전 정렬 근거는 같은 이름의 JSON과 사용자 전용 `~/.local/share/url-shortener-ai-ops/offline-resume-20261117/`에서 확인한다. 다음 추천: **GPT-6 SOL · High**.
