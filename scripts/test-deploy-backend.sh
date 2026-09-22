#!/usr/bin/env bash
# Regression check for scripts/deploy-backend.sh: syntax, and that --dry-run
# prints the expected plan without ever calling ssh. Runs on Git Bash/Windows
# and Linux; no real host is touched.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPLOY_SCRIPT="$SCRIPT_DIR/deploy-backend.sh"
FAIL=0

check() {
  if [[ "$2" -eq 0 ]]; then
    echo "ok: $1"
  else
    echo "FAIL: $1"
    FAIL=1
  fi
}

echo "== bash -n =="
set +e
bash -n "$DEPLOY_SCRIPT"
check "syntax" $?
set -e

echo "== --dry-run (multi-overlay invocation) =="
FAKE_BIN="$(mktemp -d)"
trap 'rm -rf "$FAKE_BIN"' EXIT
cat >"$FAKE_BIN/ssh" <<'EOF'
#!/usr/bin/env bash
echo "FAKE SSH WAS INVOKED: $*" >&2
exit 1
EOF
chmod +x "$FAKE_BIN/ssh"

cd "$REPO_ROOT"
set +e
OUTPUT="$(PATH="$FAKE_BIN:$PATH" "$DEPLOY_SCRIPT" \
  --host pi@example --root /srv/app \
  --compose-file compose.yaml --compose-file compose.overlay.example.yaml \
  --ref HEAD --dry-run 2>&1)"
STATUS=$?
set -e

check "dry-run exits 0" $STATUS
[[ "$STATUS" -ne 0 ]] && echo "$OUTPUT"

if echo "$OUTPUT" | grep -q "FAKE SSH WAS INVOKED"; then
  echo "FAIL: dry-run invoked real ssh"
  FAIL=1
else
  echo "ok: dry-run never invoked ssh"
fi

for needle in \
  "backup: cp -a backend" \
  "git archive HEAD backend/src backend/pyproject.toml backend/uv.lock backend/Dockerfile" \
  "swap backend/src" \
  "build + up -d --no-deps api" \
  "up -d --no-deps" \
  "docker top"; do
  if echo "$OUTPUT" | grep -qF "$needle"; then
    echo "ok: plan mentions '$needle'"
  else
    echo "FAIL: plan missing '$needle'"
    FAIL=1
  fi
done

if echo "$OUTPUT" | grep -qF "ps -eo"; then
  echo "FAIL: plan still uses 'docker exec ... ps -eo' (breaks on slim images)"
  FAIL=1
else
  echo "ok: plan does not depend on in-container ps"
fi

echo "== --dry-run (whitespace in --root/--after) =="
set +e
QUOTED_OUTPUT="$(PATH="$FAKE_BIN:$PATH" "$DEPLOY_SCRIPT" \
  --host pi@example --root "/srv/my app" \
  --after "echo a b" --dry-run 2>&1)"
QUOTED_STATUS=$?
set -e

check "whitespace dry-run exits 0" $QUOTED_STATUS
[[ "$QUOTED_STATUS" -ne 0 ]] && echo "$QUOTED_OUTPUT"

if echo "$QUOTED_OUTPUT" | grep -q "FAKE SSH WAS INVOKED"; then
  echo "FAIL: whitespace dry-run invoked real ssh"
  FAIL=1
else
  echo "ok: whitespace dry-run never invoked ssh"
fi

# Each arg must reach the remote command as one %q-quoted token, not split on
# its embedded spaces (that split is exactly what ssh's own arg-joining would
# otherwise do, and is what regressed before this fix).
for needle in '/srv/my\ app' 'echo\ a\ b'; do
  if echo "$QUOTED_OUTPUT" | grep -qF "$needle"; then
    echo "ok: remote command quotes '$needle'"
  else
    echo "FAIL: remote command missing quoted '$needle'"
    FAIL=1
  fi
done

echo "== image_repo() normalization =="
# Sourcing is safe: deploy-backend.sh guards parse_args/main behind a
# direct-execution check, so this only defines functions.
source "$DEPLOY_SCRIPT"
declare -A repo_cases=(
  ["jari-api"]="jari-api"
  ["jari-api:latest"]="jari-api"
  ["host:5000/repo:v1"]="host:5000/repo"
  ["host:5000/repo"]="host:5000/repo"
)
for input in "${!repo_cases[@]}"; do
  want="${repo_cases[$input]}"
  got="$(image_repo "$input")"
  if [[ "$got" == "$want" ]]; then
    echo "ok: image_repo('$input') = '$got'"
  else
    echo "FAIL: image_repo('$input') = '$got', want '$want'"
    FAIL=1
  fi
done

if [[ "$FAIL" -eq 0 ]]; then
  echo "== all checks passed =="
  exit 0
else
  echo "== FAILURES ABOVE =="
  exit 1
fi
