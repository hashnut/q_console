# Claude 사용량 수집 경로 정정

- 시작: 2026-10-31 Asia/Seoul, HEAD bf35b01, 기존 변경 없음·pull 최신 확인. 사용자 요청은 실제 Claude 사용량이 제대로 수집되는지 재검증하는 것이다.
- 발견: 실행 중 앱은 오늘 00:31의 주간 4%를 stale로 유지하고 5시간 한도는 미제공이었다. .claude 인증은 만료 상태이고 refresh HTTP 400이다. 이를 현재 실측으로 보고하지 않는다.
- 정상 OpenCodex 활성 구독의 공식 provider quota 응답은 주간 6%, 5시간 4%, Fable 0%와 실제 재설정 시각을 반환했다. 네이티브 metadata의 계정 UUID와 OpenCodex 활성 구독 계정이 같음을 값 공개 없이 확인했다.
- 목표: 이 PC의 사용량 소스를 OpenCodex로 명시해 인증 소유자가 갱신을 담당하고 q_console은 읽기 전용 quota만 수신한다. 기존 네이티브 수집은 선택 가능하게 보존한다. 새 계정 전환/오류/오래된 응답을 최신 값이나 0%로 만들지 않는다.
- 검증 계획: 정규화된 주간/5시간/Fable·0/100%·측정 시각·계정 전환·원본 자격 증명 불변 검사, 실제 계정 수신/캐시/표시 대조, 최종 EXE·재시작·자동 갱신 확인. 전체 e2e·실제 모델 호출은 실행하지 않는다.
- QA: PASS(아래 검증 범위 참고).

## 구현·실측 대조

- 명시적 소스 `claude-code|opencodex`를 추가했다. 이 PC에는 opencodex를 선택한다. 기본 네이티브 소스는 다른 PC의 기존 동작을 보존한다. 공식 CLI의 quota를 읽고 활성 계정·선택 revision·loginId의 전후 지문으로 계정 전환/재로그인을 거절한다. 토큰 값은 지문·캐시·로그에 넣지 않는다.
- 측정 시각은 quota.updatedAt, reset 시각은 실제 ms/초를 정규화한다. 120초 초과·미래 시각·집계 보고·다른 공급자/출처·잘못된 퍼센트는 미제공 처리한다. 0과 100은 정상 값으로 유지한다. 이전 소스·계정의 캐시는 재사용하지 않는다.
- OpenCodex 응답에는 리셋권 수량이 없어 해당 소스의 Claude 리셋권 줄은 숨긴다. 기존 네이티브·Codex 리셋권 및 API 잔액 경로는 유지한다.
- PASS: 신규 9개·기존 사용률 표시/모델 35개·Claude 세션 복구 7개 검사. 실제 모델 요청·전체 e2e는 실행하지 않음.
- PASS: 실제 OpenCodex quota와 Anthropic 원본 읽기 전용 Usage 응답(HTTP 200)의 주간 6%·5시간 4%·Fable 0% 및 reset 시각이 일치했다. renderer 모델도 status=ok, stale=false로 동일했다. 검사 전후 OpenCodex/Claude Code 자격 증명 파일 SHA256이 같았다. 원본 토큰/계정 ID는 기록하지 않는다.
- 실제 표시·최종 EXE·자동 갱신 검증도 아래에서 완료했다.

## 실제 화면·배포 적용

- PASS: 숨긴 무음 WebView2의 overlay·surfacer 캡처를 직접 확인했다. 주간 6%·5시간 4%·Fable 0% 표시, clipped=[]·스크롤 없음·창 활성화 없음, 전경 HWND 유지 확인. 증거: build/claude-owner-qa/overlay.png·surfacer.png·render.log.
- 최초 EXE 교체는 실행 중인 q_console worker의 파일 잠금으로 WinError 32를 반환했다. 이 앱의 runtime/ui/tray.ps1만 정확한 경로로 종료하고 해당 root EXE worker를 기다린 뒤 교체했다. OpenCodex 서비스·다른 앱은 종료하지 않았다.
- PASS: PyInstaller 빌드, 루트/dist/ZIP 내부 EXE 일치·CRC 검사. SHA256 c0cc40d7c8156c358da111dbb5f0dbb130d039ab3eaaa026d67ea9cf5c9d8031. 새 EXE 트레이 시작 exit 0, 실제 cache의 source=opencodex,status=ok,stale=false 및 HTML의 6.0%/4.0%를 확인했다.
- 이 PC의 claude_usage_source만 opencodex로 바꾸고 기존 오버레이 선택·API/다른 공급자를 유지했다.
- PASS: 수동 Refresh 없이 새 EXE snapshot 생성 시각 11:46:14 → 11:47:14 KST, 계정 실측 시각 11:46:15 → 11:47:15로 갱신됐다. 주간 6%·5시간 4%·Fable 0%, source=opencodex,status=ok,stale=false가 유지됐다.
- 관련 구현·기능 검사·문서·EXE/ZIP을 로컬 커밋한다. 이번 수정에는 push 요청이 없으므로 원격에는 게시하지 않는다.
