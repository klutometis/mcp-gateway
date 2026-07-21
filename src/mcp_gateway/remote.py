"""Remote/production entrypoint: Google OAuth, auth middleware, static pages.

Run via ``gateway-remote`` (installed as a console script) or
``uv run gateway-remote``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from starlette.middleware import Middleware
from starlette.responses import FileResponse
from starlette.routing import Route

from mcp_gateway.gateway import (
    StableRefreshGoogleProvider,
    create_gateway,
    load_servers,
    warm_proxies,
)


def main() -> None:
    # Load .env file if it exists
    if Path(".env.local").exists():
        print("Loading environment from .env.local")
        load_dotenv(".env.local")
    elif Path(".env").exists():
        print("Loading environment from .env")
        load_dotenv(".env")

    # Ensure our diagnostic logger.info calls in mcp_gateway.gateway
    # actually surface in container logs. Uvicorn configures the root
    # logger to WARNING by default, so without this our INFO lines for
    # exchange_refresh_token / get_client would be invisible.
    # See plans/refresh-token-stickiness-take-2.md (step 1).
    logging.basicConfig(
        level=os.environ.get("MCP_GATEWAY_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
        force=True,
    )
    logging.getLogger("mcp_gateway").setLevel(logging.INFO)

    # Get configuration from environment
    client_id = os.environ.get("MCP_OIDC_CLIENT_ID")
    client_secret = os.environ.get("MCP_OIDC_CLIENT_SECRET")
    domain = os.environ.get("MCP_DOMAIN")

    # Validate required environment variables
    missing = []
    if not client_id:
        missing.append("MCP_OIDC_CLIENT_ID")
    if not client_secret:
        missing.append("MCP_OIDC_CLIENT_SECRET")
    if not domain:
        missing.append("MCP_DOMAIN")

    if missing:
        print(
            f"Error: Missing required environment variables: {', '.join(missing)}",
            file=sys.stderr,
        )
        print("\nCreate a .env or .env.local file", file=sys.stderr)
        print("See .env.example for template", file=sys.stderr)
        sys.exit(1)

    # Narrow types after validation
    assert client_id is not None
    assert client_secret is not None
    assert domain is not None

    # Determine base URL (handle both localhost and production)
    if domain.startswith("http://") or domain.startswith("https://"):
        base_url = domain
    elif domain.startswith("localhost"):
        base_url = f"http://{domain}"
    else:
        base_url = f"https://{domain}"

    # Configure Google OAuth
    auth = StableRefreshGoogleProvider(
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
        extra_authorize_params={
            "access_type": "offline",
            "prompt": "consent",
            "scope": "openid email",
        },
    )

    print(f"Starting MCP Gateway with Google OAuth")
    print(f"Base URL: {base_url}")
    print(f"Access control: Google OAuth + consent screen")
    servers = load_servers()
    stdio_count = sum(1 for s in servers.values() if s["transport"] == "stdio")
    http_count = len(servers) - stdio_count
    print(
        f"Mounting {len(servers)} MCP servers "
        f"({stdio_count} stdio subprocess, {http_count} HTTP sidecar)"
    )

    gateway = create_gateway(auth=auth, servers=servers)

    # --- Auth middleware ---
    # Static OAuth landing pages ship inside the package so the gateway
    # works the same whether installed from source or from a wheel.
    static_dir = Path(__file__).parent / "static"

    allowed_users_env = os.environ.get("MCP_ALLOWED_USERS", "")
    allowed_users = set(
        email.strip().lower() for email in allowed_users_env.split(",") if email.strip()
    )

    middlewares: list[Middleware] = []

    # ------------------------------------------------------------------
    # /token diagnostic middleware
    # ------------------------------------------------------------------
    # Logs every POST /token attempt: inbound form (client_id, grant_type)
    # plus response status code. The 401s we see in production come from
    # mcp.server.auth.handlers.token.TokenHandler when
    # ClientAuthenticator.authenticate_request raises AuthenticationError
    # -- this middleware captures the before/after pair so we can pin down
    # which AuthenticationError branch is firing.
    #
    # See: plans/refresh-token-stickiness-take-2.md
    class TokenRequestLogMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if not (
                scope["type"] == "http"
                and scope.get("path") == "/token"
                and scope.get("method") == "POST"
            ):
                await self.app(scope, receive, send)
                return

            # Buffer the request body once so we can both parse it and pass
            # it through to the inner app unchanged.
            chunks: list[bytes] = []
            more = True
            while more:
                msg = await receive()
                if msg["type"] == "http.request":
                    chunks.append(msg.get("body", b""))
                    more = msg.get("more_body", False)
                else:
                    break
            body = b"".join(chunks)

            # Parse the form best-effort; don't crash on weird input.
            try:
                from urllib.parse import parse_qs
                form = parse_qs(body.decode("utf-8", errors="replace"))
                client_id = (form.get("client_id") or ["<missing>"])[0]
                grant_type = (form.get("grant_type") or ["<missing>"])[0]
            except Exception as exc:
                client_id = f"<parse-error: {exc}>"
                grant_type = "<parse-error>"

            captured_status: dict[str, int] = {"value": 0}

            async def replay_receive():
                # Replay the buffered body as a single chunk.
                if chunks:
                    chunks.clear()
                    return {
                        "type": "http.request",
                        "body": body,
                        "more_body": False,
                    }
                return await receive()

            async def capture_send(message):
                if message["type"] == "http.response.start":
                    captured_status["value"] = message.get("status", 0)
                await send(message)

            try:
                await self.app(scope, replay_receive, capture_send)
            finally:
                print(
                    f"[/token] client_id={client_id[:8] if client_id else '<empty>'} "
                    f"grant_type={grant_type} -> {captured_status['value']}",
                    file=sys.stderr,
                    flush=True,
                )

    middlewares.append(Middleware(TokenRequestLogMiddleware))

    # ------------------------------------------------------------------
    # /mcp 401 Www-Authenticate rewrite middleware
    # ------------------------------------------------------------------
    # FastMCP's default 401 invalid_token response carries an
    # error_description that literally says "clear authentication tokens
    # in your MCP client and reconnect. Your client should automatically
    # re-register and obtain new tokens." Well-behaved MCP clients honor
    # that instruction and trigger fresh DCR + /authorize on every
    # access-token expiry -- the DCR storm we see in production.
    #
    # This middleware rewrites the response on 401s to /mcp so the
    # description says "refresh your access token" instead. Clients
    # that handle refresh tokens correctly will then retry refresh
    # rather than re-register.
    #
    # See: plans/refresh-token-stickiness-take-2.md (step 3)
    class WwwAuthenticateRewriteMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if not (
                scope["type"] == "http"
                and scope.get("path", "").startswith("/mcp")
            ):
                await self.app(scope, receive, send)
                return

            async def rewrite_send(message):
                if (
                    message["type"] == "http.response.start"
                    and message.get("status") == 401
                ):
                    new_headers = []
                    for name, value in message.get("headers", []):
                        if name.lower() == b"www-authenticate":
                            # Replace the verbose "please re-register"
                            # description with a terse "refresh token"
                            # instruction so clients don't trigger DCR.
                            try:
                                v = value.decode("latin-1")
                                v = v.replace(
                                    'clear authentication tokens in your '
                                    'MCP client and reconnect. Your client '
                                    'should automatically re-register and '
                                    'obtain new tokens.',
                                    'refresh your access token via the '
                                    'token endpoint. Do NOT re-register.',
                                )
                                new_headers.append(
                                    (name, v.encode("latin-1"))
                                )
                            except Exception:
                                new_headers.append((name, value))
                        else:
                            new_headers.append((name, value))
                    message = {**message, "headers": new_headers}
                await send(message)

            await self.app(scope, receive, rewrite_send)

    middlewares.append(Middleware(WwwAuthenticateRewriteMiddleware))

    # ------------------------------------------------------------------
    # OAuth well-known issuer trailing-slash normalizer
    # ------------------------------------------------------------------
    # The low-level `mcp` python-sdk builds OAuth metadata URLs from
    # pydantic `AnyHttpUrl`, and `str(AnyHttpUrl("https://host"))` yields
    # "https://host/" -- a lone trailing slash. Per RFC 8414 the issuer
    # identifier for an authorization server with no path component must be
    # exactly the origin (no trailing slash), and strict clients verify the
    # discovered metadata `issuer` is byte-identical to the origin they
    # derived. The trailing slash makes that check fail, aborting discovery.
    #
    # This is upstream bug modelcontextprotocol/python-sdk#1919 (P1, still
    # unmerged as of 2026-07), so no FastMCP/mcp version bump fixes it yet.
    # Newer strict clients (e.g. @ai-sdk/mcp v2, Google ADK, IBM Context
    # Forge) refuse to connect; lenient older clients happened not to care.
    #
    # We rewrite the two public well-known documents on the way out,
    # stripping the lone trailing slash from origin-only URLs in the
    # `issuer`, `resource`, and `authorization_servers` fields. Endpoint
    # URLs (which carry a real path) are left untouched.
    class WellKnownIssuerNormalizeMiddleware:
        def __init__(self, app):
            self.app = app

        @staticmethod
        def _strip(url):
            # Only strip a lone trailing slash on an origin-only URL
            # (path == "/", no query/fragment); leave path URLs intact.
            if not isinstance(url, str) or not url.endswith("/"):
                return url
            try:
                from urllib.parse import urlsplit

                parts = urlsplit(url)
                if parts.path == "/" and not parts.query and not parts.fragment:
                    return url[:-1]
            except Exception:
                pass
            return url

        def _normalize(self, data):
            if not isinstance(data, dict):
                return data
            for key in ("issuer", "resource"):
                if key in data:
                    data[key] = self._strip(data[key])
            servers = data.get("authorization_servers")
            if isinstance(servers, list):
                data["authorization_servers"] = [self._strip(s) for s in servers]
            return data

        async def __call__(self, scope, receive, send):
            path = scope.get("path", "") if scope["type"] == "http" else ""
            if not path.startswith("/.well-known/oauth-"):
                await self.app(scope, receive, send)
                return

            start_message = {}
            body_chunks: list[bytes] = []

            async def capture_send(message):
                if message["type"] == "http.response.start":
                    start_message.clear()
                    start_message.update(message)
                    return
                if message["type"] == "http.response.body":
                    body_chunks.append(message.get("body", b""))
                    if message.get("more_body", False):
                        return
                    # Final chunk: rewrite the buffered JSON body.
                    body = b"".join(body_chunks)
                    new_body = body
                    try:
                        data = json.loads(body.decode("utf-8"))
                        data = self._normalize(data)
                        new_body = json.dumps(data).encode("utf-8")
                    except Exception:
                        new_body = body
                    headers = [
                        (n, v)
                        for n, v in start_message.get("headers", [])
                        if n.lower() != b"content-length"
                    ]
                    headers.append(
                        (b"content-length", str(len(new_body)).encode("latin-1"))
                    )
                    await send({**start_message, "headers": headers})
                    await send(
                        {"type": "http.response.body", "body": new_body}
                    )
                    return
                await send(message)

            await self.app(scope, receive, capture_send)

    middlewares.append(Middleware(WellKnownIssuerNormalizeMiddleware))

    if allowed_users:

        class AuthCheckMiddleware:
            """Pure ASGI middleware to check user email after authentication."""

            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                # Only check HTTP requests to /mcp
                if scope["type"] == "http" and scope["path"].startswith("/mcp"):
                    user = scope.get("user")

                    user_email = ""
                    if user and hasattr(user, "access_token"):
                        access_token = user.access_token
                        email = access_token.claims.get("email")
                        user_email = email.lower() if email else ""

                    if allowed_users and user_email and user_email not in allowed_users:
                        print(f"Access denied for {user_email}", file=sys.stderr)
                        body = json.dumps(
                            {
                                "error": "Access denied",
                                "message": f"User {user_email} is not authorized",
                            }
                        ).encode()
                        await send(
                            {
                                "type": "http.response.start",
                                "status": 403,
                                "headers": [
                                    [b"content-type", b"application/json"],
                                    [
                                        b"content-length",
                                        str(len(body)).encode(),
                                    ],
                                ],
                            }
                        )
                        await send({"type": "http.response.body", "body": body})
                        return
                    elif allowed_users and not user_email:
                        print(
                            "WARNING: Could not extract email from authenticated user",
                            file=sys.stderr,
                        )

                await self.app(scope, receive, send)

        middlewares.append(Middleware(AuthCheckMiddleware))
        print(f"Email restriction enabled for: {', '.join(allowed_users)}")
    else:
        print(
            "WARNING: MCP_ALLOWED_USERS not set - any Google account can authenticate!",
            file=sys.stderr,
        )

    app = gateway.http_app(middleware=middlewares)

    # Pre-warm stdio children at boot. FastMCP's stdio transport is lazy:
    # subprocesses don't spawn until the first tool call, so that first
    # call pays a multi-second cold start (and for wss-shim-backed entries
    # the bridge can't even dial in until the shim is forced to listen).
    # Chain a startup step onto FastMCP's own lifespan that forces each
    # proxy to connect. Fire-and-forget so a slow spawn doesn't block the
    # server from accepting requests. Must run before `app` is rebound to
    # the HomePageMiddleware wrapper below (which has no lifespan).
    # See gateway.warm_proxies / plans/wss-shim-lifecycle.md.
    _fastmcp_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan_with_warmup(app_):
        async with _fastmcp_lifespan(app_):
            asyncio.create_task(warm_proxies(gateway))
            yield

    app.router.lifespan_context = lifespan_with_warmup

    # --- Static page routes ---
    async def serve_privacy(request):
        return FileResponse(static_dir / "privacy.html")

    async def serve_terms(request):
        return FileResponse(static_dir / "terms.html")

    app.router.routes.insert(
        0,
        Route("/privacy", endpoint=serve_privacy, methods=["GET"], name="privacy"),
    )
    app.router.routes.insert(
        0,
        Route("/terms", endpoint=serve_terms, methods=["GET"], name="terms"),
    )

    # Home page middleware
    class HomePageMiddleware:
        """Pure ASGI middleware to serve home page at /"""

        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if (
                scope["type"] == "http"
                and scope["method"] == "GET"
                and scope["path"] == "/"
            ):
                response = FileResponse(static_dir / "index.html")
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)

    app = HomePageMiddleware(app)

    print(f"Serving static pages: / (via middleware), /privacy, /terms")
    print(f"OAuth endpoints: /.well-known/*, /register")
    print(f"MCP protocol: /mcp/*")

    port = int(os.environ.get("MCP_PORT", "8000"))
    print(f"Listening on 0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
