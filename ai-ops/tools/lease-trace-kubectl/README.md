# Lease 요청 단계 계측용 로컬 kubectl

C-04의 고정 Lease 요청에서 **완료되지 않은 단계의 시작**을 남기는 선택형 진단 빌드다. 일반 `kubectl` 완료 로그를 추측해서 분류하지 않는다. Kubernetes `kubectl/client-go v0.35.8`의 기존 proxy·인증·TLS를 사용하고, exec 인증 RoundTripper에 관측 콜백만 추가한다. `main.go`는 진단 빌드 진입점이며 공식 배포 kubectl 바이너리가 아니다.

## 빌드와 로컬 검증

Python 3.11+와 Go 1.25+가 필요하다. Go 의존성은 `go.mod/go.sum`, client-go 모듈 checksum 및 수정할 원본 파일 SHA로 고정한다. `build.py`는 별도 새 디렉터리에만 소스를 복사하며 전역 Go 모듈 캐시를 수정하지 않는다. `--offline`은 기존 모듈 캐시만 사용하고 Go 자동 toolchain 다운로드도 금지한다. 옵션 없이 빌드할 때 모듈 다운로드는 가능하지만 실행기 시작 중에는 다운로드·빌드하지 않는다.

```sh
python ai-ops/tools/lease-trace-kubectl/build.py --offline --go /absolute/local/go --output /absolute/private/new-build
```

출력의 `binary_sha256`를 확인한 뒤, `TrialLeaseClient` 생성 시 **절대 경로와 그 해시를 함께** 명시한다.

```python
client = TrialLeaseClient(
    private_kubeconfig, expected_uid,
    trace_binary="/absolute/private/new-build/kubectl-c04-trace",
    trace_sha256=verified_manifest["binary_sha256"],
)
```

심볼릭 링크, 다른 소유자, hard link, 그룹/전체 쓰기 가능 파일·상위 디렉터리, 해시 불일치는 거절한다. root 소유의 sticky 시스템 임시 디렉터리만 상위 디렉터리 쓰기 제한의 예외다. 파일은 O_NOFOLLOW로 열어 chunk 단위 해시와 inode/시각을 검사하며 오류에는 고정 코드만 남긴다. 옵션을 생략하면 기존 kubectl 경로와 진단 계약을 유지한다. 환경 변수나 PATH만으로 새 계측을 자동 활성화하지 않는다. 빌드 파일·기존 자격 파일을 Git에 넣지 않는다.

검증 명령은 loopback/Unix socket과 가짜 exec 인증만 사용한다.

```sh
PYTHONPATH=ai-ops/src python ai-ops/scripts/check_c4_lease_stages_local.py \
  --binary /absolute/private/new-build/kubectl-c04-trace --sha256 VERIFIED_SHA256
PYTHONPATH=ai-ops/src python ai-ops/scripts/check_c4_lease_proxy_local.py \
  --trace-binary /absolute/private/new-build/kubectl-c04-trace --trace-sha256 VERIFIED_SHA256
```

두 번째 검사에는 비교용 기존 `kubectl`과 `openssl`도 필요하다. `build.py`는 원본 모듈 캐시를 교체 전에 검증하고 인증 패키지의 기존 Go 시험과 새 콜백 시험을 실행한다. `go mod tidy`로 잠금을 자동 갱신하지 않으며, `-mod=readonly` 및 전후 go.mod/go.sum 대조로 변경을 거절한다. 추가 race 검사는 빌드 디렉터리에서 `go test -race k8s.io/client-go/plugin/pkg/client/auth/exec -count=1`로 한다(C 컴파일러 필요).

## 기록 계약과 해석

- 인증: 기존 `getCreds()` 전/후. 캐시 사용과 실제 exec 실행을 모두 포함하므로 `authentication done`을 새 토큰 발급으로 해석하지 않는다. 401 후 기존 갱신에는 별도 `authentication_refresh`를 기록한다.
- DNS/TCP/TLS: `httptrace` 시작·완료/실패 콜백. 주소, 서버명, 인증서, 오류 원문은 받더라도 이벤트에 넣지 않는다.
- 연결: 요청 및 획득, 실제 `reused` 값. 연결 재사용 때 DNS/TCP/TLS 이벤트가 없는 것은 정상일 수 있다.
- 응답 대기: 요청 쓰기 완료 뒤 시작, 첫 응답 바이트 때 완료. 응답 헤더 반환 및 Python의 본문/검증 단계와 구분한다.
- 필드: 스키마 버전, 생성된 32자리 요청 ID, 순번, 단계, 상태, 단조 경과 마이크로초, 유실 수, 연결 재사용 여부만 허용한다. 경로·URL·IP·헤더·본문·토큰·자격 파일·예외 원문은 저장하지 않는다.

각 GET/PUT에 새 요청 ID를 넣고 같은 ID의 이벤트만 실패 진단에 결합한다. 원문 stderr는 파일/화면으로 전달하지 않고 길이 제한·정확한 스키마 검증 후 폐기한다. Go 큐는 256개, Python 요청 이력은 32개, 한 줄은 1KiB로 제한한다. 큐가 차면 기다리지 않고 버리며 마지막 시점의 유실 정보를 표시한다. 지연된 이전 요청 이벤트는 다음 요청에서 버린다.

**이력은 best effort다.** 진단 수집을 기다려 기존 시간 제한을 늘리지 않으므로 `delivery_complete=false`다. 마지막 이벤트가 오지 않았거나 reader가 끝났을 수도 있다. `loss_observed=false`도 완전 수집의 증명이 아니다. `last_observed`는 마지막으로 수집한 이벤트이지 확정 원인이 아니다. DNS/TCP 시도가 겹칠 수 있으며, 빈 이력을 0초/정상으로 해석하지 않는다. 현재 훅은 exec 인증 경로용이며 static bearer/mTLS 등 다른 인증 방식의 동일한 단계 보장을 주장하지 않는다.

Python socket 3초와 기존 CLI 플래그, 기존 인증/갱신/요청 처리, Lease UID/CAS, 신호 처리, 원복·승인 조건을 유지한다. 새 재시도·PUT 재전송·캐시·timeout 연장은 추가하지 않는다. 기존 client-go 자체 동작을 끄거나 바꾸지도 않는다.

## 반영 범위

2026-11-17에는 소스와 로컬 대역만 검증했다. 실제 감시/실행기 옵션 전달, 이미지·원격 workflow 연결은 아직 하지 않았다. 빌드·검토·CI 및 읽기 전용 연결 검증 후 별도 통합해야 한다. 과거 Lease 오류의 직접 원인은 미확정이며, 계측 구현만으로 원인 해결이나 새 실습 준비 완료를 판정하지 않는다.

근거: [client-go exec](https://github.com/kubernetes/client-go/blob/v0.35.8/plugin/pkg/client/auth/exec/exec.go), [kubectl proxy](https://github.com/kubernetes/kubectl/blob/v0.35.8/pkg/proxy/proxy_server.go), [Go httptrace](https://pkg.go.dev/net/http/httptrace).

## 2026-11-17 로컬 검토 후 정리

검증 결과의 `kubectl_version`은 실제 시험한 proxy 바이너리가 보고한 버전이고, `reference_kubectl_version`은 비교용 공식 kubectl 버전이다. 진단 빌드는 upstream 소스 v0.35.8이며 공식 릴리스의 버전 문자열을 주입하지 않는다. 배포용 v1.35.8과 대응하는 upstream 소스로 정렬했다. 관측 훅을 추가한 별도 빌드이므로 공식 배포 바이너리와 동일하다고 주장하지 않는다. 실행기/이미지/플랫폼 감시에 반영하기 전에는 사용할 버전의 정합성·고정 source/hash·빌드와 준비 단계 비용을 별도로 검토해야 한다. 이번 검토는 실제 연결 승인이 아니다.

## 준비 비용과 버전 검증

빌더는 핵심 Kubernetes 모듈 6개의 v0.35.8 일치·사전 replace 부재를 확인한다. source/checksum/의존성 잠금을 갱신했으며 자동 실행 시에는 readonly 검사를 유지한다. manifest의 `build_seconds`는 캐시 상태에 의존하는 이번 빌드 표본이다.

로컬 proxy 검사의 `--expected-reference-version v1.35.8`은 시험 전에 공식 비교 버전을 확인한다. `binary_pin_check_seconds`는 실제 소유권/해시 검사, `preparation_seconds`는 그 검사를 다시 포함한 proxy 기동·최초 가짜 인증·TLS·UID 조회 비용이다. `first_request_trace`는 best effort 단계 관측이며 누락을 0초로 해석하지 않는다. 시간값은 로컬 표본이고 AWS 인증·네트워크 비용이나 15초 시작 성공을 보장하지 않는다. 빌드/다운로드와 최초 인증은 준비 단계에서 완료해야 하며 승인 후 기존 현재 상태/ACK 재검증을 생략하지 않는다. 실제 실행기 연결은 별도 후속 작업이다.


## 2026-11-18 감시 CLI 연결 · 로컬 검증

`run_c4_ready_guard.py`는 이제 `--lease-trace-binary /absolute/private/kubectl-c04-trace --lease-trace-sha256 VERIFIED_SHA256` 쌍을 TrialLeaseClient에 전달한다. 기존 바이너리 고정 검증을 통과해야 하며 기본 동작은 바뀌지 않는다. 준비 전 빌드/검토가 필요하고 실제 감시·원격 workflow에 자동 적용하지 않는다.

Lease 오류로 종료하면 기존 marker 옆 `<marker>.failure.json`에 같은 intent/guard/role과 정제된 실패를 기록한다. 부모는 이 기록을 실패로만 처리하며 기존 성공 캐시·죽은 프로세스를 준비 성공으로 사용하지 않는다. 원복/승인/시간 제한과 재시도 금지는 그대로다. 하위 단계가 기록되지 않은 과거 사건의 원인은 추정하지 않는다. 상세는 [로컬 검증](../../docs/C4-lease-timeout-local-20261118.md)을 따른다.
