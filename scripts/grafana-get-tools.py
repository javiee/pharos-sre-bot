
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import asyncio, json

async def main() -> None:
    # The MCP endpoint of the running server. For streamable-HTTP this is the
    # `/mcp` path; for the SSE transport use the sse_client + the `/sse` path.
    url = "http://localhost:9000/mcp"

    # `streamablehttp_client(url)` is an async context manager. Entering it opens
    # the HTTP connection and yields a (read, write) pair of message streams —
    # the low-level channels the session uses to send/receive JSON-RPC messages.
    async with streamable_http_client(url) as (read, write, _):
        # A ClientSession is the high-level MCP conversation built on those
        # streams. It knows how to do the MCP handshake and the typed calls.
        async with ClientSession(read, write) as session:
            # The MCP handshake: negotiate protocol version and capabilities.
            # You MUST call this before list_tools/call_tool.
            await session.initialize()

            # Ask the server what it can do. Returns tool definitions, each with
            # a name, a human description, and a JSON schema for its arguments.
            tools = await session.list_tools()
            for tool in tools.tools:
                print("TOOL:", tool.name)
                if tool.name == "query_prometheus":
                        print(json.dumps(tool.inputSchema, indent=2))

            # Invoke one tool by name with an arguments dict. The argument keys
            # must match that tool's input schema (seen in tool.inputSchema).
            result = await session.call_tool(
                "list_datasources", arguments={}
            )
            # The result carries `content` (a list of content blocks). For most
            # Grafana tools this is text/JSON you can read or parse.
            print("RESULT:", result.content)


asyncio.run(main())