#!/usr/bin/env bash
# Render config/config.json.tmpl to stdout with envsubst.
#
# Single source of truth for the template's variable allowlist: add new
# template variables to REQUIRED_VARS here and nowhere else. The explicit
# list keeps envsubst from touching any other $-looking content, and the
# is-set check keeps a missing secret from silently rendering as "".
#
# Usage (all variables exported by the caller):
#   render-config.sh [template-path] > config.json
set -Eeuo pipefail

REQUIRED_VARS=(
    ARK_HOST
    ARK_PORT
    ARK_AUTH_SECRET
    ANTHROPIC_API_KEY
    MINEAI_GATEWAY_URL
    MINEAI_GATEWAY_SECRET
)

for var in "${REQUIRED_VARS[@]}"; do
    [[ -n ${!var:-} ]] || {
        echo "render-config.sh: error: $var is not set" >&2
        exit 1
    }
done

envsubst "$(printf '$%s ' "${REQUIRED_VARS[@]}")" \
    < "${1:-/opt/harness-ark/deploy/config/config.json.tmpl}"
