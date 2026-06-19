from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api_traces.adapters import (
    catalog_workflow_event_projection,
    queue_catalog_workflow_event,
)
from app.api_traces.context import TraceCaptureContext, bind_trace_capture_context
from app.config import Settings
from app.database import Base
from app.models import CatalogWorkflow, CatalogWorkflowEvent


NOW = datetime(2026, 6, 19, 22, 0, tzinfo=timezone.utc)


def _context(*, trace_id: str = "1" * 32, span_id: str = "3" * 16):
    return TraceCaptureContext(
        owner_provider="clerk",
        owner_provider_user_id="owner_a",
        surface="catalog-studio",
        authorized=True,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id="2" * 16,
    )


def _workflow():
    return SimpleNamespace(
        id="workflow_1",
        title="Build the launch product",
        owner_provider="clerk",
        owner_provider_user_id="owner_a",
    )


def _event(**overrides):
    values = {
        "id": "event_1",
        "sequence": 4,
        "stage": "draft",
        "capability": "responses",
        "status": "succeeded",
        "business_summary": "Generated a product draft.",
        "model": "gpt-5.4",
        "request_id": "req_provider_1",
        "duration_ms": 120,
        "usage_json": {"input_tokens": 10, "output_tokens": 20},
        "moderation_json": {"flagged": False},
        "request_json": {"client_request_id": "client_1", "input": {"action": "draft"}},
        "response_json": {"response_id": "resp_1", "status": "ready"},
        "error_code": None,
        "retryable": False,
        "started_at": NOW,
        "completed_at": NOW,
        "created_at": NOW,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_catalog_response_event_projects_browser_api_and_openai_lifecycle():
    projection = catalog_workflow_event_projection(
        workflow=_workflow(),
        event=_event(),
        context=_context(),
    )

    assert projection.trace_id == "1" * 32
    assert projection.root_span_id == "2" * 16
    assert [span.operation for span in projection.spans] == [
        "http.client",
        "http.server",
        "openai.responses",
        "catalog.validation",
        "catalog.persistence",
        "ui.ready",
    ]
    assert projection.events[0].event_type == "openai.responses.succeeded"
    assert projection.spans[2].attributes["provider_request_id"] == "req_provider_1"
    assert projection.spans[2].attributes["usage"]["output_tokens"] == 20
    assert projection.events[0].span_id == projection.spans[2].span_id


def test_image_worker_is_a_separate_trace_linked_to_the_initiating_request():
    job = SimpleNamespace(
        id="imgjob_1",
        attempted=1,
        api_trace_id="a" * 32,
        api_trace_span_id="b" * 16,
        api_trace_retry_of_job_id="imgjob_previous",
    )
    worker_context = TraceCaptureContext(
        owner_provider="clerk",
        owner_provider_user_id="owner_a",
        surface="catalog-studio",
        authorized=True,
        trace_id="c" * 32,
        span_id="d" * 16,
    )
    projection = catalog_workflow_event_projection(
        workflow=_workflow(),
        event=_event(
            id="event_image",
            stage="image",
            capability="image_generation",
            business_summary="Generated an image.",
            response_json={
                "image_url": "https://example.test/image.png",
                "approval_status": "review",
            },
        ),
        context=worker_context,
        image_job=job,
    )

    assert projection.trace_id == "c" * 32
    assert projection.spans[0].operation == "worker.image_generation"
    assert projection.links[0].linked_trace_id == "a" * 32
    assert projection.links[0].linked_span_id == "b" * 16
    assert projection.links[0].relationship == "initiated_by"
    assert projection.links[1].relationship == "retry_of"
    assert projection.links[1].attributes["image_job_id"] == "imgjob_previous"
    assert projection.artifacts[0].artifact_type == "image"


def test_publication_event_expands_to_validation_persistence_and_publication_spans():
    projection = catalog_workflow_event_projection(
        workflow=_workflow(),
        event=_event(
            id="event_publish",
            stage="publication",
            capability="publication",
            business_summary="Published the product.",
        ),
        context=_context(),
    )

    assert [span.operation for span in projection.spans[-3:]] == [
        "catalog.validation",
        "catalog.persistence",
        "catalog.publication",
    ]
    assert projection.events[0].event_type == "catalog.publication.succeeded"


def test_catalog_projection_is_recorded_only_after_business_commit(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        api_trace_capture_enabled=True,
    )
    recorded = []

    class FakeRecorder:
        def __init__(self, *, settings):
            self.settings = settings

        def record(self, *, context, projection):
            recorded.append((context, projection))
            return True

    monkeypatch.setattr("app.api_traces.adapters.ApiTraceRecorder", FakeRecorder)
    workflow = CatalogWorkflow(
        id="workflow_commit",
        owner_provider="clerk",
        owner_provider_user_id="owner_a",
        idempotency_key_hash="a" * 64,
        request_hash="b" * 64,
        title="Committed workflow",
        business_summary="Safe summary",
        status="started",
        current_stage="workflow",
        next_event_sequence=2,
        created_at=NOW,
        updated_at=NOW,
        expires_at=NOW,
    )
    event = CatalogWorkflowEvent(
        id="event_commit",
        workflow_id=workflow.id,
        client_event_id="c" * 64,
        input_hash="d" * 64,
        sequence=1,
        stage="workflow",
        capability="workflow",
        status="started",
        business_summary="Started the workflow.",
        usage_json={},
        moderation_json={},
        request_json={},
        response_json={},
        retryable=False,
        payload_expired=False,
        created_at=NOW,
    )

    with factory() as db, bind_trace_capture_context(_context()):
        db.add_all([workflow, event])
        assert queue_catalog_workflow_event(
            db,
            workflow=workflow,
            event=event,
            settings=settings,
        )
        assert recorded == []
        db.commit()

    assert len(recorded) == 1
    assert recorded[0][1].trace_id == "1" * 32
    engine.dispose()
