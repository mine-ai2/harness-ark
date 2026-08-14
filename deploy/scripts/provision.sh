#!/usr/bin/env bash
# Provision one harness environment on DigitalOcean (mine-capstone#469).
#
# Runs on an operator machine with authenticated `doctl` (flat deploy/*.sh
# scripts run ON the VM; deploy/scripts/*.sh run from outside). Creates,
# per environment: tag, cloud firewall (22+443 only), deploy SSH keypair,
# block-storage volume, droplet (cloud-init from
# deploy/cloud-init/user-data.tmpl.yml), and a DNS A record. Dev and prod
# share nothing.
#
# Idempotent converge: safe to re-run after a partial failure — existing
# resources are verified and kept, missing ones created. The droplet's
# cloud-init only runs on FIRST boot; to reapply it, delete the droplet and
# re-run (the data volume survives: mkfs is guarded on the volume being
# blank).
#
# Usage: provision.sh <dev|prod>
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

# --- Arguments and derived names ---------------------------------------------

ENV_NAME="${1:-}"
case "$ENV_NAME" in
dev) GH_ENV=development ;;
prod) GH_ENV=production ;;
*) die "usage: provision.sh <dev|prod>" ;;
esac

DROPLET="harness-$ENV_NAME"
VOLUME="$DROPLET-data"
FIREWALL="$DROPLET-fw"
FQDN="$DROPLET.$DNS_ZONE"
TAG_ENV="$DROPLET"
KEYFILE="$HOME/.ssh/$DROPLET-deploy"

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

cat <<EOF

provision.sh: $DROPLET is ready. Configure the GitHub environment:

  gh api -X PUT repos/mine-ai2/harness-ark/environments/$GH_ENV
  gh secret set SSH_PRIVATE_KEY --env $GH_ENV < $KEYFILE
  gh variable set DEPLOY_HOST     --env $GH_ENV --body "$IP"
  gh variable set DEPLOY_USER     --env $GH_ENV --body "deploy"
  gh variable set DEPLOY_DOMAIN   --env $GH_ENV --body "$FQDN"
  gh variable set SSH_KNOWN_HOSTS --env $GH_ENV --body "\$(cat <<'KNOWN'
$KNOWN_HOSTS
KNOWN
)"

Still needed from the operator (never printed here):
  gh secret set ARK_AUTH_SECRET       --env $GH_ENV   # openssl rand -hex 32, unique per env
  gh secret set OPENAI_API_KEY     --env $GH_ENV
  gh secret set MINEAI_GATEWAY_URL    --env $GH_ENV
  gh secret set MINEAI_GATEWAY_SECRET --env $GH_ENV

Then deploy (push to main or workflow_dispatch talos-deploy) and smoke:
  ARK_AUTH_SECRET=... deploy/scripts/smoke.sh $FQDN
EOF
