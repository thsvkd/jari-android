# 개발 가이드

코드를 고치고 검증하는 방법입니다. 제품 의도는 [PRD](PRD.md), 내부 구조와 API 계약은 [SPEC](SPEC.md)에 있습니다.

- [준비물](#준비물)
- [처음 한 번](#처음-한-번)
- [로컬에서 띄우기](#로컬에서-띄우기)
- [코드 지도](#코드-지도)
- [테스트와 검증](#테스트와-검증)
- [화면을 바꿀 때](#화면을-바꿀-때)
- [에뮬레이터 기기 단계](#에뮬레이터-기기-단계)
- [브랜치와 커밋](#브랜치와-커밋)
- [문서를 고칠 때](#문서를-고칠-때)

## 준비물

| 항목 | 용도 |
|---|---|
| Node.js 22.12 이상, npm | 앱(CI는 Node 24) |
| Python 3.13 이상, [uv](https://docs.astral.sh/uv/) | 서버와 통합 테스트 |
| Playwright Chromium | 헤드리스 e2e (`npx playwright install chromium`) |
| JDK 21, Android SDK, 에뮬레이터 | 기기 단계와 APK 빌드(→ [Android 빌드·배포](android.md#준비물)) |

Redis와 Docker는 테스트에 필요하지 않습니다. 통합 테스트와 e2e는 가짜 Redis(fakeredis)를 씁니다.

## 처음 한 번

```bash
npm ci                          # 의존성 설치와 함께 git 훅(.githooks)을 켭니다
(cd backend && uv sync --frozen)
```

## 로컬에서 띄우기

**데모 모드** — 서버 없이 샘플 데이터로 화면만 봅니다.

```bash
npm run dev -- --mode demo      # http://127.0.0.1:4173
```

**실제 서버 + 가짜 코레일** — 서버와 예약 워커는 운영과 같은 코드로 돌고, 코레일 호출만 [`backend/tests/e2e/fake_korail`](../backend/tests/e2e/fake_korail)이 시나리오대로 답합니다. 실제 코레일에는 닿지 않습니다.

```bash
# 터미널 1: 개발용 스택(API 18081, 제어 서버 18090)
cd backend
uv run --frozen python tests/e2e/stack.py --origin http://127.0.0.1:4173

# 터미널 2: 그 서버를 바라보는 개발 서버
VITE_API_BASE_URL=http://127.0.0.1:18081 npm run dev
```

관리자 계정은 `curl http://127.0.0.1:18090/config`, 새 초대 회원은 `curl -X POST http://127.0.0.1:18090/user`로 받습니다. 시나리오 바꾸기·재시작 같은 제어 명령은 [`stack.py`](../backend/tests/e2e/stack.py) 머리말에 있습니다.

다른 서버에 붙일 때는 서버의 `MOBILE_ORIGINS`에 개발 서버 주소(`http://127.0.0.1:4173`)를 더해야 브라우저 요청이 거절되지 않습니다.

## 코드 지도

| 위치 | 내용 |
|---|---|
| `src/main.ts` | 부트스트랩. 데모 모드면 샘플 API, 아니면 HTTP API |
| `src/app.ts` | `JariApp`: 화면 상태, 렌더링, 상태 폴링, 시트 |
| `src/model.ts` | 상태 판정(`deriveRadarView`), 조건↔초안 변환, 좌석 필터 |
| `src/api.ts`, `src/types.ts` | 서버 요청 구현과 API 타입 |
| `src/demo.ts` | 데모·체험하기용 샘플 API |
| `backend/src/korail_bot/mobile/` | API(`api.py`), 런타임, 워커, 저장소, 알림, CLI(`__main__.py`) |
| `e2e/` | Playwright 흐름·레이아웃 검사 |
| `scripts/` | 빌드·릴리스·배포·검증 스크립트 |

## 테스트와 검증

검증은 세 겹입니다.

| 단계 | 언제 | 무엇을 |
|---|---|---|
| 커밋 훅 `.githooks/pre-commit` | 커밋할 때 | 빠른 검사(30초 안팎): 타입(앱·e2e·monitor), vitest, ruff |
| GitHub Actions [`verify.yml`](../.github/workflows/verify.yml) | 모든 브랜치 푸시와 PR | `checks`: 유닛·모듈 → 통합(실서버 + 가짜 코레일) → 헤드리스 e2e·레이아웃 측정<br>`device`: Android 에뮬레이터에서 기기에서만 의미 있는 단계(시간 휠 장면 제외, 15분 안팎)<br>`device-full`: 휠까지 전부, swangle 렌더러(약 28분). 필수 검사가 아니고 야간(03:00 한국)·수동·릴리스 태그에서만 돕니다 |
| `npm run verify` | 릴리스 태그를 붙이기 전, 로컬 | 위 전부 + 에뮬레이터 기기 단계. 통과한 내용을 기록하고, `v*` 태그 푸시는 이 기록이 있어야 통과합니다(`.githooks/pre-push`) |

푸시한 뒤에는 `gh run watch`로 결과를 확인하고, 실패를 남긴 채 다음 일로 넘어가지 않습니다.

하나씩 돌릴 때:

```bash
npm test                                          # 앱 유닛·모듈 (vitest)
npm run build                                     # 타입 검사 + 번들
npx playwright test --project=phone-light         # 헤드리스 e2e (e2e 전용 스택과 웹 빌드를 스스로 띄웁니다)
npm run verify -- --ci                            # 기기 없이 CI와 같은 전체 검증

cd backend
uv run --frozen pytest tests/unit -q              # 서버 유닛
uv run --frozen pytest tests/e2e -q               # 통합: 실서버 + 가짜 코레일
uv run --frozen ruff check src tests && uv run --frozen ruff format --check src tests
uv run --frozen python ../scripts/smoke-backend.py
bash ../scripts/test-deploy-backend.sh            # 배포 스크립트 회귀 검사
```

`npm run verify -- --only=<단계 이름 일부>`로 한 단계만 돌릴 수 있습니다(기록은 남기지 않습니다).

## 화면을 바꿀 때

- 화면·흐름을 바꾸면 `e2e/`에 흐름을 더하고, 새 화면·상태는 `expectCleanLayout`으로 검사합니다.
- 화면은 스크린숏이 아니라 실제 렌더링을 재서 규칙으로 검사합니다([`e2e/layout.ts`](../e2e/layout.ts)): 겹침, 넘침, 잘린 글자, 48px 터치 영역, 화면 여백선, 붙박이 버튼 간격. 새 디자인 규칙이 생기면 여기에 규칙을 더합니다.
- e2e는 밝은 테마(`phone-light`), 어두운 테마(`phone-dark`), 좁은 폰(`phone-narrow`)에서 돕니다.
- 코레일 테스트 객체는 실제 라이브러리 클래스로 만듭니다(`backend/tests/e2e/fake_korail`).
- 조회 실패와 좌석 없음을 구분합니다. 문구는 [PRD의 문구 원칙](PRD.md#7-문구-원칙)을 따릅니다.

## 에뮬레이터 기기 단계

`npm run verify`의 기기 단계는 별도 e2e 앱 없이 평소 앱(`dev.thsvkd.jari` 디버그 빌드, `-PjariNoFirebase`)을 로컬 e2e 서버 주소로 빌드해 에뮬레이터에 설치하고, **앱 데이터를 비운 뒤** `adb reverse`로 서버에 연결해 돌립니다.

- 에뮬레이터를 띄우고 잠금을 풀어 둡니다. 기기가 여러 대면 `ANDROID_SERIAL=<serial> npm run verify`.
- 실폰은 앱을 덮어쓰고 지우므로 거부합니다. 꼭 필요할 때만 `JARI_ALLOW_PHYSICAL_DEVICE=1`을 줍니다.
- 끝나면 `dist/`에 로컬 서버용 번들이 남습니다. 실사용 APK를 만들기 전에 웹 번들을 다시 빌드합니다.

### 시간 휠은 기기에서 따로

에뮬레이터는 호스트의 소프트웨어 렌더러(SwiftShader)가 시간 휠을 넘기는 장면에서 말없이 죽는 일이 잦습니다(에뮬레이터 프로세스가 통째로 사라지고, 뒤 테스트가 `ECONNREFUSED`로 줄줄이 실패합니다). 2026-10-10 CI 실험에서 이 장면만 빼면 같은 설정으로 5/5 살아남았고, 휠까지 넣은 전체도 swangle 렌더러에서는 2/2 살아남았습니다.

- 기기 프로젝트는 `layout.spec`의 휠 장면을 건너뜁니다(`wheelHere`). 헤드리스 세 프로젝트는 계속 잽니다. 기기에서도 보려면 `JARI_DEVICE_WHEEL=1`(CI `device-full`이 줍니다).
- 시간 휠을 넘기거나 열어 둔 채 재는 새 e2e는 같은 `wheelHere`로 감쌉니다. 휠을 단순히 열어 시각을 고르는 `pickTime`은 기기에서도 안전합니다.
- `device`가 실패했을 때 실패 기록(`adb-devices.txt`)에 에뮬레이터가 없으면 앱 실패가 아니라 에뮬레이터가 사라진 것입니다(실행 요약에 경고가 뜹니다). 그 실행만 한 번 다시 돌립니다. 에뮬레이터가 살아 있는 실패는 진짜 실패이니 고칩니다.

## 브랜치와 커밋

- `main`은 CI(`checks`·`device`)를 통과한 커밋만 받습니다(저장소 규칙). 작업 브랜치를 푸시해 초록불을 본 뒤 같은 커밋을 `main`에 fast-forward로 올립니다. 강제 푸시와 삭제는 막혀 있습니다.
- 여러 세션이 함께 일할 때: `main`에 올리는 일은 한 번에 한 세션이 합니다. 같은 브랜치에는 한 세션만 푸시합니다(새 푸시가 앞 CI 실행을 취소합니다). 세션을 시작하면 `gh run list -b main -L 3`으로 야간 `device-full`과 `main`의 상태를 먼저 봅니다. 야간 실패는 다음에 시작하는 세션이 첫 일로 맡습니다.
- 커밋 메시지는 한국어 현재형 평서문으로 씁니다. 예: `열차표의 버튼 밖 영역에서 좌석 무관 선택과 해제를 처리한다`
- 비밀번호·토큰·서명키·Firebase 설정은 커밋하지 않습니다. `VITE_` 값은 앱에 그대로 들어가는 공개 값이므로 비밀값을 넣지 않습니다.
- 실제 예약·취소·계정 등록·배포는 실서비스에 영향을 주므로 하기 전에 확인을 받습니다.

## 문서를 고칠 때

- `README.md`(한국어)와 `README.en.md`(영어)는 함께 고칩니다.
- 운영·빌드·개발 절차는 `docs/self-hosting.md`, `docs/android.md`, `docs/development.md`에, 제품 의도는 `docs/PRD.md`, 구조·계약은 `docs/SPEC.md`에 둡니다.
- `docs/superpowers/`는 2026-09-14 좌석 선택·취소표 대기 작업의 설계·계획 기록입니다. 지금 기준은 SPEC이 대신합니다.
