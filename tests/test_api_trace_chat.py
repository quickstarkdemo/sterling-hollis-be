from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api_traces.operations import (
    api_trace_database_operation,
    api_trace_http_operation,
    api_trace_operation,
    api_trace_session,
    api_trace_storage_operation,
    current_api_trace_correlation,
)
from app.api_traces.context import TraceCaptureContext, bind_trace_capture_context
from app.api_traces.service import ApiTraceRecorder, get_trace_projection
from app.config import Settings, get_settings
from app.database import Base, get_db
from app.main import create_app
from app.models import ApiTrace
from app.services.auth.clerk import AuthenticatedPrincipal
from app.services.chat.evaluator import ChatEvaluation, ChatEvaluationConstraints
from app.services.chat.safety import ChatSafetyDecision
from app.services.chat.schemas import ChatResponse
from app.services.chat.strands_orchestrator import CapturedToolCall, StrandsRunResult
from tests.test_chat_api import _seed_chat_data


def _settings(*, enabled: bool = True, admin_subject: str = "owner_a") -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        api_trace_capture_enabled=enabled,
        catalog_studio_clerk_authorized_subjects=admin_subject,
        openai_api_key=None,
        chat_orchestration_mode="deterministic",
        enable_mcp_adapter=False,
        enable_openai_apps_ui=False,
        demo_observability_enabled=False,
        product_image_output_dir="/tmp/sterling-hollis-trace-chat-images",
    )


@contextmanager
def _trace_chat_client(
    monkeypatch,
    *,
    enabled: bool = True,
    admin: bool = True,
    principal_email: str = "avery@example.com",
):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sessions = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    Base.metadata.create_all(engine)
    settings = _settings(
        enabled=enabled,
        admin_subject="owner_a" if admin else "different_admin",
    )
    app = create_app(settings=settings)
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr("app.services.chat.orchestrator.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.services.auth.clerk.verify_clerk_token",
        lambda token, settings=None: AuthenticatedPrincipal(
            provider="clerk",
            provider_user_id="owner_a",
            email=principal_email,
            claims={},
        ),
    )
    monkeypatch.setattr(
        "app.api_traces.operations.ApiTraceRecorder",
        lambda *, settings: ApiTraceRecorder(
            settings=settings,
            session_factory=sessions,
        ),
    )

    def override_db():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with sessions() as db:
        _seed_chat_data(db)
    try:
        yield TestClient(app), sessions, settings
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _chat_payload(
    *,
    conversation_id: str | None = None,
    message: str = "hello private-chat-secret-4477",
) -> dict:
    payload = {
        "message": message,
        "client_request_id": "client-trace-chat-1",
        "context": {"route": "/", "store_id": "1001"},
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id
    return payload


def _trace_headers(trace_id: str, parent_span_id: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer test-token",
        "traceparent": f"00-{trace_id}-{parent_span_id}-01",
        "x-trace-surface": "storefront-chat",
    }


def test_authorized_chat_projects_safe_routing_tool_persistence_and_ui_spans(monkeypatch):
    trace_id = "1" * 32
    with _trace_chat_client(monkeypatch) as (client, sessions, _):
        response = client.post(
            "/api/chat",
            headers=_trace_headers(trace_id, "2" * 16),
            json=_chat_payload(),
        )

        assert response.status_code == 200
        assert response.headers["x-trace-capture"] == "active"
        assert response.headers["x-trace-id"] == trace_id
        with sessions() as db:
            projection = get_trace_projection(
                db,
                trace_id=trace_id,
                owner_provider="clerk",
                owner_provider_user_id="owner_a",
            )

    assert projection is not None
    operations = {span.operation for span in projection.spans}
    assert {
        "http.client",
        "http.server",
        "database.query",
        "chat.safety",
        "chat.routing",
        "chat.authorization",
        "chat.tool",
        "ui.response",
    } <= operations
    assert projection.surface == "storefront-chat"
    assert projection.attributes["selected_tool"]
    assert "private-chat-secret-4477" not in projection.model_dump_json()


def test_duplicate_client_request_links_new_trace_to_original_execution(monkeypatch):
    first_trace_id = "3" * 32
    replay_trace_id = "4" * 32
    with _trace_chat_client(monkeypatch) as (client, sessions, _):
        first = client.post(
            "/api/chat",
            headers=_trace_headers(first_trace_id, "5" * 16),
            json=_chat_payload(),
        )
        replay = client.post(
            "/api/chat",
            headers=_trace_headers(replay_trace_id, "6" * 16),
            json=_chat_payload(conversation_id=first.json()["conversation_id"]),
        )
        with sessions() as db:
            projection = get_trace_projection(
                db,
                trace_id=replay_trace_id,
                owner_provider="clerk",
                owner_provider_user_id="owner_a",
            )

    assert replay.status_code == 200
    assert replay.json()["duplicate_replay"] is True
    assert projection is not None
    assert any(span.operation == "chat.replay" for span in projection.spans)
    assert len(projection.links) == 1
    assert projection.links[0].relationship == "replay_of"
    assert projection.links[0].linked_trace_id == first_trace_id


def test_chat_trace_headers_do_not_activate_for_non_admin_customer(monkeypatch):
    trace_id = "7" * 32
    with _trace_chat_client(monkeypatch, admin=False) as (client, sessions, _):
        response = client.post(
            "/api/chat",
            headers=_trace_headers(trace_id, "8" * 16),
            json=_chat_payload(),
        )
        with sessions() as db:
            persisted = db.scalar(select(ApiTrace).where(ApiTrace.id == trace_id))

    assert response.status_code == 200
    assert "x-trace-capture" not in response.headers
    assert persisted is None


def test_disabled_chat_capture_preserves_business_response_and_persists_nothing(monkeypatch):
    trace_id = "8" * 32
    with _trace_chat_client(monkeypatch, enabled=False) as (client, sessions, _):
        response = client.post(
            "/api/chat",
            headers=_trace_headers(trace_id, "9" * 16),
            json=_chat_payload(),
        )
        with sessions() as db:
            persisted = db.scalar(select(ApiTrace).where(ApiTrace.id == trace_id))

    assert response.status_code == 200
    assert response.json()["message"]
    assert "x-trace-capture" not in response.headers
    assert persisted is None


def test_trace_recorder_failure_is_fail_open_for_chat(monkeypatch):
    trace_id = "a" * 32
    with _trace_chat_client(monkeypatch) as (client, sessions, _):
        class BrokenRecorder:
            def __init__(self, *, settings):
                raise RuntimeError("recorder unavailable")

        monkeypatch.setattr("app.api_traces.operations.ApiTraceRecorder", BrokenRecorder)
        response = client.post(
            "/api/chat",
            headers=_trace_headers(trace_id, "b" * 16),
            json=_chat_payload(),
        )
        with sessions() as db:
            persisted = db.scalar(select(ApiTrace).where(ApiTrace.id == trace_id))

    assert response.status_code == 200
    assert response.json()["message"]
    assert response.headers["x-trace-capture"] == "active"
    assert persisted is None


def test_safety_block_is_visible_without_recording_customer_text(monkeypatch):
    monkeypatch.setattr(
        "app.services.chat.orchestrator.evaluate_chat_safety",
        lambda message, history: ChatSafetyDecision(
            intercepted=True,
            source="openai_moderation",
            action="block",
            content="I cannot help with that request.",
            category="harmful_content",
            reason="policy",
            tags=("blocked",),
        ),
    )
    trace_id = "9" * 32
    with _trace_chat_client(monkeypatch) as (client, sessions, _):
        response = client.post(
            "/api/chat",
            headers=_trace_headers(trace_id, "a" * 16),
            json=_chat_payload(),
        )
        with sessions() as db:
            projection = get_trace_projection(
                db,
                trace_id=trace_id,
                owner_provider="clerk",
                owner_provider_user_id="owner_a",
            )

    assert response.status_code == 200
    assert response.json()["route"] == "blocked"
    assert projection is not None
    assert projection.status == "blocked"
    safety_span = next(span for span in projection.spans if span.operation == "chat.safety")
    assert safety_span.status == "blocked"
    assert safety_span.attributes["category"] == "harmful_content"
    assert "private-chat-secret-4477" not in projection.model_dump_json()


def test_auth_blocked_chat_marks_authorization_and_trace_blocked(monkeypatch):
    trace_id = "c" * 32
    with _trace_chat_client(
        monkeypatch,
        principal_email="presenter@example.com",
    ) as (client, sessions, _):
        response = client.post(
            "/api/chat",
            headers=_trace_headers(trace_id, "d" * 16),
            json=_chat_payload(message="What is my order status?"),
        )
        with sessions() as db:
            projection = get_trace_projection(
                db,
                trace_id=trace_id,
                owner_provider="clerk",
                owner_provider_user_id="owner_a",
            )

    assert response.status_code == 200
    assert response.json()["route"] == "blocked"
    assert projection is not None
    auth_span = next(
        span for span in projection.spans if span.operation == "chat.authorization"
    )
    assert auth_span.status == "blocked"
    assert auth_span.attributes["blocked"] is True


def test_strands_agent_and_deterministic_fallback_have_distinct_trace_spans(monkeypatch):
    from app.services.chat import orchestrator

    monkeypatch.setattr(orchestrator, "_settings_enable_strands_product", lambda: True)
    call_count = {"value": 0}

    def run_agent(db, *, req, identity, session, decision, frame, history):
        call_count["value"] += 1
        if call_count["value"] == 1:
            return StrandsRunResult(
                response=ChatResponse(
                    conversation_id=session.id,
                    message="I found one option.",
                    identity_status=identity.status,
                    intent="catalog_search",
                    route="simple_tool",
                    cards=[],
                    actions=[],
                    selected_agent="StorefrontShoppingAgent",
                    selected_tool="search_catalog",
                ),
                tool_calls=[
                    CapturedToolCall(
                        name="search_catalog",
                        input_json={"category": "shoes"},
                        output_json={"product_ids": []},
                    )
                ],
            )
        return StrandsRunResult(error="RuntimeError")

    monkeypatch.setattr(orchestrator, "run_storefront_shopping_agent", run_agent)
    first_trace_id = "d" * 32
    fallback_trace_id = "e" * 32
    with _trace_chat_client(monkeypatch) as (client, sessions, _):
        first = client.post(
            "/api/chat",
            headers=_trace_headers(first_trace_id, "1" * 16),
            json=_chat_payload(message="Find shoes"),
        )
        fallback = client.post(
            "/api/chat",
            headers=_trace_headers(fallback_trace_id, "2" * 16),
            json={
                **_chat_payload(message="Find shoes"),
                "client_request_id": "client-trace-chat-2",
            },
        )
        with sessions() as db:
            first_projection = get_trace_projection(
                db,
                trace_id=first_trace_id,
                owner_provider="clerk",
                owner_provider_user_id="owner_a",
            )
            fallback_projection = get_trace_projection(
                db,
                trace_id=fallback_trace_id,
                owner_provider="clerk",
                owner_provider_user_id="owner_a",
            )

    assert first.status_code == 200
    assert fallback.status_code == 200
    assert first_projection is not None
    assert fallback_projection is not None
    first_agent = next(
        span for span in first_projection.spans if span.operation == "chat.agent"
    )
    fallback_agent = next(
        span for span in fallback_projection.spans if span.operation == "chat.agent"
    )
    assert first_agent.status == "succeeded"
    assert fallback_agent.status == "failed"
    assert any(
        span.operation == "chat.fallback" for span in fallback_projection.spans
    )


def test_generic_operation_helpers_share_trace_correlation_and_services(monkeypatch):
    settings = _settings()
    context = TraceCaptureContext(
        owner_provider="clerk",
        owner_provider_user_id="owner_a",
        surface="developer",
        authorized=True,
        trace_id="b" * 32,
        span_id="c" * 16,
        parent_span_id="d" * 16,
    )
    recorded = []

    class FakeRecorder:
        def __init__(self, *, settings):
            self.settings = settings

        def record(self, *, context, projection):
            recorded.append(projection)
            return True

    monkeypatch.setattr("app.api_traces.operations.ApiTraceRecorder", FakeRecorder)
    with bind_trace_capture_context(context), api_trace_session(
        settings=settings,
        name="Generic operation test",
    ):
        with api_trace_http_operation("Call inventory API", service="inventory"):
            assert current_api_trace_correlation()["app.trace_id"] == "b" * 32
        with api_trace_database_operation("Read inventory rows"):
            pass
        with api_trace_storage_operation("Read product object", service="object-store"):
            pass

    assert len(recorded) == 1
    projection = recorded[0]
    assert [span.operation for span in projection.spans[-3:]] == [
        "http.client",
        "database.query",
        "storage.operation",
    ]
    assert [span.service for span in projection.spans[-3:]] == [
        "inventory",
        "sterling-hollis-be",
        "object-store",
    ]


def test_tool_failure_closes_operation_and_trace_as_failed(monkeypatch):
    settings = _settings()
    context = TraceCaptureContext(
        owner_provider="clerk",
        owner_provider_user_id="owner_a",
        surface="storefront-chat",
        authorized=True,
        trace_id="f" * 32,
        span_id="1" * 16,
    )
    recorded = []

    class FakeRecorder:
        def __init__(self, *, settings):
            self.settings = settings

        def record(self, *, context, projection):
            recorded.append(projection)
            return True

    monkeypatch.setattr("app.api_traces.operations.ApiTraceRecorder", FakeRecorder)
    with pytest.raises(RuntimeError, match="tool unavailable"):
        with bind_trace_capture_context(context), api_trace_session(
            settings=settings,
            name="Tool failure test",
        ):
            with api_trace_operation("Run inventory tool", "chat.tool"):
                raise RuntimeError("tool unavailable")

    assert recorded[0].status == "failed"
    failed_span = next(
        span for span in recorded[0].spans if span.operation == "chat.tool"
    )
    assert failed_span.status == "failed"
    assert failed_span.attributes["error_code"] == "RuntimeError"


def test_chat_intake_sends_client_request_id_and_records_provider_id(monkeypatch):
    from app.services.chat import evaluator

    settings = _settings()
    settings.openai_api_key = "test-key"
    evaluation = ChatEvaluation(
        intent="general_style",
        target_agent="CustomerServiceAgent",
        tool="chat_response",
        confidence=0.9,
        requires_auth=False,
        constraints=ChatEvaluationConstraints(),
        rationale="safe test result",
    )
    completion = SimpleNamespace(
        id="chatcmpl_test",
        _request_id="req_provider_test",
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=evaluation))],
    )
    calls = []

    class FakeCompletions:
        def parse(self, **kwargs):
            calls.append(kwargs)
            return completion

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.beta = SimpleNamespace(
                chat=SimpleNamespace(completions=FakeCompletions())
            )

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    monkeypatch.setattr(evaluator, "get_settings", lambda: settings)
    recorded = []

    class FakeRecorder:
        def __init__(self, *, settings):
            self.settings = settings

        def record(self, *, context, projection):
            recorded.append(projection)
            return True

    monkeypatch.setattr("app.api_traces.operations.ApiTraceRecorder", FakeRecorder)
    context = TraceCaptureContext(
        owner_provider="clerk",
        owner_provider_user_id="owner_a",
        surface="storefront-chat",
        authorized=True,
        trace_id="e" * 32,
        span_id="f" * 16,
    )

    with bind_trace_capture_context(context), api_trace_session(
        settings=settings,
        name="Chat intake test",
    ):
        parsed = evaluator._run_chat_intake_llm("private prompt", model="gpt-test")

    assert parsed == evaluation
    client_request_id = calls[0]["extra_headers"]["X-Client-Request-Id"]
    assert client_request_id.startswith("sh-chatintake-")
    provider_span = next(
        span for span in recorded[0].spans if span.service == "openai"
    )
    assert provider_span.attributes["client_request_id"] == client_request_id
    assert provider_span.attributes["provider_request_id"] == "req_provider_test"
    assert "private prompt" not in recorded[0].model_dump_json()
