# 조사·결과 경로의 대상별 런북 인용 · 로컬 구현 2026-11-16

AI main `a5d3b7484e7039439741aa71ee94aaf0f37dd71a`의 런북 검색을 기존 조사·결과 흐름에 연결했다. 로컬 구현 뒤 코드 검토를 진행했으며, 최종 CI·반영 결과는 runbook-context-integration-20261116 문서/JSON과 HANDOFF에서 별도로 기록한다. 원래 AI 저장소의 기존 자료를 보존하며 실환경 배포는 수행하지 않는다. 이번 연결은 규칙 조사와 로컬 결과/가짜 Slack 회신까지이며 모델·실제 Slack·실습을 연결하지 않는다.

## 연결 흐름

1. 운영자가 신뢰 설정으로 `runbook-policy`를 읽는다. 정책은 등록된 대상의 alias와 전체 Target(`cluster_id`, `namespace`, `workload`, `service`, `environment`), 허용 문서 ID, 등록부/검색 메타데이터 해시를 고정한다. 요청자는 정책 경로·문서 ID·서비스 필터·권한을 지정할 수 없다. 정책에서 빠진 대상과 빈 허용 목록은 기본 거부다.
2. `load_b_settings(..., runbook_policy=...)`는 기존 설정 해시에 정책 해시를 결합한다. 동일 설정을 접수기와 기존 worker에 전달한다. 정책을 바꾼 설정으로 이미 접수한 작업을 처리하면 기존 `config_changed` 보류가 작동한다. 정책을 사용하지 않는 기존 설정의 해시·보고서·결과 형식은 유지한다.
3. 기존 `JobQueue`와 `Worker`가 기존 수집·규칙 분석을 수행한다. `IncidentStore`가 보고서를 검증한 뒤, 허용 문서의 등록 키워드 중 정제 질문에 일치한 용어를 우선 선택하고 없으면 검증된 보고서 사실 중 앞 10개에서 찾는다. 임의 자연어 전체를 명령이나 필터로 해석하지 않는다. 문서별 최대 1개, 전체 최대 3개를 기존 `RunbookSearch`로 찾는다. 문서는 ID 순서로 선택하고 용어는 긴 등록 키워드부터 결정적으로 선택한다. 의미 검색이나 모델 호출은 없다.
4. 원래 관측 보고서와 별도로 `runbook-context.json`을 0600·원자적 쓰기로 저장한다. 사건/실행 ID, Target, 질문·bundle·보고서·설정·정책 해시에 결합하며 context 해시는 기존 metadata에 보존한다. 보고서 Markdown에는 별도 **런북 참고 자료** 절을 추가한다. 원래 사실·가설·진단·분석 상태·실행 metadata와 관측 `evidence_ids`에는 런북을 넣지 않는다.
5. 기존 `TaskService.result`·Unix 소켓의 result와 로컬 mailbox에 검증한 `runbook_context`를 제공한다. 조회 시 현재 정책·등록부·메타데이터·문서와 결정적 검색을 다시 대조한다. 저장된 인용을 다른 버전이나 새 검색 결과로 교체하지 않는다. 현재 정책이 없거나 철회/변경/삭제됐거나 문서가 바뀌면 `withheld`와 빈 인용을 반환한다. 기존 관측 요약은 유지하며 분석을 재실행하지 않는다.
6. 합성 서명과 `local_fixture` transport만 지원하는 기존 Slack 어댑터도 같은 결과를 읽는다. 원래 허용 channel/thread에 문서 ID·버전·줄 범위·문서 SHA256을 간결하게 표시하며 제목·발췌문·본문은 복사하지 않는다. 회신 직전 결과를 다시 대조해 정책 철회를 확인한다. 실제 transport·자격·공개 endpoint는 추가하지 않았다.

기존 완료 보고서에 인용을 뒤늦게 붙이지 않는다. 정책이 활성인 새 요청만 인용을 기록한다. 다른 설정으로 같은 request_id를 재사용하면 기존 중복 계약을 따라 충돌/보류되므로 새 설정의 새 요청 ID가 필요하다. 기존 큐 작업 조회는 결과를 제공하되 회신 목적지는 추정하지 않는다.

## 출처와 상태

인용에는 `citation_id`, 문서 ID·버전·원문 SHA256·등록 경로, 등록부/메타데이터 SHA256, 절 ID·제목 계층·1부터 시작하는 양끝 포함 줄 범위·원문 절 SHA256, 마스킹 발췌의 줄 범위, 검색 용어와 본문/메타데이터 일치 구분을 포함한다. context는 관측 증거가 아닌 `reference_only_not_observed_evidence`이며, 개별 인용은 `untrusted_reference_data`다. Markdown은 인용 문자의 링크·태그·서식을 이스케이프한다. 인용 식별자는 문서/버전/해시/절에 결합되며 관측 증거 ID로 사용할 수 없다.

| 상태 | 의미 |
| --- | --- |
| available | 해당 조사에서 허용된 문서와 일치해 저장한 인용이 있음 |
| unavailable | 허용 문서가 있지만 질문/사실에 맞는 등록 키워드 또는 결과가 없음 |
| not_allowed | 대상별 허용 문서가 없음 |
| rejected | 조회 중 정책/등록부/문서/출력 제약 등을 만족하지 못해 인용 없이 기록함 |
| withheld | 결과 조회 시 현재 정책·설정·출처와 저장된 인용이 달라 인용을 제공하지 않음 |

저장 파일의 context 해시 또는 사건/실행/bundle/보고서 결합이 깨지면 기존 결과 읽기를 무결성 오류로 거절한다. 단순히 현재 참고 문서가 없어지는 경우와 구분한다. 저장 파일에서 인용을 고치고 metadata 해시를 다시 계산해도 현재 정책·검색 재대조에서 일치하지 않으면 인용이 보류된다. 영속 context는 최대 16KiB·인용 3개, 각 발췌는 기존 검색의 최대 5줄/400자다. sidecar는 링크·비정규/과대 파일·중복 JSON 키를 거절한다. 원래 7일 보관 정리의 소유 파일 목록에 sidecar만 추가해 함께 정리한다.

현재 등록 문서 세 개는 모두 버전1·2026-09-27 정적 검토·`live_verified=false`다. 검색으로 최신 코드·실환경 적합성을 검증하지 않는다. `current_source_code_checked=false`, `current_runtime_verified=false`, `execution_allowed=false`, `external_model_transfer=false`를 유지한다. 해시는 로컬 사본 일관성 근거이며 작성자 서명이나 현재 환경 적합성 증명이 아니다. 관측 사실의 원인·복구·승인은 런북과 별도로 검증해야 한다.

## 로컬 사용과 설정

외부 서비스 없이 전체 연결을 확인하는 명령은 다음과 같다. 임시 폴더에 합성 snapshot·정책·큐·Unix socket을 만들고 종료 시 회수한다.

```sh
# /Users/jounghyeon/dev/url-shortener-ai-ops 에서
PYTHONPATH=ai-ops/src /Users/jounghyeon/.local/share/url-shortener-ai-ops/continuation-20261112/local-venv/bin/python ai-ops/scripts/check_runbook_context_local.py
```

정책 예시는 `ai-ops/config/runbook-policy.local.example.json`이다. 기존 `targets.practice.example.json`의 합성 대상만 허용하며 실제 `ai-ops-lab/lab-api`에 자동 적용할 수 없다. 정책 변경 시 현재 등록부/메타데이터의 정확한 해시와 전체 Target을 검토해야 한다. 실제 운영 설정·Helm 입력·환경 변수는 수정하지 않았다.

로컬 API는 `load_b_settings(bindings_path, runtime_path, metric_path, runbook_policy=policy_path)`로 활성화한다. 기존 `collect-investigate`, `queue-submit`, `queue-work`와 로컬 TaskService의 `serve`에 선택적 `--runbook-policy` 파일 인자를 추가했다. 접수기와 worker가 같은 정책을 사용해야 한다. 경로는 기존 CLI 파일 접근 정책으로 검증하며 요청 payload의 인자가 아니다. 실제 수집 명령은 별도의 연결/인증이 필요한 기존 경로이므로 이번 검증에서는 fixture collector만 사용했다.

정책 활성 인스턴스는 읽을 때 파일·카탈로그를 다시 확인한다. 현재 파일이 바뀌면 같은 프로세스에서도 이전 인용을 숨긴다. 이미 저장된 과거 보고서/로컬 mailbox의 사본을 소급 삭제하는 기능은 아니며, 개인 저장소의 역사 기록은 기존 보관 정리에 따른다. `IncidentStore.show`의 과거 기록은 OS 운영자용이며 다중 사용자 공개 API가 아니다. 현재 정책을 적용한 결과는 TaskService 창구에서 조회한다. 기존 직접 `get_runbook`/검색 CLI의 로컬 운영자 기능과 모델 도구 허용 목록은 변경하지 않았다.

## 검증과 유지한 조건

새 연결 시험33개와 전체1,061개/skip0을 통과했다. 두 등록 대상에 cache/database를 각각 허용한 실제 fixture 조사, 문서 범위 우회·변조·복사 거절, 정책 변경의 큐 보류, 동일 프로세스 철회/삭제, 저장 context 재시작, Unix 요청→큐→worker→결과→로컬 mailbox, 합성 서명→기존 큐→가짜 Slack 인용 회신, 회신 직전 철회, 중복/취소/만료·7일 sidecar 정리를 검증했다. 검토에서 전송 본문/알림 envelope 준비 뒤 및 기존 메시지/파일 조회 중 정책이 철회되는 경계4개를 수정 전 실패로 재현했다. Slack 전송·mailbox 게시 직전과 조회 조정 후 현재 인용을 재대조해, 변경되면 원래 본문을 보내거나 delivered로 확정하지 않고 needs_review로 둔다. 자동 재전송·재조사는 하지 않는다. 시험 임시 경로도 macOS/Linux 공통 /tmp로 바꾸고 CI에 전체 연결 self-check를 추가했다. 별도 self-check는 외부 네트워크·프로세스·모델 키 조회를 차단한 채 통과했고 임시 소켓을 회수했다. fixture4종·schema14개 변경0·AST308파일·저장소 경계도 통과했다. 기존 SQLite ResourceWarning1건은 남는다.

기존 승인·잠금·원복9파일과 JobQueue/Worker, 규칙·보고서·관측 계약, 원래 런북/검색/도구 허용 목록은 바뀌지 않았다. **시작15초·매 실행 새 readiness 승인·정확 head의 실제 GitHub 사람 리뷰/CI·안내 후 최소120초/시작 후210초 리뷰 마감·소유 원복240초/독립 원복 시작300초·회복 관찰120초**와 UID/잠금/양쪽 ACK/임시 자격 회수를 유지한다. 로컬 시험 통과는 실환경 15초 예산 성공의 증명이 아니다.

실제 Slack/GitHub/AWS/모델 호출·자격 발급·실환경 배포·감시·실습·장애/복구·Terraform apply/destroy는 없다. 기존 operator/App 키/로그인/PVC/인프라는 보존했다. C04/C05 최종·A5/A7 미측정·실행 저장소 전환·AI 포트폴리오 보류도 그대로다.

검토·CI 반영 뒤에도 실제 연결은 별도이며, 최종 반영 기록은 같은 폴더의 integration 문서를 따른다. 실제 Slack 연결이나 새 실습은 자동으로 진행하지 않는다. 다음 추천은 **GPT-6 SOL · High**다.
