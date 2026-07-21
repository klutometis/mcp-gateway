"""Tests for per-request identity forwarding (multi-tenant seam)."""

from __future__ import annotations

from fastmcp.server.providers.proxy import FastMCPProxy

from mcp_gateway.identity import (
    DEFAULT_IDENTITY_HEADER,
    build_identity_headers,
    identity_forwarding_client_factory,
    make_identity_forwarding_proxy,
)


# --- build_identity_headers -------------------------------------------------


def test_build_headers_injects_identity():
    h = build_identity_headers("group-abc")
    assert h[DEFAULT_IDENTITY_HEADER] == "group-abc"


def test_build_headers_custom_name_and_merge():
    h = build_identity_headers(
        "g1", header_name="X-User", base_headers={"Authorization": "Bearer x"}
    )
    assert h == {"Authorization": "Bearer x", "X-User": "g1"}


def test_build_headers_omits_when_no_identity():
    # unauthenticated / unresolved → no identity header leaks through
    h = build_identity_headers(None, base_headers={"X-Spark-Group-Id": "stale"})
    assert DEFAULT_IDENTITY_HEADER not in h


def test_build_headers_does_not_mutate_base():
    base = {"a": "1"}
    build_identity_headers("g", base_headers=base)
    assert base == {"a": "1"}  # copy, not mutate


# --- the per-request factory ------------------------------------------------


def _transport_headers(client) -> dict:
    # ProxyClient wraps a transport; StreamableHttpTransport exposes .headers
    return dict(getattr(client.transport, "headers", {}))


def test_factory_forwards_current_identity_per_call():
    """The SAME factory must reflect whoever is calling *right now* — this is
    the multi-tenant guarantee (user A's call carries A, user B's carries B)."""
    current = {"id": None}
    factory = identity_forwarding_client_factory(
        "http://upstream.local/mcp", lambda: current["id"]
    )

    current["id"] = "user-A"
    client_a = factory()
    current["id"] = "user-B"
    client_b = factory()

    assert _transport_headers(client_a).get(DEFAULT_IDENTITY_HEADER) == "user-A"
    assert _transport_headers(client_b).get(DEFAULT_IDENTITY_HEADER) == "user-B"


def test_factory_omits_header_when_unauthenticated():
    factory = identity_forwarding_client_factory(
        "http://upstream.local/mcp", lambda: None
    )
    assert DEFAULT_IDENTITY_HEADER not in _transport_headers(factory())


def test_make_proxy_returns_fastmcp_proxy():
    proxy = make_identity_forwarding_proxy(
        "http://upstream.local/mcp", lambda: "g", name="zep"
    )
    assert isinstance(proxy, FastMCPProxy)
