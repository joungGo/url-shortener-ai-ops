# WF-10 manifest 오프라인 schema 검증(A5) · 2026-11-19

상태: **로컬 구현·검증 완료 / 미커밋.** 기준 main `f6e04337239a6f981745674ff390ce8b89e37f81`.

## 목적과 방식

WF-10 renderer가 만드는 리소스 8개(ConfigMap·Deployment 2·Service 3·NetworkPolicy 2)를 클러스터 없이 Kubernetes 구조 기준으로 검증한다. 오타 필드, 잘못된 타입, 필수 필드 누락을 인프라 단계 전에 잡는 것이 목적이다.

- 기준: Kubernetes **v1.35.8** 공식 OpenAPI v3 명세. CI의 고정 kubectl, 플랫폼 EKS 1.35와 같은 버전이다.
- 새 바이너리나 라이브러리를 추가하지 않았다. 4종 리소스의 전이적 스키마 138개에서 검증 키워드만 추출해 `ai_ops/k8s_openapi_v1_35_8.json`(45,584바이트)으로 두었다. 이 파일은 이미지에도 포함된다.
- 출처 고정: `ai-ops/scripts/build_k8s_schema_subset.py`가 원본 3개 파일의 sha256을 고정값과 대조한 뒤에만 추출하며, `--check`로 재생성 결과와 비교한다. 원본은 kubernetes/kubernetes `v1.35.8`의 `api/openapi-spec/v3/`.
- 검증기 `ai_ops.k8s_schema`: `kubectl --validate=strict`처럼 unknown 필드를 거절하고 타입, 필수 필드, int32/int64 범위, int-or-string·Quantity 합집합을 검사한다. 오류에는 필드 경로만 담고 값은 담지 않는다.
- renderer 연결: `candidate()`가 출력 전에 리소스 8개를 모두 검증한다. 스키마에 맞지 않으면 `wf10_schema_rejected`로 거절하고 출력하지 않는다. 결과에 `schema_validation`(v1.35.8, 오프라인, server dry-run 아님)을 기록하고, 실행 조건에 `server_side_dry_run`을 추가했다.

## 한계

서버 dry-run이 아니다. admission, 기본값 적용, 필드 사이 관계, 이름 형식, quota, CRD, 정책 엔진 검사는 하지 않는다. 그런 검사는 실클러스터 단계의 `server_side_dry_run` 조건으로 남는다. 명세 버전을 바꾸려면 생성 스크립트의 고정 hash와 tag를 함께 갱신해야 한다.

## 검증

- 신규 `test_k8s_schema.py` 6개: 출처·참조 완결성, 생성 스크립트의 hash 불일치 거절, 리소스 8개 유효, 위반 12종 거절(unknown 필드, 타입, 필수, 범위, 잘못된 apiVersion, 미지원 kind), int-or-string·Quantity, renderer의 출력 거절.
- 생성 렌더러 출력 hash는 변경 전과 같다(`f8908414…`). 합성 후보 54,641바이트 / 64KiB.
- 전체 1608개(206.352초, skip1=WF03 환경 게이트) 통과. 저장소 경계·diff·compileall 통과.

검증 수치는 [검증 기록](wf10-manifest-schema-validation-validation-20261119.json)에 있다. 인프라·배포·Slack·실습 실행 없음. 기존 시간·승인·원복 조건 변경 없음.
