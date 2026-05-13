# Development

Ark's built-in tools include arbitrary file I/O and shell access. During
development, run the server inside a Docker container so that surface is
contained. The CLI client stays on the host.

## One-time setup

```
mkdir -p .ark-home
cp config.example.json .ark-home/config.json
# edit .ark-home/config.json — set auth_secret and your Anthropic API key
docker compose build
docker compose run --rm ark python -m ark init
```

`docker compose run` mounts the same volumes as `up`, so the agent directories
and SQLite database land in `./.ark-home/` on the host.

## Daily workflow

Server:

```
docker compose up        # foreground; Ctrl-C to stop
docker compose up -d     # or detached
docker compose logs -f   # follow logs when detached
```

Client (from another terminal, on the host):

```
ARK_HOME=$(pwd)/.ark-home .venv/bin/python -m ark agents
ARK_HOME=$(pwd)/.ark-home .venv/bin/python -m ark chat scribe
ARK_HOME=$(pwd)/.ark-home .venv/bin/python -m ark sessions scribe
```

## Reset

```
docker compose down
rm -rf .ark-home/agents .ark-home/ark.db*
```

Leaves `config.json` in place.

## Notes

- `.ark-home/config.json` is the single source of truth — the containerized
  server reads it via `/root/.ark`, the host CLI reads it via `ARK_HOME`.
- The server binds `0.0.0.0` inside the container; the CLI translates that to
  `127.0.0.1` when dialing, which the compose port-forward maps back into the
  container.
- The agent runs as `root` inside the container intentionally — production
  intent is for agents to actually own the box they run on.
- Code changes are live (bind-mounted at `/app`). Adding a dependency to
  `requirements.txt` requires `docker compose build`.
- Tests still run on the host venv: `.venv/bin/pytest -q`.
