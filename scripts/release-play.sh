#!/usr/bin/env bash
# Google Play 에 올릴 릴리스 AAB 를 이 Mac 에서 만들어요. 올리지는 않아요(Play Console 에서 사람이 올려요).
#
#   scripts/release-play.sh
#
# 웹 번들(운영 API 주소) → Capacitor 동기화 → Gradle bundleRelease(디버그 불가, WebView 디버깅 꺼짐) → 서명·확인.
# 서명은 업로드 키(Doppler 의 JARI_ANDROID_RELEASE_*)로 해요. Play 앱 서명을 쓰면 Play 가 설치 파일을 자기 키로 다시 서명하니,
# Play 에서 받은 앱과 scripts/release-android.sh 로 만든(디버그 키) 사이드로드 앱은 같은 패키지여도 서로 업데이트되지 않아요.
#
# 비밀은 Doppler 에 두고 빌드 때만 꺼내요. 디스크에는 빌드하는 동안의 임시 파일뿐이고, 끝나면(실패해도) 지워요.
#   Doppler 시크릿   JARI_ANDROID_RELEASE_KEYSTORE_B64 / _STORE_PASSWORD / _KEY_ALIAS / _KEY_PASSWORD  (업로드 키)
#                    JARI_ANDROID_GOOGLE_SERVICES_B64  (android/app/google-services.json 을 base64 로. 없으면 푸시가 꺼진 앱이 되므로 멈춰요)
#   JARI_DOPPLER_PROJECT / JARI_DOPPLER_CONFIG   Doppler 위치 (기본 dev / dev)
#   JARI_API_BASE_URL   운영 API 주소 (기본 https://jari.thsvkd.dev)
#   JARI_BUNDLETOOL     bundletool 명령 또는 .jar (있으면 AAB 의 매니페스트를 그것으로 읽어요. 없으면 매니페스트를 직접 살펴요)
#   JAVA_HOME           JDK 21 (기본 brew 의 openjdk@21)
#   ANDROID_HOME        Android SDK (기본 brew 의 android-commandlinetools)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail() { echo "release-play: $*" >&2; exit 1; }

DOPPLER_PROJECT="${JARI_DOPPLER_PROJECT:-dev}"
DOPPLER_CONFIG="${JARI_DOPPLER_CONFIG:-dev}"
API_BASE_URL="${JARI_API_BASE_URL:-https://jari.thsvkd.dev}"
[[ "$API_BASE_URL" == https://* ]] || fail "운영 API 주소는 https 여야 해요: $API_BASE_URL"

if [[ -z "${JAVA_HOME:-}" && -d /opt/homebrew/opt/openjdk@21 ]]; then
  export JAVA_HOME=/opt/homebrew/opt/openjdk@21
fi
if [[ -z "${ANDROID_HOME:-}" && -d /opt/homebrew/share/android-commandlinetools ]]; then
  export ANDROID_HOME=/opt/homebrew/share/android-commandlinetools
fi
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-}}"
export PATH="${JAVA_HOME:-/nonexistent}/bin:$PATH"

[[ -n "${ANDROID_HOME:-}" && -d "$ANDROID_HOME" ]] || fail "ANDROID_HOME 이 없어요."
java -version 2>&1 | grep -Eq 'version "(2[1-9]|[3-9][0-9])' || fail "JDK 21 이상이 필요해요(JAVA_HOME=${JAVA_HOME:-없음})."
command -v doppler >/dev/null || fail "doppler CLI 가 없어요."

cd "$ROOT"
GRADLE_FILE="$ROOT/android/app/build.gradle"
APP_ID="$(sed -n 's/^ *applicationId "\([^"]*\)".*/\1/p' "$GRADLE_FILE" | head -1)"
VERSION_CODE="$(sed -n 's/^ *versionCode \([0-9]*\).*/\1/p' "$GRADLE_FILE" | head -1)"
VERSION_NAME="$(sed -n 's/^ *versionName "\([^"]*\)".*/\1/p' "$GRADLE_FILE" | head -1)"
TARGET_SDK="$(sed -n 's/^ *targetSdkVersion = \([0-9]*\).*/\1/p' "$ROOT/android/variables.gradle" | head -1)"
VERSION="$(node -p "require('./package.json').version")"
[[ -n "$TARGET_SDK" ]] || fail "android/variables.gradle 에서 targetSdkVersion 을 읽지 못했어요."
[[ -n "$APP_ID" && -n "$VERSION_CODE" && -n "$VERSION_NAME" ]] || fail "android/app/build.gradle 에서 applicationId·versionCode·versionName 을 읽지 못했어요."
[[ "$VERSION_NAME" == "$VERSION" ]] || fail "versionName($VERSION_NAME)이 package.json($VERSION)과 달라요."

SERVICES="$ROOT/android/app/google-services.json"
# 이미 있는 파일은 누가 둔 것인지 모르니 덮어쓰거나 지우지 않아요.
[[ ! -e "$SERVICES" ]] || fail "android/app/google-services.json 이 이미 있어요. 이 스크립트는 Doppler 의 것만 써요. 옮겨 두고 다시 실행해 주세요."

TEMP_KEYSTORE=""
OUT=""
cleanup() {
  rm -f "$SERVICES"
  # 확인을 다 거치지 않은 AAB 는 남기지 않아요(중간에 끊겨도).
  if [[ -n "$OUT" ]]; then rm -f "$OUT"; fi
  if [[ -n "$TEMP_KEYSTORE" ]]; then rm -f "$TEMP_KEYSTORE"; fi
}
trap cleanup EXIT
secret() {
  doppler secrets get "$1" --plain --project "$DOPPLER_PROJECT" --config "$DOPPLER_CONFIG" 2>/dev/null
}

# --- Firebase 설정: 없으면 푸시가 꺼진 앱이 나가요. Play 로 내는 앱에서는 멈춰요.
SERVICES_B64="$(secret JARI_ANDROID_GOOGLE_SERVICES_B64 || true)"
[[ -n "$SERVICES_B64" ]] || fail "Doppler($DOPPLER_PROJECT/$DOPPLER_CONFIG)에 JARI_ANDROID_GOOGLE_SERVICES_B64 가 없어요. Firebase 콘솔에서 $APP_ID 용 google-services.json 을 받아
  doppler secrets set JARI_ANDROID_GOOGLE_SERVICES_B64=\"\$(base64 < google-services.json)\" --project $DOPPLER_PROJECT --config $DOPPLER_CONFIG
로 넣은 뒤 다시 실행해 주세요."
( umask 077; printf '%s' "$SERVICES_B64" | base64 --decode > "$SERVICES" ) \
  || fail "JARI_ANDROID_GOOGLE_SERVICES_B64 를 base64 로 풀지 못했어요."
unset SERVICES_B64
node -e '
  const [file, appId] = process.argv.slice(1);
  let config;
  try { config = JSON.parse(require("fs").readFileSync(file, "utf8")); } catch { console.error("google-services.json 이 JSON 이 아니에요."); process.exit(1); }
  const packages = (config.client ?? []).map((client) => client?.client_info?.android_client_info?.package_name).filter(Boolean);
  if (!packages.includes(appId)) {
    console.error(`google-services.json 에 ${appId} 클라이언트가 없어요(있는 것: ${packages.join(", ") || "없음"}). Firebase 프로젝트에 ${appId} 앱을 추가하고 새 파일을 Doppler 에 넣어 주세요.`);
    process.exit(1);
  }
  console.log(`Firebase: ${config.project_info?.project_id ?? "?"} 프로젝트의 ${appId} 클라이언트`);
' "$SERVICES" "$APP_ID" || fail "Firebase 설정을 확인해 주세요."

# --- 업로드 키: 임시 파일로 복원해요. 비밀번호는 명령줄이 아니라 환경변수로 넘겨요(ps 에 보이지 않게).
TEMP_KEYSTORE="$(mktemp "${TMPDIR:-/tmp}/jari-upload-XXXXXX")"
chmod 600 "$TEMP_KEYSTORE"
secret JARI_ANDROID_RELEASE_KEYSTORE_B64 | base64 --decode > "$TEMP_KEYSTORE" \
  || fail "Doppler($DOPPLER_PROJECT/$DOPPLER_CONFIG)에서 업로드 키스토어(JARI_ANDROID_RELEASE_KEYSTORE_B64)를 꺼내지 못했어요. doppler login 과 시크릿 이름을 확인해 주세요."
[[ -s "$TEMP_KEYSTORE" ]] || fail "업로드 키스토어가 비어 있어요."
JARI_RELEASE_STORE_PASSWORD="$(secret JARI_ANDROID_RELEASE_STORE_PASSWORD || true)"
JARI_RELEASE_KEY_ALIAS="$(secret JARI_ANDROID_RELEASE_KEY_ALIAS || true)"
JARI_RELEASE_KEY_PASSWORD="$(secret JARI_ANDROID_RELEASE_KEY_PASSWORD || true)"
[[ -n "$JARI_RELEASE_STORE_PASSWORD" && -n "$JARI_RELEASE_KEY_ALIAS" && -n "$JARI_RELEASE_KEY_PASSWORD" ]] \
  || fail "업로드 키의 비밀번호·별칭 시크릿이 비어 있어요."
JARI_RELEASE_STORE_FILE="$TEMP_KEYSTORE"
# android/app/build.gradle 이 이 네 값으로 release 서명 설정을 만들어요.
export JARI_RELEASE_STORE_FILE JARI_RELEASE_STORE_PASSWORD JARI_RELEASE_KEY_ALIAS JARI_RELEASE_KEY_PASSWORD

KEYSTORE_LIST="$(keytool -list -v -keystore "$TEMP_KEYSTORE" -storepass:env JARI_RELEASE_STORE_PASSWORD -alias "$JARI_RELEASE_KEY_ALIAS" 2>/dev/null)" \
  || fail "업로드 키스토어를 열지 못했어요(비밀번호·별칭 확인)."
KEYSTORE_SHA256="$(awk -F'SHA256: ' '/SHA256:/ {print $2; exit}' <<<"$KEYSTORE_LIST")"
[[ -n "$KEYSTORE_SHA256" ]] || fail "업로드 키의 인증서 지문을 읽지 못했어요."

echo "== 앱 $VERSION ($APP_ID, versionCode $VERSION_CODE) 웹 번들 ($API_BASE_URL)"
VITE_API_BASE_URL="$API_BASE_URL" npm run build
npx cap sync android
echo "== Gradle bundleRelease"
# 데몬 없이: 업로드 키 비밀번호가 든 환경을 빌드가 끝난 뒤까지 들고 있는 프로세스를 남기지 않아요.
(cd android && ./gradlew --no-daemon clean bundleRelease)
# 광고·분석 없음(Play 에 그렇게 신고했어요): 분석·광고 SDK 가 의존성으로 끌려 들어오면 멈춰요.
ANALYTICS="$(cd android && ./gradlew --no-daemon -q :app:dependencies --configuration releaseRuntimeClasspath \
  | grep -oE 'com\.google\.(firebase:firebase-analytics|android\.gms:play-services-(measurement|ads))[a-z-]*' | sort -u || true)"
[[ -z "$ANALYTICS" ]] || fail "광고·분석 라이브러리가 들어왔어요: $ANALYTICS"

BUILT="$ROOT/android/app/build/outputs/bundle/release/app-release.aab"
[[ -s "$BUILT" ]] || fail "AAB 가 만들어지지 않았어요: $BUILT"
OUT_DIR="$ROOT/dist-release"
mkdir -p "$OUT_DIR"
FINAL="$OUT_DIR/jari-$VERSION-play.aab"
rm -f "$FINAL"
# 확인하는 동안은 다른 이름으로 두고, 모두 통과해야 최종 이름으로 옮겨요.
OUT="$FINAL.unchecked"
cp "$BUILT" "$OUT"

# --- 확인: 하나라도 어긋나면 AAB 를 남기지 않아요.
reject() { rm -f "$OUT"; fail "$* (AAB 를 지웠어요)"; }

# -strict 는 쓰지 않아요: AAB 는 META-INF 가 앞에 오지 않는 구조라 서명이 맞아도 경고로 실패해요.
# 대신 서명되지 않은 파일(jarsigner 가 0 으로 끝내요)을 "jar verified." 로 가려요.
SIGNATURE="$(jarsigner -verify "$OUT" 2>&1)" || reject "AAB 서명이 올바르지 않아요."
grep -q '^jar verified\.' <<<"$SIGNATURE" || reject "AAB 가 서명되지 않았어요."
CERT="$(keytool -printcert -jarfile "$OUT")" || reject "AAB 서명 인증서를 읽지 못했어요."
SIGNED_SHA256="$(awk -F'SHA256: ' '/SHA256:/ {print $2; exit}' <<<"$CERT")"
SIGNED_SHA1="$(awk -F'SHA1: ' '/SHA1:/ {print $2; exit}' <<<"$CERT")"
[[ "$SIGNED_SHA256" == "$KEYSTORE_SHA256" ]] || reject "AAB 의 서명 인증서가 업로드 키와 달라요: $SIGNED_SHA256"

MANIFEST_PROTO="$(unzip -p "$OUT" base/manifest/AndroidManifest.xml | LC_ALL=C tr -c '[:print:]' '\n')" \
  || reject "AAB 매니페스트를 읽지 못했어요."
# 광고 ID 권한(com.google.android.gms.permission.AD_ID)은 매니페스트에서 tools:node="remove" 로 빼 두었어요. 들어오면 멈춰요.
if grep -q 'permission.AD_ID' <<<"$MANIFEST_PROTO"; then reject "AAB 매니페스트에 광고 ID 권한(AD_ID)이 있어요."; fi
if [[ -n "${JARI_BUNDLETOOL:-}" ]] || command -v bundletool >/dev/null; then
  if [[ "${JARI_BUNDLETOOL:-}" == *.jar ]]; then BUNDLETOOL=(java -jar "$JARI_BUNDLETOOL"); else BUNDLETOOL=("${JARI_BUNDLETOOL:-bundletool}"); fi
  MANIFEST="$("${BUNDLETOOL[@]}" dump manifest --bundle "$OUT")" || reject "bundletool 이 AAB 매니페스트를 읽지 못했어요."
  grep -q "package=\"$APP_ID\"" <<<"$MANIFEST" || reject "AAB 의 패키지가 $APP_ID 가 아니에요."
  grep -q "android:versionCode=\"$VERSION_CODE\"" <<<"$MANIFEST" || reject "AAB 의 versionCode 가 $VERSION_CODE 가 아니에요."
  grep -q "android:targetSdkVersion=\"$TARGET_SDK\"" <<<"$MANIFEST" || reject "AAB 의 targetSdkVersion 이 $TARGET_SDK 가 아니에요."
  if grep -q 'android:debuggable="true"' <<<"$MANIFEST"; then reject "디버그 가능한 AAB 예요."; fi
  if grep -q 'AD_ID' <<<"$MANIFEST"; then reject "AAB 에 광고 ID 권한(AD_ID)이 있어요."; fi
  echo "매니페스트(bundletool): package=$APP_ID versionCode=$VERSION_CODE targetSdk=$TARGET_SDK, debuggable 아님, AD_ID 없음"
else
  # AAB 의 매니페스트는 protobuf 예요. 디버그 빌드는 debuggable 속성을 넣고, 릴리스 빌드는 그 속성이 아예 없어요.
  grep -qF "$APP_ID" <<<"$MANIFEST_PROTO" || reject "AAB 매니페스트에 패키지 $APP_ID 가 없어요."
  if grep -q 'debuggable' <<<"$MANIFEST_PROTO"; then reject "AAB 매니페스트에 debuggable 속성이 있어요."; fi
  echo "매니페스트: package=$APP_ID, debuggable 속성 없음, AD_ID 없음 (bundletool 이 있으면 JARI_BUNDLETOOL 로 versionCode·targetSdk 까지 봐요)"
fi
# unzip 뒤의 grep 은 -q 대신 -c 로 끝까지 읽어요. -q 가 먼저 끝나면 unzip 이 SIGPIPE 로 실패해 pipefail 이 결과를 뒤집어요.
# WebView 디버깅은 Capacitor 가 앱이 debuggable 일 때만 켜요. 설정으로 켠 것도 없어야 해요.
if unzip -p "$OUT" base/assets/capacitor.config.json | grep -c '"webContentsDebuggingEnabled": *true' >/dev/null; then
  reject "capacitor.config.json 이 WebView 디버깅을 켜요."
fi
unzip -p "$OUT" base/assets/public/index.html >/dev/null 2>&1 || reject "AAB 에 웹 번들이 없어요."
if ! unzip -p "$OUT" 'base/assets/public/assets/*.js' | grep -cF "$API_BASE_URL" >/dev/null; then
  reject "웹 번들에 운영 API 주소($API_BASE_URL)가 없어요."
fi
# google-services 플러그인이 넣은 google_app_id 가 있어야 앱이 푸시를 켤 수 있어요(SecureSessionPlugin.pushConfigured).
unzip -p "$OUT" base/resources.pb | LC_ALL=C grep -ac google_app_id >/dev/null || reject "AAB 에 Firebase 설정(google_app_id)이 없어요."

mv "$OUT" "$FINAL"
OUT=""
echo
echo "AAB: $FINAL"
echo "패키지: $APP_ID  versionCode: $VERSION_CODE  versionName: $VERSION_NAME"
echo "업로드 인증서 SHA-256: $SIGNED_SHA256"
echo "업로드 인증서 SHA-1:   $SIGNED_SHA1"
echo "AAB SHA-256: $(shasum -a 256 "$FINAL" | awk '{print $1}')"
echo "Play Console → 테스트 → 내부 테스트 에서 이 AAB 를 올려 주세요. 이 스크립트는 어디에도 올리지 않아요."
