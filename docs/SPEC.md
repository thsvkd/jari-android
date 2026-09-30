# 자리났다 — 기술 명세 (SPEC)

문서 기준일: 2026-09-23, main `880cf0a`. 제품 의도는 [PRD.md](PRD.md). 2026-09-14의 [좌석 선택·취소표 대기 설계](superpowers/specs/2026-09-14-seat-waitlist-flow-design.md)는 이 문서가 대체하되, 좌석 계획 도메인(연속 좌석·떨어져 앉기·결제 기한)은 그 문서의 "취소표 대기 동작" 절이 여전히 유효하다.

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
- 재개 실패(시작 유예 안에 워커가 죽음, 예외): 기록·로그인을 그대로 두고 죽음으로 기록하지 않는다. 백그라운드 루프(10초)가 10·20·40·80·160초 간격으로 다시 시도하고, 6번째도 실패하면 `RESUME_FAILED`로 멈춘 검색에 옮기며 알림을 한 번 보낸다(로그인은 남긴다). 시도와 포기는 모두 사용자별 락 안에서 기록을 다시 읽고 하므로, 그 사이 사용자가 멈추거나 새로 시작한 검색은 건드리지 않는다. 포기를 기록하지 못하면(Redis 실패) 아무 알림 없이 기록을 두고 다음 회차에 다시 포기한다.
- 재개를 기다리는 동안 앱: 옛 run의 기록은 `health: "unknown"`으로 보여, 앱은 이미 있는 "자리 찾기는 서버에 등록돼 있어요"(running-unverified) 상태로 그린다. 예전에는 기록을 숨겨 검색이 없어 보이면서도 새 검색은 "이미 진행 중"으로 거절됐다.
- 워커의 첫 로그인: 코레일이 답하지 못한 실패(`KorailTransportError`=전송 실패·HTTP 오류, `KorailServiceUnavailableError`(SEMGTK), `KorailNetFunnelError`, `KorailProtocolError`=코레일 응답이 아님, `OSError`, 그리고 라이브러리가 로그인 POST에서 이것들을 `KorailAuthError`로 감싸 올린 것)는 `LOGIN_RETRY_DELAYS_SECONDS`(기본 5·15·30·60·120초, 최대 6회) 동안 다시 시도한다. 계정 거절은 바로 끝난다. 끝내 닿지 못하면 로그인을 지우지 않고 `KORAIL_UNREACHABLE` 멈춘 검색(재개 가능)으로 옮겨 알린다. 로그인에 성공하면 워커가 `search_logged_in:{id}`에 자기 PID를 적는다(배포 확인이 기다리는 것).
- 로그인 보관: `resume_credentials`·`app_session_start`의 TTL(`RESUME_TTL_SECONDS`, 72시간)은 백그라운드 루프가 기록이 있는 동안 매번 다시 늘린다. 72시간보다 오래 기다린 검색도 재개된다. 런타임이 72시간 넘게 내려가 있으면 만료된다.

**의도한 예외**(재개하지 않고 기록·로그인을 지우고 알림 한 번, `resume_abandoned:{id}`에 이유를 하루 남긴다): `RESUME_ON_RESTART=0`(`resume_disabled`), 떨어져 앉기로 이미 일부 좌석을 잡음(`seats_reserved`), 그 검색의 워커가 좌석을 잡았음(`seat_held`: 워커가 코레일의 홀드를 받자마자 `search_held_seat:{id}`에 자기 PID를 적고, 그 PID가 기록의 PID와 같을 때 — 좌석을 잡고 기록을 지우기 전에 멈춘 경우로, 이어서 찾으면 좌석을 두 번 잡는다. 떨어져 앉기 지정 좌석은 일부만 잡고 이어 찾던 중이어도 여기에 든다), 로그인이 없거나 풀리지 않음(`no_credentials`, `MOBILE_SECRET` 변경 포함). 좌석표에서 바로 잡은 좌석(`reservations/designated`)은 검색이 도는 중에도 허용되지만 표시를 남기지 않으므로 검색 재개를 막지 않는다. 두 표시(`search_logged_in`·`search_held_seat`)는 새 실행 기록을 쓰기 전에 지워, 같은 PID를 받은 예전 워커의 표시로 읽히지 않는다.

**보장 밖**: 코레일이 좌석을 잡은 뒤 워커가 표시를 적기 전(수 ms)에 멈추면 알 길이 없어 재개하고, 좌석을 두 번 잡을 수 있다(코레일 예약 목록은 보지 않는다). SIGKILL·OOM·전원 차단처럼 정상 종료가 없어도 기록은 남아 다음 시작이 재개한다. 다만 놓아주지 못한 리스가 풀릴 때까지(최대 `LEASE_WAIT` 135초) 기다린다.

**배포 확인**: `docker compose exec -T api python -m korail_bot.mobile running --json`은 읽기만 하고 비밀 없이 한 줄 `RUNNING=<json>`을 낸다(로그는 stderr): `now`(컨테이너 시계), `searches[]`(`id, runId, pid, workerAlive, loggedIn, resumable, reason, credentialTtlSeconds, startedAt`), `stopped[]`(멈춘 검색의 원인과 재개하지 않은 기록의 `not_resumed_<이유>`, 각 `at`). `deploy-backend.sh`는 아무것도 바꾸기 전에 한 번, 빌드가 끝나 `up` 직전에 한 번 더 읽는다(빌드는 몇 분 걸린다). 재개하지 못할 검색이 있으면 거부하고(`--allow-unresumable`로 감수), 목록을 읽지 못해도 거부한다(`--skip-resume-check`로 확인 자체를 건너뜀). `up` 직전에 거부하면 재시작하지 않고, 이미 바뀐 src와 이미지를 재시작 없이 되돌리는 명령을 보여 준다. 재시작 뒤에는 5초마다 최대 720초 동안(`DEPLOY_RESUME_POLL`·`DEPLOY_RESUME_TIMEOUT`; `up -d`는 옛 컨테이너가 멈춘 뒤 돌아오므로 그 뒤의 풀리지 않은 리스 대기 135초 + 재개 재시도 310초 + 워커의 로그인 재시도 230초 + 시작·로그인 45초) 재개 가능했던 검색마다 새 `runId`·살아 있는 워커·`loggedIn`을 기다린다. 기록이 사라졌으면 `up` 직전 읽은 시각 이후 `stopped`에 있을 때만 잃은 것으로 보고 곧바로 실패하며, 없으면 스스로 끝난 검색(좌석 잡음·사용자가 멈춤)으로 본다. 못 채우면 id와 상태(아직 옛 run·워커 없음·로그인 전·멈춤 원인)를 적고 실패한다. 롤백 안내는 내지 않는다(옛 워커는 이미 없다). 로그인 시도 하나가 코레일 클라이언트 제한 시간까지 걸리는 경우는 720초에 넣지 않았다: 그때는 "로그인 전"으로 실패를 알리지만 검색은 나중에 돌아올 수 있다.

## 2. 앱 구조

- `src/main.ts` 부트스트랩: 데모 모드(`?demo=1` 또는 `VITE_DEMO_MODE`)면 `createDemoApi()`, 아니면 `createHttpApi()`. 세션 토큰은 Android Keystore 기반 저장소.
- `src/app.ts` `JariApp`: 뷰 상태기(home/journey/trains/confirm/activity/favourites/notifications/settings/rail-account/auth), 문자열 템플릿 `render()`, 30초 상태 폴링(`pollStatus`), 시트(`openSheet`/`confirmSheet`), 좌석 시트(`seatDialog`).
- `src/model.ts`: `deriveRadarView`(상태 판정), 조건↔초안 변환, 좌석 필터.
- `src/demo.ts`: 전 기능 목업 API(열차 3편, 좌석표, 즐겨찾기, 진행 중 검색에 seatPlan 포함).
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

`confirmSheet({ title, body, confirmLabel, danger })` → `openSheet` 기반, 상태는 `this.sheet`(재렌더에 견딤), Escape·배경 탭·취소 = false. 위험 동작은 `.button.danger`(빨강 채움). `window.confirm`은 쓰지 않는다. 사용처: 그만 찾기, 예약 전체 취소, 코레일 계정 해제, 즐겨찾기 삭제, 바로 예약, 코레일 예약 대기 신청.

## 8. 검증

- 앱: `npm test`(vitest/jsdom), `npm run build`, `npx tsc --noEmit`. `npm run build:demo`는 `dist/`를 덮어쓰므로 APK 빌드 전엔 `npm run build`를 다시 돌린다.
- 서버: `backend/`에서 `uv run --frozen pytest tests/unit -q`, `ruff check/format`. 재시작 보존은 `tests/unit/test_restart_resume.py`와 `e2e/restart.spec.ts`(e2e 스택의 `POST /restart`가 서버를 SIGTERM으로 내렸다 같은 설정으로 띄운다), 배포 스크립트는 `bash scripts/test-deploy-backend.sh`(macOS bash 3.2에서도 돈다).
- Android: `scripts/build-android.ps1`(PowerShell에서 실행), `adb install -r`. 기기 e2e는 `npm run verify`가 에뮬레이터에 평소 앱(`com.jari.app` 디버그 빌드, 로컬 e2e 서버 주소로 빌드)을 깔아 돌린다(별도 e2e 앱 없음). 디버그 APK는 WebView CDP(`adb forward tcp:9377 localabstract:webview_devtools_remote_<pid>`)로 DOM 계측이 가능하다.
- 실기기 감사 절차: 데모가 아니라 실서버로 전 경로(조회·좌석표·대기 시작/중지·시트·테마)를 돌리며 화면마다 버튼 높이·형제 간격·토큰 밖 색·터치 영역·넘침을 수집한다. 결제만 하지 않는다.

## 9. 알려진 제약·미결

- 릴리스 서명키 없음 → 디버그 APK 배포.
- 조건 바꾸기(멈추고 재시작) 미구현. 결제 대기 열차는 잠금으로 합의.
- 알림 항목은 서버 원문 문자열(제목·본문·기한 분리 미구현). 좌석 셀 라벨은 상태·위치 축이 섞여 있다.
- 즐겨찾기에 좌석표 자리(seat_plan) 저장 안 함.
- `korail_mobile_api` 상류 미수정(영숫자 종별 코드).
