# Claude Console 조직 잔액 직접 연동

- 시작: 2026-10-30 Asia/Seoul, HEAD `37bf14b`, 기존 작업 트리 깨끗함.
- 요구: 다른 PC 사용분까지 포함한 Console의 조직 크레딧을 주기적으로 직접 조회한다. 수동 기준점/로컬 로그 계산으로 대체하지 않는다.
- 확인: 로그인된 Chrome에서 Console의 읽기 전용 `/api/organizations/{uuid}/prepaid/credits` 응답을 확인했다. `balance.credits.amount_minor=12957`, `exponent=2`로 $129.57이었다. API 키/구독 OAuth와 Console 웹 로그인은 별개의 인증이다.
- 구현 계획: 앱 전용 WebView2 프로필에서 공식 Console에 로그인하고, 백그라운드 조회는 이 프로필로 해당 조직의 잔액 응답만 읽는다. 브라우저 쿠키를 다른 PC에서 가져오거나 소스/설정/로그에 저장하지 않는다. 조회 실패는 마지막 웹 실측값+실패 상태 또는 `--`로 표시한다.
- 검증 계획: 단위 테스트(단위 변환, 잘못된 값, 인증 실패, 마지막 실측 보존/조직 분리), PowerShell 구문, EXE 빌드, 실제 로그인/잔액 비교/주기 갱신/재시작.
- 구현: `core/console_credit.py`가 WebView2 조회 호스트를 실행해 조직 잔액을 수신한다. `ui/console-credit-host.cs`는 API capability 조직만 선택하고 같은 전용 프로필에서 읽기 전용 GET을 수행한다. 호스트 EXE는 `tools/build_console_host.ps1`로 재생성한다. 트레이 연결 메뉴·CLI·오버레이·카드·요약에 반영했다.
- 기본값은 웹 조회다. 기존 수동 기준점이 있어도 자동 전환된다. `--set-claude-credit`를 새로 실행한 경우만 명시적인 로컬 모드로 들어가며, `clear`/Console 연결은 웹 모드로 복귀한다.
- 서버의 최상위 `balance.credits.amount_minor / 10^exponent`를 사용한다. 반올림된 `balance_credits`나 개별 grant 잔액으로 대체하지 않는다. 만료일도 서버 응답을 사용한다.
- 로그인/조회 실패는 마지막 동일 조직의 웹 실측을 보존하고 `*`·시각·원인을 표시한다. 24시간 초과·다른 조직의 값은 재사용하지 않고 `--`로 표시한다. 정상 조회 60초, 인증/요청 제한 실패 300초 간격으로 요청을 제한한다.

## 검증 (2026-10-30 Asia/Seoul)

- PASS: 격리된 LOCALAPPDATA에서 `python -m unittest discover -s tests` 95개. 실제 단위·0원·응답 오류·인증 만료·다른 조직 캐시 격리·다른 PC 사용에 따른 원격 잔액 변경·로컬 추정 미사용·실패 표시를 확인했다. 재시도 대기 중에도 24시간을 넘긴 실측은 표시하지 않는 경계 조건을 추가했다.
- PASS: C# x64 로그인 호스트 빌드, PowerShell 호스트 실행, 트레이 PowerShell 구문 검사, 최종 PyInstaller EXE 빌드. 루트 EXE와 dist SHA256 `975c1a3eabf28adfe364d6e8f869beaf76313fb3fdab8b19782ec66ec95281c4` 일치. ZIP은 EXE 한 개만 포함하고 원본 바이트 일치 및 CRC 검사를 통과했다.
- PASS: 사용자가 앱 전용 창에서 개인 계정으로 로그인했다. 구독 조직이 아닌 Individual API 조직을 자동 선택했다. 앱 전용 프로필을 다시 열어 직접 읽은 잔액이 $124.07 → $123.45 → $122.77 → $122.13으로 변했다. 기준점이나 로컬 사용 로그로 빼지 않는다.
- PASS: 최종 EXE 재시작 exit 0, snapshot `source=console,status=ok`. Chrome 대시보드와 최종 EXE의 잔액 $122.77이 일치했다. EXE의 자동 주기 갱신으로 실측 시각이 15:34:04 → 15:35:10으로 바뀌는 것을 확인했다.
- PASS: 자동 주기 갱신에서 잔액 $121.58을 확인했고, 최종 보완 빌드 재시작 후에는 서버 잔액 $120.31을 수신했다. 재시작 후에도 재로그인하지 않았다.
- 로그인 작업은 일반 refresh의 180초 제한과 분리해 최대 930초를 기다리며, 트레이에서 연결 메뉴를 연속 클릭해도 중복 로그인 작업을 만들지 않는다.
- PASS: 별도의 숨김 조회가 1.92초에 완료되어 서버 잔액 $122.13을 반환했다. 실행 전후 foreground HWND가 같았다. 로그인 창은 최초 연결 때만 의도적으로 보인다.
- 렌더: 기존 `tools/qa_overlay.ps1`에서 실제 WebView2 캡처의 API $122.77을 직접 확인했다. 글자 잘림 0, 가로 스크롤 없음. 캡처는 `build/console-live-qa/overlay.png`에 있다. 이 기존 QA 스크립트의 창 활성화 assertion은 FAIL이므로 전체 스크립트 PASS로 처리하지 않았다. 신규 숨김 잔액 조회의 포커스 보존은 별도 실측으로 PASS했다.
- 수정 중 실패: PowerShell 프로세스의 WinForms 창이 UI 도구 목록에 나타나지 않아 사용자에게 조사용 Chrome JSON 탭과 혼동을 주었다. 전용 GUI EXE로 전환하고 기본 모니터 중앙에 연결 창을 배치했다. Google GIS 로그인을 부모 WebView의 단순 이동으로 처리하면 `gsi/transform`에서 멈췄다. 같은 WebView2 환경을 공유하는 실제 popup과 opener를 사용하도록 수정했고 실제 로그인을 통과했다.
- 보존: Chrome 쿠키 저장소를 읽거나 복사하지 않았다. 개인 로그인 프로필·연결된 조직 metadata·사용량 캐시는 LOCALAPPDATA에만 있다. 소스·배포 EXE/ZIP에는 계정 자격 증명을 넣지 않았다.
- 게시 범위: 사용자가 이 구현·테스트·문서·EXE/ZIP을 `hashnut/q_console`의 `origin/main`에 커밋·push하도록 요청했다. 게시 전 원격과 HEAD 차이는 0/0이었다. 실제 push 결과와 원격 SHA 일치는 최종 도구 결과로 확인한다. 계정 프로필과 로컬 설정은 게시하지 않는다.

## 참고

- Console 응답을 로그인된 브라우저와 실제 앱 프로필에서 직접 확인했다.
- [CodexBar의 Console 제공자 문서](https://github.com/steipete/CodexBar/blob/main/docs/claudeplatform.md): 공개 추론 키·구독 OAuth와 Console 세션의 구분, 읽기 전용 endpoint와 minor-unit 형식의 교차 근거.
- 비공개 웹 응답이 바뀌거나 로그인 세션이 만료되면 조회 불가 상태를 표시한다. 해당 PC에서 재연결/앱 수정이 필요할 수 있다.
