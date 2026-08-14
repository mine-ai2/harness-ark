# MineAI Harness Deployment

Production scaffold for the MineAI (mine-ai2) deployment of Ark. This is an
opinionated instantiation of the upstream guide's systemd option
([docs/deploy.md](../docs/deploy.md), "Option A") with a dedicated service
user instead of root.

Everything MineAI-specific lives in this directory (plus
`.github/workflows/talos-test.yml`) so that syncing the fork with upstream
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

```sh
export ARK_HOST=127.0.0.1 ARK_PORT=7777 \
       ARK_AUTH_SECRET="$(openssl rand -hex 32)" ANTHROPIC_API_KEY=sk-ant-...
envsubst '$ARK_HOST $ARK_PORT $ARK_AUTH_SECRET $ANTHROPIC_API_KEY' \
  < /opt/harness-ark/deploy/config/config.json.tmpl > /tmp/config.json
sudo install -o ark -g ark -m 0600 /tmp/config.json /mnt/harness-data/config.json
rm /tmp/config.json
```

The explicit variable list keeps `envsubst` from touching any other
`$`-looking content. The unrendered template is deliberately invalid JSON
(`"port": ${ARK_PORT}` is unquoted), so deploying it unrendered fails loudly
at config load instead of half-working.

Start and check:

```sh
sudo systemctl start ark
curl -fsS http://127.0.0.1:7777/health   # -> {"ok": true}
```

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
- **Automated deploys + config rendering from environment secrets** —
  mine-capstone#470 (GitHub environments, prod approval, `/health` gate).
- **Upstream sync process** — mine-capstone#471.

## Backups

Everything stateful is under `/mnt/harness-data`; see the Backups section of
[docs/deploy.md](../docs/deploy.md) (tar `ARK_HOME`; `sqlite3 ark.db
".backup"` for a clean snapshot).
