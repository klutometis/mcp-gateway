"""Artifact storage: Cloudflare R2, or a local directory for dev/tests.

Keys are **content-addressed** (``attachments/<sha256><ext>``), which is
the plan's dedup story: the same bytes fetched twice are uploaded once
and get the same URL.

Two ways to talk to R2, same object layout either way:

* **S3 API (SigV4)** with an access-key pair, chosen when one is configured.
  This is the one to use: R2's *bucket-scoped* tokens ("Object Read & Write"
  on one bucket, made under R2 > API Tokens) only work over S3. The
  Cloudflare REST object API answers them 403 even on their own bucket --
  measured 2026-11-11 -- so a REST-only gateway forces an account-wide
  token that can also write every other bucket. Signing is hand-rolled
  (~40 lines, below) rather than a boto3 dependency.
* **Cloudflare REST object API** with a bearer token, the fallback when no
  key pair is set. Needs an account-level R2 token.

We never use S3 *presigned* URLs; ``cap.py`` explains why.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx

from mcp_gateway.attachments.config import StorageConfig

logger = logging.getLogger(__name__)

__all__ = [
    "Artifact",
    "ArtifactStore",
    "FileStore",
    "R2Store",
    "StoreError",
    "build_store",
    "sigv4_headers",
]

_API = "https://api.cloudflare.com/client/v4"


class StoreError(Exception):
    """The object store refused or failed a read/write."""


@dataclass(frozen=True)
class Artifact:
    key: str
    data: bytes
    mime: str
    filename: str

    @property
    def size(self) -> int:
        return len(self.data)


class ArtifactStore(Protocol):
    async def put(self, key: str, data: bytes, *, mime: str, filename: str) -> None: ...

    async def get(self, key: str) -> Artifact: ...

    async def exists(self, key: str) -> bool: ...

    def describe(self) -> str: ...


def _meta_key(key: str) -> str:
    return f"{key}.meta.json"


class FileStore:
    """Local-directory store. Dev, tests, and air-gapped hosts.

    Not a toy: a laptop gateway whose consumers are all on the same
    machine can run this permanently and never touch Cloudflare — the
    signed URL still points at the gateway, which is doing the serving
    either way.
    """

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.dir / key).resolve()
        if not str(p).startswith(str(self.dir.resolve())):
            raise StoreError(f"key escapes store directory: {key!r}")
        return p

    async def put(self, key: str, data: bytes, *, mime: str, filename: str) -> None:
        def _write() -> None:
            p = self._path(key)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            self._path(_meta_key(key)).write_text(
                json.dumps({"mime": mime, "filename": filename, "size": len(data)})
            )

        await asyncio.to_thread(_write)

    async def get(self, key: str) -> Artifact:
        def _read() -> Artifact:
            p = self._path(key)
            if not p.exists():
                raise StoreError(f"no such artifact: {key}")
            meta = {}
            mp = self._path(_meta_key(key))
            if mp.exists():
                meta = json.loads(mp.read_text())
            return Artifact(
                key=key,
                data=p.read_bytes(),
                mime=meta.get("mime", "application/octet-stream"),
                filename=meta.get("filename", Path(key).name),
            )

        return await asyncio.to_thread(_read)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(lambda: self._path(key).exists())

    def describe(self) -> str:
        return f"file:{self.dir}"


class R2Store:
    """Cloudflare R2, over S3 (SigV4) when given a key pair, else the REST API."""

    def __init__(
        self,
        *,
        account_id: str,
        bucket: str,
        token: str = "",
        access_key_id: str = "",
        secret_access_key: str = "",
        timeout: float = 60.0,
        api_base: str = _API,
        s3_endpoint: str | None = None,
    ) -> None:
        if not account_id:
            raise StoreError("R2 store needs an account id")
        self.s3 = bool(access_key_id and secret_access_key)
        if not self.s3 and not token:
            raise StoreError(
                "R2 store needs an S3 access-key pair or a Cloudflare API token"
            )
        self.account_id = account_id
        self.token = token
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.bucket = bucket
        self.timeout = timeout
        self.api_base = api_base
        self.s3_endpoint = (
            s3_endpoint or f"https://{account_id}.r2.cloudflarestorage.com"
        ).rstrip("/")

    # -- addressing and auth -------------------------------------------

    def _url(self, key: str) -> str:
        if self.s3:
            return f"{self.s3_endpoint}/{self.bucket}/{quote(key, safe='/')}"
        return (
            f"{self.api_base}/accounts/{self.account_id}/r2/buckets/"
            f"{self.bucket}/objects/{quote(key, safe='/')}"
        )

    def _headers(
        self, method: str, key: str, body: bytes = b"", ctype: str | None = None
    ) -> dict[str, str]:
        headers = {"Content-Type": ctype} if ctype else {}
        if not self.s3:
            return {**headers, "Authorization": f"Bearer {self.token}"}
        return {
            **headers,
            **sigv4_headers(
                method=method,
                url=self._url(key),
                body=body,
                access_key_id=self.access_key_id,
                secret_access_key=self.secret_access_key,
                extra=headers,
            ),
        }

    async def _send(
        self,
        client: httpx.AsyncClient,
        method: str,
        key: str,
        body: bytes = b"",
        ctype: str | None = None,
    ) -> httpx.Response:
        return await client.request(
            method,
            self._url(key),
            content=body if method == "PUT" else None,
            headers=self._headers(method, key, body, ctype),
        )

    # -- the store protocol ----------------------------------------------

    async def put(self, key: str, data: bytes, *, mime: str, filename: str) -> None:
        meta = json.dumps(
            {"mime": mime, "filename": filename, "size": len(data)}
        ).encode()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for k, body, ctype in (
                (key, data, mime),
                (_meta_key(key), meta, "application/json"),
            ):
                r = await self._send(client, "PUT", k, body, ctype)
                if r.status_code >= 300:
                    raise StoreError(f"R2 PUT {k} -> {r.status_code}: {r.text[:300]}")

    async def get(self, key: str) -> Artifact:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await self._send(client, "GET", key)
            if r.status_code == 404:
                raise StoreError(f"no such artifact: {key}")
            if r.status_code >= 300:
                raise StoreError(f"R2 GET {key} -> {r.status_code}: {r.text[:300]}")
            data = r.content
            mime = r.headers.get("content-type", "application/octet-stream")
            filename = Path(key).name
            m = await self._send(client, "GET", _meta_key(key))
            if m.status_code < 300:
                try:
                    meta = m.json()
                    mime = meta.get("mime", mime)
                    filename = meta.get("filename", filename)
                except Exception:  # pragma: no cover - corrupt sidecar
                    pass
        return Artifact(key=key, data=data, mime=mime, filename=filename)

    async def exists(self, key: str) -> bool:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await self._send(client, "HEAD", key)
            if r.status_code == 405:  # HEAD unsupported -> fall back to GET
                r = await self._send(client, "GET", key)
            return r.status_code < 300

    def describe(self) -> str:
        return f"r2:{self.bucket} ({'s3' if self.s3 else 'rest'})"


def sigv4_headers(
    *,
    method: str,
    url: str,
    body: bytes,
    access_key_id: str,
    secret_access_key: str,
    extra: dict[str, str] | None = None,
    region: str = "auto",
    service: str = "s3",
    now: _dt.datetime | None = None,
) -> dict[str, str]:
    """AWS Signature Version 4 headers for one request (R2 uses region ``auto``).

    Only what this store needs: no query string, payload hashed in full.
    ``extra`` headers (Content-Type) are signed too.
    """
    u = httpx.URL(url)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    signed = {
        "host": u.netloc.decode() if isinstance(u.netloc, bytes) else str(u.netloc),
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        **{k.lower(): v.strip() for k, v in (extra or {}).items()},
    }
    names = sorted(signed)
    canonical = "\n".join(
        [
            method,
            u.raw_path.decode().split("?", 1)[0] or "/",
            "",  # no query string
            "".join(f"{n}:{signed[n]}\n" for n in names),
            ";".join(names),
            payload_hash,
        ]
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )

    def _h(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k = _h(("AWS4" + secret_access_key).encode(), day)
    for part in (region, service, "aws4_request"):
        k = _h(k, part)
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, "
            f"SignedHeaders={';'.join(names)}, Signature={sig}"
        ),
    }


def build_store(cfg: StorageConfig, env: dict[str, str] | None = None) -> ArtifactStore:
    """Instantiate the configured backend, resolving secrets by env-var name."""
    env = dict(os.environ if env is None else env)
    if cfg.backend == "file":
        directory = cfg.directory or os.path.join(
            env.get("TMPDIR", "/tmp"), "mcp-gateway-attachments"
        )
        return FileStore(directory)
    if cfg.backend == "r2":
        return R2Store(
            account_id=env.get(cfg.account_id_env, ""),
            bucket=cfg.bucket,
            token=env.get(cfg.token_env, ""),
            access_key_id=env.get(cfg.access_key_id_env, ""),
            secret_access_key=env.get(cfg.secret_access_key_env, ""),
        )
    raise StoreError(f"unknown attachment storage backend: {cfg.backend!r}")


def sha256_key(data: bytes, *, prefix: str, ext: str) -> tuple[str, str]:
    """Return ``(sha256_hex, content_addressed_key)``."""
    digest = hashlib.sha256(data).hexdigest()
    return digest, f"{prefix}{digest}{ext}"
