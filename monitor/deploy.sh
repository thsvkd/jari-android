#!/usr/bin/env bash
# 외부 감시 Worker(jari-monitor)를 배포해요. 비밀값은 Doppler 에서 꺼내 쓰고, 저장소·디스크에 남기지 않아요.
#
#   monitor/deploy.sh                 # 배포
#   monitor/deploy.sh --test-message  # 배포 없이, 같은 봇·채팅으로 시험 메시지만 보내요
#
#   Doppler(dev/dev)
#     CLOUDFLARE_API_THSVKD             Workers·KV 쓰기 권한이 있는 API 토큰
#     JARI_MONITOR_TELEGRAM_BOT_TOKEN   알림을 보낼 봇(유지보수봇) 토큰
#     JARI_MONITOR_TELEGRAM_CHAT_ID     알림을 받을 채팅 id
#   JARI_DOPPLER_PROJECT / JARI_DOPPLER_CONFIG   Doppler 위치 (기본 dev / dev)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRANGLER_VERSION="4.144.0"
NAMESPACE_TITLE="jari-monitor-state"
PROJECT="${JARI_DOPPLER_PROJECT:-dev}"
CONFIG="${JARI_DOPPLER_CONFIG:-dev}"

fail() { echo "monitor/deploy: $*" >&2; exit 1; }
secret() {
  doppler secrets get "$1" --project "$PROJECT" --config "$CONFIG" --plain 2>/dev/null || fail "Doppler $PROJECT/$CONFIG 에 $1 이 없어요."
}

BOT_TOKEN="$(secret JARI_MONITOR_TELEGRAM_BOT_TOKEN)"
CHAT_ID="$(secret JARI_MONITOR_TELEGRAM_CHAT_ID)"

if [[ "${1:-}" == "--test-message" ]]; then
  curl -fsS -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\": \"${CHAT_ID}\", \"text\": \"🔔 자리났다 서버 감시 시험 메시지예요. 장애와 복구를 이 채팅으로 알려요.\"}" >/dev/null \
    || fail "텔레그램이 시험 메시지를 받지 않았어요(봇 토큰·채팅 id, 봇과 대화를 시작했는지 확인)."
  echo "monitor/deploy: 시험 메시지를 보냈어요."
  exit 0
fi

export CLOUDFLARE_API_TOKEN
CLOUDFLARE_API_TOKEN="$(secret CLOUDFLARE_API_THSVKD)"
api() { curl -fsS -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" -H "Content-Type: application/json" "$@"; }

ACCOUNT_ID="$(api https://api.cloudflare.com/client/v4/accounts | python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]; assert len(r)==1, "계정이 하나가 아니에요"; print(r[0]["id"])')"
export CLOUDFLARE_ACCOUNT_ID="$ACCOUNT_ID"

# 감시 상태를 두는 KV. 있으면 그대로, 없으면 만들어요.
NAMESPACE_ID="$(api "https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/storage/kv/namespaces?per_page=100" \
  | python3 -c "import json,sys; print(next((n['id'] for n in json.load(sys.stdin)['result'] if n['title']=='${NAMESPACE_TITLE}'), ''))")"
if [[ -z "$NAMESPACE_ID" ]]; then
  NAMESPACE_ID="$(api -X POST "https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/storage/kv/namespaces" \
    -d "{\"title\": \"${NAMESPACE_TITLE}\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["id"])')"
  echo "monitor/deploy: KV ${NAMESPACE_TITLE} 를 만들었어요."
fi

# wrangler.toml 은 id 자리표시자를 담고 있어요. 같은 폴더에 임시 설정을 만들어 쓰고 지워요(main 경로가 설정 위치 기준이에요).
GENERATED="$HERE/.wrangler.deploy.toml"
trap 'rm -f "$GENERATED"' EXIT
sed "s/__STATE_NAMESPACE_ID__/${NAMESPACE_ID}/" "$HERE/wrangler.toml" > "$GENERATED"

wrangler() { (cd "$HERE" && npx --yes "wrangler@${WRANGLER_VERSION}" "$@" --config "$GENERATED"); }
wrangler deploy
printf '%s' "$BOT_TOKEN" | wrangler secret put TELEGRAM_BOT_TOKEN
printf '%s' "$CHAT_ID" | wrangler secret put TELEGRAM_CHAT_ID
SUBDOMAIN="$(api "https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/workers/subdomain" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["subdomain"])')"
echo "monitor/deploy: 배포했어요. 지금 상태: https://jari-monitor.${SUBDOMAIN}.workers.dev/"
