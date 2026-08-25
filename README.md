# q_console

Windows 트레이/오버레이로 **Claude Code · Fable · Codex 계정의 주간 사용률**을 보여주는 작은 도구입니다.

A tiny Windows tray/overlay app that shows the **weekly account usage** of Claude Code, Fable, and Codex.

![대시보드 (phosphor 테마)](docs/dashboard-phosphor.png)

오버레이는 화면 구석에 한 줄로 붙어 있습니다:

![오버레이](docs/overlay.png)

표시하는 것은 세 가지뿐입니다:

| 항목 | 출처 |
|---|---|
| Claude Code | Claude Usage의 All models 주간 사용률 |
| Fable | Claude Usage의 Fable 전용 주간 사용률 |
| Codex | ChatGPT Usage의 Weekly usage limit |

금액, 공개 단가 환산, 로컬 토큰 합계, 5시간 창은 표시하지 않습니다.
(구독 없이 **API 키**로 쓰는 경우에는 표시할 플랜 한도가 없으므로, 대신 이번 달 사용량을
직접 정한 예산과 비교해 보여줍니다. → [API 키 모드](#api-키-모드))

## 설치 / 실행

### 빌드된 EXE (권장)

[Releases](../../releases)에서 `q_console-windows-x64.zip`을 받아 압축을 풀고 `q_console.exe`를 더블클릭하면 트레이 + 우측 하단 오버레이로 실행됩니다.

```
q_console.exe --open                 트레이 + 대시보드 창 바로 열기
q_console.exe --install-autostart    Windows 시작 시 자동 실행
q_console.exe --uninstall-autostart
q_console.exe --install-startmenu
q_console.exe --install-webview2     WebView2 런타임 설치 재시도 (보통 불필요)
```

첫 실행 때 이 PC에 **WebView2 런타임이 없으면** Microsoft 공식 부트스트래퍼를
내려받아 조용히(관리자 권한 없이, 사용자 단위로) 한 번 설치합니다. 다운로드는 서명을
검사한 뒤에만 실행하고, 시도 여부를 `%LOCALAPPDATA%\q_console\webview2-install.json`에
기록해 매 실행마다 반복하지 않습니다. 런타임이 이미 있으면 아무것도 하지 않습니다.

### 소스 실행 (Python 3.10+)

```
q_console.cmd            트레이만 (콘솔 창 없음)
q_console.cmd --open     트레이 + 대시보드
q_console.cmd --print    텍스트 사용률 출력
```

트레이 아이콘 **좌클릭 = 대시보드**, **우클릭 = Refresh / Theme / Overlay / Always on Top / Exit**.
30분마다 자동 갱신하며 Refresh를 누르면 즉시 다시 조회합니다. 첫 실행 기본값은 Overlay ON입니다.

## 데이터 기준

세 값 모두 로컬 로그 추정치가 아니라, 설치된 클라이언트가 사용하는 **읽기 전용 계정 Usage 응답값**입니다.

- **Claude / Fable**: `~/.claude/.credentials.json`의 현재 Claude Code 로그인을 이용해 Anthropic 계정 Usage를 조회합니다. weekly_all과 Fable weekly_scoped를 각각 별도 퍼센트로 표시합니다.
- **Codex**: `~/.codex/auth.json`의 현재 ChatGPT 로그인을 이용해 Codex 계정 Usage의 7일 창을 조회합니다.

q_console은 두 자격 증명 파일을 **읽기만** 합니다. 토큰을 config/cache/html에 저장하지 않고, 자격 증명 갱신도 하지 않습니다. Claude Code 또는 Codex 앱이 로그인을 갱신하면 다음 Refresh가 새 자격 증명을 읽습니다.

조회에 실패하면(가장 흔한 원인은 Claude Code를 오래 안 켜 둬서 OAuth 액세스 토큰이
만료된 상태 - 서버가 HTTP 401을 돌려줍니다) **직전 실측값을 유지**하고 `*`와 흐린 색으로
표시합니다. 값이 더 이상 참일 수 없게 되면 - 그 주간 창이 이미 리셋됐거나 24시간
(`stale_max_age_sec`)이 지나면 - 유지하지 않고 `--`로 돌아갑니다. Claude Code를 다시 열면
토큰이 갱신되어 다음 갱신부터 정상값으로 돌아옵니다. 0%는 서버가 실제로 0을 반환했을
때만 표시합니다.

## API 키 모드

구독 로그인이 없고 **API 키**(`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` 환경변수 또는
Claude Code `settings.json`의 `env`, Codex는 `~/.codex/auth.json`의 `OPENAI_API_KEY`)만
있으면 자동으로 이 모드로 전환됩니다. API 키에는 "주간 플랜 몇 %" 같은 값이 존재하지
않기 때문에, 대신 **이번 달(달력 기준) 사용량 ÷ 내가 정한 예산**을 퍼센트로 보여줍니다.

| 항목 | 단위 | 출처 |
|---|---|---|
| Claude Code / Fable | USD | 이 PC의 `~/.claude/projects` 세션 로그를 공개 단가로 환산 |
| Codex | 토큰 | 이 PC의 `~/.codex/sessions` 롤아웃 토큰 합계 |

```
q_console.exe --set-budget claude=250          월 예산 250 USD
q_console.exe --set-budget codex=3000000000    월 예산 30억 토큰
q_console.exe --set-usage-mode api_key         자동 감지 대신 강제 (auto|subscription|api_key)
```

- 예산을 0으로 두면 퍼센트 대신 `--`가 뜹니다(없는 기준으로 비율을 지어내지 않습니다).
  실제 사용량 금액/토큰은 예산과 무관하게 카드에 항상 표시됩니다.
- Codex를 달러가 아닌 토큰으로 보여주는 이유: q_console에는 OpenAI 단가표가 없고,
  없는 단가를 추측해 금액처럼 적으면 그건 사실이 아니라 추정입니다.
- **조직 Admin 키**(`sk-ant-admin01-...`)를 `ANTHROPIC_ADMIN_KEY` 환경변수(권장) 또는
  설정의 `anthropic_admin_key`에 두면, 로컬 로그 환산 대신 Anthropic
  [Cost API](https://platform.claude.com/docs/en/manage-claude/usage-cost-api)의 **실제
  청구액**을 사용합니다. 이 키는 조직 계정에서만 발급되며 개인 계정에는 없습니다.
  실패하면 조용히 로컬 로그 환산으로 되돌아갑니다.
- 로컬 로그 환산은 **이 PC의 CLI 작업만** 봅니다. 다른 기기나 다른 앱에서 같은 키로 쓴
  API 호출은 포함되지 않습니다(그건 Admin 키 경로에서만 보입니다).

## 화면

대시보드에는 Claude Code / Fable / Codex 카드 세 개가 있고, 각 카드에 현재 주간 사용률 %, 리셋까지 남은 시간, 실측 배지가 표시됩니다. 오버레이는 한 줄로 세 사용률과 리셋 잔여 시간을 보여줍니다. (위 스크린샷 참고)

테마: `Surfacer` / `HUD (phosphor)` / `Mini`.

## 문제 해결

- **값이 `--`**: 해당 앱에서 로그아웃됐거나 네트워크 조회 실패. 앱 로그인 후 Refresh.
- **값에 `*`가 붙음**: 이번 조회가 실패해 직전 실측값을 유지 중입니다. Claude Code를 한 번
  열면 토큰이 갱신되고 다음 갱신에서 사라집니다.
- **창이 안 뜨고 텍스트 창만**: WebView2 런타임 자동 설치가 실패한 경우입니다.
  `q_console.exe --install-webview2`로 재시도하거나
  [수동 설치](https://developer.microsoft.com/microsoft-edge/webview2/).
- **오버레이가 비어 있음**: 트레이 우클릭 → Refresh 후 Overlay를 다시 선택.

## 빌드

```
pip install pyinstaller
pyinstaller q_console.spec
```

`dist/q_console.exe`가 생성됩니다.

## 구조

```
q_console.cmd           소스 실행 런처
q_console.spec          EXE 빌드 정의 (PyInstaller)
core/plan_usage.py      Claude/Fable/Codex 현재 계정 Usage 조회 (구독)
core/api_key_usage.py   API 키 모드: 이번 달 사용량 vs 예산
core/claude_code.py     ~/.claude/projects 세션 로그 파서 (단가 환산)
core/codex.py           ~/.codex/sessions 롤아웃 파서 (토큰)
core/bootstrap.py       첫 실행 WebView2 런타임 자동 설치 (1회)
core/snapshot.py        세 퍼센트용 화면 모델
core/render.py          대시보드 / Mini / Overlay 렌더
core/config.py, util.py
ui/                     트레이(PowerShell) + WebView2 호스트
```

설정과 캐시는 `%LOCALAPPDATA%\q_console`에만 기록합니다.

## License

[MIT](LICENSE)
