"""A tool map that renames a tool must not erase its description.

servers.json maps upstream tools to {name, title, description}. Leaving
"description" out has to mean "keep the upstream one". It used to mean
"replace it with None": load_servers passed description=tc.get('description')
explicitly, and FastMCP's ToolTransformConfig.apply forwards every *set* field
(model_dump(exclude_unset=True)), so an explicit None reached
TransformedTool.from_tool as a real value. Seen on mcp.danenbros.com,
2026-11-10: every play_* tool went out with no description at all, so the
model calling them saw "Load the save state" and none of the instructions.
"""

from __future__ import annotations

import json

from fastmcp.tools import Tool

from mcp_gateway.gateway import load_servers


def _upstream_tool() -> Tool:
    def look() -> str:
        """Look at the TV: silent, saves nothing."""
        return "x"

    return Tool.from_function(look)


def _transformed(tmp_path, monkeypatch, mapping):
    p = tmp_path / "servers.json"
    p.write_text(json.dumps({"play": {"command": "x", "tools": {"look": mapping}}}))
    monkeypatch.setenv("MCP_SERVERS_FILE", str(p))
    cfg = load_servers()["play"]["tools"]["look"]
    return cfg.apply(_upstream_tool())


def test_rename_without_description_keeps_upstream(tmp_path, monkeypatch):
    t = _transformed(tmp_path, monkeypatch, {"name": "play_look", "title": "Look at the TV"})
    assert t.name == "play_look"
    assert t.title == "Look at the TV"
    assert t.description == "Look at the TV: silent, saves nothing."


def test_explicit_description_still_overrides(tmp_path, monkeypatch):
    t = _transformed(tmp_path, monkeypatch, {"name": "play_look", "description": "Mine."})
    assert t.description == "Mine."


def test_rename_without_title_keeps_upstream_title(tmp_path, monkeypatch):
    t = _transformed(tmp_path, monkeypatch, {"name": "play_look"})
    assert t.name == "play_look"
    assert t.description == "Look at the TV: silent, saves nothing."
