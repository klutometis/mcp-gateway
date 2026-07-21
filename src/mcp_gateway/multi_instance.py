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
  the corresponding backing proxy, delegates. Missing/unknown/failed
  cases raise ToolError with the configured set in the message so the
  model can self-correct in one round-trip.
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
from copy import deepcopy
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.function_tool import FunctionTool
from fastmcp.tools.tool import Tool, ToolResult

log = logging.getLogger(__name__)


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
    others: Sequence[str],
    underlying: BaseException,
) -> ToolError:
    others_msg = (
        f"Other configured {param_name}s: {_list_str(others)}."
        if others
        else f"No other {param_name}s configured."
    )
    return ToolError(
        f"Call to {tool_name!r} on {param_name}={instance!r} failed: "
        f"{type(underlying).__name__}: {underlying}. {others_msg}"
    )


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
    ) -> None:
        if not instances:
            raise ValueError(
                f"MultiInstanceProxy {name!r}: at least one instance required"
            )
        super().__init__(name)
        self._instances: dict[str, FastMCP] = dict(instances)
        self._param_name = param_name
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
                others = [n for n in instances if n != instance]
                raise _backend_failed_error(
                    original_name, param_name, instance, others, e
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
