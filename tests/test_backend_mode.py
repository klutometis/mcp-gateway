"""`"mode": "legacy"` keeps a discover-intolerant backend's tools visible.

The gateway opens every backend in "auto" mode (see create_gateway): probe
`server/discover`, fall back to the initialize handshake on an error. A
backend built on rmcp, the Rust SDK, never answers the probe; it exits. Its
tools then vanish without an error: vm.danenberg.ai's tmux-mcp-rs showed
"warmed 'tmux': 0 tools available" after a 2026-11 relock. A server entry's
`"mode": "legacy"` skips the probe for that backend only.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.server import create_proxy as fastmcp_create_proxy

from mcp_gateway.gateway import _mode_override, create_gateway

BACKEND = str(Path(__file__).with_name("_discover_intolerant_backend.py"))
HAS_ERAS = "mode" in inspect.signature(fastmcp_create_proxy).parameters


def _servers(**extra) -> dict:
    return {
        "rmcp": {
            "transport": "stdio",
            "command": sys.executable,
            "args": [BACKEND],
            "tools": "*",
            **extra,
        }
    }


# The gateway's own tools (view_image, ...) come and go with the environment's
# attachment settings, so these look for the backend's tool, not an exact list.
async def _tool_names(gw) -> list[str]:
    async with Client(gw) as c:
        return [t.name for t in await c.list_tools()]


async def test_legacy_mode_lists_and_calls():
    gw = create_gateway(servers=_servers(mode="legacy"))
    async with Client(gw) as c:
        assert "echo" in [t.name for t in await c.list_tools()]
        result = await c.call_tool("echo", {"text": "hi"})
        assert result.content[0].text == "hi"


@pytest.mark.skipif(not HAS_ERAS, reason="fastmcp 3.x always uses the handshake")
async def test_without_mode_the_backend_vanishes():
    # The failure this key exists for, pinned so a fastmcp that learns to
    # survive it shows up here (and the key can be retired).
    assert "echo" not in await _tool_names(create_gateway(servers=_servers()))


def test_mode_override_kwargs():
    def with_mode(target, *, name=None, mode=None): ...

    def without_mode(target, *, name=None): ...

    assert _mode_override(with_mode, {}) == {}
    assert _mode_override(with_mode, {"mode": "legacy"}) == {"mode": "legacy"}
    assert _mode_override(without_mode, {"mode": "legacy"}) == {}
    with pytest.raises(ValueError, match="'mode' must be a string"):
        _mode_override(with_mode, {"mode": True})
