# WF11 비밀 자료 준비 절차 · 2026-11-26

상태: **절차 문서이며 실행 승인이 아니다.** WF11 설치 준비 3b 항목이다([설치 준비](wf11-install-prep-20261125.md)). 실제 생성과 적용은 재설치 중에, Terraform·설치 위임 범위 안에서 한다.

공통 규칙:

- 값은 출력하지 않는다. 터미널·로그·Git·PR·artifact에 남기지 않는다.
- 셸 추적(`set -x`)은 끈다.
- 확인은 형식 검사, 그리고 hash가 같은지 비교하는 것으로만 한다.

[B5 내부 TLS·인증 절차](B5-webhook-identity.md)를 WF10 이름과 주소에 맞게 바꾼 것이다. 기존 원칙은 그대로 따른다.

- RSA를 쓴다.
- 서버 인증서는 90일, CA는 365일로 만든다.
- CA 개인키는 클러스터에 올리지 않는다.
- 검증 우회 옵션은 쓰지 않는다.

## 1. 목록

Secret 이름은 WF10 입력 `secrets`·`recovery.secrets`에 그대로 넣는 **제안값**이다. 설치 때 WF10 입력과 함께 확정한다. renderer는 이름 형식과 서로 다른지만 검사한다.

| WF10 키 | 제안 이름 (ns `ai-ops`) | 키 | 출처 | 형식(실제 읽기 코드) |
|---|---|---|---|---|
| `signing` | `ai-ops-slack-signing` | `token` | 개인 경로 `slack-credentials/signing-secret` | 16진수 32자(`slack_web.read_private_secret`) |
| `bot` | `ai-ops-slack-bot` | `token` | 개인 경로 `slack-credentials/bot-token` | `xoxb-…`(`read_private_secret(token=True)`) |
| `sender_client` | `ai-ops-slack-sender-client` | `token` | **새로 생성** `openssl rand -hex 16` | 16진수 32자. relay와 sender가 같은 Secret을 쓴다 |
| `event_identity` | `ai-ops-events-identity` | `token`, `tls.crt`, `tls.key` | **새로 생성**(2절) | token `[A-Za-z0-9_-]{32,256}`(`alert_receiver.read_token`), SAN `ai-ops-events.ai-ops.svc` |
| `slack_tls` | `ai-ops-slack-tls` | `tls.crt`, `tls.key` | cert-manager(`wf11_edge` Certificate) | 직접 만들지 않는다 |
| `recovery.secrets.reader` | `ai-ops-recovery-reader` | `token`, `ca.crt` | Kubernetes(`wf11_edge` SA token Secret) | 직접 만들지 않는다 |
| `recovery.secrets.github` | `ai-ops-recovery-github` | `token` | **사용자 발급**: `joungGo/c03-execution-lab`, Contents: Read 하나 | `[A-Za-z0-9_]{20,4096}`(`recovery_sources.TOKEN`) |
| (`collector`) | 없음 | — | PR #75(평문 HTTP Prometheus)로 필요 없어졌다 | — |

monitoring namespace에도 하나가 필요하다.

| 이름 (ns `monitoring`) | 키 | 출처 |
|---|---|---|
| `ai-ops-alertmanager-auth` | `token`, `ca.crt` | 2절. token은 `event_identity`와 **같은 값**이다. 플랫폼 values `alertmanagerSpec.secrets`가 이름을 고정한다 |

`alertmanager-slack-secret`·`alertmanager-heartbeat-secret`과 앱 Secret은 기존 설치 가이드와 설치용 개인 자격 7종을 따른다. 이 문서의 범위가 아니다.

## 2. 순서

1. **namespace `ai-ops`·`monitoring`을 만든 직후, 모니터링 Application을 적용하기 전**에 `ai-ops-alertmanager-auth`와 `ai-ops-events-identity`를 만든다. 플랫폼 main은 이미 ai-events로 전환돼 있다. 그래서 인증 Secret이 없으면 Alertmanager Pod가 시작하지 못하고, 기존 Watchdog과 Slack 공지까지 늦어진다.
2. Slack 3종(`signing`, `bot`, `sender_client`)과 GitHub 토큰은 WF10 적용 전에 만든다.
3. `slack_tls`·`reader`는 `wf11_edge` manifest를 적용하면 생긴다. Certificate가 Ready이고 SA token이 채워졌는지 확인한다.

## 3. 명령 (값 비출력)

먼저 같은 이름의 Secret이 있는지 조회한다. 하나라도 있으면 새로 만들지 않고 멈춘 뒤 조정한다.

```sh
for s in ai-ops-events-identity ai-ops-slack-signing ai-ops-slack-bot ai-ops-slack-sender-client ai-ops-recovery-github; do
  kubectl -n ai-ops get secret "$s" --ignore-not-found -o name; done
kubectl -n monitoring get secret ai-ops-alertmanager-auth --ignore-not-found -o name
```

자료는 개인 경로에 만든다. 저장소 작업트리나 `/tmp`에는 만들지 않는다.

```sh
set +x; umask 077
D=$(mktemp -d "$HOME/.local/share/url-shortener-ai-ops/wf11-secrets.XXXXXX"); chmod 700 "$D"
# 사건 수신기 CA·서버 인증서 (SAN = Alertmanager server_name)
openssl req -x509 -newkey rsa:3072 -nodes -days 365 -subj /CN=urlshortener-ai-ops-wf11-internal-ca \
  -keyout "$D/ca.key" -out "$D/ca.crt"
openssl req -newkey rsa:2048 -nodes -subj /CN=ai-ops-events.ai-ops.svc -keyout "$D/tls.key" -out "$D/server.csr"
printf '%s\n' 'subjectAltName=DNS:ai-ops-events.ai-ops.svc,DNS:ai-ops-events.ai-ops.svc.cluster.local' \
  'basicConstraints=critical,CA:FALSE' 'keyUsage=critical,digitalSignature' 'extendedKeyUsage=serverAuth' > "$D/server.ext"
openssl x509 -req -in "$D/server.csr" -CA "$D/ca.crt" -CAkey "$D/ca.key" -CAcreateserial \
  -days 90 -sha256 -extfile "$D/server.ext" -out "$D/tls.crt"
openssl rand -hex 32 | tr -d '\n' > "$D/event-token"
openssl rand -hex 16 | tr -d '\n' > "$D/sender-client"
openssl verify -CAfile "$D/ca.crt" "$D/tls.crt"
# macOS 기본 LibreSSL 3.3.6에는 -ext가 없다. 공개 정보인 SAN 줄만 본다.
openssl x509 -in "$D/tls.crt" -noout -text | grep -A1 "Subject Alternative Name"
openssl x509 -in "$D/tls.crt" -noout -enddate
```

GitHub 토큰은 사용자가 발급한다. 사용자가 직접 `$D/github-token`(0600)에 저장한다. 값을 대화나 명령 인자로 넘기지 않는다.

**형식 검사**는 실제 읽기 코드로 한다. 결과로 `ok`만 출력한다.

```sh
PYTHONPATH=ai-ops/src "$VENV/bin/python" - "$D" "$HOME/.local/share/url-shortener-ai-ops/slack-credentials" <<'PY'
import re, sys
from pathlib import Path
from ai_ops.slack_web import read_private_secret
from ai_ops.recovery_sources import TOKEN
d, s = Path(sys.argv[1]), Path(sys.argv[2])
read_private_secret(s / 'signing-secret'); read_private_secret(s / 'bot-token', token=True)
read_private_secret(d / 'sender-client')
assert re.fullmatch(r'[A-Za-z0-9_-]{32,256}', (d / 'event-token').read_text())
assert TOKEN.fullmatch((d / 'github-token').read_text().strip())
print('ok')
PY
```

**server dry-run**을 먼저 하고, 통과하면 같은 명령에서 `--dry-run=server`를 빼고 실행한다. label은 B5와 같다.

```sh
kubectl -n monitoring create secret generic ai-ops-alertmanager-auth --from-file=token="$D/event-token" --from-file=ca.crt="$D/ca.crt" --dry-run=server -o name
kubectl -n ai-ops create secret generic ai-ops-events-identity --from-file=token="$D/event-token" \
  --from-file=tls.crt="$D/tls.crt" --from-file=tls.key="$D/tls.key" --dry-run=server -o name
kubectl -n ai-ops create secret generic ai-ops-slack-signing --from-file=token="$HOME/.local/share/url-shortener-ai-ops/slack-credentials/signing-secret" --dry-run=server -o name
kubectl -n ai-ops create secret generic ai-ops-slack-bot --from-file=token="$HOME/.local/share/url-shortener-ai-ops/slack-credentials/bot-token" --dry-run=server -o name
kubectl -n ai-ops create secret generic ai-ops-slack-sender-client --from-file=token="$D/sender-client" --dry-run=server -o name
kubectl -n ai-ops create secret generic ai-ops-recovery-github --from-file=token="$D/github-token" --dry-run=server -o name
```

**적용 후 확인**도 값 없이 한다. 결과로 `same`/`different`만 출력한다.

```sh
h() { kubectl -n "$1" get secret "$2" -o "jsonpath={.data.$3}" | base64 -d | shasum -a 256 | cut -c1-64; }
[ "$(h ai-ops ai-ops-events-identity token)" = "$(h monitoring ai-ops-alertmanager-auth token)" ] && echo same || echo different
[ "$(h monitoring ai-ops-alertmanager-auth 'ca\.crt')" = "$(shasum -a 256 < "$D/ca.crt" | cut -c1-64)" ] && echo same || echo different
```

## 4. 보관·정리

- `$D`에는 CA 개인키가 있다. WF11이 끝날 때까지 개인 경로에 0700으로 둔다. 철거 뒤에는 기준표 8절(자격 회수)에 따라 삭제하고, 삭제 사실을 기록한다.
- GitHub 토큰은 철거 직후 사용자가 GitHub에서 폐기한다.
- Slack 자격은 기존 개인 경로에 남긴다. 폐기하지 않는다.
- 클러스터의 Secret은 B5 정리 순서를 따른다.
  - AI namespace Secret은 WF10 제거와 함께 지운다.
  - `ai-ops-alertmanager-auth`는 **모니터링 종료 단계에서** 지운다.

## 5. 사전 연습 기록 (2026-11-26, 가짜 자료)

scratchpad에서 2~3절 명령을 **일회용 가짜 자료**로 실행했다. 실제 Slack 자격·GitHub 토큰·클러스터는 쓰지 않았다. 끝난 뒤 자료는 삭제했다.

- **호스트 openssl:** `/usr/bin/openssl`, LibreSSL 3.3.6
  - `x509 -ext`가 없어 실패했다. `-text | grep` 방식으로 바꿨다.
  - 그 밖의 생성 명령과 `openssl verify`는 통과했다.
- **형식 검사:** 가짜 signing·bot·sender·event·GitHub 값으로 실제 읽기 함수를 통과했다(`ok`).
- **TLS 연결 확인:** 만든 서버 인증서로 로컬 TLS 서버를 띄웠다. Alertmanager와 같은 방식(만든 CA 신뢰, `server_name=ai-ops-events.ai-ops.svc`)으로 접속해 성공했다(`TLSv1.2`).
- **하지 않은 것:** kubectl(server dry-run 포함)은 클러스터가 없어 하지 않았다. 설치 때 3절 순서대로 한다.
