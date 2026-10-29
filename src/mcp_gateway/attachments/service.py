"""The offload service: bytes in, signed-URL envelope out.

One object owns the config, the store and the signing key, so the
middleware, the two gateway-native tools and the HTTP route all agree
about what a URL means. Built once in ``create_gateway``.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from mcp_gateway.attachments.cap import mint_cap, verify_cap
from mcp_gateway.attachments.config import AttachmentConfig, load_config
from mcp_gateway.attachments.payload import ext_for, guess_filename, sniff_mime
from mcp_gateway.attachments.store import (
    Artifact,
    ArtifactStore,
    StoreError,
    build_store,
    sha256_key,
)

logger = logging.getLogger(__name__)

__all__ = ["AttachmentService", "Envelope"]


@dataclass(frozen=True)
class Envelope:
    """What the model sees instead of the bytes. ~60 tokens."""

    url: str
    mime: str
    size: int
    filename: str
    sha256: str
    expires_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "mime": self.mime,
            "size": self.size,
            "filename": self.filename,
            "sha256": self.sha256,
            "expires_at": self.expires_at,
        }


class AttachmentService:
    def __init__(self, config: AttachmentConfig, store: ArtifactStore) -> None:
        self.config = config
        self.store = store

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "AttachmentService | None":
        """Build from the environment, or ``None`` when offload is off."""
        cfg = load_config(env=env)
        if not cfg.enabled:
            return None
        store = build_store(cfg.storage, env=env)
        logger.info(
            "attachment offload: on (store=%s, rules=%s, ttl=%ss, base_url=%s)",
            store.describe(),
            [r.name for r in cfg.rules],
            cfg.ttl_seconds,
            cfg.base_url,
        )
        return cls(cfg, store)

    # -- writing ---------------------------------------------------------

    async def offload(
        self,
        data: bytes,
        *,
        declared_mime: str | None = None,
        declared_filename: str | None = None,
    ) -> Envelope:
        """Store ``data`` (deduplicated by sha256) and mint a capability URL."""
        mime = sniff_mime(data, declared_mime)
        digest, key = sha256_key(data, prefix=self.config.storage.prefix, ext=ext_for(mime))
        filename = guess_filename(declared=declared_filename, mime=mime, sha256=digest)

        if not await self.store.exists(key):
            await self.store.put(key, data, mime=mime, filename=filename)
        else:
            logger.info("attachment offload: dedup hit for %s", key)

        return self.envelope_for(key, mime=mime, size=len(data), filename=filename, sha256=digest)

    def envelope_for(
        self, key: str, *, mime: str, size: int, filename: str, sha256: str
    ) -> Envelope:
        artifact_id = self.artifact_id(key)
        cap = mint_cap(
            artifact_id,
            signing_key=self.config.signing_key,
            ttl_seconds=self.config.ttl_seconds,
        )
        expires = dt.datetime.fromtimestamp(
            time.time() + self.config.ttl_seconds, dt.timezone.utc
        ).replace(microsecond=0)
        return Envelope(
            url=self.config.url_for(artifact_id, cap),
            mime=mime,
            size=size,
            filename=filename,
            sha256=sha256,
            expires_at=expires.isoformat().replace("+00:00", "Z"),
        )

    # -- reading ---------------------------------------------------------

    def artifact_id(self, key: str) -> str:
        """Strip the storage prefix: the URL names the object, not the layout."""
        prefix = self.config.storage.prefix
        return key[len(prefix) :] if prefix and key.startswith(prefix) else key

    def storage_key(self, artifact_id: str) -> str:
        return f"{self.config.storage.prefix}{artifact_id}"

    def parse_ref(self, ref: str) -> tuple[str, str | None]:
        """Accept a full artifact URL, a bare artifact id, or a cap-less URL.

        Returns ``(artifact_id, cap_or_None)``. Gateway-native tools take
        either form: a model that has the envelope pastes the URL, a model
        that remembers only the sha pastes that.
        """
        ref = ref.strip()
        if "://" in ref:
            parsed = urlparse(ref)
            artifact_id = unquote(parsed.path.rsplit("/", 1)[-1])
            cap = parse_qs(parsed.query).get("cap", [None])[0]
            return artifact_id, cap
        if "?" in ref:
            head, _, query = ref.partition("?")
            return head.rsplit("/", 1)[-1], parse_qs(query).get("cap", [None])[0]
        return ref.rsplit("/", 1)[-1], None

    def verify(self, artifact_id: str, cap: str) -> None:
        verify_cap(cap, signing_key=self.config.signing_key, artifact_key=artifact_id)

    async def fetch(self, ref: str, *, require_cap: bool = False) -> Artifact:
        """Load an artifact by URL or id, verifying the cap when present."""
        artifact_id, cap = self.parse_ref(ref)
        if cap:
            self.verify(artifact_id, cap)
        elif require_cap:
            raise StoreError("missing cap token")
        return await self.store.get(self.storage_key(artifact_id))
