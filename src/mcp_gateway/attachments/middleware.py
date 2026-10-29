"""Response-side middleware: binary tool results become signed URLs.

This is the concrete instance of ``plans/gateway-middleware.md``'s
*response-side* direction, and the implementation of
``plans/messaging-attachments-r2.md``. The shape is the one that plan
asks for and nothing more:

    match rule (configuration)  →  handler (this module)  →  envelope

What it is **not** is a list of tool names in business logic. The
handler asks :class:`~mcp_gateway.attachments.config.AttachmentConfig`
whether a rule matches; the rules live in a JSON file. Adding Gmail's
attachment fetch is a line of config, not a patch.

Policy, in one paragraph. An ``image`` block smaller than the rule's
``inline_image_max_bytes`` is left alone — a small image is cheaper and
far more useful as a real image block (~1.5k tokens, and the model can
*see* it) than as a URL it must spend a tool call to open. Everything
else over ``min_bytes`` — big images, blobs, base64 buried in JSON,
bare base64 text — is uploaded and replaced with a ~60-token envelope.
Anything over ``max_bytes`` is refused outright (a buggy upstream must
not be able to bill us for 4 GB), and the model is told so. If storage
itself fails, the default is to *truncate*: the model gets an error
envelope, not the base64 — failing open here would reintroduce exactly
the bug this middleware exists to prevent, so "fail-open" is opt-in
(``on_error: "passthrough"``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import mcp.types as mt
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from mcp_gateway.attachments.config import Rule
from mcp_gateway.attachments.payload import (
    BLOB_KEYS,
    MIME_KEYS,
    NAME_KEYS,
    decode_b64,
    looks_like_base64,
    split_data_uri,
)
from mcp_gateway.attachments.service import AttachmentService, Envelope

logger = logging.getLogger(__name__)

__all__ = ["AttachmentOffloadMiddleware", "render_envelope"]


def render_envelope(env: Envelope) -> str:
    """The text block that replaces a payload. Keep it short; it is the cost."""
    return (
        f"[attachment offloaded: {env.size:,} bytes of {env.mime}, not inlined]\n"
        + json.dumps(env.as_dict())
        + "\nview_image(url) to see it; ask_about_attachment(url, question) to ask "
        "about it; the url fetches as-is (signed, expiring, no auth header)."
    )


def _note(text: str) -> mt.TextContent:
    return mt.TextContent(type="text", text=text)


class AttachmentOffloadMiddleware(Middleware):
    """Replace large binary payloads in tool results with signed URLs."""

    def __init__(self, service: AttachmentService) -> None:
        self.service = service

    # -- one payload ------------------------------------------------------

    async def _handle(
        self,
        data: bytes,
        *,
        rule: Rule,
        tool: str,
        mime: str | None,
        filename: str | None,
    ) -> str | None:
        """Offload one payload; return replacement text, or ``None`` to keep it."""
        if len(data) > rule.max_bytes:
            logger.warning(
                "attachment offload: %s returned %d bytes, over max_bytes=%d; dropped",
                tool,
                len(data),
                rule.max_bytes,
            )
            return (
                f"[attachment dropped: {len(data):,} bytes exceeds the gateway's "
                f"max_bytes={rule.max_bytes:,}. Neither stored nor inlined. Ask the "
                f"upstream tool for a smaller range, or raise MCP_ATTACHMENT_MAX_BYTES.]"
            )
        try:
            env = await self.service.offload(
                data, declared_mime=mime, declared_filename=filename
            )
        except Exception as exc:
            logger.error("attachment offload: store failed for %s: %s", tool, exc)
            if rule.on_error == "passthrough":
                return None
            return (
                f"[attachment NOT offloaded: {len(data):,} bytes of "
                f"{mime or 'unknown type'} could not be stored ({type(exc).__name__}: "
                f"{exc}). The bytes were withheld rather than inlined; retry, or set "
                f"on_error=passthrough if you really want them in context.]"
            )
        logger.info(
            "attachment offload: %s -> %s (%d bytes, %s)",
            tool,
            env.sha256[:12],
            env.size,
            env.mime,
        )
        return render_envelope(env)

    # -- JSON walking -----------------------------------------------------

    async def _walk_json(self, obj: Any, *, rule: Rule, tool: str) -> tuple[Any, bool]:
        """Replace big base64 strings anywhere in a JSON structure."""
        changed = False
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            for key, value in obj.items():
                if isinstance(value, str) and key.lower() in {
                    k.lower() for k in BLOB_KEYS
                }:
                    env_text = await self._maybe_string(
                        value,
                        rule=rule,
                        tool=tool,
                        mime=_first(obj, MIME_KEYS),
                        filename=_first(obj, NAME_KEYS),
                    )
                    if env_text is not None:
                        out[key] = env_text
                        changed = True
                        continue
                new_value, sub_changed = await self._walk_json(value, rule=rule, tool=tool)
                out[key] = new_value
                changed = changed or sub_changed
            return out, changed
        if isinstance(obj, list):
            out_list = []
            for item in obj:
                new_item, sub_changed = await self._walk_json(item, rule=rule, tool=tool)
                out_list.append(new_item)
                changed = changed or sub_changed
            return out_list, changed
        if isinstance(obj, str):
            env_text = await self._maybe_string(obj, rule=rule, tool=tool)
            if env_text is not None:
                return env_text, True
        return obj, changed

    async def _maybe_string(
        self,
        text: str,
        *,
        rule: Rule,
        tool: str,
        mime: str | None = None,
        filename: str | None = None,
    ) -> str | None:
        """If this string is a big base64 payload, offload it; else ``None``."""
        uri = split_data_uri(text)
        if uri is not None:
            uri_mime, b64 = uri
            mime = uri_mime or mime
        else:
            b64 = text
        # 4 base64 chars per 3 bytes: don't even decode anything that
        # can't clear the threshold.
        min_chars = (rule.min_bytes * 4) // 3
        if not looks_like_base64(b64, min_chars=min_chars):
            return None
        data = decode_b64(b64)
        if data is None or len(data) < rule.min_bytes:
            return None
        return await self._handle(
            data, rule=rule, tool=tool, mime=mime, filename=filename
        )

    # -- blocks -----------------------------------------------------------

    async def _handle_block(
        self, block: Any, *, rule: Rule, tool: str
    ) -> tuple[Any, bool]:
        if isinstance(block, mt.ImageContent):
            data = decode_b64(block.data)
            if data is None:
                return block, False
            # Small images stay inline, as images: a model can look at
            # them for ~1.5k tokens and no extra round trip.
            if len(data) <= rule.inline_image_max_bytes:
                return block, False
            text = await self._handle(
                data, rule=rule, tool=tool, mime=_mime_of(block), filename=None
            )
            return (_note(text), True) if text is not None else (block, False)

        if isinstance(block, mt.AudioContent):
            data = decode_b64(block.data)
            if data is None or len(data) < rule.min_bytes:
                return block, False
            text = await self._handle(
                data, rule=rule, tool=tool, mime=_mime_of(block), filename=None
            )
            return (_note(text), True) if text is not None else (block, False)

        if isinstance(block, mt.EmbeddedResource):
            resource = block.resource
            blob = getattr(resource, "blob", None)
            if not isinstance(blob, str):
                return block, False
            data = decode_b64(blob)
            if data is None or len(data) < rule.min_bytes:
                return block, False
            uri = str(getattr(resource, "uri", "") or "")
            text = await self._handle(
                data,
                rule=rule,
                tool=tool,
                mime=_mime_of(resource),
                filename=uri.rsplit("/", 1)[-1] or None,
            )
            return (_note(text), True) if text is not None else (block, False)

        if isinstance(block, mt.TextContent):
            text = block.text
            stripped = text.strip()
            if stripped[:1] in ("{", "["):
                try:
                    doc = json.loads(stripped)
                except ValueError:
                    doc = None
                if doc is not None:
                    new_doc, changed = await self._walk_json(doc, rule=rule, tool=tool)
                    if changed:
                        return _note(json.dumps(new_doc)), True
                    return block, False
            replacement = await self._maybe_string(stripped, rule=rule, tool=tool)
            if replacement is not None:
                return _note(replacement), True
            return block, False

        return block, False

    # -- entry point ------------------------------------------------------

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        result = await call_next(context)
        tool = getattr(context.message, "name", "?")
        rule = self.service.config.rule_for(tool)
        if rule is None:
            return result

        content = list(getattr(result, "content", None) or [])
        new_content: list[Any] = []
        changed = False
        for block in content:
            new_block, block_changed = await self._handle_block(
                block, rule=rule, tool=tool
            )
            new_content.append(new_block)
            changed = changed or block_changed

        structured = getattr(result, "structured_content", None)
        new_structured = structured
        if isinstance(structured, (dict, list)):
            new_structured, structured_changed = await self._walk_json(
                structured, rule=rule, tool=tool
            )
            changed = changed or structured_changed

        if not changed:
            return result
        result.content = new_content
        if new_structured is not structured:
            try:
                result.structured_content = new_structured
            except Exception:  # pragma: no cover - frozen result models
                logger.warning("attachment offload: could not rewrite structured_content")
        return result


def _mime_of(obj: Any) -> str | None:
    """``mime_type`` where the SDK offers it, ``mimeType`` on older models."""
    for attr in ("mime_type", "mimeType"):
        try:
            value = getattr(obj, attr, None)
        except Exception:  # pragma: no cover - deprecation shims can raise
            continue
        if isinstance(value, str) and value:
            return value
    return None


def _first(d: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    return None
