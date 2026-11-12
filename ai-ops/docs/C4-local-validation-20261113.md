# C-04 오프라인 재검증 · 2026-11-13

별도 AI 저장소의 미병합 head `0997a96142ca37e8195b9574c7766b095ac81753`를 검증했다. **로컬 재검증은 통과**했으며 구현 코드는 변경하지 않았다. C-04 실환경 복구 성공과 C-05 최종 완료는 아직 미완료다.

## 확인 결과

| 검사 | 결과 |
| --- | --- |
| 전체 회귀 테스트 | 880개 통과·skip 0·28.337초 |
| 요청 시각·복구 근거 집중 테스트 | 15개 통과·skip 0·0.059초 |
| Python 구문 | 287개 파일 통과 |
| 생성 schema 비교 | 14개 일치, 파일 재생성 없음 |
| fixture 재현 검사 | dev·holdout·holdout v2·holdout v3 통과 |
| 저장소 소유권·실행 경계 | 통과, 실제 실행 저장소 전환은 차단 유지 |
| Helm 배포·경보 통합 계약 | 고정 운영 revision `9573b8c9f323a643e65bb58c323dc932c681a6b2`의 로컬 Git 자료로 통과 |
| 의존성 | Python 3.13.12, 잠금된 5개 패키지 버전 일치(Pydantic 2.13.5) |

배포 정적 검사의 첫 시도는 외부 운영 chart 사본이 없어 중지됐다. 이미 보존한 Git 객체에서 고정 경로 두 개만 추출해 검사 입력을 준비한 뒤 통과했다. 원격 조회나 chart 설치는 하지 않았다.

## 마지막 012 실습의 거절과 현재 보완

보존된 진단 자료에서 관측 시각이 요청 생성보다 **2.009996초 뒤**였음을 재확인했다. 기록된 다른 사건·대상·bundle/report·실패 binding·Pod/Deployment/image 결합 조건은 일치했다. `fresh_proposal_not_ready`의 내부 blocker는 `initial_incident_proof_not_ready`였으며 시각 순서가 맞지 않았다. 어느 호스트의 시계가 원인인지는 측정되지 않았다.

현재 코드의 다음 동작을 검증했다.

- 미래 관측으로 요청을 생성하면 DB 저장 전에 거절한다. 요청 시각을 소급하거나 관측 시각에 강제로 맞추지 않는다.
- 작은 시각 차이는 실제 시간 경과로만 처리한다. 최초 조회·재조회까지 포함해 최대 5초와 기존 리뷰 안내 마감 이내여야 한다.
- 대기 후 인증된 사건을 다시 읽고 초기 투영과 동일한지 확인한다. 미래 차이가 크거나 시계 변경·조회 실패·투영 변경·마감 초과가 생기면 중지한다.
- 로컬 인증 소켓에서 미래 요청 미저장과, 대기 후 요청 정확히 한 건 저장을 확인했다. 제안·PR 생성은 수행하지 않았다.

과거 진단은 원인 대조용 읽기 전용 자료다. 새 실습의 현재 관측·정상 기준선·승인으로 재사용하지 않는다. 최대 5초 대기가 실환경 전체 경로의 성공을 보장하지는 않는다. 리뷰 120초·시작 후 리뷰 마감 210초·소유 원복 240초·독립 원복 300초·회복 관찰 120초 조건은 유지한다.

## 재현과 근거

검증은 `~/.local/share/url-shortener-ai-ops/continuation-20261112/local-venv/bin/python`을 사용했다. 저장소 루트에서 `PYTHONDONTWRITEBYTECODE=1`을 설정하고 다음 명령으로 재현한다.

```sh
PYTHONPATH=ai-ops/src:ai-ops/tests <venv-python> -m unittest test_c_trial_request_time test_c_recovery_proof -v
PYTHONPATH=ai-ops/src <venv-python> -m unittest discover -s ai-ops/tests -p 'test_*.py' -v
<venv-python> ai-ops/scripts/check_repository_boundary.py
```

Helm 정적 검사에는 `integration/platform-source.json`에 고정한 경로·revision의 운영 소스가 필요하다. 이번 사본은 개인 검증 폴더의 `pinned-platform-source/`에 있다. 해당 절대 경로를 `AI_OPS_PLATFORM_SOURCE_ROOT`에 지정하고 `PYTHONPATH=ai-ops/src <venv-python> ai-ops/scripts/check_deployment.py`를 실행했다. fixture는 각 `build_*fixtures.py --check`, schema는 모델이 생성한 JSON과 기존 파일의 바이트 비교로 확인했다.

공개 가능한 결과·소스 해시는 [기계 결과](assets/C4-local-validation-20261113.json)에 있다. 전체 로그·고정 chart 사본·해시 목록은 `/Users/jounghyeon/.local/share/url-shortener-ai-ops/local-c04-validation-20261113/`에 보관했다. 개인 사건 원문·DB·키·토큰·state를 저장소에 추가하지 않았다. 클라우드·유료 모델 API·Terraform·실제 실행기는 호출하지 않았다.

## 다음 작업

1. [초안 PR #1](https://github.com/joungGo/url-shortener-ai-ops/pull/1)의 현재 정확한 head·최신 CI·리뷰 요건을 확인하고 정상 반영한다. 이 로컬 검증은 PR 리뷰나 병합 승인이 아니다.
2. `integration/README.md`에 따라 별도 저장소의 이미지 발행·OIDC·private chart 및 실행기/CI/원복/수집기 연결 전환을 계획한다.
3. 이후 새 설치의 UID·PVC·권한·정상 CI·worker 설정 로드와 독립 감시를 검증한다. 새 준비 결과 승인과 새 복구 PR의 정확한 head 사람 리뷰 후에만 실환경 C-04를 진행한다.

원격 PR 조회·병합·push는 이번 로컬 작업에 포함하지 않았다. A5/A7 미측정과 기존 B 범위, AI 포트폴리오 보류도 유지한다. 다음 추천: **GPT-6 SOL · High**.
