# WF-08 명령 경로 코드 검토 · 2026-11-19

## 범위

기준 main b49672e부터 WF08 작업트리의 소스·시험·계획·작업표 변경을 검토했다. `review-bugbot` 스킬의 전용 bugbot 부재 시 직접 검토 절차를 적용했다. 기존 사건/Slack 요청 인가·JobQueue·WF06 장부와 기존 C단계 제한을 대조했다. 실제 배포·Slack·실습은 수행하지 않는다.

## 발견과 수정

| Severity | Location (file:line) | Finding |
| --- | --- | --- |
| P2 | recovery_commands.py:401 | 응답 유실 후 읽기 조정에서 WF06의 영속 취소를 누락해 applied_unverified로 표시함. 같은 DB의 명령 승인 취소를 읽어 적용 사실과 취소를 함께 보존하도록 수정. |
| P2 | recovery_commands.py:420 | 늦게 실패한 읽기 조정이 다른 조회의 완료/취소 상태를 needs_review로 덮어씀. 읽기 시작 세대와 현재 미확정 상태가 같은 경우에만 실패 기록하도록 수정. |

회귀4개 중3개가 수정 전 실패했고, 기존 취소 경쟁 경계1개는 이미 통과했다. 수정 후 집중34개(12.792초), 전체1505개(147.607초/skip0), AST3개·diff·저장소 경계 통과. 소스/로그 hash와 C단계75개 동일성은 [검증 JSON](recovery-command-review-validation-20261119.json)에 있다. 공유 Lease 구현도 변경하지 않았다.

이 수정은 쓰기 재시도나 승인 만료 연장이 아니다. 이미 발생한 변경을 읽어 확인할 때 취소 사실을 누락하지 않도록 한다. 실행 전에는 기존 승인자·정책·현재 대상·근거·잠금·원래 기한을 계속 검사한다. 적용 관측은 서비스 회복이 아니며 WF09 검증이 필요하다. 추가 검토에서 미해결 오류는 발견하지 않았다.

## 반영 범위와 한계

이번 WF08 소스·시험·문서와 이전 WF07 완료 후 메타데이터만 커밋한다. 정확한 PR head의 CI 통과 후 일반 squash 병합하고 main CI와 tree/해시를 확인한다. 이 문서 작성 시점은 CI/main 대기이며 최종 JSON/HANDOFF가 완료 결과를 기록한다. 보호 변경/관리자 우회는 하지 않는다.

자동 CI는 로컬 시험·정적 Terraform validate·통신 없는 이미지 검사다. 실제 배포/감시 workflow는 수동/저장소 경계로 분리돼 있으며 이번에 기동하지 않는다. 실제 복구 PR·Slack 전송·인프라·Terraform apply/destroy·장애·유료 모델도 실행하지 않는다.

기존3/15/120/210/240/300/120초·실행별승인·정확head 사람리뷰/CI·잠금/ACK·원복/회수 유지. 실행기는 local_fixture뿐이며 실제 인증된 Slack 승인/결과 outbox·진단/쓰기/잠금 어댑터·RBAC/자격·주기 보관 정리와 WF09 효과검증, C04/C05 실환경 완료는 후속이다. 다음 추천 **GPT-6 SOL · High**.

## 최종 반영 결과

[PR #36](https://github.com/joungGo/url-shortener-ai-ops/pull/36) head `b571d34fcb41ef37a0e404acd46964a1b6cfb684`를 main `6891144c6c31e29a856d701191c9eb177764204a`에 일반 squash 병합했다. [PR CI44457885630](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44457885630) 7종과 [main CI44458405951](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44458405951) 성공. main의 PR전용 gate skip은 설계대로이며 실제 PR gate는 통과했다. 검토/병합 전체 tree·소스 hash 일치, 보호 변경/관리자 우회 없음. 이 완료 메타데이터는 로컬 후속 기록이며 실행 활성화가 아니다.
