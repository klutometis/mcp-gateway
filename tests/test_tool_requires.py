"""A tool that ``requires`` a capability is hidden on hosts without it.

The case this exists for: deploy/common/servers.json is shared by the laptop
(attachment offload on) and danenberg-central (offload off). WhatsApp's
``download_media`` returns raw bytes, so exposing it where offload is off
hands a model ~350k tokens of base64 per photo. With
``"requires": "attachment_offload"`` the shared file can list it and only
hosts that would catch the bytes serve it.
"""

from __future__ import annotations

import json

import pytest

from mcp_gateway.gateway import load_servers

_OFFLOAD_ENV = ("MCP_ATTACHMENT_OFFLOAD", "MCP_ATTACHMENT_CONFIG")


def _servers(tmp_path, monkeypatch, tools):
    p = tmp_path / "servers.json"
    p.write_text(json.dumps({"whatsapp": {"command": "x", "tools": tools}}))
    monkeypatch.setenv("MCP_SERVERS_FILE", str(p))
    return load_servers()["whatsapp"]["tools"]


_TOOLS = {
    "read_thread": {"name": "whatsapp_read_thread"},
    "download_media": {"name": "whatsapp_download_media", "requires": "attachment_offload"},
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _OFFLOAD_ENV:
        monkeypatch.delenv(k, raising=False)


def test_hidden_when_offload_is_off(tmp_path, monkeypatch):
    tools = _servers(tmp_path, monkeypatch, _TOOLS)
    assert set(tools) == {"read_thread"}


def test_exposed_when_offload_is_on(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_ATTACHMENT_OFFLOAD", "1")
    tools = _servers(tmp_path, monkeypatch, _TOOLS)
    assert set(tools) == {"read_thread", "download_media"}
    assert tools["download_media"].name == "whatsapp_download_media"


def test_config_file_enabled_counts_as_on(tmp_path, monkeypatch):
    # deploy/common/attachments.json says "enabled": true, so pointing
    # MCP_ATTACHMENT_CONFIG at it is enough -- the same predicate the
    # middleware uses, so the two cannot disagree.
    cfg = tmp_path / "attachments.json"
    cfg.write_text(json.dumps({"enabled": True}))
    monkeypatch.setenv("MCP_ATTACHMENT_CONFIG", str(cfg))
    assert "download_media" in _servers(tmp_path, monkeypatch, _TOOLS)


def test_env_off_beats_config_file_on(tmp_path, monkeypatch):
    cfg = tmp_path / "attachments.json"
    cfg.write_text(json.dumps({"enabled": True}))
    monkeypatch.setenv("MCP_ATTACHMENT_CONFIG", str(cfg))
    monkeypatch.setenv("MCP_ATTACHMENT_OFFLOAD", "0")
    assert "download_media" not in _servers(tmp_path, monkeypatch, _TOOLS)


def test_unknown_capability_hides_the_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_ATTACHMENT_OFFLOAD", "1")
    tools = _servers(
        tmp_path,
        monkeypatch,
        {"t": {"name": "t", "requires": ["attachment_offload", "telepathy"]}},
    )
    assert tools == {}


def test_no_requires_is_unaffected(tmp_path, monkeypatch):
    tools = _servers(tmp_path, monkeypatch, {"read_thread": {"name": "whatsapp_read_thread"}})
    assert set(tools) == {"read_thread"}
