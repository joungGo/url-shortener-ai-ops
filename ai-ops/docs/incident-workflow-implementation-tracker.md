# 장애 워크플로우 구현 작업표

최종 갱신: 2026-11-21 KST. 기준 AI main `83b8e39`(A7 프로세스 연결 조각3 재검증·WF07 준비 PR56), 플랫폼 main `bf6bcbc`. 현재 작업 브랜치 `claude/a7-recovery-observer-20261121`(A7 PR 경로 현재 상태 관측기). [전체 계획](incident-workflow-and-improvement-plan-20261118.md). 로컬 코어·main 반영·실제 연결/검증은 구분한다.

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
| WF-07 / P4 | 로컬 코어 CI통과·main반영 | WF05/06 계획·준비 동의에서 기존 이미지 PR/리뷰/CI/잠금/동기화 검증으로 연결 | [로컬 구현·검증](recovery-pr-local-20261119.md) · recovery_pr · 같은 사건 결과 문자열 · 실제 인증/쓰기/outbox 포트 미활성 | WF-05, WF-06 | 로컬 Git/Helm·정확 head/CI/권한·유실/중단/중복/취소·잠금/동기화 경계; 실제 실행/효과/Slack은 후속 검증 |
| WF-08 / P5 | 로컬 코어 CI통과·main반영 | 기존 후보/큐/worker·WF06 수명주기 재사용, 명령 전용 정확 계획 승인·별도 로컬 실행기 | [명령 계약/검증](recovery-command-local-20261119.md) · 고정 JSON PATCH·단일 사건 소비·읽기 조정·D5 로컬 한정 | WF-05, WF-06 | 미승인/타 scope/임의 명령 차단, CAS·취소/만료·동시 실행·응답 유실·잠금 확인; 실제 권한/전송 미연결 |
| WF-09 / P6 | main 반영·CI완료(로컬 연결 코어) | 기존 PR/명령 적용 기록→효과/후속/회수 장부·같은 사건 결과 문자열 | [효과/원복·인계 계약](recovery-effect-local-20261119.md) · 합성 포트·실제 전송 미연결 | WF-07, WF-08 | 명령종료0/PR병합을 회복으로 오판하지 않음, 실제변경·효과·원복 구분, 자격/잠금/자기임시자원 회수 |
| WF-10 / 연결 | 비활성 준비·활성 후보 renderer·보강·Slack sender 분리·오프라인 schema 검증·변경 계획 형식 main반영(PR38~44) / 실제 입력·연결 대기 | 공지·조사 첫 완료단위 배포 및 실제 Slack 검증 | [연결 계획](wf10-connection-plan-20261119.md)·[입력 작업표](wf10-connection-inputs-20261119.json)·버전/채널/정책/DB/원복 준비 | WF-04 로컬검증·CI/main | 합성 시험 뒤 승인 범위의 실제 통지·사용자 요청·같은사건 결과 확인. 첫 단계에 장애 주입 불필요 |
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

WF00/01 설계와 WF02~09 로컬 구현·CI/main 반영 완료. 최신 AI main1bea7d6(PR37), 플랫폼 마지막 검증 mainbf6bcbc. WF06~09는 로컬 연결 코어이며 실제 인증/쓰기/효과·회수 adapter와 복구 결과 outbox는 미연결이다. 과거 실행 기록의 ‘미구현/CI 대기’는 해당 시점 이력이다.

WF10 연결 계획 문서와 검토용 입력 작업표를 작성했다. 경보 공지와 사건 접수는 독립, 두 채널 유지, 수동조사 기본·자동예외0개, 새 빈 활성 큐가 전환안이다. 실제 경보 channel/source registry·이미지/render/원복 hash·새 UID/TLS/정상 CI·적용 승인은 미확정이다. 비활성8파일·오프라인 도구·합성 전환/원복 리허설을 준비했다. 준비 도구/자료는 PR38로 main 반영됐다. 다음은 실제 입력과 활성 배포·전환/원복 적용안 확정이다. 실제 연결은 사용자 요청/확정안 승인 뒤 진행한다.

기록상 인프라는2026-11-19 정리 완료다. 이번에 실물/원본 자격/백업 내용은 조회하지 않았다. 기존 DNS·스냅샷/KMS·state/버전·operator/로그인·원본 키·개인 자료 보존. WF11/C04/C05 실환경 완료와 WF12 최종 인계는 남아 있다. 기존 시간·승인·원복 유지, 인프라·Slack·실습 자동 재개 없음. 추천 GPT-6 SOL · High.

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

### WF-07 검토·CI/main 완료 · 2026-11-19

[PR35](https://github.com/joungGo/url-shortener-ai-ops/pull/35), headbba9c33, mainb49672e. P2 세 건 수정, 집중31/전체1471개·기존 C단계75개 동일. PR CI44454087061 7종·main CI44454506379 성공, 정상 squash·보호 변경/관리자 우회 없음. 검토/병합 tree·소스 hash 일치. 실제 연결·배포·Slack·실습 없음. 다음 WF08 로컬 명령 계약·실행기; 실제 adapter와 WF09/10/11 잔여는 유지.

### WF-08 명령 경로 로컬 구현 · 2026-11-19

- 최종 검증: 집중30개·전체1501개(152.834초/skip0), AST3개·diff·저장소 경계 통과. C단계75개와 공유 Lease 구현은 기준 main과 동일.

- 기존 WF05 재시작 후보와 실제 queue/worker 근거, 같은 개인 DB와 WF06 승인 수명주기를 연결했다. 별도 execute_command 정책/테이블이 PR 준비 승인과 섞이지 않는다.
- 최초 명령은 한 Pod 격리 lab-api의 UID/resourceVersion CAS + 소유 restart annotation PATCH다. 정확 계획·실행자·현재 진단·공유 Lease 확인 후 로컬 fixture만 실행한다. 같은 사건 중복/다른 계획 재실행·응답 유실 자동 재시도는 거절한다.
- [구현 문서](recovery-command-local-20261119.md)와 [검증 JSON](recovery-command-local-validation-20261119.json)에 조건·검사 결과·한계를 기록한다. 실제 Slack/승인 endpoint, 생산 실행 포트/RBAC/자격과 주기 보관 정리·WF09 효과검증은 후속이다. 기존 C-stage 시간·승인·원복 유지.
- 코드 미커밋·CI/main 미반영. 다음은 WF08 검토·CI/main. 실제 설치·배포·Slack·장애·Terraform·모델 없음. GPT-6 SOL · High.

### WF-08 검토 보완 · 2026-11-19

응답 유실 뒤 WF06 취소 누락과 동시 읽기 조정의 완료/취소 상태 덮어쓰기 P2 두 건을 재현·수정했다. 집중34개·전체1505개 통과, C단계75개/공유 Lease 동일. [검토 기록](recovery-command-review-20261119.md), [최종 검증](recovery-command-review-validation-20261119.json)을 따른다. 정확 head CI 후 일반 main 반영 예정이며 실제 배포·Slack·실습과 실행 조건 변경은 없다.

### WF-08 검토·CI/main 완료 · 2026-11-19

[PR36](https://github.com/joungGo/url-shortener-ai-ops/pull/36), headb571d34, main6891144. P2 두 건 수정, 집중34/전체1505개·C단계75개/공유 Lease 동일. PR CI44457885630 7종·main CI44458405951 성공(푸시의 PR전용 gate 예상 skip). 일반 squash·보호 변경/관리자 우회 없음, 검토/병합 tree·소스 hash 일치. 실제 연결·배포·Slack·실습 없음. 다음 WF09 공통 효과검증·원복/수동 인계 로컬 구현이며, 생산 승인/outbox/조회·쓰기/잠금·자격 연결은 별도 잔여다. GPT-6 SOL · High.

### WF-09 로컬 구현 · 2026-11-19

기존 WF07/08의 적용 시각/정확 계획과 원래 인가·근거를 효과 장부에 연결했다. runtime/HTTP/지표/경보의 같은 관찰 창으로 성공·미해결·검증불가를 분리하고, 사전 정확 범위의 원복 후보와 최초 실패/원복 결과·자기 소유 manifest 회수·수동 인계를 기록한다. 실제 연결/전송은 비활성이다. 상세와 최종 시험은 [문서](recovery-effect-local-20261119.md), [검증 JSON](recovery-effect-local-validation-20261119.json). 기존 제한·승인·원복 조건을 유지한다. 다음 WF09 검토·CI/main, GPT-6 SOL · High.

WF09 최종: 신규36/전체1541개(198.711초/skip0)·AST4개·diff·저장소 경계 통과. 기존 C단계75개/공유 Lease 동일. 코드 미커밋·원격 CI/main 미반영.

WF09 검토 보완: 후속 원복 상태·취소 우선순위 P2 두 건을 회귀3개로 재현/수정했다. 집중39/전체1544개 통과. 검토 문서와 review-validation JSON 참조. 이 시점 원격 CI/main 대기.

WF09 최종 반영: [PR #37](https://github.com/joungGo/url-shortener-ai-ops/pull/37) head `11ff946064388a090cecadb770d4278c7ca91201`를 일반 squash 병합해 main `1bea7d6f83e60b12ddb2fe04ce181cfecf5a17ff`에 반영했다. [PR CI44461799743](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44461799743) 7종·push CI44461794113·[main CI44462377953](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44462377953) 성공. main의 PR전용 gate skip은 설계대로이며 실제 PR gate는 통과했다. 검토/병합 tree `424ccbea3ca4b0b521b4e138c9d60867c508ac99`와 소스 hash 일치. 보호 변경/관리자 우회 없음.

### WF-10 연결 계획 · 2026-11-19

첫 완료단위를 공지→사건 지정 조사→같은 사건/요청 thread 회신으로 고정했다. 대상/권한·두 채널·source registry·manual 전환·새 큐/예외 migration·배포 순서·현재/합성 증거 구분·DB 호환 원복·적용 전 승인 패키지를 문서화했다. 계획만 완료이며 실제 입력 미확정/실행준비 미완료다. 코드/설정 활성화·인프라·Slack·장애 실습 없음. 다음은 계획 검토와 disabled 설정/전환·원복 자료 준비.

### WF-10 비활성 준비 · 2026-11-19

[비활성 준비 문서](wf10-disabled-preparation-20261119.md)와 `ai-ops/config/wf10-disabled-review/`8파일을 추가했다. `wf10_preparation`은 canonical inventory/비활성 상태를 검사하며 활성화/API/자격/restore 기능은 없다. 기존 chart schema를 유지한 Helm0리소스·Slack자격 읽기전 거절, 큐/Slack 별도DB backup·새 빈큐·registry/schema·원복전 새요청 보존을 로컬 검증한다. 실제입력12개/활성렌더/원복실물은 미확정. 검증 수치는 JSON 참조, 이번 코드 미커밋/CI/main 미반영.

WF10 비활성 준비 최종: 집중21개/전체1565개(209.958초/skip0)·AST2·diff·경계·링크18개 통과. Helm리소스0·Slack자격읽기0. 기존 구현파일429개/C75/공유Lease 동일. 코드/자료 미커밋, 원격CI/main/실행 미반영.

### WF-10 준비 코드 검토 · 2026-11-19

집중21개 재실행·전체1565개 검증 해시 일치, 검토상 수정 필요 오류 없음. [검토 기록](wf10-preparation-review-20261119.md)과 [검증 JSON](wf10-preparation-review-validation-20261119.json)을 따르며 CI/main 반영 대기. 실제 배포·Slack·장애 실행 없음.

### WF-10 준비 코드 최종 반영 · 2026-11-19

[PR #38](https://github.com/joungGo/url-shortener-ai-ops/pull/38) head `c0fa27311f9127eb856db2bf5f348d03e8f6f182`를 일반 squash 병합해 main `50d0c1a309698320bc958eafc3d294f38b023f01`에 반영했다. [PR CI44465091658](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44465091658) 7종·push CI44465054234·[main CI44465684513](https://github.com/joungGo/url-shortener-ai-ops/actions/runs/44465684513) 성공. main의 PR전용 gate skip은 설계대로이며 실제 PR gate는 통과했다. 검토/병합 전체 tree `50452341417945dc2a13e9ab17130d4be9dee228` 및 소스 hash 일치. 보호 변경/관리자 우회 없음.

준비 코드 완료와 실제 연결 미완료를 구분한다. 기존 시간·승인·원복 조건 유지.

### WF-10 활성 후보 설정 로컬 구현 · 2026-11-19

[활성 설정 로컬 기록](wf10-active-config-local-20261119.md): 별도 후보 renderer·미확정 입력 차단·단일 Pod/공유 PVC·역할별 Secret/통신 제한·ConfigMap/서비스/NetworkPolicy 생성. receiver의 런북 정책 포함 설정 hash를 worker/Slack과 통일. 실제 입력12개·적용 승인·실물 검증은 미확정이며 apply/전송 기능은 없다. 신규20/전체1585개·AST3·경계/diff 통과. 미커밋/원격CI/main 미반영.

### WF-10 활성 후보 코드 검토 · 2026-11-19

[검토 기록](wf10-active-config-review-20261119.md)·[검증 JSON](wf10-active-config-review-validation-20261119.json). P1 1건(공유 PVC `fsGroupChangePolicy: OnRootMismatch` 누락) 수정·회귀 단언 추가, receiver import 정리. 집중82개·전체1585개(skip1=WF03 환경 게이트) 통과. 이 커밋 시점 PR CI/main 대기. 실제 입력12개·연결 승인·실물 검증은 미확정이며 배포·Slack·장애 실행 없음.

### WF-10 활성 후보 최종 반영 · 2026-11-19

[PR #39](https://github.com/joungGo/url-shortener-ai-ops/pull/39) head `0ff46cd`를 일반 squash 병합해 main `4b54777a20880614403d19ee98b54bec9cc45263`. PR CI44471952438 7종·main CI44472709490 성공, tree `19a045d` 일치, 보호 변경/우회 없음. renderer 코드 완료와 실제 입력·연결 미완료를 구분한다. 기존 시간·승인·원복 조건 유지.

### WF-10 후보 renderer 보강 · 2026-11-19

[보강 기록](wf10-renderer-hardening-20261119.md)·[검증 JSON](wf10-renderer-hardening-validation-20261119.json): PR39 검토 잔여 위험 중 네 프로세스 livenessProbe와 init resources를 반영. 집중83·전체1586(skip1=WF03 환경 게이트) 통과. 이 커밋 시점 PR CI/main 대기. Slack egress 경로(A4)와 실제 입력·연결은 후속.

### WF-10 후보 renderer 보강 최종 반영 · 2026-11-19

[PR #40](https://github.com/joungGo/url-shortener-ai-ops/pull/40) head `6920a07` → main `1f20442e2a70eb66a6228275add9f564cdb37c06`. PR CI44474606794 7종·main CI44475106909 성공, tree `ac77ac8` 일치, 보호 변경/우회 없음. 다음 후보 A4(Slack egress 경로 설계)는 사용자 허락 후 시작.

### WF-10 Slack sender 분리(B′) · 2026-11-19

[설계 결정](wf10-slack-egress-design-20261119.md)·[구현 기록](wf10-slack-sender-local-20261119.md)·[검증 JSON](wf10-slack-sender-local-validation-20261119.json). bot 토큰은 sender Pod에만, 메인 Pod 인터넷 송신0, sender만 443(사설·IMDS 제외)·메인 Pod 수신만. 신규 시험12, 전체1598 통과(1회차 기존 flaky 1건 기록). 이 커밋 시점 PR CI/main 대기. 플랫폼·Slack 쪽 통제와 실클러스터 음성 시험은 인프라 단계.

### WF-10 Slack sender 분리 최종 반영 · 2026-11-19

[PR #41](https://github.com/joungGo/url-shortener-ai-ops/pull/41) head `5ec3f9d` → main `ceb6c277b6a968c5be046657dc04137695e1a836`. PR CI44482224455 7종·main CI44482913902 성공, tree `24a2ea8` 일치, 보호 변경/우회 없음. 플랫폼·Slack 쪽 통제와 실클러스터 음성 시험은 인프라 단계. 다음 인프라 불필요 후보는 사용자 허락 후 시작.

### SQLite 보조 파일 삭제 경쟁 수정 · 2026-11-19

[수정 기록](sqlite-sidecar-race-fix-20261119.md)·[검증 JSON](sqlite-sidecar-race-fix-validation-20261119.json): 동시 접근 시 SQLite가 지우는 중인 `-wal`/`-shm`(`nlink 0`)을 위험 파일로 오판하던 4개 저장소 검사 수정. 보안 조건 유지. 신규 시험4·부하 재현160·전체1602 통과. 이 커밋 시점 PR CI/main 대기.

### SQLite 보조 파일 경쟁 수정 최종 반영 · 2026-11-19

[PR #42](https://github.com/joungGo/url-shortener-ai-ops/pull/42) head `a45c053` → main `f6e04337239a6f981745674ff390ce8b89e37f81`. PR CI44486424872 7종·main CI44487170865 성공, tree `15e3620` 일치, 보호 변경/우회 없음.

### WF-10 manifest 오프라인 schema 검증(A5) · 2026-11-19

[검증 기록](wf10-manifest-schema-validation-20261119.md)·[검증 JSON](wf10-manifest-schema-validation-validation-20261119.json): v1.35.8 공식 OpenAPI v3 부분집합(원본 hash 고정)으로 renderer 8개 리소스를 출력 전 strict 검증. 서버 dry-run은 live gate로 유지. 신규 시험6·전체1608 통과. 이 커밋 시점 PR CI/main 대기.

### WF-10 manifest 오프라인 schema 검증 최종 반영 · 2026-11-19

[PR #43](https://github.com/joungGo/url-shortener-ai-ops/pull/43) head `552bb86` → main `5c74985973b71400000d408ae07e1cd69c410176`. PR CI44489431925 7종·main CI44490014251 성공, tree `3c3fd0d` 일치, 보호 변경/우회 없음.

### WF-10 전환·원복 변경 계획 형식(A6) · 2026-11-19

[변경 계획 기록](wf10-change-plan-20261119.md)·[검증 JSON](wf10-change-plan-validation-20261119.json): 검증된 후보 1개 → 적용 순서·리소스 hash·사전/사후 확인·uid 일치 역순 원복 묶음·계획/원복 hash. 데이터 전용·실행 불가. 신규 시험7·전체1615 통과. 이 커밋 시점 PR CI/main 대기.

### WF-10 변경 계획 형식 최종 반영 · 2026-11-19

[PR #44](https://github.com/joungGo/url-shortener-ai-ops/pull/44) head `5b699bc` → main `7b6dcb19caca9156c492cb51c19a5a3a35cfd76a`. PR CI44491923061 7종·main CI44492763462 성공, tree `d8a0beb` 일치, 보호 변경/우회 없음.

### A7-1 복구 승인 Slack 버튼 수신 · 2026-11-19

[구현 기록](recovery-slack-approval-local-20261119.md)·[검증 JSON](recovery-slack-approval-local-validation-20261119.json): 서명된 Slack 버튼 → WF06 장부 결정 기록만. 범위별 버튼·정책 쌍·결정적 request_id. 신규 시험11·전체1626 통과. 이 커밋 시점 PR CI/main 대기. A7 남은 조각: 2 결과 outbox, 3 진단·쓰기·잠금 어댑터, 4 효과·원복·회수, 5 주기 보관 정리.

### A7-1 Slack 승인 버튼 수신 최종 반영 · 2026-11-19

[PR #45](https://github.com/joungGo/url-shortener-ai-ops/pull/45) head `56e0d30` → main `0619651393b5792da03f9a86d561e502560d5e81`. PR CI44500818121 7종·main CI44501492963 성공, tree `5b9d01a` 일치, 보호 변경/우회 없음. 다음 조각 A7-2(결과 outbox)는 사용자 허락 후.

### A7-2 복구 결과 outbox · 2026-11-19

[구현 기록](recovery-outbox-local-20261119.md)·[검증 JSON](recovery-outbox-local-validation-20261119.json): 장부 상태 → 원래 thread 승인 요청·상태 알림, 정규 버튼 블록만 허용, 기존 회신 전송 규칙(재전송 없음). 신규 시험8·전체1634 통과. 이 커밋 시점 PR CI/main 대기. A7 남은 조각: 3 진단·쓰기·잠금 어댑터, 4 효과·원복·회수, 5 주기 보관 정리.

### A7-2 복구 결과 outbox 최종 반영 · 2026-11-19

[PR #46](https://github.com/joungGo/url-shortener-ai-ops/pull/46) head `b2a97a5` → main `8fa3ad2b38b88cbe31496ad81c1ede3644cb7bdb`. PR CI44504060937 7종·main CI44504869626 성공, tree `534bae6` 일치, 보호 변경/우회 없음. 다음 조각 A7-3(진단·쓰기·잠금 어댑터)은 사용자 허락 후.

### A7-3b 재시작 사전 게이트 재설계 · 2026-11-19

[구현 기록](restart-gates-local-20261119.md)·[검증 JSON](restart-gates-local-validation-20261119.json): 진단 4종 bool 주입 → `restart_gates` 결정적 판정(지속·liveness·과거 Ready·지문·의존성) + PATCH 직전 증거 보존 필수 게이트. lab-api Recreate 반영. 시험39·전체1639 통과. 이 커밋 시점 PR CI/main 대기. 다음 A7-3a 실제 K8s 어댑터(가짜 API 서버 검증).

### A7-3b 재시작 사전 게이트 최종 반영 · 2026-11-19

[PR #47](https://github.com/joungGo/url-shortener-ai-ops/pull/47) head `8c86507` → main `6a6605d11b86a7b438286f8664d3341ae39e678a`. PR CI44522308075 7종·main CI44523032690 성공, tree `713fd3e` 일치, 보호 변경/우회 없음. 다음 A7-3a 실제 K8s 어댑터는 사용자 허락 후.

### A7-3a 재시작 Kubernetes 어댑터 · 2026-11-19

[구현 기록](restart-k8s-adapter-local-20261119.md)·[검증 JSON](restart-k8s-adapter-local-validation-20261119.json): 고정 경로·신원 분리 클라이언트, 3b 원시 관측 도출(불확실하면 차단), 설정 PromQL만, 증거 비공개 저장, 봉인 PATCH, Lease 미탈취. mode=kubernetes로 실행기 거절(쓰기 차단 유지). 신규 시험15·전체1654 통과. 이 커밋 시점 PR CI/main 대기. 다음 A7-4 효과·원복·회수.

### A7-3a 재시작 Kubernetes 어댑터 최종 반영 · 2026-11-19

[PR #48](https://github.com/joungGo/url-shortener-ai-ops/pull/48) head `9124203` → main `13d94e9a3efc8432480291ef9bbc1461e6899c60`. PR CI44525442506 7종·main CI44526171563 성공, tree `d075338` 일치, 보호 변경/우회 없음. 다음 A7-4 효과·원복·회수는 사용자 허락 후.

### A7-4 재시작 효과 관찰·봉인·Lease 회수 확인 · 2026-11-19

[구현 기록](restart-effects-adapter-local-20261119.md)·[검증 JSON](restart-effects-adapter-local-validation-20261119.json): llm-council로 혼합 리더 철회 → 실행 1회 관찰 수집기(창=적용+기동 허용, 간격+읽기≤gap, K8s·Pod proxy HTTP·설정 PromQL·설정 규칙), sha256 봉인 묶음만 WF09 판정, provenance `captured`, Lease 반납 확인·자기 것만 해제. 실제 읽기/회수 차단 유지. 신규 시험17·변형 6/6·전체1671 통과. 이 커밋 시점 PR CI/main 대기. 다음 A7-5 주기 보관 정리(증거·봉인 파일 포함).

### A7-4 재시작 효과 관찰·봉인·Lease 회수 확인 최종 반영 · 2026-11-19

[PR #49](https://github.com/joungGo/url-shortener-ai-ops/pull/49) head `6e4f5ef` → main `3c00eeecc97a2b273b713bfd247f997c94461a93`. PR CI44532528034 7종·main CI44533398664 성공, tree `0e97675` 일치, 보호 변경/우회 없음. 다음 A7-5 주기 보관 정리는 사용자 허락 후.

### A7-5 복구 장부·증거·봉인 파일 주기 보관 정리 · 2026-11-19

[구현 기록](recovery-retention-local-20261119.md)·[검증 JSON](recovery-retention-local-validation-20261119.json): `RecoveryRetention.tick()`(장부 prune·outbox·증거 참조 기반·봉인 7일, 본인 0700 디렉터리의 hash 이름 파일만), outbox 요청 만료 행 삭제(본문 잔존·10000행 영구 차단 결함 수정). 신규 시험8·변형 5/5·전체1679 통과. 이 커밋 시점 PR CI/main 대기. 다음 PR 경로 원복 관측 또는 A7 프로세스 연결.

### A7-5 복구 장부·증거·봉인 파일 주기 보관 정리 최종 반영 · 2026-11-19

[PR #50](https://github.com/joungGo/url-shortener-ai-ops/pull/50) head `21b060c` → main `ce68c5bd761db02ca1941f1aa79f9612e1d8b62d`. PR CI44536959534 7종·main CI44537829472 성공, tree `3a1f590` 일치, 보호 변경/우회 없음. 다음 작업은 사용자 허락 후.

### PR 경로 원복 관측 · 2026-11-20

[구현 기록](rollback-observe-local-20261119.md)·[검증 JSON](rollback-observe-local-validation-20261119.json): 클러스터 관측으로 원복 적용 판정(Argo 승인 revision 고정·동기화·성공·Healthy·수동 sync, Deployment 승인 digest), GitHub main 일치는 인계 정보만(`rollback_note`), 관찰기 PR 영수증 확장. 실제 읽기 차단 유지. 시험8·변형 7/7·전체1687 통과. 이 커밋 시점 PR CI/main 대기. 다음: 원복 실행 조각(llm-council 결정: 기준선 고정만, 원복 전 gitops_drift_lock, main 정렬 별도 승인).

### PR 경로 원복 관측 최종 반영 · 2026-11-20

[PR #51](https://github.com/joungGo/url-shortener-ai-ops/pull/51) head `d375e4b` → main `e16b61494fa8d5644d82670a08e23ded43ab34c6`. PR CI44657902367 7종·main CI44658449019 성공, tree `f8bca14` 일치, 보호 변경/우회 없음. 다음 작업은 사용자 허락 후.

### 원복 실행 조각1 GitOps 드리프트 잠금 · 2026-11-20

[구현 기록](gitops-drift-lock-local-20261120.md)·[검증 JSON](gitops-drift-lock-local-validation-20261120.json): 원복 전 대상 잠금, 잠금 중 WF07 준비·게시·preflight·병합·동기화 fail-closed(상태 변경 트랜잭션 안), 해제=main `ai-ops/lab` 트리==기준선 트리. 시험9·변형 7/7·전체1696 통과. 이 커밋 시점 PR CI/main 대기. 다음 조각2 원복 실행기.

### 원복 실행 조각1 GitOps 드리프트 잠금 최종 반영 · 2026-11-20

[PR #52](https://github.com/joungGo/url-shortener-ai-ops/pull/52) head `d7fd6c8` → main `564571836e33c71540313645308c258a99d3e126`. PR CI44665694359 7종·main CI44666355792 성공, tree `98eab6f` 일치, 보호 변경/우회 없음. 다음 조각2 원복 실행기는 사용자 허락 후.

### 원복 실행 조각2 기준선 고정 원복 실행기 · 2026-11-20

[구현 기록](rollback-executor-local-20261120.md)·[검증 JSON](rollback-executor-local-validation-20261120.json): 계획당 1회, 쓰기 직전 재검증(이미지 digest만 되감기는지 Git 트리 비교·Argo source 정확·수동 sync·operation 없음·Lease), 쓰기 전 드리프트 잠금, pin 1회, 실패=시도 소진→수동 인계. local_fixture만. 시험11·변형 9/9·전체1707 통과. 이 커밋 시점 PR CI/main 대기. 다음: A7 프로세스 연결 또는 A8 WF12 문서.

### 원복 실행 조각2 기준선 고정 원복 실행기 최종 반영 · 2026-11-20

[PR #53](https://github.com/joungGo/url-shortener-ai-ops/pull/53) head `f9267cf` → main `9e70a3dd96fd1d51453b3b61afba64797c5791da`. PR CI44669209443 7종·main CI44669722156 성공, tree `b621d1a` 일치, 보호 변경/우회 없음. 다음 작업은 사용자 허락 후.

### A7 프로세스 연결 조각1 Slack 복구 승인 경로 · 2026-11-20

[구현 기록](recovery-approval-wiring-local-20261120.md)·[검증 JSON](recovery-approval-wiring-local-validation-20261120.json): 사용자 결정(승인 대기=명시 명령). `request-approval <사건|this> <계획hash>` → 계획 요청자·같은 스레드/사건 확인 → WF06 `open`; 계획 hash→요청 색인; 장부 `page`→outbox 동기화(5초)·틱당 전송1; ingress `SlackRouter`로 `/slack/interactivity`(서명 키만); `recovery-approval.json` 없거나 꺼지면 기존 동작. 관측 포트 미연결(항상 실패)이라 실제 배포에선 대기 미개시(fail-closed). 재검증·PR 준비·execute_command·보관 tick·renderer 마운트는 후속. 시험17·변형 12/12 통과. 이 커밋 시점 PR CI/main 대기.

### A7 프로세스 연결 조각1 Slack 복구 승인 경로 최종 반영 · 2026-11-20

[PR #54](https://github.com/joungGo/url-shortener-ai-ops/pull/54) head `73b8a53` → main `46789b11434736aad2531a22f40605dad44fe57c`. PR CI44724810023 7종·main CI44728486857 성공, tree `43bd1aa` 일치, 보호 변경/우회 없음. 다음: A7 조각2 보관 정리 tick 연결.

### A7 프로세스 연결 조각2 복구 보관 정리 tick · 2026-11-20

[구현 기록](recovery-retention-wiring-local-20261120.md)·[검증 JSON](recovery-retention-wiring-local-validation-20261120.json): relay가 기존 요청 정리 뒤 10분마다 `RecoveryRetention.tick`(켜짐: 승인 장부 tombstone·outbox / 꺼짐: 남은 outbox만, 테이블 생성 없음). 종료 중 미실행·실패 격리. 증거·봉인 디렉터리는 재시작 경로 연결 때 추가. 시험7(모듈 24)·변형 10/10. 이 커밋 시점 PR CI/main 대기.

### A7 프로세스 연결 조각2 복구 보관 정리 tick 최종 반영 · 2026-11-21

[PR #55](https://github.com/joungGo/url-shortener-ai-ops/pull/55) head `b973505` → main `a5f59dc085b08114baeb44d950ab616e709dcace`. PR CI44730619628 7종 성공, tree `60ae06e` 일치, 보호 변경/우회 없음. 다음 A7 후보: 승인 뒤 재검증·WF07 연결 또는 PR 경로 현재 상태 관측기.

### A7 프로세스 연결 조각3 승인 뒤 자동 재검증·WF07 PR 준비 · 2026-11-21

[구현 기록](recovery-advance-wiring-local-20261121.md)·[검증 JSON](recovery-advance-wiring-local-validation-20261121.json): 사용자 결정(재검증=승인 후 자동, WF07=자동 준비까지). relay 5초 주기 `advance_once` 1단계(approved→revalidate, proposal_ready·WF07 기록 없음→`prepare`, 반복 없음), 재검증 시간 ≤15초. WF07 결과 고정 문구 알림(URL·head·diff 없음), WF07 기록 보관 정리 포함. 진입점은 WF07 포트 미전달(운영 미연결)·관측 미연결 유지. GitHub 쓰기 없음. 시험11(모듈35)·변형 13/13. 이 커밋 시점 PR CI/main 대기.

### A7 프로세스 연결 조각3 승인 뒤 자동 재검증·WF07 준비 최종 반영 · 2026-11-21

[PR #56](https://github.com/joungGo/url-shortener-ai-ops/pull/56) head `e61c442` → main `83b8e39efe3ce02405b1e1e3bb73a49b41471d3f`. PR CI44881773956 7종 성공, tree `3f2a4bf` 일치, 보호 변경/우회 없음. 다음: PR 경로 현재 상태 관측기(사용자 허락).

### A7 PR 경로 현재 상태 관측기 · 2026-11-21

[구현 기록](recovery-observer-local-20261121.md)·[검증 JSON](recovery-observer-local-validation-20261121.json): 사용자 결정(실패=현재 컨테이너 상태, healthy 판정 안 함, 설정 있을 때만 연결). `ai_ops.recovery_observe`: 고정 GET 4개(Deployment·ReplicaSets·Pods·Argo app) → selector·소유 체인·이미지 일치·`ErrImagePull`/`ImagePullBackOff`·ready=false·readyReplicas 0·최신 generation이면 `image_failure`, 아니면 `unknown`, revision=targetRevision==sync.revision. `recovery-approval.json`의 선택적 `observe` 절이 있을 때만 relay·ingress 사용. 연결 단계 RBAC: deployments get·replicasets list(신규)·pods list·Argo applications get. 시험15·변형 16/16. 이 커밋 시점 PR CI/main 대기.
