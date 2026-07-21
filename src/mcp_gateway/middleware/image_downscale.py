"""Pre-emptively downscale image content blocks in tool results.

Why
---
Image-returning tools (chrome ``take_screenshot``, messaging
``fetch_attachment``/``download_media``, …) hand back full-resolution
images as MCP ``image`` content blocks — often 0.5–2 MB of base64. Even
when a downstream client tokenizes them correctly as vision (~1.5k
tokens), a full-page retina PNG is more pixels than any model needs to
act on, and the bytes accumulate. When a client mishandles the block and
inlines it as *text* (see llm-multiplex's history bug), a single shot is
~170k tokens.

This middleware caps the damage at the source, for **every** consumer
(various MCP clients: Claude Desktop, Cursor, etc.), by uniformly downscaling
any image block on the way out.

Design notes
------------
- **Generic, not per-MCP.** It keys on the MCP protocol invariant
  (``ImageContent``: ``type == "image"``), never on tool/server names.
  One registration covers chrome, whatsapp, gmail, linkedin, … No
  per-server knowledge. (Servers that hide image bytes inside *text* or
  JSON fields are non-compliant and out of scope; add a per-server
  adapter if one ever appears.)
- **Uniform resize, no content-aware cropping.** Empirically (arXiv
  2603.26041) uniform downscale preserves GUI layout/localization better
  than "smart" pruning. Keep it dumb.
- **Pre-emptive + globally configured.** Always-on by default; tuned via
  env, not per call. A per-call high-res escape hatch belongs in the
  tool surface (e.g. a future ``view_image(max_dimension)``), not here.
- **Fail-open.** Any decode/encode error → original block is passed
  through unchanged. A screenshot is never dropped because Pillow choked.
"""

from __future__ import annotations

import base64
import io
import logging
import os

import mcp.types as mt
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from PIL import Image

logger = logging.getLogger(__name__)

__all__ = ["ImageDownscaleMiddleware"]

# Formats we re-encode into. WEBP gives the best ratio; JPEG is the safe
# fallback for anything that must stay broadly compatible. PNG stays PNG
# only if a transparent image is detected and the target isn't WEBP.
_PIL_FORMAT = {"webp": "WEBP", "jpeg": "JPEG", "jpg": "JPEG", "png": "PNG"}
_MIME = {"WEBP": "image/webp", "JPEG": "image/jpeg", "PNG": "image/png"}


class ImageDownscaleMiddleware(Middleware):
    """Downscale every ``image`` block in tool results to ``max_dim`` px.

    Parameters
    ----------
    max_dim:
        Longest-side cap in pixels. Images already within it are left
        alone (no re-encode). Default 1024.
    fmt:
        Output format: ``"webp"`` (default), ``"jpeg"``, or ``"png"``.
    quality:
        Encoder quality for lossy formats (1–100). Default 80.
    min_bytes:
        Skip images whose decoded base64 is smaller than this; not worth
        a round trip. Default 16 KiB.
    """

    def __init__(
        self,
        *,
        max_dim: int = 1024,
        fmt: str = "webp",
        quality: int = 80,
        min_bytes: int = 16 * 1024,
    ) -> None:
        self.max_dim = max_dim
        self.fmt = _PIL_FORMAT.get(fmt.lower(), "WEBP")
        self.quality = quality
        self.min_bytes = min_bytes

    @classmethod
    def from_env(cls) -> "ImageDownscaleMiddleware | None":
        """Build from env, or ``None`` if disabled.

        Env (all optional):
          ``MCP_IMAGE_DOWNSCALE``        on/off (default "1")
          ``MCP_IMAGE_MAX_DIM``          longest side px (default 1024)
          ``MCP_IMAGE_FORMAT``           webp|jpeg|png (default webp)
          ``MCP_IMAGE_QUALITY``          1–100 (default 80)
          ``MCP_IMAGE_MIN_BYTES``        skip-below threshold (default 16384)
        """
        if os.environ.get("MCP_IMAGE_DOWNSCALE", "1").lower() in ("0", "false", "no"):
            return None
        return cls(
            max_dim=int(os.environ.get("MCP_IMAGE_MAX_DIM", "1024")),
            fmt=os.environ.get("MCP_IMAGE_FORMAT", "webp"),
            quality=int(os.environ.get("MCP_IMAGE_QUALITY", "80")),
            min_bytes=int(os.environ.get("MCP_IMAGE_MIN_BYTES", str(16 * 1024))),
        )

    def _shrink(self, block: mt.ImageContent) -> mt.ImageContent:
        """Return a downscaled copy of one image block, or the original."""
        try:
            raw = base64.b64decode(block.data)
        except Exception:
            return block
        if len(raw) < self.min_bytes:
            return block

        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception:
            return block

        w, h = img.size
        longest = max(w, h)
        needs_resize = longest > self.max_dim

        out_fmt = self.fmt
        if out_fmt != "WEBP" and img.mode in ("RGBA", "LA", "P"):
            # JPEG can't do alpha; flatten onto white.
            if out_fmt == "JPEG":
                bg = Image.new("RGB", img.size, (255, 255, 255))
                img = img.convert("RGBA")
                bg.paste(img, mask=img.split()[-1])
                img = bg

        if needs_resize:
            scale = self.max_dim / longest
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))

        buf = io.BytesIO()
        save_kwargs = {}
        if out_fmt in ("WEBP", "JPEG"):
            save_kwargs["quality"] = self.quality
        try:
            if out_fmt == "JPEG" and img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(buf, format=out_fmt, **save_kwargs)
        except Exception:
            return block

        new_bytes = buf.getvalue()
        # If we somehow made it bigger and didn't need to resize, keep original.
        if not needs_resize and len(new_bytes) >= len(raw):
            return block

        logger.info(
            "image_downscale: %dx%d %d bytes -> %s %d bytes",
            w,
            h,
            len(raw),
            out_fmt,
            len(new_bytes),
        )
        return block.model_copy(
            update={
                "data": base64.b64encode(new_bytes).decode("ascii"),
                "mimeType": _MIME[out_fmt],
            }
        )

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        result = await call_next(context)
        content = getattr(result, "content", None)
        if not content:
            return result

        changed = False
        new_content = []
        for block in content:
            if isinstance(block, mt.ImageContent):
                shrunk = self._shrink(block)
                new_content.append(shrunk)
                changed = changed or (shrunk is not block)
            else:
                new_content.append(block)

        if not changed:
            return result
        result.content = new_content
        return result
