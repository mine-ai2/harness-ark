#!/usr/bin/env bash
# One-time, idempotent host setup for the Ark harness (see deploy/README.md).
#
# What it does: service user, venv + deps at /opt/harness-ark/.venv, data
# volume permissions, systemd unit install + enable.
# What it does NOT do: create/mount the data volume (#469), render
# config.json from the template (render-config.sh / deploy-remote.sh), or
# start the service.
set -Eeuo pipefail

APP_DIR=/opt/harness-ark
DATA_DIR=/mnt/harness-data
SERVICE_USER=ark
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

die() {
    echo "install.sh: error: $*" >&2
    exit 1
}

warn() {
    echo "install.sh: warning: $*" >&2
}

# --- Preflight ---------------------------------------------------------------

[[ $EUID -eq 0 ]] || die "must run as root (sudo)"
[[ -f $APP_DIR/requirements.txt ]] \
    || die "no $APP_DIR/requirements.txt — clone the repo to $APP_DIR first"
command -v python3 >/dev/null 2>&1 || die "python3 not found"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || die "python3 >= 3.10 required, found $(python3 --version)"
command -v systemctl >/dev/null 2>&1 || die "systemd not found"

# Stock Ubuntu cloud images ship python3 without the venv/ensurepip module.
if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
    echo "installing python3-venv..."
    apt-get update -qq
    apt-get install -y -qq python3-venv
fi

# Deploys render config.json with envsubst (render-config.sh); guarantee it.
if ! command -v envsubst >/dev/null 2>&1; then
    echo "installing gettext-base (envsubst)..."
    apt-get update -qq
    apt-get install -y -qq gettext-base
fi

# --- Service user ------------------------------------------------------------

# Real shell + home directory: agents run subprocesses as this user and tools
# expect $HOME to exist (caches, dotfiles).
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "/home/$SERVICE_USER" \
        --shell /bin/bash "$SERVICE_USER"
    echo "created service user $SERVICE_USER"
fi

# --- Venv + dependencies -----------------------------------------------------

# Code and venv stay root-owned: deploys replace them as root, and the agent
# (running as $SERVICE_USER) cannot self-modify the server code.
if [[ ! -x $APP_DIR/.venv/bin/python ]]; then
    python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# --- Data volume permissions -------------------------------------------------

# Only permissions here; creating and mounting the volume is provisioning's
# job. Non-recursive on purpose: serve-time bootstrap creates subdirectories
# as $SERVICE_USER.
if mountpoint -q "$DATA_DIR"; then
    chown "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
    chmod 0750 "$DATA_DIR"
else
    warn "$DATA_DIR is not a mounted volume — ark.service will refuse to start until it is"
fi

# --- Systemd unit ------------------------------------------------------------

install -m 0644 "$SCRIPT_DIR/systemd/ark.service" /etc/systemd/system/ark.service
systemctl daemon-reload
systemctl enable ark

# --- Next steps --------------------------------------------------------------

cat <<EOF
install.sh: done. Next steps:
  1. Render the config template (export the variables documented in
     deploy/README.md, then):
       $APP_DIR/deploy/render-config.sh > /tmp/config.json
       install -o $SERVICE_USER -g $SERVICE_USER -m 0600 /tmp/config.json $DATA_DIR/config.json
       rm /tmp/config.json
  2. systemctl start ark
  3. curl -fsS http://127.0.0.1:7777/health
EOF
