"""Stub schemas: advertise names, validate calls, answer with the signature.

These run a real FastMCP server with the middleware attached and talk to it
through an in-memory client, so what is asserted is what a consumer would
actually receive over ``tools/list`` and ``tools/call``.
"""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP

from mcp_gateway.middleware.stub_schemas import (
    StubSchemaMiddleware,
    first_sentence,
    signature_of,
)


def build(**kwargs) -> FastMCP:
    """A server with two tools and the middleware attached."""
    server = FastMCP("test")

    @server.tool
    def send_keys(session: str, keys: str, enter: bool = True) -> str:
        """Send keystrokes to a tmux session. Use sparingly; prefer a tool."""
        return f"sent {keys} to {session} (enter={enter})"

    @server.tool
    def ping() -> str:
        """Liveness check."""
        return "pong"

    server.add_middleware(StubSchemaMiddleware(**kwargs))
    return server


class TestAdvertisement:
    async def test_schema_is_replaced_by_an_empty_object(self) -> None:
        async with Client(build()) as c:
            tools = {t.name: t for t in await c.list_tools()}

        # The whole point: no properties, no required, nothing to read.
        assert tools["send_keys"].inputSchema == {"type": "object"}

    async def test_description_is_trimmed_to_its_first_sentence(self) -> None:
        async with Client(build()) as c:
            tools = {t.name: t for t in await c.list_tools()}

        assert tools["send_keys"].description == "Send keystrokes to a tmux session."

    async def test_full_descriptions_are_available(self) -> None:
        async with Client(build(describe="full")) as c:
            tools = {t.name: t for t in await c.list_tools()}

        assert "prefer a tool" in tools["send_keys"].description
        # Schemas are still stubbed; the two settings are independent.
        assert tools["send_keys"].inputSchema == {"type": "object"}

    async def test_always_full_keeps_its_real_schema(self) -> None:
        # For tools where a schema-valid guess is expensive, ~100 tokens to
        # remove the guesswork is the right trade.
        async with Client(build(always_full=frozenset({"send_keys"}))) as c:
            tools = {t.name: t for t in await c.list_tools()}

        assert "session" in tools["send_keys"].inputSchema["properties"]
        assert tools["ping"].inputSchema.get("properties") in (None, {})


class TestCorrectCallsAreUntouched:
    async def test_a_right_guess_just_runs(self) -> None:
        async with Client(build()) as c:
            r = await c.call_tool("send_keys", {"session": "s", "keys": "ls"})

        assert "sent ls to s" in r.content[0].text

    async def test_omitting_an_optional_is_fine(self) -> None:
        async with Client(build()) as c:
            r = await c.call_tool("send_keys", {"session": "s", "keys": "ls"})

        assert "enter=True" in r.content[0].text

    async def test_a_tool_with_no_arguments_still_works(self) -> None:
        async with Client(build()) as c:
            r = await c.call_tool("ping", {})

        assert r.content[0].text == "pong"


class TestWrongGuessesGetTheSignature:
    async def test_missing_required_argument(self) -> None:
        async with Client(build()) as c:
            with pytest.raises(Exception) as excinfo:
                await c.call_tool("send_keys", {"session": "s"})

        msg = str(excinfo.value)
        assert "keys" in msg
        assert "send_keys(session, keys, optional: enter)" in msg

    async def test_wrong_argument_name(self) -> None:
        # The upstream would have said only "keys is required", never that
        # `target` was the mistake. Both halves matter to the retry.
        async with Client(build()) as c:
            with pytest.raises(Exception) as excinfo:
                await c.call_tool("send_keys", {"target": "s", "keys": "ls"})

        msg = str(excinfo.value)
        assert "target" in msg
        assert "send_keys(session, keys, optional: enter)" in msg

    async def test_extra_unknown_argument_is_rejected(self) -> None:
        # The case that motivated validating at the gateway at all: a real
        # upstream given an extra key ignored it and ran the call, so a model
        # that half-guessed got a success and no correction.
        async with Client(build()) as c:
            with pytest.raises(Exception) as excinfo:
                await c.call_tool(
                    "send_keys", {"session": "s", "keys": "ls", "bogus": 1}
                )

        assert "bogus" in str(excinfo.value)

    async def test_wrong_type(self) -> None:
        async with Client(build()) as c:
            with pytest.raises(Exception) as excinfo:
                await c.call_tool(
                    "send_keys", {"session": "s", "keys": "ls", "enter": "yes"}
                )

        msg = str(excinfo.value)
        assert "enter" in msg
        assert "signature" in msg.lower()

    async def test_the_call_never_reaches_the_tool(self) -> None:
        ran: list[str] = []
        server = FastMCP("test")

        @server.tool
        def touchy(who: str) -> str:
            """Does something you cannot undo."""
            ran.append(who)
            return "done"

        server.add_middleware(StubSchemaMiddleware())

        async with Client(server) as c:
            await c.list_tools()  # populate the schema cache
            with pytest.raises(Exception):
                await c.call_tool("touchy", {"whom": "x"})

        assert ran == [], "a rejected call must not execute"


class TestConfiguration:
    def test_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MCP_SCHEMA_MODE", raising=False)
        assert StubSchemaMiddleware.from_env() is None

    def test_full_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SCHEMA_MODE", "full")
        assert StubSchemaMiddleware.from_env() is None

    def test_stub_turns_it_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SCHEMA_MODE", "stub")
        mw = StubSchemaMiddleware.from_env()
        assert mw is not None
        assert mw.describe == "first-sentence"

    def test_always_full_is_a_comma_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_SCHEMA_MODE", "stub")
        monkeypatch.setenv("MCP_SCHEMA_ALWAYS_FULL", "send-keys, imessage_send")
        mw = StubSchemaMiddleware.from_env()
        assert mw is not None
        assert mw.always_full == frozenset({"send-keys", "imessage_send"})


class TestSignature:
    def test_required_then_optional(self) -> None:
        schema = {
            "type": "object",
            "properties": {"a": {}, "b": {}, "c": {}},
            "required": ["a", "b"],
        }
        assert signature_of("t", schema) == "t(a, b, optional: c)"

    def test_all_required(self) -> None:
        schema = {"type": "object", "properties": {"a": {}}, "required": ["a"]}
        assert signature_of("t", schema) == "t(a)"

    def test_all_optional(self) -> None:
        schema = {"type": "object", "properties": {"a": {}, "b": {}}}
        assert signature_of("t", schema) == "t(optional: a, b)"

    def test_no_arguments(self) -> None:
        assert signature_of("t", {"type": "object"}) == "t()"
        assert signature_of("t", None) == "t()"


class TestFirstSentence:
    def test_stops_at_the_first_period(self) -> None:
        assert first_sentence("One. Two. Three.") == "One."

    def test_a_single_sentence_survives_whole(self) -> None:
        assert first_sentence("Just the one.") == "Just the one."

    def test_no_period_at_all(self) -> None:
        assert first_sentence("No period here") == "No period here"

    def test_empty(self) -> None:
        assert first_sentence("") == ""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Fetch a URL, e.g. https://x.com, and parse it. More.",
             "Fetch a URL, e.g. https://x.com, and parse it."),
            ("Scale by 0.5 and return. More.", "Scale by 0.5 and return."),
            ("Compare A vs. B and report. More.", "Compare A vs. B and report."),
        ],
    )
    def test_does_not_stop_at_an_abbreviation(
        self, text: str, expected: str
    ) -> None:
        # A description truncated at "e.g." is worse than no trimming at all.
        assert first_sentence(text) == expected
