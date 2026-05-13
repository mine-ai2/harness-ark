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
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_migrate_is_idempotent(ark_home):
    db.init_db().close()
    conn = db.init_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


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
