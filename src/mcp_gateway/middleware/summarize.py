"""Model-written one-line tool descriptions, memoized on content.

Why
---
Every consumer that economizes on tool advertisements ends up showing the
model one line per tool, and every one of them gets that line the same way:
take the first sentence of whatever the upstream author wrote. That is two
mistakes at once.

**The regex is wrong often enough to matter.** A sentence does not end at the
first ``.``, and the exceptions are not exotic — ``chrome_press_key`` advertised
itself as *"Send a keypress (e.g."*, four fifths of the sentence gone and the
remainder useless. The obvious fix, refusing to break after a capital letter,
repairs that case and breaks the ones ending in an acronym (*"Extract content
from a single URL."*, *"Send a LinkedIn DM."*). Measured across the 109 tools
on this gateway, each guard fixes two descriptions and breaks two others.
There is no regex that is simply right, which is the standing argument for
asking a model.

**A first sentence is not a summary.** It was written to open a paragraph, not
to be read alone. ``cmos_refs`` opens with "Walk CMOS's 6,498 internal
cross-references." — accurate, and it does not say that you give it a paragraph
number. A line written *to be* the advertisement can.

Where this belongs
------------------
In the gateway rather than in each consumer, because the work is
**per-description, not per-consumer**: one call per unique string, memoized,
and every client downstream gets the result. A summarizer in harness would run
over harness's view, one in llm-multiplex over llm-multiplex's, each with its
own cache, both missing the third-party clients entirely.

When
----
Lazily, never at startup and never at config-generation time.

*Not at config time* because only 30 of the 109 tools here have descriptions in
git; the other 79 arrive from proxied upstreams at runtime and change when an
upstream updates. Baking would cover a quarter of the surface and go stale
silently.

*Not at startup* because that is 109 blocking model calls against a gateway
that deliberately does not spawn a subprocess until the first tool call.

So: the first ``tools/list`` after a cold start is served from the regex, a
background task fills the cache, and every later list is served from it. The
key is ``sha256(name + description)``, which makes an upstream edit invalidate
exactly one entry and means nothing ever needs explicit invalidation.

Failure is always the regex
---------------------------
No API key, no network, a slow model, a malformed answer, a summary longer than
the cap: every one of them falls back to ``first_sentence``, which is what
ships today. This middleware can only improve a line or leave it alone, and a
gateway with no key configured behaves exactly as it does now.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Sequence

import httpx
import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool

from .stub_schemas import first_sentence, signature_of

log = logging.getLogger(__name__)

# Anthropic's REST API directly, over the httpx this package already depends
# on. The official SDK would be a second HTTP client and a versioned surface to
# track, for one POST.
_API = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"

_PROMPT = """\
You are writing one-line catalog entries for tools in an AI agent's toolbox.
The agent sees only your line when deciding whether a tool is the one it wants.

For each tool below write ONE line, under {cap} characters:

- Start with a verb. No "This tool...", no restating the name.
- Say what it ACTS ON and what comes BACK, concretely. "Search X and return Y"
  beats "Interface for searching".
- Keep the detail that distinguishes it from a neighbouring tool: which
  account, which format, which of several similar things it touches.
- Keep proper nouns, product names and units verbatim.
- Drop setup notes, caveats, examples and anything about error handling.

Reply with JSON only: an object mapping each tool name to its line. No prose,
no code fence.

Tools:
{tools}
"""


def content_key(name: str, description: str) -> str:
    """Cache key for one tool's description.

    Includes the name because the name is part of what the summary is written
    about — two tools sharing a description should not share a line.
    """
    h = hashlib.sha256()
    h.update(name.encode("utf-8"))
    h.update(b"\x00")
    h.update(description.encode("utf-8"))
    return h.hexdigest()[:32]


class SummaryCache:
    """A content-addressed dict that survives a restart.

    Deliberately not ``py-key-value-aio`` (declared here, but for FastMCP's
    OAuth storage): this is a flat map of short strings read on every
    ``tools/list``, and a JSON file loaded once into a dict is both faster and
    easier to inspect when a line looks wrong.

    Every operation is best-effort. A cache that cannot be read is an empty
    cache; one that cannot be written is a slower gateway, not a broken one.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._data: dict[str, str] = {}
        self._dirty = False
        if path is not None and path.exists():
            try:
                self._data = json.loads(path.read_text("utf-8"))
                log.info("mcp: loaded %d tool summaries from %s", len(self._data), path)
            except Exception as exc:  # pragma: no cover - corrupt cache
                log.warning("mcp: ignoring unreadable summary cache %s: %s", path, exc)

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def put(self, key: str, line: str) -> None:
        self._data[key] = line
        self._dirty = True

    def flush(self) -> None:
        """Write through a temp file, so a crash cannot leave half a cache."""
        if not self._dirty or self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=0, sort_keys=True), "utf-8")
            tmp.replace(self.path)
            self._dirty = False
        except Exception as exc:  # pragma: no cover - read-only fs
            log.warning("mcp: could not write summary cache %s: %s", self.path, exc)


class SummarizeDescriptionsMiddleware(Middleware):
    """Replace advertised descriptions with model-written one-liners.

    Independent of :class:`StubSchemaMiddleware` on purpose, because the two
    deployments want different halves:

    - the **local** gateway serves harness, which keeps full schemas and writes
      its own catalog — it wants better prose and nothing else;
    - the **remote** gateway serves clients with no prompt of their own, which
      want the prose *and* the schema stripped.

    Installing both is supported and idempotent: a summary is already one
    sentence, so the stub middleware's ``first-sentence`` trim is a no-op over
    it.

    Parameters
    ----------
    model:
        Anthropic model id. Haiku is the intended one; this is a short, highly
        constrained rewrite and the cheapest model does it well. The request
        sends no ``temperature``/``top_p``/``thinking`` on purpose: Haiku 5.5
        and Sonnet 5.x reject sampling parameters other than the default, and
        with ``thinking`` omitted Haiku 5.5 answers in plain text.
    api_key:
        ``None`` disables summarization entirely — every line falls back to the
        regex, which is today's behaviour.
    cache_path:
        Where to persist. ``None`` keeps the cache in memory only, which is
        correct for tests and merely wasteful in production.
    max_chars:
        Lines longer than this are rejected and fall back. The model is asked
        for this limit; enforcing it means a run-on answer cannot bloat a
        prompt it was supposed to shrink.
    batch:
        Tools per request. One call for all of them is cheapest but risks a
        truncated reply; 25 keeps each response small enough to be reliable.
    """

    def __init__(
        self,
        *,
        model: str = "claude-haiku-5-5",
        api_key: str | None = None,
        cache_path: Path | None = None,
        max_chars: int = 180,
        batch: int = 25,
        timeout: float = 30.0,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.max_chars = max_chars
        self.batch = batch
        self.timeout = timeout
        self.cache = SummaryCache(cache_path)
        # Keys being summarized right now. Without this, a client that lists
        # tools three times in quick succession starts three identical jobs.
        self._inflight: set[str] = set()
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def from_env(cls) -> "SummarizeDescriptionsMiddleware | None":
        """Build from env, or ``None`` if disabled.

        Env (all optional):
          ``MCP_SUMMARY``        on/off (default "off")
          ``MCP_SUMMARY_MODEL``  model id (default claude-haiku-5-5)
          ``MCP_SUMMARY_CACHE``  path (default ~/.cache/mcp-gateway/summaries.json)
          ``MCP_SUMMARY_MAX``    max chars per line (default 180)
          ``ANTHROPIC_API_KEY``  required; without it this returns None
        """
        if os.environ.get("MCP_SUMMARY", "off").lower() not in ("1", "on", "true", "yes"):
            return None
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            log.warning("mcp: MCP_SUMMARY is on but ANTHROPIC_API_KEY is unset; not summarizing")
            return None
        raw_cache = os.environ.get("MCP_SUMMARY_CACHE")
        cache_path = (
            Path(raw_cache).expanduser()
            if raw_cache
            else Path.home() / ".cache" / "mcp-gateway" / "summaries.json"
        )
        return cls(
            model=os.environ.get("MCP_SUMMARY_MODEL", "claude-haiku-5-5"),
            api_key=key,
            cache_path=cache_path,
            max_chars=int(os.environ.get("MCP_SUMMARY_MAX", "180")),
        )

    def line_for(self, name: str, description: str) -> str:
        """The best line available *right now*, without waiting for anything."""
        if not description:
            return ""
        cached = self.cache.get(content_key(name, description))
        return cached if cached else first_sentence(description)

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)

        missing: list[tuple[str, str]] = []
        out: list[Tool] = []
        for tool in tools:
            desc = tool.description or ""
            if not desc:
                out.append(tool)
                continue
            key = content_key(tool.name, desc)
            cached = self.cache.get(key)
            if cached is None and key not in self._inflight:
                missing.append((tool.name, desc))
            out.append(tool.model_copy(update={"description": cached or first_sentence(desc)}))

        # Fill in the background. The response above has already been built, so
        # this cannot delay it however long the model takes.
        if missing:
            self._schedule(missing)
        return out

    def _schedule(self, missing: list[tuple[str, str]]) -> None:
        if self._task is not None and not self._task.done():
            return  # one filler at a time; the next list picks up the remainder
        for name, desc in missing:
            self._inflight.add(content_key(name, desc))
        self._task = asyncio.create_task(self._fill(missing))
        # Without a done-callback an exception here is swallowed and reported
        # only when the task is garbage collected, which is nowhere useful.
        self._task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._inflight.clear()
        exc = task.exception() if not task.cancelled() else None
        if exc is not None:
            log.warning("mcp: tool summarization failed: %s", exc)

    async def _fill(self, missing: list[tuple[str, str]]) -> None:
        for i in range(0, len(missing), self.batch):
            chunk = missing[i : i + self.batch]
            try:
                lines = await self._ask(chunk)
            except Exception as exc:
                log.warning("mcp: summarizer batch failed, keeping regex lines: %s", exc)
                return
            for name, desc in chunk:
                line = lines.get(name, "").strip()
                # Reject rather than repair. A too-long or empty answer means
                # the model did something unexpected, and today's regex line is
                # a known quantity.
                if line and len(line) <= self.max_chars:
                    self.cache.put(content_key(name, desc), line)
            self.cache.flush()
        log.info("mcp: summarized %d tool descriptions", len(missing))

    async def _ask(self, chunk: list[tuple[str, str]]) -> dict[str, str]:
        listing = "\n\n".join(f"### {name}\n{desc}" for name, desc in chunk)
        prompt = _PROMPT.format(cap=self.max_chars, tools=listing)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                _API,
                headers={
                    "x-api-key": self.api_key or "",
                    "anthropic-version": _API_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 64 * len(chunk) + 256,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            body = resp.json()
        # Join the text blocks, never content[0]: a Sonnet 5.x override can
        # lead with a (sometimes empty) thinking block.
        text = "".join(
            b.get("text", "")
            for b in body.get("content", [])
            if isinstance(b, dict) and b.get("type") == "text"
        )
        return _parse_lines(text)


def _parse_lines(text: str) -> dict[str, str]:
    """The model's JSON, however it chose to wrap it.

    Asked for bare JSON and usually obliged, but a fenced block or a line of
    preamble is common enough that insisting would throw away good answers.
    """
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start = text.find("{")
        if start == -1:
            return {}
        text = text[start:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def described(name: str, schema: dict[str, Any] | None, line: str) -> str:
    """``name(args) — what it does``, for consumers that lost the schema.

    Only for stub mode. A consumer that still has the schema computes the
    signature itself, and prefixing it here would show it twice.
    """
    sig = signature_of(name, schema)
    return f"{sig} — {line}" if line else sig
