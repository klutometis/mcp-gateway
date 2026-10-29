"""R2 attachment offload: binary tool results become signed capability URLs.

See ``plans/messaging-attachments-r2.md`` (the design) and
``plans/gateway-middleware.md`` (the mechanism) in the deployment repo.

Wiring, in ``create_gateway``::

    service = AttachmentService.from_env()      # None unless enabled
    if service is not None:
        gateway.add_middleware(AttachmentOffloadMiddleware(service))
        register_attachment_tools(gateway, service)
        register_artifact_route(gateway, service)
"""

from mcp_gateway.attachments.config import AttachmentConfig, Rule, load_config
from mcp_gateway.attachments.middleware import AttachmentOffloadMiddleware
from mcp_gateway.attachments.routes import register_artifact_route
from mcp_gateway.attachments.service import AttachmentService, Envelope
from mcp_gateway.attachments.store import (
    Artifact,
    FileStore,
    R2Store,
    StoreError,
    build_store,
)
from mcp_gateway.attachments.tools import register_attachment_tools

__all__ = [
    "Artifact",
    "AttachmentConfig",
    "AttachmentOffloadMiddleware",
    "AttachmentService",
    "Envelope",
    "FileStore",
    "R2Store",
    "Rule",
    "StoreError",
    "build_store",
    "load_config",
    "register_artifact_route",
    "register_attachment_tools",
]
