# Deploying Ark in Production

> "Production" for Ark means a long-running server on a host the agents are
> meant to own. The dev workflow ([DEVELOPMENT.md](../DEVELOPMENT.md)) jails
> the server in a container on your laptop; production puts it on a dedicated
> machine where the agent's bash + filesystem access is the point.

## Host Sizing

Ark itself is tiny. The footprint that matters is whatever your agents do.

- **CPU**: 1 vCPU is enough unless your agents shell out to heavy work.
- **RAM**: 512 MB for the server. Add headroom for `run_command` subprocesses.
- **Disk**: a few hundred MB for the install + `~/.ark/` state. Workspaces
  grow as agents create files; budget accordingly.
- **Python**: 3.10+ recommended (3.12 in the dev container). 3.9 also works
  but requires the `eval_type_backport` shim already in `requirements.txt`.
- **OS**: any Linux. macOS works for personal use but is not the target.

## Decide: systemd or Docker

Two deployment shapes are supported. Pick one.

| | systemd | Docker |
|---|---|---|
| Easier to update | edit + `systemctl restart` | `git pull && docker compose up -d --build` |
| Easier to back up | tar `~/.ark/` | tar `./.ark-home/` |
| Cleaner blast radius | host fs is fair game | filesystem scoped to the container |
| Right when... | you intentionally want the agent on the host | the host has other workloads to protect |

The container option is recommended unless you have a specific reason to run
on bare metal.

## Option A — systemd

```
# /etc/systemd/system/ark.service
[Unit]
Description=Ark agent harness
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/ark
ExecStart=/opt/ark/.venv/bin/python -m ark serve
Restart=on-failure
RestartSec=5s
Environment=ARK_HOME=/var/lib/ark
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Setup:

```
sudo install -d -m 0750 /var/lib/ark
sudo install -m 0600 your-config.json /var/lib/ark/config.json
sudo git clone <repo> /opt/ark
cd /opt/ark && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo .venv/bin/python -m ark init   # creates agent dirs + DB under ARK_HOME
sudo systemctl daemon-reload
sudo systemctl enable --now ark
journalctl -u ark -f
```

Run as `root` only when you actually want the agents to own the box. If not,
create a dedicated user and chown `/var/lib/ark` to it.

## Option B — Docker

The dev compose file is fine for production with two changes: add a restart
policy, and drop the source bind-mount so updates require a rebuild instead
of accidental hot reloads.

```yaml
# docker-compose.yml (production)
services:
  ark:
    image: ghcr.io/your/ark:latest   # or build: .
    container_name: ark
    restart: unless-stopped
    volumes:
      - /var/lib/ark:/root/.ark
    ports:
      - "127.0.0.1:7777:7777"
```

Build + push (or build locally):

```
docker compose build
docker compose up -d
docker compose logs -f
```

Bootstrap state once:

```
docker compose run --rm ark python -m ark init
```

To update: pull new image (or rebuild), then `docker compose up -d`.

## Front-end It With TLS

The bearer token in `auth_secret` is the only thing protecting the API. Do
**not** expose the server directly on the public internet without TLS.

Caddy is the simplest option — it gets a Let's Encrypt cert automatically.

```
# /etc/caddy/Caddyfile
ark.example.com {
  reverse_proxy 127.0.0.1:7777
}
```

Nginx alternative:

```
server {
  listen 443 ssl http2;
  server_name ark.example.com;
  ssl_certificate     /etc/letsencrypt/live/ark.example.com/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/ark.example.com/privkey.pem;

  location / {
    proxy_pass http://127.0.0.1:7777;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;       # WebSocket upgrade
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 86400;                     # long-lived chat sessions
  }
}
```

Caddy handles the WebSocket upgrade automatically. Both setups require port
7777 to stay bound to `127.0.0.1` so it isn't reachable except through the
proxy.

## Secrets

`config.json` contains the bearer token and your LLM provider API keys.
Treat it like any other secret:

- `chmod 600` and a non-readable parent directory.
- Never commit it; the repo's `.gitignore` covers `.ark-home/` but not other
  locations you might choose.
- For multi-host deployments, fetch the file from your secret manager
  (Vault, 1Password CLI, AWS Secrets Manager) at boot. Ark reads it once at
  startup, so a restart picks up rotated keys.

## Backups

Everything stateful lives under `ARK_HOME`. A single tarball is enough:

```
tar -czf ark-$(date +%F).tgz -C /var/lib ark
```

What's inside:

- `config.json` — the server config (sensitive)
- `ark.db` (+ `-wal`, `-shm`) — sessions, messages, heartbeats, crons
- `agents/<name>/session_context.md`, `heartbeat_prompt.md`
- `agents/<name>/workspace/` — agent scratch space, plus `workspace/uploads/` which holds every file users have attached via the upload endpoint (see [files.md](files.md))
- `agents/<name>/skills/` + top-level `skills/` — installed skills

SQLite is hot-backup-safe via WAL mode, but for a clean snapshot run `sqlite3
ark.db ".backup ark-snapshot.db"` before tarring. Test restores periodically.

## Logs

The server logs to stdout/stderr. uvicorn emits one line per request; the
scheduler logs cron expression failures and exceptions raised during heartbeat
or cron sessions.

- **systemd**: `journalctl -u ark -f` for live, `journalctl -u ark --since
  "1 hour ago"` for retro. Rotate with `journalctl --vacuum-time=14d` or via
  `journald.conf`.
- **Docker**: `docker compose logs -f`. Persist with a log driver
  (`json-file` with `max-size`, or ship to Loki/CloudWatch/etc.).

For deeper observability you'd add structured logging + a metrics endpoint
yourself; neither ships with v1.

## Updating

systemd:

```
cd /opt/ark
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart ark
```

Docker:

```
git pull
docker compose build
docker compose up -d        # rolling: stops, recreates, starts
```

SQLite migrations run at startup. `db.MIGRATIONS` in `ark/db.py` is the
ordered list; never edit a past entry, only append.

## Security Considerations

Ark is intentionally permissive. Read this before exposing it anywhere:

- **The agent has `run_command`.** That's shell access as the user the
  server runs as. If you ran the systemd unit as root, the agent can do
  anything root can.
- **The agent has `read_file` / `write_file` on arbitrary paths.** Same caveat.
- **A single bearer token** authenticates every API caller. There's no
  per-user identity, no audit log of *who* called what — only what the agent
  did.
- **No rate limiting.** A leaked token + a public endpoint = unbounded API
  bills against your configured provider.

Recommendations:

- Keep the server bound to `127.0.0.1` and front it with TLS + a strong
  bearer token. Use a long random string (`openssl rand -hex 32`).
- Run inside the container option (B) by default. Bind-mount only the host
  paths the agent legitimately needs to see.
- Treat the host as compromised once you give an agent root on it. Don't
  share secrets with the host that you wouldn't share with the agent.
- Rotate `auth_secret` regularly. Restart picks up the new value.

## Health Checks

`GET /health` (no auth) returns `{"ok": true}`. Wire it into your
load balancer / uptime monitor / `systemctl` health probe.

## Troubleshooting

- **`no config at /root/.ark/config.json`** — `ARK_HOME` isn't pointing at
  the directory holding your `config.json`. Check the env var or
  bind-mount target.
- **`config file not found` from the CLI** — same fix, but on the host.
  Run with `ARK_HOME=/path/to/config-dir`.
- **WebSocket disconnects every minute or so** — your reverse proxy's
  `proxy_read_timeout` (nginx) or equivalent is too low. Bump it.
- **Scheduler not firing** — check `journalctl -u ark` or `docker compose
  logs`; cron expression errors are printed there but don't fail the server.
- **`unsupported provider`** — `make_provider` in [ark/runtime.py](../ark/runtime.py)
  knows about `anthropic` and `openai`. Anything else is a config typo.
