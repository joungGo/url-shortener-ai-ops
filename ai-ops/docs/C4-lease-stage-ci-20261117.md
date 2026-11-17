# 선택형 Lease 계측의 CI 검증

2026-11-17. main 788fb1e에서 검토한 선택형 Lease 단계 계측을 반영한다. 실제 실행기·감시 옵션에는 연결하지 않는다.

Python은 명시한 절대 경로·SHA·소유권·파일/상위 디렉터리 권한을 검증한 경우만 진단 바이너리를 사용한다. 기본값은 기존 kubectl이다. 요청별 인증·DNS·TCP·TLS·응답 대기 이벤트는 제한된 필드/크기만 받아 보관하며 원문 stderr·자격·주소·헤더·본문·예외 문자열을 출력하지 않는다. 이벤트 수집은 best effort이며 마지막 이벤트를 확정 원인으로 해석하지 않는다.

Go 진단 빌드는 client-go/kubectl v0.34.1, 원본 source SHA·모듈 checksum·go.mod/go.sum을 고정한다. 원본 캐시 검증 후 별도 복사본에 관측 콜백만 추가한다. `go mod tidy` 자동 변경을 제거했고 readonly build/test와 lock 전후 대조를 사용한다. 공식 비교용 kubectl v1.35.8과 실제 진단 빌드의 버전 표시를 분리했다.

## 검증 범위

검토 시 Python 전체1,229개·집중36개, 원본 인증 패키지와 계측 콜백 일반/race 시험, 오프라인 빌드, 계측/기존 kubectl 로컬 TLS 계약을 통과했다. loopback 인증·TLS·응답 지연은 각각 약3초에 종료했고, UID/CAS/ACK·잘못된 CA·경로/메서드/Host 제한·SIGTERM·socket/reader 정리와 가짜 secret 비출력을 확인했다.

기존 AI CI에 `Lease trace local contracts` 작업을 추가한다. 고정 action SHA·Go1.27.4와 checksum 검증 도구/모듈을 준비한 뒤, 오프라인 진단 빌드·Go race·합성 자격/loopback 인증·TLS·응답 지연·Lease 계약 검사를 실행한다. `C03 Main PR Gate`도 이 작업의 성공을 요구한다. contents:read만 사용하며 환경 승인·AWS OIDC·실제 자격·클러스터·모델·Slack은 사용하지 않는다. 바이너리는 CI 임시 경로에만 만들고 이미지/레지스트리/실제 실행기에 배포하지 않는다.

기존 socket3초, 시작15초, 안내 뒤 최소120초/시작 뒤210초 리뷰 마감, 소유 원복240초/독립 원복300초, 회복 관찰120초와 실행별 승인·새 정확 head 사람 리뷰·UID/Lease/CAS/ACK·자격 회수는 유지한다. 새로운 요청 재시도나 PUT 재전송, 계측 완료 대기는 추가하지 않는다.

실제 연결 전에는 진단 v0.34.1과 실습용 v1.35.8의 버전 정합성 및 준비 단계 비용을 별도로 검토해야 한다. CI 통과는 과거 연결 오류 원인 규명이나 C04/C05 성공을 뜻하지 않는다. 실제 연결·감시·장애 실습은 자동 재개하지 않는다.
