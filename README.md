# mcp-gateway

A generic **MCP gateway**: a single FastMCP endpoint that aggregates many
upstream MCP servers, driven entirely by a `servers.json` config. The
machinery is deployment-agnostic — bring your own config and auth.

Extracted as the reusable core of a personal MCP gateway so it can be
consumed as a package (each consumer provides its own `servers.json` +
auth), rather than forked.

## What it does

- **Config-driven aggregation** — declare upstreams in `servers.json`
  (stdio or streamable-http); the gateway mounts each as a proxy and
  exposes one flat tool surface.
- **Tool subset / rename** — expose only the tools you want, renamed to a
  clean cross-server vocabulary (or `"*"` to pass through).
- **Multi-instance routing** — one logical server, N backends keyed by an
  injected `account`/`host`/`instance` param (e.g. personal+work, or
  laptop+desktop+vm). Enum-in-schema, fail-at-call-time error enrichment.
- **Middleware** — e.g. image downscaling on outbound content blocks.
- **Pluggable auth** — single-user (bearer/OAuth) today; a multi-tenant
  provider (per-user identity forwarded to each upstream) is the next
  milestone.

## Config

Set `MCP_SERVERS_FILE` to a `servers.json`. Each entry declares a
transport + tools; an `instances` block turns it multi-instance.

## Run

```bash
gateway-local     # stdio gateway (local clients)
gateway-remote    # streamable-http gateway (behind a reverse proxy)
```

## Consuming as a package

```
mcp-gateway @ git+https://github.com/klutometis/mcp-gateway.git
```

The consumer owns its `servers.json` and auth config; this package never
imports consumer code.

## Status

Phase 0 extraction (generic machinery + tests). Multi-tenant auth provider
is the next milestone — see the extraction plan in the personal gateway
repo.
