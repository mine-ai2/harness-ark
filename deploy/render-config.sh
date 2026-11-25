#!/usr/bin/env bash
# Render config/config.json.tmpl to stdout with envsubst.
#
# Single source of truth for the template's variable allowlist: add new
# template variables to REQUIRED_VARS (or OPTIONAL_VARS) here and nowhere
# else. The explicit list keeps envsubst from touching any other $-looking
# content, and the is-set check keeps a missing required secret from
# silently rendering as "".
#
# OPTIONAL_VARS render as "" when unset: the feature they key stays off and
# the deploy stays green. Today that is web search (mine-capstone#1422) —
# set BRAVE_SEARCH_API_KEY or TAVILY_API_KEY and `search_web` works; set
# neither and the tool keeps answering "requires ... api_key".
#
# Usage (all variables exported by the caller):
#   render-config.sh [template-path] > config.json
set -Eeuo pipefail

REQUIRED_VARS=(
    ARK_HOST
    ARK_PORT
    ARK_AUTH_SECRET
    OPENAI_API_KEY
    OPENROUTER_API_KEY
    MINEAI_GATEWAY_URL
    MINEAI_GATEWAY_SECRET
)

for var in "${REQUIRED_VARS[@]}"; do
    [[ -n ${!var:-} ]] || {
        echo "render-config.sh: error: $var is not set" >&2
        exit 1
    }
done

OPTIONAL_VARS=(
    BRAVE_SEARCH_API_KEY
    TAVILY_API_KEY
    SEARCH_WEB_PROVIDER
)

for var in "${OPTIONAL_VARS[@]}"; do
    export "$var=${!var:-}"
done
# The provider follows the key that is present: Tavily only when it is the
# sole key, otherwise Brave (the tool's own default). An explicit
# SEARCH_WEB_PROVIDER wins.
if [[ -z $SEARCH_WEB_PROVIDER ]]; then
    if [[ -n $TAVILY_API_KEY && -z $BRAVE_SEARCH_API_KEY ]]; then
        SEARCH_WEB_PROVIDER=tavily
    else
        SEARCH_WEB_PROVIDER=brave
    fi
fi

envsubst "$(printf '$%s ' "${REQUIRED_VARS[@]}" "${OPTIONAL_VARS[@]}")" \
    < "${1:-/opt/harness-ark/deploy/config/config.json.tmpl}"
