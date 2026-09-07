"""The trailing slash on the OAuth issuer, in both places it leaks.

`str(AnyHttpUrl("https://host"))` is `"https://host/"`. RFC 8414 says an issuer
with no path component is exactly the origin, and RFC 9207 s2.4 says the client
compares the authorization response's `iss` to it with *simple string
comparison* -- explicitly without normalizing first. So one stray byte aborts
the flow, and the error a user sees is a re-authentication loop.

Upstream: modelcontextprotocol/python-sdk#1919, unmerged. Two gateway
middlewares paper over it until it lands:

- ``WellKnownIssuerNormalizeMiddleware`` rewrites the discovery documents
  (added 2026-07-11, previously untested).
- ``AuthorizeIssNormalizeMiddleware`` rewrites the ``iss`` query parameter on
  the ``/authorize`` redirect (added 2026-09-06, after Claude's connector
  rejected a response the discovery fix had made *more* inconsistent: metadata
  without the slash, redirect with it).

These test the middlewares as plain ASGI apps. Building the real gateway needs
live OAuth credentials, and the interesting behaviour is entirely in the
rewrite.
"""

from __future__ import annotations

import json

import pytest

from mcp_gateway.remote import _issuer_slash_middlewares

WellKnown, AuthorizeIss = _issuer_slash_middlewares()


async def drive(app, *, path: str, status: int, headers=None, body: bytes | None = None):
    """Run one request through an ASGI middleware; return (status, headers, body)."""
    sent: list[dict] = []

    async def inner(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": headers or [],
            }
        )
        await send({"type": "http.response.body", "body": body or b""})

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await app(inner)({"type": "http", "path": path}, receive, send)

    start = next(m for m in sent if m["type"] == "http.response.start")
    out_body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], dict(start.get("headers", [])), out_body


class TestAuthorizeRedirect:
    async def test_strips_the_lone_slash_from_iss(self) -> None:
        loc = (
            "http://127.0.0.1:9876/callback?code=abc123"
            "&state=xyz&iss=https%3A%2F%2Fmcp.danenberg.ai%2F"
        )
        _, headers, _ = await drive(
            AuthorizeIss,
            path="/authorize",
            status=302,
            headers=[(b"location", loc.encode())],
        )
        out = headers[b"location"].decode()

        assert "iss=https%3A%2F%2Fmcp.danenberg.ai&" in out or out.endswith(
            "iss=https%3A%2F%2Fmcp.danenberg.ai"
        )
        assert "mcp.danenberg.ai%2F" not in out

    async def test_code_and_state_survive_untouched(self) -> None:
        # The rewrite reassembles the query string; losing the authorization
        # code to fix a slash would be a spectacular own goal.
        loc = (
            "http://127.0.0.1:9876/callback?code=abc123"
            "&state=xyz&iss=https%3A%2F%2Fmcp.danenberg.ai%2F"
        )
        _, headers, _ = await drive(
            AuthorizeIss,
            path="/authorize",
            status=302,
            headers=[(b"location", loc.encode())],
        )
        out = headers[b"location"].decode()

        assert "code=abc123" in out
        assert "state=xyz" in out

    async def test_an_issuer_with_a_real_path_keeps_its_slash(self) -> None:
        # Only an origin-only URL is over-normalized by AnyHttpUrl. A path
        # issuer ending in "/" may well be the issuer.
        loc = "http://cb/?iss=https%3A%2F%2Fhost%2Ftenant%2F"
        _, headers, _ = await drive(
            AuthorizeIss,
            path="/authorize",
            status=302,
            headers=[(b"location", loc.encode())],
        )
        assert "host%2Ftenant%2F" in headers[b"location"].decode()

    async def test_leaves_non_redirects_alone(self) -> None:
        # The consent screen is a 200 on the same path.
        _, headers, body = await drive(
            AuthorizeIss,
            path="/authorize",
            status=200,
            headers=[(b"content-type", b"text/html")],
            body=b"<h1>Allow Access</h1>",
        )
        assert body == b"<h1>Allow Access</h1>"

    async def test_leaves_other_paths_alone(self) -> None:
        loc = "http://cb/?iss=https%3A%2F%2Fmcp.danenberg.ai%2F"
        _, headers, _ = await drive(
            AuthorizeIss,
            path="/token",
            status=302,
            headers=[(b"location", loc.encode())],
        )
        assert headers[b"location"].decode() == loc

    async def test_a_redirect_with_no_iss_is_untouched(self) -> None:
        loc = "http://127.0.0.1:9876/callback?code=abc&state=xyz"
        _, headers, _ = await drive(
            AuthorizeIss,
            path="/authorize",
            status=302,
            headers=[(b"location", loc.encode())],
        )
        assert headers[b"location"].decode() == loc

    async def test_an_unparseable_location_passes_through(self) -> None:
        # Breaking the redirect is worse than leaving the slash on.
        _, headers, _ = await drive(
            AuthorizeIss,
            path="/authorize",
            status=302,
            headers=[(b"location", b"\xff\xfe not a url")],
        )
        assert headers[b"location"] == b"\xff\xfe not a url"


class TestWellKnownMetadata:
    """The 2026-07-11 fix, which had no test until now."""

    async def test_strips_the_slash_from_issuer(self) -> None:
        doc = {
            "issuer": "https://mcp.danenberg.ai/",
            "authorization_endpoint": "https://mcp.danenberg.ai/authorize",
        }
        _, _, body = await drive(
            WellKnown,
            path="/.well-known/oauth-authorization-server",
            status=200,
            headers=[(b"content-type", b"application/json")],
            body=json.dumps(doc).encode(),
        )
        out = json.loads(body)

        assert out["issuer"] == "https://mcp.danenberg.ai"
        # Endpoints carry a real path and must not be touched.
        assert out["authorization_endpoint"] == "https://mcp.danenberg.ai/authorize"

    async def test_strips_resource_and_authorization_servers(self) -> None:
        doc = {
            "resource": "https://mcp.danenberg.ai/",
            "authorization_servers": ["https://mcp.danenberg.ai/"],
        }
        _, _, body = await drive(
            WellKnown,
            path="/.well-known/oauth-protected-resource",
            status=200,
            headers=[(b"content-type", b"application/json")],
            body=json.dumps(doc).encode(),
        )
        out = json.loads(body)

        assert out["resource"] == "https://mcp.danenberg.ai"
        assert out["authorization_servers"] == ["https://mcp.danenberg.ai"]

    async def test_content_length_is_corrected(self) -> None:
        # Shortening the body without fixing content-length truncates it.
        doc = {"issuer": "https://mcp.danenberg.ai/"}
        raw = json.dumps(doc).encode()
        _, headers, body = await drive(
            WellKnown,
            path="/.well-known/oauth-authorization-server",
            status=200,
            headers=[
                (b"content-type", b"application/json"),
                (b"content-length", str(len(raw)).encode()),
            ],
            body=raw,
        )
        assert int(headers[b"content-length"]) == len(body)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://host/", "https://host"),
        ("https://host", "https://host"),
        ("https://host/path/", "https://host/path/"),
        ("https://host/?q=1", "https://host/?q=1"),
        ("", ""),
    ],
)
def test_strip_only_touches_origin_only_urls(url: str, expected: str) -> None:
    assert WellKnown._strip(url) == expected
