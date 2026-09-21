#!/usr/bin/env bash
# Deploy the 자리났다 mobile API on pi5-s2. Does not start or restart korail-bot.
set -euo pipefail

ROOT="${JARI_ROOT:-/home/pi/Workspace/jari-android}"
GATEWAY="/home/pi/Workspace/maintain/gateway"

if [[ ! -d "$ROOT/backend" ]]; then
  echo "missing $ROOT/backend" >&2
  exit 1
fi

mkdir -p "$ROOT/backend/.secrets"
if [[ ! -f "$ROOT/backend/.secrets/mobile.key" ]]; then
  openssl rand -base64 48 > "$ROOT/backend/.secrets/mobile.key"
  chmod 600 "$ROOT/backend/.secrets/mobile.key"
  echo "Created mobile encryption key (not printed)."
else
  echo "Existing mobile encryption key preserved."
fi
chmod 700 "$ROOT/backend/.secrets"

cd "$ROOT"
docker compose -f compose.yaml -f compose.pi.yaml up --build -d

python3 - "$GATEWAY" <<'PY'
from pathlib import Path
import sys

gateway = Path(sys.argv[1])
caddy = gateway / "Caddyfile"
index = gateway / "site" / "index.html"

caddy_block = """
	# 자리났다 Android 전용 모바일 API. 텔레그램 봇(korail-bot-app:8081)과 별도 프로세스.
	@jari host jari.{$GATEWAY_DOMAIN}
	handle @jari {
		reverse_proxy jari-mobile-api:8081
	}
"""

index_card = """    <li>
      <div class="top"><span class="name">jari mobile API</span><span class="badge b-tailnet">tailnet</span></div>
      <p class="desc">자리났다 Android 앱 전용 예약 API. 텔레그램 봇과 Redis·계정이 분리되어 있다.</p>
      <a class="url" href="https://jari.thsvkd.dev">https://jari.thsvkd.dev</a>
      <p class="note">앱은 표준 HTTPS 주소로 붙는다. 이 주소는 봇 Mini App(<code>korail.thsvkd.dev</code>)이 아니다.</p>
    </li>
"""

text = caddy.read_text(encoding="utf-8")
if "@jari host" not in text:
    marker = "\t@korail-test host korail-test.{$GATEWAY_DOMAIN}\n\thandle @korail-test {\n\t\treverse_proxy korail-bot-test-app:8091\n\t}\n"
    if marker not in text:
        raise SystemExit("Caddyfile korail-test block not found")
    backup = caddy.with_name("Caddyfile.bak-jari")
    if not backup.exists():
        backup.write_text(text, encoding="utf-8")
    text = text.replace(marker, marker + caddy_block, 1)
    caddy.write_text(text, encoding="utf-8")
    print("Caddyfile: added jari route")
else:
    print("Caddyfile: jari route already present")

html = index.read_text(encoding="utf-8")
if "jari.thsvkd.dev" not in html:
    marker = """    <li>
      <div class="top"><span class="name">korail-bot-test</span><span class="badge b-tailnet">tailnet</span></div>
      <p class="desc">위 봇의 테스트 인스턴스. 같은 코드, 격리된 테스트 봇.</p>
      <a class="url" href="https://korail-test.thsvkd.dev">https://korail-test.thsvkd.dev</a>
    </li>
"""
    if marker not in html:
        raise SystemExit("index.html insert point not found")
    backup = index.with_name("index.html.bak-jari")
    if not backup.exists():
        backup.write_text(html, encoding="utf-8")
    html = html.replace(marker, marker + index_card, 1)
    index.write_text(html, encoding="utf-8")
    print("index.html: added jari card")
else:
    print("index.html: jari card already present")
PY

"$GATEWAY/scripts/gateway.sh" validate
"$GATEWAY/scripts/gateway.sh" reload
echo "Deploy steps finished."
