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

## Why not harness's mechanism

harness (`packages/daemon/src/mcp/catalog.ts`) solves the same problem by
omitting cold tools from the tool array entirely and listing them in the
**system prompt** as `name: first sentence`. That is cheaper still — 1.7k for
64 tools — and a direct call to an omitted tool is caught, promoted, and
re-run invisibly (`mcp/repair.ts`).

The gateway cannot do that. It is an MCP server, outside the agent loop, with
no channel into the consumer's system prompt. And a tool that is *absent* from
the list gets rejected by the **client**, so the call never arrives and there
is nothing to repair.

Stubs were explicitly measured and rejected *for harness* at +1,291 tokens per
turn against the catalog. That verdict does not transfer: harness's baseline
is a prose catalog it can inject, the gateway's baseline is full schemas.
Same mechanism, opposite conclusion, because the alternative differs.

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

**harness must stay on `full`.** It withholds schemas itself and promotes a
tool on first call; if the gateway then served it a stub, the promotion would
yield no schema and harness would pay a *second* round trip it does not pay
today. The two mechanisms compose only if the gateway is honest with harness.

That is also the answer to "can we eventually ship this to harness too": not
this half. harness already has the better version for its situation — a prose
catalog plus invisible repair — and stubs would be a regression there. The
transfer runs the other way. What the gateway should take from harness is what
harness measured:

- **No ask-first tool.** `request_tool` was deleted on 2026-08-16 after an A/B
  showed the model calls the tool it wants regardless (compliance 0/12 vs
  2/12 when scolded). Do not build `load_tool_schema` here.
- **First sentence is enough.** `send-keys` spends 289 chars where 26 identify
  it; trimming descriptions alone saves ~600 tokens/turn and is independent of
  everything above.
- **The error is the affordance.** Return `name(arg, arg, optional: arg)`, the
  shape harness's `signatureOf` used, not a validator dump.

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
