# Identity propagation: gateway → upstreams (decision)

**Status:** Decided 2026-07-20. Implemented in `src/mcp_gateway/identity.py`.

## Problem

A multi-tenant gateway authenticates one human per request, then fans the
call out to backend MCP servers (**upstreams**: zep, whatsapp, linkedin,
the Workspace tools-server, …). Each upstream must scope its data to
*that* caller. So the caller's identity has to travel the **gateway →
upstream** hop, per request.

Two ways to carry it.

## Option A — identity as an OAuth/JWT claim

Identity rides inside a signed bearer token (`Authorization: Bearer
<JWT>`); the tenant is a verified claim (`sub` / `org` / custom). This is
MCP's own native auth model.

- **Pro:** self-validating and tamper-proof — the token is signed, so an
  upstream can *cryptographically verify* who the caller is without
  trusting anyone upstream of it. Safe even if the upstream is exposed
  publicly.
- **Con:** every upstream must implement OAuth token validation (fetch
  JWKS, verify signature/audience/expiry). Heavy for small servers. Also
  couples each upstream to the identity provider.

## Option B — identity as W3C `baggage` (chosen)

`baggage` is a **W3C standard** (and the OpenTelemetry standard) for
propagating arbitrary key–value context across services:

```
baggage: userId=peter%40danenberg.name
```

The gateway stamps it; the upstream reads the key it cares about.

- **Pro:** dead simple to produce/consume (no crypto), a real standard
  (not a bespoke `X-` header — RFC 6648 discourages those), **generic**
  (any service — `llm.danenberg.ai`, any MCP — can read the same context
  for free), and composes with distributed tracing.
- **Con:** it's **plaintext and unsigned** — trivially spoofable by
  anyone who can reach the upstream. So it's only safe behind a trust
  boundary.

## Decision: baggage, because of our topology

We use **`baggage`** for the gateway → upstream hop. It's safe *here*
specifically because:

1. **The gateway is the trust boundary.** It authenticates the human via
   OAuth and *injects* the baggage id. Upstreams never authenticate the
   end user themselves — they trust the gateway's stamp.
2. **Upstreams are not publicly reachable.** They live behind the gateway
   on internal networking (`*.railway.internal`). The only thing that can
   set baggage on them is the gateway. No external actor can forge it.
3. **Baggage carries an opaque id, never a secret.** Just the tenant/
   group id (an identifier, not a credential). Per the W3C/OTel guidance:
   never put secrets in baggage; use opaque ids; validate at the edge.

If either invariant breaks — an upstream becomes publicly reachable, or
we can't guarantee the gateway is the only writer — **switch that
upstream to Option A** (verify the OAuth token/claim), because a
spoofable header is no longer acceptable.

## Genericity + graceful single-tenant fallback

- **Generic by construction.** The carrier is a standard header and the
  baggage **key is configurable** (default `userId`). Nothing is
  Spark-specific; `llm.danenberg.ai` or any other service can adopt the
  same mechanism.
- **Graceful fallback, no special case.** Resolution rule everywhere:
  *baggage present → use that identity; absent → fall back to an env
  default → single-tenant.* Same code path both ways. So a single-tenant
  deployment (e.g. `mcp.danenberg.ai`, no baggage, one env id) and a
  multi-tenant one (baggage per request) run **identical** code — N=1 is
  just "one id ever flows through," never a `if single_tenant:` branch.

## Shape

- **Gateway** (`identity.py`): `make_identity_forwarding_proxy(url,
  identity_resolver, *, baggage_key="userId")` builds a per-request
  `client_factory` that resolves the caller (consumer-supplied resolver,
  e.g. from the OAuth context) and stamps `baggage: <key>=<value>` on the
  upstream call. Value is percent-encoded.
- **Upstream**: parse the `baggage` header for its configured key,
  percent-decode, fall back to an env default if absent.

## References

- W3C Baggage: https://www.w3.org/TR/baggage/
- OpenTelemetry Baggage / context propagation
- RFC 6648 (deprecating new `X-` headers)
- MCP auth = OAuth 2.1 (the Option-A path, for untrusted boundaries)

## Addendum 2026-11-01: `as: signed`, for upstreams that serve many people

Baggage is a claim the upstream takes on trust, which is fine when the
upstream is reachable only through the gateway. Per-person wss-shim
sidecars (mneme plan 057) are not always: `mcp.danenberg.ai` publishes some
sidecar `/mcp` routes behind one shared static bearer, and a sidecar that
routes each call to the *caller's own* device must not let a bearer-holder
name the caller. So a third carrier:

```json
"forward_identity": { "as": "signed", "audience": "chrome", "key_env": "WSS_SHIM_ASSERTION_KEY" }
```

Per call, the gateway sends `X-Wss-Shim-Identity: <HS256 JWT {email, aud,
iat, exp}>`, 60 s, keyed with `$WSS_SHIM_ASSERTION_KEY`, audience defaulting
to the server entry's name. The address is raw (no `sanitize`; the loader
refuses one) because the shim matches it against the addresses in each
device token. HS256 is enough on this hop because gateway and sidecars run
in one container: the entrypoint generates the key at boot and hands it to
both, so it is never stored or copied. The gateway refuses to build a
signed server without its key. Contract and end-to-end check:
`tests/manual/wss_shim_signed_identity_e2e.py`.
