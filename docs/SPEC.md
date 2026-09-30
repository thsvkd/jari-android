# 자리났다 — 기술 명세 (SPEC)

문서 기준일: 2026-10-01, 4.13.0(`feat/play-release`). 제품 의도는 [PRD.md](PRD.md). 2026-09-14의 [좌석 선택·취소표 대기 설계](superpowers/specs/2026-09-14-seat-waitlist-flow-design.md)는 이 문서가 대체하되, 좌석 계획 도메인(연속 좌석·떨어져 앉기·결제 기한)은 그 문서의 "취소표 대기 동작" 절이 여전히 유효하다.

## 1. 구성

```
[Android 앱: Capacitor WebView, TypeScript/Vite SPA]
   │  HTTPS  /api/mobile/*   (Bearer 토큰, X-User-Timezone)
   ▼
[API: Flask + waitress, python -m korail_bot.mobile serve]  ── 코레일 모바일 API ──▶ [코레일]
   ├─ Redis: 세션·조건·즐겨찾기·실행 기록·하트비트 (키 접두 jari:mobile:v1:)
   ├─ SQLite: 초대·아이덴티티·레이트리밋 버킷
   └─ 워커: python -m korail_bot.mobile.worker (검색 1건 = 자식 프로세스 1개, API 컨테이너 안)
```

- 진입점은 둘: `korail_bot.mobile`(API, Dockerfile CMD)과 `korail_bot.mobile.worker`(`mobile/process.py`가 spawn). 워커는 `telegramBot/telebotBackProcess.py`의 검색 루프를 그대로 쓴다.
- 워커는 **API 컨테이너 안의 자식 프로세스**다. API를 재시작하면 워커도 죽고, 새 프로세스가 Redis의 실행 기록으로 다시 띄운다(§1.1). 배포 스크립트는 워커가 있으면 거부하고, `--force`면 재시작 뒤 재개 가능했던 검색이 모두 다시 로그인해 찾는지 확인해 아니면 실패한다.
- 실서버: pit5, `scripts/deploy-backend.sh --host pit5 --root /home/pi/services/jari-android --compose-file compose.yaml --compose-file pit5-edge.yaml --ref <sha>`. 배포 전 이미지는 `jari-api:pre-<sha>`로 태그된다.

### 1.1 재시작 중 검색 보존

**보장**: API가 SIGTERM으로 멈추고(compose `stop_grace_period` 60초 안) 새 프로세스가 같은 Redis·`MOBILE_SECRET`으로 뜨면, 재개 가능한 실행 기록(`running_reservation:*`)은 모두 새 프로세스에서 다시 돌고, 워커가 코레일에 다시 로그인한다. `docker compose up -d --no-deps api`(`deploy-backend.sh --force`)가 이 경우다.

- 멈출 때: `MobileRuntime.stop()`은 무엇보다 먼저(리스 락을 기다리기 전) `_shutting_down`을 세운다. 그 뒤로는 죽은 검색 감시가 사라진 워커를 죽음으로 기록하지 않고(기록 직전에 한 번 더 확인), 새 검색도 시작하지 않는다(`start_reservation_process`가 거부, 모바일은 `shutdown()`이 `start_lock`을 잡은 뒤 워커를 죽여 이미 시작 중인 것도 놓치지 않는다). 워커는 SIGTERM으로 콜백 없이 끝나고 기록·로그인은 남는다.
- 뜰 때: 리스를 잡은 뒤 `reconcile_after_restart`가 옛 run의 기록마다 옛 PID를 정리하고(이번 run의 자식 PID는 건드리지 않는다 — 새 컨테이너에서는 옛 PID가 방금 재개한 워커의 PID와 겹칠 수 있다) 같은 조건으로 워커를 띄운다. 사용자에게는 "검색을 다시 시작했습니다" 알림 하나만 간다.
- 재개 실패(시작 유예 안에 워커가 죽음, 예외): 기록·로그인을 그대로 두고 죽음으로 기록하지 않는다. 백그라운드 루프(10초)가 10·20·40·80·160초 간격으로 다시 시도하고, 6번째도 실패하면 `RESUME_FAILED`로 멈춘 검색에 옮기며 알림을 한 번 보낸다(로그인은 남긴다). 시도와 포기는 모두 사용자별 락 안에서 기록을 다시 읽고 하므로, 그 사이 사용자가 멈추거나 새로 시작한 검색은 건드리지 않는다. 포기는 멈춘 검색을 `announced: false`로 먼저 저장하고 알린 뒤 `true`로 바꾼다. 그 사이 어디서든 실패하면(Redis 실패, 알림 실패) 다음 회차에 이어서 한다: 기록이 남아 있으면 처음부터, 기록은 옮겨졌는데 알리지 못했으면 그 멈춘 검색을 알린다. 사용자는 정확히 한 번 듣는다.
- 재개를 기다리는 동안 앱: 옛 run의 기록은 `health: "unknown"`으로 보여, 앱은 이미 있는 "자리 찾기는 서버에 등록돼 있어요"(running-unverified) 상태로 그린다. 예전에는 기록을 숨겨 검색이 없어 보이면서도 새 검색은 "이미 진행 중"으로 거절됐다.
- 워커의 첫 로그인: 코레일이 답하지 못한 실패(`KorailTransportError`=전송 실패·HTTP 오류, `KorailServiceUnavailableError`(SEMGTK), `KorailNetFunnelError`, `KorailProtocolError`=코레일 응답이 아님, `OSError`, 그리고 라이브러리가 로그인 POST에서 이것들을 `KorailAuthError`로 감싸 올린 것)는 `LOGIN_RETRY_DELAYS_SECONDS`(기본 5·15·30·60·120초, 최대 6회) 동안 다시 시도한다. 계정 거절은 바로 끝난다. 끝내 닿지 못하면 로그인을 지우지 않고 `KORAIL_UNREACHABLE` 멈춘 검색(재개 가능)으로 옮겨 알린다. 워커는 이것을 부모의 사용자별 락 밖에서 하므로, 사용자의 그만 찾기는 워커를 멈춘 뒤 남은 멈춘 검색도 지운다. 로그인에 성공하면 워커가 `search_logged_in:{id}`에 자기 PID를 적는다(배포 확인이 기다리는 것).
- 로그인 보관: `resume_credentials`·`app_session_start`의 TTL(`RESUME_TTL_SECONDS`, 72시간)은 백그라운드 루프가 기록이 있는 동안 매번 다시 늘린다. 72시간보다 오래 기다린 검색도 재개된다. 런타임이 72시간 넘게 내려가 있으면 만료된다.

**의도한 예외**(재개하지 않고 기록·로그인을 지우고 알림 한 번, `resume_abandoned:{id}`에 이유를 하루 남긴다): `RESUME_ON_RESTART=0`(`resume_disabled`), 떨어져 앉기로 이미 일부 좌석을 잡음(`seats_reserved`), 그 검색의 워커가 좌석을 잡았음(`seat_held`: 워커가 코레일의 홀드를 받자마자 `search_held_seat:{id}`에 자기 PID를 적고, 그 PID가 기록의 PID와 같을 때 — 좌석을 잡고 기록을 지우기 전에 멈춘 경우로, 이어서 찾으면 좌석을 두 번 잡는다. 떨어져 앉기 지정 좌석은 일부만 잡고 이어 찾던 중이어도 여기에 든다. 표시는 좌석을 잡는 세 곳 — 연속 좌석, 취소표 대기, 한 자리씩 잡기 — 모두에서 홀드 직후에 적는다), 로그인이 없거나 풀리지 않음(`no_credentials`, `MOBILE_SECRET` 변경 포함). 좌석표에서 바로 잡은 좌석(`reservations/designated`)은 검색이 도는 중에도 허용되지만 표시를 남기지 않으므로 검색 재개를 막지 않는다. 두 표시(`search_logged_in`·`search_held_seat`)는 새 실행 기록을 쓰기 전에 지워, 같은 PID를 받은 예전 워커의 표시로 읽히지 않는다.

**끝난 검색 표시**: 검색이 스스로 끝나 실행 기록을 지우는 곳은 `search_ended:{id}`(`reason`·`at`·`pid`, 7일)를 기록을 지우기 **전에** 남긴다(둘 사이의 실패가 표시 없이 사라진 기록을 남기지 않게): 좌석을 잡음(`booked`, 워커의 `apply_result` status 0), 오류를 알리고 멈춤(`error`, status 1), 사용자가 그만 찾기(`cancelled`, `MobileReservationService.cancel_reservation`, 로그아웃도 이 길). 멈춘 검색(`dead_search`)과 재개하지 않은 기록(`resume_abandoned`)은 따로 남는다. 새 실행 기록을 쓰기 전에 지운다.

**되돌리기**: 이 변경 이전 빌드(지금의 main 포함)는 멈춘 검색의 원인을 `DeathCause(값)`으로 읽으므로, 새 원인(`resume_failed`·`korail_unreachable`)이 적힌 `dead_search`를 만나면 그 사용자의 상태 읽기에서 예외가 난다. 이 빌드가 모르는 원인을 `crashed`로 읽는 것은 **이 빌드 이후 릴리스에서 이 빌드로** 되돌릴 때만 돕고, 이 변경 이전으로 되돌리는 것을 안전하게 만들지 않는다. 이 변경 이전으로 되돌리기 전에는 새 원인이 적힌 `dead_search:*` 키를 지우거나 원인을 `crashed`로 고쳐 쓴다.

**보장 밖**: 코레일이 좌석을 잡은 뒤 워커가 표시를 적기 전(수 ms)에 멈추면 알 길이 없어 재개하고, 좌석을 두 번 잡을 수 있다(코레일 예약 목록은 보지 않는다). SIGKILL·OOM·전원 차단처럼 정상 종료가 없어도 기록은 남아 다음 시작이 재개한다. 다만 놓아주지 못한 리스가 풀릴 때까지(최대 `LEASE_WAIT` 135초) 기다린다.

**배포 확인**: `docker compose exec -T api python -m korail_bot.mobile running --json`은 읽기만 하고 비밀 없이 한 줄 `RUNNING=<json>`을 낸다(로그는 stderr): `now`(컨테이너 시계), `searches[]`(`id, runId, pid, workerAlive, loggedIn, resumable, reason, credentialTtlSeconds, startedAt`), `unreadable`(이 빌드가 읽지 못하는 실행 기록 수 — 모든 읽기가 조용히 건너뛴다), `stopped[]`(멈춘 검색의 원인과 재개하지 않은 기록의 `not_resumed_<이유>`, 각 `at`), `ended[]`(끝난 검색 표시의 `booked`·`cancelled`·`error`, 각 `at`). `deploy-backend.sh`는 아무것도 바꾸기 전에 한 번, 빌드가 끝나 `up` 직전에 한 번 더 읽는다(빌드는 몇 분 걸린다). 재개하지 못할 검색이나 읽지 못하는 실행 기록이 있으면 거부하고(`--allow-unresumable`로 감수), 목록을 읽지 못해도 거부한다(`--skip-resume-check`로 확인 자체를 건너뜀). `up` 직전에 거부하면 재시작하지 않고, 이미 바뀐 src와 이미지를 재시작 없이 되돌리는 명령을 보여 준다. 재시작 뒤에는 5초마다 최대 780초 동안(`DEPLOY_RESUME_POLL`·`DEPLOY_RESUME_TIMEOUT`; `up -d`는 옛 컨테이너가 멈춘 뒤 돌아오므로 그 뒤의 풀리지 않은 리스 대기 135초 + 재개 재시도 310초 + 그 여섯 번의 시도가 각각 10초 백그라운드 회차를 기다릴 수 있는 60초 + 워커의 로그인 재시도 230초 + 시작·로그인 45초) 재개 가능했던 검색마다 새 `runId`·살아 있는 워커·`loggedIn`을 기다린다. 새 빌드가 읽지 못하는 실행 기록이 `up` 직전보다 늘면 곧바로 실패한다(`--allow-unresumable`로 받아들인 원래의 것은 세지 않는다). 읽지 못하는 수는 기록을 읽는 같은 스캔에서 센다. 기록이 사라진 검색은 `up` 직전 읽은 시각 이후의 표시로만 판단한다: `stopped`에 있거나 `ended`가 `error`면 잃은 것(곧바로 실패), `ended`가 `booked`·`cancelled`면 스스로 끝난 것(통과), 아무 표시도 없으면 믿지 않고 기다렸다가 마감에 잃은 것으로 적는다(읽지 못한 기록·Redis 유실·`MOBILE_REDIS_URL` 변경이 여기에 걸린다). 못 채우면 id와 상태(아직 옛 run·워커 없음·로그인 전·멈춤 원인·표시 없이 사라짐)를 적고 실패한다. 롤백 안내는 내지 않는다(옛 워커는 이미 없다). 로그인 시도 하나가 코레일 클라이언트 제한 시간까지 걸리는 경우는 780초에 넣지 않았다: 그때는 "로그인 전"으로 실패를 알리지만 검색은 나중에 돌아올 수 있다.

## 2. 앱 구조

- `src/main.ts` 부트스트랩: 데모 모드(`?demo=1` 또는 `VITE_DEMO_MODE`)면 `createDemoApi()`, 아니면 `createHttpApi()`. 세션 토큰은 Android Keystore 기반 저장소.
- `src/app.ts` `JariApp`: 뷰 상태기(home/journey/trains/confirm/activity/favourites/notifications/settings/rail-account/auth), 문자열 템플릿 `render()`, 30초 상태 폴링(`pollStatus`), 시트(`openSheet`/`confirmSheet`), 좌석 시트(`seatDialog`).
- `src/model.ts`: `deriveRadarView`(상태 판정), 조건↔초안 변환, 좌석 필터.
- `src/demo.ts`: 전 기능 목업 API(열차 3편, 좌석표, 즐겨찾기, 진행 중 검색에 seatPlan 포함).
- **체험하기**(Play 심사처럼 코레일 계정 없이 모든 화면을 봐야 할 때): 실서버 앱의 로그인 선택 화면에 세 번째 카드. 누르면 `JariApp`이 같은 프로세스 안에서 `createDemoApi()`로 바꿔 쓴다(`this.trial`, `api` 게터가 `trial ?? liveApi`). 주소 쿼리나 재시작이 필요 없고 서버·Keystore 토큰·`onToken`을 건드리지 않는다. 최근 구간은 기기 저장소 대신 메모리(`trialRoutes`)에만 쌓는다. 화면 위에 "체험 중 · 샘플 데이터 · 실제 예약되지 않아요" 띠(`[data-trial-banner]`)가 계속 보이고, 설정의 맨 아래 버튼이 "체험 끝내기"(로그아웃 대신)이며 회원 탈퇴 줄은 숨긴다. 세션 정리(`resetSession`)가 체험을 함께 끝내므로 로그인 화면으로 돌아가는 모든 길에서 샘플 상태가 실사용과 섞이지 않는다. 주소의 `?demo=1` 데모 빌드는 그대로다.
- 테마: `data-theme` + `jari.theme` localStorage, 토큰은 `:root`/`:root[data-theme="dark"]`. 저장된 테마가 없으면 `matchMedia` change 를 따라간다. 상태바 아이콘은 `@capacitor/status-bar`로 테마 추종. 토스트는 상단 고정.

## 3. API 계약 (`/api/mobile`)

| 경로 | 메서드 | 용도 |
|---|---|---|
| `auth/register`, `auth/login`, `auth/logout`, `invites` | POST | 초대 기반 계정 |
| `bootstrap` | POST | 전체 상태(capabilities, running, scheduled, pending, favourites, draft, notifyMinutes, paymentUrl) |
| `status` | GET | running/scheduled/pending만 (폴링용) |
| `register` | POST | 코레일 계정 연결 |
| `trains` | POST | 열차 목록 조회 → `trainKey`(30분 TTL 불투명 키) |
| `trains/<key>/cars`, `trains/<key>/cars/<n>/seats`, `trains/<key>/seats` | GET | 호차 목록, 호차 좌석표, 전 호차 좌석표(모든 호차에 적용) |
| `reservations/designated` | POST | 빈 좌석 즉시 예약(홀드) |
| `search`, `schedule`, `search/cancel` | POST | 대기 시작·예약 시작·중지 |
| `reservations/cancel` | POST | 결제 대기 예약 전체 취소 |
| `favourites` GET/POST, `favourites/<id>` DELETE | | 즐겨찾기 |
| `notify`, `notifications`, `devices` | | 알림 간격, 알림 목록, 푸시 토큰 |
| `health` | GET | 인증 없음. Redis 응답·런타임 리스 보유 → 200 `{ok:true,…}`, 아니면 503. 업타임 프로브와 compose healthcheck용 |
| `account/delete` | POST | 회원 탈퇴(§10). 본문 `{password}`(앱 비밀번호를 다시 받는다). 틀리면 403(401은 앱이 세션 만료로 읽는다), 사용자당 5분 5회. 결제 대기 예약이 있거나 마지막 관리자면 409 |
| `diagnostics` | POST | 앱이 응답을 받지 못한 요청 기록(`misses`, 최대 50건, 사용자당 60초 6회)을 올린다. 서버는 한 건에 한 줄 `Client connection miss user=… at= method= path= reason= online= visible= elapsedMs=`로 로그에 남긴다 |

### 3.1 진행 중 검색(`running`)
```
depDate, srcLocate, dstLocate, depTime, maxDepTime, trainTypeShow, specialInfoShow,
passengerCount, seatStrategy, seatPreference(문구), selectedTrains[],
startedAt, health: healthy|unavailable|error, lastCheckedAt|null, attemptCount, elapsedSeconds,
seatPlan: { trains: [{ trainNo, label, seatClass: general|special|any, targets: [{ carNo, labels[] }] }] } | null,
seatPlanSummary: "015 일반실 3호차 5A·5B · 019 좌석 무관" | ""
```
- `health`: 워커 프로세스 생존(psutil cmdline + tag) → healthy; 없음 → unavailable; 하트비트의 연속 실패가 `KORAIL_FAILURE_ALERT_THRESHOLD` 이상 → error; 살아 있어도 시작 후 `SEARCH_SILENT_AFTER_SECONDS`(180) 넘게 하트비트가 없으면 stale(소켓에 걸린 워커).
- 옛 `korail2` 세션은 `_TimedSession`으로 모든 요청에 연결 `KORAIL_CONNECT_TIMEOUT`(10s)·읽기 `KORAIL_READ_TIMEOUT`(30s) 타임아웃을 준다 — 2026-09-22 워커가 응답 없는 소켓에 30분 걸린 사고 이후.
- `lastCheckedAt`/`attemptCount`: 워커가 매 회차 `search_heartbeat:{chat_id}`에 쓰는 하트비트(TTL `SEARCH_HEARTBEAT_TTL_SECONDS`=600). Redis 실패는 삼키고 검색은 계속된다.
- `seatPlan`: 실행 기록의 `search_params.seat_plan_json`을 되읽는다(앱 초안이 아니라 워커가 실제로 든 계획). 열차 이름은 세션의 `trainOptions`가 살아 있을 때만; 아니면 번호.
- 앱 판정(`deriveRadarView`): health healthy/running → 정상(시각 없어도 시작 후 `STALE_AFTER_MS`(120초) 안에서만), `lastCheckedAt`이 120초보다 오래되거나 시각 없이 그 이상 지났으면 확인 지연, error/unavailable → 조회 문제. 둘 다 없으면 running-unverified(홈은 "찾는 중"으로 동일 표시, 상세만 "확인 시각 없음").

### 3.2 좌석 계획(`seat_plan`)
- `TrainSeatTargets { trainNo, seatClass: general|special|any, targets: SeatTarget[] }`. `targets`가 비면 열차 전체(좌석 무관), `any`는 등급도 무관. 한 계획 안에 지정·무관 열차를 섞을 수 있다.
- 서버는 `seat_plan`이 있으면 취소표 대기 워커가 편별로 지정 좌석 또는 등급 전체를 폴링한다(`cancellation_wait_service.poll_once`, `_any_seat_capture`).

### 3.3 레이트리밋
- 코레일 조회(`rail:`): 사용자당 60초 10회 — 열차 조회·호차·좌석표 각 1회, "모든 호차에 적용"은 1회.
- API 전체(`api:`): 60초 120회. 로그인: IP·계정별 5분 10회 + 전역 5분 60회.

### 3.4 코레일 응답 보정(`korail_service`)
- 숫자로 와 앞자리 0이 빠진 고정폭 필드(역 코드 4, 정차 순서 6, 종별 2, 시각 6 등)는 `_padded_train`이 zfill로 복원.
- 복합열차 두 번째 편성의 종별 코드 `0A` 같은 영숫자 값은 핀 고정 라이브러리 검사에서 거절되므로 `_accept_alphanumeric_class_codes()`가 두 검사(좌석 폼·예약 폼)를 `[0-9A-Z]{2}`로 넓힌다(상류 수정 시 제거).
- 코레일 호차 목록은 지금 잔여석이 있는 호차(일행이 많으면 그 인원이 앉을 호차)만 담는다. 거의 매진된 열차는 한 호차만 와서 앱이 그 목록을 편성 전체로 그렸으므로, 좌석표를 여는 경로(`allow_layout_reference=True`)는 `_with_unlisted_cars()`가 같은 열차번호의 7일·14일 뒤 편성(그날의 목록도 일부일 수 있어 둘의 합집합)으로 빠진 호차를 채운다. 채운 호차는 `remainingSeatCount: 0`(앱은 "매진", 일행 2명 이상은 "자리 부족"), 좌석표는 참고 편성의 배치에 모든 좌석 `salePossible: false`로 돌려준다. 편성 조회는 열차·등급마다 한 번(캐시)이고, 실패하면 코레일이 준 목록 그대로 둔다. 대기 워커 경로는 이 조회를 하지 않는다. 채운 호차가 있으면 서버 로그에 `Korail listed cars [...] of a N-car formation; adding [...] as sold out`이 남는다.
- 코레일 세션은 사용자당 30분 슬라이딩 재사용, 502(세션 만료 추정) 시 폐기. 열차 키 TTL 30분.

## 4. 홈 상태 카드 규칙

| 조건 | 카드 |
|---|---|
| pending > 0 | "빈자리를 찾았어요" + 예약 카드 우선 |
| running 없음, scheduled 있음 | "정한 시각에 찾기 시작해요" |
| running, healthy/unverified | 컴팩트: 배지 "찾는 중 · N분 전" · 구간 · 날짜·시간대·인원 · 자세히 보기 / 그만 찾기 |
| running, error/stale/offline | 호박색 `!` 아이콘 + 제목 + 대처 문장 + 자세히 보기 |
| 아무것도 없음 | 조용한 자리표시자(아이콘 + "대기 중인 항목이 없어요") |

칩 선반(`homeChips`): `state.draft`(서버가 복원한 마지막 검색, 예매 중간 단계일 때만 옴) + 즐겨찾기 상위 3, 구간·시간대 중복 제거(중복이면 즐겨찾기 이름 우선). 제목 바로 밑에 상태와 무관하게 위치.

## 5. 즉시 조회 경로(칩·즐겨찾기)

`openDateSheetFor(conditions)` → 날짜 시트(오늘/내일 칩 + 날짜 입력, 기본: 오늘, `max_dep_time − 60분`을 지났으면 내일; "조건 수정" 링크는 폼으로) → `searchFromSaved`: `{...conditions, dep_date, trains: undefined, seat_plan: undefined}`로 `api.trains` → 열차 목록, 저장된 열차 번호는 `wholeTrainSeatClass()`로 좌석 무관 선택, 없는 번호는 토스트로 알림.

## 6. 연결 상태

- `pollStatus` 30초 주기. 서버 오류(5xx)는 `unknown`(카드 "현재 상태를 확인할 수 없어요"), 네트워크 실패는 `noteConnectionMiss`: 첫 실패 5초 뒤 재확인, 연속 2회 또는 `navigator.onLine === false`면 `offline` 배너. 성공 즉시 해제.
- 백그라운드(Capacitor `App` pause, 또는 `visibilityState === "hidden"`)에서는 폴링하지 않는다. Android가 백그라운드 앱의 네트워크를 끊어도 그것을 서버 장애로 판정하지 않기 위해서다. 앱 복귀(`resume`)·`visibilitychange`·`online` 때 바로 다시 묻는다. 이미 나간 폴링이 있으면 겹쳐 묻지 않는다.
- 기한은 상태 폴링에만 있다(20초, 본문 읽기까지). 넘기면 `timeout`으로, `offline`이 아니라 `unknown`("현재 상태를 확인할 수 없어요")이 된다: `/status`도 사용자별 락을 거쳐, 같은 사용자의 좌석표·예약이 코레일을 기다리는 동안 서버에 닿은 채 기다릴 수 있다. 코레일로 가는 요청에는 기한이 없다(끊으면 성공한 예약을 실패로 보일 수 있다; Cloudflare의 100초가 상한).
- 복귀·`online`은 나가 있는 폴링을 기다리지 않고 새로 묻고, 앞선 폴링의 답은 버린다(`pollSeq`). 30초 주기는 나가 있는 폴링 위에 겹쳐 묻지 않는다. 페이지가 보이게 되면(`visibilitychange`) 네이티브 resume을 놓쳤더라도 앞에 있는 것으로 본다. pause/resume 리스너는 `watchForeground`로 페이지 수명 동안 한 번만 건다.
- HTTP 응답을 받지 못한 요청(네트워크 실패·기한 초과. CORS 헤더 없는 Cloudflare 오류 페이지도 여기로 온다)은 `jari.connectionMisses`(localStorage, 최근 50건)에 시각·경로(열차 키·즐겨찾기 id는 `:key`/`:id`)·원인(`network`/`timeout`)·실패 때의 `navigator.onLine`·보낼 때의 화면 표시 여부·걸린 시간을 남긴다. `timeout`은 서버에 닿았을 수 있다(위 락). 다음 인증 요청이 성공하면 `diagnostics`로 올리고 지운다(400이면 버리고, 그 밖의 실패는 다음 성공 때 다시 올린다). 이 기록이 휴대폰에서 Cloudflare 사이 구간의 유일한 증거다: 그 구간의 실패는 Caddy·API 로그에 남지 않는다.
- 운영 점검: `docker logs jari-api-1 2>&1 | grep "Client connection miss"`로 앱이 본 실패를, `docker ps`의 `(healthy)`로 API 상태를 본다. 외부 감시는 `monitor/`의 Cloudflare Worker(`jari-monitor`)다: cron 1분마다 `https://jari.thsvkd.dev/api/mobile/health`를 앱과 같은 길(Cloudflare → 터널 → Caddy → API)로 부르고, 2분 연속 실패하면 장애, 풀리면 장애 시간과 함께 복구를 텔레그램으로 알린다. 원인은 Cloudflare 상태 코드로 가른다(530 터널, 502·504 Caddy→API, 503 API 자체 판정, 시간 초과). 상태는 KV에 바뀔 때만 쓴다(무료 한도 하루 1,000회). 배포는 `monitor/deploy.sh`(Doppler `CLOUDFLARE_API_THSVKD`, `JARI_MONITOR_TELEGRAM_BOT_TOKEN`, `JARI_MONITOR_TELEGRAM_CHAT_ID`), 시험 알림은 `monitor/deploy.sh --test-message`. 지금 상태는 Worker 주소(`https://jari-monitor.<서브도메인>.workers.dev/`)가 JSON으로 보여 준다.

## 7. 확인 시트

`confirmSheet({ title, body, confirmLabel, danger })` → `openSheet` 기반, 상태는 `this.sheet`(재렌더에 견딤), Escape·배경 탭·취소 = false. 위험 동작은 `.button.danger`(빨강 채움). `window.confirm`은 쓰지 않는다. 사용처: 그만 찾기, 예약 전체 취소, 코레일 계정 해제, 즐겨찾기 삭제, 바로 예약, 코레일 예약 대기 신청, 회원 탈퇴. 입력이 필요한 시트는 `openSheet({content})`에 입력 칸을 넣고 확인 값으로 그 값을 받는다(`#sheet-date` 날짜, `#sheet-password` 탈퇴 비밀번호).

## 8. 검증

- 앱: `npm test`(vitest/jsdom), `npm run build`, `npx tsc --noEmit`. `npm run build:demo`는 `dist/`를 덮어쓰므로 APK 빌드 전엔 `npm run build`를 다시 돌린다.
- 서버: `backend/`에서 `uv run --frozen pytest tests/unit -q`, `ruff check/format`. 재시작 보존은 `tests/unit/test_restart_resume.py`와 `e2e/restart.spec.ts`(e2e 스택의 `POST /restart`가 서버를 SIGTERM으로 내렸다 같은 설정으로 띄운다), 배포 스크립트는 `bash scripts/test-deploy-backend.sh`(macOS bash 3.2에서도 돈다).
- Android: `scripts/build-android.ps1`(PowerShell에서 실행), `adb install -r`. 기기 e2e는 `npm run verify`가 에뮬레이터에 평소 앱(`dev.thsvkd.jari` 디버그 빌드, 로컬 e2e 서버 주소로 빌드)을 깔아 돌린다(별도 e2e 앱 없음). `e2e/device.spec.ts`는 기기에서만 돌며 `adb shell input keyevent KEYCODE_BACK`으로 Android 뒤로가기가 시트를 먼저 닫고 앞 화면으로 돌아가는지 본다(targetSdk 36의 예측 뒤로가기). 디버그 APK는 WebView CDP(`adb forward tcp:9377 localabstract:webview_devtools_remote_<pid>`)로 DOM 계측이 가능하다.
- 실기기 감사 절차: 데모가 아니라 실서버로 전 경로(조회·좌석표·대기 시작/중지·시트·테마)를 돌리며 화면마다 버튼 높이·형제 간격·토큰 밖 색·터치 영역·넘침을 수집한다. 결제만 하지 않는다.

## 9. 알려진 제약·미결

- 사이드로드 APK(`release-android.sh`)는 여전히 디버그 키로 서명한다. Play 앱과는 서로 업데이트되지 않는다(§11).
- 조건 바꾸기(멈추고 재시작) 미구현. 결제 대기 열차는 잠금으로 합의.
- 알림 항목은 서버 원문 문자열(제목·본문·기한 분리 미구현). 좌석 셀 라벨은 상태·위치 축이 섞여 있다.
- 즐겨찾기에 좌석표 자리(seat_plan) 저장 안 함.
- `korail_mobile_api` 상류 미수정(영숫자 종별 코드).

## 10. 회원 탈퇴와 공개 페이지

**탈퇴**(`POST /api/mobile/account/delete`, 앱: 설정 → 계정 → 회원 탈퇴 → 위험 확인 시트에서 앱 비밀번호): 순서는 다시 시도할 수 있게 철도 쪽 → 알림 → 아이디 순이다(`api.erase_account`). 아이디를 지우기 전까지는 다시 로그인해 재시도할 수 있다.
1. 마지막 관리자면 409(`identity.delete_user`가 트랜잭션 안에서 다시 확인). 초대 코드는 관리자만 만들므로 관리자가 없어지면 아무도 들일 수 없다. 운영자가 `python -m korail_bot.mobile admin`으로 다른 관리자를 만든 뒤 탈퇴한다.
2. `MobileGateway.delete_account`(사용자별 락 안): 결제 대기 예약이 있으면 409(돌려주는 것은 사용자가 보아야 할 코레일 요청이고, 결제 감시가 지울 로그인으로 계속 코레일에 묻게 된다). 읽지 못하는 실행 기록(키는 있는데 PID를 모른다)이면 무엇이든 바꾸기 전에 409(지우면 그 워커가 없는 계정으로 계속 찾고 예약한다. 예약해 둔 찾기도 그대로 둔다). 그다음 그만 찾기와 같은 `cancel_search`로 워커를 PID로 멈추고 예약해 둔 찾기를 지운다. 그 뒤에도 키가 남으면 409. 워커는 락 없이 도는 따로의 프로세스라 처음 확인과 종료 사이에 좌석을 잡았을 수 있으므로, 결제 대기 예약이 생겼으면 "결제를 기다리는 예약"으로, 결제 기록 없이 그 워커 PID의 `search_held_seat` 표시만 있으면(좌석을 잡고 기록하기 전에 멈춤) "코레일 앱에서 예약 내역을 확인"하라는 문구로 409(찾기는 이미 멈췄고 계정·알림·기록은 남는다). 그 뒤 메모리의 코레일 세션을 버리고 `MobileStorage.delete_user_data`가 `*:{id}`·`*:{id}:*` 키를 모두 지운다(세션·조건·코레일 로그인·재개용 로그인·실행/멈춘/예약한 기록·즐겨찾기·알림 간격·시간대·결제 기록·하트비트·표시). 남기는 것은 `search_ended:{id}` 하나: 개인정보가 없고 7일 뒤 만료되며, 배포 확인이 사라진 검색을 "잃음"이 아니라 "사용자가 멈춤"으로 읽는 근거다.
3. `Notifications.forget`: 알림함·기기 토큰·보내지 못한 알림.
4. `identity.delete_user`: 세션·아이디, 그 계정의 레이트리밋 버킷(`api`·`rail`·`diag`·`account-delete`·`auth-user`).
5. `Notifications.forget`를 한 번 더: 결제 감시처럼 사용자 락 없이 쓰는 쪽이 그 사이 남긴 알림(만료가 없다)을 지운다.
- 앱은 성공하면 토큰과 최근 구간을 지우고 로그인 화면에 "탈퇴했어요" 알림을 한 번 보여 준다. 실패(비밀번호·결제 대기·관리자)는 토스트로 알리고 그대로 둔다.
- 두 관리자가 동시에 탈퇴하면 둘 다 1번 앞선 확인을 지나 철도 쪽이 지워지고, 둘째는 4번에서 409로 계정만 남을 수 있다(친구용 규모라 받아들인다). 결제 대기 확인은 기한이 지난 홀드를 세지 않으므로, 그 사이 끝난 결제 감시가 알림 한 줄을 더 남길 수 있다.
- 앱은 탈퇴에 성공하면 이 폰의 연결 실패 기록(`jari.connectionMisses`)도 지운다(다음 사람의 이름으로 올라가지 않게).
- 메일로 온 탈퇴 요청: API 옆에서 `docker compose exec -T api python -m korail_bot.mobile delete-account --username <앱 아이디>`. 같은 `erase_account`를 쓰고, 워커는 배포 확인처럼 프로세스 표로 찾아 멈춘다(워커가 API의 자식이라 멈춘 워커 하나당 약 6초 기다린다). 이 프로세스는 돌고 있는 API와 락을 나누지 않으므로 먼저 그 계정의 세션을 모두 끊어(새 요청은 401) 새 일이 시작되지 않게 하고, 계정을 지운 뒤 한 번 더 그만 찾기·`delete_user_data`·알림 지우기를 돌려 이미 들어와 있던 요청이 남긴 것(시작한 찾기, 폴링이 다시 쓴 시간대)을 치운다. 409로 거절되면 계정은 남고 로그아웃만 된 상태다. 돌고 있는 API의 메모리에 남은 코레일 세션(최대 30분)과 좌석표 열차 키(30분)는 저절로 사라진다.

**공개 페이지**: `GET /privacy`(개인정보처리방침), `GET /delete-account`(Play 데이터 보안 양식의 계정 삭제 URL이자 데이터 삭제 URL). 두 페이지 모두 앱 이름 "자리났다"와 개발자 "SonPang"을 머리에 보이고, 앱 안 탈퇴(비밀번호 재입력)·앱 없이 메일로 요청(앱 아이디, 초대할 때의 연락처로 본인 확인, 30일 안에 처리)·지우는 정보·남는 정보와 기간(끝난 검색 표시 7일, 서버 로그 10MB×3 돌려 쓰기와 업데이트 때 삭제, 프록시·Cloudflare 접속 기록, DB 파일에서 덮어써지기까지의 지연, 사용자 데이터 별도 백업 없음, 코레일의 기록)·일부만 지우기(코레일 연결 해제, 즐겨찾기 삭제, 그만 찾기, 알림 끄기, 로그아웃)를 적는다. 개인정보처리방침의 표는 Play에 신고한 데이터 유형(사용자 ID·전화번호·기타 개인정보·구매 내역·앱 내 검색 기록·진단·기기 ID) 순서를 따른다. `/api/mobile` 밖이라 Caddy가 같은 호스트 전체를 넘기는 운영에서 `https://jari.thsvkd.dev/privacy`로 열린다. 세션이 필요 없고, Origin 검사를 건너뛰어 다른 사이트에서 연 링크도 열리며(CORS 헤더는 주지 않는다), `text/html; charset=utf-8`·`Cache-Control: public, max-age=3600`·`Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'`. 내용은 `mobile/pages.py`에 있고 저장·전송하는 것이 바뀌면 함께 고친다. 연락처는 `MOBILE_PRIVACY_CONTACT`(compose.yaml에 운영 주소), 없으면 앱의 설정 → 회원 탈퇴와 초대한 운영자를 안내한다.

## 11. Android 빌드·배포와 휴대폰 알림

- `applicationId`는 `dev.thsvkd.jari`(Play에서 `com.jari.app`을 쓸 수 없었다), 코드의 `namespace`·Java 패키지·액티비티는 그대로 `com.jari.app`(`com.jari.app.MainActivity`). `capacitor.config.ts`의 `appId`도 `dev.thsvkd.jari`. 4.12.x 이하의 `com.jari.app` 앱 위에는 업데이트되지 않고 따로 깔린다.
- `compileSdk`·`targetSdk` 36(Play는 2026-08-31부터 새 앱·업데이트에 API 36을 요구), AGP 8.9.3(API 36을 지원하는 첫 줄), Gradle 8.11.1, JDK 21. Android 16에서 바뀐 것: 가장자리까지 그리기를 끌 수 없다 — 앱은 이미 `env(safe-area-inset-*)`로 그리고 끄는 설정(`windowOptOutEdgeToEdgeEnforcement`)을 쓰지 않는다. 예측 뒤로가기가 기본으로 켜져 `onBackPressed`가 불리지 않는다 — `@capacitor/app`은 `OnBackPressedDispatcher` 콜백으로 받으므로 `backButton` 리스너(`platform.ts`)가 그대로 온다(`e2e/device.spec.ts`). 600dp 이상 화면에서는 `screenOrientation="sensorPortrait"`가 무시된다(태블릿·폴드 펼침은 가로로도 돈다). 받아들인다: 대상은 친구들의 폰이고, 화면은 이미 최대 520px 폭 가운데 정렬이라 가로에서도 한 열로 그려진다. 가로 레이아웃 e2e 프로젝트는 두지 않았다.
- 광고·분석 없음(Play에 "광고 없음·광고 ID 쓰지 않음·분석 없음"으로 신고): 매니페스트가 `com.google.android.gms.permission.AD_ID`를 `tools:node="remove"`로 빼고, `firebase-analytics`·`play-services-measurement`·`play-services-ads*`는 의존성에 없다(`firebase-messaging`이 끌어오는 것은 인터페이스뿐인 `firebase-measurement-connector`). `release-play.sh`가 둘 다 확인한다.
- 아이콘: 앱 안의 로고(홈 왼쪽 위 `brand-mark`, 짙은 둥근 네모 속 좌석 둘과 바닥선)를 모든 곳에 쓴다. `node scripts/render-icons.mjs`가 Playwright(Chromium)로 적응형 아이콘(벡터 앞면·바탕색 `#202631`·Android 13+ 한 색 레이어), 옛 런처용 `mipmap-*/ic_launcher{,_round}.png`, 런치 화면 `drawable*/splash.png`, `public/icon.svg`, Play 그림 `store/icon-512.png`(32비트)·`store/feature-graphic.png`(1024×500, 알파 없음)을 만든다. 스토어 스크린숏(데모 모드, 1080×2160)은 `store/screenshots/`.
- **Play(내부 테스트)**: `scripts/release-play.sh` → `dist-release/jari-<버전>-play.aab`. `bundleRelease`(디버그 불가 → Capacitor가 WebView 디버깅을 켜지 않는다), 운영 API 주소, 업로드 키(Doppler `JARI_ANDROID_RELEASE_*`)로 서명. Doppler `JARI_ANDROID_GOOGLE_SERVICES_B64`를 빌드 동안만 `android/app/google-services.json`으로 풀고 끝나면 지운다. 없거나 `applicationId`의 클라이언트가 없으면 멈춘다. Gradle은 데몬 없이 돌고, 업로드 키 비밀번호는 export 하지 않고 keytool과 Gradle 명령의 환경으로만 넘긴다(npm·npx에 가지 않고, 비밀번호를 든 프로세스가 남지 않게). 웹 번들은 `VITE_DEMO_MODE=false`로 만들어 로컬 `.env.local`이 데모로 바꾸지 못하게 하고, 번들이 데모 빌드면(`get("demo")==="1"||!0`) 멈춘다. 의존성 목록·`capacitor.config.json`을 읽지 못하면 통과가 아니라 실패다. 끝에 서명(`jarsigner -strict`)과 인증서(업로드 키) SHA-256·SHA-1, versionCode/Name, 매니페스트(debuggable 아님·AD_ID 없음·targetSdk는 `variables.gradle`의 값, `JARI_BUNDLETOOL`이 있으면 bundletool로), 광고·분석 의존성 없음, `google_app_id` 포함, 웹 번들의 API 주소를 확인한다. 확인하는 동안은 `*.aab.unchecked`로 두고 모두 통과해야 최종 이름으로 옮기며, 어긋나거나 끊기면 지운다. 올리는 것은 사람이 Play Console에서 한다.
- **사이드로드**: `scripts/release-android.sh`(디버그 키, `assembleDebug`). 같은 Doppler 시크릿이 있으면 넣어 알림이 켜진 APK를 만든다(없으면 꺼진 채로 알린다). Play에서 받은 앱(Play 앱 서명 키)과 이 APK는 같은 패키지라도 서명이 달라 서로 업데이트되지 않는다: 한 폰에서는 한쪽만 쓰고, 바꿀 때는 지우고 다시 깐다.
- **휴대폰 알림 경로**: 설정 → 휴대폰 알림(서버 `capabilities.push`가 참일 때만 누를 수 있다) → `initializePlatform({enablePushRegistration: true})` → `SecureSession.pushConfigured()`(google-services 플러그인이 넣은 `google_app_id` 리소스가 있는지) → `PushNotifications.requestPermissions()`(Android 13+ `POST_NOTIFICATIONS` 런타임 권한, 매니페스트에 선언됨. 사용자가 누를 때만 묻는다) → `register()` → FCM 토큰 → `POST /api/mobile/devices`(`{token, platform: "android"}`, 서버는 `MOBILE_SECRET`으로 암호화해 계정당 10대) → 알림이 생기면 outbox → 런타임의 10초 루프 `Notifications.deliver` → firebase-admin(`MOBILE_FCM_CREDENTIALS`, compose의 `firebase_admin` 시크릿 = 서버의 `backend/.secrets/firebase-admin.json`). 앱의 `google-services.json`과 서버의 Admin 키는 **같은 Firebase 프로젝트(`jari-thsvkd`)**여야 한다. 다르면 FCM이 토큰을 거절해(SenderId mismatch) 알림함에는 쌓여도 휴대폰으로는 가지 않고, `deliver`가 재시도하다 멈춘다(10회). 앱 쪽 설정은 Doppler `JARI_ANDROID_GOOGLE_SERVICES_B64`, 서버 쪽 키는 `JARI_FCM_ADMIN_JSON_B64`(둘 다 dev/dev). 서버 키를 바꾸는 것은 배포 스크립트가 하지 않는 수동 단계다: 운영 PC에서 `doppler secrets get JARI_FCM_ADMIN_JSON_B64 --plain --project dev --config dev | base64 --decode | ssh pit5 'umask 077; cat > /home/pi/services/jari-android/backend/.secrets/firebase-admin.json'`, 그 뒤 API를 다시 시작해야 읽는다(firebase-admin은 시작할 때 한 번 읽는다. 검색 확인 후 승인받아 `deploy-backend.sh`로). 4.12.x 이하 앱(`com.jari.app`, `teum-android` 프로젝트)이 등록한 토큰은 새 키로는 보낼 수 없다 — 사용자가 새 앱에서 알림을 다시 켜면 새 토큰이 등록된다. 푸시에는 "새 알림이 왔어요"와 알림 번호만 싣고 내용은 앱 알림함에서 본다. 기기 e2e는 `-PjariNoFirebase`로 빌드해 권한·토큰이 끼지 않는다.
