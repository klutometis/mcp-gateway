"""Artifact storage: Cloudflare R2, or a local directory for dev/tests.

Keys are **content-addressed** (``attachments/<sha256><ext>``), which is
the plan's dedup story: the same bytes fetched twice are uploaded once
and get the same URL.

Why the Cloudflare REST object API rather than R2's S3 endpoint: the
only thing we would gain from S3 is presigned URLs, and
``cap.py`` explains why we don't want those. Going over the REST API
means the only credential involved is an account-scoped Cloudflare API
token read from the operator's environment — no S3 access-key pair to
mint, store and rotate, and no new SDK (we already depend on httpx).
"""

from __future__ import annotations

import asyncio
import hashlib
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

__all__ = ["Artifact", "ArtifactStore", "FileStore", "R2Store", "StoreError", "build_store"]

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
    """Cloudflare R2 over the account REST object API."""

    def __init__(
        self,
        *,
        account_id: str,
        token: str,
        bucket: str,
        timeout: float = 60.0,
        api_base: str = _API,
    ) -> None:
        if not account_id or not token:
            raise StoreError("R2 store needs an account id and an API token")
        self.account_id = account_id
        self.token = token
        self.bucket = bucket
        self.timeout = timeout
        self.api_base = api_base

    def _url(self, key: str) -> str:
        return (
            f"{self.api_base}/accounts/{self.account_id}/r2/buckets/"
            f"{self.bucket}/objects/{quote(key, safe='/')}"
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def put(self, key: str, data: bytes, *, mime: str, filename: str) -> None:
        meta = json.dumps(
            {"mime": mime, "filename": filename, "size": len(data)}
        ).encode()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for k, body, ctype in (
                (key, data, mime),
                (_meta_key(key), meta, "application/json"),
            ):
                r = await client.put(
                    self._url(k),
                    content=body,
                    headers={**self._headers, "Content-Type": ctype},
                )
                if r.status_code >= 300:
                    raise StoreError(f"R2 PUT {k} -> {r.status_code}: {r.text[:300]}")

    async def get(self, key: str) -> Artifact:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(self._url(key), headers=self._headers)
            if r.status_code == 404:
                raise StoreError(f"no such artifact: {key}")
            if r.status_code >= 300:
                raise StoreError(f"R2 GET {key} -> {r.status_code}: {r.text[:300]}")
            data = r.content
            mime = r.headers.get("content-type", "application/octet-stream")
            filename = Path(key).name
            m = await client.get(self._url(_meta_key(key)), headers=self._headers)
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
            r = await client.head(self._url(key), headers=self._headers)
            if r.status_code == 405:  # HEAD unsupported -> fall back to GET
                r = await client.get(self._url(key), headers=self._headers)
            return r.status_code < 300

    def describe(self) -> str:
        return f"r2:{self.bucket}"


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
            token=env.get(cfg.token_env, ""),
            bucket=cfg.bucket,
        )
    raise StoreError(f"unknown attachment storage backend: {cfg.backend!r}")


def sha256_key(data: bytes, *, prefix: str, ext: str) -> tuple[str, str]:
    """Return ``(sha256_hex, content_addressed_key)``."""
    digest = hashlib.sha256(data).hexdigest()
    return digest, f"{prefix}{digest}{ext}"
