"""Gateway-level FastMCP middleware."""

from mcp_gateway.middleware.image_downscale import ImageDownscaleMiddleware
from mcp_gateway.middleware.stub_schemas import StubSchemaMiddleware

__all__ = ["ImageDownscaleMiddleware", "StubSchemaMiddleware"]
