"""Unit tests for the MultiInstanceProxy wrapper.

These tests don't spawn real subprocesses or HTTP servers. We build
trivial in-process FastMCP instances and use them as the "backings"
to exercise the wrapper's routing + schema-mutation logic.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from mcp_gateway.multi_instance import (
    MultiInstanceProxy,
    inject_instance_param,
)


# ---------------------------------------------------------------------------
# Fixtures: minimal in-process FastMCP "backings"
# ---------------------------------------------------------------------------


def _make_backing(label: str, *, send_fails: bool = False) -> FastMCP:
    """Build a tiny FastMCP server with one ``send`` tool.

    The tool's return embeds ``label`` so tests can assert which
    backing actually handled the call. If ``send_fails`` is set, the
    tool raises so we can test the error-enrichment path.
    """
    server = FastMCP(f"backing-{label}")

    @server.tool
    def send(to: str, message: str) -> str:
        """Send a message."""
        if send_fails:
            raise RuntimeError(f"backing {label} is broken")
        return f"[{label}] sent to {to}: {message}"

    return server


# ---------------------------------------------------------------------------
# inject_instance_param: pure-function tests
# ---------------------------------------------------------------------------


class TestInjectInstanceParam:
    def test_prepends_to_properties(self) -> None:
        schema = {
            "type": "object",
            "properties": {"to": {"type": "string"}, "msg": {"type": "string"}},
            "required": ["to"],
        }
        out = inject_instance_param(schema, "account", ["personal", "work"])

        keys = list(out["properties"].keys())
        assert keys == ["account", "to", "msg"], "param must be first"

    def test_enum_when_multiple_instances(self) -> None:
        out = inject_instance_param(
            {"type": "object", "properties": {}}, "host", ["laptop", "desktop"]
        )
        assert out["properties"]["host"]["enum"] == ["laptop", "desktop"]

    def test_no_enum_when_single_instance(self) -> None:
        out = inject_instance_param(
            {"type": "object", "properties": {}}, "host", ["laptop"]
        )
        assert "enum" not in out["properties"]["host"]

    def test_required_when_multiple(self) -> None:
        out = inject_instance_param(
            {"type": "object", "properties": {"to": {}}, "required": ["to"]},
            "account",
            ["a", "b"],
        )
        assert out["required"] == ["account", "to"], "param prepended to required"

    def test_not_required_when_single(self) -> None:
        out = inject_instance_param(
            {"type": "object", "properties": {"to": {}}, "required": ["to"]},
            "account",
            ["only"],
        )
        assert out["required"] == ["to"], "single instance => param is optional"

    def test_does_not_mutate_input(self) -> None:
        original = {
            "type": "object",
            "properties": {"to": {"type": "string"}},
            "required": ["to"],
        }
        snapshot = {**original, "properties": dict(original["properties"])}
        inject_instance_param(original, "account", ["a", "b"])
        assert original == snapshot, "input schema must not be mutated"

    def test_handles_missing_properties_and_required(self) -> None:
        out = inject_instance_param({"type": "object"}, "account", ["a", "b"])
        assert "account" in out["properties"]
        assert out["required"] == ["account"]

    def test_handles_empty_schema(self) -> None:
        out = inject_instance_param({}, "account", ["a", "b"])
        assert out["type"] == "object"
        assert "account" in out["properties"]


# ---------------------------------------------------------------------------
# MultiInstanceProxy: routing tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMultiInstanceProxy:
    async def test_list_tools_does_not_synthesize_discovery_tool(self) -> None:
        # The synthetic <param>_list tool was intentionally dropped: with
        # enum-in-schema the model already sees the configured set on
        # every tools/list. A discovery tool would just duplicate that.
        # If we ever switch to bare schema (no enum), bring it back —
        # then it becomes load-bearing. See module docstring.
        wrapper = MultiInstanceProxy(
            "gmail",
            instances={"personal": _make_backing("personal")},
            param_name="account",
        )
        tools = await wrapper.list_tools()
        names = [t.name for t in tools]
        assert "account_list" not in names
        assert "send" in names, "backing tools still surfaced"

    async def test_list_tools_injects_param_into_each(self) -> None:
        wrapper = MultiInstanceProxy(
            "gmail",
            instances={
                "personal": _make_backing("personal"),
                "work": _make_backing("work"),
            },
            param_name="account",
        )
        tools = await wrapper.list_tools()
        send_tool = next(t for t in tools if t.name == "send")
        assert "account" in send_tool.parameters["properties"]
        assert send_tool.parameters["properties"]["account"]["enum"] == [
            "personal",
            "work",
        ]
        assert "account" in send_tool.parameters["required"]

    async def test_routes_to_correct_instance(self) -> None:
        wrapper = MultiInstanceProxy(
            "gmail",
            instances={
                "personal": _make_backing("personal"),
                "work": _make_backing("work"),
            },
            param_name="account",
        )
        result = await wrapper.call_tool(
            "send", {"account": "work", "to": "x@y", "message": "hi"}
        )
        text = _extract_text(result)
        assert "[work]" in text
        assert "sent to x@y" in text

    async def test_single_instance_defaults_param(self) -> None:
        wrapper = MultiInstanceProxy(
            "gmail",
            instances={"only": _make_backing("only")},
            param_name="account",
        )
        # Agent omits account; should still work.
        result = await wrapper.call_tool(
            "send", {"to": "x@y", "message": "hi"}
        )
        text = _extract_text(result)
        assert "[only]" in text

    async def test_missing_param_when_multiple_errors_with_set(self) -> None:
        wrapper = MultiInstanceProxy(
            "gmail",
            instances={
                "personal": _make_backing("personal"),
                "work": _make_backing("work"),
            },
            param_name="account",
        )
        with pytest.raises(ToolError) as excinfo:
            await wrapper.call_tool("send", {"to": "x@y", "message": "hi"})
        msg = str(excinfo.value)
        assert "requires 'account'" in msg
        assert "personal" in msg and "work" in msg

    async def test_unknown_value_errors_with_set(self) -> None:
        wrapper = MultiInstanceProxy(
            "gmail",
            instances={
                "personal": _make_backing("personal"),
                "work": _make_backing("work"),
            },
            param_name="account",
        )
        with pytest.raises(ToolError) as excinfo:
            await wrapper.call_tool(
                "send", {"account": "prod", "to": "x@y", "message": "hi"}
            )
        msg = str(excinfo.value)
        assert "'prod'" in msg
        assert "personal" in msg and "work" in msg

    async def test_backend_failure_enriches_with_others(self) -> None:
        wrapper = MultiInstanceProxy(
            "gmail",
            instances={
                "personal": _make_backing("personal", send_fails=True),
                "work": _make_backing("work"),
            },
            param_name="account",
        )
        with pytest.raises(ToolError) as excinfo:
            await wrapper.call_tool(
                "send", {"account": "personal", "to": "x@y", "message": "hi"}
            )
        msg = str(excinfo.value)
        assert "personal" in msg, "must say which instance failed"
        assert "work" in msg, "must list other configured instances"
        # FastMCP wraps the RuntimeError in its own ToolError before our
        # handler sees it; what matters for the model is that the
        # underlying message ("backing personal is broken") propagates.
        assert "broken" in msg, "must carry the underlying error message"

    async def test_param_name_is_configurable(self) -> None:
        wrapper = MultiInstanceProxy(
            "chrome",
            instances={"laptop": _make_backing("laptop")},
            param_name="host",
        )
        tools = await wrapper.list_tools()
        send = next(t for t in tools if t.name == "send")
        assert "host" in send.parameters["properties"], (
            "injected param name follows config"
        )

    async def test_empty_instances_raises(self) -> None:
        with pytest.raises(ValueError):
            MultiInstanceProxy("x", instances={}, param_name="account")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_text(result: Any) -> str:
    """Pull the text out of whatever FastMCP returned.

    call_tool returns ToolResult with .content (list of TextContent etc.).
    """
    if hasattr(result, "content"):
        return "".join(
            getattr(item, "text", "") for item in result.content
        )
    return str(result)
