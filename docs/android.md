# Android 빌드·배포

앱을 빌드하고 서명해 배포하는 방법입니다. 서버를 직접 운영하면서 그 서버에 연결되는 앱만 필요하다면 [서버 운영 가이드 5번](self-hosting.md#5-앱을-내-서버에-연결하기)으로 충분합니다.

- [한눈에 보기](#한눈에-보기)
- [준비물](#준비물)
- [디버그 APK 만들기](#디버그-apk-만들기)
- [릴리스](#릴리스)
- [휴대폰 알림 (Firebase)](#휴대폰-알림-firebase)
- [앱 보안 설정](#앱-보안-설정)
- [패키지 이름 변경 이력](#패키지-이름-변경-이력)
- [Windows에서 빌드하기](#windows에서-빌드하기)

## 한눈에 보기

| 만들 것 | 명령 | 서명 | 쓰는 곳 |
|---|---|---|---|
| 디버그 APK | `./gradlew assembleDebug` | 이 PC의 디버그 키 | 개발, 에뮬레이터, 직접 운영하는 서버 |
| 사이드로드 APK | `scripts/release-android.sh` | 지금까지의 릴리스와 같은 디버그 키(Doppler) | [GitHub Releases](https://github.com/thsvkd/jari-android/releases) |
| Play AAB | `scripts/release-play.sh` | 업로드 키(Doppler) | Google Play 내부 테스트 |

Play에서 받은 앱(Play 앱 서명 키)과 사이드로드 APK는 같은 패키지여도 서명이 달라 **서로 업데이트되지 않습니다.** 한 휴대폰에서는 한쪽만 쓰고, 바꿀 때는 지우고 다시 설치합니다.

## 준비물

| 항목 | 버전 | 비고 |
|---|---|---|
| Node.js·npm | 22.12 이상 | 웹 번들과 Capacitor 동기화 |
| JDK | 21 이상 | Capacitor 알림 모듈이 Java 21로 컴파일됩니다. macOS(brew)의 `openjdk@21`은 스크립트가 자동으로 찾습니다. |
| Android SDK | Platform 36 | `ANDROID_HOME`을 지정합니다. macOS(brew)의 `android-commandlinetools`는 스크립트가 자동으로 찾습니다. |
| (릴리스) Doppler CLI | | 서명키와 Firebase 설정을 빌드할 때만 꺼냅니다. |

프로젝트 설정: `minSdk` 24(Android 7.0), `compileSdk`·`targetSdk` 36, AGP 8.9.3, Gradle 8.11.1(래퍼 포함). Capacitor 패키지(`@capacitor/core`, `android`, `app`, `push-notifications`, `cli`)는 같은 7.x 버전으로 맞춥니다.

## 디버그 APK 만들기

```bash
VITE_API_BASE_URL=https://api.example.com npm run build   # 연결할 서버 주소
npx cap sync android
(cd android && ./gradlew assembleDebug)
```

결과물은 `android/app/build/outputs/apk/debug/app-debug.apk`입니다.

- 서버 없이 샘플 데이터로 도는 데모 APK는 첫 줄 대신 `npm run build:demo`를 씁니다. `build:demo`는 `dist/`를 덮어쓰므로 실사용 APK를 만들기 전에 `npm run build`를 다시 돌립니다.
- 에뮬레이터에 설치: `adb devices`로 에뮬레이터인지 확인한 뒤 `adb install -r <apk>`. 이 프로젝트의 스크립트는 실폰에 자동으로 설치하지 않습니다.
- `npm run verify`의 기기 단계는 로컬 e2e 서버 주소로 만든 번들을 `dist/`에 남깁니다. 실사용 APK를 만들기 전에 웹 번들을 다시 빌드합니다.

## 릴리스

릴리스는 macOS 한 대에서 끝냅니다(Windows 불필요). 순서는 다음과 같습니다.

1. 버전을 올립니다: `package.json`(+`package-lock.json`), `backend/pyproject.toml`(+`backend/uv.lock`), `android/app/build.gradle`의 `versionCode`·`versionName`, 서버 릴리스 노트 `backend/src/korail_bot/release_notes.py`.
2. 작업 브랜치를 푸시해 GitHub Actions(`checks`·`device`)가 통과하면 같은 커밋을 `main`에 fast-forward로 올립니다(→ [개발 가이드](development.md#브랜치와-커밋)).
3. 태그를 붙일 커밋을 체크아웃하고 바뀐 파일이 없는 상태에서 `npm run verify`(에뮬레이터 기기 단계 포함)를 통과시킵니다. 통과 기록이 없으면 `.githooks/pre-push`가 `v*` 태그 푸시를 막습니다.
4. 아래 스크립트로 APK·AAB를 만들고, 태그를 푸시한 뒤 `gh release create --verify-tag`로 APK를 올립니다. AAB는 사람이 Play Console에 올립니다.

### 사이드로드 APK

```bash
scripts/release-android.sh                          # 기본: 지금까지의 릴리스와 같은 디버그 키
JARI_SIGNING_KEY=release scripts/release-android.sh # 업로드 키로 서명
```

웹 번들(운영 API 주소) → Capacitor 동기화 → `assembleDebug` → 서명 → 인증서 지문 확인 순서로 돌고 `dist-release/jari-<버전>-<키>.apk`를 만듭니다. 디버그 키의 인증서 지문이 지금까지의 릴리스와 다르면 기존 앱 위에 업데이트되지 않으므로 빌드하기 전에 멈춥니다. 서명한 APK의 지문이 키스토어와 다르면 APK를 지웁니다.

| 환경 변수 | 뜻 |
|---|---|
| `JARI_SIGNING_KEY` | `debug`(기본) 또는 `release` |
| `JARI_API_BASE_URL` | 앱에 넣을 API 주소(기본: 운영 서버) |
| `JARI_DOPPLER_PROJECT`·`JARI_DOPPLER_CONFIG` | Doppler 위치(기본 `dev`/`dev`) |
| `JARI_KEYSTORE_FILE`·`JARI_KEYSTORE_PASSWORD`·`JARI_KEY_ALIAS` | Doppler를 쓰지 못할 때 로컬 키스토어 |
| `JAVA_HOME`·`ANDROID_HOME` | 자동으로 찾지 못할 때 지정 |

### Google Play (내부 테스트)

```bash
scripts/release-play.sh   # → dist-release/jari-<버전>-play.aab
```

업로드 키(Doppler `JARI_ANDROID_RELEASE_*`)로 서명한 `bundleRelease` AAB를 만듭니다. 릴리스 빌드라 WebView 디버깅이 꺼집니다. Firebase 설정(`JARI_ANDROID_GOOGLE_SERVICES_B64`)이 없으면 알림이 꺼진 앱이 되므로 멈춥니다. 끝에 서명·인증서 지문, versionCode/Name, 매니페스트(디버그 불가, 광고 ID 권한 없음, targetSdk), 광고·분석 의존성이 없는지, 웹 번들의 API 주소를 확인하고 모두 통과해야 최종 파일 이름으로 옮깁니다.

### 서명키

서명키는 저장소에 두지 않고 Doppler에 보관하다가 빌드할 때만 임시 파일로 꺼냅니다. 키를 Doppler에 올리는 것은 `scripts/doppler-store-android-keys.sh`입니다.

```bash
scripts/doppler-store-android-keys.sh debug <키스토어> <저장소 비밀번호> <별칭> [키 비밀번호]  # 기존 디버그 키 저장
scripts/doppler-store-android-keys.sh release-new                                               # 새 업로드 키 만들기
```

이미 저장된 키는 덮어쓰지 않습니다(`FORCE=1`이면 덮어쓰며, 이전 키는 되찾을 수 없습니다).

## 휴대폰 알림 (Firebase)

- `android/app/google-services.json`은 **커밋하지 않습니다.** 릴리스 스크립트가 Doppler의 `JARI_ANDROID_GOOGLE_SERVICES_B64`를 빌드하는 동안만 풀어 넣고 끝나면 지웁니다.
- 파일은 현재 패키지 `dev.thsvkd.jari`로 등록한 Firebase Android 앱의 것이어야 합니다. 예전 패키지 이름으로 받은 파일은 맞지 않습니다.
- 서버의 Admin 키(`firebase-admin.json`)와 **같은 Firebase 프로젝트**여야 합니다.
- 파일이 없으면 알림 플러그인이 빠진 채로 빌드됩니다. 앱은 알림 등록 오류를 화면에 보여 주고 전달됐다고 표시하지 않습니다. 기기 e2e는 `-PjariNoFirebase`로 빌드해 권한·토큰이 끼지 않게 합니다.
- 앱은 사용자가 **설정 → 휴대폰 알림**을 켤 때만 Android 13 알림 권한을 묻고 FCM에 등록합니다. 전체 경로는 [SPEC §11](SPEC.md#11-android-빌드배포와-휴대폰-알림)에 있습니다.

## 앱 보안 설정

- 릴리스 WebView는 `dist`에서 복사한 번들만 보여 줍니다. 라이브 서버 주소를 두지 않고, 임의 이동을 막고, Capacitor 출처는 HTTPS(`https://localhost`)입니다.
- 릴리스 빌드는 평문 HTTP를 막습니다. 디버그 빌드에만 에뮬레이터 확인용으로 `localhost`·`10.0.2.2`를 허용하는 네트워크 정책이 있습니다.
- 앱 세션은 `@capacitor/preferences`에 저장하지 않습니다. `SecureSessionPlugin`이 Android Keystore에서 만든 AES-GCM 키로 암호화해 IV와 암호문만 앱 전용 SharedPreferences에 둡니다.
- 디버그 APK는 WebView 원격 디버깅이 켜져 있습니다. 서명한 릴리스 빌드에는 없습니다.

## 패키지 이름 변경 이력

| 버전 | `applicationId` | 알아 둘 점 |
|---|---|---|
| 4.13.0부터 | `dev.thsvkd.jari` | Google Play에서 `com.jari.app`을 쓸 수 없어 바꿨습니다. 코드의 `namespace`·Java 패키지·액티비티는 `com.jari.app` 그대로입니다. |
| 4.12.x까지 | `com.jari.app` | 앱 이름을 바꾸면서 쓰던 이름입니다. |

패키지 이름이 바뀌면 기존 앱 위에 업데이트되지 않고 **별도 앱으로 설치**됩니다. 앱에 다시 로그인해야 하지만 즐겨찾기와 코레일 계정 연결은 서버에 있으므로 그대로 남습니다. Firebase에도 새 패키지 이름으로 Android 앱을 다시 등록해야 합니다.

## Windows에서 빌드하기

예전 Windows 빌드 경로인 `scripts/build-android.ps1`도 남아 있습니다(Windows PowerShell 5.1 또는 PowerShell 7).

```powershell
.\scripts\build-android.ps1            # 디버그 APK
.\scripts\build-android.ps1 -Release   # 릴리스 APK(서명 설정 필요)
.\scripts\build-android.ps1 -SkipWebBuild  # 기존 웹 번들을 그대로 사용
```

- `android\app\google-services.json`과 API 주소(`VITE_API_BASE_URL` 환경 변수 또는 `.env.production.local`)가 없으면 시작하지 않습니다.
- JDK: `JAVA_HOME`의 실제 `java -version`을 확인합니다. 비어 있으면 `%ProgramFiles%\Eclipse Adoptium`에서 찾습니다. SDK 기본 위치는 `%LOCALAPPDATA%\Android\Sdk`입니다.
- `-Release`는 `android\keystore.properties`(`storeFile`·`storePassword`·`keyAlias`·`keyPassword`) 또는 `JARI_RELEASE_STORE_FILE`·`JARI_RELEASE_STORE_PASSWORD`·`JARI_RELEASE_KEY_ALIAS`·`JARI_RELEASE_KEY_PASSWORD` 네 개가 모두 있어야 합니다. 상대 경로의 `storeFile`은 저장소 루트가 아니라 `android/` 기준입니다.
- Java 확인·웹 빌드·동기화·Gradle 중 하나라도 실패하면 멈추고 `APK:` 성공 줄을 출력하지 않습니다.

스크립트 회귀 검사(실제 APK는 만들지 않음, .NET Framework 4.x C# 컴파일러 필요):

```powershell
.\scripts\test-build-android.ps1
.\scripts\test-build-android.ps1 -RealJavaHome $env:JAVA_HOME   # 설치된 실제 JDK로 확인
```
