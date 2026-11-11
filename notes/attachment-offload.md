# Attachment offload: bytes out of the context window

**Status:** implemented 2026-10-29 (`src/mcp_gateway/attachments/`).
Design and rationale live in the deployment repo:
`mcp-gateway-config/plans/messaging-attachments-r2.md` (what and why) and
`plans/gateway-middleware.md` (the response-side mechanism this is the
canonical instance of). This note is the operator's half: what the code
does, what the knobs are, and how to check it.

## What it does

A tool result that carries bytes — an MCP `image` block, a `resource`
blob, base64 inside a JSON field, or a bare base64 string — is uploaded
to object storage and replaced with a ~170-token envelope:

```
[attachment offloaded: 57,925 bytes of image/jpeg, not inlined]
{"url": "http://127.0.0.1:3100/artifacts/09d7…c530.jpg?cap=eyJleHAi…",
 "mime": "image/jpeg", "size": 57925, "filename": "09d72a863bc8.jpg",
 "sha256": "09d7…c530", "expires_at": "2026-10-30T05:34:04Z"}
view_image(url) to see it; ask_about_attachment(url, question) to ask
about it; the url fetches as-is (signed, expiring, no auth header).
```

Measured on a real WhatsApp photo: **77,236 base64 chars (~19,300
tokens) → 699 chars (~174 tokens)**, 110×. On a 1 MB image, 2,091×.

## The pieces

| file | what |
|---|---|
| `attachments/config.py` | match rules + thresholds, from JSON/env. Secrets by env-var **name**. |
| `attachments/payload.py` | finds payloads; magic-number MIME sniffing; filename guessing. |
| `attachments/cap.py` | HMAC-SHA256 signed capability tokens (sign, verify, expire). |
| `attachments/store.py` | `R2Store` (Cloudflare REST object API) and `FileStore` (dev/tests). |
| `attachments/service.py` | sha256 dedup, upload, envelope + URL minting, fetch-back. |
| `attachments/middleware.py` | the response-side `on_call_tool` handler. |
| `attachments/routes.py` | `GET /artifacts/{id}?cap=…` on the gateway's own app. |
| `attachments/tools.py` | `view_image`, `ask_about_attachment`. |

## Three decisions worth knowing

**Capability URLs, not R2 presigned URLs.** The URL points at the
*gateway*, carries an HMAC cap, and is verified on every fetch. It works
headless (`curl`, no header, no browser session — the point of
`plans/terminal-auth-device-flow.md`), expiry is ours to enforce rather
than R2's to maybe-enforce, and the bucket stays private with no public
domain. Cost: the bytes flow through the gateway on the way out, and the
URL is only reachable where the gateway is.

**Small images stay inline.** Below `inline_image_max_bytes` an image
block is left alone: ~1.5k tokens and the model can *see* it, versus ~170
tokens plus a second tool call it may not make. Everything else over
`min_bytes` is offloaded.

**Failure withholds the bytes.** If storage breaks, the default
(`on_error: "truncate"`) returns an error envelope, not the base64 —
failing open would reintroduce the exact bug this exists to prevent.
`on_error: "passthrough"` restores the old behaviour per rule.

## Ordering against the image-downscale middleware

Registered **after** `ImageDownscaleMiddleware`, which (FastMCP nests
middleware: last added is innermost) means it runs **first** on the way
out. So offload sees the upstream's original bytes and stores those —
full resolution in R2, which is what `view_image` downsamples from —
while whatever it leaves inline still passes through the downscaler.
This one decides what the model gets at all; that one caps what remains.

## Tools that are only safe with offload: `"requires"`

A tool whose *only* output is bytes (WhatsApp's `download_media`) is a
liability on a host without offload, and `deploy/common/servers.json` is
shared between hosts that have it and hosts that do not. So a tool-map entry
may say

```json
"download_media": {"name": "whatsapp_download_media", "requires": "attachment_offload"}
```

and `load_servers` drops it unless `load_config().enabled` — the same
predicate `create_gateway` uses to install the middleware, so the tool and
the middleware cannot disagree. An unknown capability hides the tool (fail
closed). Tested in `tests/test_tool_requires.py`. Tools that return bytes
*sometimes* (LinkedIn, iMessage, Roblox thumbnails) were exposed before
offload existed and are not gated; gating them would change hosts that
never opted in.

## Configuration

Off unless `MCP_ATTACHMENT_OFFLOAD=1`. Rules and thresholds come from
the JSON file named by `MCP_ATTACHMENT_CONFIG` (see
`mcp-gateway-config/deploy/common/attachments.json`); without one, the
built-in defaults cover `*.download_media`, `*.fetch_attachment`,
`*.imessage_get_attachment`, `*.get_asset_thumbnail`.

| env | default | |
|---|---|---|
| `MCP_ATTACHMENT_OFFLOAD` | `0` | master switch |
| `MCP_ATTACHMENT_CONFIG` | — | rules file |
| `MCP_ATTACHMENT_BACKEND` | `r2` | or `file` |
| `MCP_ATTACHMENT_BUCKET` | `mcp-attachments` | |
| `MCP_ATTACHMENT_R2_ACCESS_KEY_ID` | — | S3 key pair; when both are set the store speaks S3/SigV4 — **required for a bucket-scoped token**, which the REST API refuses |
| `MCP_ATTACHMENT_R2_SECRET_ACCESS_KEY` | — | |
| `MCP_ATTACHMENT_R2_TOKEN` | — | Cloudflare API bearer token; REST fallback when no key pair (needs account-level R2) |
| `CLOUDFLARE_ACCOUNT_ID` | — | |
| `MCP_ATTACHMENT_SIGNING_KEY` | random per boot | **set it**, or URLs die at restart |
| `MCP_ATTACHMENT_BASE_URL` | `http://127.0.0.1:$MCP_PORT` | what goes in the URL |
| `MCP_ATTACHMENT_TTL` | `86400` | cap lifetime, seconds |
| `MCP_ATTACHMENT_MIN_BYTES` | `51200` | |
| `MCP_ATTACHMENT_INLINE_IMAGE_MAX_BYTES` | `51200` | |
| `MCP_ATTACHMENT_MAX_BYTES` | `104857600` | refuse above this |
| `MCP_ATTACHMENT_ON_ERROR` | `truncate` | or `passthrough` |
| `MCP_ATTACHMENT_VIEW_MAX_DIM` | `1024` | `view_image` default |
| `MCP_ATTACHMENT_ASK` | `1` | `ask_about_attachment` on/off |
| `MCP_ATTACHMENT_ASK_MODEL` | `gemini-2.5-flash` | needs `GEMINI_API_KEY` |

A pattern is a glob over the canonical `server.verb` name, and
`*.verb` also matches the flattened `prefix_verb` this gateway renames
to. Adding Gmail is one line in `match`.

## Checking it

```bash
uv run pytest tests/test_attachment_*.py          # 59 unit tests
# end to end, against a second gateway on :3199 (see the script's docstring)
uv run python tests/manual/attachment_e2e.py http://127.0.0.1:3199/mcp
```

The e2e prints a before/after table per payload shape and verifies the
far end: it fetches each signed URL with no auth header and compares
sha256 against the source bytes, checks that a capless URL is 401, that
`view_image` returns a real image block, that a small image stays
inline, and that an unmatched tool is untouched.
