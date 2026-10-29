"""The offload middleware, driven through a real FastMCP server.

A stub server returns the four shapes real MCPs use for bytes (image
block, blob resource, base64-in-JSON, bare base64 text); the middleware
is mounted on it exactly as ``create_gateway`` mounts it, and an
in-memory client calls the tools. No network, no R2 — the store is a
temp directory, which is the same interface.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json

import mcp.types as mt
import pytest
from fastmcp import Client, FastMCP
from fastmcp.tools import ToolResult
from PIL import Image as PILImage

from mcp_gateway.attachments.config import load_config
from mcp_gateway.attachments.middleware import AttachmentOffloadMiddleware
from mcp_gateway.attachments.service import AttachmentService
from mcp_gateway.attachments.store import build_store


def png_bytes(side: int) -> bytes:
    """A noisy PNG, so it doesn't compress down past the threshold."""
    import random

    rnd = random.Random(side)
    img = PILImage.new("RGB", (side, side))
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(side * side)])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


BIG_PNG = png_bytes(300)  # ~270 KB
SMALL_PNG = png_bytes(40)  # ~5 KB
PDF = b"%PDF-1.7\n" + b"x" * 200_000


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_service(tmp_path, **env_extra) -> AttachmentService:
    env = {
        "MCP_ATTACHMENT_OFFLOAD": "1",
        "MCP_ATTACHMENT_BACKEND": "file",
        "MCP_ATTACHMENT_DIR": str(tmp_path),
        "MCP_ATTACHMENT_SIGNING_KEY": "test-key",
        "MCP_ATTACHMENT_BASE_URL": "http://127.0.0.1:3199",
        **env_extra,
    }
    cfg = load_config(env=env)
    return AttachmentService(cfg, build_store(cfg.storage, env=env))


def build_server(service: AttachmentService) -> FastMCP:
    server = FastMCP("stub")
    server.add_middleware(AttachmentOffloadMiddleware(service))

    @server.tool(name="whatsapp_download_media")
    def whatsapp_download_media(size: str = "big") -> ToolResult:
        data = BIG_PNG if size == "big" else SMALL_PNG
        return ToolResult(
            content=[mt.ImageContent(type="image", data=b64(data), mimeType="image/png")]
        )

    @server.tool(name="linkedin_fetch_attachment")
    def linkedin_fetch_attachment() -> ToolResult:
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

    @server.tool(name="imessage_get_attachment")
    def imessage_get_attachment() -> ToolResult:
        doc = {
            "message_rowid": 42,
            "filename": "IMG_4242.heic",
            "mime_type": "application/octet-stream",
            "data": b64(BIG_PNG),
            "note": "attachment 0 of 1",
        }
        return ToolResult(content=[mt.TextContent(type="text", text=json.dumps(doc))])

    @server.tool(name="get_asset_thumbnail")
    def get_asset_thumbnail() -> ToolResult:
        return ToolResult(content=[mt.TextContent(type="text", text=b64(BIG_PNG))])

    @server.tool(name="whatsapp_read_thread")
    def whatsapp_read_thread() -> ToolResult:
        return ToolResult(
            content=[mt.ImageContent(type="image", data=b64(BIG_PNG), mimeType="image/png")]
        )

    return server


def result_text(result) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, mt.TextContent))


def envelope(result) -> dict:
    text = result_text(result)
    start = text.index("{")
    end = text.index("}", start) + 1
    return json.loads(text[start:end])


async def test_big_image_is_offloaded_and_the_bytes_survive(tmp_path):
    service = build_service(tmp_path)
    async with Client(build_server(service)) as client:
        result = await client.call_tool("whatsapp_download_media", {"size": "big"})

    assert not any(isinstance(b, mt.ImageContent) for b in result.content)
    env = envelope(result)
    assert env["mime"] == "image/png"
    assert env["size"] == len(BIG_PNG)
    assert env["sha256"] == sha(BIG_PNG)
    assert env["url"].startswith("http://127.0.0.1:3199/artifacts/")
    assert "cap=" in env["url"]

    artifact = await service.fetch(env["url"])
    assert artifact.data == BIG_PNG
    assert sha(artifact.data) == env["sha256"]


async def test_the_result_collapses_by_two_orders_of_magnitude(tmp_path):
    service = build_service(tmp_path)
    before = len(json.dumps({"data": b64(BIG_PNG), "mimeType": "image/png"}))
    async with Client(build_server(service)) as client:
        result = await client.call_tool("whatsapp_download_media", {"size": "big"})
    after = len(result_text(result))
    assert after < 700
    assert before / after > 100


async def test_small_image_stays_inline_as_an_image_block(tmp_path):
    service = build_service(tmp_path)
    async with Client(build_server(service)) as client:
        result = await client.call_tool("whatsapp_download_media", {"size": "small"})
    blocks = [b for b in result.content if isinstance(b, mt.ImageContent)]
    assert len(blocks) == 1
    assert base64.b64decode(blocks[0].data) == SMALL_PNG


async def test_blob_resource_is_offloaded_with_its_filename(tmp_path):
    service = build_service(tmp_path)
    async with Client(build_server(service)) as client:
        result = await client.call_tool("linkedin_fetch_attachment", {})
    assert not any(isinstance(b, mt.EmbeddedResource) for b in result.content)
    env = envelope(result)
    assert env["mime"] == "application/pdf"
    assert env["filename"] == "resume.pdf"
    assert env["size"] == len(PDF)


async def test_base64_inside_json_is_replaced_in_place(tmp_path):
    service = build_service(tmp_path)
    async with Client(build_server(service)) as client:
        result = await client.call_tool("imessage_get_attachment", {})
    doc = json.loads(result_text(result))
    # Structure preserved; only the payload field changed.
    assert doc["message_rowid"] == 42
    assert doc["note"] == "attachment 0 of 1"
    assert "attachment offloaded" in doc["data"]
    env = json.loads(doc["data"][doc["data"].index("{") : doc["data"].index("}") + 1])
    assert env["filename"] == "IMG_4242.heic"  # the server's name is kept
    assert env["mime"] == "image/png"  # ...but the magic number wins over its mime
    assert len(result_text(result)) < 1000


async def test_bare_base64_text_is_offloaded(tmp_path):
    service = build_service(tmp_path)
    async with Client(build_server(service)) as client:
        result = await client.call_tool("get_asset_thumbnail", {})
    env = envelope(result)
    assert env["mime"] == "image/png"
    assert env["sha256"] == sha(BIG_PNG)


async def test_unmatched_tools_are_untouched(tmp_path):
    service = build_service(tmp_path)
    async with Client(build_server(service)) as client:
        result = await client.call_tool("whatsapp_read_thread", {})
    blocks = [b for b in result.content if isinstance(b, mt.ImageContent)]
    assert len(blocks) == 1 and base64.b64decode(blocks[0].data) == BIG_PNG


async def test_dedup_uploads_identical_bytes_once(tmp_path):
    service = build_service(tmp_path)
    server = build_server(service)
    async with Client(server) as client:
        a = envelope(await client.call_tool("whatsapp_download_media", {"size": "big"}))
        b = envelope(await client.call_tool("get_asset_thumbnail", {}))
    assert a["sha256"] == b["sha256"]
    stored = [p for p in tmp_path.rglob("*") if p.is_file() and not p.name.endswith(".meta.json")]
    assert len(stored) == 1


async def test_oversized_payloads_are_dropped_not_stored(tmp_path):
    service = build_service(tmp_path, MCP_ATTACHMENT_MAX_BYTES="1000")
    async with Client(build_server(service)) as client:
        result = await client.call_tool("whatsapp_download_media", {"size": "big"})
    text = result_text(result)
    assert "exceeds the gateway's max_bytes" in text
    assert b64(BIG_PNG)[:64] not in text
    assert not list(tmp_path.rglob("*.png"))


async def test_store_failure_withholds_the_bytes_by_default(tmp_path):
    service = build_service(tmp_path)

    async def boom(*a, **k):
        raise RuntimeError("R2 is down")

    service.store.put = boom  # type: ignore[method-assign]
    async with Client(build_server(service)) as client:
        result = await client.call_tool("whatsapp_download_media", {"size": "big"})
    text = result_text(result)
    assert "could not be stored" in text and "R2 is down" in text
    assert b64(BIG_PNG)[:64] not in text


async def test_store_failure_can_be_configured_to_pass_through(tmp_path):
    service = build_service(tmp_path, MCP_ATTACHMENT_ON_ERROR="passthrough")

    async def boom(*a, **k):
        raise RuntimeError("R2 is down")

    service.store.put = boom  # type: ignore[method-assign]
    async with Client(build_server(service)) as client:
        result = await client.call_tool("whatsapp_download_media", {"size": "big"})
    blocks = [b for b in result.content if isinstance(b, mt.ImageContent)]
    assert len(blocks) == 1 and base64.b64decode(blocks[0].data) == BIG_PNG


@pytest.mark.parametrize(
    "data,expected",
    [
        (BIG_PNG, "image/png"),
        (b"%PDF-1.4\n" + b"z" * 100_000, "application/pdf"),
        (b"OggS" + b"\0" * 100_000, "audio/ogg"),
        (b"\x00" * 100_000, "application/octet-stream"),
    ],
)
async def test_mime_is_sniffed_from_the_bytes(tmp_path, data, expected):
    service = build_service(tmp_path)
    env = await service.offload(data, declared_mime="application/octet-stream")
    assert env.mime == expected
    assert env.filename.endswith(
        {"image/png": ".png", "application/pdf": ".pdf", "audio/ogg": ".ogg"}.get(
            expected, ".bin"
        )
    )


async def test_text_that_merely_looks_long_is_not_mistaken_for_base64(tmp_path):
    service = build_service(tmp_path)
    cfg_rule = service.config.rule_for("get_asset_thumbnail")
    server = FastMCP("prose")
    server.add_middleware(AttachmentOffloadMiddleware(service))

    prose = "The quick brown fox jumps over the lazy dog. " * 5000

    @server.tool(name="get_asset_thumbnail")
    def tool() -> str:
        return prose

    async with Client(server) as client:
        result = await client.call_tool("get_asset_thumbnail", {})
    assert len(prose) > cfg_rule.min_bytes
    assert result_text(result) == prose
