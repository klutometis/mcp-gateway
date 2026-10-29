"""End-to-end check of attachment offload against a running gateway.

Not run by pytest (``norecursedirs = tests/manual``): it needs a live
gateway with the stub server mounted and R2 credentials in the
environment.

    # terminal 1 — a SECOND gateway, so the one on :3100 is untouched
    cd ~/prg/mcp-gateway-config
    MCP_PORT=3199 \
    MCP_SERVERS_FILE=/path/to/servers.e2e.json \
    MCP_ATTACHMENT_OFFLOAD=1 \
    MCP_ATTACHMENT_BASE_URL=http://127.0.0.1:3199 \
      uv run gateway-local

    # terminal 2
    uv run python tests/manual/attachment_e2e.py http://127.0.0.1:3199/mcp

It checks, and prints the numbers for, each payload shape:

- the result the model would have got (base64) vs. the one it gets now;
- that the signed URL fetches with plain ``curl`` semantics (no header);
- that the bytes at the far end are the same bytes, by sha256;
- that ``view_image`` returns a real image block;
- that a small image is still inline;
- that an unmatched tool is untouched.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys

import httpx
import mcp.types as mt
from fastmcp import Client

GATEWAY = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3199/mcp"

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"{PASS if ok else FAIL}  {name}" + (f" — {detail}" if detail else ""))
    return ok


def text_of(result) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, mt.TextContent))


def envelope_of(result) -> dict:
    """Find the envelope wherever it landed — a block of its own, or a
    field inside the JSON document the upstream returned."""
    text = text_of(result)
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(obj, dict):
            if "url" in obj and "sha256" in obj:
                return obj
            for value in obj.values():  # envelope nested in a JSON field
                if isinstance(value, str) and "attachment offloaded" in value:
                    return envelope_of_text(value)
    raise AssertionError(f"no envelope in result: {text[:200]!r}")


def envelope_of_text(text: str) -> dict:
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
            except ValueError:
                continue
            if isinstance(obj, dict) and "url" in obj:
                return obj
    raise AssertionError(f"no envelope in text: {text[:200]!r}")


def tokens(chars: int) -> int:
    """Rough: ~4 characters per token for base64 and JSON alike."""
    return chars // 4


async def main() -> int:
    async with Client(GATEWAY) as client:
        names = {t.name for t in await client.list_tools()}
        check("gateway advertises view_image", "view_image" in names)
        check(
            "gateway advertises ask_about_attachment",
            "ask_about_attachment" in names,
            "(absent is fine if disabled in config)",
        )

        info = json.loads(text_of(await client.call_tool("payload_info", {"which": "big_image"})))
        pdf_info = json.loads(text_of(await client.call_tool("payload_info", {"which": "pdf"})))

        print(f"\n{'tool':<26}{'before (chars)':>16}{'after (chars)':>15}{'ratio':>9}")
        for tool, args, expect_sha, label in (
            ("download_media", {"size": "big"}, info["sha256"], "image block"),
            ("fetch_attachment", {}, pdf_info["sha256"], "blob resource"),
            ("imessage_get_attachment", {}, info["sha256"], "base64 in JSON"),
            ("get_asset_thumbnail", {}, info["sha256"], "bare base64"),
        ):
            result = await client.call_tool(tool, args)
            after = len(text_of(result))
            before = (pdf_info if tool == "fetch_attachment" else info)["base64_chars"]
            print(f"{tool:<26}{before:>16,}{after:>15,}{before / after:>8.0f}x")
            env = envelope_of(result)
            check(f"{tool} ({label}) returns an envelope", "url" in env and "sha256" in env)
            check(f"{tool} sha256 matches the source bytes", env["sha256"] == expect_sha)
            print(
                f"     ~{tokens(before):,} tokens -> ~{tokens(after):,} tokens"
                f"  [{env['mime']}, {env['size']:,} B]"
            )

            # The far end, fetched the way a headless client would.
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as http:
                r = await http.get(env["url"])
            check(f"{tool} URL fetches 200 with no auth header", r.status_code == 200, str(r.status_code))
            check(
                f"{tool} fetched bytes match by sha256",
                hashlib.sha256(r.content).hexdigest() == env["sha256"],
            )

            # And without the cap it must not.
            async with httpx.AsyncClient(timeout=60) as http:
                bare = await http.get(env["url"].split("?")[0])
            check(f"{tool} URL without a cap is refused", bare.status_code == 401, str(bare.status_code))

        # view_image on the offloaded image
        env = envelope_of(await client.call_tool("download_media", {"size": "big"}))
        viewed = await client.call_tool("view_image", {"url": env["url"], "max_dimension": 512})
        images = [b for b in viewed.content if isinstance(b, mt.ImageContent)]
        check("view_image returns an image content block", len(images) == 1)
        if images:
            raw = base64.b64decode(images[0].data)
            check(
                "view_image is downscaled, not the original",
                len(raw) < env["size"],
                f"{len(raw):,} B vs {env['size']:,} B",
            )

        # small image stays inline
        small = await client.call_tool("download_media", {"size": "small"})
        inline = [b for b in small.content if isinstance(b, mt.ImageContent)]
        check("small image stays inline as an image block", len(inline) == 1)

        # unmatched tool untouched
        # An unmatched tool keeps its bytes. (They may still be *smaller*
        # than the source: the image-downscale middleware runs outside this
        # one and re-encodes any image block left inline. The point here is
        # that it is still an image block, not an envelope.)
        untouched = await client.call_tool("read_thread", {})
        blocks = [b for b in untouched.content if isinstance(b, mt.ImageContent)]
        check(
            "unmatched tool (read_thread) keeps its bytes inline",
            len(blocks) == 1 and "attachment offloaded" not in text_of(untouched),
            f"{len(base64.b64decode(blocks[0].data)):,} B inline" if blocks else "",
        )

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
