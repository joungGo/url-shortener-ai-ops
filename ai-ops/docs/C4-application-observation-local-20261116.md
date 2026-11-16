# C-04 Application 관측 갱신 비교 수정 · 로컬 검증

## 2026-11-16 Application 자연 관측 갱신 재현·로컬 수정 완료 · 미반영

사용자 지시에 따라 **장애·refresh·sync 없이 정상 lab-target Application에 GET만10회** 수행했다. 17:52:07→17:55:27 KST의 자연 갱신에서 UID/spec/정상Healthy 상태는 그대로이며 **generation+1·resourceVersion·reconciledAt·managedFields[2].time**만 바뀌었다. 관리자는 `argocd-application-controller`, operation Update, API argoproj.io/v1alpha1, FieldsV1 소유권은 같고 기존time이 이전reconciledAt와, 새time이 새reconciledAt와 정확히 일치했다. 원본전후를 개인폴더에만600으로 보존했다. [Kubernetes API 설명](https://kubernetes.io/docs/reference/kubernetes-api/definitions/managed-fields-entry-v1-meta/)도 소유필드 값 변경 때 관리time이 갱신됨을 명시한다.

같은 실제 원본쌍을 a002dee 비교기에 재생하면 거절, 수정기에는 통과함을 확인했다. **기존 비교기가 허용하던 관측 시각 갱신에 동반된 controller 관리time을 전체원본 변화로 거절하는 문제**다. 004의 과거 원본값은 없어 그때의 정확한관리항목까지 소급확정하지 않는다. 이번에는 같은 네경로 변화를 정상상태로 재현한 근거를 보존했다.

AI main a002dee 기준 **/private/tmp/c04-application-observation-20261116**·branch codex/c04-application-observation-20261116에서 수정했다. Application 타입·유일한기존controller Update항목·FieldsV1 소유권·양쪽관측시각 결합·generation정확히+1·새관측시각 비미래/엄격증가·불투명버전변경을 모두 확인한 경우에만 그항목time 하나를 비교용deepcopy에서 구분한다. managedFields 전체를 제외하지 않고 관리자/소유권/항목순서/추가삭제/다른time과 UID/spec/health/기타metadata·Deployment·Lease 전체 변경은 계속 거절한다. manager이름만으로 실행권한을 부여하지 않는다.

**전체922개·집중47개·새10개 회귀시험/skip0**, 실제kubectl v1.34.1/로컬TLS의 원본형식·인증갱신/CAS/양쪽ACK·CA/경로/메서드/Host거절·SIGTERM회수, AST294파일·fixture4종/schema변경0·저장소경계/Helm배포정의를 통과했다. 기존SQLite ResourceWarning1건은 남았다. 실행/승인/기한/잠금/원복제어9파일은 a002dee와 byte동일하고 Deployment/Lease 전체비교본문도 동일하다. 15초·실행별승인·정확head 실제사람리뷰/CI·리뷰120/210초·원복240/300초·관찰120초를 유지했다.

**미커밋·미push·미병합·미배포, 새자격/정상CI/감시/실습/Terraform/모델호출은 없다.** agent의클러스터변경은0이며 controller의 자연상태갱신만 읽었다. 개인보존 **/Users/jounghyeon/.local/share/url-shortener-ai-ops/c04-application-observation-local-20261116**의 validation.json·read-only-reproduction.json·exact-pair-replay.json·원본 *.private.json·로그·source/3파일/해시·patch를 따른다. 원본자료를 Git/채팅에 복사하지 않는다.

다음은 코드검토·CI·반영이다. 이후 현재앱Pod binding/이름제한권한·새정상CI·새감시/새readiness부터 재검증하며 새실습은별도실행승인이 필요하다. 004/PR17은 종료돼 승인/리뷰를 재사용하지 않는다. C04/C05 최종·A5/A7·전체실행저장소전환은 미완료, AI포트폴리오보류 유지. 다음 추천 **GPT-6 SOL · High**.
