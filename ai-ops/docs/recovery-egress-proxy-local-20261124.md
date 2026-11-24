# relay 외부 송신: 고정 목적지 송신 proxy 조각 1 · 2026-11-24

상태: **로컬 구현·검증 완료, PR CI/main 대기.** 기준 main `4a623096fdf17dea464ae100fd45ec388395d8e1`(PR60).

## 배경과 결정

WF12 초안에서 미결정 사항이 하나 나왔다. WF10 B′ 설계에서 relay는 메인 Pod 안에 있고, 메인 Pod는 인터넷 송신이 막혀 있다. 그런데 WF07 PR 준비는 relay가 GitHub·AWS로 나가야 한다.

사용자 결정(2026-11-24, 추천안): **목적지를 고정한 송신 proxy**를 둔다.

- TLS는 relay와 목적지 사이에서 끝까지 이어진다. 그래서 proxy는 내용도 토큰도 보지 못한다.
- 비밀(GitHub 토큰·IRSA)은 relay에만 둔다.
- 메인 Pod의 송신은 proxy로만 나간다. renderer 반영은 조각 2에서 한다.

## 구현

### 1. `ai_ops.recovery_egress_proxy`

기존 `model_egress_proxy`(OpenAI 실습용)는 바꾸지 않고, 별도 모듈로 만들었다.

**허용 목적지는 다음 3개뿐이다.**

- `api.github.com:443`
- `sts.ap-northeast-2.amazonaws.com:443`
- `api.ecr.ap-northeast-2.amazonaws.com:443`

**요청 검사**

- `CONNECT host:443 HTTP/1.0|1.1` 한 줄과, 그 host와 같은 `Host` 헤더 하나만 받는다.
- 다른 헤더, 다른 포트, 절대 URL, 대소문자가 다른 메서드는 받지 않는다. 헤더 크기는 4096바이트까지다.

**DNS 검사**

- 응답이 모두 공개 IPv4 주소여야 한다. 하나라도 아니면 전체를 거절한다.
- 사설, CGNAT, loopback, link-local(메타데이터 `169.254.169.254` 포함), IPv6 응답은 거절한다.

**연결 제한**

- 동시 tunnel은 8개까지다. 넘으면 503으로 응답한다.
- 35초 동안 오가는 데이터가 없으면 닫는다.
- tunnel 수명은 120초다. 그 사이 데이터가 계속 오가도 120초가 지나면 닫는다.

**응답과 기록**

- 거절은 항상 고정 `403`으로 응답한다.
- 요청, 목적지, TLS 바이트, 오류 내용은 어디에도 기록하지 않는다.

### 2. relay 쪽: 설정된 proxy만 사용

- **설정:** `PrepareConfig.egress_proxy`에 `http://<서비스>:<포트>`를 쓴다(클러스터 내부 주소). 비어 있으면 직접 연결이며, 개발용이다.
- **GitHub reader:** 설정된 proxy로만 `CONNECT`한다. proxy가 없을 때도 환경 변수(`https_proxy` 등)의 proxy는 쓰지 않는다. redirect는 계속 따르지 않는다.
- **IRSA client:** 다음 설정을 모두 **botocore 세션 단계**에 둔다. 그래야 boto3 내부에서 **자격 교환(AssumeRoleWithWebIdentity)을 하는 STS client**까지 같은 설정을 쓴다.
  - 리전: `ap-northeast-2`
  - STS 지역 endpoint
  - proxy
  - dualstack·FIPS 끄기
  - 설정된 endpoint URL 무시

  STS·ECR client의 endpoint는 지역 주소로 고정했다.
- **검증 방법:** 로컬 기록용 proxy로 실제 요청을 받아 확인했다. 자격 교환은 `CONNECT sts.ap-northeast-2.amazonaws.com:443`, ECR은 `CONNECT api.ecr.ap-northeast-2.amazonaws.com:443`, GitHub는 `CONNECT api.github.com:443`으로 proxy를 거친다.

## 시험에서 발견해 고친 결함

AWS 관련 환경 변수를 일부러 넣은 시험에서 내부 STS client의 결함 2건이 드러났다. 둘 다 relay에 우리가 만든 client의 endpoint 고정이 내부 client에는 적용되지 않아서 생긴다.

| 환경 변수 | 고치기 전 | 영향 |
|---|---|---|
| `AWS_USE_DUALSTACK_ENDPOINT=true` | 자격 교환이 `sts.ap-northeast-2.api.aws`로 감 | proxy가 허용하지 않는 주소라 자격 교환 실패 |
| `AWS_ENDPOINT_URL[_STS]=<임의 주소>` | 자격 교환이 그 주소로 감 | **Pod의 projected 토큰이 임의 주소로 전송될 수 있음.** proxy를 쓰면 막히지만, 직접 연결 구성에서는 유출 위험이 있다. |

두 결함 모두 세션 단계에서 dualstack·FIPS를 끄고(`use_dualstack_endpoint`/`use_fips_endpoint`=False), 설정된 endpoint URL을 무시하도록(`ignore_configured_endpoint_urls`=True) 해서 고쳤다.

## 연결 단계(조각 2와 실제 연결)

- **renderer:** proxy Deployment와 Service를 추가한다(인터넷 송신 443, 사설 주소 제외).
- **NetworkPolicy:** 메인 Pod는 proxy로만 송신하고, proxy는 메인 Pod에서 오는 수신만 받는다.
- **설정:** `recovery-approval.json`의 `prepare.egress_proxy`에 proxy Service 주소를 넣는다.

## 검증

신규 시험 `test_recovery_egress_proxy.py` 12개. loopback만 썼다.

- **요청 검사:** 목적지 3개를 두 HTTP 버전으로 모두 허용한다. 그 밖의 13가지 요청 형태는 거절한다.
- **DNS:** 사설, 메타데이터, loopback, CGNAT 주소가 응답에 하나라도 섞이면 거절한다. IPv6와 빈 응답도 거절한다.
- **실제 서버:** 허용된 목적지는 바이트 그대로 중계한다. 다른 목적지나 내부 주소로 해석되는 경우 403을 돌려주고, 연결을 시도하지 않는다.
- **연결 제한:** 동시 한도를 넘으면 503이다. 수명이 지나면 데이터가 계속 오가도 닫힌다. 포트가 443이 아닌 목적지 설정은 거절한다.
- **relay 쪽 proxy 사용:**
  - GitHub reader는 설정된 proxy로만 나간다. 설정이 없을 때 환경 변수 proxy를 무시한다. 잘못된 proxy 주소는 거절한다.
  - STS 자격 교환과 ECR이 고정된 지역 주소로 proxy를 거친다. 여기에 영향을 줄 수 있는 환경 변수(legacy STS, dualstack, FIPS, endpoint URL 재지정, 다른 리전, 환경 proxy)를 모두 넣은 상태에서 확인했다.
  - ECR checker는 처음 쓸 때 만들어지는 client도 proxy를 거친다.
  - 설정값이 두 reader에 모두 전달된다.

결함 변형 검사는 21종 중 19종을 검출했다. 전체 환경을 상속하고 기준선을 먼저 확인하는 실행기를 썼다.

- 살아남은 2종(`sts_global_endpoint`, `endpoints_unpinned`)은 **동등 변형**이다. 현재 botocore 1.44에서는 다른 설정(세션 리전, dualstack·FIPS 끄기, endpoint URL 무시)이 같은 결과를 보장하므로, 하나를 지워도 동작이 바뀌지 않는다. 이중 방어로 남겨 둔다.
- 처음 실행에서 살아남은 변형은 시험을 보강해 처리했다. 대상은 수명 검사, 환경 변수 proxy, dualstack·FIPS, ECR proxy 전달이다. 이 보강 과정에서 위 결함 2건이 드러났다.

전체 결과는 [검증 기록](recovery-egress-proxy-local-validation-20261124.json)에 있다.

실제 클러스터·AWS·GitHub 쓰기·배포·Slack 없음.
