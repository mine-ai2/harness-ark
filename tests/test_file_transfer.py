"""End-to-end: upload + download + share_with_client + provider folding."""

import asyncio
import io

import pytest
from fastapi.testclient import TestClient

from ark import broker, db, runtime, tools, workspace as ws
from ark.config import AgentConfig, Config, ProviderConfig, ServerConfig
from ark.provider import to_anthropic_messages, to_openai_messages
from ark.server import create_app
from ark.tools import ToolContext
from ark.types import SharedFile, UploadMessage


def make_config(workspace):
    return Config(
        server=ServerConfig(host="127.0.0.1", port=7777, auth_secret="x"),
        providers={"anthropic": ProviderConfig(provider_type="anthropic", api_key="k")},
        tools={},
        agents={
            "scribe": AgentConfig(
                name="scribe",
                provider="anthropic",
                model="m",
                workspace=workspace,
            )
        },
    )


# ---------------------------------------------------------------------------
# REST: upload + list + download
# ---------------------------------------------------------------------------


def test_rest_upload_list_download(ark_home, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg = make_config(workspace)
    app = create_app(cfg)
    client = TestClient(app)
    headers = {"Authorization": "Bearer x"}

    # Create a session.
    r = client.post("/agents/scribe/sessions", headers=headers)
    assert r.status_code == 200
    sid = r.json()["id"]

    # Upload a file.
    r = client.post(
        f"/agents/scribe/sessions/{sid}/uploads",
        headers=headers,
        files={"file": ("hello.txt", io.BytesIO(b"hello world"), "text/plain")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "uploads/hello.txt"
    assert body["size"] == 11
    assert (workspace / "uploads" / "hello.txt").read_bytes() == b"hello world"

    # Re-upload same name → auto-suffixed.
    r = client.post(
        f"/agents/scribe/sessions/{sid}/uploads",
        headers=headers,
        files={"file": ("hello.txt", io.BytesIO(b"second"), "text/plain")},
    )
    assert r.status_code == 200
    assert r.json()["path"] == "uploads/hello-2.txt"

    # List shows both, newest first.
    r = client.get(f"/agents/scribe/sessions/{sid}/uploads", headers=headers)
    assert r.status_code == 200
    names = [e["path"] for e in r.json()]
    assert "uploads/hello.txt" in names
    assert "uploads/hello-2.txt" in names

    # Download the original.
    r = client.get("/agents/scribe/files/uploads/hello.txt", headers=headers)
    assert r.status_code == 200
    assert r.content == b"hello world"

    # Upload was persisted as an UploadMessage in session history.
    history = runtime.load_history(app.state.conn, sid)
    upload_msgs = [m for m in history if isinstance(m, UploadMessage)]
    assert len(upload_msgs) == 2
    assert upload_msgs[0].original_name == "hello.txt"
    assert upload_msgs[0].size == 11


def test_rest_upload_size_cap(ark_home, tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg = make_config(workspace)
    # Shrink the cap so we don't have to ship 25MB through the test client.
    import ark.server as srv

    monkeypatch.setattr(srv, "UPLOAD_MAX_BYTES", 10)
    app = create_app(cfg)
    client = TestClient(app)
    headers = {"Authorization": "Bearer x"}
    r = client.post("/agents/scribe/sessions", headers=headers)
    sid = r.json()["id"]
    r = client.post(
        f"/agents/scribe/sessions/{sid}/uploads",
        headers=headers,
        files={"file": ("big.bin", io.BytesIO(b"x" * 100), "application/octet-stream")},
    )
    assert r.status_code == 413
    assert not (workspace / "uploads" / "big.bin").exists()


def test_rest_download_never_leaks_external_file(ark_home, tmp_path):
    """Strong invariant: regardless of the URL encoding tried, an attacker
    must not be able to read a file outside the workspace via the download
    endpoint. ws.resolve() is the actual guard — this test exists so any
    future refactor that moves or skips it fails loudly.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    secret = tmp_path / "secret.txt"
    secret_content = "SHOULD-NEVER-LEAK-OUTSIDE-WORKSPACE"
    secret.write_text(secret_content)

    app = create_app(make_config(workspace))
    client = TestClient(app)
    headers = {"Authorization": "Bearer x"}

    attempts = [
        "../secret.txt",
        "..%2Fsecret.txt",
        "%2e%2e%2Fsecret.txt",
        "a/..%2F..%2Fsecret.txt",
        "subdir/../../secret.txt",
        "/" + str(secret),  # absolute path attempt
    ]
    for path in attempts:
        r = client.get(f"/agents/scribe/files/{path}", headers=headers)
        assert r.status_code in (400, 404), (path, r.status_code)
        assert secret_content not in r.text, f"leaked via {path!r}: {r.text}"


def test_rest_download_symlink_escape_blocked(ark_home, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("EXTERNAL")
    (workspace / "trap").symlink_to(outside)
    app = create_app(make_config(workspace))
    client = TestClient(app)
    headers = {"Authorization": "Bearer x"}
    r = client.get("/agents/scribe/files/trap", headers=headers)
    assert r.status_code == 400
    assert "EXTERNAL" not in r.text


# ---------------------------------------------------------------------------
# Agent tools: list_uploads + share_with_client
# ---------------------------------------------------------------------------


def make_ctx(conn, workspace, agent_name="scribe", session_id="s"):
    agent = AgentConfig(
        name=agent_name,
        provider="anthropic",
        model="m",
        workspace=workspace,
    )
    return ToolContext(
        conn=conn,
        config=make_config(workspace),
        agent=agent,
        session_id=session_id,
        cwd=workspace,
        loaded_skills=set(),
    )


def test_tool_list_uploads(ark_home, tmp_path):
    conn = db.init_db()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = make_ctx(conn, workspace)
    output, err = asyncio.run(tools.execute("list_uploads", {}, ctx=ctx))
    assert err is False
    assert "(no uploads)" in output

    ws.ensure_uploads_dir(workspace)
    (workspace / "uploads" / "a.txt").write_text("hi")
    output, err = asyncio.run(tools.execute("list_uploads", {}, ctx=ctx))
    assert err is False
    assert "uploads/a.txt" in output


@pytest.mark.asyncio
async def test_tool_share_with_client_persists_and_publishes(ark_home, tmp_path):
    conn = db.init_db()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sid = runtime.create_session(conn, "scribe", "conversational")
    ctx = make_ctx(conn, workspace, session_id=sid)

    chart = workspace / "chart.png"
    chart.write_bytes(b"\x89PNG fake")

    queue = broker.subscribe(sid)
    try:
        output, err = await tools.execute(
            "share_with_client",
            {"path": "chart.png", "description": "Q4"},
            ctx=ctx,
        )
        assert err is False, output

        # Persisted as SharedFile.
        history = runtime.load_history(conn, sid)
        shared = [m for m in history if isinstance(m, SharedFile)]
        assert len(shared) == 1
        assert shared[0].path == "chart.png"
        assert shared[0].description == "Q4"
        assert shared[0].size == len(b"\x89PNG fake")

        # Broker delivered file_available.
        evt = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert evt["type"] == "file_available"
        assert evt["path"] == "chart.png"
        assert evt["description"] == "Q4"
        # session_id + agent_name must be tagged so the unified /events WS
        # firehose can route this to clients.
        assert evt["session_id"] == sid
        assert evt["agent_name"] == "scribe"
    finally:
        broker.unsubscribe(sid, queue)


def test_tool_share_with_client_rejects_outside_workspace(ark_home, tmp_path):
    conn = db.init_db()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sid = runtime.create_session(conn, "scribe", "conversational")
    ctx = make_ctx(conn, workspace, session_id=sid)
    output, err = asyncio.run(
        tools.execute("share_with_client", {"path": "../escape.txt"}, ctx=ctx)
    )
    assert err is True
    assert "escapes" in output.lower() or "not a file" in output.lower()


def test_tool_share_with_client_rejects_missing(ark_home, tmp_path):
    conn = db.init_db()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sid = runtime.create_session(conn, "scribe", "conversational")
    ctx = make_ctx(conn, workspace, session_id=sid)
    output, err = asyncio.run(
        tools.execute("share_with_client", {"path": "nope.png"}, ctx=ctx)
    )
    assert err is True
    assert "not a file" in output.lower()


# ---------------------------------------------------------------------------
# Provider folding for the new message types
# ---------------------------------------------------------------------------


def test_anthropic_folds_upload_as_user_text():
    from ark.types import UserText

    out = to_anthropic_messages(
        [
            UploadMessage(path="uploads/r.pdf", original_name="report.pdf", size=42),
            UserText(text="summarize please"),
        ]
    )
    assert out[0]["role"] == "user"
    assert "report.pdf" in out[0]["content"]
    assert "uploads/r.pdf" in out[0]["content"]
    assert out[1] == {"role": "user", "content": "summarize please"}


def test_anthropic_folds_shared_file_into_assistant():
    from ark.types import AssistantText

    out = to_anthropic_messages(
        [
            AssistantText(text="here you go"),
            SharedFile(path="chart.png", description="Q4 summary", size=100),
        ]
    )
    assert out[0]["role"] == "assistant"
    text_blocks = [b for b in out[0]["content"] if b["type"] == "text"]
    assert any("here you go" in b["text"] for b in text_blocks)
    assert any("chart.png" in b["text"] and "Q4 summary" in b["text"] for b in text_blocks)


def test_openai_folds_upload_and_shared_file():
    from ark.types import AssistantText, UserText

    out = to_openai_messages(
        "system",
        [
            UploadMessage(path="uploads/r.pdf", original_name="report.pdf", size=42),
            UserText(text="summarize"),
            AssistantText(text="done"),
            SharedFile(path="summary.md", size=200),
        ],
    )
    # system + user(upload) + user(text) + assistant(text + shared)
    assert out[0] == {"role": "system", "content": "system"}
    assert "report.pdf" in out[1]["content"]
    assert out[2] == {"role": "user", "content": "summarize"}
    assert out[3]["role"] == "assistant"
    assert "done" in out[3]["content"]
    assert "summary.md" in out[3]["content"]
