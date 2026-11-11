"""Configuration for attachment offload: match rules, thresholds, storage.

Everything here is *configuration*, not code: which tools are subject to
offload is a list of glob patterns in a JSON file (or an env var), never
a tool name in business logic. Adding Gmail's attachment fetch when that
tool exists is one line in that list.

Resolution order, lowest to highest:

1. built-in defaults (:data:`DEFAULT_RULES`, the four tools named in
   ``plans/messaging-attachments-r2.md`` plus Roblox's thumbnail);
2. the JSON file named by ``MCP_ATTACHMENT_CONFIG``;
3. individual ``MCP_ATTACHMENT_*`` env vars.

Secrets are never values here, only **env-var names** (``token_env``,
``signing_key_env``, …) resolved from the operator's environment.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import secrets
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_RULES",
    "AttachmentConfig",
    "AskConfig",
    "Rule",
    "StorageConfig",
    "matches",
]

# The plan's INLINE_THRESHOLD: below this, leave the payload alone — a
# round trip to object storage costs more than the tokens it saves.
DEFAULT_MIN_BYTES = 50 * 1024
# Images this small still go inline, as a real MCP `image` block, so a
# multimodal model can just look at them (~1.5k tokens, no extra call).
DEFAULT_INLINE_IMAGE_MAX_BYTES = 50 * 1024
# Sanity cap: a buggy upstream must not be able to push 4 GB into R2.
DEFAULT_MAX_BYTES = 100 * 1024 * 1024
DEFAULT_TTL_SECONDS = 24 * 60 * 60

#: The four tools the plan names, by *shape* rather than by server.
#: A pattern of the form ``*.verb`` matches the bare tool ``verb`` and any
#: ``prefix_verb`` the gateway's renaming produces (see :func:`matches`).
DEFAULT_RULES: list[dict[str, Any]] = [
    {
        "name": "messaging-attachments",
        "match": [
            "*.download_media",  # WhatsApp
            "*.fetch_attachment",  # LinkedIn
            "*.imessage_get_attachment",  # iMessage
            "*.get_asset_thumbnail",  # Roblox (base64 PNG in text)
            # Gmail, when the tool exists, is one more line here:
            # "*.get_attachment",
        ],
    }
]


def matches(tool_name: str, pattern: str) -> bool:
    """Does ``tool_name`` match ``pattern``?

    Patterns are globs over the *canonical* ``server.verb`` name from
    ``plans/messaging-mcp-shape.md``. This gateway, though, flattens and
    renames its tool surface (``download_media`` → ``whatsapp_download_media``,
    ``fetch_attachment`` → ``linkedin_fetch_attachment``), so a
    ``server.verb`` pattern must also match the flattened forms:

    >>> matches("whatsapp_download_media", "*.download_media")
    True
    >>> matches("download_media", "*.download_media")
    True
    >>> matches("whatsapp.download_media", "*.download_media")
    True
    >>> matches("imessage_get_attachment", "*.get_asset_thumbnail")
    False
    """
    if fnmatch.fnmatchcase(tool_name, pattern):
        return True
    if pattern.startswith("*."):
        verb = pattern[2:]
        return tool_name == verb or fnmatch.fnmatchcase(tool_name, f"*_{verb}")
    return False


@dataclass(frozen=True)
class Rule:
    """One match rule: which tools, and the thresholds that apply to them."""

    name: str
    match: tuple[str, ...]
    min_bytes: int = DEFAULT_MIN_BYTES
    inline_image_max_bytes: int = DEFAULT_INLINE_IMAGE_MAX_BYTES
    max_bytes: int = DEFAULT_MAX_BYTES
    #: What to do when storage fails: ``"truncate"`` (default: drop the
    #: bytes, return an error envelope — the context window is the thing
    #: we are protecting) or ``"passthrough"`` (hand the model the
    #: original base64, bug and all).
    on_error: str = "truncate"

    def applies_to(self, tool_name: str) -> bool:
        return any(matches(tool_name, p) for p in self.match)


@dataclass(frozen=True)
class StorageConfig:
    backend: str = "r2"  # "r2" | "file"
    bucket: str = "mcp-attachments"
    prefix: str = "attachments/"
    # R2. With an S3 key pair (preferred: works with a bucket-scoped token)
    # the store speaks S3/SigV4; otherwise the REST API with token_env.
    account_id_env: str = "CLOUDFLARE_ACCOUNT_ID"
    token_env: str = "MCP_ATTACHMENT_R2_TOKEN"
    access_key_id_env: str = "MCP_ATTACHMENT_R2_ACCESS_KEY_ID"
    secret_access_key_env: str = "MCP_ATTACHMENT_R2_SECRET_ACCESS_KEY"
    # file backend
    directory: str | None = None


@dataclass(frozen=True)
class AskConfig:
    enabled: bool = True
    model: str = "gemini-2.5-flash"
    api_key_env: str = "GEMINI_API_KEY"
    endpoint: str = "https://generativelanguage.googleapis.com/v1beta/models"
    max_answer_chars: int = 8000


@dataclass(frozen=True)
class AttachmentConfig:
    enabled: bool = False
    rules: tuple[Rule, ...] = ()
    storage: StorageConfig = field(default_factory=StorageConfig)
    ask: AskConfig = field(default_factory=AskConfig)
    base_url: str = "http://127.0.0.1:3100"
    route_prefix: str = "/artifacts"
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    view_max_dimension: int = 1024
    signing_key: bytes = b""

    def rule_for(self, tool_name: str) -> Rule | None:
        for rule in self.rules:
            if rule.applies_to(tool_name):
                return rule
        return None

    def url_for(self, artifact_key: str, cap: str) -> str:
        base = self.base_url.rstrip("/")
        prefix = self.route_prefix.strip("/")
        return f"{base}/{prefix}/{artifact_key}?cap={cap}"


def _rule_from_dict(d: dict[str, Any], defaults: dict[str, Any]) -> Rule:
    match = d.get("match") or []
    if isinstance(match, str):
        match = [match]
    return Rule(
        name=d.get("name", "rule"),
        match=tuple(match),
        min_bytes=int(d.get("min_bytes", defaults["min_bytes"])),
        inline_image_max_bytes=int(
            d.get("inline_image_max_bytes", defaults["inline_image_max_bytes"])
        ),
        max_bytes=int(d.get("max_bytes", defaults["max_bytes"])),
        on_error=str(d.get("on_error", defaults["on_error"])),
    )


def _env_flag(env: dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def load_config(
    path: str | Path | None = None, env: dict[str, str] | None = None
) -> AttachmentConfig:
    """Build an :class:`AttachmentConfig` from file + environment.

    ``MCP_ATTACHMENT_OFFLOAD`` turns the whole feature on; it is **off by
    default**, so upgrading the gateway package changes nothing for a
    deployment that has not opted in.
    """
    env = dict(os.environ if env is None else env)
    path = path or env.get("MCP_ATTACHMENT_CONFIG")

    doc: dict[str, Any] = {}
    if path:
        with Path(path).open() as f:
            doc = json.load(f)
        doc = {k: v for k, v in doc.items() if not k.startswith("_")}

    defaults = {
        "min_bytes": int(doc.get("min_bytes", DEFAULT_MIN_BYTES)),
        "inline_image_max_bytes": int(
            doc.get("inline_image_max_bytes", DEFAULT_INLINE_IMAGE_MAX_BYTES)
        ),
        "max_bytes": int(doc.get("max_bytes", DEFAULT_MAX_BYTES)),
        "on_error": str(doc.get("on_error", "truncate")),
    }
    # Env overrides for the thresholds apply to every rule that did not
    # set its own; they are the knob you reach for in a one-off test.
    for key, var in (
        ("min_bytes", "MCP_ATTACHMENT_MIN_BYTES"),
        ("inline_image_max_bytes", "MCP_ATTACHMENT_INLINE_IMAGE_MAX_BYTES"),
        ("max_bytes", "MCP_ATTACHMENT_MAX_BYTES"),
    ):
        if var in env:
            defaults[key] = int(env[var])
    if "MCP_ATTACHMENT_ON_ERROR" in env:
        defaults["on_error"] = env["MCP_ATTACHMENT_ON_ERROR"]

    raw_rules = doc.get("rules", DEFAULT_RULES)
    rules = tuple(_rule_from_dict(r, defaults) for r in raw_rules)
    # A rule that took its thresholds from the file still honours an env
    # override only if it did not set them explicitly — handled above by
    # passing `defaults` in; nothing more to do here.

    s = dict(doc.get("storage", {}))
    storage = StorageConfig(
        backend=env.get("MCP_ATTACHMENT_BACKEND", s.get("backend", "r2")),
        bucket=env.get("MCP_ATTACHMENT_BUCKET", s.get("bucket", "mcp-attachments")),
        prefix=env.get("MCP_ATTACHMENT_PREFIX", s.get("prefix", "attachments/")),
        account_id_env=s.get("account_id_env", "CLOUDFLARE_ACCOUNT_ID"),
        token_env=s.get("token_env", "MCP_ATTACHMENT_R2_TOKEN"),
        access_key_id_env=s.get("access_key_id_env", "MCP_ATTACHMENT_R2_ACCESS_KEY_ID"),
        secret_access_key_env=s.get(
            "secret_access_key_env", "MCP_ATTACHMENT_R2_SECRET_ACCESS_KEY"
        ),
        directory=env.get("MCP_ATTACHMENT_DIR", s.get("directory")),
    )

    a = dict(doc.get("ask_about_attachment", {}))
    ask = AskConfig(
        enabled=_env_flag(env, "MCP_ATTACHMENT_ASK", bool(a.get("enabled", True))),
        model=env.get("MCP_ATTACHMENT_ASK_MODEL", a.get("model", "gemini-2.5-flash")),
        api_key_env=a.get("api_key_env", "GEMINI_API_KEY"),
        endpoint=a.get(
            "endpoint", "https://generativelanguage.googleapis.com/v1beta/models"
        ),
        max_answer_chars=int(a.get("max_answer_chars", 8000)),
    )

    signing_key_env = doc.get("signing_key_env", "MCP_ATTACHMENT_SIGNING_KEY")
    raw_key = env.get(signing_key_env, "")
    if raw_key:
        signing_key = raw_key.encode()
    else:
        # Ephemeral key: URLs minted before a restart stop verifying after
        # it. Fine for a laptop; say so loudly so production sets one.
        signing_key = secrets.token_bytes(32)
        if _env_flag(env, "MCP_ATTACHMENT_OFFLOAD", bool(doc.get("enabled", False))):
            logger.warning(
                "attachment offload: %s is unset; using an ephemeral signing "
                "key, so artifact URLs stop working after a gateway restart",
                signing_key_env,
            )

    port = env.get("MCP_PORT", "3100")
    cfg = AttachmentConfig(
        enabled=_env_flag(env, "MCP_ATTACHMENT_OFFLOAD", bool(doc.get("enabled", False))),
        rules=rules,
        storage=storage,
        ask=ask,
        base_url=env.get(
            "MCP_ATTACHMENT_BASE_URL", doc.get("base_url", f"http://127.0.0.1:{port}")
        ),
        route_prefix=doc.get("route_prefix", "/artifacts"),
        ttl_seconds=int(
            env.get("MCP_ATTACHMENT_TTL", doc.get("ttl_seconds", DEFAULT_TTL_SECONDS))
        ),
        view_max_dimension=int(
            env.get(
                "MCP_ATTACHMENT_VIEW_MAX_DIM",
                doc.get("view_image", {}).get("max_dimension", 1024),
            )
        ),
        signing_key=signing_key,
    )
    if cfg.enabled and not cfg.rules:
        logger.warning("attachment offload: enabled with no match rules; nothing will offload")
    return replace(cfg)
