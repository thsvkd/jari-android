#!/usr/bin/env bash
# 릴리스 APK 를 이 PC(macOS·Linux) 하나에서 만들어요. Windows 를 거치지 않아요.
#
#   scripts/release-android.sh                       # debug 키로 서명(기본)
#   JARI_SIGNING_KEY=release scripts/release-android.sh
#
# 웹 번들(운영 API 주소) → Capacitor 동기화 → Gradle assembleDebug → 서명키로 재서명 → 지문 확인.
# 릴리스는 지금까지 디버그 빌드를 디버그 키로 서명해 내왔어요. 인증서가 바뀌면 기존 앱 위에 업데이트되지 않고
# 지우고 다시 깔아야 하므로, 서명 뒤에 지문을 확인하고 다르면 APK 를 남기지 않아요.
#
# 서명키는 Doppler 에 두고 빌드 때만 꺼내요. 저장소에 없고, 디스크에는 서명하는 동안의 임시 파일뿐이에요.
#   JARI_SIGNING_KEY         debug(기본: 지금까지 낸 릴리스와 같은 키) | release
#   Doppler 시크릿           JARI_ANDROID_<DEBUG|RELEASE>_ 뒤에 KEYSTORE_B64 / STORE_PASSWORD / KEY_ALIAS / KEY_PASSWORD
#   JARI_DOPPLER_PROJECT / JARI_DOPPLER_CONFIG   Doppler 위치 (기본 dev / dev)
#   JARI_KEYSTORE_FILE       Doppler 를 쓰지 못할 때만: 로컬 키스토어 파일(비밀번호 JARI_KEYSTORE_PASSWORD, 별칭 JARI_KEY_ALIAS)
#   JARI_API_BASE_URL        운영 API 주소 (기본 https://jari.thsvkd.dev, 앱에 들어가는 공개 값이에요)
#   JAVA_HOME                JDK 21 (기본 brew 의 openjdk@21)
#   ANDROID_HOME             Android SDK (기본 brew 의 android-commandlinetools)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 지금까지 낸 릴리스(디버그 키)의 서명 인증서 SHA-256(공개 값). debug 로 서명할 때 이 값이 아니면 기존 앱 위에 업데이트되지 않아요.
EXPECTED_DEBUG_CERT_SHA256="cc949e18b5a541a7f533ef954d1b2f675aa0847d413e784234c016e233e34f5e"

fail() { echo "release-android: $*" >&2; exit 1; }

KIND="${JARI_SIGNING_KEY:-debug}"
[[ "$KIND" == "debug" || "$KIND" == "release" ]] || fail "JARI_SIGNING_KEY 는 debug 또는 release 예요."
DOPPLER_PROJECT="${JARI_DOPPLER_PROJECT:-dev}"
DOPPLER_CONFIG="${JARI_DOPPLER_CONFIG:-dev}"
API_BASE_URL="${JARI_API_BASE_URL:-https://jari.thsvkd.dev}"

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
APKSIGNER="$(ls -d "$ANDROID_HOME"/build-tools/*/apksigner 2>/dev/null | sort -V | tail -1)"
[[ -x "$APKSIGNER" ]] || fail "apksigner 를 찾지 못했어요($ANDROID_HOME/build-tools)."

# --- 서명키: Doppler 에서 꺼내 임시 파일로 복원해요. 끝나면(실패해도) 지워요.
KEYSTORE=""
TEMP_KEYSTORE=""
cleanup() { if [[ -n "$TEMP_KEYSTORE" ]]; then rm -f "$TEMP_KEYSTORE"; fi; }
trap cleanup EXIT
secret() {
  doppler secrets get "JARI_ANDROID_$(echo "$KIND" | tr '[:lower:]' '[:upper:]')_$1" --plain \
    --project "$DOPPLER_PROJECT" --config "$DOPPLER_CONFIG" 2>/dev/null
}
if [[ -n "${JARI_KEYSTORE_FILE:-}" ]]; then
  KEYSTORE="$JARI_KEYSTORE_FILE"
  JARI_STORE_PASSWORD="${JARI_KEYSTORE_PASSWORD:?JARI_KEYSTORE_PASSWORD 가 필요해요}"
  KEY_ALIAS="${JARI_KEY_ALIAS:?JARI_KEY_ALIAS 가 필요해요}"
  JARI_KEY_PASSWORD="${JARI_KEY_PASSWORD:-$JARI_STORE_PASSWORD}"
else
  command -v doppler >/dev/null || fail "doppler CLI 가 없어요."
  TEMP_KEYSTORE="$(mktemp "${TMPDIR:-/tmp}/jari-$KIND-XXXXXX")"
  chmod 600 "$TEMP_KEYSTORE"
  KEYSTORE="$TEMP_KEYSTORE"
  secret KEYSTORE_B64 | base64 --decode > "$KEYSTORE" \
    || fail "Doppler($DOPPLER_PROJECT/$DOPPLER_CONFIG)에서 $KIND 키스토어를 꺼내지 못했어요. doppler login 과 시크릿 이름을 확인해 주세요."
  [[ -s "$KEYSTORE" ]] || fail "$KIND 키스토어가 비어 있어요."
  JARI_STORE_PASSWORD="$(secret STORE_PASSWORD)"
  KEY_ALIAS="$(secret KEY_ALIAS)"
  JARI_KEY_PASSWORD="$(secret KEY_PASSWORD)"
  [[ -n "$JARI_STORE_PASSWORD" && -n "$KEY_ALIAS" && -n "$JARI_KEY_PASSWORD" ]] \
    || fail "$KIND 키의 비밀번호·별칭 시크릿이 비어 있어요."
fi
# apksigner 에 비밀번호를 명령줄이 아니라 환경변수로 넘겨요(ps 에 보이지 않게).
export JARI_STORE_PASSWORD JARI_KEY_PASSWORD

# 키스토어의 인증서가 기대한 것인지 먼저 봐요. 틀린 키로 몇 분을 빌드하지 않도록.
KEYSTORE_SHA="$(keytool -list -v -keystore "$KEYSTORE" -storepass:env JARI_STORE_PASSWORD -alias "$KEY_ALIAS" 2>/dev/null \
  | awk -F'SHA256: ' '/SHA256:/ {gsub(":", "", $2); print tolower($2); exit}')"
[[ -n "$KEYSTORE_SHA" ]] || fail "키스토어를 열지 못했어요(비밀번호·별칭 확인)."
if [[ "$KIND" == "debug" && "$KEYSTORE_SHA" != "$EXPECTED_DEBUG_CERT_SHA256" ]]; then
  fail "debug 키의 인증서 지문이 지금까지의 릴리스 서명과 달라요. 이 키로 내면 기존 앱 위에 업데이트되지 않아요."
fi

cd "$ROOT"
VERSION="$(node -p "require('./package.json').version")"
echo "== 앱 $VERSION 웹 번들 ($API_BASE_URL)"
VITE_API_BASE_URL="$API_BASE_URL" npm run build
npx cap sync android
echo "== Gradle assembleDebug"
(cd android && ./gradlew assembleDebug)

BUILT="$ROOT/android/app/build/outputs/apk/debug/app-debug.apk"
OUT_DIR="$ROOT/dist-release"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/jari-$VERSION-$KIND.apk"
cp "$BUILT" "$OUT"

echo "== $KIND 키로 서명"
"$APKSIGNER" sign --ks "$KEYSTORE" --ks-pass env:JARI_STORE_PASSWORD --ks-key-alias "$KEY_ALIAS" --key-pass env:JARI_KEY_PASSWORD "$OUT"
CERTS="$("$APKSIGNER" verify --print-certs "$OUT")"
ACTUAL="$(awk -F': ' '/certificate SHA-256 digest/ {print $2; exit}' <<<"$CERTS")"
if [[ "$ACTUAL" != "$KEYSTORE_SHA" ]]; then
  rm -f "$OUT"
  fail "서명 인증서 지문이 키스토어와 달라 APK 를 지웠어요: $ACTUAL"
fi

echo "APK: $OUT"
echo "서명 SHA-256: $ACTUAL"
if command -v shasum >/dev/null; then
  echo "APK SHA-256: $(shasum -a 256 "$OUT" | awk '{print $1}')"
else
  echo "APK SHA-256: $(sha256sum "$OUT" | awk '{print $1}')"
fi
