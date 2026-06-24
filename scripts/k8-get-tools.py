import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main() -> None:
    # The streamable-HTTP MCP endpoint of the running k8s server (note /mcp).
    url = "http://localhost:9001/mcp"

    # streamablehttp_client(url) is an async context manager. Entering it opens
    # the HTTP connection and yields (read, write, get_session_id) — a 3-tuple.
    # (SSE's sse_client yields a 2-tuple instead; do not mix them up — that
    # mismatch is what caused the "Session terminated" bug in the debug log.)
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            # The MCP handshake. MUST run before list_tools/call_tool.
            await session.initialize()

            # Ask the server what tools it has. With --read-only, only the safe
            # read tools should appear here. This is your source of truth for the
            # real tool names + argument schemas on your image version.
            tools = await session.list_tools()
            for tool in tools.tools:
                print("TOOL:", tool.name)

            # Call one read tool. pods_list_in_namespace needs a namespace arg;
            # the key name must match the tool's inputSchema (print tool.inputSchema
            # if unsure). pods_list (all namespaces) takes no arguments.
            result = await session.call_tool(
                "pods_list_in_namespace", arguments={"namespace": "default"}
            )
            # result.content is a list of content blocks; for these tools it's
            # text/JSON you can read or parse.
            print("RESULT:", result.content)


asyncio.run(main())