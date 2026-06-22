from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


BUNDLE_EXPECTATIONS = {
    "/mcp/public": {
        "required": {
            "fashion_catalog_search",
            "fashion_catalog_product_detail",
            "fashion_get_product_feed",
        },
        "forbidden": {
            "fashion_lookup_customer",
            "fashion_exec_overview",
            "fashion_send_customer_email_draft",
        },
    },
    "/mcp/associate": {
        "required": {
            "fashion_lookup_customer",
            "fashion_prepare_customer_email_draft",
            "fashion_update_customer_email_draft",
        },
        "forbidden": {
            "fashion_send_customer_email_draft",
            "fashion_exec_overview",
        },
    },
    "/mcp/associate-send": {
        "required": {
            "fashion_send_customer_email_draft",
            "fashion_send_customer_sms",
        },
        "forbidden": {
            "fashion_exec_overview",
        },
    },
    "/mcp/merchandiser": {
        "required": {
            "fashion_merch_action_recommendations",
            "fashion_merch_inventory_view",
        },
        "forbidden": {
            "fashion_lookup_customer",
            "fashion_exec_send_strategy_packet_email",
        },
    },
    "/mcp/executive": {
        "required": {
            "fashion_exec_overview",
            "fashion_exec_event_readiness_radar",
        },
        "forbidden": {
            "fashion_exec_send_strategy_packet_email",
            "fashion_send_customer_email_draft",
        },
    },
    "/mcp/executive-send": {
        "required": {
            "fashion_exec_send_strategy_packet_email",
            "fashion_exec_campaign_autopilot_send",
        },
        "forbidden": {
            "fashion_send_customer_email_draft",
        },
    },
    "/mcp/catalog-admin": {
        "required": {
            "fashion_vector_status",
            "fashion_generate_synthetic",
        },
        "forbidden": {
            "fashion_lookup_customer",
            "fashion_exec_overview",
        },
    },
}


async def _list_tools(server_url: str) -> set[str]:
    async with streamable_http_client(server_url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            return {tool.name for tool in tools.tools}


async def main(api_url: str) -> None:
    base_url = api_url.rstrip("/")
    report = {}
    for path, expectation in BUNDLE_EXPECTATIONS.items():
        server_url = f"{base_url}{path}/"
        tools = await _list_tools(server_url)
        missing = sorted(expectation["required"] - tools)
        exposed = sorted(expectation["forbidden"] & tools)
        report[path] = {
            "tool_count": len(tools),
            "required_present": sorted(expectation["required"] & tools),
            "missing_required": missing,
            "forbidden_exposed": exposed,
        }
        if missing or exposed:
            print(json.dumps(report, indent=2))
            raise SystemExit(1)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    asyncio.run(main(target))
