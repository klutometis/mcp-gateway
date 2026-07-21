"""Shared gateway construction: server configs, tool transforms, OAuth provider.

Both local and remote entrypoints call ``create_gateway()`` to get a
fully-configured :class:`FastMCP` instance with all upstream servers mounted.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastmcp import FastMCP
from fastmcp.server.auth.oauth_proxy.models import JTIMapping
from fastmcp.server.auth.oauth_proxy.proxy import (
    DEFAULT_ACCESS_TOKEN_EXPIRY_SECONDS,
    HTTP_TIMEOUT_SECONDS,
)
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.transforms.tool_transform import ToolTransform
from fastmcp.tools.tool_transform import ToolTransformConfig
from mcp.server.auth.provider import RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OAuth provider
# ---------------------------------------------------------------------------


class StableRefreshGoogleProvider(GoogleProvider):
    """GoogleProvider that disables refresh token rotation + heavy diagnostics.

    FastMCP's default OAuthProxy.exchange_refresh_token enforces one-time-use
    refresh token rotation: each refresh issues a new refresh token and
    invalidates the old one. This causes systematic failures when multiple
    concurrent clients (e.g., multiple TypingMind tabs, devices, or MCP
    connections) all try to refresh the same token simultaneously -- the
    first request succeeds and rotates the token, while all others receive
    400 invalid_grant because the old token was deleted.

    This subclass overrides exchange_refresh_token to reuse the existing
    refresh token instead of rotating it. The upstream Google token refresh
    and new FastMCP access token issuance are unchanged.

    It ALSO overrides ``get_client`` to log every DCR client lookup
    (HIT / MISS / EXCEPTION). Production logs show clients are still
    getting forced through full re-OAuth approximately hourly; the 401 on
    ``POST /token`` comes from ``ClientAuthenticator.authenticate_request``
    (i.e. *client* credential check), NOT from our refresh logic, so the
    fix must be diagnosed at the client-store layer.

    See:
      - plans/fix-refresh-token-rotation-race.md  (Feb 2026, partial fix)
      - plans/refresh-token-stickiness-take-2.md  (May 2026, this work)
    """

    async def get_client(self, client_id: str):  # type: ignore[override]
        """Log every DCR client lookup so we can see why /token returns 401.

        The /token handler in `mcp.server.auth.handlers.token` calls
        `ClientAuthenticator.authenticate_request`, which calls
        `provider.get_client(client_id)`. If that returns None it raises
        ``AuthenticationError("Invalid client_id")`` -> 401. Logging the
        result here tells us whether the storm is from storage misses
        vs. genuine client_secret mismatches.
        """
        try:
            client = await super().get_client(client_id)
        except Exception as exc:
            logger.warning(
                "get_client(client_id=%s) raised %s: %s",
                client_id[:8] if client_id else "<empty>",
                type(exc).__name__,
                exc,
            )
            raise
        if client is None:
            logger.warning(
                "get_client(client_id=%s) -> MISS (storage returned None)",
                client_id[:8] if client_id else "<empty>",
            )
        else:
            logger.info(
                "get_client(client_id=%s) -> HIT (auth_method=%s, has_secret=%s)",
                client_id[:8],
                getattr(client, "token_endpoint_auth_method", "?"),
                bool(getattr(client, "client_secret", None)),
            )
        return client

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Exchange refresh token for new access token WITHOUT rotation.

        Identical to OAuthProxy.exchange_refresh_token except:
        - Does NOT issue a new refresh token
        - Does NOT delete the old refresh token JTI mapping
        - Does NOT delete the old refresh token metadata
        - Returns the original refresh token unchanged in the response

        Logs every branch at INFO so we can see in production whether
        the refresh path is even being entered (the 401s we see on
        /token look like they come from client auth, not from here).
        """
        logger.info(
            "exchange_refresh_token: ENTER client_id=%s scopes=%s",
            client.client_id[:8] if client.client_id else "<empty>",
            scopes,
        )

        # 1. Verify FastMCP refresh token JWT.
        #
        # IMPORTANT: pass expected_token_use="refresh". The default is
        # "access", which rejects refresh tokens with
        # ``JoseError: Token type mismatch: expected access, got refresh``
        # -> every refresh attempt fails -> the client gives up and
        # re-OAuths. This was the root cause of the "have to re-auth every
        # hour" symptom (2026-05-19).
        #
        # See: plans/refresh-token-stickiness-take-2.md
        try:
            refresh_payload = self.jwt_issuer.verify_token(
                refresh_token.token, expected_token_use="refresh"
            )
            refresh_jti = refresh_payload["jti"]
            logger.info(
                "exchange_refresh_token: JWT VERIFIED jti=%s", refresh_jti[:8]
            )
        except Exception as e:
            logger.warning(
                "exchange_refresh_token: JWT VERIFY FAILED: %s: %s",
                type(e).__name__,
                e,
            )
            raise TokenError("invalid_grant", "Invalid refresh token") from e

        # 2. Look up upstream token via JTI mapping
        jti_mapping = await self._jti_mapping_store.get(key=refresh_jti)
        if not jti_mapping:
            logger.warning(
                "exchange_refresh_token: JTI MAPPING MISS jti=%s", refresh_jti[:8]
            )
            raise TokenError("invalid_grant", "Refresh token mapping not found")
        logger.info(
            "exchange_refresh_token: JTI MAPPING HIT jti=%s -> upstream=%s",
            refresh_jti[:8],
            jti_mapping.upstream_token_id[:8],
        )

        upstream_token_set = await self._upstream_token_store.get(
            key=jti_mapping.upstream_token_id
        )
        if not upstream_token_set:
            logger.warning(
                "exchange_refresh_token: UPSTREAM TOKEN MISS upstream_id=%s",
                jti_mapping.upstream_token_id[:8],
            )
            raise TokenError("invalid_grant", "Upstream token not found")
        logger.info(
            "exchange_refresh_token: UPSTREAM TOKEN HIT has_refresh=%s expires_at=%s",
            bool(upstream_token_set.refresh_token),
            upstream_token_set.expires_at,
        )

        if not upstream_token_set.refresh_token:
            logger.warning(
                "exchange_refresh_token: NO UPSTREAM REFRESH TOKEN stored"
            )
            raise TokenError("invalid_grant", "Refresh not supported for this token")

        # 3. Refresh upstream Google token
        oauth_client = AsyncOAuth2Client(
            client_id=self._upstream_client_id,
            client_secret=self._upstream_client_secret.get_secret_value(),
            token_endpoint_auth_method=self._token_endpoint_auth_method,
            timeout=HTTP_TIMEOUT_SECONDS,
        )

        upstream_scopes = self._prepare_scopes_for_upstream_refresh(scopes)

        try:
            logger.info(
                "exchange_refresh_token: CALLING GOOGLE jti=%s", refresh_jti[:8]
            )
            token_response: dict[str, Any] = await oauth_client.refresh_token(
                url=self._upstream_token_endpoint,
                refresh_token=upstream_token_set.refresh_token,
                scope=" ".join(upstream_scopes) if upstream_scopes else None,
                **self._extra_token_params,
            )
            logger.info(
                "exchange_refresh_token: GOOGLE OK new_refresh=%s",
                bool(token_response.get("refresh_token")),
            )
        except Exception as e:
            logger.warning(
                "exchange_refresh_token: GOOGLE FAILED: %s: %s",
                type(e).__name__,
                e,
            )
            raise TokenError("invalid_grant", f"Upstream refresh failed: {e}") from e

        # 4. Update stored upstream token
        if "expires_in" in token_response:
            new_expires_in = int(token_response["expires_in"])
        elif self._fallback_access_token_expiry_seconds is not None:
            new_expires_in = self._fallback_access_token_expiry_seconds
        else:
            new_expires_in = DEFAULT_ACCESS_TOKEN_EXPIRY_SECONDS

        upstream_token_set.access_token = token_response["access_token"]
        upstream_token_set.expires_at = time.time() + new_expires_in

        # Handle upstream refresh token rotation (Google may rotate its own)
        new_refresh_expires_in = None
        if new_upstream_refresh := token_response.get("refresh_token"):
            if new_upstream_refresh != upstream_token_set.refresh_token:
                upstream_token_set.refresh_token = new_upstream_refresh
                logger.debug("Upstream refresh token rotated by Google")
            if "refresh_expires_in" in token_response:
                new_refresh_expires_in = int(token_response["refresh_expires_in"])
                upstream_token_set.refresh_token_expires_at = (
                    time.time() + new_refresh_expires_in
                )
            elif upstream_token_set.refresh_token_expires_at:
                new_refresh_expires_in = int(
                    upstream_token_set.refresh_token_expires_at - time.time()
                )
            else:
                new_refresh_expires_in = 60 * 60 * 24 * 30  # 30 days
                upstream_token_set.refresh_token_expires_at = (
                    time.time() + new_refresh_expires_in
                )

        upstream_token_set.raw_token_data = {
            **upstream_token_set.raw_token_data,
            **token_response,
        }
        refresh_ttl = new_refresh_expires_in or (
            int(upstream_token_set.refresh_token_expires_at - time.time())
            if upstream_token_set.refresh_token_expires_at
            else 60 * 60 * 24 * 30
        )
        await self._upstream_token_store.put(
            key=upstream_token_set.upstream_token_id,
            value=upstream_token_set,
            ttl=max(refresh_ttl, new_expires_in, 1),
        )

        # Re-extract upstream claims from refreshed token response
        upstream_claims = await self._extract_upstream_claims(
            upstream_token_set.raw_token_data
        )

        # 5. Issue new FastMCP access token (always fresh, new JTI)
        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required")

        new_access_jti = secrets.token_urlsafe(32)
        new_fastmcp_access = self.jwt_issuer.issue_access_token(
            client_id=client.client_id,
            scopes=scopes,
            jti=new_access_jti,
            expires_in=new_expires_in,
            upstream_claims=upstream_claims,
        )

        await self._jti_mapping_store.put(
            key=new_access_jti,
            value=JTIMapping(
                jti=new_access_jti,
                upstream_token_id=upstream_token_set.upstream_token_id,
                created_at=time.time(),
            ),
            ttl=new_expires_in,
        )

        # 6. NO ROTATION: reuse the existing refresh token.
        # Do NOT issue a new refresh token, do NOT delete the old JTI mapping,
        # do NOT delete the old refresh token metadata. This allows multiple
        # concurrent clients to refresh without invalidating each other.
        logger.info(
            "Issued new FastMCP access token (stable refresh, no rotation) for "
            "client=%s (access_jti=%s, refresh_jti=%s unchanged)",
            client.client_id,
            new_access_jti[:8],
            refresh_jti[:8],
        )

        return OAuthToken(
            access_token=new_fastmcp_access,
            token_type="Bearer",
            expires_in=new_expires_in,
            refresh_token=refresh_token.token,  # Same refresh token, not rotated
            scope=" ".join(scopes),
        )


# ---------------------------------------------------------------------------
# Server configs & tool transforms
# ---------------------------------------------------------------------------

# MCP server definitions -- each mounted as an independent proxy with
# its own persistent stdio subprocess (keep_alive=True by default).
#
# Tool subsetting & renaming: each server declares which tools to
# expose and how to rename/redescribe them. This keeps the tool list
# minimal and the names clean (no prefixes like zep_, firecrawl_, etc.).
# See plans/tool-subsetting-renaming.md for rationale.

def load_servers(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Load server configuration from a JSON file.

    Each entry's "tools" mapping is materialized into
    :class:`ToolTransformConfig` instances.

    Parameters
    ----------
    path:
        Path to the servers JSON file. If "None", reads the
        "MCP_SERVERS_FILE" environment variable, which is required.

    Returns
    -------
    dict[str, dict[str, Any]]
        Server configurations keyed by server name.

    Raises
    ------
    RuntimeError
        If neither ``path`` nor ``MCP_SERVERS_FILE`` is set.
    """
    if path is None:
        env_path = os.environ.get('MCP_SERVERS_FILE')
        if not env_path:
            raise RuntimeError(
                "MCP_SERVERS_FILE env var not set and no path passed. "
                "Each deployment owns its servers.json; see deploy/*/README.md."
            )
        path = Path(env_path)
    path = Path(path)

    with path.open() as f:
        raw = json.load(f)

    servers: dict[str, dict[str, Any]] = {}
    for name, cfg in raw.items():
        transport = cfg.get('transport', 'stdio')
        # A server may set "tools": "*" to expose ALL upstream tools
        # verbatim (no subsetting, no rename). Otherwise "tools" is a
        # mapping of upstream-name -> {name,title,description} and only
        # the listed tools are exposed. See plans/tool-subsetting-renaming.md.
        raw_tools = cfg.get('tools', {})
        tools: Any
        if raw_tools == '*':
            tools = '*'
        else:
            tools = {
                upstream: ToolTransformConfig(
                    name=tc['name'],
                    title=tc.get('title'),
                    description=tc.get('description'),
                )
                for upstream, tc in raw_tools.items()
            }
        entry: dict[str, Any] = {
            'transport': transport,
            'tools': tools,
        }
        if transport == 'stdio':
            entry['command'] = cfg['command']
            entry['args'] = list(cfg.get('args', []))
            entry['env_keys'] = list(cfg.get('env_keys', []))
        elif transport in ('http', 'streamable-http', 'sse'):
            entry['url'] = cfg.get('url')
            entry['headers'] = dict(cfg.get('headers', {}))
        else:
            raise ValueError(
                f"server '{name}': unknown transport {transport!r}; "
                "expected 'stdio', 'http', 'streamable-http', or 'sse'"
            )

        # Multi-instance support. See plans/multi-instance-backends.md.
        # An `instances` block turns the server into a MultiInstanceProxy
        # at create_gateway() time: one backing proxy per instance,
        # routed by an injected `param_name` argument on every tool.
        if 'instances' in cfg:
            entry['instances'] = _parse_instances(
                name=name,
                transport=transport,
                instances_cfg=cfg['instances'],
            )
            entry['param_name'] = cfg.get('param_name', 'instance')
            # For multi-instance HTTP, per-instance URLs override the
            # top-level url (which may be absent entirely).
            if transport != 'stdio' and entry['url'] is None and not entry['instances']:
                raise ValueError(
                    f"server '{name}': transport={transport} requires either "
                    "a top-level 'url' or an 'instances' block with per-"
                    "instance urls"
                )
        elif transport != 'stdio' and entry['url'] is None:
            raise ValueError(
                f"server '{name}': transport={transport} requires 'url'"
            )

        servers[name] = entry
    return servers


def _parse_instances(
    *,
    name: str,
    transport: str,
    instances_cfg: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Validate + normalize the `instances` block of a server config.

    For stdio: each instance may declare an ``env`` mapping of
    subprocess-env-var-name -> host-env-var-name (read from
    ``os.environ``). Falls back to top-level ``env_keys`` if absent
    (i.e. all instances share the same env, which is rare but valid).

    For http/streamable-http/sse: each instance must declare a
    ``url``, optionally ``headers``.
    """
    if not instances_cfg:
        raise ValueError(
            f"server '{name}': 'instances' block is empty; declare at "
            "least one instance or remove the block"
        )
    parsed: dict[str, dict[str, Any]] = {}
    for inst_name, inst_cfg in instances_cfg.items():
        if transport == 'stdio':
            # env is a dict: { SUBPROCESS_ENV_VAR: HOST_ENV_VAR_TO_READ }
            env_map = dict(inst_cfg.get('env', {}))
            env_keys = list(inst_cfg.get('env_keys', []))
            # args control: an instance may fully override the shared
            # top-level args (`args`) or append to them (`args_append`).
            # Lets instances that differ only by a CLI flag (e.g.
            # chrome-devtools-mcp --browserUrl=...) share one command
            # without a shell wrapper. See plans/laptop-corp-local-stack.md.
            if 'args' in inst_cfg and 'args_append' in inst_cfg:
                raise ValueError(
                    f"server '{name}', instance '{inst_name}': set only one "
                    "of 'args' (override) or 'args_append' (append), not both"
                )
            parsed[inst_name] = {
                'env_map': env_map,
                'env_keys': env_keys,
                'args': list(inst_cfg['args']) if 'args' in inst_cfg else None,
                'args_append': list(inst_cfg.get('args_append', [])),
            }
        else:
            if 'url' not in inst_cfg:
                raise ValueError(
                    f"server '{name}', instance '{inst_name}': missing 'url'"
                )
            parsed[inst_name] = {
                'url': inst_cfg['url'],
                'headers': dict(inst_cfg.get('headers', {})),
            }
    return parsed


def create_gateway(
    auth: GoogleProvider | None = None,
    servers: dict[str, dict[str, Any]] | None = None,
) -> FastMCP:
    """Build the gateway with all upstream MCP servers mounted.

    Parameters
    ----------
    auth:
        An OAuth provider for remote mode, or ``None`` for local mode.
    servers:
        Server configuration dict (as produced by :func:`load_servers`).
        If ``None``, calls :func:`load_servers` which reads
        ``MCP_SERVERS_FILE``.

    Returns
    -------
    FastMCP
        A fully-configured gateway ready to be served.
    """
    from fastmcp.server import create_proxy

    if servers is None:
        servers = load_servers()

    gateway = FastMCP("MCP Gateway", auth=auth) if auth else FastMCP("MCP Gateway")

    # Pre-emptively downscale image content blocks (screenshots, attachments)
    # on the way out. Generic over the MCP `image` type; no per-server config.
    # Tuned via MCP_IMAGE_* env. See src/mcp_gateway/middleware/image_downscale.py
    # and plans/visual-context-management.md.
    from mcp_gateway.middleware import ImageDownscaleMiddleware

    _img_mw = ImageDownscaleMiddleware.from_env()
    if _img_mw is not None:
        gateway.add_middleware(_img_mw)

    # Track proxies needing warm-up on startup. FastMCP's stdio transport is
    # lazy: subprocesses don't spawn until first tool call. For wss-shim-backed
    # entries that's a chicken-and-egg — the bridge can't dial in until the
    # shim listens, and the shim doesn't listen until something forces a
    # connection. We trigger that connection at boot via list_tools().
    # See plans/wss-shim-lifecycle.md.
    warmup_proxies: list[tuple[str, FastMCP]] = []

    for name, server_config in servers.items():
        tool_configs = server_config["tools"]
        transport = server_config["transport"]

        if "instances" in server_config:
            # Multi-instance: build one backing proxy per instance,
            # wrap them in a MultiInstanceProxy. See
            # plans/multi-instance-backends.md.
            mounted = _build_multi_instance(
                name=name,
                server_config=server_config,
                tool_configs=tool_configs,
                create_proxy=create_proxy,
            )
        else:
            mounted = _build_single_instance(
                name=name,
                server_config=server_config,
                tool_configs=tool_configs,
                create_proxy=create_proxy,
            )

        gateway.mount(mounted)
        warmup_proxies.append((name, mounted))

    # Stash on the gateway for entrypoint code to schedule as a startup hook.
    gateway._warmup_proxies = warmup_proxies  # type: ignore[attr-defined]

    return gateway


# ---------------------------------------------------------------------------
# Per-server builders (called from create_gateway)
# ---------------------------------------------------------------------------


def _proxy_config_for_stdio(
    server_config: dict[str, Any],
    *,
    instance_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a FastMCP proxy config dict for one stdio backing.

    Top-level ``env_keys`` always pass through (each one read from
    os.environ verbatim). Per-instance ``env_map`` adds entries with
    renamed-from-host keys (e.g. subprocess GMAIL_OAUTH_PATH reads host
    GMAIL_OAUTH_PATH_PERSONAL). Per-instance ``env_keys`` are also
    passed through with original names.
    """
    env: dict[str, Any] = {
        k: os.environ.get(k) for k in server_config.get("env_keys", [])
    }
    if instance_overrides:
        for sub_key in instance_overrides.get("env_keys", []):
            env[sub_key] = os.environ.get(sub_key)
        for sub_key, host_key in instance_overrides.get("env_map", {}).items():
            env[sub_key] = os.environ.get(host_key)
    args = list(server_config["args"])
    if instance_overrides:
        if instance_overrides.get("args") is not None:
            args = list(instance_overrides["args"])
        else:
            args = args + list(instance_overrides.get("args_append", []))
    return {
        "command": server_config["command"],
        "args": args,
        "transport": "stdio",
        "env": env,
    }


def _expand_env(value: str) -> str:
    """Interpolate ``${VAR}`` references in a string from ``os.environ``.

    Lets ``servers.json`` reference secrets (e.g. upstream bearer tokens)
    by name instead of hardcoding them in the committed config. An unset
    variable expands to empty string (and is logged).
    """
    import re

    def repl(m: re.Match[str]) -> str:
        var = m.group(1)
        val = os.environ.get(var)
        if val is None:
            logger.warning("servers.json header references unset env var %s", var)
            return ""
        return val

    return re.sub(r"\$\{([A-Z0-9_]+)\}", repl, value)


def _expand_headers(headers: dict[str, Any]) -> dict[str, Any]:
    """Apply ``_expand_env`` to each header value."""
    return {
        k: _expand_env(v) if isinstance(v, str) else v
        for k, v in headers.items()
    }


def _proxy_config_for_http(
    server_config: dict[str, Any],
    *,
    instance_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a FastMCP proxy config dict for one HTTP backing.

    Per-instance ``url`` and ``headers`` win over the top-level ones.
    Header values support ``${VAR}`` env interpolation so upstream
    bearer tokens stay out of the committed config.
    """
    if instance_overrides and "url" in instance_overrides:
        url = instance_overrides["url"]
        headers = instance_overrides.get("headers", {})
    else:
        url = server_config["url"]
        headers = server_config.get("headers", {})
    return {
        # Expand ${VAR} in the URL too (not just headers) so the host can
        # come from env (e.g. a Railway-hosted upstream). Forgetting this
        # made the gateway try to resolve the literal "${WHATSAPP_MCP_HOST}"
        # and silently drop the server on DNS failure.
        "url": _expand_env(url) if isinstance(url, str) else url,
        "transport": server_config["transport"],
        "headers": _expand_headers(headers),
    }


def _build_single_instance(
    *,
    name: str,
    server_config: dict[str, Any],
    tool_configs: dict[str, ToolTransformConfig],
    create_proxy,
) -> FastMCP:
    """Build a regular (pre-multi-instance) proxy. Behavior unchanged."""
    transport = server_config["transport"]
    if transport == "stdio":
        proxy_config = _proxy_config_for_stdio(server_config)
    else:
        proxy_config = _proxy_config_for_http(server_config)

    proxy = create_proxy(
        {"mcpServers": {"default": proxy_config}},
        name=f"Proxy-{name}",
    )
    # "*" means expose every upstream tool as-is: skip the enable(only=True)
    # subset filter and the rename transform.
    if tool_configs != "*":
        proxy.enable(
            names=set(tool_configs.keys()),
            components={"tool"},
            only=True,
        )
        proxy.add_transform(ToolTransform(tool_configs))
    return proxy


def _build_multi_instance(
    *,
    name: str,
    server_config: dict[str, Any],
    tool_configs: dict[str, ToolTransformConfig],
    create_proxy,
) -> FastMCP:
    """Build N backing proxies and wrap them in a MultiInstanceProxy.

    Each backing gets the same filter+rename treatment as the single-
    instance path. The wrapper aggregates them, injects the instance
    selector param into every tool, and routes call_tool by name.
    """
    from mcp_gateway.multi_instance import MultiInstanceProxy

    transport = server_config["transport"]
    instances_cfg = server_config["instances"]
    param_name = server_config["param_name"]

    backings: dict[str, FastMCP] = {}
    for inst_name, inst_cfg in instances_cfg.items():
        if transport == "stdio":
            proxy_config = _proxy_config_for_stdio(
                server_config, instance_overrides=inst_cfg
            )
        else:
            proxy_config = _proxy_config_for_http(
                server_config, instance_overrides=inst_cfg
            )
        backing = create_proxy(
            {"mcpServers": {"default": proxy_config}},
            name=f"Proxy-{name}-{inst_name}",
        )
        # "*" exposes every upstream tool as-is (no subset/rename).
        if tool_configs != "*":
            backing.enable(
                names=set(tool_configs.keys()),
                components={"tool"},
                only=True,
            )
            backing.add_transform(ToolTransform(tool_configs))
        backings[inst_name] = backing

    logger.info(
        "server %r: multi-instance with %d instances (%s), param_name=%r",
        name, len(backings), ", ".join(backings.keys()), param_name,
    )
    return MultiInstanceProxy(
        name=f"MultiInstance-{name}",
        instances=backings,
        param_name=param_name,
    )


async def warm_proxies(gateway: FastMCP) -> None:
    """Force each mounted proxy to connect to its upstream subprocess.

    Run this once during application startup (e.g. via Starlette's
    ``on_startup`` event). For wss-shim-backed proxies this causes the
    shim subprocess to spawn and begin listening on its WSS port, so
    that the corresponding device-side bridge can dial in. For ordinary
    stdio proxies it's a harmless pre-warm that avoids a one-time
    cold-start latency on the first tool call.

    Errors are logged but not raised — a single upstream failing to
    warm shouldn't prevent the gateway from serving the others.
    """
    proxies = getattr(gateway, "_warmup_proxies", [])
    if not proxies:
        return
    print(f"Warming {len(proxies)} upstream proxies (forcing stdio subprocess spawn)")
    for name, proxy in proxies:
        try:
            tools = await proxy.list_tools()
            print(f"  warmed '{name}': {len(tools)} tools available")
        except Exception as e:
            print(
                f"  WARNING: warm-up of '{name}' failed: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
