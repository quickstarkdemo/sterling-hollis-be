from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent


async def main(server_url: str) -> None:
    async with streamable_http_client(server_url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            tools = await session.list_tools()
            tool_names = [tool.name for tool in tools.tools]
            print(json.dumps({"server_url": server_url, "tool_count": len(tool_names), "tools": tool_names}, indent=2))

            result = await session.call_tool("fashion_vector_status", arguments={"probe": False})
            print("fashion_vector_status")
            print(json.dumps(result.structuredContent, indent=2))

            try:
                latest = await session.call_tool("fashion_latest_run", arguments={})
                print("fashion_latest_run")
                print(json.dumps(latest.structuredContent, indent=2))
            except Exception as exc:
                print(json.dumps({"latest_run_error": str(exc)}, indent=2))

            recommendations = await session.call_tool(
                "fashion_customer_recommendations",
                arguments={
                    "store_id": "1001",
                    "occasion": "wedding guest dress",
                    "budget_max": 900,
                    "top_k": 3,
                },
            )
            print("fashion_customer_recommendations")
            print(json.dumps(recommendations.structuredContent, indent=2))

            if result.content:
                first = result.content[0]
                if isinstance(first, TextContent):
                    print("fashion_vector_status_text")
                    print(first.text)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/mcp"
    asyncio.run(main(target))
