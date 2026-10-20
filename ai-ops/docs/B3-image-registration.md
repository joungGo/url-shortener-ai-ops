# B-03 수동 이미지 빌드·등록 실행 기록

최신 자체 관측 상태: 2026-10-19 19:28:15 KST 자체 관측·중지/복구 실환경 검증 완료. PR #36·main e1b30b5의 새 이미지 등록 성공, ECR 검사 전체 0건과 코드 해시 일치를 확인했다. Helm revision 4에서 Prometheus up=1·진단 갱신·수동 수집/보고서 partial 각 1회 증가를 확인했다. AI StatefulSet 0→1 복구 후 동일 PVC·기존/신규 보고서 재조회와 해시·집계 보존, 앱 UID/generation 불변·Ready 2개를 확인했다. 외부 모델은 호출하지 않았다. [관측·중지/복구 절차](B3-observability-and-recovery.md). 아래 이전 이미지·배포 내용은 해당 시점의 이력이다.

최신 상태: 2026-10-19 18:12 KST 실제 수집→규칙 부분 보고서→PVC 저장→JSON/KST 재조회 연결 검증 완료. PR #34·#35 수정이 포함된 main 5d432f5 이미지로 일반 경로를 실행했고 Pod 2개 상태·설정, 배포/Argo·캐시 지표 등 7개 항목을 수집했다. 총 18개 중 available 7·invalid 2·missing 7·stale 2로, 자료 부족은 partial/unknown/insufficient_evidence로 유지했다. 이전 Pod의 저장 보고서도 새 Pod에서 해시 변화 없이 재조회했다. 외부 AI·키·예산 경로는 호출하지 않았고 기존 앱 UID/generation·Ready 2개는 유지됐다. [실제 수집 검증](B2-live-validation.md). 아래 이전 이미지·배포 결과는 해당 시점의 이력이다.

2026-10-19 **B-03-SEC-01 완료**: 사용자 환경 승인 후 [등록 실행 39219009295](https://github.com/f-lab-edu/F-Lab-DevOps/actions/runs/39219009295)가 성공했다. 소스는 main `b6548b82e588b09efee925a2c663ee71d3b91f1d`, 새 digest는 `sha256:bbfa1859831b5811d4bfe3057adf25141ed1c66b38c9165d235952059ef286be`다. **16:57:16 KST ECR 스캔 COMPLETE·전체 0건**으로 기존 HIGH 8건 해소를 확인했다. 등록 artifact·manifest/config digest·소스 파일 3개 해시를 대조했다. production 읽기 전용 지원도 새 이미지에 포함됐으며, [비활성 values](../config/b3-values.preflight.yaml)에 이 digest를 고정했다. AI Pod 배포는 아직 미실행이다. 다음은 암호화 StorageClass와 실제 API 경로·권한·통신·PVC 검증이다.

## 최초 등록 이력

2026-10-19 KST PR #32가 main에 병합된 뒤 이미지 수동 workflow를 실행했다. 소스는 `c2169f7b5398c07185906814d3b1d14b33ac7fab`이다.

## 등록 전 빌드 검증 완료

[빌드 실행 39204079671](https://github.com/f-lab-edu/F-Lab-DevOps/actions/runs/39204079671)은 `publish=false`로 성공했다. 잠금 의존성 설치·단위 시험·linux/amd64 빌드·통신 없는 read-only/non-root 실행·런북 검증·identity 기록·artifact 보존 단계를 통과했다. AWS 인증·이미지 push 단계는 실행하지 않았다.

artifact `ai-ops-image-39204079671-1`의 `image-registration.json`을 내려받아 다음 값을 확인했다.

- source revision: `c2169f7b5398c07185906814d3b1d14b33ac7fab`
- build image ID: `sha256:f5124a8e8184013834479474140c897645d9c7aab0edd3363eab31c9bcc96728`
- published: `false`; repository·registry digest: `null`
- Dockerfile·requirements.lock·runbook index의 SHA256 기록 존재

이 image ID는 빌드 전용 실행의 로컬 식별자다. ECR digest나 후속 등록 실행의 image ID로 사용하지 않는다. 등록 실행은 같은 소스를 다시 빌드·검증하고, 그 실행에서 검증한 이미지를 push·digest pull로 대조한다.

## 실제 등록 완료

사용자가 GitHub 환경을 승인한 뒤 [등록 실행 39204191122](https://github.com/f-lab-edu/F-Lab-DevOps/actions/runs/39204191122)가 성공했다. main 제한·전용 환경 승인 설정을 유지했으며 단위 시험·빌드·OIDC 인증·ECR 로그인·push·digest pull 대조·identity 기록·artifact 보존 단계가 모두 성공했다.

| 항목 | 확인값 |
|---|---|
| ECR | `716174522908.dkr.ecr.ap-northeast-2.amazonaws.com/urlshortener-practice-ai-ops` |
| 소스 | `c2169f7b5398c07185906814d3b1d14b33ac7fab` |
| 등록 시각 | 2026-10-19 14:25:34 KST |
| 태그 | `sha-c2169f7b5398c07185906814d3b1d14b33ac7fab-r39204191122-a1` |
| registry digest | `sha256:f1c06fe3d07598c8e13db34217e3b6462a5ba4ddf94753648e34eb8e98c65511` |
| 등록 실행 image ID | `sha256:820d3c9a3fa0f8fd1ca96b23721618ba94e385b3a0e9133b096615279d2525af` |
| artifact | `ai-ops-image-39204191122-1`, `image-registration.json` |

artifact를 내려받아 `published=true`·source revision·태그·repository·digest를 실제 ECR과 대조했다. Dockerfile·의존성 lock·runbook index 해시도 해당 Git 커밋과 일치했다. ECR manifest 원문의 SHA256이 registry digest와 같고, manifest의 config digest가 artifact image ID와 같음을 별도로 확인했다. 빌드 전용 실행과 등록 실행은 서로 빌드가 다르므로 각각의 image ID를 구분한다.

## ECR 스캔 결과

**최초 스캔 당시 필수 후속 작업 `B-03-SEC-01` (현재 완료):** 사용자 요청으로 아래 HIGH 8건을 반드시 해결할 작업으로 등록했다. [AI 작업 리스트](<../../실습 가이드 문서 모음/AI/구현계획/AI 작업 리스트.md#b-03-sec-01-ai-이미지-high-취약점-8건-해결>)에서 수정·재등록·재스캔 및 완료 기준을 관리한다. 아래는 기존 이미지의 과거 결과이며 수정본의 최신 결과는 문서 최상단과 아래 재등록 기록을 따른다.

2026-10-19 14:25:42 KST 스캔 상태 `COMPLETE`를 확인했다. 보고 건수는 **HIGH 8·MEDIUM 14·LOW 7**이다. 스캔 완료는 취약점 없음이나 운영 배포 승인과 다르다. 최초 HIGH 지적은 다음 OS 패키지에 해당했다. 수정 버전과 제거한 패키지는 [대응 기록](B3-image-security.md)에 정리했다.

| ECR에서 보고한 식별자 | 패키지 | 설치 버전 |
|---|---|---|
| CVE-2026-85091 | zlib | `1.3.dfsg+really1.3.1-1` |
| CVE-2026-103111 | pcre2 | `10.46-1~deb13u2` |
| CVE-2026-84784 | openssl | `3.5.7-1~deb13u2` |
| CVE-2026-84782 | openssl | `3.5.7-1~deb13u2` |
| CVE-2026-19499 | glibc | `2.41-12+deb13u4` |
| CVE-2026-5435 | glibc | `2.41-12+deb13u4` |
| CVE-2026-54873 | openssl | `3.5.7-1~deb13u2` |
| CVE-2026-72897 | openssl | `3.5.7-1~deb13u2` |

이미지 등록과 식별자 검증은 완료했다. 다음은 실제 대상·네트워크·StorageClass를 구성해 diagnostics Pod를 배포·검증하는 작업이다. 현재 AI Pod 배포·읽기 권한/통신/PVC 재연결은 미실행이며 자동 조사·장애 조치 기능 전체가 완성된 상태는 아니다.


## 보안 수정본 재등록·재스캔 완료

- 실행: [39219009295](https://github.com/f-lab-edu/F-Lab-DevOps/actions/runs/39219009295), 성공. 사용자 환경 승인·OIDC·동일 이미지 push/pull 대조 완료.
- 소스: `b6548b82e588b09efee925a2c663ee71d3b91f1d` (PR #33 병합).
- 등록 시각: 2026-10-19 16:57:07 KST.
- 태그: `sha-b6548b82e588b09efee925a2c663ee71d3b91f1d-r39219009295-a1`.
- registry digest: `sha256:bbfa1859831b5811d4bfe3057adf25141ed1c66b38c9165d235952059ef286be`.
- 실행 image ID / manifest config digest: `sha256:91dd0eda50af60eae65f094e6d387bf37dc44f7ee2c4aff8e85178b41a3aab2c`.
- artifact: `ai-ops-image-39219009295-1` / `image-registration.json`.
- ECR 검사: 2026-10-19 16:57:16 KST, `COMPLETE`, HIGH·MEDIUM·LOW 포함 전체 0건. 기존 HIGH 식별자 8개 모두 없음.
- Git 소스·현재 로컬 파일 3개 해시, ECR manifest 해시, config image ID 일치. [검증 JSON](assets/B3-image-security-validation.json)에 보존.

비활성 values의 image digest를 위 값으로 고정했다. 기존 이미지 삭제나 AI Pod 배포는 수행하지 않았다.


## 최종 수집 호환 수정 이미지 등록 — 2026-10-19

- 소스: `5d432f5e96a01357bf7efa711bb600197a13c95b` (PR #34·#35 포함).
- 실행: [39227539145](https://github.com/f-lab-edu/F-Lab-DevOps/actions/runs/39227539145), 사용자 승인·등록·동일 이미지 pull 대조 성공.
- 태그: `sha-5d432f5e96a01357bf7efa711bb600197a13c95b-r39227539145-a1`.
- registry digest: `sha256:49d1775ba0ebf1c2500e8cb7dfe6f46b40b955e08293954c86073123983080c5`.
- image ID / manifest config: `sha256:db65c914df8078552242f9d8432338ae6ccd1426d52259dc0d5809d8fa2f7e95`.
- artifact: `ai-ops-image-39227539145-1` / `image-registration.json`.
- ECR 검사: COMPLETE·전체 0건. UTC 검사 시각과 원본 등록 기록은 [B2 검증 JSON](assets/B2-live-validation.json)의 list_shape_fix를 따른다.
- Helm revision 3의 실제 코드 해시·Ready·같은 PVC와 일반 수집/보고서 저장/재조회 검증 통과.

## 자체 관측 이미지 등록 — 2026-10-19 19:28:15 KST

[등록 실행 39235754071](https://github.com/f-lab-edu/F-Lab-DevOps/actions/runs/39235754071) 성공. source `e1b30b510e49774ce25d54c79abc7ea21f221ec7`, digest `sha256:8f716e93ce4d51fa5c310840d9084533b8265565639182c220f4e4cc55baff76`, image ID `sha256:1dd435e607983f10f4625e4933aa72d64047652fce3ec728d19659c2ed9b2abd`다. ECR COMPLETE 전체 0건과 manifest·artifact·소스 해시를 대조했다. 실제 Helm revision 4의 같은 이미지와 Pod 코드 해시도 일치한다.
