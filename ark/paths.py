"""Filesystem paths under ARK_HOME (default ~/.ark)."""

from __future__ import annotations

import os
from pathlib import Path


def ark_home() -> Path:
    return Path(os.environ.get("ARK_HOME", str(Path.home() / ".ark"))).expanduser()


def config_path() -> Path:
    return ark_home() / "config.json"


def db_path() -> Path:
    return ark_home() / "ark.db"


def agents_dir() -> Path:
    return ark_home() / "agents"


def skills_dir() -> Path:
    return ark_home() / "skills"


def agent_dir(name: str) -> Path:
    return agents_dir() / name


def agent_skills_dir(name: str) -> Path:
    return agent_dir(name) / "skills"


def default_agent_workspace(name: str) -> Path:
    return agent_dir(name) / "workspace"
