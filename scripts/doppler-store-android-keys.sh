#!/usr/bin/env bash
# Android 서명키를 Doppler 에 저장해요. scripts/release-android.sh 가 빌드 때 꺼내 써요.
# 비밀번호·키스토어 값은 화면에 출력하지 않고, 인증서 지문만 보여 줘요.
#
#   scripts/doppler-store-android-keys.sh debug <키스토어 파일> <저장소 비밀번호> <별칭> [키 비밀번호]
#       지금 쓰는 키(지금까지의 릴리스를 서명한 디버그 키)를 JARI_ANDROID_DEBUG_* 로 저장해요.
#   scripts/doppler-store-android-keys.sh release-new
#       릴리스(업로드)용 새 키를 만들어 JARI_ANDROID_RELEASE_* 로 저장해요. 키스토어는 디스크에 남기지 않아요.
#
# 이미 저장된 키는 덮어쓰지 않아요(FORCE=1 이면 덮어써요). 덮어쓰면 이전 키는 되찾을 수 없어요.
# 위치는 JARI_DOPPLER_PROJECT / JARI_DOPPLER_CONFIG (기본 dev / dev).
set -euo pipefail

PROJECT="${JARI_DOPPLER_PROJECT:-dev}"
CONFIG="${JARI_DOPPLER_CONFIG:-dev}"
fail() { echo "doppler-store-android-keys: $*" >&2; exit 1; }
command -v doppler >/dev/null || fail "doppler CLI 가 없어요."
command -v keytool >/dev/null || fail "keytool(JDK)이 없어요. JAVA_HOME 을 JDK 21 로 잡아 주세요."

put() { # 이름 — 값은 표준입력으로 받아요(명령줄·화면에 남지 않아요).
  doppler secrets set "$1" --project "$PROJECT" --config "$CONFIG" --silent >/dev/null
}
exists() {
  doppler secrets get "$1" --plain --project "$PROJECT" --config "$CONFIG" >/dev/null 2>&1
}
guard() { # 접두사
  if [[ "${FORCE:-0}" != "1" ]] && exists "${1}_KEYSTORE_B64"; then
    fail "${1}_* 가 이미 $PROJECT/$CONFIG 에 있어요. 덮어쓰려면 FORCE=1 (이전 키는 되찾을 수 없어요)."
  fi
}
store() { # 접두사 키스토어 저장소비번 별칭 키비번
  local prefix="$1" file="$2" storepass="$3" alias_name="$4" keypass="$5"
  export JARI_TMP_STOREPASS="$storepass"
  local sha
  sha="$(keytool -list -v -keystore "$file" -storepass:env JARI_TMP_STOREPASS -alias "$alias_name" 2>/dev/null \
    | awk -F'SHA256: ' '/SHA256:/ {print $2; exit}')"
  unset JARI_TMP_STOREPASS
  [[ -n "$sha" ]] || fail "키스토어를 열지 못했어요(비밀번호·별칭 확인)."
  base64 -i "$file" | tr -d '\n' | put "${prefix}_KEYSTORE_B64"
  printf '%s' "$storepass" | put "${prefix}_STORE_PASSWORD"
  printf '%s' "$alias_name" | put "${prefix}_KEY_ALIAS"
  printf '%s' "$keypass" | put "${prefix}_KEY_PASSWORD"
  echo "저장했어요: ${prefix}_{KEYSTORE_B64,STORE_PASSWORD,KEY_ALIAS,KEY_PASSWORD} → $PROJECT/$CONFIG"
  echo "인증서 SHA-256: $sha"
}

case "${1:-}" in
  debug)
    [[ $# -ge 4 ]] || fail "사용법: debug <키스토어 파일> <저장소 비밀번호> <별칭> [키 비밀번호]"
    guard JARI_ANDROID_DEBUG
    [[ -f "$2" ]] || fail "키스토어 파일이 없어요: $2"
    store JARI_ANDROID_DEBUG "$2" "$3" "$4" "${5:-$3}"
    ;;
  release-new)
    guard JARI_ANDROID_RELEASE
    tmp="$(mktemp "${TMPDIR:-/tmp}/jari-release-XXXXXX")"
    trap 'rm -f "$tmp"' EXIT
    rm -f "$tmp" # keytool 은 없는 파일에 새로 만들어요.
    storepass="$(openssl rand -base64 24)"
    alias_name="jari-release"
    JARI_TMP_STOREPASS="$storepass" keytool -genkeypair -keystore "$tmp" -storetype PKCS12 \
      -storepass:env JARI_TMP_STOREPASS -keypass:env JARI_TMP_STOREPASS \
      -alias "$alias_name" -keyalg RSA -keysize 4096 -validity 10000 \
      -dname "CN=jari, O=thsvkd, C=KR" >/dev/null 2>&1 || fail "새 키를 만들지 못했어요."
    # PKCS12 는 키 비밀번호가 저장소 비밀번호와 같아요.
    store JARI_ANDROID_RELEASE "$tmp" "$storepass" "$alias_name" "$storepass"
    ;;
  *)
    fail "사용법: debug <키스토어 파일> <저장소 비밀번호> <별칭> [키 비밀번호] | release-new"
    ;;
esac
