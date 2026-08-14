"""An empty tool surface must not be cached forever.

Regression test for the 2026-08-12 chrome outage: danenberg-central
rebooted before the laptop's SSH RemoteForward was back, so at warm-up
the chrome backing answered ``list_tools`` with zero tools. That empty
registry was cached for the lifetime of the process, so chrome_* stayed
missing for eleven hours even after the tunnel healed. ``harness mcp
refresh`` could not fix it -- the gateway just re-served its cached
nothing -- and only a restart brought the tools back.

A backing with genuinely zero tools is indistinguishable from a backing
that is briefly not ready, so we treat empty as "ask again next time".
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import FastMCP

from mcp_gateway.multi_instance import MultiInstanceProxy


class _TunnelBacking:
    """A backing whose tools only appear once its tunnel is up.

    Mimics the forwarded chrome helper: reachable enough to answer,
    but with nothing to offer until the far end is listening.
    """

    def __init__(self) -> None:
        self.up = False
        self.list_calls = 0
        self._real = FastMCP("chrome-helper")

        @self._real.tool
        def list_pages() -> str:
            """List open tabs."""
            return "tab-1"

    async def list_tools(self) -> list[Any]:
        self.list_calls += 1
        if not self.up:
            return []
        return list(await self._real.list_tools())

    async def call_tool(self, name: str, kwargs: dict[str, Any]) -> Any:
        return await self._real.call_tool(name, kwargs)


@pytest.mark.asyncio
class TestEmptyRegistryNotCached:
    async def test_tools_appear_once_the_tunnel_comes_up(self) -> None:
        backing = _TunnelBacking()
        wrapper = MultiInstanceProxy(
            "chrome",
            instances={"personal": backing},
            param_name="profile",
        )

        # Boot: tunnel still down, nothing to serve.
        assert await wrapper.list_tools() == []

        # Tunnel heals. The next list must reflect that, without a restart.
        backing.up = True
        names = [t.name for t in await wrapper.list_tools()]
        assert names == ["list_pages"], (
            "empty tool surface was cached; the gateway would keep serving "
            "nothing until the process restarts"
        )

    async def test_tool_is_callable_after_recovery(self) -> None:
        backing = _TunnelBacking()
        wrapper = MultiInstanceProxy(
            "chrome",
            instances={"personal": backing},
            param_name="profile",
        )
        await wrapper.list_tools()  # cache the empty surface

        backing.up = True
        tool = await wrapper.get_tool("list_pages")
        assert tool is not None, "get_tool must also retry, not just list_tools"

    async def test_non_empty_surface_is_cached(self) -> None:
        """The retry must not turn into a list_tools on every call."""
        backing = _TunnelBacking()
        backing.up = True
        wrapper = MultiInstanceProxy(
            "chrome",
            instances={"personal": backing},
            param_name="profile",
        )

        await wrapper.list_tools()
        after_first = backing.list_calls
        await wrapper.list_tools()
        await wrapper.get_tool("list_pages")

        assert backing.list_calls == after_first, (
            "a populated registry should be built once and reused"
        )
