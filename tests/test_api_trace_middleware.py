from __future__ import annotations

from fastapi import Request
from fastapi.testclient import TestClient

from app.api_traces.context import current_provisional_trace_context
from app.config import Settings
from app.main import create_app


def _client(*, enabled: bool = True) -> TestClient:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        api_trace_capture_enabled=enabled,
        enable_mcp_adapter=False,
        enable_openai_apps_ui=False,
    )
    app = create_app(settings=settings)

    @app.get("/_test/trace-context")
    def trace_context(_: Request):
        context = current_provisional_trace_context()
        return {
            "trace_id": context.trace_id if context else None,
            "span_id": context.span_id if context else None,
            "parent_span_id": context.parent_span_id if context else None,
            "remote_parent_valid": context.remote_parent_valid if context else False,
        }

    return TestClient(app)


def test_valid_traceparent_creates_child_request_context():
    trace_id = "1" * 32
    parent_id = "2" * 16
    client = _client()

    response = client.get(
        "/_test/trace-context",
        headers={"traceparent": f"00-{trace_id}-{parent_id}-01"},
    )

    assert response.status_code == 200
    assert response.json()["trace_id"] == trace_id
    assert response.json()["parent_span_id"] == parent_id
    assert response.json()["span_id"] != parent_id
    assert response.json()["remote_parent_valid"] is True
    assert response.headers["traceparent"].startswith(f"00-{trace_id}-")
    assert "x-trace-capture" not in response.headers


def test_missing_or_malformed_traceparent_starts_untrusted_root():
    client = _client()

    missing = client.get("/_test/trace-context")
    malformed = client.get(
        "/_test/trace-context",
        headers={"traceparent": f"00-{'0' * 32}-{'3' * 16}-01"},
    )

    assert missing.json()["parent_span_id"] is None
    assert missing.json()["remote_parent_valid"] is False
    assert malformed.json()["parent_span_id"] is None
    assert malformed.json()["remote_parent_valid"] is False
    assert malformed.json()["trace_id"] != "0" * 32


def test_disabled_capture_bypasses_trace_context_middleware():
    response = _client(enabled=False).get(
        "/_test/trace-context",
        headers={"traceparent": f"00-{'1' * 32}-{'2' * 16}-01"},
    )

    assert response.json()["trace_id"] is None
    assert "traceparent" not in response.headers
