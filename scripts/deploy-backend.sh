#!/usr/bin/env bash
# Deploy backend/ to any Docker host reachable over SSH (Pi today, anything
# with `docker compose` and `ssh` later). Nothing host-specific is hardcoded;
# everything comes from flags with env-var fallbacks. Does not touch a host's
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
  --force                    Ignore the worker-pattern abort
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
    ssh "$HOST" "mkdir -p '$tmp'"
    git -c core.autocrlf=false archive "$REF" \
      backend/src backend/pyproject.toml backend/uv.lock backend/Dockerfile \
      | ssh "$HOST" "tar -x -C '$tmp'"
  fi
  run_remote "swap backend/src, copy build files" "$ROOT" "$tmp" "$TS" <<'EOF'
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

deploy() {
  run_remote "build + up -d --no-deps $SERVICE" "$ROOT" "$SERVICE" "${COMPOSE_ARGS[@]}" <<'EOF'
set -euo pipefail
root="$1"; service="$2"; shift 2
cd "$root"
docker compose "$@" build "$service"
docker compose "$@" up -d --no-deps "$service"
EOF
}

verify() {
  run_remote "verify $SERVICE: running, RestartCount 0, twice 5s apart (up to 30s)" \
    "$ROOT" "$SERVICE" "${COMPOSE_ARGS[@]}" <<'EOF'
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
      [[ "$code" -lt 500 ]] || { echo "health check failed: HTTP $code" >&2; return 1; }
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
[deploy] FAILED before the file swap completed. backend/src on $HOST was not
left changed (the swap step restores the previous src itself on failure);
there is nothing to roll back. Fix the issue and re-run.
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

main() {
  preflight
  backup
  transfer || rollback_and_exit
  deploy || rollback_and_exit
  verify || rollback_and_exit
  after_hook || rollback_and_exit
  log "deploy finished: $SERVICE @ $REF ($SHORT_REF) on $HOST"
}

# Only run when executed directly; sourcing (e.g. from the test script, to
# reuse image_repo()) must not parse args or deploy anything.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  parse_args "$@"
  main
fi
