"""Per-request identity forwarding for multi-tenant gateways (via W3C baggage).

A single-tenant proxy bakes a static upstream header at build time. For a
multi-tenant gateway, each incoming request belongs to a different user,
and that user's identity must reach the upstream **per call** — not once
at process start.

FastMCP's ``FastMCPProxy(client_factory=...)`` invokes the factory per
request, so the factory is exactly where per-request identity belongs:
read the authenticated caller's id from the request context, then build an
upstream client that carries that id.

We carry it as **W3C ``baggage``** (the standard for propagating context
across services), not a bespoke ``X-`` header. See
``notes/identity-propagation.md`` for the baggage-vs-JWT rationale and why
plaintext baggage is safe on the trusted gateway → upstream hop (the
gateway is the trust boundary; upstreams aren't publicly reachable;
baggage carries an opaque id, never a secret).

Usage (in a consumer, e.g. mneme)::

    from fastmcp.server.dependencies import get_access_token
    from mcp_gateway.identity import make_identity_forwarding_proxy

    def resolve_group_id() -> str | None:
        tok = get_access_token()
        return (tok.claims or {}).get("group_id") if tok else None

    proxy = make_identity_forwarding_proxy(
        url="http://zep.internal:8000/mcp",
        identity_resolver=resolve_group_id,
        baggage_key="userId",
    )
    gateway.mount(proxy)

The package stays deployment-agnostic: it never decides *what* the
identity is or *how* it's authenticated — the consumer supplies the
``identity_resolver``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.providers.proxy import FastMCPProxy, ProxyClient

BAGGAGE_HEADER = "baggage"
DEFAULT_BAGGAGE_KEY = "userId"

# A resolver returns the caller's identity (e.g. group_id) for the current
# request, or None if unauthenticated / not resolvable.
IdentityResolver = Callable[[], str | None]

# Named value sanitizers. "safe" maps to a DNS/graphiti-safe id: anything
# outside [A-Za-z0-9_-] becomes '-'. (matches mneme's emailToGroupId.)
SANITIZERS: dict[str, Callable[[str], str]] = {
    "safe": lambda v: re.sub(r"[^a-zA-Z0-9_-]", "-", v),
}


def claim_identity_resolver(
    from_claim: str = "email",
    *,
    sanitize: str | None = None,
) -> IdentityResolver:
    """Resolve the caller's identity from an OAuth access-token claim.

    Reads ``from_claim`` off the authenticated request's access token
    (e.g. ``email``/``sub``), optionally passing it through a named
    sanitizer (e.g. ``"safe"`` -> a DNS/graphiti-safe id). Returns None
    when unauthenticated / claim absent. This is the resolver a gateway
    hands to ``make_identity_forwarding_proxy`` for OAuth-authenticated
    upstreams.
    """
    fn = SANITIZERS.get(sanitize) if sanitize else None

    def resolve() -> str | None:
        try:
            tok = get_access_token()
        except Exception:
            return None
        if tok is None:
            return None
        claims = getattr(tok, "claims", None) or {}
        val = claims.get(from_claim)
        if not val:
            return None
        val = str(val)
        return fn(val) if fn else val

    return resolve


def _encode_baggage_value(value: str) -> str:
    """Percent-encode a baggage value.

    The W3C baggage grammar excludes whitespace, commas, semicolons,
    backslashes and DQUOTE from values, so we percent-encode anything
    outside a conservative safe set (identifiers like emails stay
    readable; separators get escaped).
    """
    return quote(value, safe="")


def build_baggage_header(
    identity: str | None,
    *,
    baggage_key: str = DEFAULT_BAGGAGE_KEY,
    base_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a copy of ``base_headers`` with ``identity`` merged into the
    ``baggage`` header under ``baggage_key``.

    When ``identity`` is falsy no baggage entry is added (an unauthenticated
    call forwards nothing rather than an empty/placeholder identity), and any
    pre-existing entry for this key is dropped so stale identity can't leak.
    """
    headers = dict(base_headers or {})

    # Parse any existing baggage into an ordered key→raw-pair list, minus our key.
    existing = headers.get(BAGGAGE_HEADER, "")
    members: list[str] = []
    for member in (m.strip() for m in existing.split(",") if m.strip()):
        k = member.split("=", 1)[0].strip()
        if k != baggage_key:
            members.append(member)

    if identity:
        members.insert(0, f"{baggage_key}={_encode_baggage_value(identity)}")

    if members:
        headers[BAGGAGE_HEADER] = ",".join(members)
    else:
        headers.pop(BAGGAGE_HEADER, None)
    return headers


def identity_forwarding_client_factory(
    url: str,
    identity_resolver: IdentityResolver,
    *,
    baggage_key: str = DEFAULT_BAGGAGE_KEY,
    base_headers: dict[str, str] | None = None,
    proxy_client_kwargs: dict[str, Any] | None = None,
) -> Callable[[], ProxyClient]:
    """Return a ``client_factory`` that stamps the caller's identity as
    baggage per call.

    Each invocation resolves the current caller's identity and builds a
    fresh :class:`ProxyClient` over a streamable-HTTP transport carrying
    that identity in the ``baggage`` header.
    """

    def factory() -> ProxyClient:
        identity = identity_resolver()
        headers = build_baggage_header(
            identity, baggage_key=baggage_key, base_headers=base_headers
        )
        transport = StreamableHttpTransport(url, headers=headers)
        return ProxyClient(transport, **(proxy_client_kwargs or {}))

    return factory


def make_identity_forwarding_proxy(
    url: str,
    identity_resolver: IdentityResolver,
    *,
    baggage_key: str = DEFAULT_BAGGAGE_KEY,
    base_headers: dict[str, str] | None = None,
    name: str | None = None,
    proxy_client_kwargs: dict[str, Any] | None = None,
) -> FastMCPProxy:
    """Build a multi-tenant proxy that forwards per-request identity upstream
    as W3C ``baggage``.

    Mount the result into a gateway like any other proxy; the difference is
    that every upstream call carries the *current* caller's identity instead
    of a static one.
    """
    factory = identity_forwarding_client_factory(
        url,
        identity_resolver,
        baggage_key=baggage_key,
        base_headers=base_headers,
        proxy_client_kwargs=proxy_client_kwargs,
    )
    return FastMCPProxy(client_factory=factory, name=name or f"identity-proxy:{url}")
