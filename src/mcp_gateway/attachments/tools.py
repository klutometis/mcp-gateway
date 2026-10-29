"""Gateway-native tools paired with the offload middleware.

``plans/messaging-attachments-r2.md`` Patterns 2 and 3. Both live at the
gateway, not in any upstream MCP, because both need to read back from
the store the gateway wrote to.

- :func:`view_image` — Pattern 2. Opt-in vision: the model asks for
  pixels when it actually needs them, bounded by ``max_dimension``.
- ``ask_about_attachment`` — Pattern 3. Subagent delegation: a cheap
  multimodal model reads the bytes in a *separate* context and answers
  one question. Handles PDFs, audio and video, which ``view_image``
  cannot.
"""

from __future__ import annotations

import base64
import io
import logging
from collections import OrderedDict
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from PIL import Image as PILImage

from mcp_gateway.attachments.service import AttachmentService

logger = logging.getLogger(__name__)

__all__ = ["register_attachment_tools"]

_ASK_CACHE_MAX = 64


def register_attachment_tools(gateway: FastMCP, service: AttachmentService) -> None:
    """Register ``view_image`` (+ ``ask_about_attachment`` if configured)."""

    max_dim_default = service.config.view_max_dimension

    @gateway.tool(
        name="view_image",
        title="View an offloaded attachment as an image",
        description=(
            "Look at an image the gateway offloaded to object storage. Pass the "
            "`url` from an attachment envelope (or its bare sha256 id); the "
            "image comes back as a real image content block, downscaled to "
            "`max_dimension` on its longest side so it costs ~1.5k tokens "
            "rather than the whole file. Images only — for PDFs, audio or "
            "video use ask_about_attachment."
        ),
    )
    async def view_image(url: str, max_dimension: int | None = None) -> Any:
        dim = int(max_dimension or max_dim_default)
        artifact = await service.fetch(url)
        try:
            img = PILImage.open(io.BytesIO(artifact.data))
            img.load()
        except Exception as exc:
            return (
                f"{artifact.filename} ({artifact.mime}, {artifact.size:,} bytes) is "
                f"not an image Pillow can open ({type(exc).__name__}). Use "
                f"ask_about_attachment for non-image attachments."
            )
        w, h = img.size
        longest = max(w, h)
        if longest > dim:
            scale = dim / longest
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=80)
        logger.info(
            "view_image: %s %dx%d -> %dx%d, %d bytes",
            artifact.key,
            w,
            h,
            img.size[0],
            img.size[1],
            len(buf.getvalue()),
        )
        return Image(data=buf.getvalue(), format="webp")

    ask_cfg = service.config.ask
    if not ask_cfg.enabled:
        logger.info("attachment offload: ask_about_attachment disabled by config")
        return

    cache: OrderedDict[tuple[str, str, str], str] = OrderedDict()

    @gateway.tool(
        name="ask_about_attachment",
        title="Ask a cheap multimodal model about an attachment",
        description=(
            "Ask one question about an offloaded attachment without pulling it "
            "into this context. The gateway fetches the bytes and hands them, "
            "with your question, to a cheap multimodal model in a separate "
            "context; you get back only the answer. Works for images, PDFs, "
            "audio and video. Ask for verbatim quotes when the answer matters — "
            "you cannot see the source."
        ),
    )
    async def ask_about_attachment(
        url: str, question: str, model: str | None = None
    ) -> str:
        import os

        api_key = os.environ.get(ask_cfg.api_key_env, "")
        if not api_key:
            return (
                f"ask_about_attachment is unavailable: {ask_cfg.api_key_env} is not "
                f"set in the gateway's environment. Use view_image for images, or "
                f"fetch the URL yourself."
            )
        chosen = model or ask_cfg.model
        artifact = await service.fetch(url)
        ck = (artifact.key, question, chosen)
        if ck in cache:
            cache.move_to_end(ck)
            return cache[ck]

        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": artifact.mime,
                                "data": base64.b64encode(artifact.data).decode("ascii"),
                            }
                        },
                        {"text": question},
                    ]
                }
            ]
        }
        endpoint = f"{ask_cfg.endpoint.rstrip('/')}/{chosen}:generateContent"
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                endpoint, params={"key": api_key}, json=payload
            )
        if r.status_code >= 300:
            return f"ask_about_attachment failed: {chosen} returned {r.status_code}: {r.text[:300]}"
        try:
            parts = r.json()["candidates"][0]["content"]["parts"]
            answer = "".join(p.get("text", "") for p in parts).strip()
        except Exception as exc:
            return f"ask_about_attachment: could not parse {chosen}'s reply ({exc})"
        answer = answer[: ask_cfg.max_answer_chars]
        cache[ck] = answer
        if len(cache) > _ASK_CACHE_MAX:
            cache.popitem(last=False)
        return answer
