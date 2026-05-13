import json

import pytest

from ark import config, paths


def write(d, data):
    (d / "config.json").write_text(json.dumps(data))


def minimal():
    return {
        "server": {"auth_secret": "shh"},
        "providers": {
            "anthropic": {"provider_type": "anthropic", "api_key": "k"}
        },
        "agents": {
            "scribe": {
                "provider": "anthropic",
                "model": "claude-opus-4-7",
            }
        },
    }


def test_loads_minimal_config(ark_home):
    write(ark_home, minimal())
    cfg = config.load()
    assert cfg.server.auth_secret == "shh"
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 7777
    assert "anthropic" in cfg.providers
    scribe = cfg.agents["scribe"]
    assert scribe.workspace == paths.default_agent_workspace("scribe")
    assert scribe.always_loaded_skills == []


def test_missing_config_raises(ark_home):
    with pytest.raises(config.ConfigError, match="config file not found"):
        config.load()


def test_missing_auth_secret_rejected(ark_home):
    data = minimal()
    data["server"] = {}
    write(ark_home, data)
    with pytest.raises(config.ConfigError, match="auth_secret"):
        config.load()


def test_agent_references_unknown_provider(ark_home):
    data = minimal()
    data["agents"]["scribe"]["provider"] = "nope"
    write(ark_home, data)
    with pytest.raises(config.ConfigError, match="not in providers"):
        config.load()


def test_workspace_override(ark_home, tmp_path):
    data = minimal()
    custom = tmp_path / "elsewhere"
    data["agents"]["scribe"]["workspace"] = str(custom)
    write(ark_home, data)
    cfg = config.load()
    assert cfg.agents["scribe"].workspace == custom


def test_invalid_json(ark_home):
    (ark_home / "config.json").write_text("{not json")
    with pytest.raises(config.ConfigError, match="invalid JSON"):
        config.load()


def test_provider_base_url_default_none(ark_home):
    write(ark_home, minimal())
    cfg = config.load()
    assert cfg.providers["anthropic"].base_url is None


def test_provider_base_url_parsed(ark_home):
    data = minimal()
    data["providers"]["anthropic"]["base_url"] = "https://proxy.example.com/v1"
    write(ark_home, data)
    cfg = config.load()
    assert cfg.providers["anthropic"].base_url == "https://proxy.example.com/v1"


def test_provider_base_url_must_be_string(ark_home):
    data = minimal()
    data["providers"]["anthropic"]["base_url"] = 42
    write(ark_home, data)
    with pytest.raises(config.ConfigError, match="base_url must be a string"):
        config.load()


def test_provider_type_required(ark_home):
    data = minimal()
    del data["providers"]["anthropic"]["provider_type"]
    write(ark_home, data)
    with pytest.raises(config.ConfigError, match="provider_type is required"):
        config.load()


def test_provider_type_must_be_known(ark_home):
    data = minimal()
    data["providers"]["anthropic"]["provider_type"] = "claude"  # invalid
    write(ark_home, data)
    with pytest.raises(config.ConfigError, match="provider_type"):
        config.load()


def test_multiple_providers_of_same_type(ark_home):
    """The whole point of the new shape: multiple Anthropic keys, addressable by id."""
    data = minimal()
    data["providers"]["claude_personal"] = {"provider_type": "anthropic", "api_key": "key-a"}
    data["providers"]["claude_work"] = {"provider_type": "anthropic", "api_key": "key-b"}
    data["agents"]["scribe"]["provider"] = "claude_personal"
    data["agents"]["clerk"] = {"provider": "claude_work", "model": "claude-haiku-4-5"}
    write(ark_home, data)
    cfg = config.load()
    assert cfg.providers["claude_personal"].api_key == "key-a"
    assert cfg.providers["claude_work"].api_key == "key-b"
    assert cfg.providers["claude_personal"].provider_type == "anthropic"
    assert cfg.providers["claude_work"].provider_type == "anthropic"
    assert cfg.agents["scribe"].provider == "claude_personal"
    assert cfg.agents["clerk"].provider == "claude_work"


def test_openrouter_provider(ark_home):
    data = minimal()
    data["providers"]["or"] = {"provider_type": "openrouter", "api_key": "sk-or-..."}
    data["agents"]["scribe"]["provider"] = "or"
    data["agents"]["scribe"]["model"] = "anthropic/claude-sonnet-4-6"
    write(ark_home, data)
    cfg = config.load()
    assert cfg.providers["or"].provider_type == "openrouter"
    assert cfg.agents["scribe"].provider == "or"
