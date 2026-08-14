#!/usr/bin/env bash
# Remote side of the automated deploy (mine-capstone#470). Executed as root
# over SSH by .github/workflows/talos-deploy.yml AFTER the workflow has
# rsynced the repo to /opt/harness-ark:
#
#   ssh deploy@host 'sudo /opt/harness-ark/deploy/deploy-remote.sh' < deploy.env
#
# stdin is KEY=VALUE lines — secrets travel on stdin so they never appear in
# argv (visible in `ps`) or in a file outside root-owned temp space. Blank
# lines and #-comments are ignored; values must not contain newlines.
# Required keys: ARK_AUTH_SECRET, ANTHROPIC_API_KEY, MINEAI_GATEWAY_URL,
# MINEAI_GATEWAY_SECRET. Optional: ARK_HOST, ARK_PORT.
#
# Never add `set -x` here: it would leak secret values into the workflow log.
set -Eeuo pipefail

APP_DIR=/opt/harness-ark
DATA_DIR=/mnt/harness-data
SERVICE_USER=ark
HEALTH_TIMEOUT_SECONDS=30

die() {
    echo "deploy-remote.sh: error: $*" >&2
    exit 1
}

# --- Preflight ---------------------------------------------------------------

[[ $EUID -eq 0 ]] || die "must run as root (sudo)"
[[ -f $APP_DIR/requirements.txt ]] \
    || die "no $APP_DIR/requirements.txt — rsync the repo to $APP_DIR first"
# The unit asserts this too, but failing here gives a clear message instead
# of a failed restart at phase 4.
mountpoint -q "$DATA_DIR" \
    || die "$DATA_DIR is not a mounted volume (provisioning: mine-capstone#469)"

# --- Phase 0: ingest secrets from stdin --------------------------------------

# No source/eval: values containing quotes, spaces, or shell metacharacters
# stay inert.
umask 077
envfile=$(mktemp)
rendered=$(mktemp)
trap 'rm -f "$envfile" "$rendered"' EXIT
cat > "$envfile"
while IFS= read -r line; do
    [[ -z $line || $line == \#* ]] && continue
    [[ $line == *=* ]] || die "malformed stdin line (expected KEY=VALUE)"
    export "${line%%=*}=${line#*=}"
done < "$envfile"
: "${ARK_HOST:=127.0.0.1}"
: "${ARK_PORT:=7777}"

# --- Phase 1: install (venv + deps, unit refresh, permissions) ---------------

"$APP_DIR/deploy/install.sh"

# --- Phase 2: render config --------------------------------------------------

# Validate before touching the live file: a template or secret mistake must
# fail the deploy while the running service keeps its old config.
"$APP_DIR/deploy/render-config.sh" "$APP_DIR/deploy/config/config.json.tmpl" \
    > "$rendered"
python3 -m json.tool "$rendered" > /dev/null \
    || die "rendered config is not valid JSON"
# shellcheck disable=SC2016  # literal ${ on purpose: unrendered placeholders
! grep -q '\${' "$rendered" \
    || die 'rendered config still contains ${...} placeholders'
install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0600 \
    "$rendered" "$DATA_DIR/config.json"

# --- Phase 3: sync checked-in agent personas + skills ------------------------

# No --delete: $DATA_DIR/agents also holds live state (workspace/, uploads)
# that must survive deploys. Cost: a skill removed from the repo lingers on
# the host until removed manually.
rsync -rlp --chown="$SERVICE_USER:$SERVICE_USER" \
    "$APP_DIR/deploy/agents/" "$DATA_DIR/agents/"

# --- Phase 4: restart --------------------------------------------------------

systemctl restart ark

# --- Phase 5: health gate ----------------------------------------------------

for ((i = 0; i < HEALTH_TIMEOUT_SECONDS; i++)); do
    # Restart=on-failure + start-rate limiting land a bad start in `failed`
    # state — bail immediately instead of waiting out the timeout.
    systemctl is-active --quiet ark || break
    if curl -fsS --max-time 2 "http://$ARK_HOST:$ARK_PORT/health" > /dev/null; then
        echo "deploy-remote.sh: deploy OK — /health is green"
        exit 0
    fi
    sleep 1
done

echo "deploy-remote.sh: error: health gate failed" >&2
systemctl status ark --no-pager >&2 || true
journalctl -u ark -n 50 --no-pager >&2 || true
exit 1
