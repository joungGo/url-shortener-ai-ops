# WF-07 코드 검토 · 2026-11-19

## 검토 범위

기준 main59c33a9부터 `codex/wf07-image-pr-20261119`의 미커밋 WF07 변경·문서와 후속 수정까지 검토했다. `review-bugbot` 스킬의 전용 bugbot 부재 시 직접 검토 경로를 적용했다. 기존 proof/PR/승인/큐/merge/sync 호출 계약을 대조했으며 실제 외부 읽기·쓰기 어댑터를 켜지 않았다.

## 발견과 수정

| Severity | Location (file:line) | Finding |
| --- | --- | --- |
| P2 | recovery_pr.py:305 | 마지막 원본 확인 뒤 WF06 장부만 취소되면 준비 완료가 반환됨. 같은 DB 트랜잭션에서 승인 상태를 다시 확인하도록 수정. |
| P2 | recovery_pr.py:324 | 최종 확인 사이 원본 보관 종료를 통과할 수 있음. 공통 승인 확인에 원본·PR 장부의 보관 기한 재검사를 추가. |
| P2 | recovery_pr.py:310 | 마지막 자료 조회/DB 대기가 길어지면 오래된 관측으로 준비 완료 반환. 실제 마지막 관측을 보존해 반환 직전 기존 신선도/기한 검사를 적용. |

집중31개(43.073초)·전체1471개(134.944초/skip0), AST2개·저장소 경계·diff·기존 C단계75개 동일성 검사를 통과했다.

세 회귀 시험을 수정 전에 실행해 3개 모두 실패(ValueError 미발생)를 확인했다. 수정 후 집중 시험과 최종 전체 회귀 결과는 [검증 JSON](recovery-pr-review-validation-20261119.json)을 따른다. 추가 네트워크 재조회 없이 이미 확보한 마지막 관측으로 검사하며 기존 MAX_LIVE_AGE15초·MAX_PREFLIGHT_DURATION180초를 가져와 사용한다. C04의 시작15초 등 실습 시간 제한을 바꾸는 의미가 아니다.

새 장부 연결·정확 diff·정상 이미지·재시작/응답 유실·중복·정확 head 리뷰/CI·작성/병합 신원 분리·취소/만료·잠금/동기화까지 로컬 fixture로 검증했다. 본 검토의 지적사항은 수정됐으며 실제 연결이 완료됐다는 뜻은 아니다.

## 반영과 한계

이번 소스/테스트·문서, 이전 WF06 완료 후 메타데이터만 commit/PR 대상으로 삼는다. 기존 C단계75개 파일과 실행 조건은 유지한다. 실제 복구 PR·토큰·Slack 전송·배포·장애·Terraform·모델을 실행하지 않는다. CI의 이미지 빌드는 publish가 아니며 외부 실행 workflow는 저장소 경계로 비활성화돼 있다.

일반 PR CI 통과 뒤 정확한 head로 squash 병합하고 main CI·tree를 확인한다. 이 문서 작성 시점 원격 CI/main은 대기이며 완료 뒤 JSON/HANDOFF의 최신 결과로 확정한다. 보호 변경·관리자 우회는 요청하지 않는다.

Slack 인증 승인 endpoint/결과 outbox·실제 쓰기/잠금/회수·기존 draft 감사 DB 보관 정리·WF08/09 및 C04/C05 실환경 완료는 여전히 후속이다. 추천 **GPT-6 SOL · High**.

## 최종 반영 결과

[PR #35](https://github.com/joungGo/url-shortener-ai-ops/pull/35) head `bba9c335c4589bb72903de1dea0986e8d6e5db96`를 main `b49672e95536dbc56f11a39626e300af2071d7da`로 일반 squash 병합했다. PR CI44454087061 7종과 main CI44454506379 성공, main PR전용 gate는 예상 skip. 검토/병합 tree·소스 hash 일치. 보호 변경·관리자 우회 없음. 완료 메타데이터는 로컬 후속 기록이며 실행 활성화가 아니다.
