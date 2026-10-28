#!/usr/bin/env bash
# Tests for deploy/scripts/provision.sh (mine-capstone#1085). Run by
# talos-test and locally: deploy/tests/test-provision.sh
#
# Every external tool provision.sh (or its printed pairing block) could call
# is stubbed first on PATH and logs each invocation, so:
#   - a dry run that reaches doctl/gh/ssh fails here (the log must stay empty);
#   - hostile input that slips past validation fails here (every case runs
#     with --dry-run, so acceptance means exit 0 and is caught);
#   - the printed pairing block is executed against the stubs to prove it sets
#     nothing, and generates nothing, unless both GitHub environments exist.
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
PROVISION="$ROOT/deploy/scripts/provision.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin" "$WORK/home"
export STUB_LOG="$WORK/calls.log"
: >"$STUB_LOG"

# Fail-closed stubs: log, then exit 1.
for tool in doctl ssh ssh-keygen ssh-keyscan envsubst curl; do
    cat >"$WORK/bin/$tool" <<'EOF'
#!/usr/bin/env bash
printf '%s %s\n' "$(basename "$0")" "$*" >>"$STUB_LOG"
exit 1
EOF
done
# gh: exits STUB_GH_EXIT (default 1), or 1 when its argv contains
# STUB_GH_FAIL. Drains stdin for a `secret set` without --body, as gh does
# (the value is never logged).
cat >"$WORK/bin/gh" <<'EOF'
#!/usr/bin/env bash
printf 'gh %s\n' "$*" >>"$STUB_LOG"
if [[ $* == *"secret set"* && $* != *"--body"* ]]; then cat >/dev/null; fi
if [[ -n ${STUB_GH_FAIL:-} && $* == *"$STUB_GH_FAIL"* ]]; then exit 1; fi
exit "${STUB_GH_EXIT:-1}"
EOF
cat >"$WORK/bin/openssl" <<'EOF'
#!/usr/bin/env bash
printf 'openssl %s\n' "$*" >>"$STUB_LOG"
echo 00000000000000000000000000000000
EOF
chmod +x "$WORK/bin/"*

failures=0
fail() {
    echo "FAIL: $*" >&2
    failures=$((failures + 1))
}

# Hermetic run: stubs first on PATH, throwaway HOME, no inherited overrides.
run() {
    env -u APP_HOST -u DNS_ZONE -u PROVISION_DRY_RUN \
        PATH="$WORK/bin:$PATH" HOME="$WORK/home" "$@" 2>&1
}

no_calls() {
    if [[ -s $STUB_LOG ]]; then
        fail "$1: external tools were called: $(tr '\n' ';' <"$STUB_LOG")"
        : >"$STUB_LOG"
    fi
}

out=""
expect_ok() {
    local desc="$1" rc=0
    shift
    out="$(run "$@")" || rc=$?
    ((rc == 0)) || fail "$desc: exit $rc: $out"
    no_calls "$desc"
}

expect_output() {
    local desc="$1" needle
    shift
    for needle in "$@"; do
        grep -qF -- "$needle" <<<"$out" || fail "$desc: output lacks '$needle'"
    done
}

expect_refused() {
    local rc=0
    out="$(run "$@")" || rc=$?
    if ((rc == 0)); then
        fail "accepted: $*"
    elif ! grep -q '^provision.sh: error: ' <<<"$out"; then
        fail "refused without a validation error: $*: $out"
    fi
    no_calls "refused: $*"
}

# --- 0. The harness itself sees external calls --------------------------------
# Without --dry-run, provision.sh must reach `doctl account get` (stubbed to
# fail). If this ever logs nothing, the "no calls" checks below are vacuous.
rc=0
out="$(run "$PROVISION" acme)" || rc=$?
((rc != 0)) || fail "non-dry run against stubs exited 0"
grep -q '^doctl account get' "$STUB_LOG" || fail "stub log did not record doctl (PATH stubbing broken?)"
: >"$STUB_LOG"

# --- 1. Help and the three forms ----------------------------------------------
expect_ok "--help" "$PROVISION" --help
expect_output "--help" "provision.sh dev" "provision.sh prod" "provision.sh <customer>"

expect_ok "acme --dry-run" "$PROVISION" acme --dry-run
expect_output "acme --dry-run" \
    "GitHub environment  production-acme" "droplet             harness-acme " \
    "volume              harness-acme-data" "firewall            harness-acme-fw" \
    "DNS                 harness-acme.mine.ai" "/.ssh/harness-acme-deploy" \
    "mine-capstone environment prod-acme" "OPENROUTER_API_KEY" \
    "HARNESS_ARK_SECRET" "HARNESS_GATEWAY_SECRET" 'HARNESS_ARK_URL="https://harness-acme.mine.ai"' \
    "dry run: nothing created"
acme_out="$out"

expect_ok "PROVISION_DRY_RUN=1 acme" PROVISION_DRY_RUN=1 "$PROVISION" acme
expect_output "PROVISION_DRY_RUN=1 acme" "GitHub environment  production-acme" "dry run: nothing created"

expect_ok "dev --dry-run" "$PROVISION" dev --dry-run
expect_output "dev --dry-run" "GitHub environment  development " "droplet             harness-dev " \
    "environments/development/deployment-branch-policies"
grep -q 'reviewers' <<<"$out" && fail "dev --dry-run: development must not get a required reviewer"

expect_ok "prod --dry-run" "$PROVISION" prod --dry-run
expect_output "prod --dry-run" "GitHub environment  production " "droplet             harness-prod " \
    '"reviewers"'

expect_ok "51-char slug" "$PROVISION" "$(printf 'a%.0s' {1..51})" --dry-run
expect_ok "acme --gh-env customer-acme" "$PROVISION" acme --gh-env customer-acme --dry-run
expect_output "acme --gh-env customer-acme" "GitHub environment  customer-acme"
expect_ok "acme --gh-env=production-acme (= derived)" "$PROVISION" acme --gh-env=production-acme --dry-run
expect_ok "prod --gh-env production (= derived)" "$PROVISION" prod --gh-env production --dry-run
expect_ok "APP_HOST override" APP_HOST=ops.acme.example "$PROVISION" acme --dry-run
expect_output "APP_HOST override" 'https://ops.acme.example'

# --- 2. Hostile and mistaken input is refused ---------------------------------
expect_refused "$PROVISION" --dry-run
expect_refused "$PROVISION" ../x --dry-run
expect_refused "$PROVISION" ACME --dry-run
expect_refused "$PROVISION" Acme --dry-run
expect_refused "$PROVISION" acme- --dry-run
expect_refused "$PROVISION" -acme --dry-run
expect_refused "$PROVISION" ac_me --dry-run
expect_refused "$PROVISION" 'acme;id' --dry-run
expect_refused "$PROVISION" "$(printf 'a%.0s' {1..52})" --dry-run
expect_refused "$PROVISION" production --dry-run
expect_refused "$PROVISION" production-acme --dry-run
expect_refused "$PROVISION" development --dry-run
expect_refused "$PROVISION" development-acme --dry-run
expect_refused "$PROVISION" prod-acme --dry-run
expect_refused "$PROVISION" harness --dry-run
expect_refused "$PROVISION" harness-acme --dry-run
expect_refused "$PROVISION" acme beta --dry-run
expect_refused "$PROVISION" acme --bogus --dry-run
expect_refused "$PROVISION" acme --gh-env production --dry-run
expect_refused "$PROVISION" acme --gh-env production-other --dry-run
expect_refused "$PROVISION" acme --gh-env=development --dry-run
expect_refused "$PROVISION" beta --gh-env production-acme --dry-run
expect_refused "$PROVISION" prod --gh-env production-acme --dry-run
expect_refused "$PROVISION" dev --gh-env production --dry-run
expect_refused "$PROVISION" acme --gh-env=-x --dry-run
expect_refused "$PROVISION" acme --gh-env=.. --dry-run
expect_refused "$PROVISION" acme --gh-env 'bad name' --dry-run
expect_refused "$PROVISION" acme --gh-env= --dry-run
expect_refused "$PROVISION" acme --dry-run --gh-env
expect_refused APP_HOST='acme.mine.ai"; id; "' "$PROVISION" acme --dry-run
# shellcheck disable=SC2016 # a literal $(...) must be refused, not expanded
expect_refused APP_HOST='$(id).mine.ai' "$PROVISION" acme --dry-run
expect_refused APP_HOST='https://acme.mine.ai' "$PROVISION" acme --dry-run
expect_refused APP_HOST='acme' "$PROVISION" acme --dry-run
expect_refused DNS_ZONE='mine.ai;id' "$PROVISION" acme --dry-run
expect_refused PROVISION_DRY_RUN=true "$PROVISION" acme --dry-run

# --- 3. The printed pairing block never pairs one-sided -----------------------
block="$(sed -n '/^  if gh api repos\/mine-ai2\/mine-capstone\/environments\//,/^  fi$/p' <<<"$acme_out")"
[[ -n $block ]] || fail "pairing block not found in acme --dry-run output"
check_line="$(grep -n 'environments/prod-acme --silent' <<<"$acme_out" | head -1 | cut -d: -f1)"
rand_line="$(grep -n 'openssl rand' <<<"$acme_out" | head -1 | cut -d: -f1)"
if [[ -z $check_line || -z $rand_line ]] || ((check_line >= rand_line)); then
    fail "pairing block generates secrets before checking the environments"
fi

pair() { # pair <STUB_GH_FAIL pattern or ''>: runs the block, sets $out
    # shellcheck disable=SC2016 # the appended check expands inside bash -c
    out="$(env PATH="$WORK/bin:$PATH" STUB_GH_EXIT=0 STUB_GH_FAIL="$1" \
        bash -c "$block"$'\n''[[ -z ${ARK+x} && -z ${GW+x} ]] || echo LEAKED' 2>&1 </dev/null)" || true
}
sets() { grep -c ' secret set ' "$STUB_LOG" || true; }

for missing in mine-capstone/environments/prod-acme harness-ark/environments/production-acme; do
    : >"$STUB_LOG"
    pair "$missing"
    grep -q 'nothing set' <<<"$out" || fail "pairing with $missing missing: no 'nothing set' message: $out"
    [[ $(sets) == 0 ]] || fail "pairing with $missing missing: secrets were set"
    grep -q '^openssl' "$STUB_LOG" && fail "pairing with $missing missing: secrets were generated"
done

: >"$STUB_LOG"
pair ""
grep -q '^paired: harness-ark production-acme <-> mine-capstone prod-acme' <<<"$out" \
    || fail "pairing with both environments present did not report success: $out"
[[ $(sets) == 5 ]] || fail "pairing set $(sets) secrets, expected 5"
[[ $(grep -c 'secret set .*--repo mine-ai2/mine-capstone --env prod-acme' "$STUB_LOG") == 2 ]] \
    || fail "pairing: expected 2 mine-capstone prod-acme secrets"
grep -q LEAKED <<<"$out" && fail "pairing block left ARK/GW set"

: >"$STUB_LOG"
pair "secret set MINEAI_GATEWAY_SECRET"
grep -q 'PAIRING INCOMPLETE' <<<"$out" || fail "mid-block failure not reported: $out"
grep -q 'secret set MINEAI_GATEWAY_URL' "$STUB_LOG" && fail "pairing continued after a failed secret set"
grep -q LEAKED <<<"$out" && fail "pairing block left ARK/GW set after a failure"
: >"$STUB_LOG"

if ((failures)); then
    echo "test-provision.sh: $failures failure(s)" >&2
    exit 1
fi
echo "test-provision.sh: all checks passed"
