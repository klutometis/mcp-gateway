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


# --- signed carrier (per-person wss-shim sidecars, plan 057) ---------------

import time as _time

import jwt as _jwt
import pytest as _pytest

from mcp_gateway.gateway import _build_single_instance
from mcp_gateway.identity import (
    SIGNED_IDENTITY_HEADER,
    SIGNED_IDENTITY_LIFETIME_S,
    build_signed_identity,
)

_KEY = b"k" * 40


def _assertion(headers: dict) -> dict:
    return _jwt.decode(headers[SIGNED_IDENTITY_HEADER], _KEY, algorithms=["HS256"],
                       audience="chrome")


def test_signed_identity_matches_the_shim_contract():
    h = build_signed_identity("Peter@Danenberg.AI", key=_KEY, audience="chrome")
    claims = _assertion(h)
    assert claims["email"] == "peter@danenberg.ai"
    assert claims["aud"] == "chrome"
    assert claims["exp"] - claims["iat"] == SIGNED_IDENTITY_LIFETIME_S == 60
    assert abs(claims["iat"] - _time.time()) < 5
    assert _jwt.get_unverified_header(h[SIGNED_IDENTITY_HEADER])["alg"] == "HS256"


def test_signed_identity_is_useless_elsewhere():
    token = build_signed_identity("a@x.com", key=_KEY, audience="chrome")[SIGNED_IDENTITY_HEADER]
    with _pytest.raises(_jwt.InvalidAudienceError):
        _jwt.decode(token, _KEY, algorithms=["HS256"], audience="tmux")
    with _pytest.raises(_jwt.InvalidSignatureError):
        _jwt.decode(token, b"x" * 40, algorithms=["HS256"], audience="chrome")


def test_signed_identity_omits_and_strips_when_unauthenticated():
    stale = {SIGNED_IDENTITY_HEADER: "from-a-previous-caller", "x-other": "kept"}
    assert build_signed_identity(None, key=_KEY, audience="chrome", base_headers=stale) == {
        "x-other": "kept"}


def test_factory_signs_whoever_is_calling_now():
    current = {"id": None}
    stamp = lambda ident, base: build_signed_identity(ident, key=_KEY, audience="chrome",
                                                      base_headers=base)
    factory = identity_forwarding_client_factory("http://chrome.local/mcp",
                                                 lambda: current["id"], stamp=stamp)
    current["id"] = "alice@example.com"
    a = dict(factory().transport.headers)
    current["id"] = "bob@example.com"
    b = dict(factory().transport.headers)
    assert _assertion(a)["email"] == "alice@example.com"
    assert _assertion(b)["email"] == "bob@example.com"


def test_signed_carrier_refuses_to_build_half_configured():
    url, who = "http://chrome.local/mcp", lambda: "a@x.com"
    assert isinstance(make_identity_forwarding_proxy(
        url, who, carrier="signed", signing_key=_KEY, audience="chrome"), FastMCPProxy)
    for kwargs, why in [({"audience": "chrome"}, "signing key"),
                        ({"signing_key": b"short", "audience": "chrome"}, "signing key"),
                        ({"signing_key": _KEY}, "audience")]:
        with _pytest.raises(ValueError, match=why):
            make_identity_forwarding_proxy(url, who, carrier="signed", **kwargs)


def _servers(tmp_path, fi: dict) -> dict:
    p = tmp_path / "servers.json"
    p.write_text(json.dumps({"chrome": {"transport": "streamable-http",
                                        "url": "http://127.0.0.1:8008/mcp",
                                        "forward_identity": fi, "tools": "*"}}))
    return load_servers(str(p))


def test_load_servers_parses_signed_with_defaults(tmp_path):
    fi = _servers(tmp_path, {"as": "signed"})["chrome"]["forward_identity"]
    assert fi["as"] == "signed"
    assert fi["audience"] == "chrome"                 # the entry's own name
    assert fi["key_env"] == "WSS_SHIM_ASSERTION_KEY"
    assert fi["from_claim"] == "email" and fi["sanitize"] is None


def test_load_servers_refuses_signed_with_a_sanitizer(tmp_path):
    with _pytest.raises(ValueError, match="sanitize"):
        _servers(tmp_path, {"as": "signed", "sanitize": "safe"})


def test_building_a_signed_server_needs_its_key(tmp_path, monkeypatch):
    servers = _servers(tmp_path, {"as": "signed"})
    build = lambda: _build_single_instance(name="chrome", server_config=servers["chrome"],
                                           tool_configs="*", create_proxy=None)
    monkeypatch.delenv("WSS_SHIM_ASSERTION_KEY", raising=False)
    with _pytest.raises(ValueError, match="WSS_SHIM_ASSERTION_KEY"):
        build()
    monkeypatch.setenv("WSS_SHIM_ASSERTION_KEY", "z" * 40)
    assert isinstance(build(), FastMCPProxy)
