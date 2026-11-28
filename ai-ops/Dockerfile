# AI 실행에 필요한 라이브러리만 제공하는 공식 Alpine 베이스를 고정한다.
FROM python:3.11.17-alpine3.24@sha256:5fb4bc53dd0180955e31e5245b648bbb24a4ac4ab03a1de81513642325220bcd AS base
# 고정 베이스의 보안 지적을 배포판의 수정 패키지로 해결한다.
RUN apk add --no-cache --upgrade zlib=1.4-r0 musl=1.2.6-r3 krb5-libs=1.22.3-r0 libssl3=3.5.9-r1 libcrypto3=3.5.9-r1
# 패키지 제거·수정 버전과 Python 필수 기능을 빌드 중에도 확인한다.
RUN python - <<'PY_CHECK'
import pathlib, ssl, sqlite3, zlib
packages = {}
for entry in pathlib.Path('/lib/apk/db/installed').read_text().split('\n\n'):
    fields = dict(line.split(':', 1) for line in entry.splitlines() if ':' in line)
    if 'P' in fields:
        packages[fields['P']] = fields['V']
assert packages['zlib'] == '1.4-r0'
assert packages['libssl3'] == packages['libcrypto3'] == '3.5.9-r1'
assert packages['musl'] == '1.2.6-r3'
assert packages['krb5-libs'] == '1.22.3-r0'
assert not any(name in packages for name in ('glibc', 'pcre2'))
assert ssl.OPENSSL_VERSION.startswith("OpenSSL 3.5.9 ")
assert zlib.decompress(zlib.compress(b'ai-ops-runtime')) == b'ai-ops-runtime'
with sqlite3.connect(':memory:') as database:
    assert database.execute('SELECT 1').fetchone() == (1,)
assert ssl.create_default_context().get_ca_certs()
PY_CHECK

FROM base AS dependencies
WORKDIR /build
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: --target=/install -r requirements.lock

# WF07 준비는 복구 diff가 이미지 값 하나만 바꾸는지 `helm template` 이중 렌더로 검증한다(c_pr_link).
# CI와 같은 Helm v4.1.3 공식 배포본을 sha256으로 고정해 바이너리만 실행 이미지에 넣는다(2026-11-28 실물에서 부재 발견).
# ECR BASIC 스캔은 Go 바이너리를 보지 않으므로 버전·sha256·CVE 확인은 사람이 맡는다.
FROM base AS helm
RUN python - <<'HELM_FETCH'
import hashlib, io, os, tarfile, urllib.request
URL = 'https://get.helm.sh/helm-v4.1.3-linux-amd64.tar.gz'
SHA256 = '02ce9722d541238f81459938b84cf47df2fdf1187493b4bfb2346754d82a4700'
data = urllib.request.urlopen(URL, timeout=120).read()
assert hashlib.sha256(data).hexdigest() == SHA256, 'helm_archive_digest_mismatch'
with tarfile.open(fileobj=io.BytesIO(data)) as archive:
    member = archive.getmember('linux-amd64/helm')
    assert member.isfile() and member.size < 200_000_000
    with open('/usr/local/bin/helm', 'wb') as binary:
        binary.write(archive.extractfile(member).read())
os.chmod('/usr/local/bin/helm', 0o755)
HELM_FETCH

FROM base AS runtime
# 빌드 도구는 의존성 단계에서만 사용하고 실행 이미지에서 제거한다.
RUN python -m pip uninstall --yes pip setuptools wheel \
    && rm -rf /usr/local/lib/python3.11/ensurepip
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/ai-ops/src:/opt/ai-ops/dependencies \
    AI_OPS_RUNBOOK_ROOT=/opt/ai-ops/runbooks \
    HOME=/tmp \
    HELM_CACHE_HOME=/tmp/helm/cache \
    HELM_CONFIG_HOME=/tmp/helm/config \
    HELM_DATA_HOME=/tmp/helm/data
COPY --from=helm /usr/local/bin/helm /usr/local/bin/helm
WORKDIR /opt/ai-ops
COPY --from=dependencies /install ./dependencies
COPY src/ ./src/
COPY runbooks/ ./runbooks/
COPY config/metric-queries.json ./config/metric-queries.json
# 키·로컬 조사 기록·사이트·평가 자료는 이미지에 넣지 않는다.
RUN mkdir /data && chown 10001:10001 /data \
    && python -c 'from ai_ops.replay_tools import _runbook; [_runbook(name) for name in ("cache", "database", "deployment")]'
USER 10001:10001
ENTRYPOINT ["python", "-m", "ai_ops"]
CMD ["--help"]
