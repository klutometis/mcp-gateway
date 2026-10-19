"""A legacy-only backend must stay visible to a modern-era client.

FastMCP 4 proxies copy the front connection's protocol era onto the backend
by default. A fastmcp>=4 Client speaks the modern 2026-07-28 era, so the
gateway reached every backend in that era, with no initialize handshake. A
backend that only speaks the handshake era answered 404. The SDK reports that
as -32601, which ProxyProvider._list_tools reads as "no tools", so the
backend vanished without an error: whatsapp-mcp showed 0 tools instead of 4,
and chrome-cdp-mcp 0 instead of 12. See create_gateway in gateway.py.

The backend here is a minimal handshake-only streamable-HTTP server, over a
real socket, that answers the way whatsapp-mcp did: 404 to anything without a
session.
"""

from __future__ import annotations

import inspect

import pytest
from fastmcp import Client

from mcp_gateway.gateway import create_gateway

from ._legacy_backend import make_legacy_app, serve


@pytest.fixture
async def legacy_url():
    async with serve(make_legacy_app()) as base:
        yield f"{base}/mcp"


def _modes() -> list[str | None]:
    """Client eras to try. fastmcp 3.x Clients have no `mode`."""
    if "mode" not in inspect.signature(Client.__init__).parameters:
        return [None]
    return ["auto", "legacy"]  # "auto" is the default: the modern era


@pytest.mark.parametrize("mode", _modes())
async def test_legacy_backend_lists_and_calls_from_any_era(legacy_url, mode):
    gw = create_gateway(
        servers={
            "legacy": {
                "transport": "streamable-http",
                "url": legacy_url,
                "tools": "*",
            }
        }
    )
    kwargs = {} if mode is None else {"mode": mode}
    async with Client(gw, **kwargs) as c:
        names = [t.name for t in await c.list_tools()]
        assert names == ["echo"], f"client mode={mode}: backend vanished"
        result = await c.call_tool("echo", {"text": "hi"})
        assert result.content[0].text == "hi"
