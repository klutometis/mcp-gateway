"""Local entrypoint: no auth, localhost only, port 3100.

Run via ``gateway-local`` (installed as a console script) or
``uv run gateway-local``.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from mcp_gateway.gateway import create_gateway, load_servers, warm_proxies


def main() -> None:
    # Load .env file if it exists
    if Path(".env.local").exists():
        print("Loading environment from .env.local")
        load_dotenv(".env.local")
    elif Path(".env").exists():
        print("Loading environment from .env")
        load_dotenv(".env")

    print("Starting MCP Gateway in LOCAL mode (no auth, localhost only)")
    servers = load_servers()
    stdio_count = sum(1 for s in servers.values() if s["transport"] == "stdio")
    http_count = len(servers) - stdio_count
    print(
        f"Mounting {len(servers)} MCP servers "
        f"({stdio_count} stdio subprocess, {http_count} HTTP sidecar)"
    )

    gateway = create_gateway(auth=None, servers=servers)

    host = "127.0.0.1"
    port = int(os.environ.get("MCP_PORT", "3100"))
    print(f"MCP protocol: /mcp/*")
    print(f"Listening on {host}:{port}")

    # Stateless: no Mcp-Session-Id to go stale, so a gateway restart no longer
    # strands connected clients with "Session not found". Our tools are pure
    # request/response (no server-initiated push), so sessions buy us nothing.
    app = gateway.http_app(stateless_http=True)

    # Pre-warm stdio children at boot. FastMCP's stdio transport is lazy:
    # subprocesses (e.g. `npx chrome-devtools-mcp`) don't spawn until the
    # first tool call, so that first call pays a multi-second cold start
    # and often times out. Chain a startup step onto FastMCP's own
    # lifespan that forces each proxy to connect. Fire-and-forget so a
    # slow chrome spawn doesn't block the server from accepting requests.
    # See gateway.warm_proxies / plans/wss-shim-lifecycle.md.
    _fastmcp_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan_with_warmup(app_):
        async with _fastmcp_lifespan(app_):
            asyncio.create_task(warm_proxies(gateway))
            yield

    app.router.lifespan_context = lifespan_with_warmup

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
