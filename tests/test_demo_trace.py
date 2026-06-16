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

from app.catalog.demo_schemas import DemoEventInput
from app.config import Settings
from app.database import Base
from app.models import CatalogProduct, OpenAIDemoEvent, OpenAIDemoRun, SyntheticRun
from app.services.auth.clerk import AuthenticatedPrincipal
from app.services.demo_trace import (
    append_demo_event,
    cleanup_expired_demo_payloads,
    get_demo_run_projection,
    sanitize_demo_payload,
    start_demo_run,
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
        "catalog_studio_shared_demo_runs": False,
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


def test_demo_trace_migration_adds_tables_without_changing_catalog_rows(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'demo-trace.db'}", future=True)
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

        table_names = set(inspect(connection).get_table_names())
        catalog_count = connection.scalar(text("select count(*) from catalog_products"))

    assert {"openai_demo_runs", "openai_demo_events"} <= table_names
    assert catalog_count == 1


def test_start_run_records_owner_and_ordered_initial_event(db):
    run = start_demo_run(
        db,
        principal=_principal(),
        title="Create the launch coat",
        business_summary="Preparing a new catalog product.",
        settings=_settings(),
        idempotency_key="start-launch-coat",
    )
    replay = start_demo_run(
        db,
        principal=_principal(),
        title="Create the launch coat",
        business_summary="Preparing a new catalog product.",
        settings=_settings(),
        idempotency_key="start-launch-coat",
    )
    with pytest.raises(HTTPException) as conflict:
        start_demo_run(
            db,
            principal=_principal(),
            title="A different launch coat",
            business_summary="Preparing a different product.",
            settings=_settings(),
            idempotency_key="start-launch-coat",
        )

    events = db.scalars(
        select(OpenAIDemoEvent)
        .where(OpenAIDemoEvent.run_id == run.id)
        .order_by(OpenAIDemoEvent.sequence)
    ).all()
    assert run.owner_provider_user_id == "user_admin"
    assert replay.id == run.id
    assert conflict.value.status_code == 409
    assert run.next_event_sequence == 2
    assert [(event.sequence, event.stage, event.status) for event in events] == [
        (1, "run", "started")
    ]


def test_projection_preserves_allowlisted_developer_fields(db):
    settings = _settings()
    principal = _principal()
    run = start_demo_run(
        db,
        principal=principal,
        title="Launch coat",
        business_summary="Run started.",
        settings=settings,
        idempotency_key="projection-run",
    )
    append_demo_event(
        db,
        run_id=run.id,
        principal=principal,
        event=DemoEventInput(
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

    business = get_demo_run_projection(
        db, run_id=run.id, principal=principal, developer=False, settings=settings
    )
    developer = get_demo_run_projection(
        db, run_id=run.id, principal=principal, developer=True, settings=settings
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

    projected = sanitize_demo_payload(raw, settings=settings)
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

    first = sanitize_demo_payload(raw, settings=settings)
    second = sanitize_demo_payload(raw, settings=settings)
    encoded = json.dumps(first, sort_keys=True, separators=(",", ":"))

    assert first == second
    assert len(encoded.encode()) <= settings.catalog_studio_trace_max_bytes
    assert "truncated" in encoded.lower()

    string_limited = sanitize_demo_payload(
        {"title": "A title that is much too long"},
        settings=_settings(catalog_studio_trace_max_string_length=12),
    )
    assert len(string_limited["title"]) <= 12


def test_run_ownership_and_shared_business_view(db):
    owner = _principal("owner")
    other = _principal("other")
    private_settings = _settings(catalog_studio_shared_demo_runs=False)
    run = start_demo_run(
        db,
        principal=owner,
        title="Private run",
        business_summary="Private summary",
        settings=private_settings,
        idempotency_key="private-run",
    )

    with pytest.raises(HTTPException) as hidden:
        get_demo_run_projection(
            db, run_id=run.id, principal=other, developer=False, settings=private_settings
        )
    assert hidden.value.status_code == 404

    shared_settings = _settings(catalog_studio_shared_demo_runs=True)
    shared = get_demo_run_projection(
        db, run_id=run.id, principal=other, developer=False, settings=shared_settings
    )
    assert shared.title == "Private run"
    with pytest.raises(HTTPException) as forbidden:
        get_demo_run_projection(
            db, run_id=run.id, principal=other, developer=True, settings=shared_settings
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
    run = start_demo_run(
        db,
        principal=principal,
        title="Expiring run",
        business_summary="Run started.",
        settings=settings,
        idempotency_key="expiring-run",
        published_product_id="cat_retained",
        now=now - timedelta(days=3),
    )
    append_demo_event(
        db,
        run_id=run.id,
        principal=principal,
        event=DemoEventInput(
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

    scrubbed = cleanup_expired_demo_payloads(db, settings=settings, now=now)

    assert scrubbed == 2
    assert db.get(CatalogProduct, "cat_retained") is not None
    events = db.scalars(select(OpenAIDemoEvent).where(OpenAIDemoEvent.run_id == run.id)).all()
    assert events
    assert all(event.payload_expired for event in events)
    assert all(event.request_json == {"_retention": "expired"} for event in events)


def test_failed_stage_and_retry_append_history_idempotently(db):
    settings = _settings()
    principal = _principal()
    run = start_demo_run(
        db,
        principal=principal,
        title="Retry run",
        business_summary="Run started.",
        settings=settings,
        idempotency_key="retry-run",
    )
    failed_input = DemoEventInput(
        client_event_id="image-attempt-1",
        stage="image",
        capability="image_generation",
        status="failed",
        business_summary="Image generation failed and can be retried.",
        error_code="provider_timeout",
        retryable=True,
    )
    first = append_demo_event(
        db, run_id=run.id, principal=principal, event=failed_input, settings=settings
    )
    replay = append_demo_event(
        db, run_id=run.id, principal=principal, event=failed_input, settings=settings
    )
    with pytest.raises(HTTPException) as conflict:
        append_demo_event(
            db,
            run_id=run.id,
            principal=principal,
            event=failed_input.model_copy(
                update={"business_summary": "A different event reused the same ID."}
            ),
            settings=settings,
        )
    assert conflict.value.status_code == 409
    succeeded = append_demo_event(
        db,
        run_id=run.id,
        principal=principal,
        event=DemoEventInput(
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
        select(OpenAIDemoEvent)
        .where(OpenAIDemoEvent.run_id == run.id)
        .order_by(OpenAIDemoEvent.sequence)
    ).all()
    assert [event.status for event in events] == ["started", "failed", "succeeded"]
