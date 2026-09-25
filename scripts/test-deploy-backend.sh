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

echo "== transfer()/verify(): a remote failure must propagate through 'fn || rollback' =="
# main() calls these as `transfer || rollback_and_exit` and `verify ||
# rollback_and_exit`, which turns off errexit inside the function body (bash
# quirk). Reproduce that exact call shape with a faked ssh/run_remote so a
# regression (a swallowed failure falling through to success) shows up here
# without touching a real host.

set +e
TRANSFER_OUT="$(
  ssh() { echo "FAKE SSH FAILING: $*" >&2; return 1; }
  HOST="fake@host"; ROOT="/srv/app"; REF="HEAD"; TS="20260101T000000Z"; DRY_RUN=0; SWAPPED=0
  transfer || echo "TRANSFER_RC=$?"
  echo "SWAPPED_AFTER=$SWAPPED"
)" 2>&1
set -e
if echo "$TRANSFER_OUT" | grep -q "TRANSFER_RC=1"; then
  echo "ok: transfer() returns non-zero when ssh fails"
else
  echo "FAIL: transfer() swallowed the ssh failure (silent-deploy bug)"
  FAIL=1
fi
if echo "$TRANSFER_OUT" | grep -q "SWAPPED_AFTER=0"; then
  echo "ok: SWAPPED stays 0 when the transfer step fails"
else
  echo "FAIL: SWAPPED was set to 1 despite the transfer step failing"
  FAIL=1
fi

set +e
VERIFY_NO_HEALTHURL_OUT="$(
  run_remote() { echo "FAKE run_remote FAILING: $*" >&2; return 1; }
  HOST="fake@host"; ROOT="/srv/app"; SERVICE="api"; HEALTH_URL=""; DRY_RUN=0
  verify || echo "VERIFY_RC=$?"
)" 2>&1
set -e
if echo "$VERIFY_NO_HEALTHURL_OUT" | grep -q "VERIFY_RC=1"; then
  echo "ok: verify() propagates a failed remote check when --health-url is unset"
else
  echo "FAIL: verify() swallowed a failed remote check (returned 0)"
  FAIL=1
fi

set +e
VERIFY_HTTP000_OUT="$(
  run_remote() { return 0; }
  curl() { printf '000'; }
  HOST="fake@host"; ROOT="/srv/app"; SERVICE="api"; HEALTH_URL="http://example.invalid/health"; DRY_RUN=0
  verify || echo "VERIFY_RC=$?"
)" 2>&1
set -e
if echo "$VERIFY_HTTP000_OUT" | grep -q "VERIFY_RC=1"; then
  echo "ok: verify() treats HTTP 000 (connection refused) as a failure"
else
  echo "FAIL: verify() accepted HTTP 000 as a healthy response"
  FAIL=1
fi

echo "== transfer(): the swap step alone fails (ssh itself succeeds) =="
# The two ssh calls in transfer() (mkdir, and git archive | ssh tar) are not
# the only failure point: the swap step goes through run_remote and has its
# own `|| return 1` guard. The TRANSFER_OUT case above never reaches that
# guard, since it fails ssh on the very first call. Fake ssh succeeding (and
# draining the git-archive pipe so it cannot block on a full pipe buffer) so
# run_remote failing on the swap step is the only thing under test here.
set +e
SWAP_ONLY_OUT="$(
  ssh() { case "$2" in *tar*) cat >/dev/null ;; esac; return 0; }
  run_remote() { echo "FAKE run_remote FAILING (swap step): $*" >&2; return 1; }
  HOST="fake@host"; ROOT="/srv/app"; REF="HEAD"; TS="20260101T000000Z"; DRY_RUN=0; SWAPPED=0
  transfer || echo "TRANSFER_RC=$?"
  echo "SWAPPED_AFTER=$SWAPPED"
)" 2>&1
set -e
if echo "$SWAP_ONLY_OUT" | grep -q "TRANSFER_RC=1"; then
  echo "ok: transfer() returns non-zero when only the swap step fails"
else
  echo "FAIL: transfer() swallowed the swap-step failure"
  FAIL=1
fi
if echo "$SWAP_ONLY_OUT" | grep -q "SWAPPED_AFTER=0"; then
  echo "ok: SWAPPED stays 0 when only the swap step fails"
else
  echo "FAIL: SWAPPED was set to 1 despite the swap step failing"
  FAIL=1
fi

echo "== main(): a failing transfer step ends main() non-zero and rolls back =="
set +e
MAIN_OUT="$(
  preflight() { return 0; }
  backup() { return 0; }
  deploy() { return 0; }
  verify() { return 0; }
  after_hook() { return 0; }
  ssh() { echo "FAKE SSH FAILING: $*" >&2; return 1; }
  run_remote() { echo "FAKE run_remote FAILING: $*" >&2; return 1; }
  HOST="fake@host"; ROOT="/srv/app"; REF="HEAD"; SERVICE="api"
  SHORT_REF="deadbee"; TS="20260101T000000Z"; SWAPPED=0
  main 2>&1
)"
MAIN_STATUS=$?
set -e
if [[ "$MAIN_STATUS" -ne 0 ]]; then
  echo "ok: main() exits non-zero when the transfer step fails"
else
  echo "FAIL: main() exited 0 despite a failing transfer step"
  FAIL=1
fi
if echo "$MAIN_OUT" | grep -q "FAILED before the swap step reported success"; then
  echo "ok: main() called rollback_and_exit after the transfer failure"
else
  echo "FAIL: main() did not roll back after the transfer failure"
  FAIL=1
fi

if [[ "$FAIL" -eq 0 ]]; then
  echo "== all checks passed =="
  exit 0
else
  echo "== FAILURES ABOVE =="
  exit 1
fi
