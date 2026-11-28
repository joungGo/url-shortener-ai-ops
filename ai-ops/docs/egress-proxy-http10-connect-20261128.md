# 송신 proxy: Python 3.11 클라이언트의 헤더 없는 HTTP/1.0 CONNECT 허용 · 2026-11-28

상태: 2026-11-28 재설치 중 실물에서 발견해 고쳤다.

## 문제
- WF10 `ci-relay`가 `ci_scan_unavailable`만 반복했다. 원인은 `URLError: Tunnel connection failed: 403 Forbidden`이었다.
- 이미지(Python 3.11.17, urllib3 2.8.0) 안에서 가짜 proxy로 캡처하니, urllib과 urllib3(boto3) 모두 `CONNECT host:443 HTTP/1.0\r\n\r\n`처럼 **헤더 없이** 보냈다.
- 송신 proxy(`parse_connect`)는 요청 줄과 Host 헤더, 정확히 2줄만 받아서 이를 모두 거부했다.
- 그래서 proxy를 거치는 클라이언트(CI relay, PR writer, WF07 GitHub·STS·ECR 읽기)가 실제 환경에서 모두 막혀 있었다.
- 로컬 시험은 Python 3.13에서 돌았다. 3.12 이상은 Host 헤더를 붙이므로 드러나지 않았다.

## 변경
- `parse_connect`가 **HTTP/1.0이고 헤더가 하나도 없는** CONNECT도 받는다.
- 대상은 여전히 요청 줄의 허용 목록으로만 정해진다.
- HTTP/1.1은 계속 같은 값의 Host 헤더가 있어야 한다.
- Host가 아닌 헤더, 다른 값의 Host, 허용하지 않은 대상·포트는 그대로 거부한다.

## 검증
- 이미지에서 캡처한 바이트 그대로(GitHub·STS·artifact 저장소)를 받는다. 헤더 없는 HTTP/1.1, 허용 밖 대상, 다른 헤더는 거부한다.
- 결함 변형 2/2 검출(헤더 없는 HTTP/1.1 허용, 헤더 없는 요청 다시 거부). 관련 시험 23개 통과.
