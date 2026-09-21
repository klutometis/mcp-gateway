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
from fastmcp.tools import Tool
from jsonschema import Draft7Validator

log = logging.getLogger(__name__)

_STUB_SCHEMA: dict[str, Any] = {"type": "object"}

# Types worth keeping in ``names`` mode. ``string`` is deliberately absent: it
# is what a model sends by default, so saying so spends tokens confirming the
# status quo. Every type error observed across two corpora was a string sent
# where a non-string was wanted — 15 integer, 7 array, 1 boolean — and never
# the reverse.
_KEEP_TYPES = frozenset({"integer", "number", "boolean", "array", "object"})


def _slim(spec: Any) -> dict[str, Any]:
    """One property, reduced to its type — and, for an array, its item type.

    ``items`` is not optional decoration. Gemini rejects the WHOLE tool array
    with ``function_declarations[0].parameters.properties[excludeTags].items:
    missing field`` if any array lacks it, so a single bare ``{"type":
    "array"}`` anywhere makes every tool uncallable for that provider. The
    first version of this function dropped ``items`` and did exactly that to 44
    properties across these 109 tools.

    Kept deliberately shallow otherwise: an object's own properties are not
    recursed into, because that is where the token weight lives and a model
    that knows an argument is an object can ask. Arrays are the exception only
    because the item type is load-bearing for validation upstream.
    """
    if not isinstance(spec, dict):
        return {}
    t = spec.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    if t not in _KEEP_TYPES:
        return {}
    out: dict[str, Any] = {"type": t}
    if t == "array":
        # Recurse: `create_sheet.data` is an array of arrays of strings, and
        # the inner array needs its own `items` for the same reason.
        inner = spec.get("items")
        out["items"] = _slim(inner) if isinstance(inner, dict) else {"type": "string"}
        if not out["items"]:
            # An untyped item still has to be *something* Gemini accepts.
            out["items"] = {"type": "string"}
    return out


def names_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """The argument names, and nothing else worth paying for.

    ``{"query": {}, "limit": {"type": "integer"}}`` — no descriptions, no
    enums, no nested item schemas, no defaults. Those are the expensive parts
    of a JSON Schema and the parts a model can infer or ask about; the names
    are the cheap part it cannot guess.

    WHY THIS EXISTS. ``stub`` mode replaced the whole schema with
    ``{"type": "object"}`` and moved the argument names into the description,
    which works for a model that reads descriptions. GPT does not: its
    function-calling obeys the schema, so an empty ``properties`` reads as
    *this tool takes no arguments* and it sends ``{}`` — 175 times out of 175
    in production, with a 0% recovery rate, because there is no wrong guess for
    the error to correct. The names have to be in the schema, which is the one
    channel every consumer reads.
    """
    props = (schema or {}).get("properties") or {}
    if not props:
        return dict(_STUB_SCHEMA)
    slim: dict[str, Any] = {name: _slim(spec) for name, spec in props.items()}
    out: dict[str, Any] = {"type": "object", "properties": slim}
    required = [p for p in (schema or {}).get("required") or [] if p in props]
    if required:
        out["required"] = required
    return out

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
    signatures:
        Prefix each stubbed description with ``name(a, b; optional: c)``. On by
        default as of 2026-09-21. Without it the advertisement says what a tool
        does and not how to call it, which is what the model was guessing at;
        with it the signature costs ~13 tokens a tool and removes the guess.
        Turn it off only to reproduce the older behaviour.
    """

    def __init__(
        self,
        *,
        describe: str = "first-sentence",
        always_full: frozenset[str] = frozenset(),
        signatures: bool = True,
        mode: str = "stub",
    ) -> None:
        self.describe = describe
        self.always_full = always_full
        self.signatures = signatures
        self.mode = mode
        # name -> the real schema, captured as we stub it on the way out.
        self._schemas: dict[str, dict[str, Any]] = {}

    @classmethod
    def from_env(cls) -> "StubSchemaMiddleware | None":
        """Build from env, or ``None`` if disabled.

        Env (all optional):
          ``MCP_SCHEMA_MODE``        names|stub|full (default "full" — off)
          ``MCP_SCHEMA_DESCRIBE``    first-sentence|full (default first-sentence)
          ``MCP_SCHEMA_ALWAYS_FULL`` comma-separated tool names, never stubbed
          ``MCP_SCHEMA_SIGNATURES``  0/off to omit the signature (default on;
                                     ignored in ``names`` mode, where the
                                     schema already names the arguments)

        ``names`` is the one to want. ``stub`` is kept because it is what ran
        from 2026-09-04 and its numbers are in notes/; it should not be chosen
        fresh, since it is the mode that made GPT unable to call a tool at all.
        """
        mode = os.environ.get("MCP_SCHEMA_MODE", "full").lower()
        if mode not in ("stub", "names"):
            return None
        raw = os.environ.get("MCP_SCHEMA_ALWAYS_FULL", "")
        return cls(
            describe=os.environ.get("MCP_SCHEMA_DESCRIBE", "first-sentence"),
            always_full=frozenset(n.strip() for n in raw.split(",") if n.strip()),
            signatures=os.environ.get("MCP_SCHEMA_SIGNATURES", "1").lower()
            not in ("0", "off", "false", "no"),
            mode=mode,
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

            real = self._schemas.get(tool.name)
            # `names` keeps the argument names in the schema; `stub` throws the
            # whole schema away and reconstructs the names in prose. The
            # difference only matters to a consumer that reads one channel and
            # not the other, which is exactly the case that forced this: see
            # names_schema.
            update: dict[str, Any] = {
                "parameters": names_schema(real) if self.mode == "names" else _STUB_SCHEMA
            }
            line = tool.description or ""
            if self.describe == "first-sentence" and line:
                line = first_sentence(line)
            # In `names` mode the schema already carries them, so a signature
            # in the description would say the same thing twice — and it is
            # the redundancy that pays for the change.
            if self.signatures and self.mode != "names":
                # The schema is about to be removed, so the argument names go
                # into the only channel left. This is not decoration: sixteen
                # days of production traffic through this gateway produced 324
                # schema-validation failures, and once an error handed the
                # model the signature, the retry used the canonical names 98%
                # of the time (Claude) and 100% (Gemini). The names were the
                # entire gap — so stop making every consumer discover them by
                # failing first.
                line = f"{signature_of(tool.name, self._schemas.get(tool.name))} — {line}".strip(
                    " —"
                )
            if line != (tool.description or ""):
                update["description"] = line
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
