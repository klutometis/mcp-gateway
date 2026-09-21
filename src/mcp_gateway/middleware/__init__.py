"""Gateway-level FastMCP middleware."""

from mcp_gateway.middleware.image_downscale import ImageDownscaleMiddleware
from mcp_gateway.middleware.stub_schemas import StubSchemaMiddleware
from mcp_gateway.middleware.summarize import SummarizeDescriptionsMiddleware

__all__ = [
    "ImageDownscaleMiddleware",
    "StubSchemaMiddleware",
    "SummarizeDescriptionsMiddleware",
]
