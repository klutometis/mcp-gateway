"""Tests for per-request identity forwarding via W3C baggage (multi-tenant seam)."""

from __future__ import annotations

import json

from fastmcp.server.providers.proxy import FastMCPProxy

from mcp_gateway import identity as idmod
from mcp_gateway.gateway import load_servers
from mcp_gateway.identity import (
    BAGGAGE_HEADER,
    DEFAULT_BAGGAGE_KEY,
    DEFAULT_IDENTITY_HEADER,
    build_baggage_header,
    build_header_identity,
    claim_identity_resolver,
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


# --- header carrier (coexistence with X-Spark-Group-Id upstreams) ----------


def test_build_header_identity():
    assert build_header_identity("g1")[DEFAULT_IDENTITY_HEADER] == "g1"
    assert build_header_identity("g1", header_name="X-Tenant") == {"X-Tenant": "g1"}
    assert DEFAULT_IDENTITY_HEADER not in build_header_identity(None)


def test_factory_header_carrier_forwards_per_call():
    current = {"id": None}
    stamp = lambda ident, base: build_header_identity(ident, base_headers=base)
    factory = identity_forwarding_client_factory(
        "http://tools.local/mcp", lambda: current["id"], stamp=stamp
    )
    current["id"] = "user-A"
    a = dict(getattr(factory().transport, "headers", {}))
    current["id"] = "user-B"
    b = dict(getattr(factory().transport, "headers", {}))
    assert a.get(DEFAULT_IDENTITY_HEADER) == "user-A"
    assert b.get(DEFAULT_IDENTITY_HEADER) == "user-B"


def test_make_proxy_header_carrier():
    proxy = make_identity_forwarding_proxy(
        "http://tools.local/mcp", lambda: "g", carrier="header", name="tools"
    )
    assert isinstance(proxy, FastMCPProxy)


# --- claim_identity_resolver (OAuth claim -> identity) ----------------------


class _Tok:
    def __init__(self, claims):
        self.claims = claims


def test_claim_resolver_reads_and_sanitizes(monkeypatch):
    monkeypatch.setattr(idmod, "get_access_token", lambda: _Tok({"email": "peter@danenberg.name"}))
    assert claim_identity_resolver("email", sanitize="safe")() == "peter-danenberg-name"


def test_claim_resolver_raw(monkeypatch):
    monkeypatch.setattr(idmod, "get_access_token", lambda: _Tok({"sub": "abc123"}))
    assert claim_identity_resolver("sub")() == "abc123"


def test_claim_resolver_none_when_unauthenticated(monkeypatch):
    monkeypatch.setattr(idmod, "get_access_token", lambda: None)
    assert claim_identity_resolver("email")() is None


def test_claim_resolver_none_when_claim_missing(monkeypatch):
    monkeypatch.setattr(idmod, "get_access_token", lambda: _Tok({}))
    assert claim_identity_resolver("email")() is None


# --- servers.json forward_identity parsing ---------------------------------


def test_load_servers_parses_forward_identity(tmp_path):
    cfg = {
        "whatsapp": {
            "transport": "streamable-http",
            "url": "http://whatsapp.railway.internal:8080/mcp",
            "forward_identity": {"as": "baggage", "key": "userId", "from_claim": "email", "sanitize": "safe"},
            "tools": "*",
        }
    }
    p = tmp_path / "servers.json"
    p.write_text(json.dumps(cfg))
    servers = load_servers(str(p))
    fi = servers["whatsapp"]["forward_identity"]
    assert fi == {
        "as": "baggage",
        "key": "userId",
        "name": "X-Spark-Group-Id",
        "from_claim": "email",
        "sanitize": "safe",
    }
