# B-06 S3 이미지 실패 규칙 v3와 동일 저장 증거 비교

상태: **PR #50 병합·승인 이미지 등록·운영/실습 v3 배포·기존 보고서 보존·새 운영 보고서와 실제 S3 하위 사례 검증 완료**. [배포 검증](assets/B6-rules-v3-deployment-validation.json)과 [동일 입력 후보 비교](assets/B6-image-rules-v3-comparison.json)를 구분한다. 최초 S3 v2 needs_review는 원래 기록에 유지하며 B-06 전체·B 완료·C 진입은 보류한다.

## 작업 목적과 흐름

실제 S3에서는 Pod가 ErrImagePull·Ready 0이었고 Kubernetes 이벤트 reason은 Failed였다. 이벤트의 정제된 message에 이미지 실패 표식이 있었지만 기존 v2는 reason=ImagePullBackOff와 Argo progressing의 조합을 기대했다. 실습에는 Argo Application이 없으므로 배포 상태는 unknown이었고 원인 후보를 만들지 못했다.

이번 작업은 **수집한 진짜 이벤트 형태를 해석하면서, 다른 Pod·과거 장애·무관한 Failed를 현재 이미지 장애로 섞지 않는 규칙을 추가하는 것**이다. 기존 수집 필드·마스킹·로그 원문 비보존·외부 모델 차단은 유지한다.

v2 보고서 계산 → 새 v3 상관 조건 확인 → 세 근거로 관측 사실과 제한된 원인 후보 추가 → 보고서 계약 검사 → 기존 v1/v2 규칙으로 저장 보고서 재조회 → 입력·보고서·수집 감사 해시 비교 순서다.

## v3가 추가한 판단 조건

다음 조건을 모두 만족하는 available captured 자료만 새 상관 판단에 사용한다.

1. Pod가 Pending·Ready false이고, 감사 기록에 Pod UID/resourceVersion·Deployment UID·ReplicaSet UID/resourceVersion이 있다.
2. 배포 자료의 실제 UID가 해당 Deployment UID이고 이미지가 Pod 이미지와 같다. 배포·Pod 관측 시각 차이는 120초 이내다.
3. 이벤트가 Warning이고 reason은 Failed·BackOff·ImagePullBackOff·ErrImagePull 중 하나다. Failed/BackOff는 별도의 유한한 ImagePullBackOff/ErrImagePull 표식도 필요하다. 표식의 부분 문자열만 맞는 경우는 제외한다.
4. 이벤트의 involved_uid가 해당 Pod UID와 같다. 이벤트는 Pod 관측보다 늦지 않고 120초 이내다. 다른 Pod·다른 Deployment·다른 이미지·오래된 이벤트는 제외한다.

120초는 이번 보수적 상관 규칙의 범위이며 Kubernetes가 보장하는 일반 장애 시간 기준이 아니다. 이보다 오래된 증거만 있으면 v3는 새 이미지 후보를 추가하지 않는다. 수집 감사가 없거나 근거가 missing/invalid/stale이면 해당 근거를 사용하지 않는다. 수집 단계에서 검증한 소유 관계를 감사 UID와 대조하며 이벤트 자유 문장 전체를 다시 읽지 않는다.

새 사실과 가설은 **Pod 상태·이미지 실패 이벤트·같은 이미지의 Deployment 자료** 세 ID를 함께 인용한다. 원인 후보는 “이미지를 가져오지 못해 컨테이너가 시작되지 못했을 가능성”이다. 태그 부재·레지스트리 인증·네트워크 원인은 따로 확인하도록 제안한다. 전체 서비스 장애, 정확한 하위 원인, 복구 완료는 확정하지 않고 current_state=unknown을 유지한다. 부분 수집 상태도 complete로 바꾸지 않는다. 기존 v2의 다른 규칙은 보존하며 이번 조건은 새 native 이벤트 상관 판단에 적용된다.

## 파일별 변경과 버전 호환

| 파일 | 변경 |
|---|---|
| `src/ai_ops/rules.py` | 기존 본문을 analyze_snapshot_rules_v2로 보존하고 새 기본 분석기를 rules-captured-v3로 추가했다. v1도 보존한다. |
| `src/ai_ops/incidents.py` | 저장 engine에 따라 v1/v2/v3를 선택한다. v3는 사실·가설·확인 제안·실행 제안·한계까지 동일 입력에서 다시 계산해 변조를 거부한다. v2의 기존 사실 검사도 유지한다. |
| `tests/test_captured_rules_v3.py` | 성공 형태와 다른 UID·이미지·시간·Ready·무관한 이벤트·누락 감사·v1/v2 호환·v3 내용 변조를 검사한다. 6개 메서드에 여러 하위 사례를 포함한다. |
| `tests/test_b_investigation.py` | 새 기본 버전 기대값을 v3로 바꾸고 v2 변조 시험은 v2 생성기로 명시해 유지했다. |
| `scripts/compare_b6_image_rules.py` | 실습 Pod의 저장 자료를 읽고 후보 소스를 별도 메모리 프로세스에서 계산한다. 본문·원시 로그를 반출하거나 기존 보고서를 덮어쓰지 않는다. |
| `src/ai_ops/b6_scenarios.py`, `scripts/probe_b6_lab.py` | 후속 실제 배포 때 v2/v3 두 engine의 확인이 가능하도록 검사 범위를 명시했다. 현재 배포를 바꾸지는 않는다. |

## 실제 동일 입력 비교

2026-10-21 16:21 KST, bundle `907058cd…`의 저장된 사건·실행을 실습 수집기 Pod 안에서 읽었다. 원래 v2 보고서의 계약·해시를 확인한 뒤 후보 rules.py를 별도 Python 프로세스 메모리에서만 실행했다. 조회 시점의 현재 상태를 조사한 것이 아니라, 16:06 KST에 고정한 과거 증거를 다시 계산한 결과다.

| 항목 | 저장 v2 | 후보 v3 |
|---|---|---|
| 사실 수 | 3 | 4 |
| 원인 후보 수 | 0 | 1 |
| 원인 판단 | insufficient_evidence | suspected_cause |
| 분석 상태 | partial | partial |
| 현재 상태 | unknown | unknown |

새 관측과 후보는 ev-pod-0-status·ev-event-0·ev-deployment-object를 인용한다. 모두 available 자료이고 후보 보고서는 보고서 계약을 통과했다. 원래 사건·실행 ID와 bundle hash는 그대로이며 실행기가 ECR 부재를 확인한 정답 기록을 후보 분석 입력에 넣지 않았다.

실습 저장소의 v2 보고서 6건은 보존한 v2 함수로 다시 계산했을 때 사실·가설·판단·확인 제안·한계가 같았다. 각 report.json/input.json/collection-audit.json의 총 18개 파일 해시가 비교 전후 동일했다. v1 저장·재조회 호환은 로컬 시험에서 확인했다. 현재 실습 저장소에 v1 보고서가 있었다고 주장하지 않는다. 외부 모델 호출·API 비용은 0이며 기존 captured 외부 전송 차단도 확인했다.

## 재현과 다음 단계

```bash
export AI_OPS_FILES_ROOT=/private/tmp/ai-b6-lab-20261021
export PYTHONPATH=ai-ops/src
python ai-ops/scripts/compare_b6_image_rules.py --incident inc-966b3ed541f34cd38e8516b8217f7357 --run run-12eb01ea00a641238e71c30e75db6772 --output "$AI_OPS_FILES_ROOT/s3-rules-v3-comparison-new.json"
```

출력은 새 파일이어야 한다. 실습 수집기의 emptyDir가 유지돼 원본 자료가 남아 있을 때만 재현 가능하다. Pod를 삭제해 자료가 없거나 조회 권한이 없으면 시험 실패이며, 샘플 입력으로 바꿔 성공이라 표시하지 않는다.

이 결과는 **후보 소스의 동일 저장 입력 비교**다. worker나 수집기 이미지를 배포하지 않았고 장애 재주입·새 v3 운영 보고서 저장도 하지 않았다. 최초 S3 결과를 성공으로 덮어쓰지 않았다. 다음은 변경을 커밋·원격 CI로 검증해 승인 이미지를 등록하고, 기존 v1/v2 재조회·새 v3 저장/조회와 필요 범위의 S3 실환경 검증을 수행하는 작업이다. GPT-6 SOL high를 추천한다. 후보 비교 통과만으로 S3 최종 품질·B-06 완료·C 진입을 승인하지 않는다.

## 원격 통합·사용자 승인과 실제 배포

2026-10-21 main을 기준으로 B-06 변경만 분리한 배포 브랜치를 만들었다. 로컬 전체 290개와 [원격 계약·이미지 검사](https://github.com/f-lab-edu/F-Lab-DevOps/actions/runs/39562744841)가 통과해 [PR #50](https://github.com/f-lab-edu/F-Lab-DevOps/pull/50)을 main `10a7ef68bfb338b5faa5ec75b94d8ea35c8ed917`에 병합했다. 기존 앱 CI나 GitOps 이미지는 변경하지 않았다.

[이미지 등록 실행 39562933098](https://github.com/f-lab-edu/F-Lab-DevOps/actions/runs/39562933098)은 사용자 직접 승인 후 성공했다. 승인 설정은 유지했다. artifact의 main 소스·ECR manifest/image ID·digest와 보안 검사 COMPLETE/지적 0건을 대조했다. 배포 digest는 `sha256:50a8c699bee3270f2bf9ad905820e32be590e653363ad73c764cb5ce8cbfb99e`다.

2026-10-21 16:40 KST, 기존 Helm 입력 중 이미지 digest만 교체한 revision 3을 배포했다. 같은 PVC `b0236f94-4395-4aaa-91ac-3b2bbe31a78d`를 유지했고 두 컨테이너 Ready·실제 image ID·worker probe를 확인했다. 운영 v1 3건·v2 1건의 재조회와 report/input/collection-audit 12개 해시는 배포 전과 같다. 새 조사 한 건은 약 13.90초·시도 1회에 v3 보고서로 저장됐다. partial/insufficient_evidence/unknown·사실 3·가설 0, available 7·missing 9·stale 1·invalid 0이었다. 접수 전후 중복 키·읽기 인가·외부 모델 차단을 확인했다. Prometheus up=1·retention enabled=1·overdue=0·진행 시각 갱신, 앱 Ready 2개·HTTPS readyz/items 200·TLS 검증 성공을 확인했다. [새 운영 조사 기록](assets/B6-operational-demo-v3.json).


## 실습 emptyDir 자료의 보존·이관

실습 수집기는 emptyDir이므로 일반적인 Deployment 교체만 하면 과거 증거·보고서가 사라진다. 같은 namespace의 임시 Pod `lab-v3-store-transfer`를 승인 digest로 생성해 기존 저장소를 Pod 간 파이프로 직접 전달했다. 자동 토큰 마운트 없음·lab-workload SA·기본 통신 차단·일반 사용자·읽기 전용 루트·권한 제거 상태이며 추가 PVC/LB/IAM을 만들지 않았다. 전송은 파일 경로·링크·유형·총 크기를 검사하고 메모리 스트림으로 처리했다. 저장소의 내부 지문 키도 이동하지만 로컬 파일이나 도구 출력으로 반출하지 않았다.

기존 v2 보고서 6건을 재조회하고 전체 저장 파일 38개 해시가 임시 Pod에서 같을 때만 수집기 이미지를 교체했다. 새 수집기에 복원한 뒤 같은 검사를 통과했고 비공개 실습 설치 원본·namespace 구성 해시를 새 digest로 갱신했다. 직접 만든 임시 Pod만 UID 조건으로 삭제했으며 최종 부재를 확인했다. 이것은 이번 운영자 이관 검증이고 상시 백업·범용 이관 기능을 구현한 것은 아니다. emptyDir의 노드/Pod 삭제 위험은 여전히 남는다.

## 배포된 v3의 실제 S3 재현과 원복

2026-10-21 16:54:37~16:55:16 KST, 기존 단일 실행 잠금·이전 지표 창·실제 scrape 기준선을 지키며 수정 후 필요한 S3 한 번만 재현했다. ECR 태그 부재와 변경 전 복구 계획을 먼저 고정하고 실습 앱 이미지 한 필드만 바꿨다. ErrImagePull·Ready 0·해당 Pod의 Failed 이벤트를 관측한 뒤 정답 기록을 분석기에 주지 않고 실제 수집으로 조사했다.

새 v3 보고서는 사실 4·원인 후보 1·partial/suspected_cause/unknown이며 `image_failure_supported`로 판정됐다. available Pod 상태·Failed 이미지 표식 이벤트·Deployment 자료 세 ID를 인용했고, 인용 Pod UID가 실제 주입 Pod와 같았다. Pod-이벤트 시각 차이는 약 7.39초다. 수집 자료는 available 13·missing 8·stale 7·invalid 1이며 부족·부적합 자료를 숨기지 않았다. 태그 부재·인증·네트워크 하위 원인이나 전체 서비스 상태를 확정하지 않는다.

finally에서 원래 digest·Ready 1·readyz/items HTTP 200·Redis PING을 복구했다. 수동 HTTP는 총 4회이고 전체 실행은 약 38.50초로, 장애 해결 시간 개선률이나 MTTR 지표로 쓰지 않는다. 운영 앱/AI/PVC 식별자와 나머지 실습 UID·이미지를 유지했다. 기존 실습 6건·38개 파일 해시는 재현 후에도 같았고 새 보고서 저장·재조회·v3 실제 소스 해시를 확인했다. [새 S3 실측](assets/B6-s3-image-failure-v3.json), [조사 전 관측](assets/B6-s3-image-failure-v3.ground-truth.json), [변경 전 복구 계획](assets/B6-s3-image-failure-v3.recovery-plan.json)을 별도 보존한다.

원래 v2 needs_review와 같은 저장 입력의 후보 비교 기록은 그대로 둔다. 서로 다른 시각의 두 실측만으로 정확도 개선률을 계산하지 않는다. 이번 복구는 운영자 실습 실행기의 수동 원복이며 AI 실행·CI 자동 롤백이 아니다. 외부 모델 호출·API 비용은 0이고 captured 외부 전송 차단을 유지한다. AWS 인프라 비용을 0으로 주장하지 않는다.

## 남은 범위

이번 규칙 보완·배포 검증 묶음은 완료다. 캐시 TTL/미초기화 변형, S9 실제 Replica 범위 결정, S10 CI 실패·롤백 이력, S11 캐시 오류 의미 분리와 실제 모델 연결 정책·비교는 남는다. B-06 전체 완료나 C 실행 권한 부여로 확대하지 않는다. 후속 S9 목록의 표식/캐시 자연 만료와 단일 Primary 한계는 [S9 실측](B6-primary-read-scenario.md)에 별도로 기록했다. 다음은 미검증 S11 캐시 오류 의미 변형의 실습 설정·원복 범위 정의이며 GPT-6 SOL high를 추천한다. 모델 경로는 별도 전송 정책·승인 범위를 정한 뒤 진행한다.
