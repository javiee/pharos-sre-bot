from __future__ import annotations
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from ..config import settings
from .base import ToolInfo

# Read-only Kubernetes MCP tools the agent is allowed to use. Excludes
# configuration_view (dumps the kubeconfig). We expose ONLY these to the LLM and
# reject anything else at call time.
ALLOWED_K8S_TOOLS = frozenset({
    "events_list",
    "namespaces_list",
    "nodes_log",
    "nodes_stats_summary",
    "nodes_top",
    "pods_get",
    "pods_list",
    "pods_list_in_namespace",
    "pods_log",
    "pods_top",
    "resources_get",
    "resources_list",
})


class KubernetesSignals:

    def __init__(self) -> None:
        self._url = settings.k8s_mcp_url

    async def _list_tools_async(self) -> list[ToolInfo]:
        """ List the tools the kubernetes MCP server advertises."""
        async with streamable_http_client(self._url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return [ToolInfo(name=t.name, description=t.description, parameters=t.inputSchema) for t in tools.tools]
    async def _call_tool_async(self, name: str, arguments: dict) -> str:
        """ Call a kubernetes MCP tool by name with the given arguments."""
        async with streamable_http_client(self._url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments=arguments)
                parts = [getattr(block, "text", str(block)) for block in result.content]
                print("\n".join(parts))
                return "\n".join(parts)
    
    def list_tools(self) -> list[ToolInfo]:
        """ Read-only tools the agent may use (sync wrapper, allowlist-filtered)."""
        tools = asyncio.run(self._list_tools_async())
        return [t for t in tools if t.name in ALLOWED_K8S_TOOLS]

    def call_tool(self, name: str, arguments: dict) -> str:
        """ Run one tool and return its result as text (sync wrapper)."""
        if name not in ALLOWED_K8S_TOOLS:
            raise PermissionError(f"kubernetes tool {name!r} is not in the read-only allowlist")
        return asyncio.run(self._call_tool_async(name, arguments))