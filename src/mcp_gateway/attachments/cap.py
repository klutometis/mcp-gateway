"""Signed capability tokens for artifact fetches.

Per ``plans/terminal-auth-device-flow.md`` §5: an artifact URL carries a
**self-authorizing signed capability**, not a session cookie and not an
opaque unguessable path.

    GET /artifacts/{artifact_id}?cap=<token>
    cap = sign({k: artifact_id, exp, sc: "read", sub: group_id}, key)

Why a cap and not an R2 presigned URL:

- It works **headless**. A terminal/tmux client with no browser session
  can fetch the bytes with nothing but the URL (`curl`), which is the
  whole point of the device-flow plan.
- Expiry is enforced by us, on every request. The R2 plan flagged
  "do presigned expiries actually fire?" as an open question; with a cap
  we never have to answer it.
- The bucket stays private. No public bucket, no public r2.dev domain,
  no object ACLs — the gateway is the only reader.

HMAC-SHA256, because the gateway is both issuer and verifier (the open
question in that plan, answered the way it guessed).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

__all__ = ["CapError", "CapPayload", "mint_cap", "verify_cap"]


class CapError(Exception):
    """A capability token is malformed, forged, expired, or for another object."""


@dataclass(frozen=True)
class CapPayload:
    key: str
    exp: int
    scope: str = "read"
    sub: str | None = None


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(body: str, key: bytes) -> str:
    return _b64e(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())


def mint_cap(
    artifact_key: str,
    *,
    signing_key: bytes,
    ttl_seconds: int,
    scope: str = "read",
    sub: str | None = None,
    now: float | None = None,
) -> str:
    """Mint a capability token for one artifact, good for ``ttl_seconds``."""
    exp = int((now if now is not None else time.time()) + ttl_seconds)
    payload: dict[str, object] = {"k": artifact_key, "exp": exp, "sc": scope}
    if sub:
        payload["sub"] = sub
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_sign(body, signing_key)}"


def verify_cap(
    token: str,
    *,
    signing_key: bytes,
    artifact_key: str | None = None,
    scope: str = "read",
    now: float | None = None,
) -> CapPayload:
    """Verify signature, expiry, scope and (optionally) the object it names.

    Raises :class:`CapError` on any failure; never returns a partial
    verdict. Signature is compared with :func:`hmac.compare_digest`.
    """
    try:
        body, sig = token.split(".", 1)
    except ValueError as exc:
        raise CapError("malformed cap token") from exc
    if not hmac.compare_digest(sig, _sign(body, signing_key)):
        raise CapError("bad signature")
    try:
        payload = json.loads(_b64d(body))
    except Exception as exc:  # pragma: no cover - only on a forged-but-signed body
        raise CapError("malformed cap payload") from exc
    if not isinstance(payload, dict) or "k" not in payload or "exp" not in payload:
        raise CapError("malformed cap payload")
    exp = int(payload["exp"])
    if (now if now is not None else time.time()) >= exp:
        raise CapError("cap expired")
    if payload.get("sc", "read") != scope:
        raise CapError("cap scope mismatch")
    if artifact_key is not None and payload["k"] != artifact_key:
        raise CapError("cap is for a different artifact")
    return CapPayload(
        key=str(payload["k"]),
        exp=exp,
        scope=str(payload.get("sc", "read")),
        sub=payload.get("sub"),
    )
