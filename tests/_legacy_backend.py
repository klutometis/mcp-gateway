"""A minimal handshake-era MCP backend over streamable HTTP, for tests.

Answers the way whatsapp-mcp does: 404 to anything without a session, and
401 unless a request carries exactly the bearer it wants. Records every
request's Authorization headers (all of them) in ``seen``.
"""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager

import uvicorn


def make_legacy_app(*, bearer: str | None = None, seen: list | None = None):
    async def app(scope, receive, send):
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
        # Every Authorization header, not the dict's last one: a request can
        # carry two, and a real server reads the first.
        auths = tuple(v.decode() for k, v in scope["headers"] if k.lower() == b"authorization")
        if seen is not None:
            seen.append(auths)

        async def reply(status, body=None, extra=()):
            payload = b"" if body is None else json.dumps(body).encode()
            hdrs = [(b"content-type", b"application/json"), *extra]
            await send({"type": "http.response.start", "status": status, "headers": hdrs})
            await send({"type": "http.response.body", "body": payload})

        if bearer is not None and auths != (f"Bearer {bearer}",):
            return await reply(401)
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


    return app


@asynccontextmanager
async def serve(app):
    """Serve an ASGI app on a free localhost port; yield its base URL."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
