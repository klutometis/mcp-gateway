"""ImageDownscaleMiddleware must relabel what it re-encodes.

mcp 2.x renamed ImageContent's field to ``mime_type`` (``mimeType`` is now
only the wire alias). ``model_copy(update=...)`` bypasses validation and
aliases, so an update keyed ``"mimeType"`` sets a stray attribute and leaves
the real field alone: the WebP bytes went out labelled ``image/jpeg``. Seen on
mcp.danenbros.com, 2026-11-10: a 1280x720 JPEG screenshot arrived as a
1024-wide WebP whose block still said image/jpeg.
"""

import base64
import io

import mcp.types as mt
from PIL import Image

from mcp_gateway.middleware.image_downscale import ImageDownscaleMiddleware


def _jpeg_block(w: int = 1280, h: int = 720) -> mt.ImageContent:
    img = Image.effect_noise((w, h), 64).convert("RGB")  # noise: won't shrink below min_bytes
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return mt.ImageContent(
        type="image", data=base64.b64encode(buf.getvalue()).decode("ascii"), mimeType="image/jpeg"
    )


def _wire(block: mt.ImageContent) -> dict:
    return block.model_dump(by_alias=True, exclude_none=True)


def _format_of(block: mt.ImageContent) -> str:
    return Image.open(io.BytesIO(base64.b64decode(block.data))).format


def test_reencoded_image_is_relabelled():
    out = ImageDownscaleMiddleware(max_dim=1024, fmt="webp")._shrink(_jpeg_block())
    assert _format_of(out) == "WEBP"
    assert _wire(out)["mimeType"] == "image/webp"


def test_label_matches_bytes_for_every_format():
    for fmt, pil, mime in (("webp", "WEBP", "image/webp"), ("jpeg", "JPEG", "image/jpeg"), ("png", "PNG", "image/png")):
        out = ImageDownscaleMiddleware(max_dim=512, fmt=fmt)._shrink(_jpeg_block())
        assert _format_of(out) == pil, fmt
        assert _wire(out)["mimeType"] == mime, fmt


def test_untouched_image_keeps_its_label():
    small = _jpeg_block(64, 64)  # under min_bytes: passed through as is
    out = ImageDownscaleMiddleware()._shrink(small)
    assert out is small
    assert _wire(out)["mimeType"] == "image/jpeg"
