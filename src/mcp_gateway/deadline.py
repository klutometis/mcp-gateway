"""A deadline on each backend's list calls, serving its last good answer.

A gateway's ``tools/list`` is a fan-out. FastMCP's AggregateProvider gathers
``list_tools()`` from every mounted backend and answers when the slowest one
does, so one slow backend delays the whole list, and a client whose connect
timeout is shorter than that gets no tools at all. On plane Wi-Fi
(2026-10-18) imessage took 17.8s to list and whatsapp 12.9s; every local
backend took under half a second. The gateway's list took 22-24s, and
harness, which allows 15s, called the whole gateway dead and every session
lost all 112 tools to wait for 8.

:class:`DeadlineProvider` wraps one mounted backend and gives each of its
``list_*`` calls ``deadline`` seconds:

- **In time:** the answer, which is also remembered.
- **Late or failed:** the last answer this backend gave in this process, or
  nothing if it has never answered. The rest of the gateway is not held up.

A late call is not cancelled. It finishes in the background and refreshes
the remembered answer, so the next list has it. A list that arrives while a
call is already in flight waits on that call, not a second one, so a slow
link is not buried under retries.

``get_tool`` is bounded the same way. AggregateProvider resolves every tool
call by asking *every* backend whether it has that name, and a proxy whose
cache has expired answers by listing its upstream again. Without this, a call
to a local tool could wait out a slow remote backend's re-list. With a
remembered answer, a name this backend did not advertise is answered "not
mine" immediately.

The cost is staleness. A backend that changed its tools while it was slow is
advertised with its old ones until it answers in time once. Calls still go to
the live backend, so a stale definition fails at call time, which is what a
call to an unreachable backend would have done anyway. The memory is
per-process, not on disk: a gateway that starts with a backend unreachable
serves nothing for it until it answers once.

Backends that forward the caller's identity upstream (``forward_token``,
``forward_identity``) may give each caller a different list, so they are
bounded but never remembered: one caller's tools are never served to
another.

``MCP_LIST_DEADLINE`` sets the deadline in seconds (default 5); ``0`` or
``off`` turns all of this off and mounts backends exactly as before.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastmcp.server.providers.base import Provider

if TYPE_CHECKING:
    from fastmcp.prompts.base import Prompt
    from fastmcp.resources.base import Resource
    from fastmcp.resources.template import ResourceTemplate
    from fastmcp.tools.base import Tool
    from fastmcp.utilities.components import FastMCPComponent
    from fastmcp.utilities.versions import VersionSpec

logger = logging.getLogger(__name__)

DEFAULT_LIST_DEADLINE = 5.0


def list_deadline_from_env() -> float | None:
    """Seconds from ``MCP_LIST_DEADLINE``, or None when it is turned off."""
    raw = os.environ.get("MCP_LIST_DEADLINE", "").strip().lower()
    if not raw:
        return DEFAULT_LIST_DEADLINE
    if raw in ("0", "off", "no", "false"):
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "MCP_LIST_DEADLINE=%r is not a number; using %ss", raw, DEFAULT_LIST_DEADLINE
        )
        return DEFAULT_LIST_DEADLINE
    return value if value > 0 else None


class _Slot:
    """One kind of list for one backend: what it last said, what is in flight."""

    __slots__ = ("answer", "task")

    def __init__(self) -> None:
        self.answer: list[Any] | None = None
        self.task: asyncio.Task[list[Any]] | None = None


class DeadlineProvider(Provider):
    """Wrap one backend's provider; bound its list calls (module docstring)."""

    def __init__(
        self,
        inner: Provider,
        *,
        name: str,
        deadline: float,
        remember: bool = True,
    ) -> None:
        super().__init__()
        self._inner = inner
        self._name = name
        self._deadline = deadline
        self._remember = remember
        self._slots: dict[str, _Slot] = {}

    def __repr__(self) -> str:
        return f"DeadlineProvider({self._name!r}, {self._deadline:g}s, {self._inner!r})"

    # ------------------------------------------------------------------
    # The mechanism
    # ------------------------------------------------------------------

    def _slot(self, kind: str) -> _Slot:
        return self._slots.setdefault(kind, _Slot())

    def _start(self, kind: str, fetch: Callable[[], Awaitable[Sequence[Any]]]) -> asyncio.Task:
        """The in-flight call for ``kind``, starting one if there is none."""
        slot = self._slot(kind)
        if slot.task is not None and not slot.task.done():
            return slot.task

        async def run() -> list[Any]:
            started = time.monotonic()
            try:
                result = list(await fetch())
            except Exception as e:
                logger.warning(
                    "%s: %s failed after %.1fs: %s: %s",
                    self._name, kind, time.monotonic() - started, type(e).__name__, e,
                )
                raise
            elapsed = time.monotonic() - started
            # Not an empty answer: a backing that is briefly not ready
            # answers with nothing (tests/test_empty_registry.py), and
            # remembering that would make _get_tool turn its tools away
            # once it is back.
            if self._remember and result:
                slot.answer = result
            if elapsed > self._deadline:
                logger.warning(
                    "%s: late %s answer arrived after %.1fs (%d items)%s",
                    self._name, kind, elapsed, len(result),
                    "; remembered for the next list" if self._remember else "",
                )
            return result

        task = asyncio.ensure_future(run())
        # A late call that fails has nobody awaiting it. Retrieve its
        # exception so asyncio doesn't log "never retrieved"; run() has
        # already logged it.
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        slot.task = task
        return task

    async def _bounded(self, kind: str, fetch: Callable[[], Awaitable[Sequence[Any]]]) -> list[Any]:
        slot = self._slot(kind)
        task = self._start(kind, fetch)
        # asyncio.wait, not wait_for: on timeout the task keeps running, so
        # a slow backend's answer still lands and is remembered.
        done, _ = await asyncio.wait({task}, timeout=self._deadline)
        if done and not task.cancelled() and task.exception() is None:
            return task.result()
        fallback = slot.answer if self._remember else None
        logger.warning(
            "%s: %s %s; serving %s",
            self._name, kind,
            f"exceeded {self._deadline:g}s" if not done else "failed",
            f"its last answer ({len(fallback)} items)" if fallback is not None else "nothing",
        )
        return list(fallback) if fallback is not None else []

    async def warm(self) -> Sequence[Tool]:
        """List tools with no deadline, remembering the answer (boot warm-up)."""
        return await self._start("list_tools", self._inner.list_tools)

    def _advertised(self, kind: str) -> set[str] | None:
        """Names this backend last advertised, or None if unknown."""
        slot = self._slots.get(kind)
        if not self._remember or slot is None or slot.answer is None:
            return None
        return {getattr(c, "name", None) for c in slot.answer}

    # ------------------------------------------------------------------
    # Lists: bounded
    # ------------------------------------------------------------------

    async def _list_tools(self) -> Sequence[Tool]:
        return await self._bounded("list_tools", self._inner.list_tools)

    async def _list_resources(self) -> Sequence[Resource]:
        return await self._bounded("list_resources", self._inner.list_resources)

    async def _list_resource_templates(self) -> Sequence[ResourceTemplate]:
        return await self._bounded(
            "list_resource_templates", self._inner.list_resource_templates
        )

    async def _list_prompts(self) -> Sequence[Prompt]:
        return await self._bounded("list_prompts", self._inner.list_prompts)

    # ------------------------------------------------------------------
    # Lookups: answered from memory when the name isn't ours
    # ------------------------------------------------------------------

    async def _get_tool(self, name: str, version: VersionSpec | None = None) -> Tool | None:
        known = self._advertised("list_tools")
        if known is not None and name not in known:
            return None
        return await self._inner.get_tool(name, version)

    async def _get_prompt(self, name: str, version: VersionSpec | None = None) -> Prompt | None:
        known = self._advertised("list_prompts")
        if known is not None and name not in known:
            return None
        return await self._inner.get_prompt(name, version)

    # ------------------------------------------------------------------
    # Everything else: straight through
    # ------------------------------------------------------------------

    async def get_app_tool(self, app_name: str, tool_name: str) -> Tool | None:
        return await self._inner.get_app_tool(app_name, tool_name)

    async def get_tool_by_hash(self, tool_hash: str, tool_name: str) -> Tool | None:
        return await self._inner.get_tool_by_hash(tool_hash, tool_name)

    async def _get_resource(
        self, uri: str, version: VersionSpec | None = None
    ) -> Resource | None:
        return await self._inner.get_resource(uri, version)

    async def _get_resource_template(
        self, uri: str, version: VersionSpec | None = None
    ) -> ResourceTemplate | None:
        return await self._inner.get_resource_template(uri, version)

    async def get_tasks(self) -> Sequence[FastMCPComponent]:
        return await self._inner.get_tasks()

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        async with self._inner.lifespan():
            yield
