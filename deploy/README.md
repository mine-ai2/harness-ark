# MineAI Harness Deployment

Production scaffold for the MineAI (mine-ai2) deployment of Ark. This is an
opinionated instantiation of the upstream guide's systemd option
([docs/deploy.md](../docs/deploy.md), "Option A") with a dedicated service
user instead of root.

Everything MineAI-specific lives in this directory (plus the `talos-*`
workflows under `.github/workflows/`) so that syncing the fork with upstream
(`druths/ark`) never conflicts.

## Layout contract

| Path | Role | Ownership | Lifecycle |
|---|---|---|---|
| `/opt/harness-ark` | Code + `.venv` | root | **Replaced wholesale on every deploy.** Never store state here. |
| `/mnt/harness-data` | `ARK_HOME` — all state | `ark:ark` | Persistent attached volume. Survives deploys and VM rebuilds. |

Inside `/mnt/harness-data`:

- `config.json` — rendered from [config/config.json.tmpl](config/config.json.tmpl), mode 0600
- `ark.db` (+ `-wal`, `-shm`) — sessions, messages, heartbeats, crons
- `agents/<name>/` — `session_context.md`, `heartbeat_prompt.md`, `workspace/`
  (including `workspace/uploads/`), `skills/`
- `skills/` — global skills
- `projects/` — shared project working directories

### Why the service can never silently run against the root disk

Three independent layers:

1. `RequiresMountsFor=/mnt/harness-data` in the unit — systemd fails the
   start if the volume's mount unit isn't active.
2. `AssertPathIsMountPoint=/mnt/harness-data` — fails the start loudly even
   if no mount unit exists and the path is a plain directory.
3. `python -m ark serve` exits nonzero when `$ARK_HOME/config.json` is
   missing, so an empty mountpoint directory can't bootstrap.

**Provisioning contract**: the volume must have an `/etc/fstab` entry (with
`nofail` so boot doesn't hang if the volume is detached) or an explicit
`.mount` unit. `RequiresMountsFor` needs that mount unit to exist; a raw
`mount` command at provision time is not enough across reboots.

## Provisioning (DigitalOcean)

Scripted-manual v1 (no Terraform precedent in this org):
[scripts/provision.sh](scripts/provision.sh) drives `doctl`, cloud-init does
first-boot configuration, and this section is the runbook. Script placement
rule: flat `deploy/*.sh` scripts run **on the VM**; `deploy/scripts/*.sh`
run **from outside it** (operator laptop or CI runner).

### What one environment is

Per environment (`dev` → GitHub environment `development`, `prod` →
`production`) — dev and prod share **nothing**:

| Resource | Name (dev shown) | Notes |
|---|---|---|
| Droplet | `harness-dev` | `s-2vcpu-4gb`, `ubuntu-24-04-x64`, region `nyc3`, VPC `mineai-internal` |
| Volume | `harness-dev-data` | 10 GiB block storage, ext4, mounted `/mnt/harness-data` |
| Firewall | `harness-dev-fw` | inbound tcp 22 + 443 only, applied by tag |
| DNS | `harness-dev.mine.ai` | A record in the DO-managed `mine.ai` zone, TTL 300 |
| Deploy keypair | `~/.ssh/harness-dev-deploy` | ed25519, no passphrase (CI consumes it) |
| Tag | `harness-dev` (+ shared label `harness`) | binds firewall to droplet |

Cost: ~$25/mo per environment (~$50 total for both).

### Provision an environment

Prerequisites: authenticated `doctl` and `gh`; the DO account's SSH keys
("Main", "milo-agent") act as root break-glass.

```sh
deploy/scripts/provision.sh dev    # or: prod
```

Idempotent converge — safe to re-run after a partial failure; existing
resources are kept. The droplet's cloud-init
([cloud-init/user-data.tmpl.yml](cloud-init/user-data.tmpl.yml), rendered
by [scripts/render-user-data.sh](scripts/render-user-data.sh)) runs on
**first boot only**: it formats the volume *only if blank*, writes the
fstab entry (`nofail`), creates the `deploy` user, hardens sshd, and
installs Caddy with the environment's domain. To reapply cloud-init,
delete the droplet and re-run provision.sh — the data volume survives
because of the format-if-blank guard.

The script ends by verifying first boot over SSH (`cloud-init status`,
mountpoint, caddy active) and printing the exact `gh` commands that wire
the GitHub environment (`SSH_PRIVATE_KEY`, `DEPLOY_HOST`, `DEPLOY_USER`,
`DEPLOY_DOMAIN`, `SSH_KNOWN_HOSTS`). Five secrets remain operator-supplied:
`ARK_AUTH_SECRET` (`openssl rand -hex 32`, unique per env), `OPENAI_API_KEY`,
`DO_INFERENCE_API_KEY`, `MINEAI_GATEWAY_URL`, `MINEAI_GATEWAY_SECRET`.

Until the first deploy runs, `https://<domain>/health` returns **502 —
this is expected**: Caddy is up (cert issuance takes 1–5 min once the DNS
record lands; watch `journalctl -u caddy`) but ark isn't installed yet.
The first talos-deploy run fixes that.

### Access model

- Day-to-day: `ssh -i ~/.ssh/harness-<env>-deploy deploy@<IP>` — the same
  key CI uses. NOPASSWD sudo (required by the deploy workflow's
  `--rsync-path='sudo rsync'` and deploy-remote.sh).
- Break-glass: key-only root SSH using the DO account keys
  (`PermitRootLogin prohibit-password`; password root login is off).
- Port 22 is open to 0.0.0.0/0 **by necessity**: GitHub-hosted runners
  have no stable egress CIDRs (thousands of ranges vs the DO firewall's
  ~50-rule cap). The control is key-only auth: password and interactive
  auth are disabled by the sshd drop-in, and only `root` and `deploy` may
  log in.

### Firewall

The DO cloud firewall is the single enforcement layer: inbound tcp 22 and
443, all outbound. Port 80 stays closed — Caddy is configured for the
443-only TLS-ALPN-01 ACME challenge (`disable_http_challenge`). ufw is
deliberately **not** enabled on the host (one source of truth; the only
listeners are caddy and key-only sshd — ark binds loopback). If you want
belt-and-suspenders anyway:

```sh
sudo ufw default deny incoming && sudo ufw allow 22/tcp && sudo ufw allow 443/tcp && sudo ufw enable
```

Caddy is the chosen proxy (automatic certs, WebSocket upgrades handled
natively, no default proxy read timeout so long-lived `/events` sockets
survive). If it ever needs replacing, the nginx fallback config —
including the WebSocket upgrade headers and `proxy_read_timeout` — is in
[docs/deploy.md](../docs/deploy.md); remember to open port 80 or keep
TLS-ALPN-01 for ACME.

### Smoke tests

[scripts/smoke.sh](scripts/smoke.sh) runs from an external network and
exercises DNS + TLS + the WebSocket proxy path: `/health` over TLS,
unauthenticated `/events` rejected, authenticated `/events` round-trip.
CI runs it after every deploy; run it manually with:

```sh
ARK_AUTH_SECRET=... deploy/scripts/smoke.sh harness-dev.mine.ai
ARK_AUTH_SECRET=... deploy/scripts/smoke.sh harness-dev.mine.ai --soak 330   # >5 min WS soak
```

(Needs `pip install 'websockets>=13.0'` locally.)

### Teardown / rebuild

Droplet-only rebuild (keeps all data — the volume is never formatted when
it already has a filesystem):

```sh
doctl compute droplet delete harness-dev
deploy/scripts/provision.sh dev      # recreates droplet, re-runs cloud-init
# then update DEPLOY_HOST + SSH_KNOWN_HOSTS in the GitHub environment and redeploy
```

Full teardown (destroys data; rotate the GH environment secrets after):

```sh
doctl compute droplet delete harness-dev
doctl compute volume delete <id of harness-dev-data>
doctl compute firewall delete <id of harness-dev-fw>
doctl compute domain records delete mine.ai <id of harness-dev A record>
```

Note Let's Encrypt's duplicate-certificate limit (5/week) if repeatedly
tearing down and re-provisioning the same domain.

## Fresh VM bring-up

This is the **manual** path. On a provisioned droplet the CI deploy makes
it unnecessary: the workflow rsyncs the repo to `/opt/harness-ark` and runs
everything below itself — no on-VM clone required.

```sh
sudo git clone https://github.com/mine-ai2/harness-ark /opt/harness-ark
sudo /opt/harness-ark/deploy/install.sh
```

`install.sh` is idempotent (safe to re-run). It creates the `ark` service
user, builds the venv, installs dependencies, fixes data-volume permissions,
and installs + boot-enables the systemd unit. It does **not** render the
config or start the service.

Then render the config. Variables:

| Variable | Value |
|---|---|
| `ARK_HOST` | `127.0.0.1` — bearer token is the only auth; keep the port loopback-only behind the TLS proxy |
| `ARK_PORT` | `7777` |
| `ARK_AUTH_SECRET` | long random string: `openssl rand -hex 32` |
| `OPENAI_API_KEY` | provider key (talos runs on gpt-5-mini) |
| `DO_INFERENCE_API_KEY` | DigitalOcean inference router key (`providers.do`, OpenAI-compatible at inference.do-ai.run) |
| `MINEAI_GATEWAY_URL` | MineAI gateway endpoint (rendered into the `tools.mineai_gateway` passthrough) |
| `MINEAI_GATEWAY_SECRET` | MineAI gateway credential |

```sh
export ARK_HOST=127.0.0.1 ARK_PORT=7777 \
       ARK_AUTH_SECRET="$(openssl rand -hex 32)" OPENAI_API_KEY=sk-... \
       DO_INFERENCE_API_KEY=... MINEAI_GATEWAY_URL=https://... MINEAI_GATEWAY_SECRET=...
/opt/harness-ark/deploy/render-config.sh > /tmp/config.json
sudo install -o ark -g ark -m 0600 /tmp/config.json /mnt/harness-data/config.json
rm /tmp/config.json
```

[render-config.sh](render-config.sh) owns the `envsubst` variable allowlist —
add new template variables there and nowhere else. The explicit list keeps
`envsubst` from touching any other `$`-looking content, and the script fails
if any variable is unset instead of rendering it as `""`. The unrendered
template is deliberately invalid JSON (`"port": ${ARK_PORT}` is unquoted),
so deploying it unrendered fails loudly at config load instead of
half-working.

Start and check:

```sh
sudo systemctl start ark
curl -fsS http://127.0.0.1:7777/health   # -> {"ok": true}
```

## Automated deploys (CI/CD)

[.github/workflows/talos-deploy.yml](../.github/workflows/talos-deploy.yml)
deploys on every push to `main` (and via manual `workflow_dispatch`, e.g.
after rotating a secret). Two GitHub environments, configured in repo
settings → Environments, both with deployment-branch policy `main`:

- **development** — auto-deploys on every push to `main`.
- **production** — same steps, but the job waits for required-reviewer
  approval, and via `needs:` only ever deploys a commit whose dev deploy
  already passed the health gate. Pending approvals accumulate one per push;
  approve only the latest.

Per-environment **secrets**: `OPENAI_API_KEY`, `DO_INFERENCE_API_KEY`,
`ARK_AUTH_SECRET`, `MINEAI_GATEWAY_URL`, `MINEAI_GATEWAY_SECRET`, `SSH_PRIVATE_KEY` (dedicated
ed25519 deploy key per environment). Per-environment **variables**:
`DEPLOY_HOST` (droplet IP — SSH stays independent of DNS health),
`DEPLOY_USER` (the `deploy` user below), `DEPLOY_DOMAIN` (the environment's
FQDN, used by the post-deploy smoke test), `SSH_KNOWN_HOSTS` (output of
`ssh-keyscan -t ed25519 <host>`, captured at provision time — the workflow
pins it with `StrictHostKeyChecking=yes`). `provision.sh` emits all of
these ready to paste.

Each deploy: rsync the repo to `/opt/harness-ark` (excluding `.venv`, which
holds the live interpreter), then run
[deploy-remote.sh](deploy-remote.sh) as root, which re-runs `install.sh`,
renders and validates the config **before** replacing the live one, syncs
[agents/](agents/) into `$ARK_HOME/agents/` (never deleting — live
`workspace/` state survives; a skill removed from the repo lingers until
removed manually), restarts the unit, and fails the run unless `/health`
returns 200. Secrets travel to the VM over ssh stdin only — never argv,
never a persisted file — and the scripts never `set -x`.

**VM contract** (satisfied by cloud-init at provision time — see
"Provisioning" above): a `deploy` user whose `authorized_keys` holds the
environment's deploy public key, with passwordless sudo
(`deploy ALL=(ALL) NOPASSWD:ALL` — needed for `--rsync-path='sudo rsync'`
and the remote script). Password root login is disabled; key-only root
remains as break-glass via the DO account keys.

**No rollback**: a failure after the restart leaves the new code live —
fix forward. A failure before then (including any render/validation error)
leaves the old code, config, and service untouched.

## Operations

- Logs: `journalctl -u ark -f` (live), `journalctl -u ark --since "1 hour ago"`.
- Restart semantics: `Restart=on-failure` restarts crashes, kills, and
  nonzero exits, but not a clean `systemctl stop`. Systemd's default
  start-rate limiting means a persistently failing start (e.g. missing
  config) lands in `failed` state rather than looping forever.
- The unit is boot-enabled; state (DB migrations, agent dirs) is
  re-bootstrapped idempotently on every start.
- The unit deliberately has **no** systemd sandbox hardening: agents get
  real shell access via `run_command` by design, and the unprivileged `ark`
  user is the security boundary. Don't "fix" this.

## Upstream sync

This repo is a fork: `origin = mine-ai2/harness-ark`, `upstream =
druths/ark` (the sync script adds the `upstream` remote if missing).

**Fork rules (additive-only):**

- MineAI-specific code lives **only** in `deploy/` and the `talos-*`
  workflows (plus the root `CONTRIBUTING.md` pointer). Nothing else in the
  tree may diverge from upstream.
- Any change under `ark/` core must be an upstream-PR candidate — written
  as if druths/ark would merge it, never a MineAI-only hack.
- The two currently sanctioned core extensions are: real mid-turn cancel
  (mine-capstone#485, branch `ark-real-cancel`) and per-agent `max_tokens`
  (mine-capstone#481, `talos-provisioning`).

**Process:** run [scripts/sync-upstream.sh](scripts/sync-upstream.sh) from
any clean checkout. It fetches both remotes; if upstream is already
contained in `main` it says so and exits. Otherwise it creates
`sync/ark-YYYY-MM-DD` from `origin/main`, merges `upstream/main`, pushes,
and opens a PR — CI (talos-test) validates the merged tree before it can
land. Merge the PR like any other; never sync by pushing to `main`
directly.

**Conflicts** should not happen if the rules above hold (upstream doesn't
touch `deploy/`; the fork doesn't touch `ark/`). If one occurs, the script
leaves the merge in progress on the sync branch: prefer the upstream side
for `ark/` core unless the conflicting change is a sanctioned extension,
resolve, commit, push, and open the PR manually (the script prints the
commands).

## Backups

Everything stateful is under `/mnt/harness-data`; see the Backups section of
[docs/deploy.md](../docs/deploy.md) (tar `ARK_HOME`; `sqlite3 ark.db
".backup"` for a clean snapshot).
