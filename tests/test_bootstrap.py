import json

from ark import bootstrap, config, paths


def make_config(ark_home):
    (ark_home / "config.json").write_text(
        json.dumps(
            {
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
        )
    )
    return config.load()


def test_bootstrap_creates_structure(ark_home):
    cfg = make_config(ark_home)
    bootstrap.bootstrap(cfg)
    assert paths.agents_dir().is_dir()
    assert paths.skills_dir().is_dir()
    agent_root = paths.agent_dir("scribe")
    assert (agent_root / "session_context.md").is_file()
    assert (agent_root / "heartbeat_prompt.md").is_file()
    assert (agent_root / "skills").is_dir()
    assert (agent_root / "workspace").is_dir()


def test_bootstrap_does_not_overwrite(ark_home):
    cfg = make_config(ark_home)
    bootstrap.bootstrap(cfg)
    ctx = paths.agent_dir("scribe") / "session_context.md"
    ctx.write_text("custom user content")
    bootstrap.bootstrap(cfg)
    assert ctx.read_text() == "custom user content"


def test_bootstrap_idempotent(ark_home):
    cfg = make_config(ark_home)
    bootstrap.bootstrap(cfg)
    bootstrap.bootstrap(cfg)
