# Designing Skills

A **skill** is a Python module that extends an Ark agent with one or more
tools. Skills are the primary way to grow what an agent can do without
touching the harness itself.

> Read this with [ark/skills.py](../ark/skills.py) and [ark/tools.py](../ark/tools.py)
> open if you want to see the machinery directly. The model here is small,
> and the code is short enough to skim end to end.

## The minimum viable skill

A skill is one `.py` file. Drop it in `~/.ark/skills/` (global, available to
every agent) or `~/.ark/agents/<agent-name>/skills/` (scoped to one agent).
The module docstring is the skill's one-line description; `@tool`-decorated
functions become tools.

```python
# ~/.ark/skills/notes.py
"""Capture and retrieve quick notes."""

from ark.skills import tool

@tool
def add_note(title: str, body: str) -> str:
    """Save a note to the notebook. Returns a confirmation string."""
    ...

@tool
def search_notes(query: str, limit: int = 10) -> str:
    """Search notes by full-text match. Returns most recent first."""
    ...
```

After dropping that file and restarting the server, the agent can:

- Call `list_skills()` and see `notes — Capture and retrieve quick notes.`
- Call `load_skill(name="notes")` to expose `add_note` and `search_notes`.
- Use those tools for the rest of the session.

## Why lazy-loaded

Every tool's JSON schema costs tokens on every model call. With dozens of
skills installed, exposing them all on every turn is wasteful — and noisy
enough to degrade the model's tool selection.

Ark's approach:

1. Agents see a **manifest** (skill name + one-line description) on every
   session start. Cheap.
2. Tools are revealed **only when the agent calls `load_skill`**. The agent
   decides what's relevant for the task at hand.
3. Skills in the agent's `always_loaded_skills` config list skip step 2 —
   their tools are exposed up front. Use this for a small set of tools that
   define the agent's core role.

The implication for skill authors: **make your descriptions discoverable.**
The first line of your module docstring is the only thing the agent sees
before deciding whether to load it. Be specific.

```python
# Good — tells the agent when it would want this
"""Send and receive messages in our team's Slack workspace."""

# Bad — too generic to choose
"""Slack utilities."""
```

## Schema generation

`@tool` builds an OpenAPI-style JSON schema from the function's signature and
docstring. The decorator:

- Uses the **function name** as the tool name.
- Uses the **first paragraph of the docstring** as the tool description.
- Walks **type hints** to build the input schema.
- Marks parameters without defaults as `required`.

Supported type → JSON schema mappings ([ark/skills.py:_hint_to_schema](../ark/skills.py)):

| Python | JSON schema |
|---|---|
| `str` | `string` |
| `int` | `integer` |
| `float` | `number` |
| `bool` | `boolean` |
| `list[...]`, `tuple[...]`, `set[...]` | `array` |
| `dict[...]` | `object` |
| `Optional[T]` / `T \| None` | mapping of `T`, not in `required` |
| anything else | `string` |

Per-argument descriptions aren't extracted from docstrings yet. If you want
the model to know what a parameter is for, name it descriptively. A short
function docstring covers the usual case.

## Always-on vs lazy-loaded

In `config.json`:

```json
{
  "agents": {
    "scribe": {
      "provider": "anthropic",
      "model": "claude-sonnet-4-6",
      "endpoint": "https://api.anthropic.com/v1/messages",
      "always_loaded_skills": ["notes", "calendar"]
    }
  }
}
```

Use `always_loaded_skills` for the core 1–3 skills that define an agent.
Everything else stays lazy so the manifest scales.

A skill placed in `~/.ark/agents/<name>/skills/` overrides the global skill
of the same name — useful when one agent needs a customized version.

## Accessing live context

Most tools just take their args and return a string. When a tool needs to
know *which* agent or session it's running for, or wants to use the
server's DB connection, it pulls live context from a contextvar:

```python
from ark.skills import tool
from ark.tools import current_context

@tool
def whoami() -> str:
    """Return the current agent's name and session id."""
    ctx = current_context()
    return f"agent={ctx.agent.name} session={ctx.session_id}"
```

`ToolContext` ([ark/tools.py:ToolContext](../ark/tools.py)) gives you:

- `agent` — the `AgentConfig` for the running agent
- `session_id` — id of the current session
- `conn` — the SQLite connection (read or write Ark's DB if you must)
- `config` — the full `Config`
- `cwd` — the agent's workspace directory (also set as `os.getcwd()` for
  the duration of the call)
- `loaded_skills` — set of skill names loaded in this session (mutable, but
  prefer the `load_skill` meta-tool to mutate it)

Only call `current_context()` from inside a tool function. Outside of a
tool dispatch, the contextvar isn't set.

## Async tools

Tools can be `async def` if they do I/O. The runtime detects coroutine
functions and awaits them directly; sync tools are dispatched to a thread.

```python
import httpx
from ark.skills import tool

@tool
async def fetch(url: str) -> str:
    """Fetch a URL and return its text body."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)
        return r.text[:5000]
```

Long-blocking sync work is fine too — `tools.execute` runs it on a worker
thread so the FastAPI event loop stays responsive. Pick async if the
library you're calling is async-native; pick sync if it isn't.

## Error handling

Three ways a tool call can end:

1. **Return a string.** It's passed to the model as the tool result.
2. **Raise `ToolError`.** The message becomes the tool result and the result
   is flagged as an error (`is_error=True` in the persisted message, `error:
   true` in the WS event). The model sees both pieces.
3. **Raise anything else.** Caught by `tools.execute`; the exception type
   and message are returned as an error result. Equivalent to (2) in
   effect, but `ToolError` is the idiomatic signal.

```python
from ark.skills import tool
from ark.tools import ToolError

@tool
def get_secret(name: str) -> str:
    """Look up a value in the secret store."""
    value = my_store.get(name)
    if value is None:
        raise ToolError(f"no such secret: {name}")
    return value
```

Rule of thumb: raise `ToolError` for *expected* failures the model can
recover from (bad input, missing record). Let unexpected exceptions
propagate — they get logged and the model sees enough to react.

## Return values

`tools.execute` coerces whatever you return:

- `str` → passed through.
- `None` → empty string.
- Anything else → JSON-encoded if possible, otherwise `repr()`.

Strings are best. The model is a language model; structured data is fine,
but format it deliberately (Markdown, JSON, plain prose) rather than
relying on `repr` to produce something legible.

Avoid returning gigabytes. The model has to swallow the whole result on the
next turn, and our DB row holds the whole string. Truncate or summarize
inside the tool when needed.

## Side effects, state, persistence

Skills run **in-process** in the server. A module-level variable in your
skill file persists across calls (and across sessions) for the life of the
server. That's a feature, not a bug — use it for caches and clients:

```python
import httpx
from ark.skills import tool

_client = httpx.Client(timeout=10)  # reused across calls

@tool
def fetch(url: str) -> str:
    """Fetch a URL."""
    return _client.get(url).text[:5000]
```

But: module-level state is **shared across all sessions and agents** that
load this skill. If state needs to be per-session, key it by
`current_context().session_id`. If it needs to be persistent across server
restarts, write to disk (your tool's `cwd` is the agent workspace) or use
the Ark DB via `current_context().conn`.

Concurrency: multiple sessions may invoke your skill simultaneously. Your
module-level state must tolerate it — use a `threading.Lock` for shared
mutable state, or design it to be effectively immutable / atomic.

## Skill scope: global or per-agent

| Where | Loaded for | Use when |
|---|---|---|
| `~/.ark/skills/` | every agent | the skill is generic ("fetch a URL", "send mail") |
| `~/.ark/agents/<name>/skills/` | one agent | the skill is part of *that agent's identity*, or you want a per-agent override of a global skill |

Per-agent skills shadow globals of the same name.

## Loading and reloading

Skills are imported when the server starts. **There is no hot reload in
v1** — editing a skill file requires a server restart for the change to
take effect. The model can call `load_skill` to surface a known skill, but
it cannot pick up *new* skills you've just dropped on disk without a
restart.

In Docker: `docker compose restart ark`.
In systemd: `sudo systemctl restart ark`.

## Testing a skill before shipping

The simplest test is direct — skills are plain Python:

```python
# tests/test_my_skill.py
from my_skill import add_note  # the @tool decorator doesn't change the function

def test_add_note_returns_confirmation():
    assert "saved" in add_note(title="t", body="b")
```

For tools that use `current_context()`, exercise them through
`tools.execute(...)` — the runtime sets the contextvar for you:

```python
import asyncio
from unittest.mock import MagicMock
from ark import skills, tools
from ark.config import AgentConfig
from ark.tools import ToolContext

def test_tool_using_context(ark_home, tmp_path):
    # Drop the skill file into ARK_HOME and discover it.
    (ark_home / "skills" / "mine.py").write_text(MY_SKILL_SOURCE)
    skills.discover([])

    agent = AgentConfig(name="t", provider="anthropic", model="m",
                        endpoint="e", workspace=tmp_path)
    ctx = ToolContext(conn=MagicMock(), config=MagicMock(), agent=agent,
                      session_id="s", cwd=tmp_path, loaded_skills={"mine"})
    output, err = asyncio.run(tools.execute("my_tool", {"arg": "x"}, ctx=ctx))
    assert err is False
```

See `tests/test_skills.py` and `tests/test_post_to_session.py` for working
end-to-end examples.

## Patterns worth respecting

- **One skill, one capability.** "notes" is a skill; "save_note" is not.
  Bundle the related tools together; let the agent load the whole bundle.
- **Make descriptions actionable.** A skill description should answer "when
  would I want this?", not "what is this?".
- **Stateless tools by default.** Each call should be independent. If you
  need state, key it by session and document the lifetime.
- **Fail loudly.** `ToolError` with a useful message beats silently
  returning an empty string.
- **Return strings shaped for an LLM.** Markdown sections, JSON when
  structure matters, terse plain text otherwise. Don't dump pickled bytes.
- **Don't print.** stdout/stderr go to the server log, not back to the
  model. Return what the model needs to see.
- **Watch your imports.** Module-level imports run once at server start,
  so they're free per-call — but if they're slow they delay startup. Heavy
  optional dependencies belong inside the function.

## Pitfalls

- **Editing a skill and expecting it to take effect.** Restart the server.
- **Returning huge outputs.** They land in the model's context AND your DB.
- **Catching all exceptions inside a tool.** You'll hide real bugs from the
  log. Let unexpected exceptions propagate; the runtime turns them into
  flagged tool results without crashing.
- **Storing secrets in skill files.** They're not auto-gitignored. Read
  from env vars or the agent's `config.tools` entry instead.
- **Assuming you're the only caller.** If your skill talks to a remote API
  with per-account rate limits, two parallel sessions can hit them together.
- **Forgetting `Optional` for nullable args.** `path: str` without a default
  becomes required; `path: str | None = None` is optional and nullable.
