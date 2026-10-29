"""``GET /artifacts/{artifact_id}?cap=…`` — the fetch half of the capability.

Mounted on the gateway's own HTTP app, so it is reachable wherever the
gateway is (``http://127.0.0.1:3100`` locally). No session, no
Authorization header: the signed cap in the query string *is* the
authorization, which is what makes `curl` from a headless terminal work
(``plans/terminal-auth-device-flow.md`` §5).
"""

from __future__ import annotations

import logging

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp_gateway.attachments.cap import CapError
from mcp_gateway.attachments.service import AttachmentService
from mcp_gateway.attachments.store import StoreError

logger = logging.getLogger(__name__)

__all__ = ["register_artifact_route"]


def register_artifact_route(gateway: FastMCP, service: AttachmentService) -> None:
    path = f"{service.config.route_prefix.rstrip('/')}/{{artifact_id}}"

    @gateway.custom_route(path, methods=["GET", "HEAD"], name="artifact_fetch")
    async def fetch_artifact(request: Request) -> Response:
        artifact_id = request.path_params["artifact_id"]
        cap = request.query_params.get("cap", "")
        if not cap:
            return JSONResponse({"error": "missing cap"}, status_code=401)
        try:
            service.verify(artifact_id, cap)
        except CapError as exc:
            logger.info("artifact fetch denied for %s: %s", artifact_id, exc)
            return JSONResponse({"error": str(exc)}, status_code=403)
        try:
            artifact = await service.store.get(service.storage_key(artifact_id))
        except StoreError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        headers = {
            "Content-Disposition": f'inline; filename="{artifact.filename}"',
            "Cache-Control": "private, max-age=3600",
            "X-Artifact-Id": artifact_id,
        }
        if request.method == "HEAD":
            headers["Content-Length"] = str(artifact.size)
            return Response(status_code=200, media_type=artifact.mime, headers=headers)
        return Response(artifact.data, media_type=artifact.mime, headers=headers)
