# WF07 PR 발행용 단기 토큰 도구 (A2, PR-e) · 2026-11-27

상태: 로컬 구현과 시험만 했다. 실제 발급·폐기는 다음 설치의 S1에서 한다.

## 목적
- A2(사용자 결정 2026-11-26)에서 relay는 C03 writer 앱의 **단기 설치 토큰**으로 PR을 연다(PR #82·#83).
- 앱 개인키는 운영자 Mac에만 둔다.
- 이 도구는 실습 직전에 토큰을 발급해 WF10 Secret에 넣고, 실습 뒤에 폐기한다.

## 사용
```
python -m ai_ops.recovery_pr_token issue  --namespace ai-ops --secret ai-ops-pr-writer [--kubeconfig …]
python -m ai_ops.recovery_pr_token revoke --namespace ai-ops --secret ai-ops-pr-writer [--kubeconfig …]
```
키 기본 경로는 `~/.local/share/url-shortener-ai-ops/c03-app-keys/writer.pem`이다.

## 동작
- **issue**
  1. 같은 Secret이 있으면 거부한다. 살아 있는 토큰을 덮어쓰지 않으려는 것이다.
  2. `openssl`로 앱 JWT에 서명한다(유효 5분). 키는 Python으로 읽지 않는다.
  3. `POST /app/installations/180499782/access_tokens`를 보낸다. 저장소는 `joungGo/c03-execution-lab`(ID 1452572723) 하나, 권한은 `contents: write`·`pull_requests: write`만 요청한다.
  4. 응답 범위를 검사한다.
     - 권한은 요청한 두 개(+GitHub가 붙이는 `metadata: read`)여야 한다.
     - `repository_selection=selected`이고, 저장소 1개의 ID와 이름이 모두 일치해야 한다.
     - 만료는 1시간 이내여야 한다.
     - 어긋나면 **즉시 폐기**하고 Secret을 만들지 않는다.
  5. Secret은 `kubectl apply -f -`(stdin)로만 만든다. 토큰은 인자·파일·출력에 남지 않는다. Secret 저장이 실패해도 토큰을 즉시 폐기한다.
- **revoke**
  1. Secret에서 토큰을 읽는다(비출력).
  2. `DELETE /installation/token`을 보낸다.
     - 204면 폐기, 401이면 이미 만료 또는 폐기된 상태다. 이때만 Secret을 지운다.
     - 다른 응답이면 Secret을 남기고 오류를 낸다.
- **보내는 요청은 위 두 개뿐이다.** 출력은 고정 JSON(상태, 만료 시각)만 내고, 실패도 고정 코드로만 알린다.
- C-04 실습 코드(`c_trial_*`)를 재사용하지 않는다(WF11 절차).
- 고정 값은 2026-11-27 JWT 읽기 전용 조회로 확인했다. 앱 `c03-execution-lab-writer`(5696038), 설치 180499782(활성, selected, contents·pull_requests write, metadata read)다.

## 검증
- 새 시험 6개(`test_recovery_pr_token.py`)
  - 좁은 범위 요청, JWT iss·수명, stdin 저장, 인자·결과에 토큰 없음
  - 넓은 권한·관리 권한·전체 저장소·다른 저장소·같은 이름 다른 ID·2개 저장소·긴 수명·만료된 응답은 즉시 폐기하고 Secret을 만들지 않음
  - Secret이 이미 있으면 발급하지 않음. 저장 실패 시 폐기. HTTP 오류 코드
  - 폐기 순서(토큰 → Secret), 401 처리, 없음, 실패 시 Secret 유지
  - 키 권한, 이름 형식, 고정 요청
  - CLI 고정 출력
- 결함 변형 14/14 검출. 처음 살아남은 1개(저장소 ID 검사 제거)는 "같은 이름 다른 ID" 사례를 추가해 잡았다.
- 실제 `openssl`과 writer 키로 JWT 서명이 되는지 로컬에서 확인했다(네트워크 없음).
