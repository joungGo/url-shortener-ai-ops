# C-04 리소스 비교 원본 형식 수정 · 로컬 검증

## 2026-11-16 리소스 비교 실패 로컬 재현·수정 완료 · 미반영

사용자 지시에 따라 원인 해결 전 장애 실습 반복을 중지했다. **클라우드·모델·새 실습·Terraform 호출 없이**, 보존된 003 runtime의 `c_trial_source.py:52` 거절을 변경 없는 인메모리 대상에서 재현했다. 원인은 **Lease proxy가 보존하는 `metadata.managedFields`를 기존 kubectl JSON 기본 출력이 숨겨 같은 리소스도 서로 다르게 보이는 조회 형식 불일치**다. 실제 kubectl v1.34.1·로컬 TLS API에서도 기본 출력의 누락과 proxy 원본을 재현했다. 당시 003의 전후 원본이 저장되지 않아 다른 차이가 함께 있었는지는 소급 확정하지 않는다.

AI main `2c2c59d` 기준 별도 작업사본 **`/private/tmp/c04-proof-consistency-20261116`**, branch `codex/c04-proof-consistency-20261116`에서 수정했다. trial transport와 audit GET에 `--show-managed-fields=true`를 명시해 전체 원본을 보존한다. 비교에서 필드를 삭제하지 않았고 `same_application_observation`·`same_recovery_resources` 함수 AST는 기존과 동일하다. 향후 거절 시 단계·고정 필드 경로만 최대32개/예외체인8개 남기며 값·임의 key·응답 본문은 기록하지 않는다. 실행기/준비/PR 준비의 결과에 연결했다.

**전체912개 시험·새6개 회귀·skip0**, 실제 kubectl/로컬 TLS의 형식 일치·CAS/양쪽ACK·잘못된CA/경로/메서드/Host 거절·인증 재사용/갱신·SIGTERM 회수, fixture4종·schema변경0·AST293파일·소유권/Helm 배포 정의 검사를 통과했다. 배포 정적 검사는 로컬 고정 플랫폼9573 chart를 준비한 뒤 통과했다. 기존 SQLite ResourceWarning1건은 남았다. 실제 managedFields·UID/resourceVersion/holder/source 변경은 계속 거절한다.

15초 시작·실제 안내 뒤 최소120초·시작 뒤210초 리뷰 마감·240초 소유 원복·300초 독립 원복 시작·120초 관찰과 실행별 승인·정확head 실제 사람 리뷰/CI·잠금/원복 조건을 유지했다. **코드는 미커밋·미push·미병합·미배포이며 실습은 재실행하지 않았다.** 기존 운영/AI main·guard와 자격·인프라는 변경하지 않았다.

개인 보존 **`/Users/jounghyeon/.local/share/url-shortener-ai-ops/c04-proof-comparison-local-20261116`**의 `validation.json`·`archived-reproduction.json`·전후 로컬TLS 로그·전체시험 로그·`source/` 12파일/해시·tracked patch를 따른다. 재현 회귀 시험 파일도 source/에 포함해 작업사본이 없어져도 보존한다. 기존 003 종료 근거는 이전 폴더 그대로다.

다음은 코드 검토·필수 CI·정상 반영이다. 이후 현재 Pod UID binding·새 정상 CI부터 갱신하고 새 감시/새 readiness를 준비한다. 새 실습은 새 준비 결과의 실행 승인 후에만 가능하며 003 승인·PR16 리뷰를 재사용하지 않는다. C04/C05 최종·A5/A7·전체 실행 저장소 전환은 미완료이고 포트폴리오 보류를 유지한다. 다음 추천 **GPT-6 SOL · High**.
