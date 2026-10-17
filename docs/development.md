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

관리자 계정은 `curl http://127.0.0.1:18090/config`, 바로 사용할 수 있는 승인된 테스트 회원은 `curl -X POST http://127.0.0.1:18090/user`로 받습니다. 시나리오 바꾸기·재시작 같은 제어 명령은 [`stack.py`](../backend/tests/e2e/stack.py) 머리말에 있습니다.

다른 서버에 붙일 때는 서버의 `MOBILE_ORIGINS`에 개발 서버 주소(`http://127.0.0.1:4173`)를 더해야 브라우저 요청이 거절되지 않습니다.

## 코드 지도

| 위치 | 내용 |
|---|---|
| `src/main.ts` | 부트스트랩. 데모 모드면 샘플 API, 아니면 HTTP API |
| `src/app.ts` | `JariApp`: 화면 상태, 렌더링, 상태 폴링, 시트 |
| `src/model.ts` | 상태 판정(`deriveRadarView`), 조건↔초안 변환, 좌석 필터 |
| `src/api.ts`, `src/types.ts` | 서버 요청 구현과 API 타입 |
| `src/updates.ts`, `android/app/src/main/java/com/jari/app/updates/` | 앱 내 업데이트의 웹 경계와 네이티브 서명·다운로드·복구 |
| `src/demo.ts` | 데모·체험하기용 샘플 API |
| `backend/src/korail_bot/mobile/` | API(`api.py`), 런타임, 워커, 저장소, 알림, CLI(`__main__.py`) |
| `e2e/` | Playwright 흐름·레이아웃 검사 |
| `scripts/` | 빌드·릴리스·배포·검증 스크립트 |

## 테스트와 검증

검증은 세 겹입니다.

| 단계 | 언제 | 무엇을 |
|---|---|---|
| 커밋 훅 `.githooks/pre-commit` | 커밋할 때 | 바뀐 파일에 맞춰: `backend/`면 ruff·서버 유닛(병렬)·통합(2~3분), 그 밖의 코드면 타입(앱·e2e·monitor)·vitest(30초 안팎), 문서(`*.md`, `docs/`)만이면 건너뜀 |
| GitHub Actions [`verify.yml`](../.github/workflows/verify.yml) | `main`을 뺀 브랜치 푸시와 PR (`main` 푸시는 같은 커밋이라 다시 돌지 않음) | `checks`: 유닛·모듈 → 통합(실서버 + 가짜 코레일) → 헤드리스 e2e·레이아웃 측정<br>`device`: Android 에뮬레이터에서 기기에서만 의미 있는 단계와 시간 슬라이더 조작<br>`device-full`: 같은 흐름을 swangle 렌더러로 추가 검사. 필수 검사가 아니고 야간(03:00 한국)·수동·릴리스 태그에서만 돕니다 |
| `npm run verify` | 릴리스 태그를 붙이기 전, 로컬 | 위 전부 + 에뮬레이터 기기 단계. 통과한 내용을 기록하고, `v*` 태그 푸시는 이 기록이 있어야 통과합니다(`.githooks/pre-push`) |

CI 기기 단계는 에뮬레이터만 대상으로 화면을 깨우고 잠금 화면을 걷은 뒤, 실제 `Awake` 상태와 키가드 해제를 제한 시간 동안 확인합니다. 준비되지 않으면 전원·창 상태를 그대로 남기고 실패하며, 실물 기기의 화면이나 보안 설정은 건드리지 않습니다.

푸시한 뒤에는 `gh run watch`로 결과를 확인하고, 실패를 남긴 채 다음 일로 넘어가지 않습니다.

하나씩 돌릴 때:

```bash
npm test                                          # 앱 유닛·모듈 (vitest)
npm run build                                     # 타입 검사 + 번들
npm test -- scripts/build-apk-update.test.mjs src/updates.test.ts  # APK 업데이트 목록·화면
npx playwright test --project=phone-light         # 헤드리스 e2e (e2e 전용 스택과 웹 빌드를 스스로 띄웁니다)
npm run verify -- --ci                            # 기기 없이 CI와 같은 전체 검증

cd backend
uv run --frozen pytest tests/unit -q              # 서버 유닛
uv run --frozen pytest tests/e2e -q               # 통합: 실서버 + 가짜 코레일
uv run --frozen ruff check src tests && uv run --frozen ruff format --check src tests
uv run --frozen python ../scripts/smoke-backend.py
bash ../scripts/test-deploy-backend.sh            # 배포 스크립트 회귀 검사
```

패키지 업데이트의 네이티브 검사는 `cd android && ./gradlew testDebugUnitTest testReleaseUnitTest -PjariNoFirebase`로 실행합니다. Play 빌드에는 APK 설치 권한/코드가 없고, 직접 설치본에는 검증한 APK만 설치기로 전달되는지 확인합니다. 설정과 배포 절차는 [앱 내 업데이트](android.md#앱-내-업데이트)를 따릅니다.

`ANDROID_SERIAL=emulator-5554 npm run test:updates:device`는 서명된 HTTPS 목록 조회, 변조 거부, 설치 취소·재시도, **Android 시스템 설치 화면을 통한 실제 상위 버전 APK 설치**, 설치된 버전 코드와 Keystore 세션 보존을 검사합니다. 기준 APK 설치 이후 후보 APK는 `adb install`로 덮어쓰지 않습니다. 임시 키/디버그 전용 CA와 `.invalid` API 주소를 사용해 운영 API에 연결하지 않습니다. 임시 자산/설정은 끝에 복원하지만 에뮬레이터 앱을 지우므로 실폰은 거부합니다. macOS·Linux의 `npm run verify` 기기 단계에도 포함됩니다. Play의 실제 패키지 업데이트는 별도 Play 계정·배포 버전 조건을 충족한 [내부 앱 공유](https://developer.android.com/guide/playcore/in-app-updates/test)로 확인하며, 직접 APK 검사나 브라우저 대역을 Play 설치 성공으로 보고하지 않습니다.

다른 작업 트리의 e2e 스택이 기본 포트를 쓰고 있으면 `JARI_E2E_PORT_OFFSET=1000 npx playwright test --project=phone-light`처럼 포트 오프셋을 줍니다. `JARI_E2E_PORT_OFFSET=1000 npm run verify -- --ci`에도 같은 값이 전달되어 각 작업 트리의 API·제어·웹 포트를 함께 옮깁니다.

`npm run verify -- --only=<단계 이름 일부>`로 한 단계만 돌릴 수 있습니다(기록은 남기지 않습니다).

## 화면을 바꿀 때

- 화면·흐름을 바꾸면 `e2e/`에 흐름을 더하고, 새 화면·상태는 `expectCleanLayout`으로 검사합니다.
- 화면은 스크린숏이 아니라 실제 렌더링을 재서 규칙으로 검사합니다([`e2e/layout.ts`](../e2e/layout.ts)): 겹침, 넘침, 잘린 글자, 48px 터치 영역, 화면 여백선, 붙박이 버튼 간격. 새 디자인 규칙이 생기면 여기에 규칙을 더합니다.
- 한 화면에 들어오는 상태는 `expectCleanLayout(page, 이름, { noPageScroll: true })`로 불필요한 세로 스크롤도 검사합니다. 배너·기기 상단 여백을 포함해 재고, 긴 설정·폼·목록의 정상 스크롤은 유지합니다.
- e2e는 밝은 테마(`phone-light`), 어두운 테마(`phone-dark`), 좁은 폰(`phone-narrow`)에서 돕니다.
- 코레일 테스트 객체는 실제 라이브러리 클래스로 만듭니다(`backend/tests/e2e/fake_korail`).
- 조회 실패와 좌석 없음을 구분합니다. 문구는 [PRD의 문구 원칙](PRD.md#7-문구-원칙)을 따릅니다.

## 에뮬레이터 기기 단계

`npm run verify`의 기기 단계는 별도 e2e 앱 없이 평소 앱(`dev.thsvkd.jari` 디버그 빌드, `-PjariNoFirebase`)을 로컬 e2e 서버 주소로 빌드해 에뮬레이터에 설치하고, **앱 데이터를 비운 뒤** `adb reverse`로 서버에 연결해 돌립니다.

- 에뮬레이터를 띄우고 잠금을 풀어 둡니다. 기기가 여러 대면 `ANDROID_SERIAL=<serial> npm run verify`.
- 실폰은 앱을 덮어쓰고 지우므로 거부합니다. 꼭 필요할 때만 `JARI_ALLOW_PHYSICAL_DEVICE=1`을 줍니다.
- 끝나면 `dist/`에 로컬 서버용 번들이 남습니다. 실사용 APK를 만들기 전에 웹 번들을 다시 빌드합니다.

### 시간 슬라이더와 기기 검사

시간 선택은 시·분 슬라이더를 사용합니다. `layout.spec`은 손잡이 조작, 5분 단위 입력, 다음 칸 이동과 레이아웃을 헤드리스와 기기에서 모두 검사합니다. `pickTime`은 같은 슬라이더를 사용합니다.

과거 시간 휠을 넘기는 장면에서는 SwiftShader 에뮬레이터가 사라지는 문제가 있어 기기 검사를 분리했습니다. 현재 슬라이더 검사에는 그 장면을 건너뛰는 조건이 없습니다. 야간·수동·릴리스 태그의 `device-full`은 swangle 렌더러에서도 추가 검사합니다.

`device`가 실패했을 때 실패 기록(`adb-devices.txt`)에 에뮬레이터가 없으면 그 실행만 한 번 다시 돌립니다. 에뮬레이터가 살아 있는 실패는 고칩니다.

## 브랜치와 커밋

- `main`은 CI(`checks`·`device`)를 통과한 커밋만 받습니다(저장소 규칙). 작업 브랜치를 푸시해 초록불을 본 뒤 같은 커밋을 `main`에 fast-forward로 올립니다. 강제 푸시와 삭제는 막혀 있습니다.
- 여러 세션이 함께 일할 때: `main`에 올리는 일은 한 번에 한 세션이 합니다. 같은 브랜치에는 한 세션만 푸시합니다(새 푸시가 앞 CI 실행을 취소합니다). 세션을 시작하면 `gh run list -b main -L 3`으로 야간 `device-full` 상태를 먼저 봅니다(`main` 푸시에는 CI가 돌지 않습니다). 야간 실패는 다음에 시작하는 세션이 첫 일로 맡습니다.
- 커밋 메시지는 한국어 현재형 평서문으로 씁니다. 예: `열차표의 버튼 밖 영역에서 좌석 무관 선택과 해제를 처리한다`
- 비밀번호·토큰·서명키·Firebase 설정은 커밋하지 않습니다. `VITE_` 값은 앱에 그대로 들어가는 공개 값이므로 비밀값을 넣지 않습니다.
- 실제 예약·취소·계정 등록·배포는 실서비스에 영향을 주므로 하기 전에 확인을 받습니다.

## 문서를 고칠 때

- `README.md`(한국어)와 `README.en.md`(영어)는 함께 고칩니다.
- 운영·빌드·개발 절차는 `docs/self-hosting.md`, `docs/android.md`, `docs/development.md`에, 제품 의도는 `docs/PRD.md`, 구조·계약은 `docs/SPEC.md`에 둡니다.
- `docs/superpowers/`는 작업별 설계·계획 기록입니다. 현재 구현 기준은 SPEC을 봅니다.
