"""A stdio MCP server that returns big binary payloads, four ways.

Stands in for WhatsApp / LinkedIn / iMessage / Roblox in the end-to-end
test (``tests/manual/attachment_e2e.py``), so the gateway can be driven
against every payload shape without touching anyone's real account.

Run it directly (``uv run python tests/manual/stub_attachment_server.py``)
or let the gateway spawn it as a stdio child.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import random

import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from PIL import Image

server = FastMCP("attachment-stub")


def _png(side: int, seed: int = 0) -> bytes:
    rnd = random.Random(seed or side)
    img = Image.new("RGB", (side, side))
    img.putdata(
        [
            (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
            for _ in range(side * side)
        ]
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


BIG_IMAGE = _png(600)  # ~1.1 MB, the "photo someone sent you" case
SMALL_IMAGE = _png(32)  # ~3 KB, the "stays inline" case
PDF = b"%PDF-1.7\n" + bytes(random.Random(7).randrange(256) for _ in range(300_000))
PAYLOADS = {"big_image": BIG_IMAGE, "small_image": SMALL_IMAGE, "pdf": PDF}


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@server.tool
def payload_info(which: str = "big_image") -> dict:
    """Size and sha256 of a payload, so the test can check the far end."""
    data = PAYLOADS[which]
    return {
        "which": which,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "base64_chars": len(b64(data)),
    }


@server.tool
def download_media(size: str = "big") -> ToolResult:
    """WhatsApp's shape: an MCP image content block of raw base64."""
    data = BIG_IMAGE if size == "big" else SMALL_IMAGE
    return ToolResult(
        content=[mt.ImageContent(type="image", data=b64(data), mimeType="image/png")]
    )


@server.tool
def fetch_attachment() -> ToolResult:
    """LinkedIn's shape: a blob resource (the PDF resume that killed a chat)."""
    return ToolResult(
        content=[
            mt.EmbeddedResource(
                type="resource",
                resource=mt.BlobResourceContents(
                    uri="file:///resume.pdf",
                    mimeType="application/pdf",
                    blob=b64(PDF),
                ),
            )
        ]
    )


@server.tool
def imessage_get_attachment() -> ToolResult:
    """iMessage's shape: base64 in a JSON field, with siblings worth keeping."""
    return ToolResult(
        content=[
            mt.TextContent(
                type="text",
                text=json.dumps(
                    {
                        "message_rowid": 90210,
                        "filename": "IMG_9021.png",
                        "mime_type": "application/octet-stream",
                        "data": b64(BIG_IMAGE),
                    }
                ),
            )
        ]
    )


@server.tool
def get_asset_thumbnail() -> ToolResult:
    """Roblox's shape: a bare base64 string in a text block."""
    return ToolResult(content=[mt.TextContent(type="text", text=b64(BIG_IMAGE))])


@server.tool
def read_thread() -> ToolResult:
    """A tool no rule matches: must come back untouched, bytes and all."""
    return ToolResult(
        content=[mt.ImageContent(type="image", data=b64(BIG_IMAGE), mimeType="image/png")]
    )


if __name__ == "__main__":
    server.run()
