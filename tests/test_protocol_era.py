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

import asyncio
import inspect
import json
import socket

import pytest
import uvicorn
from fastmcp import Client

from mcp_gateway.gateway import create_gateway


async def _legacy_app(scope, receive, send):
    """Handshake-era MCP over streamable HTTP, JSON responses only."""
    if scope["type"] == "lifespan":
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}

    async def reply(status, body=None, extra=()):
        payload = b"" if body is None else json.dumps(body).encode()
        hdrs = [(b"content-type", b"application/json"), *extra]
        await send({"type": "http.response.start", "status": status, "headers": hdrs})
        await send({"type": "http.response.body", "body": payload})

    if scope["method"] == "GET":
        return await reply(405)  # no server-to-client stream; allowed by spec
    if scope["method"] == "DELETE":
        return await reply(200)
    raw = b""
    while True:
        msg = await receive()
        raw += msg.get("body", b"")
        if not msg.get("more_body"):
            break
    req = json.loads(raw)
    method, rid = req.get("method"), req.get("id")
    if method == "initialize":
        return await reply(
            200,
            {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "legacy", "version": "0"},
                },
            },
            extra=[(b"mcp-session-id", b"legacy-session")],
        )
    if headers.get("mcp-session-id") != "legacy-session":
        return await reply(404)  # what whatsapp-mcp answers
    if rid is None:
        return await reply(202)  # a notification
    if method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "echo",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                    },
                }
            ]
        }
    elif method == "tools/call":
        text = req["params"]["arguments"]["text"]
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    else:
        result = {}
    return await reply(200, {"jsonrpc": "2.0", "id": rid, "result": result})


@pytest.fixture
async def legacy_url():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(_legacy_app, log_level="warning", lifespan="on")
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    await task


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
