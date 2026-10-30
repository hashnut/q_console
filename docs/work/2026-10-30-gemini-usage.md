# Gemini 사용량 추가

- 시작: 2026-10-30 Asia/Seoul, HEAD `d36ad8c`, 작업 트리 깨끗함. `git pull --ff-only` 최신 확인.
- 목표: Codex 오른쪽에 Gemini 사용량·재설정 시간을 표시하고 대시보드·Mini·트레이 토글·배포 EXE에 반영한다.
- 범위: 계정의 읽기 전용 한도. 로컬 토큰 합계나 임의 예산으로 구독 사용률을 대체하지 않는다.
- 조사: 설치된 공식 Gemini CLI 소스와 Google 공식 저장소에서 `loadCodeAssist` / `retrieveUserQuota`를 확인했다. 기존 OAuth 로그인의 `loadCodeAssist`는 `UNSUPPORTED_CLIENT`를 반환하고 Antigravity 제품으로 이전을 안내한다. 사용자가 원하는 Gemini 제품을 질문했다.
- 검증 계획: 한도 파싱·0/100%·오류/만료·캐시 보존과 계정 분리, Codex 오른쪽 순서·표시 토글·레이아웃의 기능 검사. 전체 e2e 실행하지 않음.
- QA: PASS(아래 실제 검사 범위 참고).

## 구현·중간 검증

- 사용자 선택: Antigravity에서 쓰는 Gemini 한도. 공식 비생성 `agy -p /usage --output-format json` 보고서의 `command.data.groups`를 사용한다. 설치 버전은 1.4.10. 1.1.11 미만/버전 미확인은 보고서 요청을 보내지 않는다.
- Gemini 공용 주간·5시간, 별도 Argon 주간을 각각 표시한다. Antigravity의 Claude/GPT 그룹은 기존 Claude Code·Codex 계정과 다르므로 제외한다. disabled·미제공 한도는 0%로 만들지 않는다.
- 로그인·토큰 갱신은 agy가 관리한다. 앱은 자격 증명 파일을 복사하지 않는다. 보고서에 계정 식별자가 없어 실패 시 기존 값을 재사용하지 않는다.
- 조회는 비입력 숨김 Win32 데스크톱에서 CREATE_NO_WINDOW로 실행한다. 자동 업데이트를 비활성화하고 private 임시 폴더에서 30초 제한으로 실행한다. 실측 보고서의 num_turns=0, 모든 토큰 0 확인.
- 실계정: 공용 주간 10.441768%, 5시간 0%, Argon 주간 99.158493%. 각 reset_time은 서버 응답을 그대로 사용.
- 1차 단위 검사: 기존 95개+신규 8개 중 101 PASS, 2 FAIL은 Codex가 마지막 제공자라는 기존 테스트 가정 때문이었다. 해당 기대 위치를 Codex 인덱스로 고치고 관련 10개 재검사 PASS. 이후 구버전 차단 검사 1개 추가해 신규 9개 PASS.
- 렌더: 별도 숨김 데스크톱의 실제 WebView2에서 overlay / surfacer / phosphor / mini 네 화면 캡처·범위 검사 PASS, clipped=[]·스크롤 없음·창 활성화 없음. build/gemini-qa/*.png와 render.log. 캡처를 직접 확인해 Codex 오른쪽 Gemini 순서를 확인했다. Surfacer Codex 실측 배지 줄바꿈은 글자 크기·간격을 조정해 보완 중.
- 초기 렌더 래퍼는 결과 출력의 cp949 인코딩 오류로 종료 1이었으나 내부 PowerShell 검사는 종료 0, 네 캡처 및 로그 PASS. 검증 결과와 래퍼 오류를 분리한다.
- 참고: [Google 공식 /usage](https://antigravity.google/docs/cli/commands/usage), [공식 CLI usage 소스](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/code_assist/server.ts). 모델 버킷 API 대신 현재 설치된 Antigravity 공식 usage 보고서를 채택했다.

## 최종 적용

- Surfacer 배지 줄바꿈 보완 후 재캡처 PASS: `build/gemini-qa-final/surfacer.png`. 큰 사용률·실측 배지·재설정 시간과 Gemini의 두 추가 한도를 직접 확인했다.
- 트레이 PowerShell 구문 검사 및 PyInstaller 최종 빌드 PASS. 루트/dist EXE SHA256 `6a6b9a7ce9128f9a26a76df87ecc09139f152be3b6ca7f4b79fa815cce64f5f6` 일치. ZIP은 동일 EXE 하나만 포함하고 CRC·바이트 일치 검사 PASS. ZIP 직접 덮어쓰기가 일시적으로 EINVAL을 반환해 검증한 임시 ZIP의 원자적 교체로 완료했다.
- 로컬 설정의 기존 선택(Claude·API 잔액·Codex·추가 크레딧·시계)을 보존하고 Gemini를 추가했다. 최종 EXE로 트레이 재시작, runtime/ui/tray.ps1의 Gemini 토글 포함 확인.
- 최종 EXE 실측 `status=ok, source=antigravity-cli`, Gemini 주간 10.441768%, 5시간 0%, Argon 주간 99.158493%. 실제 생성된 overlay.html에서도 Codex 뒤 Gemini 순서 확인.
- 신규 9개 검사 PASS(잘못된 응답 구조/비율, disabled, 0/100%, 실패 시 다른 계정 캐시 미사용, 구버전 비생성 명령 차단, 0턴 보고서, 표시 순서·토글·세 테마). 기존 공유 모델 검사도 실패했던 두 기대 위치를 고친 뒤 해당 10개를 재실행해 PASS했다.
- 포커스·마우스 조작 없이 숨긴 데스크톱과 API/파일을 사용했다. 실제 스피커 출력·전체 e2e는 실행하지 않았다.
- 직전 실행 파일/ZIP은 `build/gemini-before/`에 보존했다. 로그인 프로필·자격 증명·사용량 캐시는 커밋/배포에 포함하지 않는다.
- 이번 구현·테스트·문서·EXE/ZIP을 로컬 커밋한다. 이 작업에서 push 요청은 없으므로 원격에 push하지 않는다.

- 최종 EXE의 자동 갱신: 초기 실측 20:54:14 → 20:55:14 KST. 수동 refresh·재로그인 없이 `status=ok` 및 Gemini 세 한도가 유지됨을 확인했다.

## Gemini 아이콘 보완

- 사용자 요청: Gemini에도 브랜드 아이콘을 표시한다. 시작 HEAD `01f0457`, 기존 변경 없음, pull 최신 확인.
- Google Gemini 공개 사이트에서 사용하는 공식 컬러 sparkle SVG를 `ui/gemini.svg`에 보관했다. 오버레이에는 data URI로 내장해 오프라인에서도 표시한다. 기존 아이콘과 같은 13px 크기·6px 간격이며 추가 폭 19px을 반영한다.
- 출처: https://www.gstatic.com/lamda/images/gemini_sparkle_aurora_33f86dc0c0257da337c63.svg
- QA: PASS. 오버레이 표시 조합 검사 4개 통과. 별도 숨김 데스크톱의 무음 WebView2 캡처에서 컬러 아이콘·정렬을 직접 확인했고 clipped=[]·스크롤 없음·활성화 없음. 증거: `build/gemini-icon-qa/overlay.png`와 `render.log`.
- PyInstaller 재빌드, 루트/dist EXE 및 단일 EXE ZIP 바이트·CRC 일치 확인. 새 EXE SHA256 `9b015d9d437d9676e294d2d91eb60cbffaa1545978819d64b9bc29a71dca08d0`.
- 신규 테스트·전체 e2e는 추가하지 않았다. 현재 기능의 표시 조합과 실제 렌더만 확인했다. 실행 파일을 갱신하고 트레이 재시작 exit 0 확인. 실제 runtime의 SVG 파일·오버레이 data URI 및 Gemini status=ok 확인. 로컬 커밋하며 push하지 않는다.

## Claude·Codex 아이콘 보완

- 사용자 요청: Claude·Codex도 예쁜 아이콘으로 교체한다. 시작 HEAD `1aabfd8`, 작업 트리 깨끗함, `git pull --ff-only` 최신 확인.
- Claude: https://claude.ai/favicon.svg 의 공식 주황색 심볼을 `ui/claude.svg`에 보관했다.
- Codex: 현재 설치된 공식 OpenAI.Codex 26.1023.6621.0의 `app.asar` 내 `webview/assets/codex-app-ga-logo-3e5209898ca3.png` 이미지 에셋(9,974 bytes)을 원본 그대로 `ui/codex.png`에 보관했다. 파란 꽃 모양에 터미널 기호가 들어간 컬러 앱 아이콘이다.
- 기존 간이 Claude 선형 심볼·청록색 ChatGPT 로고를 교체한다. Gemini 포함 세 아이콘을 16px로 맞추고 항목 폭도 3px씩 늘린다. 원본을 다시 그리거나 재색칠하지 않으며 HTML에 data URI로 내장한다.
- 기존 표시 테스트의 ChatGPT path 기대값은 새 Codex 이미지에 맞춘다. 신규 테스트·전체 e2e는 추가하지 않는다.
- QA: PASS. 표시 조합 4개·사용량 표시/모델 35개 통과. 숨긴 데스크톱의 실제 무음 WebView2 캡처에서 세 컬러 아이콘·크기·정렬을 직접 확인했다. clipped=[]·스크롤 없음·창 활성화 없음. 증거: `build/brand-icon-qa/overlay.png`·`render.log`.
- 최종 PyInstaller 빌드·EXE/ZIP 바이트 및 CRC 검사 PASS. SHA256 `bdf340a7a291c063da5394d6abece79f9ab9ebd12829d5aeac2e241135ba3667`.
- 새 EXE 재시작 exit 0. 실제 runtime의 세 이미지 파일과 생성된 오버레이의 data URI·브랜드별 alt, 실계정 Gemini status=ok 확인. 로컬 커밋하며 push하지 않는다.
