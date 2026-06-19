from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app


def test_cors_allows_production_storefront_origin(monkeypatch):
    monkeypatch.setenv("ENABLE_MCP_ADAPTER", "false")
    monkeypatch.setenv("ENABLE_OPENAI_APPS_UI", "false")
    get_settings.cache_clear()
    client = TestClient(create_app())

    response = client.options(
        "/api/recommendations/image",
        headers={
            "Origin": "https://sterling-hollis-fe.quickstark.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://sterling-hollis-fe.quickstark.com"
    get_settings.cache_clear()


def test_cors_allows_trace_headers_and_exposes_trace_metadata(monkeypatch):
    monkeypatch.setenv("ENABLE_MCP_ADAPTER", "false")
    monkeypatch.setenv("ENABLE_OPENAI_APPS_UI", "false")
    get_settings.cache_clear()
    client = TestClient(create_app())

    response = client.options(
        "/api/admin/traces",
        headers={
            "Origin": "https://sterling-hollis.quickstark.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,traceparent,tracestate,x-trace-surface",
        },
    )
    simple_response = client.get(
        "/health",
        headers={"Origin": "https://sterling-hollis.quickstark.com"},
    )

    assert response.status_code == 200
    assert "traceparent" in response.headers["access-control-allow-headers"].lower()
    assert "x-trace-id" in simple_response.headers["access-control-expose-headers"].lower()
    get_settings.cache_clear()


def test_cors_rejects_unknown_origin_and_header(monkeypatch):
    monkeypatch.setenv("ENABLE_MCP_ADAPTER", "false")
    monkeypatch.setenv("ENABLE_OPENAI_APPS_UI", "false")
    get_settings.cache_clear()
    client = TestClient(create_app())

    response = client.options(
        "/api/admin/traces",
        headers={
            "Origin": "https://attacker.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-secret-debug-header",
        },
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
    get_settings.cache_clear()
