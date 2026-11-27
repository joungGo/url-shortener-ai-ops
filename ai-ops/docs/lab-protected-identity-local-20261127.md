# lab 설치 도구 B 전제 제거 (WF11 후속 5) · 2026-11-27

상태: 로컬 구현과 시험만 했다. 실제 사용은 다음 설치의 lab 설치(키트 15단계)에서 한다.

## 문제
- lab 설치와 lab 시나리오는 작업 전후에 "운영 자원 불변"을 확인한다. 확인 목록은 다음 3개였다.
  - `url-shortener/url-shortener-api` Deployment(운영 앱)
  - `ai-ops/ai-ops` StatefulSet(예전 B 방식 AI worker)
  - `ai-ops/data-ai-ops-0` PVC(B 저장소)
- WF10만 설치한 환경에는 뒤의 두 객체가 없다. 그래서 조회가 실패했다.
  - 2026-11-26 설치에서는 키트 wrapper(`deploy_lab_wf11.py`)가 두 항목을 빼고 진행했다([실행 기록](wf11-execution-20261126.md) 6번).
- 같은 목록이 `scripts/deploy_b6_lab.py`와 `b6_scenarios.LabClient.production()` 두 곳에 있었다.
  - 뒤의 것은 GitOps 등록 준비, 장애 시나리오, 순서 관측에서도 쓴다.
- 정상 CI 실행(`normal_ci_runner`)은 자체 `guard()`를 써서 이 문제와 무관하다.

## 변경
- `b6_lab.py`에 공통 정의를 두었다.
  - **필수** `PROTECTED_REQUIRED`: 운영 앱 `url-shortener-api`. 없으면 이전처럼 실패한다.
  - **선택** `PROTECTED_OPTIONAL`: 있으면 확인한다.
    - WF10: `ai-ops/ai-ops` Deployment, `ai-ops/ai-ops-data` PVC
    - 예전 B: StatefulSet, PVC
- `protected_identity(command)`
  - 선택 객체는 `--ignore-not-found`로 읽고, 없으면 `{"absent": true}`로 기록한다.
  - 필수 객체는 `--ignore-not-found` 없이 읽는다.
  - 열쇠는 `namespace/kind/name`이다. 이름이 같은 `ai-ops` Deployment와 StatefulSet이 겹치지 않는다.
  - 보내는 명령은 `get`뿐이다.
- 전후 비교는 그대로다. 객체가 생기거나 없어지거나 uid·generation이 바뀌면 실패한다.
  - 그래서 lab을 WF10보다 먼저 설치해도, 나중에 설치해도 보호가 유지된다.
- `deploy_b6_lab.production_identity()`와 `LabClient.production()`이 이 정의를 함께 쓴다.

## 검증
- 새 시험 7개(`test_b6_protected_identity.py`, 가짜 kubectl)
  - WF10만 있는 설치, 예전 B 설치, WF10 전 lab 설치가 모두 통과한다.
  - 생김·없어짐·generation 변경을 검출한다. 운영 앱이 없으면 실패한다.
  - 두 사용처가 같은 결과를 내고, 고정 context로 `get`만 보낸다.
  - 설치 중 보호 객체가 바뀌면 설치가 실패한다.
- 기존 lab 관련 시험 67개 그대로 통과.
- 결함 변형 14/14 검출.
- 전체 시험 1964개 통과(skip 1). 저장소 경계 검사 통과.

## 남은 일
- 재설치 키트: `deploy_lab_wf11.py` wrapper를 없애고 `install-lab.py`가 `deploy_b6_lab.py`를 직접 부르게 바꾼다(병합 후, 저장소 밖 개인 자료).
