# 자리났다 Android

텔레그램 없이 사용하는 지인 초대형 기차 예약 앱과 전용 Python API 서버입니다.
선택한 **상태 카드형** 검색 현황, 여정 조건, 검색·예약 관리, 즐겨찾기,
철도 계정 및 알림 화면을 포함합니다. 결제는 앱이 대행하지 않습니다.

## 저장소 범위와 현재 상태

기존 `korail_KTX_macro_telegrambot`의 Android 전용 작업에서 앱과 백엔드를 분리했습니다.
기존 봇과 작업 중인 백엔드는 이동하거나 삭제하지 않았습니다.
`backend/`에는 전용 API·작업 프로세스·계정/알림 저장소와 필요한 공통 패키지가 포함됩니다.
원래 저장소나 외부 로컬 경로를 참조하지 않습니다. 실사용에는 Redis와 배포된 HTTPS 주소가
필요하며 API 타입은 `src/types.ts`, 요청 구현은 `src/api.ts`에 있습니다.
기존 패키지명 `korail_bot`과 버전/잠금 파일은 호환성을 위해 보존했습니다.
원저작자 및 fork의 MIT 저작권은 [LICENSE](LICENSE)에 보존합니다.

- 모바일 테스트 101개, 일반/데모 빌드와 S26 Ultra 데모 화면 검증을 수행했습니다.
- 실철도 예약·결제 및 푸시 수신의 종단 간 검증은 완료되지 않았습니다.
- SRT는 지원하지 않습니다. 코레일 예약 대기는 일반실에만, 좌석 위치 지정 없이 신청합니다.
- 배포 서명키와 Firebase 설정은 포함하지 않습니다. 현재 APK 빌드는 디버그용입니다.
- 앱 이름을 바꾸면서 Android `applicationId`가 `com.jari.app`으로 바뀌었습니다.
  기존 앱 위에 덮어쓰는 업데이트가 아니라 별도 앱으로 설치되고, 사용자는 다시 로그인해야 합니다.
  즐겨찾기와 코레일 계정 연결은 서버에 있으므로 그대로 유지됩니다.
  Firebase도 새 패키지명으로 Android 앱을 다시 등록해 `google-services.json`을 받아야 합니다.

## 로컬 시작

Node.js 22.12 이상과 npm을 준비합니다. 저장소 루트에서:

```powershell
npm ci
npm test
npm run dev -- --mode demo
```

브라우저에서 `http://127.0.0.1:4173`을 엽니다. 데모는 샘플 데이터로만 동작합니다.
실서버를 사용할 때는 `.env.example`을 `.env.local`로 복사해
`VITE_API_BASE_URL`을 설정하고 `npm run build`로 빌드합니다.
`VITE_` 설정은 앱에 포함되는 공개 값이므로 비밀값을 넣지 않습니다.

데모 APK:

```powershell
npm run build:demo
.\scripts\build-android.ps1 -SkipWebBuild
```

## 서버 실행

Windows에서는 아래 명령으로 로컬 키와 환경 파일을 준비합니다. 키는 출력하지 않고,
기존 키가 있으면 보존합니다. `.secrets`와 `.env`는 Git에서 제외됩니다.
Firebase 키가 없으면 빈 `backend/.secrets/firebase-admin.json`을 만들어 두어 compose가
기동하되 푸시는 꺼집니다. 실제 서비스 계정 키로 이 파일을 덮어쓰면 푸시가 켜집니다.

```powershell
.\scripts\init-backend.ps1
docker compose up --build -d
docker compose exec api python -m korail_bot.mobile invite --ttl-hours 24
```

Docker Desktop 또는 Docker Engine이 필요합니다. 이 스택은 **텔레그램 봇을 실행하지 않습니다.**
Redis 포트는 외부로 공개하지 않고 API도 호스트의 `127.0.0.1:8081`에만 바인딩합니다.
휴대폰에서 실사용하려면 이 주소 앞에 HTTPS 프록시를 구성하고, 루트 `.env.local`의
`VITE_API_BASE_URL`을 해당 HTTPS 주소로 지정한 후 앱을 다시 빌드해야 합니다.
초대 코드는 운영자가 안전하게 전달합니다. SQLite/Redis 볼륨과 암호화 키를 함께 백업하며,
`docker compose down --volumes`는 데이터를 삭제하므로 사용하지 않습니다.

Docker 없이 인증 API만 검증하려면 Python 3.13+와 uv를 설치하고:

```powershell
cd backend
uv sync --frozen
uv run --env-file .env --frozen python -m korail_bot.mobile serve --auth-only --host 127.0.0.1 --port 8081
```

`--auth-only`는 Redis/철도 연결 없이 실제 앱 인증 API만 실행합니다.
전체 서버·푸시·보안 설정은 [backend/README.md](backend/README.md)를 참고하세요.
서명키, API 도메인/TLS 인증서, 철도 계정, Firebase 자격증명은 사용자 환경에서 준비해야 합니다.
외부 패키지는 `package-lock.json`과 `backend/uv.lock`에 고정되어 설치됩니다.

### 서버 테스트

```powershell
cd backend
uv run --frozen pytest tests/unit -q
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv run --frozen python ../scripts/smoke-backend.py
```

현재 포함된 서버 테스트는 독립 앱 API·인증·런타임·예약·알림에 대한 회귀 테스트이며,
원본 텔레그램 UI 전용 테스트는 이 저장소에 포함하지 않습니다.
배포 스크립트는 아래 `scripts/test-deploy-backend.sh`로 따로 검증합니다.
실제 예약/푸시 검증과 Docker 실행 검증은 별도이며, 미검증 항목을 완료로 취급하지 않습니다.

### 원격 배포

`scripts/deploy-backend.sh`는 SSH로 접근 가능한 아무 Docker 호스트에 `backend/`를
배포합니다. 호스트별 정보는 하드코딩하지 않고 플래그 또는 환경 변수로 받습니다.

```bash
./scripts/deploy-backend.sh \
  --host pi@example --root /srv/app \
  --compose-file compose.yaml --compose-file compose.overlay.example.yaml \
  --after "docker compose -f compose.overlay.example.yaml up -d proxy" \
  --dry-run   # 실제 호스트 없이 계획만 출력. 실행 전 항상 먼저 확인합니다.
```

환경 변수로도 동일하게 지정할 수 있습니다(`DEPLOY_HOST`, `DEPLOY_ROOT`,
`DEPLOY_REF`, `DEPLOY_COMPOSE_FILES`(쉼표 구분), `DEPLOY_SERVICE`, `DEPLOY_WORKER_PATTERN`).
게이트웨이·프록시 등록처럼 호스트별 후속 작업은 `--after "<원격 명령>"`으로 넘깁니다.
공백이 든 값(`--after`, `--root` 등)도 원격에서 그대로 한 인자로 전달됩니다.
`scripts/test-deploy-backend.sh`가 문법과 `--dry-run` 출력 계획을 회귀 검증합니다
(가짜 `ssh`로 실제 접속이 일어나지 않는지도 확인).

## Android packaging details

The release WebView only serves the Vite bundle copied from `dist`. It has no
configured live-server URL, permits no arbitrary navigation, and uses HTTPS
for the Capacitor origin. Android release builds therefore do not allow
cleartext HTTP. A debug-only network policy permits `localhost` and `10.0.2.2`
for emulator API verification; it is not included in release builds.

## Prerequisites

- Node dependencies supplied by the frontend package owner, including the
  Capacitor 7 packages listed below.
- JDK 21 or later (the Capacitor Push Notifications Android module is compiled
  for Java 21). The script checks the actual `java -version` output from
  `JAVA_HOME`; an invalid or older explicit setting stops the build. When
  `JAVA_HOME` is unset, it discovers a supported Temurin installation under
  `%ProgramFiles%\Eclipse Adoptium`.
- Android SDK Platform 35 and Build Tools 35.0.1. Set `ANDROID_HOME` when the
  SDK is not installed in `%LOCALAPPDATA%\Android\Sdk`.

From this directory, build with:

```powershell
.\scripts\build-android.ps1
```

The resulting debug artifact is
`android/app/build/outputs/apk/debug/app-debug.apk`. To use an installed
emulator, run `adb install -r` on that file only after `adb devices` identifies
an emulator; this project never installs onto a physical device automatically.

The script stops on a nonzero exit from Java validation, the web build,
Capacitor sync, or Gradle. Failed web builds or syncs never reach Gradle or
print the `APK:` success line. `-SkipWebBuild` intentionally reuses the existing
web bundle; sync and Gradle failures are still checked. Explicit exit checks
also work in Windows PowerShell 5.1; see Microsoft's
[native command error handling reference](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_error_handling?view=powershell-7.6).

### Script regression checks (no APK build)

Run from the repository root using Windows PowerShell 5.1 or PowerShell 7:

```powershell
.\scripts\test-build-android.ps1
# Optional: validate a real installed JDK while all build commands stay mocked.
.\scripts\test-build-android.ps1 -RealJavaHome $env:JAVA_HOME
```

The standalone suite needs the Windows .NET Framework C# compiler (included
with .NET Framework 4.x). It copies the build script into a temporary fixture,
injects native executable failures, and checks exit status, command order,
and success output. It covers npm/sync/Gradle/Java failures, unsupported and
unreadable Java versions, JDK 21 and newer, SDK discovery, and `-SkipWebBuild`.
It restores environment variables and removes its temporary files; it does
not invoke the repository's real npm, Capacitor, or Gradle commands.

## Required frontend dependencies

Keep the Capacitor package versions on the same supported 7.x release:

```text
dependencies: @capacitor/core, @capacitor/android, @capacitor/app,
              @capacitor/push-notifications
devDependencies: @capacitor/cli
```

`@capacitor/preferences` must not store the app session: `SecureSessionPlugin`
uses an AES-GCM key generated in Android Keystore and stores only its IV and
ciphertext in private SharedPreferences.

## Push is intentionally unavailable until configured

Do not add a Firebase configuration file or cloud credentials to this
repository. The client requests Android 13 notification permission and calls
FCM registration only when the frontend passes `enablePushRegistration: true`
and supplies `onPushToken`. Without a valid Firebase Android app configuration,
the registration error is surfaced to the UI and no delivery is claimed. A
future operator must provision Firebase outside this repository for the current
`com.jari.app` package name, place the appropriate non-secret runtime
configuration through the approved Android release process, and configure the
server's authenticated `/api/mobile/devices` endpoint before enabling that
option. A `google-services.json` issued for the former package name no longer
matches this build.
