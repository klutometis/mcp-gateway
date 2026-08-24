"""Multi-instance MCP server wrapper.

A `MultiInstanceProxy` holds N backing FastMCP proxies for the same logical
server (e.g. one gmail proxy per account, or one shim URL per host) and
exposes them as a single flat tool surface to the gateway. Tool calls get
routed to the right backing proxy via an injected `instance` parameter
(name configurable per server).

See ``plans/multi-instance-backends.md`` for full design rationale,
including:
- Why this lives in the gateway (transport-agnostic aggregator)
  rather than in shim or in a new sidecar.
- Why we explicitly do NOT track liveness (push-based liveness has
  been brittle; we fail at call time and enrich errors instead).
- Why enum-in-schema is the default (bare-schema documented as
  fallback if context bloat ever bites).

Key behaviors:
- ``list_tools()``: returns each backing tool with the instance param
  prepended to ``parameters.properties``. The enum already advertises
  the configured instances, so no synthetic discovery tool is needed
  in this configuration.
- ``call_tool()``: pops the instance param from arguments, looks up
  the corresponding backing proxy, delegates. Missing and unknown
  values raise ToolError naming the configured set, so the model can
  self-correct in one round-trip. A call that names a real instance
  and then fails inside it reports the underlying error alone — the
  set is not the fix there, and saying it anyway reads as advice to
  retry elsewhere.
- Failed calls are triaged before they are reported: if the backing's
  URL refuses a TCP connection, the error says the backend is
  unreachable instead of parroting FastMCP's ``NotFoundError: Unknown
  tool``. An unreachable upstream and a misspelled tool name are
  indistinguishable at the proxy layer, and conflating them cost a
  debugging session once (chrome tunnel down, error read as a
  tool-registration bug).
- Single-instance edge case: if exactly one instance is configured and
  the agent omits the param, use it (single-instance setups feel
  identical to today).

Not included (intentionally): a synthetic ``<param>_list`` discovery
tool. With enum-in-schema the model already sees the valid set on
every ``tools/list``; a separate discovery tool conveys nothing new
and pollutes the tool surface. If we ever switch to the bare-schema
fallback (see ``plans/multi-instance-backends.md``), bring it back
at the same time — then it becomes load-bearing as the primary
proactive-enumeration mechanism.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import suppress
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.function_tool import FunctionTool
from fastmcp.tools.tool import Tool, ToolResult

log = logging.getLogger(__name__)

# Seconds to wait for a backing's TCP port to accept a connection when
# diagnosing a failed call. Only ever spent on a call that already failed,
# and these backings are localhost or a localhost-forwarded port, so a
# generous-feeling timeout is still cheap.
_PROBE_TIMEOUT = 2.0


# ---------------------------------------------------------------------------
# Schema mutation
# ---------------------------------------------------------------------------


def inject_instance_param(
    schema: dict[str, Any],
    param_name: str,
    instance_names: Sequence[str],
) -> dict[str, Any]:
    """Return a copy of ``schema`` with ``param_name`` prepended to
    ``properties`` and (when >1 instance) to ``required``.

    Order matters for model readability — the disambiguator should
    appear before the tool's own args. JSON Schema's ``properties`` is
    technically unordered, but the serialized JSON we send on the wire
    preserves insertion order, which the model reads top-to-bottom.

    When there's only one instance, the param is added but NOT marked
    required, so single-instance setups feel identical to today.
    """
    new = deepcopy(schema) if schema else {"type": "object", "properties": {}}
    new.setdefault("type", "object")
    new.setdefault("properties", {})

    param_schema: dict[str, Any] = {
        "type": "string",
        "description": (
            f"Which instance to target. Configured: "
            f"{', '.join(instance_names)}."
        ),
    }
    if len(instance_names) > 1:
        param_schema["enum"] = list(instance_names)

    # Prepend param to properties (insertion-ordered dict).
    new_props = {param_name: param_schema}
    for k, v in new["properties"].items():
        if k == param_name:
            # Collision — let the load-time check in gateway.py catch
            # this; we just shouldn't silently clobber.
            continue
        new_props[k] = v
    new["properties"] = new_props

    # Prepend to required when there are multiple instances.
    if len(instance_names) > 1:
        existing_required = [r for r in new.get("required", []) if r != param_name]
        new["required"] = [param_name] + existing_required

    return new


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


def _list_str(names: Sequence[str]) -> str:
    return ", ".join(names) if names else "<none>"


def _missing_param_error(tool_name: str, param_name: str, instances: Sequence[str]) -> ToolError:
    return ToolError(
        f"Tool {tool_name!r} requires {param_name!r}. "
        f"Configured {param_name}s: {_list_str(instances)}. "
        f"Retry with {param_name}=<name>."
    )


def _unknown_value_error(
    tool_name: str, param_name: str, value: Any, instances: Sequence[str]
) -> ToolError:
    return ToolError(
        f"Unknown {param_name}={value!r} for {tool_name!r}. "
        f"Configured {param_name}s: {_list_str(instances)}. "
        f"Retry with one of those."
    )


def _backend_failed_error(
    tool_name: str,
    param_name: str,
    instance: str,
    underlying: BaseException,
) -> ToolError:
    """The instance was named correctly and the backend failed on its own terms.

    Silent about the other configured instances, for the same reason
    ``_backend_unreachable_error`` below already is: the parameter was right,
    so the set is not the fix. That sibling reached this conclusion first, for
    the transport-down case; this is the general case catching up.

    It used to append "Other configured {param_name}s: ..." to every backend
    exception, whatever the cause. Two real examples, both on a correct
    profile:

        cdp error -32000: Not allowed.   Other configured profiles: work.
        context deadline exceeded.       Other configured profiles: work.

    One is Chrome refusing to capture a surface; the other is a timeout.
    Neither has anything to do with profiles. But the sentence arrives exactly
    where a remedy belongs and is the only actionable-looking noun in it, so it
    reads as one — an agent took it for a suggestion and retried on ``work``,
    where that tab id did not exist, turning one clear failure into two
    confusing ones. A human reader called it a tic.

    The set is already on the model's side of the wire three times over: in the
    tool description, in the injected param's ``description``, and in its
    ``enum`` (see ``inject_instance_param``). A fourth copy, appended to
    unrelated failures, only competes with the real error.
    """
    return ToolError(
        f"Call to {tool_name!r} on {param_name}={instance!r} failed: "
        f"{type(underlying).__name__}: {underlying}"
    )


def _backend_unreachable_error(
    tool_name: str,
    param_name: str,
    instance: str,
    url: str,
    reason: str,
) -> ToolError:
    """Error for "the backing process isn't answering", as distinct from
    "the tool doesn't exist".

    Deliberately says nothing about other instances: when the transport
    is down, the configured-set boilerplate is noise at best and a
    false lead at worst (see ``_diagnose_unreachable``).
    """
    return ToolError(
        f"Backend for {param_name}={instance!r} is unreachable at {url} "
        f"({reason}), so {tool_name!r} could not be called. The tool exists "
        f"and the {param_name} is spelled correctly; the process serving it "
        f"is down or its tunnel is dead. This is an infrastructure failure, "
        f"not a bad argument — retrying with a different {param_name} will "
        f"not help."
    )


async def _probe_reachable(url: str, timeout: float = _PROBE_TIMEOUT) -> str | None:
    """Return None if ``url``'s host:port accepts a TCP connection, else why not.

    A bare TCP connect, not an HTTP request: it is protocol-agnostic
    (every backing speaks *something* over TCP), it cannot be fooled by
    an app-level 404, and it costs a round-trip on localhost. That is
    all we need to separate "nothing is listening" from "something is
    listening and gave a real answer".
    """
    parsed = urlsplit(url)
    host = parsed.hostname
    if not host:
        return None  # Not a URL we can probe; don't guess.
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        fut = asyncio.open_connection(host, port)
        _reader, writer = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        return f"no response from {host}:{port} within {timeout:g}s"
    except OSError as e:
        # ConnectionRefusedError is the interesting one: port closed.
        return f"cannot connect to {host}:{port}: {e.strerror or e}"
    writer.close()
    with suppress(Exception):
        await writer.wait_closed()
    return None


async def _diagnose_unreachable(url: str | None) -> str | None:
    """Reason string if ``url`` looks dead, else None.

    Why this exists: FastMCP's proxy provider catches connection
    failures inside ``get_tool()``, logs a warning, and returns None —
    so the caller raises ``NotFoundError: Unknown tool: 'x'``. A dead
    TCP tunnel therefore presents as a nonexistent tool, and the
    reader goes hunting through config for a tool-registration bug
    that does not exist. One connect() tells the two apart.
    """
    if not url:
        return None
    try:
        return await _probe_reachable(url)
    except Exception as e:  # A probe must never mask the original error.
        log.debug("reachability probe for %s raised %s: %s", url, type(e).__name__, e)
        return None


# ---------------------------------------------------------------------------
# The wrapper
# ---------------------------------------------------------------------------


class MultiInstanceProxy(FastMCP):
    """A FastMCP server that fans out to N backing FastMCP proxies.

    All backings are assumed to expose the same tool surface (they're
    different instances of the same logical server, e.g. gmail-personal
    and gmail-work running the same MCP binary with different env).
    The wrapper presents that surface once, with one extra parameter
    (the instance selector) added to every tool.

    The wrapper also synthesizes one extra tool, ``<param_name>_list``,
    that returns the configured instance names. Cheap, useful for
    proactive enumeration; not load-bearing thanks to error enrichment.

    Parameters
    ----------
    name
        Server name (e.g. "gmail-multi"). Shown in logs.
    instances
        Mapping of instance-name → backing FastMCP proxy. Iteration order
        is preserved (used for enum order in schemas).
    param_name
        The argument name to inject into every tool (e.g. "account",
        "host"). Defaults to "instance".
    """

    def __init__(
        self,
        name: str,
        instances: dict[str, FastMCP],
        param_name: str = "instance",
        instance_urls: dict[str, str] | None = None,
    ) -> None:
        if not instances:
            raise ValueError(
                f"MultiInstanceProxy {name!r}: at least one instance required"
            )
        super().__init__(name)
        self._instances: dict[str, FastMCP] = dict(instances)
        self._param_name = param_name
        # Per-instance upstream URL, for reachability diagnosis on failure.
        # HTTP-ish backings only; stdio instances simply have no entry and
        # fall back to the generic error.
        self._instance_urls: dict[str, str] = dict(instance_urls or {})
        # Lazy cache of routing tools, populated on first list_tools() or
        # get_tool() call. Keyed by tool name. We populate atomically under
        # a lock to avoid double-init from concurrent requests.
        self._routing_tools: dict[str, FunctionTool] | None = None
        self._populate_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Introspection helpers (also useful for tests)
    # ------------------------------------------------------------------

    @property
    def instance_names(self) -> list[str]:
        return list(self._instances.keys())

    @property
    def param_name(self) -> str:
        return self._param_name

    # ------------------------------------------------------------------
    # Populating the routing-tool registry (lazy, once)
    # ------------------------------------------------------------------

    async def _fetch_backing_tools(self) -> list[Tool]:
        """Fetch the tool surface from any one backing proxy.

        All backings are assumed to expose the same surface; we only
        need one. If the first one is unreachable we try the rest
        before giving up, so the gateway's tool list doesn't go empty
        just because (say) the laptop helper happens to be offline at
        boot.
        """
        last_exc: BaseException | None = None
        for inst_name, backing in self._instances.items():
            try:
                tools = await backing.list_tools()
                return list(tools)
            except Exception as e:
                log.warning(
                    "MultiInstanceProxy %r: list_tools failed on instance %r: "
                    "%s: %s; trying next instance.",
                    self.name, inst_name, type(e).__name__, e,
                )
                last_exc = e
        # All backings failed. Re-raise the last one — caller (FastMCP) will
        # surface this; preferable to silently returning [].
        assert last_exc is not None
        raise last_exc

    async def _ensure_routing_tools(self) -> dict[str, FunctionTool]:
        """Lazily build the routing-tool registry on first access.

        Each routing tool wraps one backing tool: same name, mutated
        schema (instance param prepended), handler that pops the
        instance arg and forwards to the right backing.
        """
        if self._routing_tools is not None:
            return self._routing_tools
        async with self._populate_lock:
            if self._routing_tools is not None:
                return self._routing_tools
            backing_tools = await self._fetch_backing_tools()
            registry: dict[str, FunctionTool] = {}
            for backing_tool in backing_tools:
                routing_tool = self._make_routing_tool(backing_tool)
                registry[routing_tool.name] = routing_tool
            if not registry:
                # Don't cache nothing. A backing that answers with zero
                # tools is usually one that isn't ready yet -- a forwarded
                # port whose far end is still coming up, say -- and caching
                # that makes an empty tool surface permanent for the life
                # of the process. (Chrome went missing for eleven hours
                # this way on 2026-08-12: central rebooted ahead of the
                # laptop's SSH RemoteForward, and no amount of refreshing
                # helped because the gateway kept re-serving its cached
                # empty registry.) Returning uncached costs one list_tools
                # per access while genuinely empty, and self-heals the
                # moment the backing has something to say.
                log.warning(
                    "MultiInstanceProxy %r: backing returned no tools; "
                    "not caching, will retry on next access.",
                    self.name,
                )
                return registry
            self._routing_tools = registry
            log.info(
                "MultiInstanceProxy %r: built routing registry (%d tools, "
                "%d instances: %s)",
                self.name, len(registry), len(self._instances),
                ", ".join(self._instances.keys()),
            )
            return registry

    def _make_routing_tool(self, backing_tool: Tool) -> FunctionTool:
        """Wrap a backing tool with the instance param + routing handler."""
        names = self.instance_names
        new_params = inject_instance_param(
            backing_tool.parameters or {"type": "object", "properties": {}},
            self._param_name,
            names,
        )
        original_name = backing_tool.name
        param_name = self._param_name
        instances = self._instances
        instance_urls = self._instance_urls
        self_name = self.name

        async def handler(**kwargs: Any) -> Any:
            instance = kwargs.pop(param_name, None)
            if instance is None:
                if len(instances) == 1:
                    instance = next(iter(instances))
                else:
                    raise _missing_param_error(
                        original_name, param_name, list(instances)
                    )
            if instance not in instances:
                raise _unknown_value_error(
                    original_name, param_name, instance, list(instances)
                )
            backing = instances[instance]
            try:
                return await backing.call_tool(original_name, kwargs)
            except Exception as e:
                # Before blaming the call, ask whether the backing is even
                # answering. FastMCP reports an unreachable upstream as
                # NotFoundError ("Unknown tool"), which reads as a config
                # error and sends readers into servers.json for an hour.
                reason = await _diagnose_unreachable(instance_urls.get(instance))
                if reason is not None:
                    log.warning(
                        "MultiInstanceProxy %r: %s=%r backing unreachable at "
                        "%s (%s); original error was %s: %s",
                        self_name, param_name, instance,
                        instance_urls.get(instance), reason,
                        type(e).__name__, e,
                    )
                    raise _backend_unreachable_error(
                        original_name,
                        param_name,
                        instance,
                        instance_urls[instance],
                        reason,
                    ) from e
                raise _backend_failed_error(
                    original_name, param_name, instance, e
                ) from e

        return FunctionTool(
            name=original_name,
            title=backing_tool.title,
            description=backing_tool.description,
            parameters=new_params,
            output_schema=None,
            fn=handler,
            return_type=Any,
            run_in_thread=False,
            tags=set(),
        )

    # ------------------------------------------------------------------
    # FastMCP overrides — list_tools and get_tool consult the routing
    # registry. call_tool inherits FastMCP's default, which dispatches
    # via get_tool → tool.fn (our routing handler).
    # ------------------------------------------------------------------

    async def _list_tools(self) -> Sequence[Tool]:  # type: ignore[override]
        """Internal list_tools used by FastMCP's transform pipeline.

        Returns the routing tools (list-tool first, then one per
        backing tool). FastMCP's outer list_tools applies any
        transforms before returning to the client.
        """
        registry = await self._ensure_routing_tools()
        return list(registry.values())

    async def _get_tool(  # type: ignore[override]
        self,
        name: str,
        version: Any = None,
    ) -> Tool | None:
        """Internal get_tool used by FastMCP's transform pipeline.

        FastMCP's mount provider calls this when dispatching tools/call.
        Returning our routing tool means the default dispatch path
        invokes our handler, which does the actual instance routing.
        """
        registry = await self._ensure_routing_tools()
        return registry.get(name)
