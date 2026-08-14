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

## Fresh VM bring-up

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
| `ANTHROPIC_API_KEY` | provider key |
| `MINEAI_GATEWAY_URL` | MineAI gateway endpoint (rendered into the `tools.mineai_gateway` passthrough) |
| `MINEAI_GATEWAY_SECRET` | MineAI gateway credential |

```sh
export ARK_HOST=127.0.0.1 ARK_PORT=7777 \
       ARK_AUTH_SECRET="$(openssl rand -hex 32)" ANTHROPIC_API_KEY=sk-ant-... \
       MINEAI_GATEWAY_URL=https://... MINEAI_GATEWAY_SECRET=...
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

Per-environment **secrets**: `ANTHROPIC_API_KEY`, `ARK_AUTH_SECRET`,
`MINEAI_GATEWAY_URL`, `MINEAI_GATEWAY_SECRET`, `SSH_PRIVATE_KEY` (dedicated
ed25519 deploy key per environment). Per-environment **variables**:
`DEPLOY_HOST`, `DEPLOY_USER` (the `deploy` user below), `SSH_KNOWN_HOSTS`
(output of `ssh-keyscan -t ed25519 <host>`, captured at provision time — the
workflow pins it with `StrictHostKeyChecking=yes`).

Each deploy: rsync the repo to `/opt/harness-ark` (excluding `.venv`, which
holds the live interpreter), then run
[deploy-remote.sh](deploy-remote.sh) as root, which re-runs `install.sh`,
renders and validates the config **before** replacing the live one, syncs
[agents/](agents/) into `$ARK_HOME/agents/` (never deleting — live
`workspace/` state survives; a skill removed from the repo lingers until
removed manually), restarts the unit, and fails the run unless `/health`
returns 200. Secrets travel to the VM over ssh stdin only — never argv,
never a persisted file — and the scripts never `set -x`.

**VM contract** (provisioning, mine-capstone#469): a `deploy` user whose
`authorized_keys` holds the environment's deploy public key, with passwordless
sudo (`deploy ALL=(ALL) NOPASSWD:ALL` — needed for `--rsync-path='sudo
rsync'` and the remote script; root login stays disabled).

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

## Intentionally not here

- **TLS / reverse proxy** — use the Caddy or nginx WebSocket-aware configs
  in [docs/deploy.md](../docs/deploy.md). The API must stay loopback-only
  behind it.
- **Droplet + volume provisioning** — mine-capstone#469.
- **Upstream sync process** — mine-capstone#471.

## Backups

Everything stateful is under `/mnt/harness-data`; see the Backups section of
[docs/deploy.md](../docs/deploy.md) (tar `ARK_HOME`; `sqlite3 ark.db
".backup"` for a clean snapshot).
