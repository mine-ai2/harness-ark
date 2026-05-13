"""Create the on-disk structure under ARK_HOME for the server and its agents.

Idempotent: never overwrites existing files; only fills in what's missing.
"""

from __future__ import annotations

from pathlib import Path

from . import paths
from .config import AgentConfig, Config

DEFAULT_SESSION_CONTEXT = """\
You are {name}, an agent running inside the Ark harness.
Edit this file (~/.ark/agents/{name}/session_context.md) to set your persona,
goals, and any persistent context that should accompany every session.
"""

DEFAULT_HEARTBEAT_PROMPT = """\
This is your scheduled heartbeat. Check on anything that needs your attention,
then end your turn when there is nothing further to do.
"""


def ensure_ark_home() -> None:
    paths.ark_home().mkdir(parents=True, exist_ok=True)
    paths.agents_dir().mkdir(parents=True, exist_ok=True)
    paths.skills_dir().mkdir(parents=True, exist_ok=True)


def ensure_agent_dir(agent: AgentConfig) -> None:
    base = paths.agent_dir(agent.name)
    base.mkdir(parents=True, exist_ok=True)
    paths.agent_skills_dir(agent.name).mkdir(parents=True, exist_ok=True)
    agent.workspace.mkdir(parents=True, exist_ok=True)
    _write_if_missing(
        base / "session_context.md",
        DEFAULT_SESSION_CONTEXT.format(name=agent.name),
    )
    _write_if_missing(base / "heartbeat_prompt.md", DEFAULT_HEARTBEAT_PROMPT)


def bootstrap(config: Config) -> None:
    ensure_ark_home()
    for agent in config.agents.values():
        ensure_agent_dir(agent)


def _write_if_missing(path: Path, content: str) -> None:
    if path.exists():
        return
    path.write_text(content)
