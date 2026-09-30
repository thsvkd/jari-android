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
  "build api" \
  "up -d --no-deps api" \
  "up -d --no-deps" \
  "docker top" \
  "read running searches in api" \
  "python -m korail_bot.mobile running --json" \
  "refuse if a running search is not resumable" \
  "wait for the resumable searches read above to be searching again"; do
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
# "input want" pairs rather than an associative array: macOS ships bash 3.2,
# which has none, and deploys run from a Mac.
for pair in \
  "jari-api jari-api" \
  "jari-api:latest jari-api" \
  "host:5000/repo:v1 host:5000/repo" \
  "host:5000/repo host:5000/repo"; do
  input="${pair% *}"
  want="${pair#* }"
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
  COMPOSE_ARGS=(-f compose.yaml)
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
  COMPOSE_ARGS=(-f compose.yaml)
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
# The case pattern is written (*tar*): bash 3.2, the one macOS ships, cannot
# parse a pattern's lone closing parenthesis inside a command substitution.
SWAP_ONLY_OUT="$(
  ssh() { case "$2" in (*tar*) cat >/dev/null ;; esac; return 0; }
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
  snapshot_running() { return 0; }
  backup() { return 0; }
  build() { return 0; }
  up() { return 0; }
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

echo "== snapshot_running(): a search the restart would lose is refused =="
# What parse_args sets from the default --compose-file. Needed even with
# run_remote faked: bash 3.2 reads an empty array as unset under set -u.
COMPOSE_FILES=(compose.yaml)
COMPOSE_ARGS=(-f compose.yaml)
# What `python -m korail_bot.mobile running --json` prints inside the container.
search() { # id runId workerAlive loggedIn resumable reason
  local reason="null"
  [[ "$6" != "-" ]] && reason="\"$6\""
  printf '{"id":%s,"runId":"%s","pid":10,"workerAlive":%s,"loggedIn":%s,"resumable":%s,"reason":%s,"credentialTtlSeconds":259000,"startedAt":"2026-09-30T00:00:00+00:00"}' \
    "$1" "$2" "$3" "$4" "$5" "$reason"
}
stopped() { # id why at
  printf '{"id":%s,"why":"%s","at":%s}' "$1" "$2" "$3"
}
ended() { # id why at
  printf '{"id":%s,"why":"%s","at":%s}' "$1" "$2" "$3"
}
# running <now> <stopped json|-> <search json>...; ENDED=<ended json> and
# UNREADABLE=<count> add the rest of the listing.
running() {
  local now="$1" gone="$2"; shift 2
  [[ "$gone" == "-" ]] && gone=""
  local IFS=,
  printf 'RUNNING={"now":%s,"searches":[%s],"unreadable":%s,"stopped":[%s],"ended":[%s]}\n' \
    "$now" "$*" "${UNREADABLE:-0}" "$gone" "${ENDED:-}"
}
BEFORE="$(running 1000 - "$(search -1 old true true true -)" "$(search -2 old true true false seats_reserved)")"

set +e
SNAP_REFUSED="$(
  run_remote() { cat >/dev/null; echo "$BEFORE"; }
  DRY_RUN=0; ALLOW_UNRESUMABLE=0; SKIP_RESUME_CHECK=0; EXPECTED_RESUMES=""
  { snapshot_running || echo "SNAPSHOT_RC=$?"; } 2>&1
)"
SNAP_ALLOWED="$(
  # A log line ahead of the listing, as an image with stdout logging printed.
  run_remote() { cat >/dev/null; echo "2026-09-30 WARNING something"; echo "$BEFORE"; }
  DRY_RUN=0; ALLOW_UNRESUMABLE=1; SKIP_RESUME_CHECK=0; EXPECTED_RESUMES=""
  { snapshot_running || echo "SNAPSHOT_RC=$?"; } 2>&1
  echo "EXPECTED=[$EXPECTED_RESUMES] AT=[$SNAPSHOT_AT]"
)"
SNAP_UNREADABLE="$(
  run_remote() { cat >/dev/null; echo "usage: python -m korail_bot.mobile {serve,invite,admin}" >&2; return 2; }
  DRY_RUN=0; ALLOW_UNRESUMABLE=1; SKIP_RESUME_CHECK=0; EXPECTED_RESUMES=""
  { snapshot_running || echo "SNAPSHOT_RC=$?"; } 2>&1
)"
SNAP_SKIPPED="$(
  run_remote() { echo "FAKE run_remote CALLED" >&2; return 1; }
  DRY_RUN=0; ALLOW_UNRESUMABLE=0; SKIP_RESUME_CHECK=1; EXPECTED_RESUMES=""
  { snapshot_running || echo "SNAPSHOT_RC=$?"; } 2>&1
)"
set -e
if echo "$SNAP_REFUSED" | grep -q "SNAPSHOT_RC=1" \
  && echo "$SNAP_REFUSED" | grep -q "REFUSING" \
  && echo "$SNAP_REFUSED" | grep -qF -- "-2 old alive in no seats_reserved"; then
  echo "ok: snapshot_running() refuses a search that would not come back, and names it"
else
  echo "FAIL: snapshot_running() let a search that would not come back through"
  echo "$SNAP_REFUSED"
  FAIL=1
fi
if ! echo "$SNAP_ALLOWED" | grep -q "SNAPSHOT_RC" && echo "$SNAP_ALLOWED" | grep -qF "EXPECTED=[-1 old] AT=[1000]"; then
  echo "ok: --allow-unresumable deploys, expecting back only the resumable search (a log line ahead of the listing is ignored)"
else
  echo "FAIL: --allow-unresumable did not narrow the expected searches to the resumable one"
  echo "$SNAP_ALLOWED"
  FAIL=1
fi
if echo "$SNAP_UNREADABLE" | grep -q "SNAPSHOT_RC=1" && echo "$SNAP_UNREADABLE" | grep -q -- "--skip-resume-check"; then
  echo "ok: an unreadable listing is refused even with --allow-unresumable, pointing at --skip-resume-check"
else
  echo "FAIL: snapshot_running() read an unreadable listing as nothing running"
  echo "$SNAP_UNREADABLE"
  FAIL=1
fi
if ! echo "$SNAP_SKIPPED" | grep -q "SNAPSHOT_RC\|FAKE run_remote CALLED" && echo "$SNAP_SKIPPED" | grep -q "skipping"; then
  echo "ok: --skip-resume-check reads nothing and says so"
else
  echo "FAIL: --skip-resume-check still read the running searches"
  echo "$SNAP_SKIPPED"
  FAIL=1
fi

echo "== verify_resumed(): every search must be searching again, or have ended on its own =="
verify_resumed_against() { # the listing the new container reports; timeout
  local AFTER="$1"
  run_remote() { cat >/dev/null; echo "$AFTER"; }
  DRY_RUN=0; RESUME_TIMEOUT="${2:-0}"; RESUME_POLL=0; EXPECTED_RESUMES="-1 old"; SNAPSHOT_AT=1000
  SNAPSHOT_UNREADABLE="${3:-0}"
  { verify_resumed || echo "VERIFY_RESUMED_RC=$?"; } 2>&1
}
set +e
RESUMED_OK="$(verify_resumed_against "$(running 1300 - "$(search -1 new true true true -)")")"
# Booked while the deploy waited; an older stop on file changes nothing.
RESUMED_ENDED="$(verify_resumed_against "$(ENDED="$(ended -1 booked 1200)" running 1300 "$(stopped -1 crashed 900)")")"
RESUMED_CANCELLED="$(verify_resumed_against "$(ENDED="$(ended -1 cancelled 1200)" running 1300 -)")"
# Its mark a second older than the snapshot clock: the end left the mark just
# before the record went, and the snapshot listed the record in between.
RESUMED_ENDED_EDGE="$(verify_resumed_against "$(ENDED="$(ended -1 booked 999)" running 1300 -)")"
# Gone with no mark of how: not taken on trust, waited for, then reported.
RESUMED_VANISHED="$(verify_resumed_against "$(running 1300 -)")"
# An end mark from before the snapshot is not how this search ended.
RESUMED_OLD_END="$(verify_resumed_against "$(ENDED="$(ended -1 booked 900)" running 1300 -)")"
RESUMED_ERROR="$(verify_resumed_against "$(ENDED="$(ended -1 error 1200)" running 1300 -)" 600)"
# A record the new build cannot read: failed at once, even with the search
# itself listed as fine.
RESUMED_UNREADABLE="$(verify_resumed_against "$(UNREADABLE=1 running 1300 - "$(search -1 new true true true -)")" 600)"
# One was already unreadable at the snapshot and accepted (--allow-unresumable):
# the same one after the restart is not the new build's doing; a second is.
RESUMED_SAME_UNREADABLE="$(verify_resumed_against "$(UNREADABLE=1 running 1300 - "$(search -1 new true true true -)")" 600 1)"
RESUMED_MORE_UNREADABLE="$(verify_resumed_against "$(UNREADABLE=2 running 1300 - "$(search -1 new true true true -)")" 600 1)"
RESUMED_OLD="$(verify_resumed_against "$(running 1300 - "$(search -1 old true true true -)")")"
RESUMED_DEAD="$(verify_resumed_against "$(running 1300 - "$(search -1 new false true true -)")")"
RESUMED_LOGGING_IN="$(verify_resumed_against "$(running 1300 - "$(search -1 new true false true -)")")"
# Stopped after the snapshot: lost for good, so failed at once however long
# the wait would have been.
RESUMED_STOPPED="$(verify_resumed_against "$(running 1300 "$(stopped -1 korail_unreachable 1200)")" 600)"
RESUMED_ABANDONED="$(verify_resumed_against "$(running 1300 "$(stopped -1 not_resumed_seat_held 1100)")" 600)"
set -e
if ! echo "$RESUMED_OK" | grep -q "VERIFY_RESUMED_RC" && echo "$RESUMED_OK" | grep -qF -- "-1 resumed and searching"; then
  echo "ok: verify_resumed() passes once the search runs under the new run, logged in"
else
  echo "FAIL: verify_resumed() did not accept a resumed search"
  echo "$RESUMED_OK"
  FAIL=1
fi
for case in "RESUMED_ENDED:booked" "RESUMED_CANCELLED:cancelled" "RESUMED_ENDED_EDGE:booked"; do
  name="${case%%:*}"
  want="${case#*:}"
  out="${!name}"
  if ! echo "$out" | grep -q "VERIFY_RESUMED_RC" && echo "$out" | grep -qF -- "-1 ended while this waited ($want)"; then
    echo "ok: a search marked as ended ($want) while the deploy waited does not fail it"
  else
    echo "FAIL: verify_resumed() failed the deploy for a search that ended on its own ($want)"
    echo "$out"
    FAIL=1
  fi
done
for case in \
  "RESUMED_OLD:not resumed yet" \
  "RESUMED_DEAD:resumed, but its worker is gone" \
  "RESUMED_LOGGING_IN:resumed, not logged in to Korail yet" \
  "RESUMED_STOPPED:stopped (korail_unreachable)" \
  "RESUMED_ABANDONED:stopped (not_resumed_seat_held)" \
  "RESUMED_VANISHED:record gone with no mark of how it ended" \
  "RESUMED_OLD_END:record gone with no mark of how it ended" \
  "RESUMED_ERROR:ended on an error (error)"; do
  name="${case%%:*}"
  want="${case#*:}"
  out="${!name}"
  if echo "$out" | grep -q "VERIFY_RESUMED_RC=1" && echo "$out" | grep -qF -- "-1 $want"; then
    echo "ok: verify_resumed() fails loudly and names the search ($want)"
  else
    echo "FAIL: verify_resumed() missed a search that did not come back ($want)"
    echo "$out"
    FAIL=1
  fi
done

if echo "$RESUMED_UNREADABLE" | grep -q "VERIFY_RESUMED_RC=1" \
  && echo "$RESUMED_UNREADABLE" | grep -q "1 running record(s) the new build cannot read"; then
  echo "ok: verify_resumed() fails at once on running records the new build cannot read"
else
  echo "FAIL: verify_resumed() passed with running records the new build cannot read"
  echo "$RESUMED_UNREADABLE"
  FAIL=1
fi

if ! echo "$RESUMED_SAME_UNREADABLE" | grep -q "VERIFY_RESUMED_RC" \
  && echo "$RESUMED_SAME_UNREADABLE" | grep -qF -- "-1 resumed and searching"; then
  echo "ok: an unreadable record already there at the snapshot does not fail every poll"
else
  echo "FAIL: verify_resumed() failed on an unreadable record accepted before the restart"
  echo "$RESUMED_SAME_UNREADABLE"
  FAIL=1
fi
if echo "$RESUMED_MORE_UNREADABLE" | grep -q "VERIFY_RESUMED_RC=1" \
  && echo "$RESUMED_MORE_UNREADABLE" | grep -q "2 running record(s) the new build cannot read (1 before the restart)"; then
  echo "ok: more unreadable records after the restart than at the snapshot fail at once"
else
  echo "FAIL: verify_resumed() missed records that became unreadable after the restart"
  echo "$RESUMED_MORE_UNREADABLE"
  FAIL=1
fi

set +e
SNAP_UNREADABLE_ALLOWED="$(
  run_remote() { cat >/dev/null; UNREADABLE=2 running 1000 - "$(search -1 old true true true -)"; }
  DRY_RUN=0; ALLOW_UNRESUMABLE=1; SKIP_RESUME_CHECK=0; EXPECTED_RESUMES=""; SNAPSHOT_UNREADABLE=0
  { snapshot_running || echo "SNAPSHOT_RC=$?"; } 2>&1
  echo "BEFORE_UNREADABLE=[$SNAPSHOT_UNREADABLE]"
)"
set -e
if ! echo "$SNAP_UNREADABLE_ALLOWED" | grep -q "SNAPSHOT_RC" && echo "$SNAP_UNREADABLE_ALLOWED" | grep -qF "BEFORE_UNREADABLE=[2]"; then
  echo "ok: --allow-unresumable accepts unreadable records and remembers how many there were"
else
  echo "FAIL: snapshot_running() did not remember the unreadable records it accepted"
  echo "$SNAP_UNREADABLE_ALLOWED"
  FAIL=1
fi

set +e
SNAP_UNREADABLE_RECORD="$(
  run_remote() { cat >/dev/null; UNREADABLE=2 running 1000 -; }
  DRY_RUN=0; ALLOW_UNRESUMABLE=0; SKIP_RESUME_CHECK=0; EXPECTED_RESUMES=""
  { snapshot_running || echo "SNAPSHOT_RC=$?"; } 2>&1
)"
set -e
if echo "$SNAP_UNREADABLE_RECORD" | grep -q "SNAPSHOT_RC=1" && echo "$SNAP_UNREADABLE_RECORD" | grep -q "2 running record(s) cannot be read"; then
  echo "ok: snapshot_running() refuses when running records cannot be read by the running build"
else
  echo "FAIL: snapshot_running() ignored unreadable running records"
  echo "$SNAP_UNREADABLE_RECORD"
  FAIL=1
fi

echo "== main(): the listing is read again right before the restart =="
READS="$(mktemp)"
set +e
MAIN_REFUSED_OUT="$(
  preflight() { return 0; }
  backup() { return 0; }
  transfer() { SWAPPED=1; }
  build() { echo "BUILT" >&2; }
  up() { echo "UP CALLED" >&2; }
  verify() { return 0; }
  after_hook() { return 0; }
  # Resumable when first read; by the time the build is done a seat is held.
  run_remote() {
    cat >/dev/null
    echo x >>"$READS"
    if [[ "$(wc -l <"$READS" | tr -d ' ')" -eq 1 ]]; then
      running 1000 - "$(search -1 old true true true -)"
    else
      running 1100 - "$(search -1 old true true false seat_held)"
    fi
  }
  HOST="fake@host"; ROOT="/srv/app"; REF="HEAD"; SERVICE="api"; DRY_RUN=0
  SHORT_REF="deadbee"; TS="20260101T000000Z"; ALLOW_UNRESUMABLE=0; SKIP_RESUME_CHECK=0
  main 2>&1
)"
MAIN_REFUSED_STATUS=$?
set -e
rm -f "$READS"
if [[ "$MAIN_REFUSED_STATUS" -ne 0 ]] \
  && echo "$MAIN_REFUSED_OUT" | grep -q "BUILT" \
  && echo "$MAIN_REFUSED_OUT" | grep -q "REFUSED right before the restart" \
  && ! echo "$MAIN_REFUSED_OUT" | grep -q "UP CALLED"; then
  echo "ok: main() re-reads after the build and refuses without restarting"
else
  echo "FAIL: main() restarted on a listing read before the build"
  echo "$MAIN_REFUSED_OUT"
  FAIL=1
fi

echo "== main(): a search that does not come back fails the deploy without a rollback =="
set +e
MAIN_RESUME_OUT="$(
  preflight() { return 0; }
  snapshot_running() { EXPECTED_RESUMES="-1 old"; SNAPSHOT_AT=1000; }
  backup() { return 0; }
  transfer() { SWAPPED=1; }
  build() { return 0; }
  up() { return 0; }
  verify() { return 0; }
  after_hook() { return 0; }
  run_remote() { cat >/dev/null; running 1300 - "$(search -1 old true true true -)"; }
  HOST="fake@host"; ROOT="/srv/app"; REF="HEAD"; SERVICE="api"; DRY_RUN=0
  SHORT_REF="deadbee"; TS="20260101T000000Z"; RESUME_TIMEOUT=0; RESUME_POLL=0
  main 2>&1
)"
MAIN_RESUME_STATUS=$?
set -e
if [[ "$MAIN_RESUME_STATUS" -ne 0 ]] \
  && echo "$MAIN_RESUME_OUT" | grep -qF -- "-1 not resumed yet" \
  && echo "$MAIN_RESUME_OUT" | grep -q "Rolling back would not bring them back" \
  && ! echo "$MAIN_RESUME_OUT" | grep -q "Roll back on"; then
  echo "ok: main() exits non-zero, names what did not come back, and does not roll back"
else
  echo "FAIL: main() did not fail loudly on a search that did not come back"
  echo "$MAIN_RESUME_OUT"
  FAIL=1
fi

if [[ "$FAIL" -eq 0 ]]; then
  echo "== all checks passed =="
  exit 0
else
  echo "== FAILURES ABOVE =="
  exit 1
fi
