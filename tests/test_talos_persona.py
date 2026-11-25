"""The deployed Talos persona (deploy/agents/talos/session_context.md).

mine-capstone#1420 rewrote it around finish-or-ask, truth from tool results
and the identity line. These pins keep the rules — and the two lines the
file-handling flow depends on — from being edited away. The same pin phrases
are asserted on the MineAI side (protocol + charter)."""

from pathlib import Path

import pytest

PERSONA = (
    Path(__file__).resolve().parents[1]
    / "deploy" / "agents" / "talos" / "session_context.md"
)


@pytest.fixture(scope="module")
def text():
    return PERSONA.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize(
    "phrase",
    [
        # identity (decision T7)
        "You are Talos, built by MineAI",
        "never name the underlying model",
        "a hosted model MineAI selects",
        # finish-or-ask
        "exactly one of three ways",
        "Never end on a promise",
        "chat.ask_user",
        # truth from tool results
        "A write is done only when a tool result says so",
        "quote the id",
        # errors: correct once, else ask
        "`error.message`",
        "correct the call once",
        # totals in code
        "Compute totals",
        "show the rows they came from",
        # uploads
        "`list_uploads()`",
        "never guess its contents",
        # run_command foreground rule (mine-capstone#1418)
        "runs in the foreground",
        "`a & b & wait`",
        # kept from the previous persona
        "Your working directory is your file home",
        "PROJECT ROOT",
        '`share_with_client("<relative path>")`',
        "`embed_block`",
        "`mineai_call_tool(name, arguments)`",
    ],
)
def test_persona_keeps_its_rules(text, phrase):
    assert phrase in _flat(text)


def test_persona_does_not_name_a_vendor_or_model(text):
    lowered = text.lower()
    for name in ("kimi", "moonshot", "openrouter", "anthropic", "claude", "gpt", "gemini"):
        assert name not in lowered, name


def test_persona_stays_small(text):
    # Sent with every session: keep it well under the prompt's other blocks.
    assert len(text.encode("utf-8")) < 6144
