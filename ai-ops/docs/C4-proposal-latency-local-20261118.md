# C-04 복구안 재검증 지연 · 로컬 개선

## 확인한 범위

202611180005의 `fresh_c02_proposal_recheck`부터 PR 생성 직전까지 50.740117초가 걸렸다. 이 구간은 세 번의 원본 수집, 근거 비교·초안 저장, Git/Helm 기반 PR 연결 검증을 모두 포함한다. 시작 당시 최소 120초 리뷰를 보장하는 남은 시간은 50.277026초였으므로 약 0.463초 초과했다. 당시 단계별 timing context가 활성화되지 않았기 때문에 50.740초 중 각 외부 서비스의 실제 비중은 확정할 수 없다.

검증된 AI main `651cdc6016af1e7b7dcc507aa0804591927bd5e9`의 전용 worktree에서 작업했다. 기존 미커밋 파일·실행 묶음·현재 클러스터는 변경하지 않았다.

## 수정

1. `prepare_c4_recovery_pr.py`의 첫째/둘째/셋째 수집, 근거 판단, 초안 저장, PR 연결 및 Helm 렌더를 계측한다. `TrialSource`의 전후 snapshot, CI/계정/클러스터·ECR·Pod·ReplicaSet·사건 조회도 같은 context로 연결한다. 성공·거절·취소 모두 `proposal-timing.json`에 고정 단계명·시작/소요시간·남은 예산·성공 여부만 남긴다. 응답 본문·자격·URL·argv·예외 원문은 기록하지 않는다. 기존 최대 256개 기록/초과 개수 제한을 유지한다.
2. 같은 최초 리소스와 values를 입력으로 하는 CI/클러스터 신원 조회와 ECR/Pod/ReplicaSet 조회를 병렬 실행한다. 양쪽 완료와 검증 후에만 근거를 판정한다. 세 번의 신선한 수집, 수집 내 마지막 원본 조회, 수집 전후 UID/상태/Lease 비교는 유지한다. 변할 수 있는 CI 메타데이터·ECR·Pod·Lease는 캐시하지 않는다.
3. PR 연결 단계는 앞서 검증한 full-SHA Git 객체만 복사해 재사용한다. 새 reader의 기존 45초 상한과 개별 GET 8초 상한을 유지하며, main은 연결 전후 새로 읽는다. 차트 내용·렌더 비교와 정확한 base 조건을 생략하지 않는다.
4. 재검증 읽기는 기존 `review_cutoff - minimum_review_seconds`까지만 허용한다. UTC/monotonic 중 먼저 도달하는 기한을 적용하고 GitHub GET/Helm 프로세스도 남은 시간으로 제한한다. 만료 후 다음 수집·작성 단계로 넘어가지 않는다. scope 종료 때 context를 복원해 자격 회수 경로를 막지 않는다. 기존 작성 전 리뷰창 검사도 그대로다.

## 로컬 재현 결과

- 합성 지연 100ms를 CI·이미지·ReplicaSet 읽기에 적용하고 동일한 세 번의 수집을 비교: 기존 순차 대기 946.588ms, 개선 병렬 대기 633.685ms. 각 history/EKS/STS/정상·실패 이미지/Pod/ReplicaSet 3회, Application/Deployment/Lease 각 6회로 동일하다.
- 로컬 Git 저장소·실제 Helm과 GET당 합성 지연 10ms로 PR 연결 비교: 외부 reader 호출 14→8회, 519.505→331.092ms. main 재조회는 2회 유지하고 PR 연결 결과는 같다.
- 기록된 시작/마감/50.740초를 가상 시계에 적용해 리뷰창 침범 거절을 재현했다. 실제 로컬 지연 프로세스에도 잔여 예산을 적용한다.
- 병렬 겹침은 Barrier로 검증한다. 실행 시간 차이는 진단 수치이며 CI의 속도 통과 기준으로 삼지 않는다.
- 잘못된 계정·CI 이력·ECR 실패, 중간 Application/Deployment/Lease 변경, main 변경, 오래된 reader, 벽시계 역행, 취소, 실제 작성 블록 이전 실패 계측을 검증한다.

수치는 합성 로컬 지연 조건의 비교다. **실환경 50.740초가 특정 수치로 줄었다는 뜻이 아니며 C-04/C-05 완료 증거가 아니다.** 실행 전 증거의 신선도·실제 네트워크 변동에 따른 거절은 계속 필요하다.

재현 명령(프로젝트 의존성이 설치된 Python 사용):

```sh
PYTHONPATH=ai-ops/src python ai-ops/scripts/check_c4_proposal_latency_local.py
PYTHONPATH=ai-ops/src python -m unittest discover -s ai-ops/tests -p 'test_*.py' -q
```

상세 단계별 합성 측정은 `C4-proposal-latency-local-20261118.json`에 있다. 실제 사건·토큰·원본 응답은 포함하지 않는다.

검증 완료: **전체 로컬 시험 1,276개 통과(신규 회귀 9개 포함)**, compileall·diff 검사 통과. 원격 CI는 실행하지 않았다. 소스 해시는 함께 보관한 JSON에 기록했다.

## 유지 조건과 다음 단계

조회3초·시작15초·안내 후 최소120초/시작 후210초 리뷰 마감·소유 원복240초·독립 원복300초·관찰120초, 실행별 사용자 승인·정확 head 사람 리뷰/CI·UID/CAS/Lease/ACK·회수 조건을 유지했다. 이번에는 실제 연결·배포·Slack·Terraform·원격 CI·장애 실습을 수행하지 않았다.

다음은 수정 코드 검토·원격 CI·main 반영이다. 이후 장애 없는 준비 경로 검증으로 실행 연결을 확인하고, 새로운 실습은 별도 현재 준비 결과의 승인 후에만 진행한다. 원인을 확인하지 않은 상태에서 동일 실습을 자동 반복하지 않는다. 추천 **GPT-6 SOL · High**.
