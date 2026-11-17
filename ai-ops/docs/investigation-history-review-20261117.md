# 일반 조사 CI 이력 연결 검토

2026-11-17. AI main ce96d46 기반의 변경이다.

일반 요청·Slack 조사에 기존 비공개 큐의 검증된 CI 실패/원복 입력을 읽기 전용으로 연결한다. source/target/config hash·HMAC를 재검증하고 collector의 현재 UID·이미지·revision·원래 관측 시각 검사를 유지한다. 원래 질문·중복 키를 변경하거나 과거 작업을 재실행하지 않는다. 새로운 외부 조회나 자격은 없다.

신규 조사 후보는 최근 접수20개에서 원본 artifact 시각으로 선택한다. 결과 조회와 worker 재개는 snapshot의 정확한 run/attempt에 대응하는 기존 unique episode key를 조회한 뒤 artifact ID/hash·단계를 다시 대조한다. purge/변조/잘못된 현재 대상은 거절하며 stale은 stale이다.

## 검토에서 고친 문제

같은 run의 새 attempt가 들어오면 과거 결과 조회가 ambiguous 또는 출처 불일치로 거절됐다. 이를 합성 rerun으로 재현한 뒤 run뿐 아니라 attempt를 고정했다. 정확한 결과 출처를 최신20개 후보 목록과 분리해, 새 CI가21개 추가돼도 과거 결과를 원래 출처로 재검증한다. 다른 attempt를 대신 쓰지 않는다. readonly SQLite 연결을 명시적으로 닫으며 worker의 결과 저장 트랜잭션과 쓰기 잠금 충돌이 없다.

신규12개 회귀: 정상 연결/원래 질문/출처, 누락·진행중, target/config mismatch, HMAC 변조, 재개, UID/source mismatch, stale, purge, 결과 재검증, rerun 및 최신 후보 범위 이탈. 독립 체크아웃 전체1,218개(54.614초)와 요청/Slack 대역/런북 통합3종이 통과했다. 기존 미반영 Lease WIP는 포함하지 않았다.

## 경계와 남은 검증

기존 플랫폼 lab CI/source import 계약을 유지한다. 별도 c03-execution-lab 저장소를 자동 허용하지 않는다. 재설치 후 새 큐·현재 UID/binding에 맞는 새 검증 입력이 필요하고, 백업 큐를 재실행하지 않는다. 정상 CI 영수증은 실패/원복 자료가 아니다. 실제 partial 해소·C04/C05 완료를 주장하지 않는다.

15초 시작 제한·안내 후 최소120초/시작 후210초 리뷰 마감·소유 원복240초/독립 원복 시작300초·회복 관찰120초·실행별 승인·정확한 head 사람 리뷰·자격 회수는 변경하지 않았다. 실제 Slack 연결·배포·장애 실습·유료 모델 호출은 실행하지 않는다.
