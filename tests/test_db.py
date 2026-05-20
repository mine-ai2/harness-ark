from ark import db


def test_migrate_creates_schema(ark_home):
    conn = db.init_db()
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"sessions", "messages", "agent_state", "crons"} <= tables
    # Schema version tracks the highest applied migration in db.MIGRATIONS.
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.MIGRATIONS[-1][0]


def test_migrate_is_idempotent(ark_home):
    db.init_db().close()
    conn = db.init_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.MIGRATIONS[-1][0]


def test_tool_call_thought_signature_roundtrips(ark_home):
    """Gemini 2.5+ requires the per-call thought_signature to be echoed back.
    Verify the bytes survive serialization via base64."""
    from ark.types import ToolCall, message_from_row, message_to_row

    sig = b"\x00\x01\x02\xff\xfe binary blob"
    role, body = message_to_row(
        ToolCall(id="t1", name="fetch_url", input={"url": "x"}, thought_signature=sig)
    )
    assert role == "tool_call"
    assert "thought_signature_b64" in body
    restored = message_from_row(role, body)
    assert restored.thought_signature == sig

    # No signature → field absent in payload + read back as None
    _, body2 = message_to_row(ToolCall(id="t1", name="x", input={}))
    assert "thought_signature_b64" not in body2
    assert (
        message_from_row("tool_call", {"id": "t1", "name": "x", "input": {}}).thought_signature
        is None
    )


def test_tool_result_name_roundtrips(ark_home):
    """ToolResult.name is required by Google's API; verify it persists through
    message_to_row / message_from_row even when not all callers set it."""
    from ark.types import ToolResult, message_from_row, message_to_row

    role, body = message_to_row(
        ToolResult(call_id="x", output="ok", name="read_file")
    )
    assert role == "tool_result"
    assert body["name"] == "read_file"
    restored = message_from_row(role, body)
    assert restored.name == "read_file"

    # Default-empty name should NOT appear in the row (keeps old payloads tidy)
    _, body2 = message_to_row(ToolResult(call_id="x", output="ok"))
    assert "name" not in body2
    # And reading an old row (no name key) gives empty
    assert message_from_row("tool_result", {"call_id": "x", "output": "ok"}).name == ""


def test_messages_cascade_on_session_delete(ark_home):
    conn = db.init_db()
    conn.execute(
        "INSERT INTO sessions(id, agent_name, kind, created_at) VALUES (?,?,?,?)",
        ("s1", "scribe", "conversational", 0),
    )
    conn.execute(
        "INSERT INTO messages(session_id, seq, role, content_json, created_at) "
        "VALUES (?,?,?,?,?)",
        ("s1", 0, "user", "{}", 0),
    )
    conn.execute("DELETE FROM sessions WHERE id = ?", ("s1",))
    remaining = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert remaining == 0
