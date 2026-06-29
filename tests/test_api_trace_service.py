from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api_traces.context import (
    TraceCaptureContext,
    bind_trace_capture_context,
    current_trace_capture_context,
)
from app.api_traces.schemas import (
    ApiTraceProjection,
    ClientTraceEventInput,
    TraceArtifactProjection,
    TraceEventProjection,
    TraceLinkProjection,
    TraceSpanProjection,
)
from app.api_traces.service import (
    ApiTraceRecorder,
    append_client_trace_event,
    cleanup_expired_api_traces,
    get_trace_projection,
)
from app.config import Settings
from app.database import Base
from app.models import ApiTrace, ApiTraceEvent, ApiTraceSpan, SyntheticRun


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    try:
        yield factory
    finally:
        engine.dispose()


def _settings(**overrides) -> Settings:
    values = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "api_trace_capture_enabled": True,
        "api_trace_payload_retention_hours": 1,
        "api_trace_metadata_retention_days": 7,
        "api_trace_max_bytes": 4096,
        "api_trace_redacted_keys": "vendor_private",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _context(*, authorized: bool = True) -> TraceCaptureContext:
    return TraceCaptureContext(
        owner_provider="clerk",
        owner_provider_user_id="user_admin",
        surface="catalog",
        authorized=authorized,
    )


def _projection(now: datetime) -> ApiTraceProjection:
    return ApiTraceProjection(
        trace_id="trace_catalog_1",
        surface="catalog",
        name="Generate product draft",
        root_span_id="span_root",
        status="succeeded",
        started_at=now,
        completed_at=now + timedelta(milliseconds=850),
        duration_ms=850,
        attributes={"provider": "openai", "model": "gpt-5.5"},
        spans=[
            TraceSpanProjection(
                span_id="span_root",
                name="Product workflow",
                operation="workflow",
                service="sterling-hollis-be",
                status="succeeded",
                started_at=now,
                completed_at=now + timedelta(milliseconds=850),
                duration_ms=850,
                attributes={"product_id": "product_1"},
            ),
            TraceSpanProjection(
                span_id="span_openai",
                parent_span_id="span_root",
                name="Responses API",
                operation="api_call",
                service="openai",
                status="succeeded",
                started_at=now + timedelta(milliseconds=20),
                completed_at=now + timedelta(milliseconds=700),
                duration_ms=680,
                attributes={
                    "request": {
                        "method": "POST",
                        "route": "/v1/responses",
                        "authorization": "Bearer private-token",
                        "raw_audio": b"private-audio",
                        "image_bytes": "private-image",
                        "private_reasoning": "private-chain",
                        "unknown_body": "private-unknown",
                    }
                },
            ),
        ],
        links=[
            TraceLinkProjection(
                link_id="link_retry",
                span_id="span_openai",
                linked_trace_id="trace_previous",
                linked_span_id="span_previous",
                relationship="retry_of",
                attributes={"attempt": 2, "retry_reason": "timeout"},
            )
        ],
        events=[
            TraceEventProjection(
                event_id="event_request",
                span_id="span_openai",
                sequence=1,
                name="Request sent",
                event_type="request",
                status="started",
                occurred_at=now + timedelta(milliseconds=20),
                attributes={"request_bytes": 640},
            ),
            TraceEventProjection(
                event_id="event_response",
                span_id="span_openai",
                sequence=2,
                name="Response received",
                event_type="response",
                status="succeeded",
                occurred_at=now + timedelta(milliseconds=700),
                attributes={"status_code": 200, "response_bytes": 1024},
            ),
        ],
        artifacts=[
            TraceArtifactProjection(
                artifact_id="artifact_json",
                span_id="span_openai",
                artifact_type="json",
                name="Sanitized response metadata",
                media_type="application/json",
                size_bytes=1024,
                attributes={"status": "available"},
            )
        ],
    )


def test_projection_round_trips_topology_status_timing_and_sanitized_payloads(
    session_factory,
):
    now = datetime(2026, 6, 19, 20, 0, tzinfo=timezone.utc)
    recorder = ApiTraceRecorder(settings=_settings(), session_factory=session_factory)

    assert recorder.record(context=_context(), projection=_projection(now), now=now)
    with session_factory() as db:
        projected = get_trace_projection(
            db,
            trace_id="trace_catalog_1",
            owner_provider="clerk",
            owner_provider_user_id="user_admin",
            now=now,
        )

    assert projected is not None
    assert projected.projection_version == "1.0"
    assert projected.root_span_id == "span_root"
    assert [span.span_id for span in projected.spans] == ["span_root", "span_openai"]
    assert projected.spans[1].parent_span_id == "span_root"
    assert projected.links[0].relationship == "retry_of"
    assert [event.sequence for event in projected.events] == [1, 2]
    assert projected.artifacts[0].media_type == "application/json"
    assert projected.status == "succeeded"
    assert projected.duration_ms == 850

    encoded = projected.model_dump_json()
    for secret in (
        "private-token",
        "private-audio",
        "private-image",
        "private-chain",
        "private-unknown",
    ):
        assert secret not in encoded
    assert "[REDACTED]" in encoded


def test_duplicate_event_ids_are_idempotent(session_factory):
    now = datetime(2026, 6, 19, 20, 0, tzinfo=timezone.utc)
    recorder = ApiTraceRecorder(settings=_settings(), session_factory=session_factory)
    projection = _projection(now)

    assert recorder.record(context=_context(), projection=projection, now=now)
    assert recorder.record(context=_context(), projection=projection, now=now)

    with session_factory() as db:
        event_count = db.scalar(
            select(func.count()).select_from(ApiTraceEvent).where(
                ApiTraceEvent.trace_id == projection.trace_id
            )
        )
        span_count = db.scalar(
            select(func.count()).select_from(ApiTraceSpan).where(
                ApiTraceSpan.trace_id == projection.trace_id
            )
        )
    assert event_count == 2
    assert span_count == 2


def test_projection_rejects_duplicate_event_sequences():
    payload = _projection(datetime(2026, 6, 19, 20, 0, tzinfo=timezone.utc)).model_dump()
    payload["events"][1]["sequence"] = payload["events"][0]["sequence"]

    with pytest.raises(ValueError, match="event sequence values must be unique"):
        ApiTraceProjection(**payload)


def test_payload_expiry_preserves_topology_then_metadata_expiry_deletes_trace(
    session_factory,
):
    now = datetime(2026, 6, 19, 20, 0, tzinfo=timezone.utc)
    recorder = ApiTraceRecorder(settings=_settings(), session_factory=session_factory)
    assert recorder.record(context=_context(), projection=_projection(now), now=now)

    with session_factory() as db:
        payload_result = cleanup_expired_api_traces(
            db, now=now + timedelta(hours=2)
        )
        projected = get_trace_projection(
            db,
            trace_id="trace_catalog_1",
            owner_provider="clerk",
            owner_provider_user_id="user_admin",
            now=now + timedelta(hours=2),
        )

        assert payload_result.payloads_expired == 1
        assert payload_result.traces_deleted == 0
        assert projected is not None
        assert projected.payload_expired is True
        assert projected.attributes == {"_retention": "expired"}
        assert len(projected.spans) == 2
        assert len(projected.events) == 2
        assert projected.spans[0].duration_ms == 850

        metadata_result = cleanup_expired_api_traces(
            db, now=now + timedelta(days=8)
        )
        assert metadata_result.traces_deleted == 1
        assert db.get(ApiTrace, "trace_catalog_1") is None


def test_projection_enforces_retention_before_physical_cleanup(session_factory):
    now = datetime(2026, 6, 19, 20, 0, tzinfo=timezone.utc)
    recorder = ApiTraceRecorder(settings=_settings(), session_factory=session_factory)
    assert recorder.record(context=_context(), projection=_projection(now), now=now)

    with session_factory() as db:
        payload_expired = get_trace_projection(
            db,
            trace_id="trace_catalog_1",
            owner_provider="clerk",
            owner_provider_user_id="user_admin",
            now=now + timedelta(hours=2),
        )
        stored = db.get(ApiTrace, "trace_catalog_1")
        metadata_expired = get_trace_projection(
            db,
            trace_id="trace_catalog_1",
            owner_provider="clerk",
            owner_provider_user_id="user_admin",
            now=now + timedelta(days=8),
        )

    assert payload_expired is not None
    assert payload_expired.payload_expired is True
    assert payload_expired.attributes == {"_retention": "expired"}
    assert payload_expired.spans[0].attributes == {"_retention": "expired"}
    assert stored is not None
    assert stored.payload_expired is False
    assert metadata_expired is None


def test_cardinality_limits_are_visible(session_factory):
    now = datetime(2026, 6, 19, 20, 0, tzinfo=timezone.utc)
    projection = _projection(now)
    projection.events.extend(
        [
            TraceEventProjection(
                event_id=f"event_extra_{index}",
                span_id="span_root",
                sequence=10 + index,
                name="Extra",
                event_type="status",
                occurred_at=now,
            )
            for index in range(3)
        ]
    )
    recorder = ApiTraceRecorder(
        settings=_settings(api_trace_max_events=2),
        session_factory=session_factory,
    )

    assert recorder.record(context=_context(), projection=projection, now=now)
    with session_factory() as db:
        trace = db.get(ApiTrace, projection.trace_id)
        event_count = db.scalar(
            select(func.count()).select_from(ApiTraceEvent).where(
                ApiTraceEvent.trace_id == projection.trace_id
            )
        )

    assert trace is not None
    assert trace.truncation_json == {"events": 3}
    assert event_count == 2


def test_cardinality_limits_apply_across_incremental_recordings(session_factory):
    now = datetime(2026, 6, 19, 20, 0, tzinfo=timezone.utc)
    recorder = ApiTraceRecorder(
        settings=_settings(api_trace_max_events=2),
        session_factory=session_factory,
    )
    first = _projection(now)
    second = ApiTraceProjection.model_validate(
        {
            **first.model_dump(),
            "events": [
                TraceEventProjection(
                    event_id=f"event_late_{index}",
                    span_id="span_root",
                    sequence=10 + index,
                    name="Late event",
                    event_type="status",
                    occurred_at=now,
                )
                for index in range(2)
            ],
        }
    )

    assert recorder.record(context=_context(), projection=first, now=now)
    assert recorder.record(context=_context(), projection=second, now=now)

    with session_factory() as db:
        trace = db.get(ApiTrace, first.trace_id)
        event_count = db.scalar(
            select(func.count()).select_from(ApiTraceEvent).where(
                ApiTraceEvent.trace_id == first.trace_id
            )
        )

    assert trace is not None
    assert trace.truncation_json == {"events": 2}
    assert event_count == 2


def test_incremental_recording_closes_existing_spans_and_resolves_sequence_collisions(
    session_factory,
):
    now = datetime(2026, 6, 19, 20, 0, tzinfo=timezone.utc)
    recorder = ApiTraceRecorder(settings=_settings(), session_factory=session_factory)
    first = _projection(now)
    first.status = "running"
    first.completed_at = None
    first.duration_ms = None
    first.spans[0].status = "running"
    first.spans[0].completed_at = None
    first.spans[0].duration_ms = None
    first.events = [first.events[0]]

    second = _projection(now)
    second.events = [
        TraceEventProjection(
            event_id="event_late_collision",
            span_id="span_root",
            sequence=1,
            name="Late lifecycle event",
            event_type="status",
            occurred_at=now + timedelta(milliseconds=850),
        )
    ]

    assert recorder.record(context=_context(), projection=first, now=now)
    assert recorder.record(context=_context(), projection=second, now=now)

    with session_factory() as db:
        projected = get_trace_projection(
            db,
            trace_id=first.trace_id,
            owner_provider="clerk",
            owner_provider_user_id="user_admin",
            now=now,
        )

    assert projected is not None
    assert projected.status == "succeeded"
    assert projected.spans[0].status == "succeeded"
    assert projected.spans[0].completed_at == now + timedelta(milliseconds=850)
    assert [event.sequence for event in projected.events] == [1, 2]


def test_client_ui_events_promote_the_browser_action_to_trace_root(session_factory):
    now = datetime(2026, 6, 19, 20, 0, tzinfo=timezone.utc)
    recorder = ApiTraceRecorder(settings=_settings(), session_factory=session_factory)
    projection = _projection(now)
    assert recorder.record(context=_context(), projection=projection, now=now)

    with session_factory() as db:
        for event_type, status, offset in (
            ("ui.started", "running", 0),
            ("ui.completed", "completed", 900),
        ):
            append_client_trace_event(
                db,
                trace_id=projection.trace_id,
                owner_provider="clerk",
                owner_provider_user_id="user_admin",
                event=ClientTraceEventInput(
                    event_id=f"browser-{event_type}",
                    span_id="span_browser_action",
                    name="Generate product draft",
                    event_type=event_type,
                    status=status,
                    occurred_at=now + timedelta(milliseconds=offset),
                    attributes={"operation": "ui.action"},
                ),
                settings=_settings(),
                now=now + timedelta(milliseconds=offset),
            )
        projected = get_trace_projection(
            db,
            trace_id=projection.trace_id,
            owner_provider="clerk",
            owner_provider_user_id="user_admin",
            now=now,
        )

    assert projected is not None
    assert projected.root_span_id == "span_browser_action"
    assert projected.spans[0].span_id == "span_browser_action"
    assert projected.spans[1].parent_span_id == "span_browser_action"
    assert projected.status == "completed"
    assert projected.spans[0].status == "completed"


def test_client_ui_event_cannot_relabel_a_backend_span(session_factory):
    now = datetime(2026, 6, 19, 20, 0, tzinfo=timezone.utc)
    recorder = ApiTraceRecorder(settings=_settings(), session_factory=session_factory)
    projection = _projection(now)
    assert recorder.record(context=_context(), projection=projection, now=now)

    with session_factory() as db, pytest.raises(
        ValueError, match="browser action span"
    ):
        append_client_trace_event(
            db,
            trace_id=projection.trace_id,
            owner_provider="clerk",
            owner_provider_user_id="user_admin",
            event=ClientTraceEventInput(
                event_id="forged-ui-complete",
                span_id="span_openai",
                name="Relabel provider span",
                event_type="ui.completed",
                status="completed",
                occurred_at=now,
            ),
            settings=_settings(),
            now=now,
        )


def test_incremental_request_roots_attach_to_the_existing_trace_root(session_factory):
    now = datetime(2026, 6, 19, 20, 0, tzinfo=timezone.utc)
    recorder = ApiTraceRecorder(settings=_settings(), session_factory=session_factory)
    first = _projection(now)
    second = ApiTraceProjection(
        trace_id=first.trace_id,
        surface=first.surface,
        name=first.name,
        root_span_id="span_request_two",
        status="succeeded",
        started_at=now + timedelta(seconds=1),
        completed_at=now + timedelta(seconds=2),
        duration_ms=1000,
        spans=[
            TraceSpanProjection(
                span_id="span_request_two",
                name="Second browser request",
                operation="http.client",
                service="sterling-hollis-fe",
                status="succeeded",
                started_at=now + timedelta(seconds=1),
                completed_at=now + timedelta(seconds=2),
                duration_ms=1000,
            ),
            TraceSpanProjection(
                span_id="span_server_two",
                parent_span_id="span_request_two",
                name="Second API request",
                operation="http.server",
                service="sterling-hollis-be",
                status="succeeded",
                started_at=now + timedelta(seconds=1),
                completed_at=now + timedelta(seconds=2),
                duration_ms=1000,
            ),
        ],
    )

    assert recorder.record(context=_context(), projection=first, now=now)
    assert recorder.record(context=_context(), projection=second, now=now)

    with session_factory() as db:
        projected = get_trace_projection(
            db,
            trace_id=first.trace_id,
            owner_provider="clerk",
            owner_provider_user_id="user_admin",
            now=now,
        )

    assert projected is not None
    request_two = next(
        span for span in projected.spans if span.span_id == "span_request_two"
    )
    assert projected.root_span_id == first.root_span_id
    assert request_two.parent_span_id == first.root_span_id


def test_disabled_unauthorized_or_failed_recording_is_fail_open(session_factory):
    now = datetime(2026, 6, 19, 20, 0, tzinfo=timezone.utc)
    projection = _projection(now)
    disabled = ApiTraceRecorder(
        settings=_settings(api_trace_capture_enabled=False),
        session_factory=session_factory,
    )
    unauthorized = ApiTraceRecorder(settings=_settings(), session_factory=session_factory)

    assert disabled.record(context=_context(), projection=projection, now=now) is False
    assert (
        unauthorized.record(
            context=_context(authorized=False), projection=projection, now=now
        )
        is False
    )

    def failing_factory():
        raise RuntimeError("database unavailable")

    failed = ApiTraceRecorder(settings=_settings(), session_factory=failing_factory)
    assert failed.record(context=_context(), projection=projection, now=now) is False

    with session_factory() as business_db:
        business_db.add(
            SyntheticRun(
                id="business_request",
                seed=1,
                status="succeeded",
                started_at=now,
                config={},
            )
        )
        business_db.commit()
        assert business_db.get(SyntheticRun, "business_request") is not None
        assert business_db.get(ApiTrace, projection.trace_id) is None


def test_existing_trace_cannot_be_recorded_by_another_owner(session_factory):
    now = datetime(2026, 6, 19, 20, 0, tzinfo=timezone.utc)
    recorder = ApiTraceRecorder(settings=_settings(), session_factory=session_factory)
    projection = _projection(now)
    other_owner = TraceCaptureContext.authorized_for(
        owner_provider="clerk",
        owner_provider_user_id="user_other",
        surface="catalog",
    )

    assert recorder.record(context=_context(), projection=projection, now=now)
    assert recorder.record(context=other_owner, projection=projection, now=now) is False

    with session_factory() as db:
        trace = db.get(ApiTrace, projection.trace_id)
        assert trace is not None
        assert trace.owner_provider_user_id == "user_admin"


def test_projection_rejects_invalid_local_topology():
    now = datetime(2026, 6, 19, 20, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="root_span_id"):
        ApiTraceProjection(
            trace_id="trace_invalid",
            surface="catalog",
            name="Invalid",
            root_span_id="missing",
            status="failed",
            started_at=now,
            spans=[],
        )


def test_bound_capture_context_is_scoped_and_restored():
    context = _context()

    assert current_trace_capture_context() is None
    with bind_trace_capture_context(context):
        assert current_trace_capture_context() == context
    assert current_trace_capture_context() is None
