"""Skill discovery + the `@tool` decorator.

A skill is a Python module under ~/.ark/skills/ or ~/.ark/agents/<name>/skills/.
The module's docstring is the skill description shown in the manifest. Any
function decorated with `@tool` is exposed as a tool once the skill is loaded.

Skill tools are kwargs-style. They can access the live agent context via
`ark.tools.current_context()`.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Union, get_args, get_origin

from . import paths
from .types import ToolSchema


# ---------------------------------------------------------------------------
# Decorator + registry
# ---------------------------------------------------------------------------


_TOOL_ATTR = "__ark_tool_schema__"


def tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorate a function as a skill tool.

    The tool name is the function name. The schema is derived from type hints
    and the docstring (first paragraph used as description).
    """

    setattr(fn, _TOOL_ATTR, _build_schema(fn))
    return fn


def _build_schema(fn: Callable[..., Any]) -> ToolSchema:
    sig = inspect.signature(fn)
    try:
        hints = typing.get_type_hints(fn)
    except Exception:  # noqa: BLE001
        hints = {}
    properties: dict[str, dict[str, Any]] = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        if pname == "self":
            continue
        hint = hints.get(pname, str)
        properties[pname] = _hint_to_schema(hint)
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    doc = (fn.__doc__ or "").strip()
    description = doc.split("\n\n", 1)[0].strip() if doc else ""
    schema_obj: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema_obj["required"] = required
    return ToolSchema(name=fn.__name__, description=description, input_schema=schema_obj)


def _hint_to_schema(hint: Any) -> dict[str, Any]:
    origin = get_origin(hint)
    if origin is Union:
        non_none = [a for a in get_args(hint) if a is not type(None)]  # noqa: E721
        if len(non_none) == 1:
            return _hint_to_schema(non_none[0])
    if hint is str or hint is None:
        return {"type": "string"}
    if hint is int:
        return {"type": "integer"}
    if hint is float:
        return {"type": "number"}
    if hint is bool:
        return {"type": "boolean"}
    if origin in (list, tuple, set):
        return {"type": "array"}
    if origin is dict:
        return {"type": "object"}
    return {"type": "string"}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@dataclass
class SkillTool:
    schema: ToolSchema
    fn: Callable[..., Any]
    is_async: bool


@dataclass
class Skill:
    name: str
    description: str
    tools: list[SkillTool]


_skills: dict[str, Skill] = {}


def discover(agent_names: list[str] | None = None) -> None:
    """Re-scan global + per-agent skill directories and (re)populate the registry."""

    _skills.clear()
    _load_dir(paths.skills_dir(), scope="global")
    for name in agent_names or []:
        _load_dir(paths.agent_skills_dir(name), scope=f"agent:{name}")


def _load_dir(dir_path: Path, scope: str) -> None:
    if not dir_path.is_dir():
        return
    for py in sorted(dir_path.glob("*.py")):
        if py.name.startswith("_"):
            continue
        skill_name = py.stem
        # Per-agent skills shadow globals of the same name.
        try:
            module = _import_skill(py, f"ark_skill__{scope.replace(':', '_')}__{skill_name}")
        except Exception as e:  # noqa: BLE001
            print(f"[skills] failed to load {py}: {e}", file=sys.stderr)
            continue
        tools: list[SkillTool] = []
        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            schema = getattr(obj, _TOOL_ATTR, None)
            if schema is None or not callable(obj):
                continue
            tools.append(
                SkillTool(
                    schema=schema, fn=obj, is_async=inspect.iscoroutinefunction(obj)
                )
            )
        description = (module.__doc__ or "").strip().split("\n\n", 1)[0]
        _skills[skill_name] = Skill(name=skill_name, description=description, tools=tools)


def _import_skill(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def get(name: str) -> Skill | None:
    return _skills.get(name)


def manifest() -> list[tuple[str, str]]:
    return [(s.name, s.description) for s in _skills.values()]


def all_skills() -> list[Skill]:
    return list(_skills.values())
