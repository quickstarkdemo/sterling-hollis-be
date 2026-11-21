from __future__ import annotations

import asyncio

import httpx

from app import mcp_server
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def _server(monkeypatch):
    monkeypatch.setattr(mcp_server.settings, "mcp_allowed_hosts", "127.0.0.1:*")
    server = mcp_server._new_mcp_server("transport-lifecycle-test")

    @server.tool()
    def echo(value: str) -> str:
        return value

    return server


def test_abandoned_initializations_do_not_retain_sessions(monkeypatch):
    server = _server(monkeypatch)
    app = server.streamable_http_app()

    async def exercise():
        async with server.session_manager.run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8000",
                headers={"Accept": "application/json, text/event-stream"},
            ) as client:
                for request_id in range(50):
                    response = await client.post(
                        "/",
                        json={
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2025-03-26",
                                "capabilities": {},
                                "clientInfo": {"name": "memory-test", "version": "1"},
                            },
                        },
                    )
                    assert response.status_code == 200
                # Clients may disappear without DELETE; the server must not retain them.
                assert len(server.session_manager._server_instances) == 0
                assert "mcp-session-id" not in response.headers

    asyncio.run(exercise())


def test_independent_requests_can_list_and_call_tools(monkeypatch):
    server = _server(monkeypatch)
    app = server.streamable_http_app()

    async def exercise():
        async with server.session_manager.run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8000",
                headers={"Accept": "application/json, text/event-stream"},
            ) as client:
                listed = await client.post(
                    "/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                )
                assert listed.status_code == 200
                assert [tool["name"] for tool in listed.json()["result"]["tools"]] == ["echo"]
                called = await client.post(
                    "/",
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "echo", "arguments": {"value": "ok"}},
                    },
                )
                assert called.status_code == 200
                assert called.json()["result"]["content"][0]["text"] == "ok"
                assert len(server.session_manager._server_instances) == 0

    asyncio.run(exercise())


def test_standard_mcp_client_remains_compatible(monkeypatch):
    server = _server(monkeypatch)
    app = server.streamable_http_app()

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), **kwargs)

    async def exercise():
        async with server.session_manager.run():
            async with streamablehttp_client(
                "http://127.0.0.1:8000/", httpx_client_factory=client_factory
            ) as (read, write, session_id):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    tools = await client.list_tools()
                    assert [tool.name for tool in tools.tools] == ["echo"]
                    result = await client.call_tool("echo", {"value": "compatible"})
                    assert result.content[0].text == "compatible"
                    assert session_id() is None
            assert len(server.session_manager._server_instances) == 0

    asyncio.run(exercise())


def test_stateless_transport_keeps_host_validation(monkeypatch):
    server = _server(monkeypatch)
    app = server.streamable_http_app()

    async def exercise():
        async with server.session_manager.run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://untrusted.example:8000",
                headers={"Accept": "application/json, text/event-stream"},
            ) as client:
                response = await client.post(
                    "/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                )
                assert response.status_code == 421
                assert len(server.session_manager._server_instances) == 0

    asyncio.run(exercise())
