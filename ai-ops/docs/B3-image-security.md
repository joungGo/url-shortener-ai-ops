# B-03-SEC-01 AI 이미지 HIGH 지적 대응

2026-10-19 **B-03-SEC-01 완료**: 사용자 환경 승인 후 [등록 실행 39219009295](https://github.com/f-lab-edu/F-Lab-DevOps/actions/runs/39219009295)가 성공했다. 소스는 main `b6548b82e588b09efee925a2c663ee71d3b91f1d`, 새 digest는 `sha256:bbfa1859831b5811d4bfe3057adf25141ed1c66b38c9165d235952059ef286be`다. **16:57:16 KST ECR 스캔 COMPLETE·전체 0건**으로 기존 HIGH 8건 해소를 확인했다. 등록 artifact·manifest/config digest·소스 파일 3개 해시를 대조했다. production 읽기 전용 지원도 새 이미지에 포함됐으며, [비활성 values](../config/b3-values.preflight.yaml)에 이 digest를 고정했다. AI Pod 배포는 아직 미실행이다. 다음은 암호화 StorageClass와 실제 API 경로·권한·통신·PVC 검증이다.

## 변경 이유와 실제 수정

Debian stable에는 PCRE2·OpenSSL 보안 수정본이 있지만 glibc 두 지적은 현재 stable 수정본이 없었다. zlib는 tracker의 영향 범위 설명과 패키지 판정이 엇갈렸다. 미사용 추정·지적 숨김으로 닫지 않고 공식 Python Alpine 3.24 기반으로 전환해 필요한 실행 구성만 남겼다. 앱 Dockerfile은 이번 변경 대상이 아니다.

Python을 3.11.17로 갱신하고 공식 이미지 index `sha256:d9368b3a5ac59afea7b5d4f2e2aea0941dbf9fdee9c369c5bec00b98244bc929`를 고정했다. zlib는 안정 저장소의 `1.3.2-r1`로 명시 갱신한다. 고정 의존성과 musllinux wheel만 설치하고 non-root·읽기 전용 실행 경계를 유지한다. runtime에서는 pip·setuptools·wheel·ensurepip를 제거한다. 빌드 단계의 pip는 잠금 의존성 설치에만 사용한다.

| 기존 HIGH 식별자 | 기존 패키지 | 후보의 대응과 확인값 |
|---|---|---|
| CVE-2026-85091 | zlib | Alpine 보안 수정 `1.3.2-r1` 설치 |
| CVE-2026-103111 | pcre2 | 실행 구성에 패키지 없음; 필수 런북·Pydantic·TLS·SQLite 검증 |
| CVE-2026-84784, CVE-2026-84782, CVE-2026-54873, CVE-2026-72897 | openssl | libssl3/libcrypto3 `3.5.9-r0`; Python 실제 OpenSSL 3.5.9 확인 |
| CVE-2026-19499, CVE-2026-5435 | glibc | glibc 대신 musl `1.2.6-r2`; ABI 변경의 실행 회귀 확인 |

처음 후보에서 기존 8건은 제거됐지만 Trivy가 jaraco.context·wheel의 HIGH 2건과 pip/setuptools 지적을 추가로 검출했다. 이들은 runtime에서 사용하지 않는 빌드 도구에 포함돼 있어 최종 runtime에서 해당 도구와 ensurepip를 제거했다. 마지막 스캔에서는 OS·Python 패키지 지적 모두 0건이었다. 원래 Debian 이미지의 저장된 ECR 스캔 8건은 역사적 결과로 유지한다.

## 검증 결과와 한계

- 고정 의존성의 호스트 전체 Python 154건·실습/운영 Helm 읽기 경계·잘못된 설정 11종·위험 변경 3종 검출 통과.
- 최종 linux/amd64 runtime에서 수집·production 규칙 보고서·저장/재조회·오프라인 진단 34건 통과.
- UID 10001·network none·read-only·capability 제거 조건에서 런북·Pydantic core·로컬 DNS·SQLite 및 zlib 기능 확인.
- 외부 통신 없이 로컬 인증서로 TLS handshake 성공과 잘못된 hostname 거부 확인. 실제 Kubernetes API 접근 시험과 구분한다.
- Docker socket 없이 저장 archive를 고정 Trivy 이미지로 검사해 취약점 0건. [검증 기록](assets/B3-image-security-validation.json)에 base·패키지·스캔·이미지 식별자를 남겼다.

Docker Desktop containerd의 로컬 descriptor는 OCI index이며 archive의 config digest와 다르다. archive의 실행 설정·rootfs layer digest와 로컬 이미지의 일치를 대조했고 Trivy가 읽은 config digest도 archive와 일치했다. 이 로컬 ID들을 미래 ECR registry digest와 혼용하지 않는다.

## 원격 반영과 완료 확인

[PR #33](https://github.com/f-lab-edu/F-Lab-DevOps/pull/33)의 계약·이미지 CI가 성공한 뒤 main에 병합했다. 기존 main 제한·사용자 환경 승인·전용 OIDC 역할로 새 이미지를 등록했다. ECR manifest SHA256과 등록 digest, manifest config와 artifact image ID, Git 소스의 Dockerfile·lock·런북 색인 해시가 일치했다. 해당 digest의 ECR 검사 결과는 COMPLETE·0건으로 위 8개 식별자도 검출되지 않았다. 자세한 식별자와 UTC 검사 시각은 [검증 JSON](assets/B3-image-security-validation.json)의 ecr_verification에 보존한다.

기존 취약 이미지와 그 HIGH 8·MEDIUM 14·LOW 7건 기록은 과거 결과로 유지하며 이번에 삭제하지 않았다. 새로운 스캔 DB에서 결과가 달라질 수 있으므로 0건은 위 검사 시점의 결과다. 비활성 values는 새 digest를 참조하지만 enabled=false를 유지하고 StorageClass·API CIDR은 미확정으로 둔다.

## 확인한 공식 자료

- [Debian PCRE2 수정본](https://security-tracker.debian.org/tracker/CVE-2026-103111): trixie security `10.46-1~deb13u3`.
- [Debian glibc 상태](https://security-tracker.debian.org/tracker/CVE-2026-19499), [두 번째 glibc 상태](https://security-tracker.debian.org/tracker/CVE-2026-5435): stable 수정본 부재와 후속 버전의 수정 상태.
- [Debian zlib 영향 범위](https://security-tracker.debian.org/tracker/CVE-2026-85091): 판정과 원인 코드·수정 commit 기록.
- [OpenSSL 2026-09-29 보안 공지](https://openssl-library.org/news/secadv/20260929.txt): 3.5 계열 수정 버전 3.5.9.
- [Alpine 3.24 공식 security DB](https://secdb.alpinelinux.org/v3.24/main.json): zlib `1.3.2-r1` 수정 기록.
- [공식 Python 이미지 설명](https://github.com/docker-library/docs/blob/master/python/README.md): Alpine의 musl 사용과 호환성 차이.
