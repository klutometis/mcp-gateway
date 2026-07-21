"""Tests for per-request identity forwarding via W3C baggage (multi-tenant seam)."""

from __future__ import annotations

from fastmcp.server.providers.proxy import FastMCPProxy

from mcp_gateway.identity import (
    BAGGAGE_HEADER,
    DEFAULT_BAGGAGE_KEY,
    build_baggage_header,
    identity_forwarding_client_factory,
    make_identity_forwarding_proxy,
)


# --- build_baggage_header ---------------------------------------------------


def test_baggage_injects_identity():
    h = build_baggage_header("group-abc")
    assert h[BAGGAGE_HEADER] == f"{DEFAULT_BAGGAGE_KEY}=group-abc"


def test_baggage_custom_key():
    h = build_baggage_header("acme", baggage_key="tenantId")
    assert h[BAGGAGE_HEADER] == "tenantId=acme"


def test_baggage_percent_encodes_value():
    # emails contain '@'/'.' (legal) but separators must be escaped; encode all
    h = build_baggage_header("peter@danenberg.name")
    assert h[BAGGAGE_HEADER] == f"{DEFAULT_BAGGAGE_KEY}=peter%40danenberg.name"


def test_baggage_omits_when_no_identity():
    # unauthenticated / unresolved → no baggage entry leaks through
    h = build_baggage_header(None, base_headers={"baggage": "userId=stale"})
    assert BAGGAGE_HEADER not in h


def test_baggage_preserves_other_members():
    h = build_baggage_header(
        "g1", base_headers={"baggage": "region=us-east-1,userId=old"}
    )
    # our key is refreshed (old dropped), other members preserved
    assert "userId=g1" in h[BAGGAGE_HEADER]
    assert "region=us-east-1" in h[BAGGAGE_HEADER]
    assert "userId=old" not in h[BAGGAGE_HEADER]


def test_baggage_does_not_mutate_base():
    base = {"baggage": "region=x"}
    build_baggage_header("g", base_headers=base)
    assert base == {"baggage": "region=x"}  # copy, not mutate


# --- the per-request factory ------------------------------------------------


def _baggage(client) -> str:
    return dict(getattr(client.transport, "headers", {})).get(BAGGAGE_HEADER, "")


def test_factory_forwards_current_identity_per_call():
    """The SAME factory must reflect whoever is calling *right now* — the
    multi-tenant guarantee (user A's call carries A, user B's carries B)."""
    current = {"id": None}
    factory = identity_forwarding_client_factory(
        "http://upstream.local/mcp", lambda: current["id"]
    )

    current["id"] = "user-A"
    a = _baggage(factory())
    current["id"] = "user-B"
    b = _baggage(factory())

    assert a == f"{DEFAULT_BAGGAGE_KEY}=user-A"
    assert b == f"{DEFAULT_BAGGAGE_KEY}=user-B"


def test_factory_omits_baggage_when_unauthenticated():
    factory = identity_forwarding_client_factory(
        "http://upstream.local/mcp", lambda: None
    )
    assert _baggage(factory()) == ""


def test_make_proxy_returns_fastmcp_proxy():
    proxy = make_identity_forwarding_proxy(
        "http://upstream.local/mcp", lambda: "g", name="zep"
    )
    assert isinstance(proxy, FastMCPProxy)
