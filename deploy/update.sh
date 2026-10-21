#!/usr/bin/env bash
# Safe update of a gateway from the git repository.
#
#   bash update.sh [branch]      update to the latest commit of <branch>
#                                (default: the branch used last time, else "main")
#   bash update.sh --rollback    restore the code AND database from just before the last update
#   FORCE=1 bash update.sh       redeploy even if already up to date
#
# What it does, in order: backs up bridge.db and the current code -> pulls the code ->
# installs requirements only if they changed -> syntax-checks -> restarts the service ->
# waits for the web app to answer. If anything fails it rolls back by itself.
# bridge.db, backup/ and the venv are never touched by git (see .gitignore).
#
# Settings (environment variables, all optional):
#   APP_DIR (/opt/modbus_bridge)   REPO_URL (https://github.com/usamasial/epic---annexe.git; use the
#   git@github.com:... form together with a deploy key for a private repository)   KEEP (5 backups)
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/modbus_bridge}"
DEFAULT_REPO_URL="https://github.com/usamasial/epic---annexe.git"
SERVICE="${SERVICE:-modbus-bridge}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/api/auth/status}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"      # busy gateways (hundreds of meters) can take a while to start answering
HEALTH_REQUEST_TIMEOUT="${HEALTH_REQUEST_TIMEOUT:-10}"
RESTART_CMD="${RESTART_CMD:-systemctl restart $SERVICE}"
STOP_CMD="${STOP_CMD:-systemctl stop $SERVICE}"
KEEP="${KEEP:-5}"
VENV_PY="${VENV_PY:-$APP_DIR/venv/bin/python}"

log()  { echo "[update] $*"; }
die()  { echo "[update] ERROR: $*" >&2; exit 1; }

health_ok() {
  if [ -n "${HEALTH_CMD:-}" ]; then eval "$HEALTH_CMD" >/dev/null 2>&1; return; fi
  python3 - "$HEALTH_URL" "$HEALTH_REQUEST_TIMEOUT" <<'PY' >/dev/null 2>&1
import sys, urllib.request
# no proxy for the local check, whatever the environment says
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
opener.open(sys.argv[1], timeout=float(sys.argv[2])).read()
PY
}

wait_healthy() {
  local waited=0
  while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    health_ok && return 0
    sleep 2; waited=$((waited + 2))
    [ $((waited % 20)) -eq 0 ] && log "still waiting for the app to answer (${waited}s of ${HEALTH_TIMEOUT}s)..."
  done
  return 1
}

app_version() { grep -m1 '^APP_VERSION' "$APP_DIR/app.py" 2>/dev/null | cut -d'"' -f2 || true; }

lowprio() {  # keep the gateway responsive while copying a large database
  if command -v ionice >/dev/null 2>&1; then nice -n 19 ionice -c3 "$@"; else nice -n 19 "$@"; fi
}

# ---------------------------------------------------------------- rollback
do_rollback() {  # $1 = stamp, $2 = old commit or "none", $3 = reason
  log "ROLLING BACK ($3)"
  if command -v journalctl >/dev/null 2>&1; then
    log "last log lines of the version that failed:"
    journalctl -u "$SERVICE" -n 40 --no-pager 2>/dev/null | grep -v "MQTT message received" | tail -12 | sed 's/^/[update]   /' || true
  fi
  eval "$STOP_CMD" || true
  local code="$APP_DIR/code-backups/code-$1.tgz" db="$APP_DIR/db-backups/bridge-$1.db"
  if [ -f "$code" ]; then
    rm -rf "$APP_DIR/templates" "$APP_DIR/static"
    tar -xzf "$code" -C "$APP_DIR"
  fi
  if [ "$2" != "none" ]; then git -C "$APP_DIR" reset -q --hard "$2" || true
  else rm -rf "$APP_DIR/.git"; fi            # first-time install: re-bootstrap on the next run
  if [ -f "$db" ]; then
    cp -f "$db" "$APP_DIR/bridge.db"
    rm -f "$APP_DIR/bridge.db-wal" "$APP_DIR/bridge.db-shm"
    log "database restored from $db"
  fi
  eval "$RESTART_CMD" || true
  if wait_healthy; then log "rolled back; the previous version is running again."
  else log "WARNING: rolled back but the app is not answering yet -- check: journalctl -u $SERVICE -n 50"; fi
}

[ -d "$APP_DIR" ] || die "$APP_DIR does not exist"
cd "$APP_DIR"
exec 9>"$APP_DIR/.update.lock"
flock -n 9 || die "another update is already running"

if [ "${1:-}" = "--rollback" ]; then
  [ -f .last_update ] || die "nothing to roll back (no .last_update file)"
  read -r STAMP OLD _ < .last_update
  log "This restores the code AND database from $STAMP. Readings stored since then are lost from bridge.db"
  log "(they remain in backup/*.csv)."
  if [ "${YES:-}" != "1" ]; then read -r -p "Type yes to continue: " ans; [ "$ans" = "yes" ] || die "aborted"; fi
  do_rollback "$STAMP" "$OLD" "manual rollback"
  exit 0
fi

command -v git >/dev/null 2>&1 || die "git is not installed on this gateway (see docs/DEPLOY.md)"
command -v python3 >/dev/null 2>&1 || die "python3 not found"
command -v tar >/dev/null 2>&1 || die "tar not found"

BRANCH="${1:-}"
[ -n "$BRANCH" ] || { [ -f .deploy_branch ] && BRANCH="$(cat .deploy_branch)"; } || true
BRANCH="${BRANCH:-main}"

BOOTSTRAP=0
if [ ! -d .git ]; then
  log "first run: turning $APP_DIR into a git checkout of the repository (data files are left alone)"
  git init -q
  BOOTSTRAP=1
fi
if [ -n "${REPO_URL:-}" ]; then
  git remote remove origin 2>/dev/null || true; git remote add origin "$REPO_URL"
elif ! git remote get-url origin >/dev/null 2>&1; then
  git remote add origin "$DEFAULT_REPO_URL"
fi

log "fetching branch '$BRANCH' from $(git remote get-url origin)"
n=0
until git fetch -q --prune origin "$BRANCH"; do
  n=$((n + 1)); [ "$n" -lt 3 ] || die "could not fetch '$BRANCH' (network? wrong branch name? repository access?)"
  log "fetch failed, retrying in $((n * 5))s"; sleep $((n * 5))
done
NEW="$(git rev-parse FETCH_HEAD)"
OLD="$(git rev-parse -q --verify HEAD 2>/dev/null || echo none)"

if [ "$OLD" = "$NEW" ] && [ "$BOOTSTRAP" = 0 ] && [ "${FORCE:-}" != "1" ]; then
  log "already up to date (version $(app_version), commit ${NEW:0:8})"
  exit 0
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p db-backups code-backups
if [ -f bridge.db ]; then
  log "backing up database -> db-backups/bridge-$STAMP.db"
  lowprio python3 - "$APP_DIR/bridge.db" "db-backups/bridge-$STAMP.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
fi
if [ "$BOOTSTRAP" = 0 ] && [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  log "note: files on this gateway were edited by hand; they will be replaced (a copy is in code-backups/)"
fi
files=""; for f in app.py requirements.txt modbus-bridge.service static templates deploy; do [ -e "$f" ] && files="$files $f"; done
[ -z "$files" ] || tar -czf "code-backups/code-$STAMP.tgz" $files
echo "$STAMP $OLD $BRANCH" > .last_update

REQ_BEFORE="$(sha256sum requirements.txt 2>/dev/null | cut -d' ' -f1 || true)"
git checkout -q -f -B "$BRANCH" "$NEW"
echo "$BRANCH" > .deploy_branch
REQ_AFTER="$(sha256sum requirements.txt | cut -d' ' -f1)"

if [ "$REQ_BEFORE" != "$REQ_AFTER" ]; then
  log "requirements changed -> installing"
  if ! "$APP_DIR/venv/bin/pip" install -q -r requirements.txt; then
    do_rollback "$STAMP" "$OLD" "pip install failed"; exit 1
  fi
fi

PYCHECK="$VENV_PY"; [ -x "$PYCHECK" ] || PYCHECK="python3"
if ! "$PYCHECK" -m py_compile app.py; then
  do_rollback "$STAMP" "$OLD" "app.py does not compile"; exit 1
fi

if [ "$APP_DIR" = "/opt/modbus_bridge" ] && command -v systemctl >/dev/null 2>&1 \
   && [ -d /etc/systemd/system ] && ! cmp -s modbus-bridge.service /etc/systemd/system/modbus-bridge.service; then
  log "updating systemd unit"
  cp modbus-bridge.service /etc/systemd/system/modbus-bridge.service && systemctl daemon-reload
fi

log "restarting $SERVICE"
eval "$RESTART_CMD" || { do_rollback "$STAMP" "$OLD" "restart failed"; exit 1; }
if ! wait_healthy; then
  do_rollback "$STAMP" "$OLD" "the app did not answer within ${HEALTH_TIMEOUT}s"; exit 1
fi

ls -1t db-backups/bridge-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
ls -1t code-backups/code-*.tgz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
log "OK: version $(app_version), commit ${NEW:0:8} (was ${OLD:0:8})"
