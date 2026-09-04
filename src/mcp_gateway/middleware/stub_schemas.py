"""Advertise tools without their argument schemas; supply the shape on demand.

Why
---
``tools/list`` ships every tool's full JSON Schema to every consumer on every
call. Measured over the 30 device-backed tools on mcp.danenberg.ai:

    descriptions   4,007 chars  ~1,001 tokens
    schemas       11,993 chars  ~2,998 tokens   <- 75% of the payload
    total         17,865 chars  ~4,466 tokens

A hosted assistant pays that on every turn whether or not it calls anything.

With this middleware the advertisement becomes name + first sentence +
``{"type": "object"}`` — ~994 tokens, a 78% saving — and the argument shape is
delivered on the first wrong guess instead of pre-emptively to everyone.

The model is asked to do exactly what it already does: call the tool it wants
and infer the arguments from the name, the sentence, and the conversation. Most
calls are right and cost nothing extra. A wrong one is answered with the
signature, and the retry is correct. There is no "load the schema first" tool,
deliberately: harness measured that pattern at 0/12 compliance because the
model calls the tool it wants regardless, and deleted it.

See ``notes/stub-schemas.md`` for the full argument, including why this is the
same mechanism as harness's prompt catalog in a more expensive envelope.

Design notes
------------
- **Validate here, do not forward and hope.** Tested against a real upstream:
  missing and mistyped arguments produce decent errors, but an *extra unknown
  argument is silently accepted and the call executes*. Upstream behaviour also
  varies by implementation language (Go, TypeScript/zod, Python/FastMCP), and
  none of them return the tool's signature. Validating at the gateway is the
  only way to get one uniform, useful answer — and the validation *is* the
  affordance.

- **The error is a signature, not a validator dump.** ``send-keys(session,
  keys, optional: enter)`` is what the model was missing. A jsonschema
  traceback is not.

- **Off by default.** ``full`` is today's behaviour, so enabling this is a
  config change and so is rolling it back.

- **Not for every consumer.** harness runs its own catalog: it withholds
  schemas in the prompt and reveals them on promotion. Serving it stubs would
  strip the schema that promotion exists to reveal, and it would pay the JSON
  envelope for advertisements it already has cheaper. Leave harness on
  ``full``.

- **Fail-open on unknown tools.** If a call names a tool this middleware has
  never advertised, it forwards untouched rather than inventing an error.

What it cannot do
-----------------
Semantic mistakes. A call that is schema-valid and wrong — the right shape
pointed at the wrong session, the wrong tab — is indistinguishable from a
correct one here, and belongs to the tool.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Sequence

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import Tool
from jsonschema import Draft7Validator

log = logging.getLogger(__name__)

_STUB_SCHEMA: dict[str, Any] = {"type": "object"}

# A sentence ends at ". " — not at "e.g. " or "0.5 " or "Dr. ".
_SENTENCE_END = re.compile(r"(?<![A-Z])(?<!\be\.g)(?<!\bi\.e)(?<!\bvs)\.(?=\s)")


def first_sentence(text: str) -> str:
    """The first sentence of ``text``, or all of it if there is only one."""
    if not text:
        return ""
    m = _SENTENCE_END.search(text)
    return text[: m.end()] if m else text.strip()


def signature_of(name: str, schema: dict[str, Any] | None) -> str:
    """``name(a, b, optional: c)`` — the one line a wrong guess was missing.

    Required parameters first, in schema order, then optionals behind a single
    ``optional:`` marker. Types are omitted: the names carry nearly all the
    information, and a model that knows a parameter is called ``limit`` does
    not also need to be told it is an integer.
    """
    props = (schema or {}).get("properties") or {}
    if not props:
        return f"{name}()"
    required = [p for p in props if p in set((schema or {}).get("required") or [])]
    optional = [p for p in props if p not in set((schema or {}).get("required") or [])]
    parts = list(required)
    if optional:
        parts.append("optional: " + ", ".join(optional))
    return f"{name}({', '.join(parts)})"


class StubSchemaMiddleware(Middleware):
    """Serve argless tool stubs; validate calls against the real schema.

    Parameters
    ----------
    describe:
        ``"first-sentence"`` (default) trims each advertised description to its
        first sentence; ``"full"`` leaves descriptions alone and only strips
        the schemas. Descriptions are ~1,001 of the 4,466 tokens, so trimming
        them is worth ~600/turn on its own.
    always_full:
        Tool names that keep their real schema in the advertisement. For tools
        where a schema-valid guess would be costly — ``send-keys``,
        ``imessage_send`` — paying ~100 tokens to remove the guesswork is the
        right trade.
    """

    def __init__(
        self,
        *,
        describe: str = "first-sentence",
        always_full: frozenset[str] = frozenset(),
    ) -> None:
        self.describe = describe
        self.always_full = always_full
        # name -> the real schema, captured as we stub it on the way out.
        self._schemas: dict[str, dict[str, Any]] = {}

    @classmethod
    def from_env(cls) -> "StubSchemaMiddleware | None":
        """Build from env, or ``None`` if disabled.

        Env (all optional):
          ``MCP_SCHEMA_MODE``        stub|full (default "full" — off)
          ``MCP_SCHEMA_DESCRIBE``    first-sentence|full (default first-sentence)
          ``MCP_SCHEMA_ALWAYS_FULL`` comma-separated tool names, never stubbed
        """
        if os.environ.get("MCP_SCHEMA_MODE", "full").lower() != "stub":
            return None
        raw = os.environ.get("MCP_SCHEMA_ALWAYS_FULL", "")
        return cls(
            describe=os.environ.get("MCP_SCHEMA_DESCRIBE", "first-sentence"),
            always_full=frozenset(n.strip() for n in raw.split(",") if n.strip()),
        )

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)

        out: list[Tool] = []
        for tool in tools:
            # Remember the real schema even for always_full tools: on_call_tool
            # validates whatever it knows about, and a tool that ships its
            # schema still benefits from a signature instead of an upstream's
            # idiosyncratic complaint.
            if tool.parameters:
                self._schemas[tool.name] = tool.parameters

            if tool.name in self.always_full:
                out.append(tool)
                continue

            update: dict[str, Any] = {"parameters": _STUB_SCHEMA}
            if self.describe == "first-sentence" and tool.description:
                update["description"] = first_sentence(tool.description)
            out.append(tool.model_copy(update=update))

        return out

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, Any],
    ) -> Any:
        name = context.message.name
        schema = self._schemas.get(name)

        # Never advertised, or genuinely takes no arguments: nothing to check.
        if schema is None:
            return await call_next(context)

        args = context.message.arguments or {}
        errors = sorted(
            Draft7Validator(self._strict(schema)).iter_errors(args),
            key=lambda e: list(e.absolute_path),
        )
        if not errors:
            return await call_next(context)

        # One line of what went wrong, then the shape. The shape is the point:
        # the model guessed because it had no schema, so hand it the schema in
        # the form it can act on.
        detail = "; ".join(self._describe(e) for e in errors[:3])
        raise ToolError(f"{detail}. Signature: {signature_of(name, schema)}")

    @staticmethod
    def _strict(schema: dict[str, Any]) -> dict[str, Any]:
        """The schema with unknown top-level keys made an error.

        JSON Schema permits extra properties unless told otherwise, and this is
        precisely the case that motivated validating here at all: an upstream
        given ``{ref, tail, bogus}`` ignored ``bogus`` and ran the call, so a
        model that half-guessed got a success and no correction.

        Only the top level, and only when the schema does not already have an
        opinion — nested objects are the tool's business, and a schema that
        deliberately allows extras keeps that.
        """
        if "additionalProperties" in schema or not schema.get("properties"):
            return schema
        return {**schema, "additionalProperties": False}

    @staticmethod
    def _describe(error: Any) -> str:
        """A jsonschema error as something worth reading."""
        where = ".".join(str(p) for p in error.absolute_path)
        if error.validator == "required":
            return error.message  # "'ref' is a required property" — already good
        if error.validator == "additionalProperties":
            return error.message  # names the unexpected key
        if where:
            return f"{where}: {error.message}"
        return error.message
