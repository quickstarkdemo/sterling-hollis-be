from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api_traces.context import TraceCaptureContext
from app.api_traces.schemas import (
    ApiTraceProjection,
    TraceEventProjection,
    TraceSpanProjection,
)
from app.api_traces.service import ApiTraceRecorder, get_trace_projection
from app.config import Settings, get_settings
from app.database import Base, get_db
from app.main import create_app
from app.models import ApiTrace, ApiTraceEvent
from app.routers.api_traces import _trace_event_stream
from app.services.auth.admin import require_api_trace_capture
from app.services.auth.clerk import AuthenticatedPrincipal, require_clerk_principal


@contextmanager
def _trace_client(*, enabled: bool = True):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    Base.metadata.create_all(engine)
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        catalog_studio_clerk_authorized_subjects="owner_a,owner_b",
        api_trace_capture_enabled=enabled,
        api_trace_stream_poll_seconds=0.01,
        api_trace_stream_keepalive_seconds=0.01,
        enable_mcp_adapter=False,
        enable_openai_apps_ui=False,
    )
    app = create_app(settings=settings)

    @app.get("/_test/active-trace-owner")
    def active_trace_owner(
        context: TraceCaptureContext = Depends(require_api_trace_capture),
    ):
        return {
            "owner_provider": context.owner_provider,
            "owner_provider_user_id": context.owner_provider_user_id,
            "trace_id": context.trace_id,
            "parent_span_id": context.parent_span_id,
        }

    app.dependency_overrides[get_settings] = lambda: settings
    principal = {"value": _principal("owner_a")}
    app.dependency_overrides[require_clerk_principal] = lambda: principal["value"]

    def override_db():
        with testing_session() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), testing_session, settings, principal
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _principal(subject: str) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        provider="clerk",
        provider_user_id=subject,
        email=f"{subject}@example.com",
        claims={},
    )


def _projection(trace_id: str, *, started_at: datetime | None = None) -> ApiTraceProjection:
    started_at = started_at or datetime.now(timezone.utc)
    span_id = trace_id[-16:]
    return ApiTraceProjection(
        trace_id=trace_id,
        surface="catalog",
        name="Generate product description",
        root_span_id=span_id,
        status="running",
        started_at=started_at,
        attributes={"authorization": "Bearer secret", "safe": "value"},
        spans=[
            TraceSpanProjection(
                span_id=span_id,
                name="Catalog action",
                operation="ui.action",
                service="sterling-hollis-fe",
                status="running",
                started_at=started_at,
            )
        ],
        events=[
            TraceEventProjection(
                event_id=f"event-{trace_id[-4:]}",
                span_id=span_id,
                sequence=0,
                name="Action started",
                event_type="ui.started",
                occurred_at=started_at,
            )
        ],
    )


def _seed(
    session_factory,
    settings: Settings,
    trace_id: str,
    *,
    owner: str = "owner_a",
    started_at=None,
):
    recorded = ApiTraceRecorder(settings=settings, session_factory=session_factory).record(
        context=TraceCaptureContext.authorized_for(
            owner_provider="clerk",
            owner_provider_user_id=owner,
            surface="catalog",
        ),
        projection=_projection(trace_id, started_at=started_at),
    )
    assert recorded is True


def test_trace_api_lists_reads_ingests_catches_up_and_exports_safely():
    trace_id = "a" * 32
    with _trace_client() as (client, sessions, settings, _):
        _seed(sessions, settings, trace_id)

        listed = client.get("/api/admin/traces?limit=10")
        detail = client.get(f"/api/admin/traces/{trace_id}")
        ingested = client.post(
            f"/api/admin/traces/{trace_id}/events",
            json={
                "event_id": "browser-complete",
                "name": "Browser completed",
                "event_type": "ui.completed",
                "status": "ok",
                "attributes": {"api_key": "secret", "result": "saved"},
            },
        )
        caught_up = client.get(f"/api/admin/traces/{trace_id}/events?after_sequence=0")
        exported = client.get(f"/api/admin/traces/{trace_id}/export")

        assert listed.status_code == 200
        assert [item["trace_id"] for item in listed.json()["items"]] == [trace_id]
        assert detail.status_code == 200
        assert detail.headers["x-trace-capture"] == "active"
        assert detail.headers["x-trace-id"]
        assert ingested.status_code == 201
        assert ingested.json()["sequence"] == 1
        assert ingested.json()["attributes"] == {
            "api_key": "[REDACTED]",
            "result": "saved",
        }
        assert caught_up.json()["next_cursor"] == 1
        assert [item["event_id"] for item in caught_up.json()["items"]] == [
            "browser-complete"
        ]
        assert exported.status_code == 200
        assert exported.json()["projection_version"] == "1.0"
        assert exported.json()["attributes"]["authorization"] == "[REDACTED]"
        assert exported.headers["content-disposition"].endswith(f'{trace_id}.json"')


def test_conversation_turn_event_records_visible_transcript_artifact():
    trace_id = "f" * 32
    span_id = trace_id[-16:]
    with _trace_client() as (client, sessions, settings, _):
        _seed(sessions, settings, trace_id)

        ingested = client.post(
            f"/api/admin/traces/{trace_id}/events",
            json={
                "event_id": "voice-turn-1",
                "span_id": span_id,
                "name": "Visible realtime turn",
                "event_type": "conversation.turn",
                "attributes": {
                    "route": "catalog_realtime_voice",
                    "workflow_id": "workflow_visible_voice",
                    "raw_audio": "base64-private-audio",
                    "client_secret": "ek_short_lived_private",
                    "sdp": "private-offer",
                    "visible_messages": [
                        {
                            "visible_role": "presenter",
                            "visible_text": "Which stores are low stock?",
                            "visible_source": "realtime_transcript",
                        },
                        {
                            "visible_role": "assistant",
                            "visible_text": "Dallas Downtown is low stock.",
                            "visible_source": "realtime_transcript",
                        },
                    ],
                },
            },
        )

        assert ingested.status_code == 201, ingested.text
        with sessions() as db:
            projection = get_trace_projection(
                db,
                trace_id=trace_id,
                owner_provider="clerk",
                owner_provider_user_id="owner_a",
            )

        assert projection is not None
        artifact = next(
            item for item in projection.artifacts if item.artifact_type == "chat_transcript"
        )
        assert artifact.media_type == "application/vnd.sterling.chat-transcript+json"
        assert artifact.span_id == span_id
        assert artifact.attributes["route"] == "catalog_realtime_voice"
        assert artifact.attributes["visible_messages"] == [
            {
                "visible_role": "presenter",
                "visible_text": "Which stores are low stock?",
                "visible_source": "realtime_transcript",
            },
            {
                "visible_role": "assistant",
                "visible_text": "Dallas Downtown is low stock.",
                "visible_source": "realtime_transcript",
            },
        ]
        encoded_projection = projection.model_dump_json()
        assert "base64-private-audio" not in encoded_projection
        assert "ek_short_lived_private" not in encoded_projection
        assert "private-offer" not in encoded_projection


def test_authorized_activation_binds_owner_to_validated_w3c_context():
    incoming_trace_id = "1" * 32
    incoming_span_id = "2" * 16
    with _trace_client() as (client, _, _, _):
        response = client.get(
            "/_test/active-trace-owner",
            headers={
                "traceparent": f"00-{incoming_trace_id}-{incoming_span_id}-01",
                "x-trace-surface": "catalog-studio",
            },
        )

        assert response.status_code == 200
        assert response.json() == {
            "owner_provider": "clerk",
            "owner_provider_user_id": "owner_a",
            "trace_id": incoming_trace_id,
            "parent_span_id": incoming_span_id,
        }
        assert response.headers["x-trace-id"] == incoming_trace_id
        assert response.headers["x-trace-capture"] == "active"


def test_catalog_workflow_route_activates_trace_capture(monkeypatch):
    incoming_trace_id = "5" * 32
    incoming_span_id = "6" * 16
    with _trace_client() as (client, sessions, _, _):
        monkeypatch.setattr(
            "app.api_traces.adapters.ApiTraceRecorder",
            lambda *, settings: ApiTraceRecorder(
                settings=settings,
                session_factory=sessions,
            ),
        )
        response = client.post(
            "/api/admin/catalog/workflows",
            headers={
                "Idempotency-Key": "trace-workflow-create",
                "traceparent": f"00-{incoming_trace_id}-{incoming_span_id}-01",
                "x-trace-surface": "catalog-studio",
            },
            json={
                "title": "Trace the product workflow",
                "business_summary": "Create an instrumented catalog workflow.",
            },
        )

        assert response.status_code == 201
        assert response.headers["x-trace-capture"] == "active"
        assert response.headers["x-trace-id"] == incoming_trace_id
        with sessions() as db:
            trace = db.get(ApiTrace, incoming_trace_id)
            assert trace is not None
            assert trace.owner_provider_user_id == "owner_a"
            assert trace.surface == "catalog-studio"


def test_trace_headers_without_capture_activation_never_persist():
    with _trace_client() as (client, sessions, _, _):
        response = client.get(
            "/health",
            headers={"traceparent": f"00-{'3' * 32}-{'4' * 16}-01"},
        )

        assert response.status_code == 200
        assert "x-trace-capture" not in response.headers
        with sessions() as db:
            assert db.scalar(select(ApiTrace)) is None


def test_trace_list_cursor_is_stable():
    with _trace_client() as (client, sessions, settings, _):
        now = datetime.now(timezone.utc)
        _seed(sessions, settings, "1" * 32, started_at=now - timedelta(seconds=1))
        _seed(sessions, settings, "2" * 32, started_at=now)

        first = client.get("/api/admin/traces?limit=1")
        second = client.get(
            "/api/admin/traces",
            params={"limit": 1, "cursor": first.json()["next_cursor"]},
        )

        assert first.json()["next_cursor"]
        assert first.json()["items"][0]["trace_id"] != second.json()["items"][0]["trace_id"]


def test_cross_owner_cannot_guess_read_ingest_stream_or_export():
    trace_id = "b" * 32
    with _trace_client() as (client, sessions, settings, principal):
        _seed(sessions, settings, trace_id)
        principal["value"] = _principal("owner_b")

        assert client.get("/api/admin/traces").json()["items"] == []
        assert client.get(f"/api/admin/traces/{trace_id}").status_code == 404
        assert client.get(f"/api/admin/traces/{trace_id}/events").status_code == 404
        assert client.post(
            f"/api/admin/traces/{trace_id}/events",
            json={
                "event_id": "guess",
                "name": "Guess",
                "event_type": "ui.started",
            },
        ).status_code == 404
        assert client.get(f"/api/admin/traces/{trace_id}/stream").status_code == 404
        assert client.get(f"/api/admin/traces/{trace_id}/export").status_code == 404


def test_stream_rejects_query_credentials_and_event_ingest_is_bounded():
    trace_id = "c" * 32
    with _trace_client() as (client, sessions, settings, _):
        _seed(sessions, settings, trace_id)

        query_credential = client.get(
            f"/api/admin/traces/{trace_id}/stream?access_token=secret"
        )
        unknown_event = client.post(
            f"/api/admin/traces/{trace_id}/events",
            json={
                "event_id": "unsupported",
                "name": "Unsupported",
                "event_type": "arbitrary.debug.payload",
            },
        )
        oversized = client.post(
            f"/api/admin/traces/{trace_id}/events",
            json={
                "event_id": "large",
                "name": "Large payload",
                "event_type": "ui.completed",
                "attributes": {
                    "description": "x" * (settings.api_trace_max_string_length + 10)
                },
            },
        )
        rejected_payload = client.post(
            f"/api/admin/traces/{trace_id}/events",
            json={
                "event_id": "too-large",
                "name": "Too large",
                "event_type": "ui.completed",
                "attributes": {"description": "x" * 70_000},
            },
        )

        assert query_credential.status_code == 400
        assert unknown_event.status_code == 422
        assert oversized.status_code == 201
        assert "[truncated 10 chars]" in oversized.json()["attributes"]["description"]
        assert rejected_payload.status_code == 422


def test_stream_orders_resumes_keeps_alive_expires_and_stops_on_disconnect():
    trace_id = "e" * 32

    class RequestState:
        def __init__(self, *, disconnected: bool = False):
            self.disconnected = disconnected

        async def is_disconnected(self):
            return self.disconnected

    with _trace_client() as (client, sessions, settings, _):
        _seed(sessions, settings, trace_id)
        ingested = client.post(
            f"/api/admin/traces/{trace_id}/events",
            json={
                "event_id": "resume-event",
                "name": "Resume event",
                "event_type": "http.completed",
            },
        )
        assert ingested.status_code == 201
        context = TraceCaptureContext.authorized_for(
            owner_provider="clerk",
            owner_provider_user_id="owner_a",
            surface="developer",
        )

        async def exercise_stream():
            with sessions() as db:
                stream = _trace_event_stream(
                    request=RequestState(),
                    db=db,
                    trace_id=trace_id,
                    context=context,
                    after_sequence=0,
                    poll_seconds=0,
                    keepalive_seconds=0,
                )
                event = await anext(stream)
                keepalive = await anext(stream)
                await stream.aclose()
                assert "id: 1" in event
                assert '"event_id":"resume-event"' in event
                assert keepalive == ": keepalive\n\n"

            with sessions() as db:
                db.delete(db.get(ApiTrace, trace_id))
                db.commit()
            with sessions() as db:
                expired_stream = _trace_event_stream(
                    request=RequestState(),
                    db=db,
                    trace_id=trace_id,
                    context=context,
                    after_sequence=1,
                    poll_seconds=0,
                    keepalive_seconds=0,
                )
                assert "event: expired" in await anext(expired_stream)

            with sessions() as db:
                disconnected_stream = _trace_event_stream(
                    request=RequestState(disconnected=True),
                    db=db,
                    trace_id=trace_id,
                    context=context,
                    after_sequence=1,
                    poll_seconds=0,
                    keepalive_seconds=0,
                )
                try:
                    await anext(disconnected_stream)
                except StopAsyncIteration:
                    pass
                else:
                    raise AssertionError("disconnected stream should stop without polling")

        asyncio.run(exercise_stream())


def test_export_remains_valid_after_payload_expiry():
    trace_id = "d" * 32
    with _trace_client() as (client, sessions, settings, _):
        _seed(sessions, settings, trace_id)
        with sessions() as db:
            trace = db.get(ApiTrace, trace_id)
            trace.payload_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()

        exported = client.get(f"/api/admin/traces/{trace_id}/export")

        assert exported.status_code == 200
        assert exported.json()["payload_expired"] is True
        assert exported.json()["attributes"] == {"_retention": "expired"}


def test_disabled_capture_reports_unavailable_and_persists_nothing():
    with _trace_client(enabled=False) as (client, sessions, _, _):
        response = client.get("/api/admin/traces")
        session = client.get("/api/admin/session")

        assert response.status_code == 503
        assert response.json() == {"detail": "API trace capture is disabled."}
        assert session.json()["capabilities"]["api_traces"] == {
            "configured": False,
            "reason": "feature_disabled",
        }
        with sessions() as db:
            assert db.scalar(select(ApiTrace)) is None
            assert db.scalar(select(ApiTraceEvent)) is None
