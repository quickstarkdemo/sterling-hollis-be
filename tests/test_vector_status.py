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
