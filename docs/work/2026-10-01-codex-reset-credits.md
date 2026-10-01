# Codex 리셋권 수량 표시 수정

- 시작: 2026-10-01 (Asia/Seoul), HEAD `1b79bb399e36f6cdf0b4f0a3505f67bd0af209ac`.
- 저장소: `hashnut/q_console`, `main`. 기존 변경 없음. `git pull --ff-only`: Already up to date.
- 목표: Codex 사용량 웹페이지와 q_console에서 같은 리셋권 수량을 표시하고 EXE/ZIP을 갱신한 뒤 커밋·푸시한다.
- 재현·원인: 실제 Usage 응답은 `available_count=2`, `applicable_available_count=0`. 조회 전용 리셋권 목록에서도 `status=available` 2개를 확인했다. 표시 함수가 두 공급자 모두 적용 가능 수량만 사용해 Codex를 0개로 표시한다.
- 범위: Codex 화면 모델의 표시 수량, 공통 텍스트·만료 툴팁, 회귀 테스트, README, 실제 화면 증거 및 배포물. Claude의 `usable_now` 기준은 유지한다.
- 완료 조건: Codex 보유 2개/적용 가능 0개 응답이 모든 테마·오버레이·요약에서 `리셋권 2개`로 표시되고 두 만료일 툴팁이 나온다. 0개·미제공·조회 실패와 Claude 수량도 올바르다. 최종 EXE의 실제 계정 조회 및 ZIP 내용 일치를 확인한다.
- QA 계획: 단위 테스트, 비전경 WebView2 렌더/잘림/캡처, 최종 EXE 배경 refresh, ZIP 무결성, diff 검사 및 push 후 원격 SHA 비교.
- 초기 QA: NOT_RUN. API 조회만으로 원인을 확인했으며 인증값·리셋권 ID는 기록하지 않았다. 리셋권 소비 요청 없음.

## 수정 및 재현

- 회귀 테스트를 먼저 추가해 기존 코드에서 `리셋권 0개 != 리셋권 2개` 실패를 재현했다. 보유 0개/미제공인데 적용 가능 수량이 있으면 그 수량을 잘못 보여 주는 경우도 재현했다.
- 화면 모델에 Codex의 `display_count=available_count`를 복사해 원본 수량을 보존한다. 공통 텍스트와 만료 툴팁이 이 표시 수량을 사용하며, Claude는 기존 적용 가능 수량 기준을 사용한다. 기존 조회 전용 요청 횟수와 목록 필터는 유지한다.
- 이전 [Claude 작업 기록](2026-10-01-claude-session.md)의 Codex 0개 검증은 당시 결과이며 이번 수정으로 대체한다.

## QA 및 배포물

- PASS: `python -X utf8 -m unittest discover -s tests -q` — 76개. Codex 보유 2개/적용 가능 0개, 보유 0개, 미제공, Usage 실패, 만료일 조회 실패, 전체 테마·요약·툴팁, Claude 수량 보존을 검사했다.
- 테스트 데이터 수정: 새 테스트의 만료일을 처음에는 epoch 정수로 작성해 목록 파서가 인식하지 못했다. 실제 서버 형식인 ISO 8601 문자열로 바꾼 뒤 통과했다. 제품 파서를 변경하지 않았다.
- PASS: `python -m PyInstaller q_console.spec --noconfirm` — Python 3.14.3 / PyInstaller 6.22.2. `dist/q_console.exe`와 루트 EXE 바이트 일치.
- PASS: 최종 루트 EXE를 `subprocess.CREATE_NO_WINDOW`로 `--refresh` 실행, exit 0. 실행 중인 트레이의 worker 경로가 프로젝트 루트 EXE임을 확인했다. 실제 캐시의 Codex status ok, `available_count=2`, `applicable_available_count=0`, `display_count=2`. 모든 테마와 요약에서 `리셋권 2개` 확인.
- PASS: 실제 만료일 두 개는 한국 시각 10월 23일 05:42, 10월 30일 03:59이며 사용자 제공 웹페이지의 10월 23일/30일과 일치한다. 만료 툴팁도 함께 표시된다.
- 검증 스크립트 수정: PowerShell 파이프 기본 인코딩으로 한글 기대값이 손상돼 최초 캐시 검사가 실패했다. `$OutputEncoding`을 UTF-8로 설정한 뒤 같은 최종 EXE 결과를 재검사해 통과했다. 추가 refresh 없이 확인했다.
- PASS: `python tools/make_overlay_fixtures.py release/codex-reset-qa` 후 `powershell.exe -STA -NoProfile -ExecutionPolicy Bypass -File tools/qa_overlay.ps1 -AppHome release/codex-reset-qa` — 15개 선택 조합·예시·실계정 총 17개 WebView2 렌더. 잘림 없음, 전경 활성화 없음. 실제 캡처를 직접 열어 Codex 2개를 확인하고 [오버레이 증거](../overlay.png)를 갱신했다. 실제 계정 캐시를 테스트 데이터로 덮어쓰지 않았다.
- PASS: `q_console.zip` 무결성 및 포함 EXE 바이트 일치. EXE SHA256 `d73726672323e81be9eb9d42e19e2c7281dfa4aa2c482beb1ae1f3bc56496318`.
- PASS: `git diff --check`. 변경 범위는 화면 모델·회귀 테스트·문서·실측 오버레이·EXE·ZIP이다. 인증값을 추가하지 않았고 리셋권을 소비하지 않았다.

## 완료 및 이어받기

- 기능 완료 조건을 모두 확인했다. 실제 계정 조회와 실행 파일 갱신이 완료됐으며 트레이를 재시작하지 않고 적용했다.
- 이번 변경은 수량 표시와 툴팁에 한정된다. 리셋권 소비 버튼이나 소비 동작은 검사 대상이 아니다.
- 공유: 사용자 요청에 따라 관련 파일을 함께 커밋·푸시하고 원격 main SHA와 로컬 HEAD를 비교한다. 최종 게시 결과는 작업 종료 응답에 보고한다.
