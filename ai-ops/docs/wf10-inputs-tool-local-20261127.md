# WF10 입력 작성 도구 (WF11 후속 4) · 2026-11-27

상태: 로컬 구현과 시험만 했다. 실제 사용은 다음 설치에서 한다.

## 목적
- WF11 설치 때는 재설치 키트의 개인 스크립트(`build-wf10-inputs.py`)로 WF10 입력을 만들었다.
- 그 과정에서 `collector_cidrs`에 Service IP 2개가 빠지는 실수가 한 번 있었다(2026-11-26). VPC CNI 네트워크 정책은 DNAT 전 Service IP로 판정하기 때문이다.
- 이 조립 규칙을 저장소 도구로 옮기고 시험으로 고정한다.

## 구조
| 단계 | 하는 일 | 성격 |
|---|---|---|
| `collect` | 클러스터에서 고정 `kubectl get` 5개만 실행해 사실 파일을 만든다 | 읽기 전용, Secret을 읽지 않음 |
| `build` | 사실 파일과 운영자 입력으로 WF10 입력을 만든다 | 순수 함수, renderer 검증 통과 후 새 파일로만 저장 |

## 사용
```
python -m ai_ops.wf10_inputs collect --output facts.json [--kubeconfig …]
python -m ai_ops.wf10_inputs build --facts facts.json \
  --normal-binding ci-runtime/normal-preparation.json \
  --image-proof ai-image-import-proof.json \
  --bot-user-id U… --alert-channel-id C… --output wf10/inputs.json \
  [--no-ci-relay] [--no-publish] [--team-id …] [--app-id …] [--user-id …] [--investigation-channel-id …]
```

## 수집하는 값 (`collect`)
- `ai-ops-lab/lab-collector` ConfigMap의 `bindings.json`, `runtime.json`, `metric-queries.json`
- `default` 네임스페이스 `kubernetes` EndpointSlice의 API 서버 주소
- `kubernetes` Service IP, `kube-prometheus-stack-prometheus` Service IP
- Prometheus Pod(`prometheus-kube-prometheus-stack-prometheus-0`) IP
- 주소는 IPv4만 받는다. loopback·link-local·0.0.0.0은 거부한다.
- 출력 파일은 새로 만들 때만 쓴다(0600, 덮어쓰기 금지).

## 조립 규칙 (`build`)
- `collector_cidrs`는 API 서버 주소, **Service IP 2개**, Prometheus Pod IP다. 모두 `/32`이고 중복은 하나로 합친다.
- Prometheus가 평문 HTTP이면 `collector` Secret을 두지 않는다(자격 증명 없이만 허용).
- 이미지와 소스 SHA는 **이미지 반입 증명 파일**에서 읽는다.
  - `scan_findings`가 0이어야 한다.
  - 이미지는 practice ECR `urlshortener-practice-ai-ops`의 digest여야 한다.
- 정상 CI binding은 `prepare_b6_normal_ci` 결과의 `binding`이다. 결과 파일 형태가 아니면 거부한다.
- 기본값은 2026-11-26 설치에서 확인한 값이다.

  | 항목 | 값 | 바꾸기 |
  |---|---|---|
  | 워크스페이스·앱 | `T0AAZ9N5GT1`, `A0CJHH8ULUC` | CLI 옵션 |
  | 조사 채널·승인자 | `C0CK82SD8NL`, `U0ABBAD4FT7` | CLI 옵션 |
  | GitHub ID | 승인자 149856960, PR 작성자(writer 봇) 354870297 | 고정, 같으면 거부 |
  | Secret 이름 | `ai-ops-slack-*` 5개, `ai-ops-recovery-reader`·`-github`, `ai-ops-ci-github`, `ai-ops-pr-writer`(+TLS Prometheus이면 `ai-ops-collector`) | 고정, 모두 서로 다름 |
  | CI relay, PR 발행 | 켬 | `--no-ci-relay`, `--no-publish` |
  | 승인 시간 | 대기 3600, 승인 600, 관찰 30, 재검증 10초 | 고정 |

- 봇 사용자 ID와 알림 채널 ID는 설치 때 확인해 **반드시 입력**한다.
  - 알림 채널은 조사 채널과 달라야 한다.
  - 봇은 승인자와 달라야 한다.
- 결과는 renderer의 `inputs`와 `candidate`를 모두 통과해야 저장한다. 출력은 요약(상태, manifest hash, 자원 목록)만 낸다.

## 검증
- 새 시험 8개(`test_wf10_inputs.py`)
  - 가짜 kubectl로 정확히 5개 `get` 명령만 실행하는지 확인했다. Secret 조회와 쓰기 동사는 없다.
  - 주소 거부, `collector_cidrs` 구성, 기본값과 켜고 끄기, Secret 분리를 시험했다.
  - 도구가 자체 오류 코드로 먼저 거부하는지 확인했다(Slack ID, 경계, 작성자·승인자, 덮어쓰기 키, 이미지).
  - 반입 증명, CLI 새 파일·권한·요약 출력도 시험했다.
- **동등성 확인:** 지난 설치 입력(`install-20261126/wf10/inputs.json`, sha256 `47bcb8a5…`)에서 사실을 되살려 CI relay와 PR 발행을 끄고 조립했다. 결과가 원본과 **완전히 같았다**. 켜면 renderer가 자원 12개로 통과한다.
- 결함 변형 35/35 검출.
  - 처음에는 6개가 살아남았다(작성자·채널 검사 제거, 정규식 `fullmatch`→`match` 2개, SHA 검사 제거, renderer 호출 제거).
  - renderer도 같은 입력을 거부해서 오류 종류만 보고 통과한 경우였다. 도구 고유 오류 코드와 실제 renderer hash를 비교하도록 시험을 강화했다.
- 전체 시험 1957개 통과(skip 1). 저장소 경계 검사 통과.

## 남은 일
- 재설치 키트의 개인 스크립트를 이 도구 호출로 바꾼다(키트는 저장소 밖 개인 자료).
- 실제 클러스터 수집은 다음 설치에서 확인한다.
