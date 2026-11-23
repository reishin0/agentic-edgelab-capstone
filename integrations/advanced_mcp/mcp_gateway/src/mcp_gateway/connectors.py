"""A common capability boundary for MCP and native HTTP services."""
from typing import Protocol

from mcp import types

from .backend import BackendPool
from .config import Backend


class Connector(Protocol):
    config: Backend

    def start(self) -> None: ...
    async def close(self) -> None: ...
    async def list_tools(self, ttl: float, *, credential=None, cache_key="service") -> list[types.Tool]: ...
    async def request(self, operation: str, *args, credential=None) -> types.CallToolResult: ...


def create_connector(config: Backend) -> Connector:
    if config.kind == "openapi":
        from .openapi_connector import OpenAPIConnector
        return OpenAPIConnector(config)
    return BackendPool(config)
