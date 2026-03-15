from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def _fake_vector_status_payload(probe: bool = False) -> dict:
    return {
        "mode": "cloud_full",
        "openai": {
            "configured": True,
            "client_available": True,
            "enabled": True,
            "model": "text-embedding-3-small",
            "dimension": 1536,
            "probe_attempted": probe,
            "probe_ok": True if probe else None,
            "probe_error": None,
        },
        "pinecone": {
            "configured": True,
            "client_available": True,
            "enabled": True,
            "index_name": "fashion-products-v1",
            "cloud": "aws",
            "region": "us-east-1",
            "dimension": 1536,
            "probe_attempted": probe,
            "probe_ok": True if probe else None,
            "probe_error": None,
        },
    }


def test_vector_status_endpoint_reports_cloud_mode(monkeypatch):
    import app.routers.admin_synthetic as admin_router

    monkeypatch.setattr(admin_router, "vector_status_payload", _fake_vector_status_payload)

    client = TestClient(create_app())
    response = client.get("/admin/system/vector-status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "cloud_full"
    assert payload["openai"]["enabled"] is True
    assert payload["pinecone"]["enabled"] is True
    assert payload["openai"]["probe_attempted"] is False


def test_vector_status_endpoint_supports_live_probe(monkeypatch):
    import app.routers.admin_synthetic as admin_router

    monkeypatch.setattr(admin_router, "vector_status_payload", _fake_vector_status_payload)

    client = TestClient(create_app())
    response = client.get("/admin/system/vector-status?probe=true")

    assert response.status_code == 200
    payload = response.json()
    assert payload["openai"]["probe_attempted"] is True
    assert payload["openai"]["probe_ok"] is True
    assert payload["pinecone"]["probe_attempted"] is True
    assert payload["pinecone"]["probe_ok"] is True


def test_widget_session_endpoint_allows_chatgpt_sandbox_origin(monkeypatch):
    import app.main as app_main

    monkeypatch.setattr(
        app_main,
        "get_widget_state",
        lambda token: {"kind": "merch", "payload": {"widgetSessionId": token}},
    )

    client = TestClient(create_app())
    origin = "https://connector_69b5c127e7208191b4ae3a02044726fa.web-sandbox.oaiusercontent.com"
    response = client.get("/ui-assets/session/test-token.json", headers={"Origin": origin})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.json()["kind"] == "merch"
