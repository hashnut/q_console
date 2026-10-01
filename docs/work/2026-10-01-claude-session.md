# Claude 5시간 오버레이 복구

- 시작: 2026-10-01 (Asia/Seoul), HEAD `4a57292d74fb78889730122e2b8ec37f70bf74d6`.
- 저장소: `hashnut/q_console`, `main`. 기존 변경 없음. `git pull --ff-only`: Already up to date.
- 목표: Claude 주간·현재 세션(5시간) 사용량과 개별 재설정 시간을 오버레이에서 확인한다. 수정 EXE/ZIP을 갱신하고 커밋·푸시한다.
- 범위: Claude 응답 파싱, 실패 시 세션 표시, 요청 제한 대기, 오버레이 가독성/폭, 관련 테스트·README·배포물.
- 확인: 기존 소스는 `session`/기존 `five_hour` 추출과 표시를 구현했지만 실계정 조회는 HTTP 429. 현재 캐시에는 이전 주간 값만 있으며 `_session_limit`이 알 수 없는 세션을 제거한다. 1분 주기 조회가 요청 제한 중에도 계속된다.
- QA 계획: 응답 변형·0%·실패·세션 재설정·API/Enterprise 구분·재시도 단위 테스트, 실제 WebView2 CapturePreviewAsync/DOM으로 텍스트와 잘림 확인, 배포 EXE 캐시 생성 확인, git diff 검사.
- 구현: 구독 계정의 5시간 항목은 조회 실패에도 `--`로 유지한다. 요청 제한 중에는 `조회 대기`와 원인·재조회 시간 툴팁을 표시한다. 주간은 `Claude 주간`으로 구분한다. API 키·월간 Enterprise에는 구독의 5시간 항목을 넣지 않는다.
- 재시도: HTTP 429의 Retry-After(초/HTTP-date)를 읽는다. 안내가 없으면 5·10·20·30분 간격으로 대기한다. 매번 새로 실행되는 worker가 캐시에서 대기 상태를 이어받고, 로그인 토큰이 바뀌면 즉시 새 로그인으로 조회한다. 캐시에는 토큰 원문을 저장하지 않는다.
- UI 검사 중 발견: Claude만 표시하는 좁은 조합에서 scrollWidth가 clientWidth보다 5px 컸다. 한글·폰트·DPI 여유 16px를 추가한 후 15가지 선택 조합 모두 통과했다.
- QA 캡처 경로: 최초 WinForms Activated 이벤트 검사는 실제 OS 전경과 구분하지 못했다. QA 창에 WS_EX_NOACTIVATE를 적용하고 foreground HWND로 검사하도록 수정했다. 화면 픽셀 캡처는 빈 이미지여서 증거로 사용하지 않았고, 실제 WebView2 CapturePreviewAsync 및 DOM 경계를 검증했다.
- 사용 가이드·실행물: README의 기존 “5시간 창 미표시” 설명을 수정하고 테스트 데이터의 새 오버레이 예시를 교체했다. 사용자 규칙에 따라 소스 런처를 `q_console.bat`(CRLF)로 바꾼다. PyInstaller EXE와 ZIP을 갱신한다.

## QA

- PASS: `python -X utf8 -m unittest discover -s tests -q` — 65개. 0%·응답 호환·실패·독립 세션 재설정·구독/API/Enterprise 구분·재시도 지속/해제·네이티브 오버레이 복구/포커스 포함.
- PASS: `python tools/make_overlay_fixtures.py release/session-final-qa` 후 `powershell.exe -STA -NoProfile -ExecutionPolicy Bypass -File tools/qa_overlay.ps1 -AppHome release/session-final-qa` — 15개 선택 조합, 예시, 현재 실패 상태의 총 17개 실제 WebView2 렌더. DOM 잘림 없음. 예시·조회 대기 캡처를 직접 열어 확인.
- PASS: PyInstaller 빌드 및 최종 EXE의 격리된 `%LOCALAPPDATA%` 배경 worker 실행. 테스트 캐시의 주간 29%·세션 2%를 각 창의 stale 값으로 유지하고 재시도 상태를 이어받는 것을 확인했다. 실제 사용자 캐시는 테스트 데이터로 덮어쓰지 않았다.
- PASS: 현재 트레이의 worker 경로에 최종 EXE 반영, `--refresh` exit 0. 별도 트레이 재시작 없이 적용했다.
- PASS: `q_console.zip` 무결성 및 압축 안의 EXE와 루트 EXE가 바이트 단위로 일치. 최종 EXE SHA256 `3698ed2cc2f5b3a19cbd254989ef91d8abfddc87ab11750de220c813fae272f8`.
- PASS: `git diff --check`. 구현·테스트·문서·예시 캡처·BAT·EXE·ZIP만 커밋 대상이다.
- 외부 제한: 실제 계정 Usage는 HTTP 429를 반환하며, 캐시에는 이전 주간 27%만 있다. 실제 5시간 퍼센트의 최신값 수신은 아직 확인하지 못했다. 사용자의 스크린샷 값을 실제 캐시에 넣거나 현재값으로 꾸미지 않는다. 수정 worker는 `5h -- (조회 대기)`를 표시하며 제한 대기 후 자동 재조회한다.

## 이어받기

- 현재 트레이는 프로젝트 루트 `q_console.exe`를 worker로 사용한다. EXE 교체 후 실행 중 트레이의 다음 갱신에도 수정 코드가 적용된다.
- 실제 계정 수신을 확인하려면 자동 재조회 후 `%LOCALAPPDATA%/q_console/usage-cache.json`의 Claude session을 확인한다. 요청 제한 중 Refresh를 반복해도 대기 상태는 유지한다.
