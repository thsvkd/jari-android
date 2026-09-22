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
- 워커는 **API 컨테이너 안의 자식 프로세스**다. API를 재시작하면 워커도 죽는다. 배포 스크립트는 워커가 있으면 거부한다(`--force` 시 재시작 후 `reconcile_after_restart`가 기록을 보고 다시 띄우려 하지만 보장하지 않는다).
- 실서버: pit5, `scripts/deploy-backend.sh --host pit5 --root /home/pi/services/teum-android --compose-file compose.yaml --compose-file pit5-edge.yaml --ref <sha>`. 배포 전 이미지는 `jari-api:pre-<sha>`로 태그된다.

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

## 7. 확인 시트

`confirmSheet({ title, body, confirmLabel, danger })` → `openSheet` 기반, 상태는 `this.sheet`(재렌더에 견딤), Escape·배경 탭·취소 = false. 위험 동작은 `.button.danger`(빨강 채움). `window.confirm`은 쓰지 않는다. 사용처: 그만 찾기, 예약 전체 취소, 코레일 계정 해제, 즐겨찾기 삭제, 바로 예약, 코레일 예약 대기 신청.

## 8. 검증

- 앱: `npm test`(vitest/jsdom), `npm run build`, `npx tsc --noEmit`. `npm run build:demo`는 `dist/`를 덮어쓰므로 APK 빌드 전엔 `npm run build`를 다시 돌린다.
- 서버: `backend/`에서 `uv run --frozen pytest tests/unit -q`, `ruff check/format`.
- Android: `scripts/build-android.ps1`(PowerShell에서 실행), `adb install -r`. 디버그 APK는 WebView CDP(`adb forward tcp:9377 localabstract:webview_devtools_remote_<pid>`)로 DOM 계측이 가능하다.
- 실기기 감사 절차: 데모가 아니라 실서버로 전 경로(조회·좌석표·대기 시작/중지·시트·테마)를 돌리며 화면마다 버튼 높이·형제 간격·토큰 밖 색·터치 영역·넘침을 수집한다. 결제만 하지 않는다.

## 9. 알려진 제약·미결

- 릴리스 서명키 없음 → 디버그 APK 배포.
- 조건 바꾸기(멈추고 재시작) 미구현. 결제 대기 열차는 잠금으로 합의.
- 알림 항목은 서버 원문 문자열(제목·본문·기한 분리 미구현). 좌석 셀 라벨은 상태·위치 축이 섞여 있다.
- 즐겨찾기에 좌석표 자리(seat_plan) 저장 안 함.
- `korail_mobile_api` 상류 미수정(영숫자 종별 코드).
- pit5 배포 디렉터리 이름이 `teum-android`(내용은 jari).
