#!/usr/bin/env python
"""Ask each provider whether it will accept our tool advertisement.

    uv run tools/check_advertisement.py                 # against the live gateway
    uv run tools/check_advertisement.py --mode names    # against a local transform

WHY THIS EXISTS, in one sentence: on 2026-09-21 a schema this repo generated was
rejected outright by Gemini, in production, and no test here could have caught
it, because the thing that rejected it was Gemini.

The bug was one missing field. ``names`` mode emitted ``{"type": "array"}`` with
no ``items`` for 44 properties across 109 tools, and Gemini answers that with

    GenerateContentRequest.tools[0].function_declarations[0]
      .parameters.properties[excludeTags].items: missing field

— which fails the WHOLE request, not that one tool. Every tool became
uncallable for that provider, and the symptom at the other end was a model
that simply stopped using tools, with nothing in our logs to say why.

The lesson generalises past the one bug. We do not consume these schemas; three
providers do, each with its own unpublished subset of JSON Schema, and the only
authority on whether an advertisement is valid is the provider itself. Unit
tests assert what we *meant*. This asks what they will *take*.

It is cheap — one minimal request per provider ("hi"), no tool actually
invoked — so there is no reason not to run it before a deploy that touches
schema shape.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mcp_gateway.middleware.stub_schemas import (  # noqa: E402
    first_sentence,
    names_schema,
    signature_of,
)

TIMEOUT = 60.0


async def anthropic(tools: list[dict[str, Any]]) -> tuple[bool, str]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return True, "skipped (no ANTHROPIC_API_KEY)"
    body = {
        "model": "claude-haiku-5-5",
        # Only the status code matters, but leave adaptive thinking room.
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"name": t["name"], "description": t.get("description") or "", "input_schema": t["schema"]}
            for t in tools
        ],
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json=body,
        )
    return r.status_code == 200, _why(r)


async def gemini(tools: list[dict[str, Any]]) -> tuple[bool, str]:
    key = os.environ.get("GOOGLE_GENERATIVE_AI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key:
        return True, "skipped (no GOOGLE_GENERATIVE_AI_API_KEY)"
    # Gemini rejects the empty-object schema outright ("should be non-empty for
    # OBJECT type"), so a tool with no arguments is sent with no parameters at
    # all — which is what a real client does too.
    decls = []
    for t in tools:
        d: dict[str, Any] = {"name": t["name"], "description": t.get("description") or ""}
        if (t["schema"].get("properties") or {}) != {}:
            d["parameters"] = t["schema"]
        decls.append(d)
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{os.environ.get('GEMINI_CHECK_MODEL', 'gemini-3.6-flash')}:generateContent",
            headers={"x-goog-api-key": key},
            json={
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                "tools": [{"function_declarations": decls}],
                "generationConfig": {"maxOutputTokens": 1},
            },
        )
    return r.status_code == 200, _why(r)


async def openai(tools: list[dict[str, Any]]) -> tuple[bool, str]:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return True, "skipped (no OPENAI_API_KEY)"
    body = {
        "model": "gpt-4o-mini",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description") or "",
                    "parameters": t["schema"],
                },
            }
            for t in tools
        ],
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=body,
        )
    return r.status_code == 200, _why(r)


def _why(r: httpx.Response) -> str:
    if r.status_code == 200:
        return "accepted"
    try:
        err = r.json().get("error", {})
        return f"HTTP {r.status_code}: {err.get('message', json.dumps(err))[:240]}"
    except Exception:
        return f"HTTP {r.status_code}: {r.text[:240]}"


def load_live() -> list[dict[str, Any]]:
    """Whatever the running gateway is advertising right now."""
    path = Path(os.environ.get("TOOLS_JSON", "/tmp/gw-tools-full.json"))
    raw = json.loads(path.read_text())
    return [
        {"name": t["name"], "description": t.get("description"), "schema": t.get("inputSchema") or {}}
        for t in raw
    ]


def transform(tools: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    out = []
    for t in tools:
        if mode == "names":
            schema, desc = names_schema(t["schema"]), first_sentence(t.get("description") or "")
        elif mode == "stub":
            schema = {"type": "object"}
            desc = f"{signature_of(t['name'], t['schema'])} — {first_sentence(t.get('description') or '')}"
        else:
            schema, desc = t["schema"], t.get("description") or ""
        out.append({"name": t["name"], "description": desc, "schema": schema})
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["live", "names", "stub", "full"], default="live")
    args = ap.parse_args()

    tools = load_live()
    if args.mode != "live":
        tools = transform(tools, args.mode)
    print(f"{len(tools)} tools, mode={args.mode}\n")

    failed = 0
    for label, fn in (("anthropic", anthropic), ("gemini", gemini), ("openai", openai)):
        ok, why = await fn(tools)
        print(f"  {'ok  ' if ok else 'FAIL'} {label:10} {why}")
        if not ok:
            failed += 1
            # Name the offending tool. The provider points at an argument, not
            # a tool, and hunting it by hand is the slow part.
            bad = json.dumps({"schema": [t for t in tools]})
            for t in tools:
                for k, v in (t["schema"].get("properties") or {}).items():
                    if k in why and v.get("type") == "array" and "items" not in v:
                        print(f"       -> {t['name']}.{k} is an array with no `items`")
            del bad
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
