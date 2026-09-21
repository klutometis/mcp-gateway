# One advertisement mechanism: stubs everywhere, and Haiku in the gateway

**Status:** scoping, 2026-09-20. Nothing built.
**Supersedes** the "extract harness's triad into a shared library" idea from
earlier the same evening, which was wrong for the reason `notes/stub-schemas.md`
already recorded on 2026-08-30 and this note now has production data for.

## Corrections to what was measured first

Two numbers were wrong in the first pass and both mattered:

1. **mcp.danenberg.ai already runs stub mode.** Probed directly: 75 tools, every
   one `{"type": "object"}`, **3,047 tokens**. The earlier claim that
   llm.danenberg.ai carries "~25,641 tokens" was the *local* gateway
   (`localhost:3100`, 109 tools, stubbing off) measured and then attributed to
   the remote. llm-multiplex has been on stubs since 2026-09-04.
2. **`MCP_SCHEMA_MODE` is set in Railway, not in `mcp-gateway-config`.** Nothing
   in the config repo mentions it, which is why grepping said "off everywhere".
   Worth committing, or at least noting in the deploy README — a mode this
   consequential should not be invisible to the repo.

## The production number that settles the design

The stub design's whole risk is that the model guesses arguments and guesses
wrong. That has been running on mcp.danenberg.ai for sixteen days, so it can be
counted rather than argued. From the llm-multiplex Postgres, tool results since
2026-09-04:

| | |
|---|---|
| tool results | 2,403 |
| signature errors (wrong guess) | 327 |
| **wrong-guess rate** | **13.6%** |

So **86.4% of calls cost nothing at all** — no schema sent, no repair, no
retry, the guess was simply right. The remaining 13.6% pay one retry, and the
error carries the shape:

```
'host' is a required property. Signature: chrome_list_pages(host)
'query' is a required property. Signature: search(query, optional: limit, tbs, …)
```

The misses cluster on tools whose first argument is not guessable from the name
(`search_memory` 95, `add_card` 34, `harness_list_sessions` 22) — exactly the
population a better one-line description would help, which is the case for
Haiku below.

## Harness pays the same 13.6%, and more besides

This is the part the earlier reasoning missed. Harness's catalog does **not**
give the model a schema either — a cold tool is one prose line, so its
arguments are guessed exactly as a stub's are. Both designs pay the same
wrong-guess rate. What harness pays *on top* is promotion:

- `promoteTool` adds the tool to `activeTools`, so the serialized tool array
  changes mid-session;
- the tool block is **first** in Anthropic's cache hierarchy (tools → system →
  messages) and carries the `cacheControl` breakpoint, so changing it
  invalidates the entire cached prefix, not just the tools;
- measured over the corpus: **2.2 distinct cold tools per session** (max 26).
  That is ~2.2 whole-prefix cache invalidations per session, each one
  re-processing the full context.

And promotion is *sticky*: once hot, a tool's full schema sits in every
subsequent turn's block for the life of the session, whether or not it is ever
called again. A signature error in the transcript does the same job and then
**ages out of the context window on its own** — the cost is proportional to
recent use rather than permanent. That self-cleaning property is the strongest
argument for stubs and it is not a token argument at all.

## Measured 2026-09-21: the wrong-guess rate is provider-specific, and mostly one bug

The 13.6% quoted above counted signature errors against all tool-result
messages. Joining each error to the arguments the model actually sent (324 of
324 matched) gives the per-call rate, and splitting it by provider is the whole
story — llm-multiplex fans out to Claude, Gemini and GPT in parallel, so the
same 75 stubs are tested against three models at once:

| provider | calls | signature errors | rate | of those, sent NO args |
|---|---|---|---|---|
| Claude | 547 | 118 | 21.6% | 36 |
| Gemini | 404 | 31 | 7.7% | 4 |
| **GPT** | **256** | **175** | **68.4%** | **175** |
| total | 1,207 | 324 | 26.8% | 215 |

**Every one of GPT's 175 failures sent no arguments at all.** Not a wrong
guess — no guess. `{"type": "object"}` with no `properties` key reads to that
provider as *this tool takes no arguments*, so the model is structurally unable
to pass any, and the signature error teaches it nothing it can act on within
the call. Claude and Gemini infer arguments from the description text; GPT
respects the schema.

This is a latent bug in stub mode that harness could never have found: harness
runs Claude, and its catalog omits the tool from the array entirely rather than
advertising an empty schema.

Classifying all 324 by what would have fixed them:

| | count | share | fix |
|---|---|---|---|
| called with no args at all | 248 | 80% | the schema must name its properties |
| clean one-to-one rename | 31 | 10% | an alias would fix it outright |
| other shape mismatch | 29 | 9% | signature in the advertisement |

The aliases are real and cheap: `read_email` sent `message_id` or `id` for
`messageId` (12), `define` sent `headword` for `word` (5), `etymology` sent
`word` for `query` (5), `save_memory` sent `fact` or `text` for `content` (7),
`restyle_as_me` sent `draft` for `text` (2). An `aliases` map in the stub
middleware — rewrite on the way in, before validation — costs nothing per turn
and removes a tenth of the failures. `etymology(query)` wanting `word` is
arguably the upstream tool's bug, and worth fixing there instead.

But 80% is one bug with one fix.

## What the advertisement should actually be

Five shapes, priced over the same 109 local tools:

| shape | tokens | names the args? |
|---|---|---|
| prose catalog, name only (harness today) | 2,690 | no |
| **prose catalog with signature** | **4,092** | **yes** |
| JSON stub, empty schema (gateway today) | 4,439 | no |
| JSON stub, names-only schema | 7,514 | yes |
| full schemas | 25,642 | yes, with types and prose |

`search_memory(query; optional: limit): Search long-term memory for relevant
facts…` — that line is **cheaper than the empty JSON stub** and fixes the
248-call failure outright. The signature is the information that was missing;
prose is simply a denser envelope for it than JSON.

So the two consumers want different things, and neither wants what it has:

- **Third-party clients** (no system prompt to write into) need the
  names-only JSON schema, 7,514 tokens. Three times today's stub, still 29% of
  full, and it is the only thing that makes the gateway usable from GPT at all.
- **harness and llm-multiplex** (own their prompt) want the prose catalog with
  the signature, 4,092 tokens — cheaper *and* more informative than the stub
  they were about to adopt.

## Revision: keep repair, drop promotion

This changes the "delete the triad" conclusion above, and the reason is worth
stating precisely, because the two halves were doing different jobs.

- **Repair** makes a catalogued tool *callable*. Without it, a call to a tool
  that is not in `activeTools` is a `NoSuchToolError` and simply fails. It is
  load-bearing and cheap — no tokens, no state beyond the step.
- **Promotion** makes it *stay* callable, by adding it to `activeTools` for
  every later step. That is what changes the tool block mid-session, invalidates
  the cached prefix (tools are first in Anthropic's hierarchy), and leaves the
  schema sitting in every subsequent turn whether or not the tool is touched
  again. Measured: 2.2 distinct cold tools per session.

With the signature in the catalog line the model rarely needs the full schema,
so promotion's benefit mostly evaporates while its costs remain. Dropping it
alone gets the property Peter identified: a wrong guess is answered by a
validation error that carries the shape, and **that error ages out of the
context window on its own**. Cost proportional to recent use rather than
permanent. Keeping repair keeps the tool callable while it happens.

That is one deletion instead of three, and it is the one with the cache
invalidation attached.

## The tax, measured

| | today | all-stub | tax |
|---|---|---|---|
| 109 MCP tools (local gateway) | 2,690 (prose catalog) | 4,439 | +1,749 |
| 19 natives (8 hot + 11 cold) | 2,547 | 2,707 | +160 |
| **total** | **5,237** | **7,146** | **+1,909** |

~16 tokens per tool, and it is the irreducible
`{"name":…,"description":…,"input_schema":{…}}` envelope. For reference the
same 109 tools with real schemas are 25,642 tokens, so both designs are doing
the important work; this is an argument about the last 7%.

**What the 1,909 buys:**

- `catalog.ts`, `repair.ts` and the promotion registry are deleted, not
  refactored — including `splitNativeTools`/`splitMcpTools`, `promotedTools`,
  `clearPromotions`, `coldCallRepair`, and the `activeTools` plumbing in
  `prepareStep`.
- No mid-session cache invalidation. The tool block becomes byte-stable for the
  whole session, which is what the `withToolCacheBreakpoint` comment says it
  wants.
- No concurrency hazard. `repair.ts` carries a careful note about two turns in
  one session racing to promote; with no promotion there is no race.
- **No native exception.** A built-in and a gateway tool are advertised
  identically, by the same rule, and the gateway runs that rule for third-party
  clients too. This was the actual complaint — "having these native exceptions
  makes me uncomfortable" — and it is answered by deletion rather than by
  symmetry of two mechanisms.

Both taxes shrink as descriptions get shorter, which is the next section.

## Haiku: the gateway, not a library

The summarizer belongs in the gateway for a mechanical reason: it is the one
place every upstream description passes through, and the work is
**per-description, not per-consumer**. In the gateway it runs once per unique
string and is memoized; in a shared library it runs once per client, three
times over, with three caches.

It also has more to do than fix truncation, though it fixes that too. The two
current regexes fail on disjoint inputs:

| description | harness `catalogLine` | gateway `first_sentence` |
|---|---|---|
| `Send a keypress (e.g. 'Enter'…` | `Send a keypress (e.g.` ✗ | correct ✓ |
| `…public_identifier (URL slug, e.g. …` | truncated ✗ | correct ✓ |
| `Extract content from a single URL. Supports…` | correct ✓ | over-runs ✗ |
| `Send a LinkedIn DM. Reply mode:…` | correct ✓ | over-runs ✗ |

The `(?<![A-Z])` guard fixes abbreviations and breaks acronym-final sentences.
There is no regex that is simply right. Beyond that, a summary can be written
*to be* a catalog entry — and given the miss list above, it can name the
first argument: `search_memory: Search long-term memory for a fact. Takes a
query.` would remove the single largest source of wrong guesses in production.

### When: JIT, memoized on content hash

Not at config-generation time: only **30 of 109** tools have descriptions in git
(`shim-configs/*.json`); the other 79 arrive from proxied upstreams at runtime
and change when an upstream updates. Baking covers a quarter of the surface and
goes stale silently.

Not at startup: 109 blocking model calls against a gateway that deliberately
does not spawn a subprocess until the first tool call.

So **lazily, memoized on `sha256(name + description)`**, regex as the fallback
and as the cold-start answer. An upstream edit invalidates exactly one entry;
nothing needs explicit invalidation. With no API key it degrades to exactly
today's behaviour. This does not undo `fa2356e`: that removed *session* state,
and this is a pure-function memo keyed by content.

Serve it in the `description` field directly — with stubs everywhere there is no
second consumer shape to satisfy, so the `_meta` channel proposed earlier is
unnecessary complexity. (It would be needed only if some consumer wanted the
full description *and* the summary in one response, which no longer describes
anyone.)

## Work, in dependency order

1. **Gateway: Haiku summarizer** behind `MCP_SUMMARY_MODEL`, memoized on content
   hash, regex fallback. Independent of everything else; improves
   mcp.danenberg.ai and llm-multiplex the day it ships.
2. **Gateway: commit `MCP_SCHEMA_MODE=stub`** to the mcp.danenberg.ai deploy
   config, and turn it on for the **local** gateway too. That second half is
   what lets harness delete its MCP catalog.
3. **harness: signature in the catalog line**, from the tool's own zod schema.
   (An earlier draft said harness "already has `signatureOf` and does not use
   it" — it does not have it at all. The gateway has `signature_of`; harness
   would need ~20 lines against zod.)
4. **harness: delete** `catalog.ts`, `repair.ts`, the promotion registry, and
   the `activeTools` filtering. Keep `tool-budget.test.ts`; with stubs every
   description is sent on every turn, so the ratchet matters *more*, and its
   400-char cap should probably tighten toward the 180 the catalog used.
5. **llm-multiplex** needs no change. It already consumes stubs.

## Not doing, and why

- **Gateway-side promotion or per-connection catalog.** Would re-add the state
  `fa2356e` removed to reach an outcome stubs already reach.
- **A shared triad library.** Dies with step 4 — there is nothing left to share.
- **`load_tool_schema` / ask-first.** Deleted from harness 2026-08-16 after an
  A/B: 0/12 compliance.
- **Routing harness's native execution through MCP.** Uniformity of
  *advertisement* is the goal and steps 3–4 achieve it. Uniformity of
  *execution* would cost the `AbortSignal` plumbing, the spill path, per-turn
  closures over live session state, and would make `bash` depend on a second
  process being up — including when that process is what is broken.

## The measurement that says what to do: recovery, not failure

Counting errors was the wrong denominator. What matters is whether the model
*recovers* — the stub design's premise is that a wrong guess is cheap because
the signature error teaches the shape. Tracing each error forward to the next
call of the same tool by the same model in the same conversation:

| provider | recovered | never | recovery rate |
|---|---|---|---|
| Claude | 113 | 5 | **96%** |
| Gemini | 30 | 2 | 94% |
| GPT | 0 | 175 | **0%** |

**The stub design works exactly as designed on Claude and Gemini, and does not
function at all on GPT.** GPT never retries because it never guessed: an empty
`properties` map tells it the tool takes no arguments, so there is nothing for
the correction to correct. Peter's independent observation — "wondering whether
the correction even reached GPT at all" — is confirmed, and it is structural
rather than a context-handling quirk.

Claude's 118 errors over 547 calls break down as: 38% type/extra-property, 31%
no args, 16% shape mismatch, 15% one-to-one rename. So even on a provider that
recovers, roughly half the failures are the advertisement failing to say what
the arguments are.

### This means GPT is not the reason to change anything

If the estate ran Claude only, stubs would already be working: 96% recovery, one
extra round trip on 21.6% of calls. The reasons to change are independent of
GPT and hold on Claude alone:

1. **The signature is free.** Prose catalog *with* signature is 4,092 tokens
   against 4,439 for the empty stub — cheaper *and* it names the arguments.
   There is no trade to weigh.
2. **Promotion's cache invalidation.** 2.2 distinct cold tools per session, each
   rewriting the tool block, which is first in Anthropic's cache hierarchy.
   Nothing to do with which model is downstream.
3. **The native/MCP asymmetry**, which was the original complaint.

GPT's 0% is a real bug and worth fixing, but only a names-only `properties` map
fixes it — signature-in-description does not, because GPT is obeying the schema,
not the prose. That is a separate, larger change (7,514 tokens) and should be
decided on its own merits if and when a GPT-backed client matters.

## The shape that serves everyone: signature first in the description

The gateway cannot write into a consumer's system prompt, so it cannot ship a
prose catalog. But it can put the signature **in the description field**, which
every consumer already reads:

```jsonc
{ "name": "search_memory",
  "description": "search_memory(query; optional: limit) — Search long-term memory for a fact.",
  "inputSchema": { "type": "object" } }
```

That one change serves both consumers from one implementation:

- **Third-party clients** get the signature in the only channel they have.
- **harness and llm-multiplex** get it for free, because harness's `catalogLine`
  already takes the first sentence of the description — so the signature lands
  in the catalog line with no harness-side change at all.

And it is the same string Haiku is being asked to write. The summarizer's job
becomes "signature, then one clause of what it is for", which is a more
constrained and more reliable task than free-form summarization.

## The measurement that says what to do: recovery, not failure

Counting errors was the wrong denominator. What matters is whether the model
*recovers* — the stub design's premise is that a wrong guess is cheap because
the signature error teaches the shape. Tracing each error forward to the next
call of the same tool by the same model in the same conversation:

| provider | recovered | never | recovery rate |
|---|---|---|---|
| Claude | 113 | 5 | **96%** |
| Gemini | 30 | 2 | 94% |
| GPT | 0 | 175 | **0%** |

**The stub design works exactly as designed on Claude and Gemini, and does not
function at all on GPT.** GPT never retries because it never guessed: an empty
`properties` map tells it the tool takes no arguments, so there is nothing for
the correction to correct. Peter's independent observation — "wondering whether
the correction even reached GPT at all" — is confirmed, and it is structural
rather than a context-handling quirk.

Claude's 118 errors over 547 calls break down as: 38% type/extra-property, 31%
no args, 16% shape mismatch, 15% one-to-one rename. So even on a provider that
recovers, roughly half the failures are the advertisement failing to say what
the arguments are.

### This means GPT is not the reason to change anything

If the estate ran Claude only, stubs would already be working: 96% recovery, one
extra round trip on 21.6% of calls. The reasons to change are independent of
GPT and hold on Claude alone:

1. **The signature is free.** Prose catalog *with* signature is 4,092 tokens
   against 4,439 for the empty stub — cheaper *and* it names the arguments.
   There is no trade to weigh.
2. **Promotion's cache invalidation.** 2.2 distinct cold tools per session, each
   rewriting the tool block, which is first in Anthropic's cache hierarchy.
   Nothing to do with which model is downstream.
3. **The native/MCP asymmetry**, which was the original complaint.

GPT's 0% is a real bug and worth fixing, but only a names-only `properties` map
fixes it — signature-in-description does not, because GPT is obeying the schema,
not the prose. That is a separate, larger change (7,514 tokens) and should be
decided on its own merits if and when a GPT-backed client matters.

## The shape that serves everyone: signature first in the description

The gateway cannot write into a consumer's system prompt, so it cannot ship a
prose catalog. But it can put the signature **in the description field**, which
every consumer already reads:

```jsonc
{ "name": "search_memory",
  "description": "search_memory(query; optional: limit) — Search long-term memory for a fact.",
  "inputSchema": { "type": "object" } }
```

That one change serves both consumers from one implementation:

- **Third-party clients** get the signature in the only channel they have.
- **harness and llm-multiplex** get it for free, because harness's `catalogLine`
  already takes the first sentence of the description — so the signature lands
  in the catalog line with no harness-side change at all.

And it is the same string Haiku is being asked to write. The summarizer's job
becomes "signature, then one clause of what it is for", which is a more
constrained and more reliable task than free-form summarization.

## Aliases: casing, not semantics

Of the 31 one-to-one renames, every single one is a synonym or a casing
difference — `message_id`/`id` for `messageId`, `headword` for `word`, `fact`
for `content`. None is a semantic remapping.

So: normalize `snake_case` ↔ `camelCase` in the stub middleware before
validation (12 of 31, and it is a dozen lines with no per-tool config), and
hand-alias the handful of genuine synonyms. The *semantic* remapping case —
right argument name, wrong meaning — is real dark matter: it validates, returns
a wrong answer, and leaves no trace. Unmeasurable, so YAGNI until something
surfaces it.


## Final shape (2026-09-21): prose for clients that own their loop, JSON for the rest

The last open question was harness's prose catalog versus the gateway's JSON
stubs, and whether the two clients we own should differ. Priced over the same
109 tools, with the signature included in every case:

| | tokens | needs omission? | needs repair? |
|---|---|---|---|
| prose catalog, no signature (harness today) | 2,690 | yes | yes |
| **prose catalog + signature** | **4,119** | yes | yes |
| JSON stub, signature in the description | 6,474 | no | no |

The JSON envelope is **2,355 tokens** (21.6/tool) — the price of deleting
`repair.ts` and the `activeTools` plumbing. That is the whole trade, and it is
worth naming the causal chain rather than treating the pieces as independent:

> a prose catalog means the tool is **not in the tool array**, which means a
> direct call raises `NoSuchToolError`, which is why `repairToolCall` exists.

Repair is not a feature; it is the cost of omission. A JSON stub keeps the tool
in the array with an empty schema, so there is nothing to repair. Confirmed by
llm-multiplex, which has no `activeTools`, no `prepareStep` and no
`repairToolCall` anywhere: it passes every gateway tool straight to
`streamText`, and `NoSuchToolError` is structurally impossible there.

**Recommendation: prose + signature, and llm-multiplex adopts it too.** 2,355
tokens per turn, on every turn, against ~40 lines of well-tested repair that
costs nothing at runtime. Attention is the scarce resource; repair is not.

That also dissolves the asymmetry the whole thread was about. The line is not
native-versus-MCP, and not harness-versus-multiplex — both of those are ours and
both own a system prompt (`lib/root-system-prompt.ts` in multiplex's case). The
line is:

- **clients that own their agent loop** (harness, llm-multiplex) — prose catalog
  with signature, in the system prompt, plus repair. A shared TS package, now
  much smaller than the "triad" earlier drafts imagined: catalog + repair, no
  promotion.
- **clients that do not** (Claude Desktop, anything on mcp.danenberg.ai) — JSON
  stubs with the signature in the description, which is the only channel the
  gateway has.

Both carry the same information. The gateway writes the signature once, into
the description; harness and multiplex lift it into their prose line for free,
because `catalogLine` already takes the first sentence.

### Where the casing normalization lives

In the gateway's `StubSchemaMiddleware.on_call_tool`, immediately before
`Draft7Validator` runs: rewrite `snake_case` keys to the schema's `camelCase`
spelling when that is the only difference. One place, all 109 tools, no
per-tool configuration, and it never touches the upstream servers.

Harness's natives do not pass through the gateway, so they need the same
normalization in their own zod path — or, since we own those tool definitions,
just rename the arguments and skip the machinery.

### The aliases stay invisible

The signature names the **canonical** argument, because that is what validation
accepts and what the tool's own documentation says. An alias is a safety net for
a guess, not a second public name — advertising `message_id` when the tool takes
`messageId` would be lying about the interface. If an alias fires often, that is
a signal to rename the argument upstream, not to promote the alias.


## Aliases are unnecessary once the signature is advertised

The 31 one-to-one renames were measured in a regime with **no signature**: the
model invented a plausible argument name because it had nothing to go on. The
right question is not "how often do models guess a synonym" but "do they still
do it once told the canonical name". Tracing every signature error to the next
call of that tool by that model:

| provider | used the canonical names | still wrong | compliance |
|---|---|---|---|
| Claude | 111 | 2 | **98%** |
| Gemini | 29 | 0 | **100%** |

And the two residual failures are not naming errors at all — both are
`search_events` omitting the required `timeMax` from a nine-argument signature.
**Zero post-signature failures were caused by an argument name.**

So: no alias map, and no casing normalization either. Both were solutions to a
problem that only exists while the advertisement withholds the argument names.
Advertise them and the problem does not arise; the ~40 lines of alias plumbing
would run on every call forever to fix a class of error that no longer occurs.

If a rename ever does fire post-signature, it is a signal that the argument is
badly named — fix it upstream rather than paper over it. And the residual
`search_events` case points at something more useful than aliases: a signature
with nine arguments is hard to satisfy, which is an argument for the gateway's
existing arg-subsetting, not for renaming.
