# 장애 워크플로우 구현 작업표

최종 갱신: 2026-11-19 KST. 설계 기준: [전체 워크플로우·변경 이유](incident-workflow-and-improvement-plan-20261118.md). 현재 작업 기준: AI main `59c33a9`(WF06 PR34), 플랫폼 main `bf6bcbc`. WF07 작업 브랜치 `codex/wf07-image-pr-20261119`. 로컬 구현·main 반영·실제 연결 상태를 구분한다.

## 상태와 완료 규칙

상태는 `예정 / 설계중 / 구현중 / 검토중 / 로컬검증완료 / CI통과·main반영 / 배포완료 / 실환경검증완료 / 보류` 중 하나로 기록한다. 설계 문서는 문서 검토가 끝나면 `문서완료`로 표시할 수 있다. 보류에는 이유와 재개 조건이 필요하다. 코드 병합을 실제 사용 흐름 완료로 표시하지 않는다.

작업마다 아래 실행 기록에 변경 파일/커밋·PR·CI·검증 범위/결과·실제 연결 여부·실패/한계·다음 행동을 기록한다. 해당 단계가 없는 항목은 근거와 함께 `해당없음`으로 표시한다. 완료 비율이나 시험 개수만으로 제품 완료를 판정하지 않는다. 상태 변경 때 같은 파일의 해당 행을 갱신하고 이력에는 의미 있는 결정/완료/범위 변경만 추가한다.

## 작업 목록

| ID / 단계 | 상태 | 작업·재사용할 구성 | 산출물 / 예상 소유 위치 | 의존성 | 완료·검증 기준 |
| --- | --- | --- | --- | --- | --- |
| WF-00 / 계획 | 문서완료 | 변경 이유·현재 격차·PR/명령 흐름·검증 범위 | 전체 계획 문서·이 작업표·양쪽 HANDOFF | 없음 | 이유R1~R7와 작업 연결, 현행/제안/실제검증 구분, 로컬 링크 유효 |
| WF-01 / P1 | 문서완료 | 사건·발생 회차·채널·조사 정책 계약; alerts/TaskService 분석 | [WF-01 상세 계약](incident-identity-channel-policy-contract-20261118.md)·D1/D2 설계 채택; 실행 스키마는 WF-02 | WF-00 | 중복·재발·해제·다른대상 경계, 공지/조사/복구 독립 상태, 자동조사 예외와 전환안 명시 |
| WF-02 / P1 | CI통과·main반영 | 사건 저장·보관·현재시점/발생시점 정책; 기존 큐/장부 재사용 | [사건 저장/보관/전환 구현](incident-store-local-implementation-20261118.md) · event_store/queue/worker · 32개 집중/1325개 전체 시험 · [PR #30](https://github.com/joungGo/url-shortener-ai-ops/pull/30), main9c9f3fc | WF-01 | 재전송·프로세스중단·재시작·이전 DB 보존·동시접수·보관만료 시험, 해제 경보로 실행승인 생성0 |
| WF-03 / P2 | CI통과·main반영 | 기존 경보 공지와 사건 참조·조사 채널 안내 연결 | [로컬 구현·검증](incident-notice-local-20261118.md) · 플랫폼 후보/template, AI 인증 EventInbox·참조 상태 · 플랫폼 PR163/mainbf6bcbc, AI PR31/mainca63fcf·양쪽 main CI 통과 | WF-01, WF-02 | 공지·접수 독립 라우팅, Watchdog/억제 보존, 나노초 참조 매핑, pending/unknown·저장 실패 경계 통과; 실제 전송은 미검증 |
| WF-04 / P2 | CI통과·main반영 | Slack 사건 지정 조사·상태조회·기존worker 결과 연결 | [로컬 구현·검증](incident-slack-local-20261119.md) · PR32/main e3a4efa·PR7종/main CI 통과·전체1372개 · 사건 인가·기존 outbox/worker·같은 thread 결과·보관 정리 | WF-02, WF-03 | 같은 사건의 요청/결과, 미허용 사용자·채널·대상 거절, 수동 기본에서 요청전 조사0, 재시작 후 중복 회신 방지 |
| WF-05 / P3 | CI통과·main반영 | 복구 계획 요청·지원 조치 목록·공통 계약 | [로컬 구현·계약](recovery-plan-local-20261119.md) · 고정 registry·RecoveryPlan·plan-recovery·기존 사건/job/회신 장부 | WF-04 | 지원/미지원·증거부족 분기, 사건/대상/근거/계획해시/실패행동 명시, 요청만으로 클러스터 쓰기0 |
| WF-06 / P3 | 로컬 핵심 CI통과·main반영 | 승인 대기·만료·취소·늦은 승인·재검증 | [승인 장부/로컬 계약](recovery-approval-local-20261119.md) · prepare_proposal 한정·D3/D4 활성화 조건 · 실제 인증/조회 연결은 WF07/08 | WF-05 | 가짜시계로1시간+대기·자연회복·UID/계획변경·승인중복·취소 경쟁 시험, 대기중 쓰기토큰/잠금0 |
| WF-07 / P4 | 검토 보완·CI 반영 준비 | WF05/06 계획·준비 동의에서 기존 이미지 PR/리뷰/CI/잠금/동기화 검증으로 연결 | [로컬 구현·검증](recovery-pr-local-20261119.md) · recovery_pr · 같은 사건 결과 문자열 · 실제 인증/쓰기/outbox 포트 미활성 | WF-05, WF-06 | 로컬 Git/Helm·정확 head/CI/권한·유실/중단/중복/취소·잠금/동기화 경계; 실제 실행/효과/Slack은 후속 검증 |
| WF-08 / P5 | 예정 | 명령1종 계획·승인·별도 실행기·권한 | AI 고정 command 어댑터·chart의 제한 RBAC·D5 결정 | WF-05, WF-06 | 임의shell·인자삽입·다른대상 차단, 승인 계획과 실행 일치, timeout/응답유실 후 무조건 재실행0, 가역성/실패행동 표시 |
| WF-09 / P6 | 예정 | 두 실행방식 공통 효과검증·원복/수동인계·결과 통지 | AI 결과계약·조치별 verifier/실패정책 | WF-07, WF-08 | 명령종료0/PR병합을 회복으로 오판하지 않음, 실제변경·효과·원복 구분, 자격/잠금/자기임시자원 회수 |
| WF-10 / 연결 | 예정 | 공지·조사 첫 완료단위 배포 및 실제 Slack 검증 | 버전/digest/config·대상/채널·정상CI·원복 계획/기록 | WF-04 로컬검증·CI/main | 합성 시험 뒤 승인 범위의 실제 통지·사용자 요청·같은사건 결과 확인. 첫 단계에 장애 주입 불필요 |
| WF-11 / 실환경 | 예정 | PR/명령 실제 효과와 C04/C05 남은 필수 검증 | 실행별 계획·승인·결과·원복 증거 | WF-09, WF-10 및 실제준비 | 새실행 승인과 해당 방식 승인 확인, 정상/실패/취소/독립원복 필수사례, 현재지원범위 내 결과 기록 |
| WF-12 / 인계 | 예정 | 설치·정리·재개·사용법과 지원 조치표 정리 | AI 문서/플랫폼 연결 런북/HANDOFF | 각 단계 완료에 맞춰 갱신, 최종 WF-11 | 배포 버전/검증 범위/지원·미지원·남은 한계 추적, 임의 권한확대/과거승인 재사용 없음 |

WF-07과WF-08은 공통 계약이 안정되면 독립 검토할 수 있으나 초기 적용 순서는 PR→명령1종이다. WF-10은 모든 복구 기능이 끝날 때까지 기다리지 않고 공지·조사 첫 완료단위를 검증한다.

## 설계 시 먼저 해결할 연결 문제

1. WF-03에서는 message ts/permalink 없이 `al1/source_alias/fingerprint/startsAt`을 같은 canonical evt로 해석한다. 고정 Alertmanager의 실제 Go template과 수신 payload 간 나노초 정밀도를 로컬 검증했다. 원본 Slack 링크 조회·history 권한 확대는 하지 않으며 사건 접수만으로 Slack 전달 성공을 기록하지 않는다.
2. 모든 경보 사실 공지를 유지하면서 조사 허용5종과 일반 경보의 사건 등록 범위를 분리한다. 기존 AlertInbox의5종 조사 허용조건을 풀어 미지원 경보를 조사 대상으로 만드는 방식은 피한다. 새 사건 접수는 저장/알림 역할이며 수집 가능한 대상과 실행 가능한 조치는 별도 인가한다.
3. 경보 텍스트·사용자 질문을 실행 명령으로 사용하지 않는다. 사건 링크만으로 다른 채널/대상의 조회 권한이 생기지 않는다.
4. Slack 상호작용 엔드포인트의 서명·재전송·승인자·계획해시 검증은 기존 app_mention 인증과 별도로 검토한다. 기존 설치 범위에 포함되지 않은 scope/권한은 연결 전 구체안을 제시한다.
5. restart를 일반적인 무해 작업으로 가정하지 않는다. Deployment 가용성·소유권·이미 진행 중인 rollout·GitOps와의 충돌을 판단하고 실패 시 자동 재시작 반복을 막는다.

## 구현 진행 규칙

- 시작할 때 이 표와 최신 HANDOFF를 읽고 하나의 완료 단위를 선택한다. 기존 큐/worker/Slack 장부를 재사용하고 운영 앱/플랫폼 구현을 AI 저장소로 복제하지 않는다.
- 구현 순서: 계약/fixture → 코드 → 로컬 통합 → 직접 검토/필요 검토 → 원격 CI/main → 필요한 배포 → 실제 검증. 사용자 요청 범위가 단계별이면 다음 외부 단계까지 승인됐다고 확대하지 않는다.
- 의미 있는 실패를 로컬 재현으로 먼저 고친다. 테스트가 통과하고 변경이 없으면 같은 검사를 이유 없이 반복하지 않는다.
- 상태를 올릴 때 성공 증거와 한계를 함께 연결한다. 기능 요구 추가는 이유R-ID·작업WF-ID·완료 기준부터 추가하고 기존 완료 기준을 몰래 늘리지 않는다.
- 문서가 채널통합·자동조사중단·명령권한/시간정책 변경을 승인한 것으로 해석되지 않게 D1~D5의 확정 여부를 기록한다.
- 임시 자격·토큰·원문 사건·개인 로그를 문서나Git에 넣지 않는다. 정제된 근거·코드/PR/CI 링크·검증 범위만 남긴다.

## 실행 기록

| 날짜 | ID | 결과·근거 | 다음 행동 |
| --- | --- | --- | --- |
| 2026-11-18 | WF-00 | 사용자 목표와 현재 차이, 이유R1~R7, 설계 선택D1~D5, 구현WF-01~12 정의. 문서만 작성. CI/main/배포 없음 | WF-01 사건·채널·조사정책 계약 구체화 |
| 2026-11-18 | WF-01 | evt와 기존inc/job 분리, canonical 발생키·18개 시험명세, alert_ref로 두채널 연결, manual기본/자동예외·거절/해제/중복 정책 문서화. 실제시험/코드/CI/배포 없음 | WF-02 계약/저장/outbox/보관·마이그레이션 로컬 구현 |

### WF-02 실행 기록 · 2026-11-18

- 상태: 로컬검증완료. 기준main36a55fd, 로컬 미커밋. PR/원격 CI/main 반영 없음.
- 코드: event_store.py 신규; job_queue.py/worker.py 수집 전 guard; test_event_store.py 신규. 상세 [구현 기록](incident-store-local-implementation-20261118.md), [검증 JSON](incident-store-local-validation-20261118.json).
- 집중29개·사건/큐45개·전체1322개 통과, 변경Python4개 AST와 diff 검사 통과. 실제 SQLite/기존worker/보관정리·동시접수·강제프로세스종료 포함. 초기 전체시험의 bare DB version3 오류를 고친 뒤 최종 전체통과.
- 사건발생 기준7일/해시 tombstone·장부 각각1만개 상한, metadata 정리는 explicit expire API. active 근거 보존·기존 큐100개/7일/Lease 유지.
- migration은 원래 job/시도를 보존하고 검증없는 전환 거절. 전환된 legacy 대기는 새 정책으로 자동 실행하지 않고 새 명시 요청 요구.
- 한계: HTTP/Slack 인증·실제두채널·공지템플릿·자동정리 호출·장기장부 아카이브 자동화 미연결. 기존 SQLite ResourceWarning 관찰. 외부/실환경 정상 증거로 해석하지 않음.
- 실제 연결·배포·Slack·장애·Terraform·모델 호출 없음. 기존 시간/승인/원복·C04/C05 미완료 유지.
- 다음: 코드검토/CI/main 뒤 WF-03/04; 실제 전환은 이전 접수/worker 중지·백업·미전환 목록 대조를 포함한 별도 계획.

향후 작업 기록 양식:

- 작업 ID / 시작·종료일 / 상태:
- 변경한 코드·문서 / 기준 SHA / 커밋·PR:
- 검토 결과 / 로컬 검증과 한계:
- CI URL·결과 / main SHA:
- 배포 대상·버전 / 실제 연결·검증 결과:
- 실패·보류 이유 / 다음 행동 / 필요한 사용자 결정:

## 현재 재개 지점과 보존할 한계

WF-00/01 문서완료, WF-02/03 CI통과·main반영, WF-04 로컬검증완료. WF-03은 AI PR31/mainca63fcf·플랫폼 PR163/mainbf6bcbc와 각 main CI 성공으로 완료했다. 기존 로컬검증만 완료라는 기록은 과거 이력이다.

현재 WF-04 PR32/main e3a4efa·CI 반영 완료, WF-05 계획 요청/계약도 PR33/main17093a7·CI 반영 완료다. [구현 기록](incident-slack-local-20261119.md)의 사건 명령은 실제 Slack에 배포하지 않았다. WF-02~04 실물 DB/정책 전환·WF-10 실제 공지→요청→결과, WF-06~09 승인/PR/명령/효과 구현과 WF-11 C04/C05 실환경 검증은 남아 있다.

인프라는 2026-11-19 정리 완료 상태다. 실행 리소스/state0을 검증했고 DNS·최종/기존 RDS 스냅샷/KMS·S3 state/버전·operator/기존 로그인·원본 자격·개인 자료는 보존했다. 새 설치/자동 실습 재개를 요청한 것으로 해석하지 않는다. 기존 시간·승인·원복 조건은 유지한다. 추천 GPT-6 SOL · High.

## WF-02 코드 검토 · 2026-11-18

[검토 기록](incident-store-review-20261118.md): 정확 대상 인가·큐 부분 용량·반복 migration 해제/만료 처리3건을 재현·수정했다. 집중32개/큐 포함48개/전체1325개·AST4개·diff·저장소 경계 통과. 사용자 요청에 따라 이 브랜치의 PR CI 통과 후 일반 main 반영을 진행한다. 이 기록 작성 시 원격 CI/main은 아직 미확정이며 최종 상태는 PR 병합 기록과 최신 HANDOFF를 따른다. 실제 배포·Slack·장애 없음.

## WF-02 main 반영 결과 · 2026-11-18

[PR #30](https://github.com/joungGo/url-shortener-ai-ops/pull/30)의 검토된 head fd5d6ef가 PR CI 6종(run44317979405) 통과 후 일반 squash 병합됐다. main `9c9f3fc265d998be95746a09d859b02a5f0affee`, [main CI](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44318734083) 성공. 이전 실행 기록의 원격 미확정 상태는 이 결과로 갱신한다. 보호 변경·배포·실물 장부 전환은 없었다.

## WF-03 로컬 구현·검증 · 2026-11-18

- 양쪽 `codex/wf03-alert-reference-20261118` 작업트리. 기준 AI9c9f3fc/플랫폼01ca82b. 로컬 미커밋이며 WF-03 PR·CI/main 반영 없음.
- 플랫폼: 경보 사실·발생 참조·조사 채널 Go template와 검토용 values 후보 생성기. 원래 Watchdog/억제/간격/Secret 유지. 기존 배포 values와 Application은 변경하지 않았다.
- AI: 서버 발신원에 고정한 인증 `/v1/events` 접수, 기존 EventStore 재사용, pending/registered/expired 조회. 기본 `/v1/alerts`와 현재 배포 args 유지. notice 전달은 항상 unknown으로 구분하며 접수만으로 조사/실행 승인 생성 없음.
- 플랫폼 8개, AI 집중 50개 및 전체 1,334개(81.827초, skip 0), AST 6개·diff·저장소 경계·실제 inline template 렌더 통과. 공식 고정 Alertmanagerv0.32.0 도구의 체크섬 검증 후 사용. [검증 JSON](incident-notice-local-validation-20261118.json).
- T11 실제 template→수신 payload의 나노초/offset 매핑은 로컬 완료. T16의 반대 도착 순서·독립 라우팅·저장 실패503/unknown 경계는 로컬 완료지만 실제 Slack/Alertmanager의 외부 전송 실패·재시도 SLA는 미검증.
- 제한: 사건 지정 Slack 명령·결과 회신과 정기 보관 정리는 WF-04. 실제 채널/발신원·DB 전환/정책 diff는 WF-10 연결 계획에서 다시 확인. 후보는 기존 자동조사 분기를 사건 접수로 전환하므로 검토 없이 배포하지 않는다.
- 기존 시간·실행별 승인·정확 head 리뷰/CI·잠금/ACK·원복 조건과 C04/C05 미완료 유지. 실제 배포·Slack 전송·장애 실습·Terraform·유료 모델 없음.
- 다음: WF-03 코드 검토·CI/main → WF-04 로컬 연결. 추천 GPT-6 SOL · High.

## WF-03 검토·CI 반영 준비 · 2026-11-18

[검토 기록](incident-notice-review-20261118.md): Slack 기본 fallback의 라벨 노출·대체 설정·CI 누락3건을 보완했다. 플랫폼10개/AI교차9개 통과, 기존 AI 전체1334개 검증 소스4개 해시 동일. 양쪽 C03 gate에 실제 renderer/연결 검사를 추가했다. 플랫폼 PR163 head d6c2c8f 고정, AI PR/CI와 양쪽 main 상태는 완료 후 최신 HANDOFF에 기록한다. 새 보호 변경 승인이나 실제 배포를 포함하지 않는다.

## WF-04 로컬 구현·검증 · 2026-11-19

- 상태: 로컬검증완료, 기준 mainca63fcf. 브랜치 `codex/wf04-slack-events-20261119`, 미커밋·PR/원격CI/main/배포 없음.
- [변경 이유·설계·소스·한계](incident-slack-local-20261119.md), [검증 JSON](incident-slack-local-validation-20261119.json).
- 사건 지정 조사/status/같은 thread 결과, 현재 ACL·인가 세대 fence·기존 큐/worker·기존 전달 장부 호환·60초 maintenance 연결. 신규36개·전체1370개·AST8개·diff·소유 경계 통과.
- 미등록/미허용/unbound/지워진 사건은 같은 비공개 응답. 상세가 남아 대상 인가를 확인할 수 있을 때만 만료 구분. 새 요청은 새 조사, 재전송은 기존 job; 상태조회는 job0. 자동예외 전송 활성화 없음.
- 실물 DB 전환/배포·Slack·장애·Terraform·모델 호출 없음. 기존 시간·승인·원복/C04/C05미완료 유지.
- 다음: 코드 검토·CI/main. WF-10 실제 연결은 별도 준비/승인 범위이며 인프라 없이 WF-05 로컬 작업 가능.

## WF-04 코드 검토 · 2026-11-19

[검토 기록](incident-slack-review-20261119.md): 정책 전환 중단 시 이전 큐 권한 잔존(P1), 사건 연결 비활성화 뒤 복사본 만료 지연(P2)을 로컬 재현·수정했다. 신규회귀2개 포함 Slack125개 통과. 원격 CI와 main 반영은 정확한 PR head 검사 후 진행하며 실제 배포/Slack/장애는 포함하지 않는다.

## 2026-11-19 WF-04 코드 검토·CI 반영 준비

사건 지정 Slack 조사·상태/결과 연결을 직접 검토해 정책 변경 중단 뒤 이전 큐 권한 잔존과 연결 비활성화 뒤 사건 복사본 만료 지연2건을 재현·수정했다. Slack125개·전체1372개(84.572초/skip0)·AST8개·diff·저장소 경계 통과. 검토 기록 `ai-ops/docs/incident-slack-review-20261119.md`, 최종 소스 해시 `incident-slack-review-validation-20261119.json`을 따른다. 최초 로컬1370개/해시는 이전 시점 기록이다.

사용자 요청에 따라 이 검토 코드의 PR CI 통과 후 일반 main 반영을 진행한다. 이 커밋 시점의 원격 CI/main은 아직 미확정이며 최종 상태는 최신 인계 기록을 따른다. 기존 시간·승인·원복 조건 유지. 실제 배포·DB전환·Slack·장애·인프라·Terraform·모델 실행 없음. 추천 GPT-6 SOL · High.

## 2026-11-19 WF-04 반영 완료 / WF-05 로컬 구현

- WF-04: PR32 검토 head2a1aefc·PR CI44338728798(필수 gate 포함7종) → 일반 squash main e3a4efa·main CI44339438355 통과. 보호 변경/우회 없음. 이전 검토 문서/JSON의 완료 메타데이터도 보존했다.
- WF-05: 고정 action registry·해시 결합 RecoveryPlan·명시적 `plan-recovery 사건 job [action]` 입력·같은 사용자/채널의 사건 조사 결과 결합을 구현했다. 기존 outbox/큐/worker와 Slack inbox/회신/정리를 재사용한다. 계획 요청 자체로 새 조사·PR·명령·쓰기 자격 생성0.
- `supported`는 저장된 이미지 장애 근거에 맞는 계획 후보일 뿐이다. 정상 이미지·현재 UID/revision·정확 diff·승인/CI는 후속 조건이며 항상 proposal_ready/execution_allowed=false. deployment-restart는 명령 방식의 미구현 후보로 반환한다. D3/D4/D5와 WF06~09는 완료 처리하지 않는다.
- [구현·제한·사용법](recovery-plan-local-20261119.md), [집중/전체 검증·파일 해시](recovery-plan-local-validation-20261119.json). 코드 미커밋, 원격 CI/main·실제 배포·Slack·인프라·장애 실습 없음. 기존 시간·승인·원복 조건 유지.
- 다음: WF-05 검토·CI/main 후 WF-06 로컬 구현. 추천 GPT-6 SOL · High.

## 2026-11-19 WF-05 검토 보완·CI 반영 준비

직접 코드/호출 경로 검토에서 사건 시각 나노초 손실로 직전 관측이 지원 근거가 되는 P2 한 건을 재현·수정했다. 원래 정밀도 보관·비교와 해시/계약 재검증을 추가했다. 최종 로컬 시험·소스 해시는 [검토 검증 기록](recovery-plan-review-validation-20261119.json), 상세는 [검토 기록](recovery-plan-review-20261119.md)을 따른다. 사용자가 검토·CI/main 단계를 진행하도록 요청했다. 정확 head의 CI 뒤 일반 병합하며 실제 배포·Slack·인프라·실습은 포함하지 않는다. 기존 시간·승인·원복 유지.

## 2026-11-19 WF-05 검토·CI/main 반영 완료

[AI PR #33](https://github.com/joungGo/url-shortener-ai-ops/pull/33)의 최종 head `33a9a19cb832af106af65d94621547ba43aa75f7`를 일반 squash 병합해 main `17093a7684c885574388d81f3168033d526a6115`에 반영했다. 직접 검토에서 사건 시각 나노초 손실로 직전 관측이 지원 근거가 되는 P2 한 건을 재현·수정했다. 이미지 CI의 OpenSSL 간접 갱신/검사 불일치도 공식 Alpine 양 아키텍처 목록 확인 후 설치·검사를3.5.9-r1로 함께 고정했다.

WF05 집중29개 통과. 로컬 전체1401개 중1400개 통과 후 임시 amtool 부재의1개는 고정 notice9개 검사에서 통과해 고유1401개 검증 완료(skip0). [최종 PR CI44446823328](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44446823328)의 gate 포함7종·pushCI44446816507·[main CI44447269162](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44447269162) 성공. main PR전용 gate skip은 설계대로이며 실제 PR gate는 통과했다. 검토/병합 전체tree·소스해시 일치, 기존 C단계47개 소스 동일, 보호변경/관리자우회 없음.

상세 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf05-recovery-plans-20261119/ai-ops/docs/recovery-plan-review-20261119.md`와 `recovery-plan-review-validation-20261119.json`, 구현 문서·작업표를 따른다. 코드/검증 문서는 main 반영됐으며 완료 후 인계·작업표·검증 메타데이터는 로컬 갱신했다. 원래 작업트리의 다른 코드/개인자료는 보존했다.

계획 후보는 여전히 draft/미승인/proposal_ready=false/execution_allowed=false다. 기존 시간·승인·원복·C04/C05미완료 유지. 실제 배포·Slack전송·인프라·장애실습·ECR검사·Terraform/모델 실행 없음. 다음은 **WF-06 승인 대기·만료·취소·늦은 승인·재검증 로컬 구현**이다. 추천 GPT-6 SOL · High.


## 2026-11-19 WF-06 승인 대기 핵심 로컬 구현

[구현 기록](recovery-approval-local-20261119.md)과 [검증/해시](recovery-approval-local-validation-20261119.json). 기존 WF05 저장 계획/인가와 JobQueue DB를 연결해 장시간 대기·정확한 승인 subject·중복/만료/취소·현재 관측 재검증을 구현했다. 준비 동의만 표현하며 execution_allowed=false. 실제 명령 승인·Slack interactivity 인증·실환경 조회/PR 실행 연결은 WF07/08, 실행 후 검증/원복은 WF09로 남긴다. D3/D4 로컬 계약과 운영 활성화 선택을 구분했다.

집중35개·전체1436개(89.924초/skip0)·AST2개·diff·소유경계 통과. 기존 C단계 시간/승인/원복 조건 유지, 인프라·실제 Slack·배포·장애 없음. 다음은 WF06 코드 검토·CI/main. 추천 GPT-6 SOL · High.


## 2026-11-19 WF-06 검토·CI 반영 준비

[검토 기록](recovery-approval-review-20261119.md)의 P2 세 건을 로컬 재현·수정했다. DB 대기 뒤 기한 재확인, 종료 상태 보존, 원본 조사 결과/해시 재검증을 보완했다. 회귀4개 포함 집중39개 통과; 전체·정적 검증과 최종 소스 해시는 [검증 JSON](recovery-approval-review-validation-20261119.json). 정확 head CI 후 일반 main 반영하며 기존 시간·승인·원복 조건과 실제 미배포 범위를 유지한다.


## 2026-11-19 WF-06 검토·CI/main 반영 완료

[AI PR #34](https://github.com/joungGo/url-shortener-ai-ops/pull/34)의 최종 head `18f3eb41bcebedf0a57d2e971a8c5bebe25b88a9`를 일반 squash 병합해 main `59c33a9d4e4dfd8f524161ce2cc2d590e1ca1133`에 반영했다. DB 대기 뒤 예산 재확인·시계 역행 시 종료 상태 보존·원본 조사 결과/해시 재확인 P2 세 건을 로컬 재현·수정했다.

집중39개·전체1440개(90.613초/skip0)·AST2개·diff·저장소 경계 통과. [PR CI44450455429](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44450455429) 7종과 [main CI44450888910](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44450888910) 성공. main PR전용 gate는 설계상 skip이며 실제 PR gate는 통과했다. 검토/병합 전체 tree와 파일 해시 일치, C단계68개 기준 main과 동일, 보호 변경/관리자 우회 없음.

상세 `/Users/jounghyeon/dev/url-shortener-ai-ops/.worktrees/wf06-approval-lifecycle-20261119/ai-ops/docs/recovery-approval-review-20261119.md`와 `recovery-approval-review-validation-20261119.json`, 작업표를 따른다. 코드·검증 문서는 main 반영됐고 완료 후 인계/최종 결과 메타데이터는 로컬 갱신했다. 원래 작업트리의 다른 수정/개인자료는 보존했다.

이번 장부는 prepare_proposal 동의와 재검증이며 모든 결과 execution_allowed=false다. 실제 Slack 승인 endpoint·실환경 조회·정확 diff/PR 실행·명령 실행은 미연결. 기존3/15/120/210/240/300/120초·실행별승인·정확head리뷰/CI·잠금/ACK·원복/회수·C04/C05미완료 유지. 배포·Slack 전송·인프라 설치·장애 실습·Terraform·유료 모델은 실행하지 않았다.

다음은 **WF-07 기존 이미지 복구 PR 경로에 계획/승인 장부 연결을 로컬에서 구현·검증**하는 단계다. 정확 diff/정상 이미지·서명된 실제 승인 입력/조회 어댑터 계약을 먼저 연결하고 새 실행권한이나 실습은 자동 활성화하지 않는다. 추천 **GPT-6 SOL · High**.

### WF-07 연결 코어 로컬 구현 · 2026-11-19

- 기존 계획/승인/JobQueue DB와 C단계 proof/PR/리뷰/CI/merge/sync guard를 연결했다. digest 한 항목의 로컬 Git/Helm diff, 같은 사건·job·정확 head 결과 문자열을 만든다. 실제 Slack 전송과 생산용 writer는 미연결이다.
- 중복 생성 방지·응답 유실/프로세스 종료 뒤 읽기 조정·취소 경쟁·정책/대상 변경·만료·잠금 유실을 검증했다. 병합/동기화 계획은 write_enabled=false, 서비스 회복 판정은 WF09다.
- 집중28개(38.806초)·전체1468개(131.644초/skip0)·AST2개·저장소 경계/diff 통과, 기존 C단계75개 동일. 최종 시험/해시/한계는 [검증 JSON](recovery-pr-local-validation-20261119.json)과 [구현 문서](recovery-pr-local-20261119.md). 기존 C단계 파일·시간/리뷰/승인/원복 조건 유지. 코드 미커밋·원격 CI/main 미반영.
- 남은 연결: 실제 인증된 Slack 승인/UI와 결과 outbox, 제한된 쓰기/잠금/회수 어댑터, 기존 draft 감사 DB 보관 정리. 이는 단순 배포만으로 완료되지 않으며 별도 코드·로컬 검증 후 활성화한다.
- 다음: WF07 코드 검토·CI/main. 이어 WF08/09 로컬 구현과 WF10/11 실제 검증 준비. 인프라 설치·실제 PR/Slack/배포·장애 실습 없음. GPT-6 SOL · High.

### WF-07 검토 보완 · 2026-11-19

최종 DB 확인 전 취소·원본 보관 종료·관측 지연 P2 세 건을 회귀 시험으로 재현·수정했다. [검토 기록](recovery-pr-review-20261119.md)과 [최종 검증](recovery-pr-review-validation-20261119.json)을 따른다. 기존 제한/승인/원복과 실환경 미완료 유지. CI 통과 후 일반 main 반영 예정이며 배포·Slack·실습은 실행하지 않는다.
