"""Model-written catalog lines: never block, never regress, never repeat work.

The summarizer's value is easy to state and easy to break. It must improve a
line when it can and be invisible when it cannot — no blocked ``tools/list``,
no lost description because a model was slow or a key was missing, no second
call for a string it has already seen.

These run a real FastMCP server through an in-memory client, with the HTTP call
to Anthropic replaced. What is asserted is what a consumer receives.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from mcp_gateway.middleware.stub_schemas import StubSchemaMiddleware
from mcp_gateway.middleware.summarize import (
    SummarizeDescriptionsMiddleware,
    SummaryCache,
    _parse_lines,
    content_key,
    thinking_off,
)

# A description this gateway's regex gets wrong, and the direction of the
# error is worth knowing. `first_sentence` refuses to break after a capital
# letter — the guard that makes it handle "e.g." correctly — so a sentence
# ending in an acronym is not a sentence end either, and the trim silently
# fails to trim. "Extract content from a single URL." runs straight on into
# whatever follows.
#
# harness's copy of the same idea fails the other way: it has no capital-letter
# guard, so it stops dead inside "e.g.". Measured across the 109 tools here,
# each guard fixes two descriptions and breaks two others, which is why the
# answer is a model rather than a better regex.
AWKWARD = "Extract content from a single URL. Supports markdown and screenshots."
REGEX_GIVES = AWKWARD  # i.e. no trimming at all


class FakeModel:
    """Stands in for the Anthropic call. Records what it was asked."""

    def __init__(self, reply: dict[str, str] | str | None = None, *, fail: bool = False):
        self.reply = reply
        self.fail = fail
        self.calls: list[list[tuple[str, str]]] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, chunk):
        self.calls.append(list(chunk))
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("model unavailable")
        if isinstance(self.reply, str):
            return _parse_lines(self.reply)
        if self.reply is not None:
            return dict(self.reply)
        return {name: f"Summary of {name}." for name, _ in chunk}


def build(mw: SummarizeDescriptionsMiddleware, *, stub: bool = False) -> FastMCP:
    server = FastMCP("test")

    @server.tool
    def scrape(url: str, formats: str = "markdown") -> str:
        """Extract content from a single URL. Supports markdown and screenshots."""
        return "ok"

    @server.tool
    def ping() -> str:
        """Liveness check. Returns pong."""
        return "pong"

    if stub:
        server.add_middleware(StubSchemaMiddleware())
    server.add_middleware(mw)
    return server


def summarizer(model: FakeModel | None = None, **kw) -> SummarizeDescriptionsMiddleware:
    mw = SummarizeDescriptionsMiddleware(api_key="test-key", cache_path=None, **kw)
    if model is not None:
        mw._ask = model  # type: ignore[method-assign]
    return mw


async def settle(mw: SummarizeDescriptionsMiddleware) -> None:
    """Wait for the background fill, which is the whole point of it existing."""
    if mw._task is not None:
        await asyncio.wait_for(asyncio.shield(mw._task), timeout=5)


class TestNeverBlocks:
    async def test_the_first_list_is_served_from_the_regex(self) -> None:
        # A cold cache must not make anyone wait for a model. The first caller
        # gets today's answer; the improvement lands for the next one.
        model = FakeModel()
        model.gate = asyncio.Event()  # the model never returns during this call
        mw = summarizer(model)

        async with Client(build(mw)) as c:
            tools = {t.name: t for t in await c.list_tools()}

        assert tools["scrape"].description == REGEX_GIVES
        model.gate.set()
        await settle(mw)

    async def test_the_next_list_is_served_from_the_summary(self) -> None:
        mw = summarizer(FakeModel({"scrape": "Fetch a page and return its text.", "ping": "Ping."}))
        server = build(mw)

        async with Client(server) as c:
            await c.list_tools()
            await settle(mw)
            tools = {t.name: t for t in await c.list_tools()}

        assert tools["scrape"].description == "Fetch a page and return its text."

    async def test_a_slow_model_does_not_delay_a_list(self) -> None:
        model = FakeModel()
        model.gate = asyncio.Event()
        mw = summarizer(model)

        async with Client(build(mw)) as c:
            await asyncio.wait_for(c.list_tools(), timeout=2)

        model.gate.set()
        await settle(mw)


class TestFallsBackRatherThanFails:
    async def test_a_failing_model_leaves_the_regex_line(self) -> None:
        mw = summarizer(FakeModel(fail=True))
        server = build(mw)

        async with Client(server) as c:
            await c.list_tools()
            await settle(mw)
            tools = {t.name: t for t in await c.list_tools()}

        # Degraded to exactly today's behaviour, and nothing raised.
        assert tools["scrape"].description == REGEX_GIVES

    async def test_an_overlong_summary_is_rejected(self) -> None:
        # A run-on answer would bloat the very prompt this is meant to shrink,
        # so it is refused rather than truncated: the regex line is a known
        # quantity and half a model sentence is not.
        mw = summarizer(FakeModel({"scrape": "x" * 500}), max_chars=180)
        server = build(mw)

        async with Client(server) as c:
            await c.list_tools()
            await settle(mw)
            tools = {t.name: t for t in await c.list_tools()}

        assert tools["scrape"].description == REGEX_GIVES

    async def test_an_empty_summary_is_rejected(self) -> None:
        mw = summarizer(FakeModel({"scrape": "   "}))
        server = build(mw)

        async with Client(server) as c:
            await c.list_tools()
            await settle(mw)
            tools = {t.name: t for t in await c.list_tools()}

        assert tools["scrape"].description == REGEX_GIVES

    async def test_no_api_key_means_no_middleware(self, monkeypatch) -> None:
        monkeypatch.setenv("MCP_SUMMARY", "on")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert SummarizeDescriptionsMiddleware.from_env() is None

    async def test_off_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv("MCP_SUMMARY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        assert SummarizeDescriptionsMiddleware.from_env() is None


class TestDoesNotRepeatWork:
    async def test_a_cached_description_is_never_re_asked(self) -> None:
        model = FakeModel()
        mw = summarizer(model)
        server = build(mw)

        async with Client(server) as c:
            await c.list_tools()
            await settle(mw)
            await c.list_tools()
            await c.list_tools()

        assert len(model.calls) == 1

    async def test_concurrent_lists_start_one_job(self) -> None:
        # Three clients listing at once must not start three identical jobs.
        model = FakeModel()
        model.gate = asyncio.Event()
        mw = summarizer(model)
        server = build(mw)

        async with Client(server) as c:
            await asyncio.gather(c.list_tools(), c.list_tools(), c.list_tools())

        model.gate.set()
        await settle(mw)
        assert len(model.calls) == 1

    async def test_an_edited_description_invalidates_only_itself(self) -> None:
        # The key is sha256(name + description), so an upstream editing one
        # tool's text cannot cost anything for the others.
        a = content_key("t", "one")
        b = content_key("t", "two")
        c = content_key("other", "one")
        assert a != b and a != c

    async def test_the_cache_survives_a_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "summaries.json"
        first = SummarizeDescriptionsMiddleware(api_key="k", cache_path=path)
        first._ask = FakeModel({"scrape": "Fetch a page.", "ping": "Ping."})  # type: ignore[method-assign]

        async with Client(build(first)) as c:
            await c.list_tools()
            await settle(first)

        # A new process, same disk. It must not call the model at all.
        model = FakeModel()
        second = SummarizeDescriptionsMiddleware(api_key="k", cache_path=path)
        second._ask = model  # type: ignore[method-assign]

        async with Client(build(second)) as c:
            tools = {t.name: t for t in await c.list_tools()}

        assert tools["scrape"].description == "Fetch a page."
        assert model.calls == []

    async def test_an_unreadable_cache_is_an_empty_cache(self, tmp_path: Path) -> None:
        path = tmp_path / "corrupt.json"
        path.write_text("{not json", "utf-8")
        cache = SummaryCache(path)
        assert cache.get("anything") is None


class TestWithStubSchemas:
    async def test_the_signature_wraps_the_summary(self) -> None:
        # Both middlewares on, as the remote gateway runs them. The summarizer
        # must see the raw description and the stub middleware must see the
        # summary — which depends on registration order, since FastMCP runs the
        # last-registered middleware first on the way out.
        mw = summarizer(FakeModel({"scrape": "Fetch a page and return its text.", "ping": "Ping."}))
        server = build(mw, stub=True)

        async with Client(server) as c:
            await c.list_tools()
            await settle(mw)
            tools = {t.name: t for t in await c.list_tools()}

        assert tools["scrape"].description == (
            "scrape(url, optional: formats) — Fetch a page and return its text."
        )
        assert tools["scrape"].input_schema == {"type": "object"}

    async def test_the_summarizer_sees_the_untrimmed_description(self) -> None:
        # The ordering bug this guards: if the stub middleware ran first, the
        # summarizer would be handed REGEX_GIVES and would cache a
        # summary of a fragment, permanently.
        model = FakeModel()
        mw = summarizer(model)
        server = build(mw, stub=True)

        async with Client(server) as c:
            await c.list_tools()
            await settle(mw)

        asked = dict(model.calls[0])
        assert asked["scrape"] == AWKWARD


class TestParsing:
    def test_bare_json(self) -> None:
        assert _parse_lines('{"a": "one"}') == {"a": "one"}

    def test_a_fenced_block(self) -> None:
        assert _parse_lines('```json\n{"a": "one"}\n```') == {"a": "one"}

    def test_preamble_before_the_object(self) -> None:
        assert _parse_lines('Here you go:\n{"a": "one"}') == {"a": "one"}

    def test_garbage_yields_nothing_rather_than_raising(self) -> None:
        assert _parse_lines("I would rather not") == {}
        assert _parse_lines("") == {}

    def test_non_string_values_are_dropped(self) -> None:
        assert _parse_lines('{"a": "one", "b": 2, "c": null}') == {"a": "one"}


class TestRequestShape:
    """What goes over the wire to /v1/messages, and how the reply is read."""

    async def _ask_with(self, monkeypatch, model: str, content: list[dict]) -> tuple[dict, dict]:
        sent: dict = {}

        class Resp:
            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict:
                return {"content": content}

        async def post(self, url, **kw):
            sent.update(kw["json"])
            return Resp()

        monkeypatch.setattr("httpx.AsyncClient.post", post)
        mw = SummarizeDescriptionsMiddleware(model=model, api_key="k")
        return sent, await mw._ask([("scrape", AWKWARD)])

    async def test_haiku_turns_thinking_off_and_sends_no_sampling(self, monkeypatch) -> None:
        sent, _ = await self._ask_with(monkeypatch, "claude-haiku-5-5", [])
        assert sent["thinking"] == {"type": "disabled"}
        assert not {"temperature", "top_p", "top_k"} & sent.keys()

    async def test_sonnet_5_5_gets_between_tools_not_disabled(self, monkeypatch) -> None:
        # Sonnet 5.5 answers `disabled` with a 400.
        sent, _ = await self._ask_with(monkeypatch, "claude-sonnet-5-5", [])
        assert sent["thinking"] == {"type": "between_tools"}

    async def test_a_leading_thinking_block_is_skipped(self, monkeypatch) -> None:
        content = [
            {"type": "thinking", "thinking": "", "signature": "x"},
            {"type": "text", "text": '{"scrape": '},
            {"type": "text", "text": '"Fetch a page."}'},
        ]
        _, lines = await self._ask_with(monkeypatch, "claude-haiku-5-5", content)
        assert lines == {"scrape": "Fetch a page."}

    def test_thinking_off_is_disabled_for_haiku_4_5_too(self) -> None:
        assert thinking_off("claude-haiku-4-5") == {"type": "disabled"}
