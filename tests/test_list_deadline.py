"""One slow backend must not hold the whole tools/list hostage.

On plane Wi-Fi (2026-10-18) imessage took 17.8s to list its tools and
whatsapp 12.9s, while every local backend answered in under half a second.
The gateway's tools/list waited for the slowest, took 22-24s, and harness
(15s connect timeout) declared the whole gateway dead: every session lost all
112 tools to wait for 8. See src/mcp_gateway/deadline.py.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.providers.fastmcp_provider import FastMCPProvider

from mcp_gateway.deadline import DeadlineProvider, list_deadline_from_env


class _Backend:
    """A backend whose tools/list can be made slow, failing, or different."""

    def __init__(self, name: str, tools: list[str]) -> None:
        self.server = FastMCP(name)
        self.delay = 0.0
        self.fail = False
        self.list_calls = 0
        self._names = list(tools)
        for t in tools:
            self.add(t)

    def add(self, tool_name: str) -> None:
        def fn() -> str:
            return f"{tool_name} ran"

        fn.__name__ = tool_name
        self.server.tool(fn, name=tool_name)

    def provider(self) -> FastMCPProvider:
        backend = self
        inner = FastMCPProvider(self.server)
        real_list = inner.list_tools

        async def list_tools():
            backend.list_calls += 1
            if backend.delay:
                await asyncio.sleep(backend.delay)
            if backend.fail:
                raise ConnectionError("upstream unreachable")
            return await real_list()

        inner.list_tools = list_tools  # type: ignore[method-assign]
        return inner


def _gateway(*backends: tuple[_Backend, bool], deadline: float = 0.2) -> FastMCP:
    gw = FastMCP("gateway")
    for backend, remember in backends:
        gw.add_provider(
            DeadlineProvider(
                backend.provider(),
                name=backend.server.name,
                deadline=deadline,
                remember=remember,
            )
        )
    return gw


async def _names(gw: FastMCP) -> tuple[set[str], float]:
    async with Client(gw) as c:
        t = time.monotonic()
        tools = await c.list_tools()
        return {t.name for t in tools}, time.monotonic() - t


async def test_slow_backend_does_not_hold_up_the_rest():
    fast = _Backend("local", ["chrome_list_pages", "define"])
    slow = _Backend("remote", ["imessage_search"])
    slow.delay = 1.0
    gw = _gateway((fast, True), (slow, True))

    names, elapsed = await _names(gw)

    assert names == {"chrome_list_pages", "define"}
    assert elapsed < 0.8, f"list waited for the slow backend ({elapsed:.2f}s)"


async def test_late_answer_is_remembered_and_served_next_time():
    fast = _Backend("local", ["define"])
    slow = _Backend("remote", ["imessage_search"])
    slow.delay = 0.5
    gw = _gateway((fast, True), (slow, True))
    bounded = gw.providers[-1]

    first, _ = await _names(gw)
    assert first == {"define"}
    await asyncio.sleep(0.6)  # the late call finishes in the background

    second, elapsed = await _names(gw)
    assert second == {"define", "imessage_search"}
    assert elapsed < 0.8
    assert isinstance(bounded, DeadlineProvider)


async def test_failing_backend_serves_its_last_answer():
    remote = _Backend("remote", ["whatsapp_send_message"])
    gw = _gateway((remote, True))

    assert (await _names(gw))[0] == {"whatsapp_send_message"}
    remote.fail = True
    assert (await _names(gw))[0] == {"whatsapp_send_message"}


async def test_never_answered_backend_serves_nothing_not_an_error():
    fast = _Backend("local", ["define"])
    dead = _Backend("remote", ["imessage_search"])
    dead.fail = True
    gw = _gateway((fast, True), (dead, True))

    assert (await _names(gw))[0] == {"define"}


async def test_one_call_in_flight_not_a_pile_of_retries():
    slow = _Backend("remote", ["imessage_search"])
    slow.delay = 0.6
    gw = _gateway((slow, True))

    for _ in range(3):
        await _names(gw)
    assert slow.list_calls == 1


async def test_identity_forwarding_backend_is_bounded_but_never_remembered():
    per_caller = _Backend("remote", ["drive_search"])
    gw = _gateway((per_caller, False))

    assert (await _names(gw))[0] == {"drive_search"}
    per_caller.fail = True
    assert (await _names(gw))[0] == set()


async def test_empty_answer_is_not_remembered():
    # tests/test_empty_registry.py: a backing that is briefly not ready
    # answers with nothing. Remembering that would make get_tool turn its
    # tools away once it is back.
    remote = _Backend("remote", [])
    gw = _gateway((remote, True))
    assert (await _names(gw))[0] == set()

    remote.add("late_tool")
    async with Client(gw) as c:
        result = await c.call_tool("late_tool", {})
    assert result.content[0].text == "late_tool ran"


async def test_calls_still_route_while_a_slow_backend_is_relisting():
    # AggregateProvider resolves a call by asking every backend for the name.
    # A slow backend that remembers its tools answers "not mine" at once.
    fast = _Backend("local", ["define"])
    slow = _Backend("remote", ["imessage_search"])
    gw = _gateway((fast, True), (slow, True))
    await _names(gw)  # both answer in time; both remembered

    slow.delay = 2.0
    async with Client(gw) as c:
        t = time.monotonic()
        result = await c.call_tool("define", {})
        elapsed = time.monotonic() - t
    assert result.content[0].text == "define ran"
    assert elapsed < 1.0, f"call to a local tool waited on the slow backend ({elapsed:.2f}s)"


async def test_warm_waits_out_a_slow_backend_and_remembers():
    slow = _Backend("remote", ["imessage_search"])
    slow.delay = 0.4
    gw = _gateway((slow, True))
    bounded = gw.providers[-1]
    assert isinstance(bounded, DeadlineProvider)

    warmed = await bounded.warm()
    assert [t.name for t in warmed] == ["imessage_search"]
    assert (await _names(gw))[0] == {"imessage_search"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", 5.0), ("2.5", 2.5), ("0", None), ("off", None), ("nonsense", 5.0)],
)
def test_deadline_from_env(monkeypatch, raw, expected):
    monkeypatch.setenv("MCP_LIST_DEADLINE", raw)
    assert list_deadline_from_env() == expected
