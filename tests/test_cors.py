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
