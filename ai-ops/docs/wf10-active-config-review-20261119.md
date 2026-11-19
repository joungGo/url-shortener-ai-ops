# WF-10 활성 배포 후보 코드 검토 · 2026-11-19

대상: `ai_ops.wf10_deployment`(신규 renderer), `alert_receiver.load_receiver_settings`(설정 hash 호환 보완), 신규 시험 `test_wf10_deployment.py`, 미확정 입력 `ai-ops/config/wf10-deployment.unresolved.json`, 로컬 구현 문서·검증 기록. 기준 main `50d0c1a309698320bc958eafc3d294f38b023f01`(검토 시작 시 원격 fetch로 변화 없음 확인). 검토 실행자는 Claude Code이며, Codex 로컬 구현 결과를 이어받았다.

## 검토 방법

renderer가 만든 컨테이너 명령·인자·마운트·권한을 실제 실행 모듈의 계약과 1:1 대조했다: `worker_runtime`(--probe, /etc/ai-ops·/data/incidents 기본값, `check_local`의 token/CA 읽기), `alert_receiver`(--cluster-id·event source 인자, 9443, `/etc/ai-ops-webhook` 권한 검사), `slack_server`(ingress/relay/probe-*, --enable-web, 9444, `/run/ai-slack-tls`), `slack_web.read_private_secret`(소유자=euid·0600·nlink1·O_NOFOLLOW), `slack_runtime`(relay의 signing 사용), `JobQueue`/`_private_directory`(0700/0600 검사). 기존 Helm chart `statefulset.yaml`의 보안 설정과도 비교했다. 테스트 환경변수 없이 CLI build/verify/미확정 입력 차단을 직접 실행했다.

## 발견과 수정

1. **[수정] 공유 PVC 권한 확대 — `fsGroupChangePolicy` 누락.** 후보 Pod는 `fsGroup: 10001`만 지정해 기본 정책(Always)이 적용된다. 이 경우 kubelet이 기동마다 기존 PVC를 재귀적으로 파일0660/폴더2770으로 넓혀 `JobQueue._safe_files`·`_private_directory`의 0600/0700 검사가 실패한다. 저장소 기록(`B6-ci-service.md`, `B6-normal-source-persistence.md`)의 과거 실제 사고와 같은 경로이며, 기존 chart·`model_service`·`check_deployment.py`는 모두 `OnRootMismatch`를 요구한다. renderer에 `fsGroupChangePolicy: OnRootMismatch`를 추가하고 `test_one_pod_recreate_and_shared_existing_pvc`에 회귀 단언을 넣었다. 이미 권한이 넓어진 실물 PVC의 복구는 이 코드가 하지 않으며 기존 B6 절차(프로세스 중지·소유자/종류 확인 후 복구)를 따른다.
2. **[정리] receiver 지연 import 제거.** `load_receiver_settings` 내부 import를 모듈 상단으로 옮겼다. `runtime_settings`는 pathlib만 import하므로 순환이 없고 동작은 같다.

## 확인했으나 변경하지 않은 사항

- 역할별 자격 분리: worker(projected SA token·collector), receiver(사건 인증/TLS), ingress(signing/TLS), relay(signing/bot — `slack_runtime.py`가 signing도 읽음)가 실제 사용과 일치한다.
- init 복사: Secret 0440+fsGroup으로 uid10001 init이 읽고 메모리 emptyDir에 0600 복사 → `read_private_secret`의 소유자/모드/링크 검사와 receiver의 `& 0o037` 검사를 모두 통과한다.
- 설정 hash: 같은 ConfigMap을 네 프로세스가 읽고 receiver 보완으로 worker/Slack/receiver hash가 일치한다(시험으로 확인).
- 패키지 크기: 합성 후보 `candidate.json` 38.8KB / 한도 64KiB. 실제 설정이 커지면 `wf10_package_too_large`로 실패하며 잘라내지 않는다.
- 미확정 입력은 CLI exit2·리소스0·출력 폴더 없음, 완성 합성 입력은 build/verify 성공·execution_allowed=false.

## 남은 위험(후속 실제 준비에서 판단)

- worker/receiver에 기존 chart의 `livenessProbe`가 없다. 정지 상태 프로세스의 자동 재시작은 readiness만으로 이루어지지 않는다. 실제 운영 기준을 정할 때 결정한다.
- init 컨테이너에 resources가 없어 LimitRange/ResourceQuota가 강제되는 namespace에서는 거절될 수 있다.
- Slack egress를 공인 `/32` 목록으로만 허용한다. Slack API 주소는 고정 목록이 공개되지 않으므로 실제 연결 때 egress proxy/NAT 등 별도 경로 결정이 필요할 수 있다. 값은 추측하지 않았다.
- 서버 schema 검증(`kubectl --dry-run=server`)과 CNI/kubelet probe 허용·물리 PVC/SQLite WAL 동작은 실물 없이 확인하지 않았다.

## 검증

결과 수치와 소스 hash는 [검증 기록](wf10-active-config-review-validation-20261119.json)에 기록한다. 기존 시간 3/15/120/210/240/300/120초와 승인·정확 head 리뷰/CI·잠금/ACK·원복·회수 경로는 변경하지 않았다. 인프라 설치·실제 배포·Slack 전송·장애 실습·Terraform·유료 모델 호출은 하지 않았다.

## 반영

이 커밋 시점 원격 CI·main 반영은 대기다. 이 저장소는 비공개·무료 플랜이라 GitHub 브랜치 보호/ruleset API가 403으로 제공되지 않는다. 따라서 기존 PR38과 같이 정확 head의 PR CI 성공을 확인한 뒤 일반 squash 병합하며, 보호 설정 변경·관리자 우회는 하지 않는다. 실제 입력12개 확정과 연결·WF11/C04/C05는 후속이다.
