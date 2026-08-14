#!/usr/bin/env bash
# Render deploy/cloud-init/user-data.tmpl.yml to stdout with envsubst.
#
# Single source of truth for the template's variable allowlist (same
# pattern as deploy/render-config.sh). The explicit list is load-bearing:
# the template is full of shell $vars in runcmd that must pass through
# rendering untouched, and the is-set check keeps a missing value from
# silently rendering as "".
#
# Usage (all variables exported by the caller):
#   render-user-data.sh [template-path] > user-data.yml
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

REQUIRED_VARS=(
    DOMAIN
    VOLUME_NAME
    DEPLOY_PUBKEY
)

for var in "${REQUIRED_VARS[@]}"; do
    [[ -n ${!var:-} ]] || {
        echo "render-user-data.sh: error: $var is not set" >&2
        exit 1
    }
done

envsubst "$(printf '$%s ' "${REQUIRED_VARS[@]}")" \
    < "${1:-$SCRIPT_DIR/../cloud-init/user-data.tmpl.yml}"
