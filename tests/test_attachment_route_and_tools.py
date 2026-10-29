"""The fetch half: ``GET /artifacts/{id}?cap=…`` and ``view_image``.

Driven through the gateway's own ASGI app (httpx ASGITransport), so the
route is exercised the way `curl` exercises it — path, query string,
status codes, headers — without binding a port.
"""

from __future__ import annotations

import base64
import hashlib
import io

import httpx
import mcp.types as mt
import pytest
from fastmcp import Client, FastMCP
from PIL import Image as PILImage

from mcp_gateway.attachments.cap import mint_cap
from mcp_gateway.attachments.config import load_config
from mcp_gateway.attachments.routes import register_artifact_route
from mcp_gateway.attachments.service import AttachmentService
from mcp_gateway.attachments.store import build_store
from mcp_gateway.attachments.tools import register_attachment_tools


def png_bytes(side: int) -> bytes:
    import random

    rnd = random.Random(side)
    img = PILImage.new("RGB", (side, side))
    img.putdata(
        [(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(side * side)]
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


BIG_PNG = png_bytes(300)


def build(tmp_path) -> tuple[FastMCP, AttachmentService]:
    env = {
        "MCP_ATTACHMENT_OFFLOAD": "1",
        "MCP_ATTACHMENT_BACKEND": "file",
        "MCP_ATTACHMENT_DIR": str(tmp_path),
        "MCP_ATTACHMENT_SIGNING_KEY": "test-key",
        "MCP_ATTACHMENT_BASE_URL": "http://testserver",
    }
    cfg = load_config(env=env)
    service = AttachmentService(cfg, build_store(cfg.storage, env=env))
    server = FastMCP("artifact-host")
    register_artifact_route(server, service)
    register_attachment_tools(server, service)
    return server, service


async def client_for(server: FastMCP) -> httpx.AsyncClient:
    app = server.http_app(stateless_http=True)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


async def test_curl_with_the_signed_url_returns_the_exact_bytes(tmp_path):
    server, service = build(tmp_path)
    env = await service.offload(BIG_PNG, declared_filename="photo.png")
    async with await client_for(server) as http:
        r = await http.get(env.url.replace("http://testserver", ""))
    assert r.status_code == 200
    assert r.content == BIG_PNG
    assert hashlib.sha256(r.content).hexdigest() == env.sha256
    assert r.headers["content-type"].startswith("image/png")
    assert 'filename="photo.png"' in r.headers["content-disposition"]


async def test_no_cap_is_401_and_a_bad_cap_is_403(tmp_path):
    server, service = build(tmp_path)
    env = await service.offload(BIG_PNG)
    path = env.url.replace("http://testserver", "").split("?")[0]
    async with await client_for(server) as http:
        assert (await http.get(path)).status_code == 401
        assert (await http.get(f"{path}?cap=abc.def")).status_code == 403
        # A cap minted for a different object does not open this one.
        other = mint_cap("deadbeef.png", signing_key=b"test-key", ttl_seconds=60)
        assert (await http.get(f"{path}?cap={other}")).status_code == 403


async def test_an_expired_cap_is_refused(tmp_path):
    server, service = build(tmp_path)
    env = await service.offload(BIG_PNG)
    artifact_id = env.url.split("/")[-1].split("?")[0]
    stale = mint_cap(
        artifact_id, signing_key=b"test-key", ttl_seconds=1, now=0.0
    )  # expired in 1970
    async with await client_for(server) as http:
        r = await http.get(f"/artifacts/{artifact_id}?cap={stale}")
    assert r.status_code == 403
    assert "expired" in r.json()["error"]


async def test_a_valid_cap_for_a_missing_object_is_404(tmp_path):
    server, _ = build(tmp_path)
    cap = mint_cap("doesnotexist.png", signing_key=b"test-key", ttl_seconds=60)
    async with await client_for(server) as http:
        r = await http.get(f"/artifacts/doesnotexist.png?cap={cap}")
    assert r.status_code == 404


async def test_view_image_returns_a_real_image_block_downscaled(tmp_path):
    server, service = build(tmp_path)
    env = await service.offload(BIG_PNG, declared_filename="photo.png")
    async with Client(server) as client:
        result = await client.call_tool("view_image", {"url": env.url, "max_dimension": 128})
    blocks = [b for b in result.content if isinstance(b, mt.ImageContent)]
    assert len(blocks) == 1
    img = PILImage.open(io.BytesIO(base64.b64decode(blocks[0].data)))
    assert max(img.size) == 128
    assert len(base64.b64decode(blocks[0].data)) < len(BIG_PNG)


async def test_view_image_accepts_a_bare_artifact_id(tmp_path):
    server, service = build(tmp_path)
    env = await service.offload(BIG_PNG)
    artifact_id = env.url.split("/")[-1].split("?")[0]
    async with Client(server) as client:
        result = await client.call_tool("view_image", {"url": artifact_id})
    assert any(isinstance(b, mt.ImageContent) for b in result.content)


async def test_view_image_declines_a_pdf_politely(tmp_path):
    server, service = build(tmp_path)
    env = await service.offload(b"%PDF-1.7\n" + b"x" * 100_000, declared_filename="cv.pdf")
    async with Client(server) as client:
        result = await client.call_tool("view_image", {"url": env.url})
    text = " ".join(b.text for b in result.content if isinstance(b, mt.TextContent))
    assert "ask_about_attachment" in text


async def test_ask_about_attachment_says_so_when_unconfigured(tmp_path, monkeypatch):
    server, service = build(tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    env = await service.offload(BIG_PNG)
    async with Client(server) as client:
        result = await client.call_tool(
            "ask_about_attachment", {"url": env.url, "question": "what is this?"}
        )
    text = " ".join(b.text for b in result.content if isinstance(b, mt.TextContent))
    assert "GEMINI_API_KEY" in text


@pytest.mark.parametrize("ref_shape", ["url", "path+query", "bare"])
async def test_refs_are_accepted_in_every_shape_a_model_might_paste(tmp_path, ref_shape):
    _, service = build(tmp_path)
    env = await service.offload(BIG_PNG)
    artifact_id = env.url.split("/")[-1].split("?")[0]
    cap = env.url.split("cap=")[1]
    ref = {
        "url": env.url,
        "path+query": f"/artifacts/{artifact_id}?cap={cap}",
        "bare": artifact_id,
    }[ref_shape]
    artifact = await service.fetch(ref)
    assert artifact.data == BIG_PNG
