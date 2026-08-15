"""Per-agent max_tokens config (sanctioned core extension, mine-capstone#481)."""

import json

import pytest

from ark import config


def write(d, data):
    (d / "config.json").write_text(json.dumps(data))


def minimal(agent_extra=None):
    return {
        "server": {"auth_secret": "shh"},
        "providers": {"anthropic": {"provider_type": "anthropic", "api_key": "k"}},
        "agents": {
            "scribe": {
                "provider": "anthropic",
                "model": "claude-opus-4-7",
                **(agent_extra or {}),
            }
        },
    }


def test_max_tokens_defaults_to_none(ark_home):
    write(ark_home, minimal())
    assert config.load().agents["scribe"].max_tokens is None


def test_max_tokens_parsed(ark_home):
    write(ark_home, minimal({"max_tokens": 8192}))
    assert config.load().agents["scribe"].max_tokens == 8192


@pytest.mark.parametrize("bad", [0, -1, "8192", 1.5])
def test_max_tokens_rejects_non_positive_ints(ark_home, bad):
    write(ark_home, minimal({"max_tokens": bad}))
    with pytest.raises(config.ConfigError, match="max_tokens"):
        config.load()
