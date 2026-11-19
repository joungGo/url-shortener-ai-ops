# WF-10 활성 배포 설정 로컬 구현 · 2026-11-19

상태: 로컬 구현/검증 완료, [코드 검토](wf10-active-config-review-20261119.md) 완료(P1 1건 수정)·CI/main 진행. 실제 값·환경·권한·적용 승인은 미확정이다. 새 모듈 `ai_ops.wf10_deployment`는 **검토 가능한 활성 후보 설정**만 생성하며 apply/install/전송/복원 기능은 없다. 기존 비활성 준비 패키지는 변경하지 않는다.

## 구현과 입력 단계

1. `ai-ops/config/wf10-deployment.unresolved.json`에는 실제 namespace/PVC/SA·이미지/소스 SHA·발신원·Slack 신원·Secret 이름·네트워크·공유 설정을 모두 null로 남겼다. 누락이 있으면 `blocked_missing_inputs`, 리소스0, CLI exit2이며 출력 디렉터리도 만들지 않는다. 과거 채널/UID/이미지/토큰을 자동 대입하지 않는다.
2. 완성된 입력은 기존 BindingRegistry/BRuntime/MetricCatalog/SlackPolicy/SlackWebConfig/런북 정책으로 검증한다. 격리 `url-shortener-b6-lab`·`ai-ops-lab/lab-api` 1대상, 같은 source cluster·별도 경보/조사 채널·요청자1명, 모델 비활성 및 기존 수집 제한을 유지한다.
3. 합성 입력은 시험 안에서만 구성한다. 값이 형식에 맞는다는 것은 실제 신원/권한/이미지 출처가 확인됐다는 의미가 아니다. `source_revision`은 이미지 출처 대조의 입력이며 renderer가 registry를 조회하지 않는다.
4. 생성 결과는 `candidate.json`의 resources에 ConfigMap·Deployment·ClusterIP Service2개·NetworkPolicy 총5개로 들어간다. `inputs.json`과 함께 0700 폴더/0600 파일에 배타 생성한다. 검증은 입력으로 다시 생성한 전체 결과와 바이트 비교하므로 manifest hash만 고친 변조도 거절한다. 해시는 전자서명·승인이 아니다.
5. render 입력과 **render 후 확인할 실행 조건**을 분리한다. 자신의 manifest hash를 사전 입력으로 요구하는 순환은 없다. 기존 실제 입력12개는 계속 미검증이며, 최종 manifest/원복 hash·UID/binding·정상CI·정확 연결 승인은 실제 준비 때 확인한다. 결과는 항상 execution_allowed=false/live_verified=false다.

## 후보 배치와 보안 범위

- 단일 Deployment replicas1/Recreate, 같은 namespace의 **기존 PVC** 하나에 네 컨테이너(worker·event receiver·Slack ingress·relay)를 배치한다. 같은 `/data`를 마운트해 큐 DB와 Slack DB가 물리적으로 공유되도록 제안한다. 실제 SQLite WAL 지원/기존 Pod 종료/CSI 동작은 로컬만으로 보장하지 않는다.
- worker만 projected Kubernetes token과 collector 자격, receiver만 사건 인증/TLS, ingress는 signing/TLS, relay는 signing/bot을 마운트한다. 서비스 계정 자동 마운트는 끄고 기존 read-only SA를 참조하며 RBAC/IAM을 생성하지 않는다.
- 기존 Slack 독자가 요구하는 소유자10001·일반파일0600을 맞추기 위해 고정 Secret key만 역할별 메모리 emptyDir로 복사하는 non-root init 컨테이너를 생성한다. 원본 Secret·키 값을 렌더러가 읽거나 출력하지 않는다. Pod init 컨테이너들은 동일 서비스 계정/Pod 경계 안에 있으므로 이것을 컨테이너별 네트워크 보안 격리로 주장하지 않는다.
- 단일 설정 ConfigMap을 네 프로세스가 읽고 변경 hash를 Pod template에 넣는다. receiver에 source alias/ID/cluster를 함께 지정해 `/v1/events` 경로를 선택한다.
- NetworkPolicy는 정확 namespace+Pod selector로 Alertmanager9443/ingress9444와 DNS53을 허용하고, 입력된 collector `/32` IP·설정된 HTTPS port 및 Slack public `/32`443만 허용한다. 전체 CIDR·빈 selector·Slack 사설/loopback 주소를 거절한다. 실제 DNS 해석/IP 갱신·CNI/DNAT·기존 additive 정책은 재설치 때 검증해야 한다.
- namespace/PVC/SA/Secret·Ingress·DNS·인증서·Alertmanager 설정은 자동 생성/변경하지 않는다. 기존 정상 CI binding/relay 연결도 이 후보만으로 완료되지 않으며 새 환경에서 별도 검증한다. 이 패키지는 전체 설치 실행기가 아니다.
- 초기 전환은 새 빈 활성 store와 manual rules=[]가 전제다. 기존 store/자동 정책·대기 jobs/sending/승인을 붙이지 않는다. 이 사실은 renderer가 DB를 읽어 검증하지 않는다. 외부 적용자는 쓰기 중지/백업·새 store/대조와 명시 승인까지 완료해야 한다. 원복 시 전환 후 새 요청/모호한 전송 보존·호환 코드/DB·수동 인계 조건은 기존 WF10 전환/원복 문서를 따른다.

## 접수기 설정 해시 호환 보완

기존 receiver는 `runbook-policy.json`을 설정 hash에 포함하지 않고 worker/Slack만 포함했다. 이 경우 같은 사건의 metadata.config_hash가 달라 사건 지정 요청이 거절될 수 있다. `alert_receiver.load_receiver_settings`에서 기존 `runbook_settings_kwargs`를 재사용한다. 정책이 없는 기존 설정은 기존 동작을 유지하고, 정책이 있으면 세 프로세스의 hash가 일치한다.

회귀 시험은 이전 방식으로 접수한 사건의 요청 거절과 수정된 방식의 요청 성공을 실제 로컬 SQLite/사건 장부로 확인한다. 실제 과거 DB의 metadata/hash를 덮어쓰는 migration은 수행하지 않는다.

## 로컬 명령

AI 작업트리 루트에서 잠금 의존성을 설치한 Python을 사용한다.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=ai-ops/src:ai-ops/tests python -m unittest test_wf10_deployment test_wf10_preparation test_alert_receiver test_slack_events -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=ai-ops/src python -m ai_ops.wf10_deployment build --input ai-ops/config/wf10-deployment.unresolved.json --output /tmp/wf10-unresolved-must-not-render
```

두 번째 명령은 미확정 입력으로 **exit2·리소스0·출력 폴더 없음**이 정상이다. 완성된 합성 후보는 unittest의 임시 폴더에만 생성/검증한다. 실환경 후보가 생성되어도 `verify`는 정합성 검사이며 실행 승인이 아니다.

기존3/15/120/210/240/300/120초·실행별 승인·정확head 리뷰/CI·잠금/ACK·원복/회수 경로는 변경하지 않는다. 실습을 실행하지 않는다. 전체 검증 수치와 소스 해시는 [검증 기록](wf10-active-config-local-validation-20261119.json)에 기록한다.

다음은 코드 검토·CI/main 반영이며, 실제 입력 확정/연결·C04/C05 검증은 별도 후속이다. 이번 코드는 미커밋이며 push·이미지 빌드/등록·실제 배포·Slack·클라우드/유료 모델 호출은 없다. 추천 **GPT-6 SOL · High**.

## 최종 로컬 검증

신규20개, 관련80개 선행 통과(최종2개 추가 전), 전체1585개(207.001초/skip0) 통과. AST3개·저장소 경계·diff 확인. 합성 DB의 임시 경로 거절1건은 테스트의 전용 허용 경로 등록으로 해결했으며 제품 경로 제한은 완화하지 않았다. 전체 suite의 최종 결과와 소스 hash를 검증 JSON에 보관했다. 서버 schema 검증·실물/자격/TLS·실제 회신은 미검증이다.
