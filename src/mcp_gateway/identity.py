"""Per-request identity forwarding for multi-tenant gateways.

A single-tenant proxy bakes a static upstream header at build time. For a
multi-tenant gateway, each incoming request belongs to a different user,
and that user's identity must reach the upstream **per call** — not once
at process start.

FastMCP's ``FastMCPProxy(client_factory=...)`` invokes the factory per
request, so the factory is exactly where per-request identity belongs:
read the authenticated caller's id from the request context, then build an
upstream client with that id injected as a header.

Usage (in a consumer, e.g. mneme)::

    from fastmcp.server.dependencies import get_access_token
    from mcp_gateway.identity import make_identity_forwarding_proxy

    def resolve_group_id() -> str | None:
        tok = get_access_token()
        return (tok.claims or {}).get("group_id") if tok else None

    proxy = make_identity_forwarding_proxy(
        url="http://zep.internal:8000/mcp",
        identity_resolver=resolve_group_id,
        header_name="X-Spark-Group-Id",
    )
    gateway.mount(proxy)

The package stays deployment-agnostic: it never decides *what* the
identity is or *how* it's authenticated — the consumer supplies the
``identity_resolver``. This is the seam that keeps auth out of the
generic machinery.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.providers.proxy import FastMCPProxy, ProxyClient

DEFAULT_IDENTITY_HEADER = "X-Spark-Group-Id"

# A resolver returns the caller's identity (e.g. group_id) for the current
# request, or None if unauthenticated / not resolvable.
IdentityResolver = Callable[[], str | None]


def build_identity_headers(
    identity: str | None,
    *,
    header_name: str = DEFAULT_IDENTITY_HEADER,
    base_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """Merge ``identity`` into a copy of ``base_headers`` under ``header_name``.

    When ``identity`` is falsy the header is omitted entirely (so an
    unauthenticated call doesn't forward an empty/placeholder identity).
    """
    headers = dict(base_headers or {})
    if identity:
        headers[header_name] = identity
    else:
        headers.pop(header_name, None)
    return headers


def identity_forwarding_client_factory(
    url: str,
    identity_resolver: IdentityResolver,
    *,
    header_name: str = DEFAULT_IDENTITY_HEADER,
    base_headers: dict[str, str] | None = None,
    proxy_client_kwargs: dict[str, Any] | None = None,
) -> Callable[[], ProxyClient]:
    """Return a ``client_factory`` that injects the caller's identity per call.

    Each invocation resolves the current caller's identity and builds a
    fresh :class:`ProxyClient` over a streamable-HTTP transport carrying
    that identity as ``header_name``.
    """

    def factory() -> ProxyClient:
        identity = identity_resolver()
        headers = build_identity_headers(
            identity, header_name=header_name, base_headers=base_headers
        )
        transport = StreamableHttpTransport(url, headers=headers)
        return ProxyClient(transport, **(proxy_client_kwargs or {}))

    return factory


def make_identity_forwarding_proxy(
    url: str,
    identity_resolver: IdentityResolver,
    *,
    header_name: str = DEFAULT_IDENTITY_HEADER,
    base_headers: dict[str, str] | None = None,
    name: str | None = None,
    proxy_client_kwargs: dict[str, Any] | None = None,
) -> FastMCPProxy:
    """Build a multi-tenant proxy that forwards per-request identity upstream.

    Mount the result into a gateway like any other proxy; the difference is
    that every upstream call carries the *current* caller's identity header
    instead of a static one.
    """
    factory = identity_forwarding_client_factory(
        url,
        identity_resolver,
        header_name=header_name,
        base_headers=base_headers,
        proxy_client_kwargs=proxy_client_kwargs,
    )
    return FastMCPProxy(client_factory=factory, name=name or f"identity-proxy:{url}")
