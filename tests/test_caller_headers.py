"""A caller's Authorization must not reach a backend.

FastMCP proxies forward the caller's inbound HTTP headers upstream,
Authorization included. A backend with its own configured bearer then saw the
caller's token instead (the inbound header is lowercase, the configured one is
not, and both survived the merge): 401, and the backend's tools vanished.
On mcp.danenberg.ai that removed whatsapp_* for llm-multiplex. A backend with
no Authorization of its own received the caller's gateway token outright. See
_proxy_target in gateway.py.

The gateway is served over a real socket here, because the bug needs an
inbound HTTP request whose headers FastMCP can forward.
"""

from __future__ import annotations

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from mcp_gateway.gateway import create_gateway

from ._legacy_backend import make_legacy_app, serve


async def test_configured_bearer_wins_and_caller_token_stays_home():
    with_token, without_token = [], []
    async with (
        serve(make_legacy_app(bearer="backend-secret", seen=with_token)) as a,
        serve(make_legacy_app(seen=without_token)) as b,
    ):
        gw = create_gateway(
            servers={
                "tokened": {
                    "transport": "streamable-http",
                    "url": f"{a}/mcp",
                    "headers": {"Authorization": "Bearer backend-secret"},
                    "tools": {"echo": _rename("tokened_echo")},
                },
                "open": {
                    "transport": "streamable-http",
                    "url": f"{b}/mcp",
                    "tools": {"echo": _rename("open_echo")},
                },
            }
        )
        async with serve(gw.http_app()) as g:
            caller = StreamableHttpTransport(
                f"{g}/mcp", headers={"Authorization": "Bearer caller-token"}
            )
            async with Client(caller) as c:
                names = sorted(t.name for t in await c.list_tools())
                assert names == ["open_echo", "tokened_echo"]
                result = await c.call_tool("tokened_echo", {"text": "hi"})
                assert result.content[0].text == "hi"

    assert with_token and set(with_token) == {("Bearer backend-secret",)}
    assert without_token and set(without_token) == {()}


def _rename(name: str):
    from fastmcp.tools.tool_transform import ToolTransformConfig

    return ToolTransformConfig(name=name)
