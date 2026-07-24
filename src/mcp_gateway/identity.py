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

import logging
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.dependencies import get_access_token, get_http_headers
from fastmcp.server.providers.proxy import FastMCPProxy, ProxyClient

_log = logging.getLogger("mcp_gateway.identity")

BAGGAGE_HEADER = "baggage"
DEFAULT_BAGGAGE_KEY = "userId"
DEFAULT_IDENTITY_HEADER = "X-Spark-Group-Id"

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


def build_header_identity(
    identity: str | None,
    *,
    header_name: str = DEFAULT_IDENTITY_HEADER,
    base_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """Merge ``identity`` into a copy of ``base_headers`` as a plain header.

    For upstreams that read a bespoke identity header (e.g. the existing
    tools-server reads ``X-Spark-Group-Id``) rather than W3C baggage.
    Falsy identity omits the header (no stale/empty identity leaks).
    """
    headers = dict(base_headers or {})
    if identity:
        headers[header_name] = identity
    else:
        headers.pop(header_name, None)
    return headers


# A stamp merges a resolved identity into a base header dict.
HeaderStamp = Callable[["str | None", "dict[str, str] | None"], "dict[str, str]"]


def identity_forwarding_client_factory(
    url: str,
    identity_resolver: IdentityResolver,
    *,
    stamp: HeaderStamp | None = None,
    base_headers: dict[str, str] | None = None,
    proxy_client_kwargs: dict[str, Any] | None = None,
) -> Callable[[], ProxyClient]:
    """Return a ``client_factory`` that stamps the caller's identity per call.

    ``stamp`` decides *how* identity is carried (baggage vs plain header);
    defaults to W3C baggage under the default key.
    """
    if stamp is None:
        def stamp(ident: str | None, base: dict[str, str] | None) -> dict[str, str]:
            return build_baggage_header(ident, base_headers=base)

    def factory() -> ProxyClient:
        identity = identity_resolver()
        headers = stamp(identity, base_headers)
        # FastMCP's ProxyProvider force-enables forward_incoming_headers, which
        # merges the caller's inbound auth as ``get_http_headers({"authorization"})
        # | self.headers``. get_http_headers yields a lowercase ``authorization``
        # key; if our upstream credential is under ``Authorization`` (capitalized)
        # BOTH survive the dict-union and the caller's token can win, breaking
        # upstream auth. Normalize our auth key to lowercase so it collides with
        # and overrides the forwarded one.
        headers = {
            ("authorization" if k.lower() == "authorization" else k): v
            for k, v in headers.items()
        }
        transport = StreamableHttpTransport(url, headers=headers)
        return ProxyClient(transport, **(proxy_client_kwargs or {}))

    return factory


def make_identity_forwarding_proxy(
    url: str,
    identity_resolver: IdentityResolver,
    *,
    carrier: str = "baggage",
    baggage_key: str = DEFAULT_BAGGAGE_KEY,
    header_name: str = DEFAULT_IDENTITY_HEADER,
    base_headers: dict[str, str] | None = None,
    name: str | None = None,
    proxy_client_kwargs: dict[str, Any] | None = None,
) -> FastMCPProxy:
    """Build a multi-tenant proxy that forwards per-request identity upstream.

    ``carrier`` selects how identity is carried:
      - ``"baggage"`` (default): W3C ``baggage: <baggage_key>=<id>``.
      - ``"header"``: a plain ``<header_name>: <id>`` (e.g. the existing
        tools-server's ``X-Spark-Group-Id``) — for coexistence with
        upstreams that predate baggage.
    """
    if carrier == "baggage":
        def stamp(ident: str | None, base: dict[str, str] | None) -> dict[str, str]:
            return build_baggage_header(ident, baggage_key=baggage_key, base_headers=base)
    elif carrier == "header":
        def stamp(ident: str | None, base: dict[str, str] | None) -> dict[str, str]:
            return build_header_identity(ident, header_name=header_name, base_headers=base)
    else:
        raise ValueError(f"carrier must be 'baggage' or 'header', got {carrier!r}")

    factory = identity_forwarding_client_factory(
        url,
        identity_resolver,
        stamp=stamp,
        base_headers=base_headers,
        proxy_client_kwargs=proxy_client_kwargs,
    )
    return FastMCPProxy(client_factory=factory, name=name or f"identity-proxy:{url}")


def token_forwarding_client_factory(
    url: str,
    *,
    base_headers: dict[str, str] | None = None,
    proxy_client_kwargs: dict[str, Any] | None = None,
) -> Callable[[], ProxyClient]:
    """Return a ``client_factory`` that forwards the caller's inbound
    ``Authorization`` to the upstream, explicitly, per request.

    FastMCP's implicit ``forward_incoming_headers`` only reliably fires for
    the identity/baggage proxy path; the plain proxy path leaves the upstream
    with no auth (observed: sidecars saw ``auth=<none>``). This factory does
    it deterministically — read the current request's ``authorization`` via
    ``get_http_headers`` and stamp it on the upstream transport. For upstreams
    that consume the caller's token directly (e.g. Google token-consumers).
    """

    def factory() -> ProxyClient:
        headers = dict(base_headers or {})
        # Prefer the validated access token from the auth context: for an
        # OAuthProxy upstream this is the swapped-in upstream (Google) token,
        # and get_access_token() is populated inside the proxy factory at
        # call scope (get_http_headers() is NOT — it comes back empty here).
        # Fall back to the raw inbound Authorization if present.
        tok = None
        at = None
        try:
            at = get_access_token()
            tok = at.token if at else None
        except Exception as e:
            _log.info("forward_token: get_access_token raised: %s", e)
        raw = None
        try:
            incoming = get_http_headers(include={"authorization"})
            raw = incoming.get("authorization") if incoming else None
        except Exception as e:
            _log.info("forward_token: get_http_headers raised: %s", e)
        _log.info(
            "forward_token probe: access_token=%s token=%s http_authz=%s",
            bool(at), (tok[:12] + "…") if tok else None,
            (raw[:19] + "…") if raw else None,
        )
        if not tok and raw:
            headers["authorization"] = raw
        if tok:
            headers["authorization"] = f"Bearer {tok}"
        elif "authorization" not in headers:
            headers.pop("Authorization", None)
        transport = StreamableHttpTransport(url, headers=headers)
        return ProxyClient(transport, **(proxy_client_kwargs or {}))

    return factory


def make_token_forwarding_proxy(
    url: str,
    *,
    base_headers: dict[str, str] | None = None,
    name: str | None = None,
    proxy_client_kwargs: dict[str, Any] | None = None,
) -> FastMCPProxy:
    """Build a proxy that forwards the caller's ``Authorization`` upstream.

    For upstreams that are token-consumers of the caller's own credential
    (e.g. a Google access token). Distinct from the baggage/identity proxy,
    which forwards a derived claim rather than the raw token.
    """
    factory = token_forwarding_client_factory(
        url,
        base_headers=base_headers,
        proxy_client_kwargs=proxy_client_kwargs,
    )
    return FastMCPProxy(client_factory=factory, name=name or f"token-proxy:{url}")
