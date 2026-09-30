#!/usr/bin/env bash
# Deploy backend/ to any Docker host reachable over SSH — anything with
# `docker compose` and `ssh`. Nothing host-specific is hardcoded; everything
# comes from flags with env-var fallbacks. Does not touch a host's
# proxy/gateway config — pass that as --after.
set -euo pipefail

HOST="${DEPLOY_HOST:-}"
ROOT="${DEPLOY_ROOT:-}"
REF="${DEPLOY_REF:-HEAD}"
SERVICE="${DEPLOY_SERVICE:-api}"
WORKER_PATTERN="${DEPLOY_WORKER_PATTERN:-mobile.worker}"
AFTER_HOOK=""
HEALTH_URL=""
DRY_RUN=0
FORCE=0
ALLOW_UNRESUMABLE=0
SKIP_RESUME_CHECK=0
# How long to wait, after the restart, for every search that was running to
# be searching again in the new container: recorded under the new run, its
# worker alive and logged in to Korail. `up -d` only returns once the old
# container has stopped, so its 60 s stop grace is already spent. What is
# left, at worst, one after another:
#   135 s  a lease the old container failed to release (runtime.LEASE_WAIT)
#   310 s  every resume retry before the runtime gives up
#          (10+20+40+80+160, reservation_service._RESUME_RETRY_SECONDS)
#    60 s  those six tries only happen on a pass of the background loop,
#          one every 10 s (runtime.PASS_WAIT), so each can start up to one
#          pass after it falls due
#   230 s  the resumed worker's login retries while Korail does not answer
#          (5+15+30+60+120, settings.LOGIN_RETRY_DELAYS_SECONDS)
#    45 s  the starts and the logins themselves
# A login attempt that hangs to the client's own timeout instead of failing
# at once can take longer than this; the check then fails loudly, naming the
# search as not logged in yet, and the search may still come back.
RESUME_TIMEOUT="${DEPLOY_RESUME_TIMEOUT:-$((135 + 310 + 60 + 230 + 45))}"
RESUME_POLL="${DEPLOY_RESUME_POLL:-5}"
# "<id> <runId>" per search the new container must bring back, one per line,
# and the container's clock when that list was read: a search stopped after
# that moment was stopped by this deploy.
EXPECTED_RESUMES=""
SNAPSHOT_AT=0
declare -a COMPOSE_FILES=()
if [[ -n "${DEPLOY_COMPOSE_FILES:-}" ]]; then
  IFS=',' read -r -a COMPOSE_FILES <<<"$DEPLOY_COMPOSE_FILES"
fi
declare -a COMPOSE_ARGS=()
TS=""
SHORT_REF=""
IMAGE_NAME=""
IMAGE_REPO=""
IMAGE_TAGGED=0
SWAPPED=0

usage() {
  cat <<'USAGE'
Usage: deploy-backend.sh --host user@host --root /remote/dir [options]
  --host <user@host>        SSH target (env DEPLOY_HOST, required)
  --root <path>              Remote dir holding backend/ + compose files (env DEPLOY_ROOT, required)
  --ref <git-ref>            Git ref to deploy (env DEPLOY_REF, default HEAD)
  --compose-file <file>      Repeatable (env DEPLOY_COMPOSE_FILES, comma-separated; default compose.yaml)
  --service <name>           Compose service to build/restart (env DEPLOY_SERVICE, default api)
  --worker-pattern <regex>   Abort if a matching process runs in the service container (env DEPLOY_WORKER_PATTERN, default mobile.worker)
  --health-url <url>         Local curl check after restart, expects HTTP < 500
  --after "<cmd>"            Remote shell command run in --root after a healthy deploy
  --dry-run                  Print every remote command, run nothing
  --force                    Ignore the worker-pattern abort. The restart kills the
                             running searches; the new container resumes them, and
                             the deploy waits until each one is running again
  --allow-unresumable        Deploy even if some running search would not come back
                             (listed first); the rest are still checked
  --skip-resume-check        Do not read or check the running searches at all, e.g.
                             when the running image predates `korail_bot.mobile running`
USAGE
}

log() { echo "[deploy] $*" >&2; }

# Strip an image ref's tag, keeping a registry host:port intact.
#   jari-api            -> jari-api        (no tag)
#   jari-api:latest      -> jari-api
#   host:5000/repo:v1    -> host:5000/repo
#   host:5000/repo       -> host:5000/repo  (no tag; the ':' is the registry port)
image_repo() {
  local img="$1" tail="${1##*:}"
  if [[ "$img" == *:* && "$tail" != */* ]]; then
    printf '%s\n' "${img%:*}"
  else
    printf '%s\n' "$img"
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --host) HOST="$2"; shift 2 ;;
      --root) ROOT="$2"; shift 2 ;;
      --ref) REF="$2"; shift 2 ;;
      --compose-file) COMPOSE_FILES+=("$2"); shift 2 ;;
      --service) SERVICE="$2"; shift 2 ;;
      --worker-pattern) WORKER_PATTERN="$2"; shift 2 ;;
      --health-url) HEALTH_URL="$2"; shift 2 ;;
      --after) AFTER_HOOK="$2"; shift 2 ;;
      --dry-run) DRY_RUN=1; shift ;;
      --force) FORCE=1; shift ;;
      --allow-unresumable) ALLOW_UNRESUMABLE=1; shift ;;
      --skip-resume-check) SKIP_RESUME_CHECK=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "unknown arg: $1" >&2; usage; exit 1 ;;
    esac
  done
  [[ ${#COMPOSE_FILES[@]} -eq 0 ]] && COMPOSE_FILES=(compose.yaml)
  [[ -z "$HOST" ]] && { echo "--host/DEPLOY_HOST required" >&2; exit 1; }
  [[ -z "$ROOT" ]] && { echo "--root/DEPLOY_ROOT required" >&2; exit 1; }

  for f in "${COMPOSE_FILES[@]}"; do COMPOSE_ARGS+=(-f "$f"); done
  TS="$(date -u +%Y%m%dT%H%M%SZ)"
  SHORT_REF="$(git rev-parse --short "$REF")"
}

# run_remote <description> <arg>... <<'EOF' ... EOF
# %q-quotes each arg into a single "bash -s -- ..." string locally, then sends
# that as ONE argument to ssh. ssh joins multiple command-line args with plain
# spaces before handing them to the remote shell, which would otherwise split
# any arg (e.g. --after "a b", or --root "/path with space") back apart.
# Args are read back as $1, $2, ... inside the remote script. In --dry-run,
# prints the script instead of connecting (never invokes ssh).
run_remote() {
  local desc="$1"; shift
  local remote_cmd="bash -s -- $(printf '%q ' "$@")"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY-RUN ssh $HOST $remote_cmd : $desc"
    cat
    return 0
  fi
  log "$desc"
  ssh "$HOST" "$remote_cmd"
}

preflight() {
  run_remote "preflight: backend dir, docker compose, worker check" \
    "$ROOT" "$SERVICE" "$WORKER_PATTERN" "$FORCE" "${COMPOSE_ARGS[@]}" <<'EOF'
set -euo pipefail
root="$1"; service="$2"; pattern="$3"; force="$4"; shift 4
cd "$root"
[[ -d backend ]] || { echo "missing $root/backend" >&2; exit 1; }
docker compose version >/dev/null
cid="$(docker compose "$@" ps -q "$service" 2>/dev/null || true)"
# docker top reads the host's process table for the container's PIDs, so it
# works even when the container image has no `ps` (slim/distroless) inside it.
# Capture the output before grepping it: piping straight into `grep -q` can
# SIGPIPE docker top once grep is satisfied, and under pipefail that nonzero
# exit would make this condition false even though the pattern matched.
if [[ -n "$cid" ]]; then
  top="$(docker top "$cid" 2>/dev/null || true)"
  if grep -Eq "$pattern" <<<"$top"; then
    if [[ "$force" != "1" ]]; then
      echo "worker matching '$pattern' running in $service; use --force to override" >&2
      exit 1
    fi
    echo "worker matching '$pattern' running, continuing (--force)"
  fi
fi
echo "preflight ok"
EOF
}

# The running searches, as the API container itself reports them
# (python -m korail_bot.mobile running --json: read-only, no secrets). The
# command prints one RUNNING=<json> line and sends its logging to stderr;
# with no container running, this prints an empty listing of its own.
read_running() {
  run_remote "read running searches in $SERVICE" "$ROOT" "$SERVICE" "${COMPOSE_ARGS[@]}" <<'EOF'
set -euo pipefail
root="$1"; service="$2"; shift 2
cd "$root"
cid="$(docker compose "$@" ps -q "$service" 2>/dev/null || true)"
if [[ -z "$cid" ]]; then
  echo 'RUNNING={"now":0,"searches":[],"stopped":[]}'
  exit 0
fi
docker compose "$@" exec -T "$service" python -m korail_bot.mobile running --json
EOF
}

# running_rows < read_running output: the last RUNNING= line, as
#   now <epoch>
#   unreadable <count>      running records this build cannot parse
#   search <id> <runId> <alive|gone> <in|out> <yes|no> <reason|->
#   stopped <id> <why> <epoch>   stopped without finishing, or not resumed
#   ended <id> <why> <epoch>     ended on its own: booked|cancelled|error
# (in|out: logged in to Korail; yes|no: resumable). Only that line is read,
# so whatever else reaches stdout cannot spoil it; fails without one.
running_rows() {
  python3 -c '
import json, sys
lines = [line[len("RUNNING="):] for line in sys.stdin if line.startswith("RUNNING=")]
listing = json.loads(lines[-1])
print("now", listing["now"])
print("unreadable", listing["unreadable"])
for row in listing["searches"]:
    print("search", row["id"], row["runId"], "alive" if row["workerAlive"] else "gone",
          "in" if row["loggedIn"] else "out", "yes" if row["resumable"] else "no",
          row["reason"] or "-")
for row in listing["stopped"]:
    print("stopped", row["id"], row["why"], row["at"])
for row in listing["ended"]:
    print("ended", row["id"], row["why"], row["at"])
'
}

# Which searches are running, and would the restart bring each back? One that
# would not (seats already held, no stored login, resume turned off) is lost
# by this deploy, so it is refused unless --allow-unresumable says that is
# accepted. The rest are what verify_resumed waits for afterwards.
#
# Called twice: once before anything changes, to refuse early, and again
# right before `up`, because the build between them takes minutes and the
# list that counts is the one the restart actually meets.
#
# Every failure returns explicitly, as transfer() does: main() calls the
# second one as `fn || ...`, which turns errexit off inside it.
snapshot_running() {
  if [[ "$SKIP_RESUME_CHECK" -eq 1 ]]; then
    log "skipping the running-search check (--skip-resume-check): nothing is checked afterwards"
    return 0
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    read_running >&2
    log "DRY-RUN refuse if a running search is not resumable; after the restart wait up to ${RESUME_TIMEOUT}s for each to be searching again"
    return 0
  fi
  local out rows searches unresumable unreadable
  if ! out="$(read_running)" || ! rows="$(printf '%s\n' "$out" | running_rows)"; then
    echo "[deploy] REFUSING: could not read the running searches from $SERVICE (an image without 'python -m korail_bot.mobile running'?); pass --skip-resume-check to deploy without checking them" >&2
    return 1
  fi
  SNAPSHOT_AT="$(printf '%s\n' "$rows" | awk '$1 == "now" { print $2 }')"
  unreadable="$(printf '%s\n' "$rows" | awk '$1 == "unreadable" { print $2 }')"
  if [[ "${unreadable:-0}" -gt 0 ]]; then
    if [[ "$ALLOW_UNRESUMABLE" -ne 1 ]]; then
      echo "[deploy] REFUSING: $unreadable running record(s) cannot be read by the running build; nothing would resume them" >&2
      echo "[deploy] pass --allow-unresumable to accept losing them" >&2
      return 1
    fi
    log "WARNING: $unreadable unreadable running record(s) will not come back (--allow-unresumable)"
  fi
  searches="$(printf '%s\n' "$rows" | awk '$1 == "search" { $1 = ""; sub(/^ /, ""); print }')"
  if [[ -z "$searches" ]]; then
    EXPECTED_RESUMES=""
    log "no running searches"
    return 0
  fi
  log "running searches (id run worker login resumable reason):"
  printf '%s\n' "$searches" | sed 's/^/[deploy]   /' >&2
  unresumable="$(printf '%s\n' "$searches" | awk '$5 == "no"')"
  if [[ -n "$unresumable" ]]; then
    if [[ "$ALLOW_UNRESUMABLE" -ne 1 ]]; then
      echo "[deploy] REFUSING: the restart would end these searches for good:" >&2
      printf '%s\n' "$unresumable" | sed 's/^/[deploy]   /' >&2
      echo "[deploy] wait for them to finish, or pass --allow-unresumable to accept losing them" >&2
      return 1
    fi
    log "WARNING: these will not come back (--allow-unresumable):"
    printf '%s\n' "$unresumable" | sed 's/^/[deploy]   /' >&2
  fi
  EXPECTED_RESUMES="$(printf '%s\n' "$searches" | awk '$5 == "yes" { print $1, $2 }')"
}

# judge_resumes <rows>: one line per expected search -
#   ok <id> <note>    searching again, or booked or cancelled while this waited
#   wait <id> <why>   not back yet; may still come back
#   lost <id> <why>   stopped, not resumed, or ended on an error after the
#                     snapshot; or records the new build cannot read
# A record that is simply gone counts as finished only with the mark the end
# of a search leaves (search_ended): without one it may as well have been
# lost - unreadable to the new build, or gone with Redis - and it is waited
# for, then reported, rather than taken on trust.
judge_resumes() {
  awk -v since="$SNAPSHOT_AT" '
    NR == FNR {
      if ($1 == "unreadable") unreadable = $2 + 0
      if ($1 == "search") { run[$2] = $3; alive[$2] = $4; login[$2] = $5 }
      if ($1 == "stopped" && $4 + 0 >= since + 0) stopped[$2] = $3
      if ($1 == "ended" && $4 + 0 >= since + 0) ended[$2] = $3
      next
    }
    {
      id = $1; old = $2
      if (id in run) {
        if (run[id] == old)              print "wait", id, "not resumed yet (still the old run)"
        else if (alive[id] != "alive")   print "wait", id, "resumed, but its worker is gone"
        else if (login[id] != "in")      print "wait", id, "resumed, not logged in to Korail yet"
        else                             print "ok", id, "resumed and searching"
      } else if (id in stopped)          print "lost", id, "stopped (" stopped[id] ")"
      # "in" before any lookup: reading ended[id] would create the entry.
      else if (!(id in ended))           print "wait", id, "record gone with no mark of how it ended"
      else if (ended[id] == "booked" || ended[id] == "cancelled")
                                         print "ok", id, "ended while this waited (" ended[id] ")"
      else                               print "lost", id, "ended on an error (" ended[id] ")"
    }
    END {
      if (unreadable > 0) print "lost", "-", unreadable " running record(s) the new build cannot read"
    }
  ' <(printf '%s\n' "$1") <(printf '%s\n' "$EXPECTED_RESUMES")
}

# After the restart: every search snapshot_running saw as resumable must be
# searching again - under the new container's run, its worker alive and
# logged in to Korail - or have ended on its own. A worker merely spawned is
# not enough: it can spend minutes riding out Korail before it logs in, and
# then fail to. Anything else by the deadline is named; a search found
# stopped fails the deploy as soon as nothing else is left to wait for.
verify_resumed() {
  if [[ -z "$EXPECTED_RESUMES" ]]; then
    [[ "$DRY_RUN" -eq 1 ]] && log "DRY-RUN wait for the resumable searches read above to be searching again (every ${RESUME_POLL}s, up to ${RESUME_TIMEOUT}s)"
    return 0
  fi
  local deadline=$((SECONDS + RESUME_TIMEOUT)) out rows verdicts pending lost
  while :; do
    if out="$(read_running 2>/dev/null)" && rows="$(printf '%s\n' "$out" | running_rows)"; then
      verdicts="$(judge_resumes "$rows")"
    else
      verdicts="$(printf '%s\n' "$EXPECTED_RESUMES" | awk '{ print "wait", $1, "unknown (running searches unreadable)" }')"
    fi
    pending="$(printf '%s\n' "$verdicts" | awk '$1 == "wait" { $1 = ""; sub(/^ /, ""); print }')"
    lost="$(printf '%s\n' "$verdicts" | awk '$1 == "lost" { $1 = ""; sub(/^ /, ""); print }')"
    if [[ -z "$pending" && -z "$lost" ]]; then
      printf '%s\n' "$verdicts" | awk '{ $1 = ""; sub(/^ /, ""); print }' | sed 's/^/[deploy]   /' >&2
      log "every running search is back or ended on its own ($(printf '%s\n' "$EXPECTED_RESUMES" | wc -l | tr -d ' '))"
      return 0
    fi
    if [[ -z "$pending" ]] || (( SECONDS >= deadline )); then
      echo "[deploy] FAILED: after the restart these searches are not searching again:" >&2
      printf '%s\n' "$lost" "$pending" | sed '/^$/d; s/^/[deploy]   /' >&2
      return 1
    fi
    sleep "$RESUME_POLL"
  done
}

# One canonical place computes the repo (image_repo(), above); backup() just
# reads .Config.Image and calls it locally, then ships the already-normalized
# repo to a second remote call. (Duplicating that logic inside a quoted
# heredoc would make it untestable dead code — a heredoc can't call a local
# bash function.)
backup() {
  local out
  out="$(run_remote "backup: cp -a backend, read current image" \
    "$ROOT" "$SERVICE" "$TS" "${COMPOSE_ARGS[@]}" <<'EOF'
set -euo pipefail
root="$1"; service="$2"; ts="$3"; shift 3
cd "$root"
cp -a backend "backend.bak-${ts}"
cid="$(docker compose "$@" ps -q "$service" 2>/dev/null || true)"
if [[ -n "$cid" ]]; then
  echo "IMAGE=$(docker inspect --format '{{.Config.Image}}' "$cid")"
else
  echo "IMAGE="
fi
EOF
)"
  echo "$out" >&2

  if [[ "$DRY_RUN" -eq 1 ]]; then
    # The real image name is only known after the call above actually runs,
    # so show the tag step's shape with a placeholder instead of skipping it.
    IMAGE_NAME="<image from the read above>"
    IMAGE_REPO="<repo>"
  else
    IMAGE_NAME="$(echo "$out" | sed -n 's/^IMAGE=//p' | tail -1)"
    if [[ -z "$IMAGE_NAME" ]]; then
      log "no running $SERVICE container; skipping image tag"
      return 0
    fi
    if [[ "$IMAGE_NAME" == sha256:* ]]; then
      log "current image is a bare digest ($IMAGE_NAME), not a name:tag; skipping pre-deploy tag (rollback would fall back to backend.bak-${TS} only)"
      return 0
    fi
    IMAGE_REPO="$(image_repo "$IMAGE_NAME")"
  fi

  run_remote "backup: tag current image as ${IMAGE_REPO}:pre-${SHORT_REF}" \
    "$IMAGE_NAME" "$IMAGE_REPO" "$SHORT_REF" <<'EOF' >&2
set -euo pipefail
image="$1"; repo="$2"; short_ref="$3"
docker tag "$image" "${repo}:pre-${short_ref}"
echo "tagged ${repo}:pre-${short_ref}"
EOF
  [[ "$DRY_RUN" -eq 1 ]] || IMAGE_TAGGED=1
}

transfer() {
  local tmp="/tmp/deploy-backend-${TS}"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY-RUN git archive $REF backend/src backend/pyproject.toml backend/uv.lock backend/Dockerfile | ssh $HOST tar -x -C $tmp"
  else
    log "transfer: archiving $REF to $HOST:$tmp"
    # This function is called as `transfer || rollback_and_exit`, which turns
    # off errexit for its whole body (bash quirk: `set -e` does not apply
    # inside a command that is the left side of `||`). Every step that can
    # fail must therefore propagate with an explicit `|| return 1`, or a
    # remote failure here would fall through silently to SWAPPED=1 below.
    ssh "$HOST" "mkdir -p '$tmp'" || return 1
    git -c core.autocrlf=false archive "$REF" \
      backend/src backend/pyproject.toml backend/uv.lock backend/Dockerfile \
      | ssh "$HOST" "tar -x -C '$tmp'" || return 1
  fi
  run_remote "swap backend/src, copy build files" "$ROOT" "$tmp" "$TS" <<'EOF' || return 1
set -euo pipefail
root="$1"; tmp="$2"; ts="$3"
cd "$root/backend"
mkdir -p ".replaced-${ts}"
mv src ".replaced-${ts}/src"
mv "$tmp/backend/src" src || {
  mv ".replaced-${ts}/src" src
  echo "swap rolled back: could not move new src into place" >&2
  exit 1
}
cp "$tmp/backend/pyproject.toml" pyproject.toml
cp "$tmp/backend/uv.lock" uv.lock
cp "$tmp/backend/Dockerfile" Dockerfile
rm -rf "$tmp"
echo "swap ok, previous src at backend/.replaced-${ts}/src"
EOF
  SWAPPED=1
}

# Build and restart are separate steps so that the running searches can be
# read again between them (snapshot_running): the build takes minutes.
build() {
  run_remote "build $SERVICE" "$ROOT" "$SERVICE" "${COMPOSE_ARGS[@]}" <<'EOF'
set -euo pipefail
root="$1"; service="$2"; shift 2
cd "$root"
docker compose "$@" build "$service"
EOF
}

up() {
  run_remote "up -d --no-deps $SERVICE" "$ROOT" "$SERVICE" "${COMPOSE_ARGS[@]}" <<'EOF'
set -euo pipefail
root="$1"; service="$2"; shift 2
cd "$root"
docker compose "$@" up -d --no-deps "$service"
EOF
}

verify() {
  # Called as `verify || rollback_and_exit`: same errexit-off quirk as
  # transfer() above, so the remote check's failure must be propagated
  # explicitly instead of relying on set -e.
  run_remote "verify $SERVICE: running, RestartCount 0, twice 5s apart (up to 30s)" \
    "$ROOT" "$SERVICE" "${COMPOSE_ARGS[@]}" <<'EOF' || return 1
set -euo pipefail
root="$1"; service="$2"; shift 2
cd "$root"
healthy() {
  local cid state restarts
  cid="$(docker compose "$@" ps -q "$service" 2>/dev/null || true)"
  [[ -n "$cid" ]] || return 1
  read -r state restarts < <(docker inspect --format '{{.State.Status}} {{.RestartCount}}' "$cid")
  [[ "$state" == "running" && "$restarts" == "0" ]]
}
for _ in $(seq 1 15); do
  if healthy "$@"; then
    sleep 5
    if healthy "$@"; then
      echo "verify ok"
      exit 0
    fi
    echo "verify failed: $service crashed shortly after starting" >&2
    exit 1
  fi
  sleep 2
done
echo "verify failed: $service not healthy after 30s" >&2
exit 1
EOF
  if [[ -n "$HEALTH_URL" ]]; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "DRY-RUN curl $HEALTH_URL, expect HTTP < 500"
    else
      local code
      code="$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH_URL")"
      # curl prints "000" (not a real HTTP status) when it never got a
      # response at all, e.g. connection refused; "000" < 500 would
      # otherwise read as a passing health check.
      [[ "$code" -lt 500 && "$code" != "000" ]] || { echo "health check failed: HTTP $code" >&2; return 1; }
      log "health check ok: HTTP $code"
    fi
  fi
}

after_hook() {
  [[ -z "$AFTER_HOOK" ]] && return 0
  run_remote "after hook" "$ROOT" "$AFTER_HOOK" <<'EOF'
set -euo pipefail
cd "$1"
bash -c "$2"
EOF
}

rollback_and_exit() {
  if [[ "$SWAPPED" -ne 1 ]]; then
    cat >&2 <<MSG
[deploy] FAILED before the swap step reported success. In the common case
backend/src on $HOST was not left changed (moving the new src into place
itself puts the previous src back on failure); there is nothing to roll
back. But if the swap step failed AFTER moving the new src in -- copying
pyproject.toml/uv.lock/Dockerfile, or the final cleanup -- src was already
replaced even though this run reports failure. Check $HOST:$ROOT/backend for
a leftover .replaced-${TS}/src before assuming nothing changed. Fix the
issue and re-run.
MSG
    exit 1
  fi
  local docker_rollback
  if [[ "$IMAGE_TAGGED" -eq 1 ]]; then
    docker_rollback="docker tag '${IMAGE_REPO}:pre-${SHORT_REF}' '${IMAGE_NAME}'"
  else
    docker_rollback="# no pre-deploy image tag exists (no running $SERVICE container at backup time, or its image was a bare sha256 ID with no name:tag to restore) -- restore the image from backend.bak-${TS} or rebuild it instead"
  fi
  cat >&2 <<MSG
[deploy] FAILED after the file swap. Roll back on $HOST with:

  ssh $HOST bash -s <<'ROLLBACK'
set -euo pipefail
cd '$ROOT/backend'
rm -rf src
mv '.replaced-${TS}/src' src
cd '$ROOT'
${docker_rollback}
docker compose $(printf -- '-f %q ' "${COMPOSE_FILES[@]}") up -d --no-deps '$SERVICE'
ROLLBACK

Full pre-deploy backend/ is also at $ROOT/backend.bak-${TS} if src alone is not enough.
MSG
  exit 1
}

resume_failed_and_exit() {
  cat >&2 <<MSG
[deploy] The new $SERVICE is up and healthy, but the searches listed above are
not searching again. Rolling back would not bring them back: the old container
and its workers are gone. Find out why on $HOST:

  ssh $HOST 'cd $ROOT && docker compose $(printf -- '-f %q ' "${COMPOSE_FILES[@]}")logs --since 10m $SERVICE'

A search the app is still retrying may yet come back by itself; one it gave
up on has been announced to its user as stopped.
MSG
  exit 1
}

refused_before_up_and_exit() {
  local docker_restore
  if [[ "$IMAGE_TAGGED" -eq 1 ]]; then
    docker_restore="docker tag '${IMAGE_REPO}:pre-${SHORT_REF}' '${IMAGE_NAME}'"
  else
    docker_restore="# no pre-deploy image tag exists; the next build from the restored src rebuilds it"
  fi
  cat >&2 <<MSG
[deploy] REFUSED right before the restart. Nothing was restarted and the
running searches are untouched, but backend/src on $HOST is already the new
version and the $SERVICE image was rebuilt from it (the running container
still runs the old image). Re-run the deploy once those searches are done,
or put the old version back WITHOUT restarting:

  ssh $HOST bash -s <<'RESTORE'
set -euo pipefail
cd '$ROOT/backend'
rm -rf src
mv '.replaced-${TS}/src' src
${docker_restore}
RESTORE
MSG
  exit 1
}

main() {
  preflight
  snapshot_running
  backup
  transfer || rollback_and_exit
  build || rollback_and_exit
  snapshot_running || refused_before_up_and_exit
  up || rollback_and_exit
  verify || rollback_and_exit
  after_hook || rollback_and_exit
  verify_resumed || resume_failed_and_exit
  log "deploy finished: $SERVICE @ $REF ($SHORT_REF) on $HOST"
}

# Only run when executed directly; sourcing (e.g. from the test script, to
# reuse image_repo()) must not parse args or deploy anything.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  parse_args "$@"
  main
fi
