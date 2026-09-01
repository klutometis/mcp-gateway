# Stub schemas: defer the shape until someone calls (decision)

**Status:** Decided 2026-08-30. Not implemented.

## Problem

The gateway advertises every tool with its full JSON Schema, on every
`tools/list`, to every consumer. Measured over the 30 device-backed tools in
`mcp-gateway-config/deploy/mcp.danenberg.ai/shim-configs/`:

| | chars | ~tokens |
|---|---|---|
| descriptions | 4,007 | 1,001 |
| **schemas** | **11,993** | **2,998** |
| total advertisement | 17,865 | 4,466 |

Schemas are **75%** of the payload, and that is only the device-backed
servers — the deployment has 60 tools across 11. A hosted assistant pays this
on every turn whether or not it calls anything.

## Decision

Advertise a **stub** — name, first sentence of the description, and
`inputSchema: {"type": "object"}` — and keep the real schema server-side. On
call, validate against the real schema; on failure, return the signature as
the error. The model's second attempt is correct and everything after that is
free.

| | ~tokens/turn |
|---|---|
| full advertisement | 4,466 |
| stub advertisement | 994 |
| **saving** | **3,471 (78%)** |

## Relation to harness's catalog — the same idea, serialized differently

harness (`packages/daemon/src/mcp/catalog.ts`) omits cold tools from the tool
array and lists them in its **system prompt** as `name: first sentence`. A
direct call to an omitted tool raises `NoSuchToolError` client-side, which
`mcp/repair.ts` catches, promotes, and re-runs with the model's original
arguments.

It is tempting to call that "invisible repair" and treat it as a category
apart. It is not. Both designs advertise a name and a sentence, ask the model
to infer the arguments, and correct it once if it infers wrong:

| | harness | gateway stub |
|---|---|---|
| what the model sees up front | name + first sentence | name + first sentence |
| asked to load a schema first | no | no |
| args inferred from name, sentence, context | yes | yes |
| **inference right** | runs, 0 extra samples | runs, 0 extra samples |
| **inference wrong** | `InvalidToolInputError`, 1 correction | signature error, 1 correction |

Measured over the same 30 device-backed tools, the difference is the envelope
and nothing else:

| advertisement | tokens/tool |
|---|---|
| harness catalog line (prose, system prompt) | 17.9 |
| gateway stub (JSON, tool array) | 33.1 |
| full schema | 148.9 |

Prose is cheaper than JSON because a tool-array entry has to carry `{"name":
…, "description": …, "inputSchema": {…}}` per tool. That is the whole of the
+1,291-tokens-per-turn measurement that rejected stubs *for harness*: 89 tools
× the envelope. It is a serialization result, not evidence that one mechanism
repairs and the other does not.

The genuine asymmetry is narrow: **harness's repair exists to undo a problem
harness creates.** Omitting a tool from the array manufactures a
`NoSuchToolError` that has to be caught and replayed. The gateway's stub never
manufactures it — the tool is present, the empty schema accepts anything
client-side, and the call simply arrives. Fewer moving parts, no repair hook,
same outcome. If anything the stub design is the cleaner of the two; it just
costs 15 tokens a tool more to advertise.

The reason the gateway cannot adopt harness's cheaper form is positional, not
conceptual: it is an MCP server outside the agent loop, with no channel into
the consumer's system prompt, and a tool absent from the list is rejected by
the **client** before any call reaches it.

## Why intercept rather than let the upstream complain

The obvious cheaper design is to forward whatever arrives and let the upstream
reject it. Tested against a real stdio upstream (`harness-mcp`, TypeScript,
zod-validated — same family as the shims):

| input | upstream response |
|---|---|
| missing required arg | `-32602`, names the path and expected type — good |
| wrong arg name | `-32602`, says `ref` is missing — but never says `session` was wrong |
| wrong type | `-32602`, names path, expected, received — good |
| **extra unknown arg** | **executed, `isError=None`** — silently ignored |

Three problems, in order of importance:

1. **Extra args are swallowed.** The call runs. A model that guessed
   `{ref, tail, bogus}` gets a successful result and no signal that `bogus`
   meant nothing.
2. **Nothing returns the signature.** The model learns which field is
   *missing*, never what the tool accepts. That is the one thing a stub
   consumer needs, and it is exactly what the interception can supply.
3. **Behaviour is per-upstream.** This one is TypeScript/zod; `chrome-cdp-mcp`
   is Go; others are Python/FastMCP. Three validation stacks, no guarantee of
   uniform error shape, and no guarantee the next one validates at all.

The gateway does **no** input validation today (`grep -rn "jsonschema\|validate"
src/mcp_gateway/` finds nothing but comments). So this is new code, not a
change to existing behaviour: hold the real schema, validate on call, and
return the signature on failure. One piece, two purposes — the validation *is*
the informative error.

## Accepted risk

A guess that is both schema-valid and semantically wrong reaches a
side-effecting tool: `send-keys`, `imessage_send`, `whatsapp_send_message`.
Low, because a stub advertises no property names to guess from, but nonzero.

**Accepted for now.** If it bites, the mitigation is a per-tool
`always_full_schema` allowlist so the dangerous handful ship complete shapes
and pay their ~100 tokens.

## Configuration

Per-consumer, not global, and reversible:

```jsonc
// servers.json, or per-deployment
"schema_mode": "stub" | "full"     // default "full" — today's behaviour
```

Keyed on the consumer, which the gateway already distinguishes by bearer
token. `full` restores exactly what ships today, so a bad rollout is a config
change rather than a revert.

**harness should stay on `full`,** for a plainer reason than "harness is
special". Its catalog already advertises cold tools at 17.9 tokens each, so a
gateway stub adds nothing it does not have. What the stub *removes* is the
real schema that promotion exists to reveal: promote a tool whose schema is
`{"type":"object"}` and the model is no better informed than before, and every
shape has to be learned from an error that now costs a network hop instead of
an in-process validation.

So it is not a catastrophe, just pointless — harness would pay the JSON
envelope for advertisements it already has cheaper, and lose local validation
in exchange. Serve it the schemas; it withholds them itself.

That is also the honest answer to "can we ship this to harness too": there is
nothing to ship. It is the same mechanism, and harness is already running the
cheaper serialization of it.

The transfer that *is* worth making runs the other way, and one leg of it runs
back again:

- **No ask-first tool.** `request_tool` was deleted on 2026-08-16 after an A/B
  showed the model calls the tool it wants regardless (compliance 0/12 vs
  2/12 when scolded). Do not build `load_tool_schema` here.
- **First sentence is enough.** `send-keys` spends 289 chars where 26 identify
  it; trimming descriptions alone saves ~600 tokens/turn and is independent of
  everything above.
- **The error is the affordance.** Return `name(arg, arg, optional: arg)`, the
  shape harness's `signatureOf` used, not a validator dump.

And the leg running back: **harness should return a signature on a wrong-guess
too.** Today a cold tool called with bad arguments produces
`AI_InvalidToolInputError` with the SDK's zod dump — correct, and much less
useful than the one line naming the shape. harness already has `signatureOf`;
it was written for the predecessor of `repair.ts` and is now unused. The
model's inference failed for want of a schema, and a validator dump is a poor
substitute for the signature it was missing. Cheap fix, same idea as step 3
below, and it makes the two implementations behave identically on the only
path where they visibly differ.

## Work

1. Hold real schemas server-side; serve stubs when `schema_mode: "stub"`.
2. Validate on call against the real schema.
3. On failure, return the signature — not a zod/jsonschema dump.
4. Trim advertised descriptions to the first sentence (independent, ~600/turn).
5. Leave `full` as the default and pin harness to it.

Steps 1–3 are one change; 4 stands alone and could land first.

## Measurements

Reproduce with `mcp-gateway-config/deploy/mcp.danenberg.ai/shim-configs/*.json`:

```
30 device-backed tools
  full advertisement : 17,865 chars  ~4,466 tokens
  stub advertisement :  3,978 chars  ~  994 tokens
  saving             : 13,887 chars  ~3,471 tokens/turn  (78%)

per server (tools, full-schema tokens)
  chrome  12  1,478      tmux     6  1,343
  harness  8  1,024      imessage 4    619
```
