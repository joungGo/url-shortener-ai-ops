# 런북 키워드·메타데이터 검색 로컬 구현 · 2026-11-16

사용자 요청에 따라 AI main `54a90a9d4cf4f7ee0148f88c1146b32cd9f9900b`에서 로컬 검색을 구현했다. 기존 `get_runbook(document_id)`와 등록·버전·원문 SHA256 검증을 재사용한다. 새 검색은 Python API와 별도 CLI로만 제공하며 기존 여섯 조사 도구의 허용 목록, 모델 게이트웨이, Slack 어댑터, 큐·worker와 실행 경로에는 연결하지 않았다. 로컬 구현 뒤 코드 검토를 진행했고, 최종 CI·반영 결과는 같은 폴더의 runbook-search-integration-20261116 문서/JSON과 HANDOFF에서 별도로 기록한다. 실환경 배포·실제 연결은 수행하지 않는다.

## 사용 방법

AI 저장소 `/Users/jounghyeon/dev/url-shortener-ai-ops`에서 기존 잠금 의존성이 있는 Python 환경으로 실행한다. 아래 예시는 이미 보존한 로컬 venv를 사용한다.

```sh
PYTHONPATH=ai-ops/src /Users/jounghyeon/.local/share/url-shortener-ai-ops/continuation-20261112/local-venv/bin/python -m ai_ops.runbook_search --query '캐시 오류' --service redis --limit 3
PYTHONPATH=ai-ops/src /Users/jounghyeon/.local/share/url-shortener-ai-ops/continuation-20261112/local-venv/bin/python -m ai_ops.runbook_search --query ImagePullBackOff --document-id deployment
PYTHONPATH=ai-ops/src /Users/jounghyeon/.local/share/url-shortener-ai-ops/continuation-20261112/local-venv/bin/python -m ai_ops.runbook_search --symptom 'DB 지연' --failure-type db-latency --document-version 1
PYTHONPATH=ai-ops/src /Users/jounghyeon/.local/share/url-shortener-ai-ops/continuation-20261112/local-venv/bin/python ai-ops/scripts/check_runbook_search_local.py
```

Python에서는 `RunbookSearch(allowed_document_ids=['cache']).search('캐시 오류', service='redis')`를 사용한다. 생성자의 허용 ID는 신뢰 설정이며 질의의 `document_ids`는 그 범위를 좁힐 수만 있다. 생략하면 로컬 운영자가 등록 문서 전체를 검색한다. 이후 외부 요청 경로에 연결할 경우, 요청자·대상별 허용 ID를 신뢰 정책에서 결정해야 한다. CLI 자체는 외부 인증 API가 아니다.

## 검색과 출처

현재 등록 문서는 `cache`, `database`, `deployment` 세 개다. `runbooks/search-metadata.json`에 서비스·증상·장애 유형·키워드를 추가했으며 등록부 전체 해시와 각 문서 버전·해시에 결합했다. 필터는 `service`, `symptom`, `failure_type`, `document_version`, 기록된 코드 기준 `code_revision`, `document_ids`다. 필터를 함께 지정하면 모두 만족해야 한다.

- 키워드는 NFKC·대소문자 정규화 후 AND로 찾는다. 한국어는 부분 문자열, 영문은 단어와 식별자 구성 요소를 사용하며 `ImagePullBackOff만으로`처럼 한국어 조사가 붙은 영문 오류명도 처리한다. 임베딩·의미 검색·모델 호출은 없다.
- 문서의 절 본문과 등록 메타데이터를 구분해 검색한다. 본문 일치 용어당 4점, 절 제목 추가 3점, 메타데이터 일치 용어당 1점이며 동점은 문서 ID·시작 줄 순으로 정렬한다. 점수는 관련성 정렬용이며 원인 확률이나 문서 신뢰도가 아니다.
- 메타데이터만 일치한 결과는 `metadata_keyword`로 표시하고 본문 일치 줄을 만들지 않는다. 이때 문서 머리 절을 참조 위치로 반환한다. 질의 없이 필터만 지정하면 문서별 머리 절 하나를 반환한다. 메타데이터 일치는 해당 절에 실제로 그 증상이 적혀 있다는 뜻이 아니다.
- 결과에는 `document_id`, `document_version`, 원문 `content_sha256`, 등록 경로, 등록부·메타데이터 파일 해시를 포함한다. `section`은 제목 계층, 원문 기준 1부터 시작하는 양끝 포함 줄 번호, `L43-L57` 같은 절 ID, 원문 절 해시를 제공한다. ID는 줄 위치이므로 문서가 바뀌면 달라질 수 있다.
- Markdown ATX 제목을 기준으로 절을 나누며 코드 fence 내부의 제목은 구분자로 쓰지 않는다. 상위 제목 경로를 별도로 제공하고 각 절 범위는 겹치지 않는다. Setext 제목은 현재 지원하지 않는다.
- 발췌는 마스킹된 본문 중 최대 5줄·400자다. 일치한 원문 줄을 별도 기록하며 400자 초과로 발췌에서 잘린 부분은 `truncated`로 표시한다. 문서·절 해시는 마스킹 전 원문 기준이므로 발췌문 자체의 해시와는 다르다.
- 결과 없음을 `unavailable`, 입력·등록·무결성 오류를 `rejected`로 구분한다. 기본 결과 5개, 최대 10개이며 전체 일치 수와 잘림 여부를 제공한다. CLI의 결과 없음은 종료 0, 거절은 고정 오류 JSON과 종료 2다.

현재 런북 버전은 모두 1이며 확인일은 **2026-09-27**, 기록된 코드 기준은 `e4c51585bbc449f444527ed3c0614aac4b4b5700`, 범위는 정적 코드·로컬 검사다. **기존 `live_verified=false`를 유지**한다. 검색은 최신 소스 파일·클러스터를 조회하지 않으며 `current_source_code_checked=false`, `current_runtime_verified=false`를 명시한다. 해시는 조회한 로컬 사본의 일관성을 확인하며 서명·작성자 신뢰·최신성이나 현재 환경 적합성을 증명하지 않는다.

## 무결성과 실행 경계

등록부의 알려진 제한 형식만 파싱하며 임의 YAML 태그나 실행 기능을 사용하지 않는다. 새 YAML 패키지나 외부 의존성을 추가하지 않았다. 미등록 문서·요청 경로·범위 확대, 문서/메타데이터 버전·해시 불일치, 중복 키/ID, 링크/비정규 파일, 과대 파일, 읽기 중 등록부 변경을 거절한다. 원본 `get_runbook` 검증 결과와 같은 원문 스냅샷인지 대조한다. 문서 경로는 등록 ID로만 만들고 기존 플랫폼 소스 참조 경로는 열지 않는다.

파일별 최대 64KiB, 문서 최대 32개·절 최대 128개, 질의 512자·고유 용어 최대 12개·용어 2~64자, 응답 최대 64KiB를 적용한다. 허용된 로컬 등록부를 신뢰 설정으로 사용하며 폴더를 질의로 바꿀 수 없다. 메타데이터에 비밀값을 넣으면 거절하고 제목·발췌·질의는 기존 마스킹을 적용한다. 원문의 마스킹으로 줄 매핑이 달라지는 경우에는 잘못된 위치를 반환하지 않고 거절한다.

문서의 지시문은 `untrusted_reference_data`이며 모든 검색 결과의 `execution_allowed=false`다. 검색을 승인, PR 안내 영수증, 사람 리뷰, 장애 원인 확정 또는 복구 완료로 취급하지 않는다. 기존 시작 15초, 실행별 새 준비 승인, 정확 head의 실제 GitHub 사람 리뷰·CI, 안내 후 최소 120초/시작 후 210초 리뷰 마감, 소유 원복 240초·독립 원복 시작 300초·회복 관찰 120초와 UID/잠금/양쪽 ACK·자격 회수 조건을 유지한다.

## 검증과 남은 작업

새 검색 시험 37개에서 한국어·영문·필터·정렬·범위 제한·문서/절 해시와 줄 번호 독립 재계산·CRLF/fence·마스킹·무결성 실패·중복·링크/과대 파일·읽기 중 변경·CLI 거절을 확인했다. 별도 self-check는 대표 질의 세 개와 11개 결과의 출처를 재계산하고 외부 네트워크·프로세스 실행을 차단한 채 통과했다. 전체 회귀 및 정적 검사의 최종 수치는 같은 이름 JSON을 따른다. 검토 회귀 2개에서 마스킹된 제목의 비밀값이 점수에 주는 영향과 잘못된 CLI 인자의 원문 노출을 수정 전 재현하고 보완했다. 제목 점수도 마스킹 후 계산하며 argparse 입력 오류도 고정 JSON·종료 2로 처리한다. CI에 출처 재계산 self-check와 네트워크 없는 읽기 전용 이미지 검색 검사도 추가했다.

원래 런북 세 개·등록부와 기존 조회, 승인·잠금·원복, 큐/worker/작업, Slack 코드·의존성은 변경하지 않았다. 기존 CI에는 위 로컬 검색 검사를 추가했다. 실제 Slack·AWS·유료 모델 호출, 자격 발급, 감시·실습·장애·복구 실행, Terraform apply/destroy는 없다. C04/C05 최종 완료·A5/A7 미측정·실행 저장소 전환·AI 포트폴리오 보류 상태도 그대로다.

이번 검토/CI 반영 이후에도 검색은 로컬 API/CLI로만 제공한다. 검색 결과를 모델 근거나 Slack 요청/보고서에 넣는 작업은 대상별 허용 범위·인용 계약을 별도 검토한 뒤 진행하며 자동으로 연결하지 않는다. 다음 추천은 **GPT-6 SOL · High**다.
