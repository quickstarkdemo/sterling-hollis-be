from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.catalog.workflow_schemas import WorkflowEventInput
from app.config import Settings
from app.database import Base
from app.models import CatalogProduct, CatalogWorkflowEvent, CatalogWorkflow, SyntheticRun
from app.services.auth.clerk import AuthenticatedPrincipal
from app.services.catalog_workflow import (
    append_workflow_event,
    cleanup_expired_workflow_payloads,
    get_catalog_workflow_projection,
    sanitize_workflow_payload,
    start_catalog_workflow,
)


@pytest.fixture
def db():
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
    session = testing_session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _settings(**overrides) -> Settings:
    defaults = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "catalog_studio_trace_retention_days": 7,
        "catalog_studio_trace_max_depth": 5,
        "catalog_studio_trace_max_string_length": 80,
        "catalog_studio_trace_max_array_length": 4,
        "catalog_studio_trace_max_object_keys": 12,
        "catalog_studio_trace_max_bytes": 4096,
        "catalog_studio_trace_redacted_keys": "internal_note",
        "catalog_studio_shared_workflows": False,
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def _principal(subject: str = "user_admin") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        provider="clerk",
        provider_user_id=subject,
        email=f"{subject}@example.com",
        claims={},
    )


def test_catalog_workflow_migration_preserves_rows_and_supports_downgrade(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'catalog-workflow.db'}", future=True)
    with engine.begin() as connection:
        connection.execute(text("create table catalog_draft_revisions (id varchar(64) primary key)"))
        connection.execute(text("create table image_generation_jobs (id varchar(64) primary key)"))
        connection.execute(text("create table catalog_products (id varchar(64) primary key)"))
        connection.execute(text("insert into catalog_products (id) values ('cat_existing')"))

        migration_path = (
            Path(__file__).parents[1]
            / "alembic/versions/e3f4a5b6c7d8_add_openai_demo_runs.py"
        )
        spec = importlib.util.spec_from_file_location("openai_demo_runs_migration", migration_path)
        assert spec and spec.loader
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        connection.execute(
            text(
                """
                insert into openai_demo_runs (
                    id, owner_provider, owner_provider_user_id, idempotency_key_hash,
                    request_hash, title, business_summary, status, current_stage,
                    next_event_sequence, created_at, updated_at, expires_at
                ) values (
                    'workflow_existing', 'clerk', 'user_admin', 'key_hash',
                    'request_hash', 'Existing workflow', 'Existing summary', 'started',
                    'run', 2, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            text(
                """
                insert into openai_demo_events (
                    id, run_id, client_event_id, input_hash, sequence, stage, capability,
                    status, business_summary, usage_json, moderation_json, request_json,
                    response_json, retryable, payload_expired, created_at
                ) values (
                    'event_existing', 'workflow_existing', 'workflow-started', 'event_hash',
                    1, 'run', 'run', 'started', 'Existing summary', '{}', '{}', '{}',
                    '{}', 0, 0, CURRENT_TIMESTAMP
                )
                """
            )
        )

        rename_path = (
            Path(__file__).parents[1]
            / "alembic/versions/f4a5b6c7d8e9_promote_catalog_workflows.py"
        )
        rename_spec = importlib.util.spec_from_file_location(
            "promote_catalog_workflows_migration", rename_path
        )
        assert rename_spec and rename_spec.loader
        rename_migration = importlib.util.module_from_spec(rename_spec)
        rename_spec.loader.exec_module(rename_migration)
        rename_migration.op = Operations(MigrationContext.configure(connection))
        rename_migration.upgrade()

        table_names = set(inspect(connection).get_table_names())
        catalog_count = connection.scalar(text("select count(*) from catalog_products"))
        workflow_count = connection.scalar(text("select count(*) from catalog_workflows"))
        event_parent = connection.scalar(
            text("select workflow_id from catalog_workflow_events where id = 'event_existing'")
        )
        migrated_stage = connection.execute(
            text(
                "select stage, capability from catalog_workflow_events "
                "where id = 'event_existing'"
            )
        ).one()
        inspector = inspect(connection)
        schema_object_names = {
            item["name"]
            for table_name in ("catalog_workflows", "catalog_workflow_events")
            for collection in (
                inspector.get_indexes(table_name),
                inspector.get_unique_constraints(table_name),
                inspector.get_check_constraints(table_name),
            )
            for item in collection
            if item.get("name")
        }

        assert {"catalog_workflows", "catalog_workflow_events"} <= table_names
        assert "openai_demo_runs" not in table_names
        assert "openai_demo_events" not in table_names
        assert catalog_count == 1
        assert workflow_count == 1
        assert event_parent == "workflow_existing"
        assert migrated_stage == ("workflow", "workflow")
        assert all("demo" not in name for name in schema_object_names)

        rename_migration.downgrade()
        downgraded_tables = set(inspect(connection).get_table_names())
        downgraded_parent = connection.scalar(
            text("select run_id from openai_demo_events where id = 'event_existing'")
        )
        downgraded_stage = connection.execute(
            text("select stage, capability from openai_demo_events where id = 'event_existing'")
        ).one()

    assert {"openai_demo_runs", "openai_demo_events"} <= downgraded_tables
    assert downgraded_parent == "workflow_existing"
    assert downgraded_stage == ("run", "run")


def test_start_run_records_owner_and_ordered_initial_event(db):
    workflow = start_catalog_workflow(
        db,
        principal=_principal(),
        title="Create the launch coat",
        business_summary="Preparing a new catalog product.",
        settings=_settings(),
        idempotency_key="start-launch-coat",
    )
    replay = start_catalog_workflow(
        db,
        principal=_principal(),
        title="Create the launch coat",
        business_summary="Preparing a new catalog product.",
        settings=_settings(),
        idempotency_key="start-launch-coat",
    )
    with pytest.raises(HTTPException) as conflict:
        start_catalog_workflow(
            db,
            principal=_principal(),
            title="A different launch coat",
            business_summary="Preparing a different product.",
            settings=_settings(),
            idempotency_key="start-launch-coat",
        )

    events = db.scalars(
        select(CatalogWorkflowEvent)
        .where(CatalogWorkflowEvent.workflow_id == workflow.id)
        .order_by(CatalogWorkflowEvent.sequence)
    ).all()
    assert workflow.owner_provider_user_id == "user_admin"
    assert replay.id == workflow.id
    assert conflict.value.status_code == 409
    assert workflow.next_event_sequence == 2
    assert [(event.sequence, event.stage, event.status) for event in events] == [
        (1, "workflow", "started")
    ]


def test_projection_preserves_allowlisted_developer_fields(db):
    settings = _settings()
    principal = _principal()
    workflow = start_catalog_workflow(
        db,
        principal=principal,
        title="Launch coat",
        business_summary="Run started.",
        settings=settings,
        idempotency_key="projection-workflow",
    )
    append_workflow_event(
        db,
        workflow_id=workflow.id,
        principal=principal,
        event=WorkflowEventInput(
            client_event_id="responses-1",
            stage="draft",
            capability="responses",
            status="succeeded",
            business_summary="Product details are ready for review.",
            model="gpt-5.4",
            request_id="req_demo_123",
            duration_ms=842,
            usage={
                "input_tokens": 120,
                "output_tokens": 48,
                "total_tokens": 168,
                "input_tokens_details": {"cached_tokens": 20},
                "output_tokens_details": {"reasoning_tokens": 6},
                "ignored": 999,
            },
            request_payload={"product": {"title": "Midnight Coat", "category": "outerwear"}},
            response_payload={"draft_id": "draft_123", "status": "ready"},
        ),
        settings=settings,
    )

    business = get_catalog_workflow_projection(
        db, workflow_id=workflow.id, principal=principal, developer=False, settings=settings
    )
    developer = get_catalog_workflow_projection(
        db, workflow_id=workflow.id, principal=principal, developer=True, settings=settings
    )

    assert business.events[-1].business_summary == "Product details are ready for review."
    assert business.events[-1].developer is None
    detail = developer.events[-1].developer
    assert detail is not None
    assert detail.model == "gpt-5.4"
    assert detail.request_id == "req_demo_123"
    assert detail.duration_ms == 842
    assert detail.usage == {
        "input_tokens": 120,
        "output_tokens": 48,
        "total_tokens": 168,
        "cached_tokens": 20,
        "reasoning_tokens": 6,
    }
    assert detail.request_payload["product"]["title"] == "Midnight Coat"
    assert detail.response_payload["draft_id"] == "draft_123"


def test_redaction_removes_sensitive_content_at_every_depth():
    settings = _settings()
    raw = {
        "model": "gpt-5.4",
        "headers": {"Authorization": "Bearer top-secret"},
        "system_prompt": "Never reveal this instruction",
        "raw_audio": b"voice-bytes",
        "image_bytes": "base64-image-secret",
        "internal_note": "configured-private-value",
        "product": {
            "title": "Safe Coat",
            "description": "A safe product description",
            "customer": {
                "email": "customer@example.com",
                "phone": "+15551234567",
            },
            "metadata": {
                "api_key": "sk-secret-token",
                "authorization": "Bearer nested-secret",
            },
        },
    }

    projected = sanitize_workflow_payload(raw, settings=settings)
    encoded = json.dumps(projected, sort_keys=True)

    assert projected["product"]["title"] == "Safe Coat"
    for secret in (
        "top-secret",
        "Never reveal this instruction",
        "voice-bytes",
        "base64-image-secret",
        "configured-private-value",
        "customer@example.com",
        "+15551234567",
        "sk-secret-token",
        "nested-secret",
    ):
        assert secret not in encoded
    assert "[REDACTED]" in encoded


def test_oversized_payloads_are_deterministically_bounded():
    settings = _settings(
        catalog_studio_trace_max_depth=2,
        catalog_studio_trace_max_string_length=12,
        catalog_studio_trace_max_array_length=2,
        catalog_studio_trace_max_object_keys=3,
        catalog_studio_trace_max_bytes=180,
    )
    raw = {
        "product": {
            "title": "A title that is much too long",
            "variants": [
                {"color": "black"},
                {"color": "ivory"},
                {"color": "rose"},
            ],
            "metadata": {"one": 1, "two": 2, "three": 3, "four": 4},
        },
        "response": {"result": {"nested": {"too": {"deep": True}}}},
    }

    first = sanitize_workflow_payload(raw, settings=settings)
    second = sanitize_workflow_payload(raw, settings=settings)
    encoded = json.dumps(first, sort_keys=True, separators=(",", ":"))

    assert first == second
    assert len(encoded.encode()) <= settings.catalog_studio_trace_max_bytes
    assert "truncated" in encoded.lower()

    string_limited = sanitize_workflow_payload(
        {"title": "A title that is much too long"},
        settings=_settings(catalog_studio_trace_max_string_length=12),
    )
    assert len(string_limited["title"]) <= 12


def test_run_ownership_and_shared_business_view(db):
    owner = _principal("owner")
    other = _principal("other")
    private_settings = _settings(catalog_studio_shared_workflows=False)
    workflow = start_catalog_workflow(
        db,
        principal=owner,
        title="Private workflow",
        business_summary="Private summary",
        settings=private_settings,
        idempotency_key="private-workflow",
    )

    with pytest.raises(HTTPException) as hidden:
        get_catalog_workflow_projection(
            db, workflow_id=workflow.id, principal=other, developer=False, settings=private_settings
        )
    assert hidden.value.status_code == 404

    shared_settings = _settings(catalog_studio_shared_workflows=True)
    shared = get_catalog_workflow_projection(
        db, workflow_id=workflow.id, principal=other, developer=False, settings=shared_settings
    )
    assert shared.title == "Private workflow"
    with pytest.raises(HTTPException) as forbidden:
        get_catalog_workflow_projection(
            db, workflow_id=workflow.id, principal=other, developer=True, settings=shared_settings
        )
    assert forbidden.value.status_code == 403


def test_retention_scrubs_payloads_without_deleting_catalog_records(db):
    now = datetime(2026, 6, 16, tzinfo=timezone.utc)
    settings = _settings(catalog_studio_trace_retention_days=1)
    principal = _principal()
    db.add(SyntheticRun(id="catalog_seed", seed=1, status="loaded", started_at=now, config={}))
    db.add(
        CatalogProduct(
            id="cat_retained",
            seed_run_id="catalog_seed",
            catalog_key="sterling|retained|outerwear",
            title="Retained Coat",
            description="Published catalog data remains.",
            brand="Sterling Hollis",
            category="outerwear",
            metadata_json={},
        )
    )
    db.commit()
    workflow = start_catalog_workflow(
        db,
        principal=principal,
        title="Expiring workflow",
        business_summary="Run started.",
        settings=settings,
        idempotency_key="expiring-workflow",
        published_product_id="cat_retained",
        now=now - timedelta(days=3),
    )
    append_workflow_event(
        db,
        workflow_id=workflow.id,
        principal=principal,
        event=WorkflowEventInput(
            client_event_id="old-event",
            stage="draft",
            capability="responses",
            status="succeeded",
            business_summary="Old payload.",
            request_payload={"product": {"title": "Old private draft"}},
            response_payload={"draft_id": "draft_old"},
        ),
        settings=settings,
        now=now - timedelta(days=2),
    )

    scrubbed = cleanup_expired_workflow_payloads(db, settings=settings, now=now)

    assert scrubbed == 2
    assert db.get(CatalogProduct, "cat_retained") is not None
    events = db.scalars(select(CatalogWorkflowEvent).where(CatalogWorkflowEvent.workflow_id == workflow.id)).all()
    assert events
    assert all(event.payload_expired for event in events)
    assert all(event.request_json == {"_retention": "expired"} for event in events)


def test_failed_stage_and_retry_append_history_idempotently(db):
    settings = _settings()
    principal = _principal()
    workflow = start_catalog_workflow(
        db,
        principal=principal,
        title="Retry workflow",
        business_summary="Run started.",
        settings=settings,
        idempotency_key="retry-workflow",
    )
    failed_input = WorkflowEventInput(
        client_event_id="image-attempt-1",
        stage="image",
        capability="image_generation",
        status="failed",
        business_summary="Image generation failed and can be retried.",
        error_code="provider_timeout",
        retryable=True,
    )
    first = append_workflow_event(
        db, workflow_id=workflow.id, principal=principal, event=failed_input, settings=settings
    )
    replay = append_workflow_event(
        db, workflow_id=workflow.id, principal=principal, event=failed_input, settings=settings
    )
    with pytest.raises(HTTPException) as conflict:
        append_workflow_event(
            db,
            workflow_id=workflow.id,
            principal=principal,
            event=failed_input.model_copy(
                update={"business_summary": "A different event reused the same ID."}
            ),
            settings=settings,
        )
    assert conflict.value.status_code == 409
    succeeded = append_workflow_event(
        db,
        workflow_id=workflow.id,
        principal=principal,
        event=WorkflowEventInput(
            client_event_id="image-attempt-2",
            stage="image",
            capability="image_generation",
            status="succeeded",
            business_summary="Image generation succeeded.",
        ),
        settings=settings,
    )

    assert replay.id == first.id
    assert (first.sequence, succeeded.sequence) == (2, 3)
    events = db.scalars(
        select(CatalogWorkflowEvent)
        .where(CatalogWorkflowEvent.workflow_id == workflow.id)
        .order_by(CatalogWorkflowEvent.sequence)
    ).all()
    assert [event.status for event in events] == ["started", "failed", "succeeded"]
