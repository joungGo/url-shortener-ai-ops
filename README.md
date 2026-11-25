# URL Shortener AI Ops

URL 단축 플랫폼의 운영 증거 검증, 규칙·AI 조사, 제한된 복구 제안과 검증 코드를 관리하는 독립 저장소다. 기존 `f-lab-edu/F-Lab-DevOps`의 AI 경로 이력을 보존해 분리했다. 비공개 저장소이며 원문 비밀값·개인 사건 백업·Terraform state는 포함하지 않는다.

- [AI 구성·기능·검증 범위](ai-ops/README.md)
- [플랫폼 연결·재설치 전환 절차](integration/README.md)
- [현재 작업 인계](HANDOFF.md)
- [추출 경로·원본/분리 commit 대응](docs/repository-extraction.json)
- [저장소 구조와 CI 검사 안내](docs/repository-and-ci.md)

## 로컬 검증

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r ai-ops/requirements.lock
PYTHONPATH=ai-ops/src .venv/bin/python -m unittest discover -s ai-ops/tests -p 'test_*.py' -v
```

배포 정의 통합 검사는 Helm·Ruby와 고정된 플랫폼 chart 소스가 필요하다. `AI_OPS_PLATFORM_SOURCE_ROOT`를 `integration/platform-source.json`의 정확한 commit checkout으로 지정하고 `PYTHONPATH=ai-ops/src .venv/bin/python ai-ops/scripts/check_deployment.py`를 실행한다. 자동 CI는 이 공개 의존성을 고정 SHA로 읽는다. 코드 작성·단위 시험에는 AWS 인프라가 필요하지 않다.

## 현재 상태

새 main은 기존 main(PR155)에서 추출했다. 원본 PR156의 미병합 변경은 `codex/c04-request-time-20261112`의 별도 검토 대상으로 이관한다. C04 실환경 성공과 C05 최종 완료는 미완료다. 새 live 장애·모델 호출·Terraform은 이관 과정에서 실행하지 않는다.

운영 배포 workflow와 원문 사건·로컬 키·state는 플랫폼/개인 보존 위치에 유지한다. 새 저장소의 권한·Secret·OIDC를 자동 확대하지 않는다. 기존 플랫폼 저장소의 AI 복사본은 실행 연결 전환까지 보존한다. 옛 기록의 상대 경로와 commit/PR 번호는 원본 monorepo 기준이며, 새 코드 변경은 이 저장소에서 관리한다.
