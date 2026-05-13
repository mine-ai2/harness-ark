import asyncio
import textwrap
from unittest.mock import MagicMock

from ark import skills, tools
from ark.config import AgentConfig
from ark.tools import ToolContext


def write_skill(home_path, name, body):
    skills_dir = home_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / f"{name}.py").write_text(textwrap.dedent(body))


def make_ctx(tmp_path, *, agent_name="scribe", loaded=()):
    cwd = tmp_path / "ws"
    cwd.mkdir(exist_ok=True)
    agent = AgentConfig(
        name=agent_name,
        provider="anthropic",
        model="m",
        workspace=cwd,
    )
    return ToolContext(
        conn=MagicMock(),
        config=MagicMock(),
        agent=agent,
        session_id="s",
        cwd=cwd,
        loaded_skills=set(loaded),
    )


def test_discover_skill_with_tool_decorator(ark_home):
    write_skill(
        ark_home,
        "notes",
        """
        \"\"\"Capture and retrieve quick notes.\"\"\"

        from ark.skills import tool

        @tool
        def add_note(title: str, body: str) -> str:
            \"\"\"Append a note to the notebook.\"\"\"
            return f"saved: {title}"

        @tool
        def count_notes() -> str:
            \"\"\"Return the number of notes.\"\"\"
            return "0"
        """,
    )
    skills.discover([])
    skill = skills.get("notes")
    assert skill is not None
    assert skill.description == "Capture and retrieve quick notes."
    tool_names = {t.schema.name for t in skill.tools}
    assert tool_names == {"add_note", "count_notes"}
    add_note_schema = next(t for t in skill.tools if t.schema.name == "add_note").schema
    assert add_note_schema.input_schema["required"] == ["title", "body"]
    assert add_note_schema.input_schema["properties"]["title"]["type"] == "string"


def test_skill_tool_must_be_loaded_to_appear_in_active_schemas(ark_home, tmp_path):
    write_skill(
        ark_home,
        "math",
        """
        \"\"\"Basic math.\"\"\"

        from ark.skills import tool

        @tool
        def double(x: int) -> int:
            return x * 2
        """,
    )
    skills.discover([])
    ctx_unloaded = make_ctx(tmp_path)
    names_unloaded = {s.name for s in tools.active_schemas(ctx_unloaded.agent, ctx_unloaded.loaded_skills)}
    assert "double" not in names_unloaded
    # Now load and check again.
    ctx_loaded = make_ctx(tmp_path, loaded=("math",))
    names_loaded = {s.name for s in tools.active_schemas(ctx_loaded.agent, ctx_loaded.loaded_skills)}
    assert "double" in names_loaded


def test_load_skill_meta_tool(ark_home, tmp_path):
    write_skill(
        ark_home,
        "greet",
        """
        \"\"\"Say hello.\"\"\"
        from ark.skills import tool

        @tool
        def hello(name: str) -> str:
            return f"hi {name}"
        """,
    )
    skills.discover([])
    ctx = make_ctx(tmp_path)
    output, err = asyncio.run(tools.execute("load_skill", {"name": "greet"}, ctx=ctx))
    assert err is False
    assert "greet" in ctx.loaded_skills
    # Now the tool is callable
    output, err = asyncio.run(tools.execute("hello", {"name": "world"}, ctx=ctx))
    assert err is False
    assert output == "hi world"


def test_list_skills_marks_loaded(ark_home, tmp_path):
    write_skill(ark_home, "a", '"""Skill a."""\nfrom ark.skills import tool\n@tool\ndef f():\n    return "ok"\n')
    write_skill(ark_home, "b", '"""Skill b."""\nfrom ark.skills import tool\n@tool\ndef g():\n    return "ok"\n')
    skills.discover([])
    ctx = make_ctx(tmp_path, loaded=("a",))
    output, err = asyncio.run(tools.execute("list_skills", {}, ctx=ctx))
    assert err is False
    assert "a [loaded]" in output
    assert "b —" in output and "b [loaded]" not in output
