# partial 처리 개선 코드 검토 · 2026-11-18

## 범위

사용자의 코드 검토·CI 통과 후 main 반영 요청에 따라 `bc398ffb5513393ef031670ffdfbc5b95ed63630` 기준 변경을 검토했다. `review-bugbot` 스킬의 전용 Bugbot subagent가 제공되지 않아 같은 세션에서 직접 diff와 호출 경로를 검토했다. 별도 사람의 GitHub 리뷰로 표시하지 않는다.

## 발견 및 보완

| Severity | Location (file:line) | Finding |
|---|---|---|
| P2 · 수정 완료 | ai-ops/src/ai_ops/evidence_coverage.py:103 | 로그 원본이 truncated이고 부분 표본도 payload_too_large인 경우 동일 출처를 2개로 집계했다. 실제 collector 응답과 payload 상한으로 재현한 후 원본/표본을 한 출처로 묶었다. 현재·이전 컨테이너는 서로 다른 출처로 유지한다. |

검토 범위는 이벤트 전체 UID 조회의 성공/실패 구분, 빈 관측 표시 재검증, 구형 기록/기존 판독기 호환, 로그 표본 수·시각·상한, 과거 배포/CI 이력의 상태 및 시각, Slack 회신 길이·정제, 승인/원복/수집 기한 불변이다. 나머지 변경에서 추가 수정이 필요한 기능 오류는 찾지 못했다.

원본/표본의 중복 집계 회귀 시험을 추가했다. 최종 로컬 전체 결과와 파일 해시는 `partial-treatment-local-20261118.json`에 기록했다. CI는 이 검토 뒤 생성하는 PR의 정확한 head에서 확인한다.

## 반영 경계

- 원격 main을 읽어 작업 기준과 일치함을 확인했다.
- 저장소 `joungGo/url-shortener-ai-ops`의 main 조회 결과 protected=false다. 보호 API는 현재 비공개 저장소 요금제 제한을 반환한다. 보호 설정을 변경하거나 관리자 우회 명령을 사용하지 않는다.
- 검토한 head의 contracts/image/terraform/Lease trace 및 PR gate 성공 뒤 일반 squash 병합한다.
- 워크플로의 Terraform은 backend=false 구성 검증이며 apply/destroy가 아니다. 이미지 검사는 CI 내 빌드/격리 실행으로 실제 ECR 등록·클러스터 배포를 포함하지 않는다.
- 기존 시작/리뷰/원복/관찰 기한, UID/CAS/ACK, 정확 head 사람 리뷰, 임시 자격 회수 조건은 유지한다. 실제 Slack 전송·장애 실습·유료 모델 호출은 없다.

다음 작업 추천: GPT-6 SOL · High.
