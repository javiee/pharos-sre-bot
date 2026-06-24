from __future__ import annotations

import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from ..config import settings
from .base import ToolInfo

# Read-only Grafana MCP tools the agent is allowed to use. The server advertises
# ~56 tools, many of which MUTATE state (add_activity_to_incident, create_incident,
# create_annotation, update_dashboard, install_plugin, grafana_api_request,
# alerting_manage_*, ...). We expose ONLY the names below to the LLM and reject
# anything else at call time, so a wrong choice can neither be offered nor executed.
ALLOWED_GRAFANA_TOOLS = frozenset({
    # Prometheus / metrics
    "query_prometheus",
    "query_prometheus_histogram",
    "list_prometheus_metric_names",
    "list_prometheus_metric_metadata",
    "list_prometheus_label_names",
    "list_prometheus_label_values",
    # Loki / logs
    "query_loki_logs",
    "query_loki_stats",
    "query_loki_patterns",
    "list_loki_label_names",
    "list_loki_label_values",
    "find_error_pattern_logs",
    # Incidents / alerts / on-call (read-only)
    "list_incidents",
    "get_incident",
    "list_alert_groups",
    "get_alert_group",
    "get_current_oncall_users",
    # Datasources / dashboards (read-only)
    "list_datasources",
    "get_datasource",
    "search_dashboards",
    "get_dashboard_by_uid",
    "get_dashboard_summary",
    # Sift / assertions (read-only diagnostics)
    "get_assertions",
    "get_sift_analysis",
    "get_sift_investigation",
    "list_sift_investigations",
})


class GrafanaSignals:

    def __init__(self) -> None:
        self._url= settings.grafana_mcp_url
    
    async def _list_tools_async(self) -> list[ToolInfo]:
        """List the tools the Grafana MCP server advertises."""
        async with streamable_http_client(self._url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return [ToolInfo(name=t.name, description=t.description, parameters=t.inputSchema) for t in tools.tools]
        
    async def _call_tool_async(self, name: str, argumerts: dict) -> str:
        """Call a Grafana MCP tool by name with the given arguments."""
        async with streamable_http_client(self._url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments=argumerts)
                parts = [getattr(block, "text", str(block)) for block in result.content]
                print("\n".join(parts))
                return "\n".join(parts)
            
       # --- public, SYNCHRONOUS API the agent uses -------------------------
    def list_tools(self) -> list[ToolInfo]:
        """Read-only tools the agent may use (sync wrapper, allowlist-filtered)."""
        tools = asyncio.run(self._list_tools_async())
        return [t for t in tools if t.name in ALLOWED_GRAFANA_TOOLS]

    def call_tool(self, name: str, arguments: dict) -> str:
        """Run one tool and return its result as text (sync wrapper)."""
        if name not in ALLOWED_GRAFANA_TOOLS:
            raise PermissionError(f"grafana tool {name!r} is not in the read-only allowlist")
        args = dict(arguments or {})
        # query_prometheus needs a datasource UID; the LLM can't know it, so we
        # inject the configured Prometheus datasource when it's missing.
        if name == "query_prometheus" and not args.get("datasourceUid"):
            args["datasourceUid"] = settings.grafana_prometheus_uid
        return asyncio.run(self._call_tool_async(name, args))

