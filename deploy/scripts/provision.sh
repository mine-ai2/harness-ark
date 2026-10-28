#!/usr/bin/env bash
# Provision one harness environment on DigitalOcean (mine-capstone#469,
# #1085).
#
# Runs on an operator machine with authenticated `doctl` (flat deploy/*.sh
# scripts run ON the VM; deploy/scripts/*.sh run from outside). Creates,
# per environment: tag, cloud firewall (22+443 only), deploy SSH keypair,
# block-storage volume, droplet (cloud-init from
# deploy/cloud-init/user-data.tmpl.yml), and a DNS A record. No two
# environments share anything: dev, prod, and one production harness per
# customer (Ark session/project ids only mean something on the harness that
# created them, so a harness is never re-pointed at another app).
#
# Idempotent converge: safe to re-run after a partial failure — existing
# resources are verified and kept, missing ones created. The droplet's
# cloud-init only runs on FIRST boot; to reapply it, delete the droplet and
# re-run (the data volume survives: mkfs is guarded on the volume being
# blank).
#
# Usage: provision.sh <dev|prod|<customer>> [--gh-env NAME] [--dry-run]
#        (provision.sh --help for the details)
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Overridable via environment.
DO_REGION="${DO_REGION:-nyc3}"
DO_VPC_UUID="${DO_VPC_UUID:-1f34ca52-40ac-48e2-9daf-0870e0599847}" # mineai-internal
DROPLET_SIZE="${DROPLET_SIZE:-s-2vcpu-4gb}"
DROPLET_IMAGE="${DROPLET_IMAGE:-ubuntu-24-04-x64}"
VOLUME_SIZE="${VOLUME_SIZE:-10GiB}"
DNS_ZONE="${DNS_ZONE:-mine.ai}"
DNS_TTL="${DNS_TTL:-300}"
# DO account SSH key IDs installed for root as break-glass ("Main", "milo-agent").
BREAK_GLASS_KEYS="${BREAK_GLASS_KEYS:-53862425,55334748}"

die() {
    echo "provision.sh: error: $*" >&2
    exit 1
}

note() {
    echo "provision.sh: $*"
}

usage() {
    cat <<'EOF'
Usage:
  provision.sh dev          harness-dev        -> GitHub environment development
  provision.sh prod         harness-prod       -> GitHub environment production
  provision.sh <customer>   harness-<customer> -> GitHub environment production-<customer>

<customer> is the customer slug: lowercase a-z, 0-9 and inner hyphens, at
most 51 characters (e.g. `provision.sh acme` -> harness-acme,
production-acme). development, production, harness (each also as "-..."),
and prod-... are refused: they are environment/droplet names, not slugs.

Options:
  --gh-env NAME   use NAME as the GitHub environment instead of the derived
                  one (same charset as a slug; development[-...] and
                  production[-...] only when equal to the derived name)
  --dry-run       print every derived name and the setup checklist, then exit
                  before the first doctl call (same as PROVISION_DRY_RUN=1)
  -h, --help      show this help

Environment overrides: DO_REGION, DO_VPC_UUID, DROPLET_SIZE, DROPLET_IMAGE,
VOLUME_SIZE, DNS_ZONE, DNS_TTL, BREAK_GLASS_KEYS, and (customers only)
APP_HOST, the paired app's host (default <customer>.<DNS_ZONE>).
EOF
}

# --- Arguments and derived names ---------------------------------------------

ENV_NAME=""
GH_ENV_OVERRIDE=""
case "${PROVISION_DRY_RUN:-0}" in
0) DRY_RUN=0 ;;
1) DRY_RUN=1 ;;
*) die "PROVISION_DRY_RUN must be 0 or 1" ;;
esac
while (($#)); do
    case "$1" in
    -h | --help)
        usage
        exit 0
        ;;
    --dry-run) DRY_RUN=1 ;;
    --gh-env)
        [[ $# -ge 2 && -n $2 && $2 != -* ]] || die "--gh-env needs a NAME"
        GH_ENV_OVERRIDE="$2"
        shift
        ;;
    --gh-env=*)
        GH_ENV_OVERRIDE="${1#*=}"
        [[ -n $GH_ENV_OVERRIDE ]] || die "--gh-env needs a NAME"
        ;;
    -*)
        usage >&2
        die "unknown option: $1"
        ;;
    *)
        [[ -z $ENV_NAME ]] || die "one environment per run (got '$ENV_NAME' and '$1')"
        ENV_NAME="$1"
        ;;
    esac
    shift
done

IS_CUSTOMER=0
case "$ENV_NAME" in
"")
    usage >&2
    die "missing environment: dev, prod, or a customer slug"
    ;;
dev) GH_ENV=development ;;
prod) GH_ENV=production ;;
# Lookalikes: an operator pasting a GitHub environment (harness-ark
# production-<c>, mine-capstone prod-<c>) or droplet name instead of the
# slug would otherwise get production-production-acme / harness-prod-acme /
# harness-harness-acme, or a second "production" harness
# (harness-production) next to harness-prod that nothing deploys to.
development | development-* | production | production-* | prod-* | harness | harness-*)
    die "'$ENV_NAME' is an environment or droplet name, not a slug: use dev, prod, or the bare customer slug (provision.sh acme)"
    ;;
*)
    # ^[a-z0-9-]+$ without a leading/trailing hyphen: the slug becomes a DNS
    # label (harness-<c>.mine.ai) and DO resource names. 51 = the 64-char
    # cap on DO volume names minus "harness-" and "-data".
    [[ $ENV_NAME =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] \
        || die "invalid customer slug '$ENV_NAME': lowercase a-z, 0-9 and inner hyphens only"
    ((${#ENV_NAME} <= 51)) || die "customer slug '$ENV_NAME' is longer than 51 characters"
    GH_ENV="production-$ENV_NAME"
    IS_CUSTOMER=1
    ;;
esac

if [[ -n $GH_ENV_OVERRIDE ]]; then
    # Same charset as slugs: it lands in API paths and pasteable commands.
    [[ $GH_ENV_OVERRIDE =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] \
        || die "invalid --gh-env '$GH_ENV_OVERRIDE': lowercase a-z, 0-9 and inner hyphens only"
    # The printed setup commands overwrite the environment's SSH key and
    # DEPLOY_HOST: aiming this droplet at development, production or another
    # customer's production-<c> would re-point that environment's deploys.
    # Those names are only ever reachable as the derived name itself.
    if [[ $GH_ENV_OVERRIDE =~ ^(development|production)(-.*)?$ && $GH_ENV_OVERRIDE != "$GH_ENV" ]]; then
        die "--gh-env $GH_ENV_OVERRIDE is reserved: development/production[-*] can only be the derived name ($GH_ENV here)"
    fi
    GH_ENV="$GH_ENV_OVERRIDE"
fi

# Hostnames land in DNS records and pasteable commands: lowercase labels and
# dots only (no quotes, spaces or $), like mine-capstone's render-app-spec.sh.
HOST_RE='^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'
[[ $DNS_ZONE =~ $HOST_RE ]] || die "invalid DNS_ZONE '$DNS_ZONE': expected a lowercase domain like mine.ai"

DROPLET="harness-$ENV_NAME"
VOLUME="$DROPLET-data"
FIREWALL="$DROPLET-fw"
FQDN="$DROPLET.$DNS_ZONE"
TAG_ENV="$DROPLET"
KEYFILE="$HOME/.ssh/$DROPLET-deploy"
# Customers pair with their own mine-capstone app (GitHub environment
# prod-<c> there). MINEAI_GATEWAY_URL / API_PUBLIC_BASE_URL are the app's
# host ROOT: Ark appends /api/agent-gateway/... itself.
APP_HOST="${APP_HOST:-$ENV_NAME.$DNS_ZONE}"
[[ $APP_HOST =~ $HOST_RE ]] \
    || die "invalid APP_HOST '$APP_HOST': a lowercase hostname without scheme or path (e.g. acme.mine.ai)"
APP_GH_ENV="prod-$ENV_NAME"

cat <<EOF
provision.sh: environment '$ENV_NAME'
  GitHub environment  $GH_ENV (mine-ai2/harness-ark)
  droplet             $DROPLET ($DROPLET_SIZE, $DROPLET_IMAGE, $DO_REGION)
  volume              $VOLUME ($VOLUME_SIZE)
  firewall            $FIREWALL (inbound tcp 22 + 443)
  tag                 $TAG_ENV (+ shared label harness)
  DNS                 $FQDN (A record in $DNS_ZONE, TTL $DNS_TTL)
  deploy keypair      $KEYFILE
EOF
if ((IS_CUSTOMER)); then
    cat <<EOF
  paired app          https://$APP_HOST (mine-capstone environment $APP_GH_ENV)
EOF
fi

# Everything after provisioning: GitHub environment wiring, the
# operator-supplied secrets, and (customers) the app-side pairing. Reads the
# globals above plus IP and KNOWN_HOSTS. Prints names and commands only;
# never a secret value.
print_setup() {
    local ENV_BODY
    if [[ $ENV_NAME == dev ]]; then
        ENV_BODY='{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}'
    else
        # Production-class: every leg of the talos-deploy matrix waits for its
        # own environment's required reviewer.
        ENV_BODY='{"reviewers": [{"type": "User", "id": <reviewer-user-id>}],
 "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}'
    fi

    cat <<EOF

Configure GitHub environment $GH_ENV (protection rules first):

EOF
    if [[ $ENV_NAME != dev ]]; then
        echo "  # <reviewer-user-id> below: gh api users/<login> --jq .id"
    fi
    cat <<EOF
  # Skip the PUT when $GH_ENV already exists: a re-run would replace its
  # protection rules (reviewer list). Check: gh api repos/mine-ai2/harness-ark/environments/$GH_ENV --jq '[.protection_rules[].type]'
  gh api -X PUT repos/mine-ai2/harness-ark/environments/$GH_ENV --input - <<'JSON'
$ENV_BODY
JSON
  gh api -X POST repos/mine-ai2/harness-ark/environments/$GH_ENV/deployment-branch-policies -f name=main -f type=branch
  gh secret set SSH_PRIVATE_KEY   --repo mine-ai2/harness-ark --env $GH_ENV < $KEYFILE
  gh variable set DEPLOY_HOST     --repo mine-ai2/harness-ark --env $GH_ENV --body "$IP"
  gh variable set DEPLOY_USER     --repo mine-ai2/harness-ark --env $GH_ENV --body "deploy"
  gh variable set DEPLOY_DOMAIN   --repo mine-ai2/harness-ark --env $GH_ENV --body "$FQDN"
  gh variable set SSH_KNOWN_HOSTS --repo mine-ai2/harness-ark --env $GH_ENV --body "\$(cat <<'KNOWN'
$KNOWN_HOSTS
KNOWN
)"

Still needed from the operator (values are never printed here; gh prompts):
  gh secret set OPENAI_API_KEY        --repo mine-ai2/harness-ark --env $GH_ENV
  gh secret set OPENROUTER_API_KEY    --repo mine-ai2/harness-ark --env $GH_ENV   # required by render-config.sh
EOF
    if ((IS_CUSTOMER)); then
        cat <<EOF
  ARK_AUTH_SECRET, MINEAI_GATEWAY_SECRET, MINEAI_GATEWAY_URL: the pairing block below

Pair with the mine-capstone app. PRECONDITION: the mine-capstone GitHub
environment $APP_GH_ENV exists (create it first, with its required reviewer
and deployment branches, per the mine-capstone new-customer runbook). The
block checks both environments before generating anything, so it never sets
one side only; on any failure re-run the WHOLE block (it regenerates and
overwrites both sides). Values go over stdin, never argv:

  if gh api repos/mine-ai2/mine-capstone/environments/$APP_GH_ENV --silent &&
     gh api repos/mine-ai2/harness-ark/environments/$GH_ENV --silent; then
    ARK=\$(openssl rand -hex 32); GW=\$(openssl rand -hex 32)
    printf '%s' "\$ARK" | gh secret set HARNESS_ARK_SECRET     --repo mine-ai2/mine-capstone --env $APP_GH_ENV &&
    printf '%s' "\$ARK" | gh secret set ARK_AUTH_SECRET        --repo mine-ai2/harness-ark   --env $GH_ENV &&
    printf '%s' "\$GW"  | gh secret set HARNESS_GATEWAY_SECRET --repo mine-ai2/mine-capstone --env $APP_GH_ENV &&
    printf '%s' "\$GW"  | gh secret set MINEAI_GATEWAY_SECRET  --repo mine-ai2/harness-ark   --env $GH_ENV &&
    gh secret set MINEAI_GATEWAY_URL --repo mine-ai2/harness-ark --env $GH_ENV --body "https://$APP_HOST" &&
    echo "paired: harness-ark $GH_ENV <-> mine-capstone $APP_GH_ENV" ||
    echo "PAIRING INCOMPLETE: re-run this whole block" >&2
    unset ARK GW
  else
    echo "nothing set: create mine-capstone env $APP_GH_ENV and harness-ark env $GH_ENV first" >&2
  fi

  App side, not secret (mine-capstone .do/customers/$ENV_NAME.vars):
    HARNESS_ARK_URL="https://$FQDN"
  The app's spec template derives API_PUBLIC_BASE_URL=https://$APP_HOST (host
  root, no /api); it must equal MINEAI_GATEWAY_URL above.

Then add $GH_ENV to the deploy-prod matrix in
.github/workflows/talos-deploy.yml (PR to main). Once merged, every
talos-deploy run has a $GH_ENV leg that waits for its reviewer. Smoke:
  ARK_AUTH_SECRET=... deploy/scripts/smoke.sh $FQDN
EOF
    else
        cat <<EOF
  gh secret set ARK_AUTH_SECRET       --repo mine-ai2/harness-ark --env $GH_ENV   # openssl rand -hex 32, unique per env; = the app's HARNESS_ARK_SECRET
  gh secret set MINEAI_GATEWAY_URL    --repo mine-ai2/harness-ark --env $GH_ENV   # the app's host root https://<host> (no /api) = its API_PUBLIC_BASE_URL
  gh secret set MINEAI_GATEWAY_SECRET --repo mine-ai2/harness-ark --env $GH_ENV   # = the app's HARNESS_GATEWAY_SECRET

Then deploy (push to main or workflow_dispatch talos-deploy) and smoke:
  ARK_AUTH_SECRET=... deploy/scripts/smoke.sh $FQDN
EOF
    fi
}

if ((DRY_RUN)); then
    IP="<droplet-ip>"
    KNOWN_HOSTS="<ssh-keyscan -t ed25519 lines, captured after first boot>"
    print_setup
    note "dry run: nothing created, no doctl or gh call made"
    exit 0
fi

# --- Preflight ---------------------------------------------------------------

for cmd in doctl ssh-keygen ssh-keyscan envsubst; do
    command -v "$cmd" >/dev/null 2>&1 || die "$cmd not found"
done
doctl account get >/dev/null 2>&1 || die "doctl is not authenticated (doctl auth init)"
doctl compute domain get "$DNS_ZONE" >/dev/null 2>&1 \
    || die "DNS zone $DNS_ZONE is not managed in this DO account"

# --- Tags --------------------------------------------------------------------

for tag in harness "$TAG_ENV"; do
    doctl compute tag get "$tag" >/dev/null 2>&1 \
        || doctl compute tag create "$tag" >/dev/null
done

# --- Firewall (before the droplet, so it is covered from first boot) ---------

# 22: key-only SSH for deploys — GitHub-hosted runners have no stable
# egress CIDRs, so the port stays open to the world and sshd hardening is
# the control. 443: Caddy (TLS + ACME TLS-ALPN-01). Port 80 deliberately
# closed. Applied by tag so a rebuilt droplet is covered automatically.
if ! doctl compute firewall list --format Name --no-header | grep -qx "$FIREWALL"; then
    doctl compute firewall create \
        --name "$FIREWALL" \
        --tag-names "$TAG_ENV" \
        --inbound-rules "protocol:tcp,ports:22,address:0.0.0.0/0,address:::/0 protocol:tcp,ports:443,address:0.0.0.0/0,address:::/0" \
        --outbound-rules "protocol:icmp,address:0.0.0.0/0,address:::/0 protocol:tcp,ports:all,address:0.0.0.0/0,address:::/0 protocol:udp,ports:all,address:0.0.0.0/0,address:::/0" \
        >/dev/null
    note "created firewall $FIREWALL (inbound 22, 443)"
else
    note "firewall $FIREWALL exists — keeping"
fi

# --- Deploy SSH keypair ------------------------------------------------------

# No passphrase: the private half becomes the GH environment secret
# SSH_PRIVATE_KEY, consumed non-interactively by CI.
if [[ ! -f $KEYFILE ]]; then
    ssh-keygen -t ed25519 -N '' -C "$DROPLET-deploy" -f "$KEYFILE" >/dev/null
    note "generated deploy keypair $KEYFILE"
else
    note "deploy keypair $KEYFILE exists — reusing"
fi
DEPLOY_PUBKEY="$(cat "$KEYFILE.pub")"

# --- Volume ------------------------------------------------------------------

# No --fs-type on purpose: cloud-init's format-if-blank guard is the single
# formatting authority, which is what makes rebuilding a droplet against an
# existing data volume safe.
VOLUME_ID="$(doctl compute volume list --format ID,Name,Region --no-header \
    | awk -v n="$VOLUME" '$2 == n {print $1; r=$3} END {if (r && r != "'"$DO_REGION"'") exit 1}')" \
    || die "volume $VOLUME exists in the wrong region"
if [[ -z $VOLUME_ID ]]; then
    VOLUME_ID="$(doctl compute volume create "$VOLUME" \
        --region "$DO_REGION" --size "$VOLUME_SIZE" \
        --tag "$TAG_ENV" --format ID --no-header)"
    note "created volume $VOLUME ($VOLUME_SIZE)"
else
    note "volume $VOLUME exists — keeping (data preserved)"
fi

# --- Droplet -----------------------------------------------------------------

EXISTING_IP="$(doctl compute droplet list --format Name,PublicIPv4 --no-header \
    | awk -v n="$DROPLET" '$1 == n {print $2}')"
if [[ -z $EXISTING_IP ]]; then
    userdata="$(mktemp)"
    trap 'rm -f "$userdata"' EXIT
    DOMAIN="$FQDN" VOLUME_NAME="$VOLUME" DEPLOY_PUBKEY="$DEPLOY_PUBKEY" \
        "$SCRIPT_DIR/render-user-data.sh" > "$userdata"
    note "creating droplet $DROPLET ($DROPLET_SIZE, $DROPLET_IMAGE, $DO_REGION)..."
    doctl compute droplet create "$DROPLET" \
        --region "$DO_REGION" \
        --size "$DROPLET_SIZE" \
        --image "$DROPLET_IMAGE" \
        --vpc-uuid "$DO_VPC_UUID" \
        --ssh-keys "$BREAK_GLASS_KEYS" \
        --volumes "$VOLUME_ID" \
        --tag-names "harness,$TAG_ENV" \
        --user-data-file "$userdata" \
        --wait >/dev/null
    IP="$(doctl compute droplet get "$DROPLET" --format PublicIPv4 --no-header)"
    note "droplet $DROPLET created at $IP"
else
    IP="$EXISTING_IP"
    note "droplet $DROPLET exists at $IP — skipping create (cloud-init only runs on first boot)"
fi
[[ -n $IP ]] || die "could not determine droplet IP"

# --- DNS ---------------------------------------------------------------------

RECORD_LINE="$(doctl compute domain records list "$DNS_ZONE" \
    --format ID,Type,Name,Data --no-header \
    | awk -v n="$DROPLET" '$2 == "A" && $3 == n {print $1, $4}')"
if [[ -z $RECORD_LINE ]]; then
    doctl compute domain records create "$DNS_ZONE" \
        --record-type A --record-name "$DROPLET" \
        --record-data "$IP" --record-ttl "$DNS_TTL" >/dev/null
    note "created DNS A record $FQDN -> $IP"
elif [[ "${RECORD_LINE#* }" != "$IP" ]]; then
    doctl compute domain records update "$DNS_ZONE" \
        --record-id "${RECORD_LINE%% *}" --record-data "$IP" >/dev/null
    note "updated DNS A record $FQDN -> $IP (was ${RECORD_LINE#* })"
else
    note "DNS A record $FQDN -> $IP already correct"
fi

# --- Host key scan (droplet active != sshd ready: retry) ---------------------

# ssh-keyscan may emit its "# host:port banner" comment on stdout (macOS
# does) — take the first real key line, never the comment.
HOSTKEY=""
for _ in $(seq 1 36); do
    HOSTKEY="$(ssh-keyscan -t ed25519 -T 5 "$IP" 2>/dev/null \
        | awk '!/^#/ && $2 == "ssh-ed25519" {print; exit}')" || true
    [[ -n $HOSTKEY ]] && break
    sleep 5
done
[[ -n $HOSTKEY ]] || die "ssh-keyscan never succeeded against $IP"
# Two known_hosts lines: the IP (what CI connects to) and the FQDN.
KNOWN_HOSTS="$HOSTKEY
$FQDN ${HOSTKEY#* }"

# --- Verify first-boot convergence ------------------------------------------

note "verifying first-boot configuration (cloud-init, mount, caddy)..."
knownfile="$(mktemp)"
trap 'rm -f "${userdata:-}" "$knownfile"' EXIT
printf '%s\n' "$KNOWN_HOSTS" > "$knownfile"
ssh -i "$KEYFILE" \
    -o UserKnownHostsFile="$knownfile" \
    -o StrictHostKeyChecking=yes \
    -o ConnectTimeout=10 \
    "deploy@$IP" \
    'sudo cloud-init status --wait >/dev/null && mountpoint -q /mnt/harness-data && systemctl is-active --quiet caddy' \
    || die "first-boot verification failed — inspect: ssh -i $KEYFILE deploy@$IP 'sudo cloud-init status --long; sudo journalctl -u caddy -n 50'"
note "verified: cloud-init done, /mnt/harness-data mounted, caddy active"

# --- Emit GitHub environment setup -------------------------------------------

echo
note "$DROPLET is ready."
print_setup
