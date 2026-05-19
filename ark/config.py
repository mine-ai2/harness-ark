"""Load and validate ~/.ark/config.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths


class ConfigError(Exception):
    pass


KNOWN_PROVIDER_TYPES = frozenset({"anthropic", "google", "openai", "openrouter"})


@dataclass
class ServerConfig:
    host: str
    port: int
    auth_secret: str


@dataclass
class ProviderConfig:
    provider_type: str  # one of KNOWN_PROVIDER_TYPES
    api_key: str
    base_url: str | None = None


@dataclass
class AgentConfig:
    name: str
    provider: str  # references a key in Config.providers
    model: str
    workspace: Path
    always_loaded_skills: list[str] = field(default_factory=list)
    # Override for the model's input-token ceiling. Optional — falls back to
    # the table in ark.models. Used purely to compute the usage indicator.
    max_context_tokens: int | None = None


@dataclass
class Config:
    server: ServerConfig
    providers: dict[str, ProviderConfig]
    tools: dict[str, dict[str, Any]]
    agents: dict[str, AgentConfig]


def load(path: Path | None = None) -> Config:
    path = path or paths.config_path()
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(f"invalid JSON in {path}: {e}") from e
    return _from_dict(raw)


def _from_dict(raw: dict[str, Any]) -> Config:
    server = _server(raw.get("server") or {})
    providers = _providers(raw.get("providers") or {})
    tools = dict(raw.get("tools") or {})
    agents = _agents(raw.get("agents") or {}, providers)
    return Config(server=server, providers=providers, tools=tools, agents=agents)


def _server(raw: dict[str, Any]) -> ServerConfig:
    secret = raw.get("auth_secret")
    if not secret or not isinstance(secret, str):
        raise ConfigError("server.auth_secret is required and must be a non-empty string")
    return ServerConfig(
        host=raw.get("host", "127.0.0.1"),
        port=int(raw.get("port", 7777)),
        auth_secret=secret,
    )


def _providers(raw: dict[str, Any]) -> dict[str, ProviderConfig]:
    out: dict[str, ProviderConfig] = {}
    for name, cfg in raw.items():
        if not isinstance(cfg, dict):
            raise ConfigError(f"providers.{name} must be an object")
        ptype = cfg.get("provider_type")
        if not ptype:
            raise ConfigError(f"providers.{name}.provider_type is required")
        if ptype not in KNOWN_PROVIDER_TYPES:
            raise ConfigError(
                f"providers.{name}.provider_type {ptype!r} is not one of "
                f"{sorted(KNOWN_PROVIDER_TYPES)}"
            )
        if not cfg.get("api_key"):
            raise ConfigError(f"providers.{name}.api_key is required")
        base_url = cfg.get("base_url")
        if base_url is not None and not isinstance(base_url, str):
            raise ConfigError(f"providers.{name}.base_url must be a string")
        out[name] = ProviderConfig(
            provider_type=ptype, api_key=cfg["api_key"], base_url=base_url
        )
    return out


def _agents(
    raw: dict[str, Any], providers: dict[str, ProviderConfig]
) -> dict[str, AgentConfig]:
    out: dict[str, AgentConfig] = {}
    for name, cfg in raw.items():
        if not isinstance(cfg, dict):
            raise ConfigError(f"agents.{name} must be an object")
        for key in ("provider", "model"):
            if not cfg.get(key):
                raise ConfigError(f"agents.{name}.{key} is required")
        provider = cfg["provider"]
        if provider not in providers:
            raise ConfigError(
                f"agents.{name}.provider '{provider}' is not in providers"
            )
        workspace = (
            Path(cfg["workspace"]).expanduser()
            if cfg.get("workspace")
            else paths.default_agent_workspace(name)
        )
        skills = cfg.get("always_loaded_skills") or []
        if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
            raise ConfigError(
                f"agents.{name}.always_loaded_skills must be a list of strings"
            )
        max_ctx = cfg.get("max_context_tokens")
        if max_ctx is not None and (not isinstance(max_ctx, int) or max_ctx <= 0):
            raise ConfigError(
                f"agents.{name}.max_context_tokens must be a positive integer if set"
            )
        out[name] = AgentConfig(
            name=name,
            provider=provider,
            model=cfg["model"],
            workspace=workspace,
            always_loaded_skills=list(skills),
            max_context_tokens=max_ctx,
        )
    return out
