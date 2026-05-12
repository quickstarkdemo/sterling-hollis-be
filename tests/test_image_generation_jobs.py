from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.database import Base, get_db
from app.main import create_app
from app.models import ImageGenerationJob, Product, Store, SyntheticRun
from app.schemas import ImageGenerationJobRequest
from app.services.catalog_normalization import backfill_catalog_from_legacy_products
from app.services.image_jobs import (
    enqueue_image_generation_job,
    list_image_generation_jobs,
    maybe_recover_stale_image_generation_jobs,
    process_next_image_generation_job,
    recover_stale_image_generation_jobs,
)
from app.services.product_images import ProductImageGenerationResult


class _FakeProductImageGenerator:
    def __init__(self, options):
        self.options = options

    def generate_for_variant(self, db, variant):
        image_link = f"https://products.example/product-images/{variant.id}-detail-1.jpg"
        thumbnail_link = f"https://products.example/product-images/{variant.id}-thumb.jpg"
        variant.image_link = image_link
        variant.image_set = {
            "thumbnail_url": thumbnail_link,
            "primary_url": image_link,
            "detail_urls": [image_link],
        }
        db.add(variant)
        db.commit()
        return ProductImageGenerationResult(
            product_id=variant.catalog_product_id,
            variant_id=variant.id,
            title=variant.product.title,
            status="generated",
            image_link=image_link,
            thumbnail_link=thumbnail_link,
            detail_links=[image_link],
        )


def _product(product_id: str, **overrides) -> Product:
    defaults = {
        "id": product_id,
        "seed_run_id": "run_image_jobs",
        "store_id": "1001",
        "title": "Valentino Rose Silk Dress",
        "description": "Event-ready silk dress with soft rose color",
        "link": f"https://fashion.example/products/{product_id}",
        "image_link": f"https://fashion.example/images/{product_id}.jpg",
        "price": Decimal("750.00"),
        "availability": "in stock",
        "brand": "Valentino",
        "category": "womens_apparel",
        "color": "Rose",
        "size": "M",
        "material": "silk",
        "gender": "women",
        "season": "spring",
        "margin_pct": Decimal("0.6200"),
        "inventory_qty": 12,
        "objective_weight": Decimal("0.9000"),
        "metadata_json": {},
    }
    defaults.update(overrides)
    return Product(**defaults)


def _session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    with TestingSessionLocal() as session:
        _seed(session)
    return engine, TestingSessionLocal


def _seed(session):
    now = datetime(2026, 3, 14, tzinfo=timezone.utc)
    session.add(SyntheticRun(id="run_image_jobs", seed=1, status="loaded", started_at=now, config={}))
    session.add(
        Store(
            id="1001",
            seed_run_id="run_image_jobs",
            name="Dallas Downtown",
            city="Dallas",
            state="TX",
            postal_code="75201",
            address_line1="1 Main St",
            address_line2=None,
            phone=None,
            latitude=Decimal("32.770000"),
            longitude=Decimal("-96.790000"),
            profile_type="texas_core",
            services=[],
            raw_source={},
        )
    )
    session.add_all(
        [
            _product("prod_1"),
            _product("prod_2", color="Ivory", size="S"),
            _product("prod_3", category="shoes", title="Jimmy Choo Satin Pump", color="Gold", material="satin"),
        ]
    )
    session.commit()
    backfill_catalog_from_legacy_products(session, run_id="run_image_jobs")


@contextmanager
def _client(monkeypatch):
    monkeypatch.setenv("ENABLE_MCP_ADAPTER", "false")
    monkeypatch.setenv("ENABLE_OPENAI_APPS_UI", "false")
    get_settings.cache_clear()
    engine, TestingSessionLocal = _session_factory()
    app = create_app()

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app), TestingSessionLocal
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
        get_settings.cache_clear()


def test_image_generation_job_processes_normalized_variants(monkeypatch):
    monkeypatch.setattr("app.services.image_jobs.ProductImageGenerator", _FakeProductImageGenerator)
    engine, TestingSessionLocal = _session_factory()
    try:
        with TestingSessionLocal() as session:
            queued = enqueue_image_generation_job(
                session,
                ImageGenerationJobRequest(category="womens_apparel", limit=10, detail_count=2, thumbnail_size=256),
            )

        result = process_next_image_generation_job(TestingSessionLocal)

        assert result is not None
        assert result.id == queued.id
        assert result.status.value == "succeeded"
        assert result.attempted == 2
        assert result.generated == 2
        assert result.skipped == 0
        assert result.failed_count == 0
        assert result.status_breakdown == {"generated": 2}
        assert result.result_sample[0]["variant_id"].startswith("var_")
        assert result.result_sample[0]["thumbnail_link"].endswith("-thumb.jpg")
        assert result.last_heartbeat_at is not None

        with TestingSessionLocal() as session:
            enqueue_image_generation_job(
                session,
                ImageGenerationJobRequest(category="womens_apparel", limit=10),
            )
        second_result = process_next_image_generation_job(TestingSessionLocal)
        assert second_result is not None
        assert second_result.attempted == 0
        assert second_result.last_heartbeat_at is not None
    finally:
        engine.dispose()


def test_image_generation_job_api_enqueues_and_lists(monkeypatch):
    monkeypatch.setattr("app.services.image_jobs.ProductImageGenerator", _FakeProductImageGenerator)

    with _client(monkeypatch) as (client, TestingSessionLocal):
        response = client.post(
            "/admin/product-images/generate",
            json={"category": "womens_apparel", "limit": 1, "detail_count": 1},
        )
        assert response.status_code == 200
        job_id = response.json()["id"]
        assert response.json()["status"] == "queued"

        processed = process_next_image_generation_job(TestingSessionLocal)
        detail = client.get(f"/admin/product-images/jobs/{job_id}")
        listed = client.get("/admin/product-images/jobs", params={"limit": 10})

    assert processed is not None
    assert detail.status_code == 200
    assert detail.json()["status"] == "succeeded"
    assert detail.json()["generated"] == 1
    assert listed.status_code == 200
    assert [job["id"] for job in listed.json()["jobs"]] == [job_id]


def test_image_generation_job_can_complete_with_no_matching_variants(monkeypatch):
    engine, TestingSessionLocal = _session_factory()
    try:
        with TestingSessionLocal() as session:
            queued = enqueue_image_generation_job(
                session,
                ImageGenerationJobRequest(category="does_not_exist", limit=10),
            )

        result = process_next_image_generation_job(TestingSessionLocal)

        assert result is not None
        assert result.id == queued.id
        assert result.status.value == "succeeded"
        assert result.attempted == 0
        assert result.generated == 0
        assert result.result_sample == []
    finally:
        engine.dispose()


def test_stale_running_image_generation_job_is_failed(monkeypatch):
    engine, TestingSessionLocal = _session_factory()
    try:
        now = datetime(2026, 5, 2, 18, 0, tzinfo=timezone.utc)
        stale_at = now - timedelta(minutes=30)
        with TestingSessionLocal() as session:
            queued = enqueue_image_generation_job(
                session,
                ImageGenerationJobRequest(category="womens_apparel", limit=10),
            )
            job = session.get(ImageGenerationJob, queued.id)
            assert job is not None
            job.status = "running"
            job.started_at = stale_at
            job.last_heartbeat_at = stale_at
            session.add(job)
            session.commit()

            recovered = recover_stale_image_generation_jobs(
                session,
                stale_after_seconds=60,
                now=now,
            )
            refreshed = session.get(ImageGenerationJob, queued.id)

        assert recovered == 1
        assert refreshed is not None
        assert refreshed.status == "failed"
        assert refreshed.finished_at == now
        assert "No heartbeat since" in (refreshed.error_message or "")
    finally:
        engine.dispose()


def test_process_next_image_generation_job_does_not_recover_stale_jobs_on_empty_poll(monkeypatch):
    engine, TestingSessionLocal = _session_factory()
    calls = 0

    def recover(_db):
        nonlocal calls
        calls += 1
        return 0

    monkeypatch.setattr("app.services.image_jobs.recover_stale_image_generation_jobs", recover)
    try:
        result = process_next_image_generation_job(TestingSessionLocal)
    finally:
        engine.dispose()

    assert result is None
    assert calls == 0


def test_admin_stale_recovery_is_throttled(monkeypatch):
    import app.services.image_jobs as image_jobs

    engine, TestingSessionLocal = _session_factory()
    calls = 0

    def recover(_db):
        nonlocal calls
        calls += 1
        return 0

    monkeypatch.setattr(image_jobs, "_last_admin_stale_recovery_at", None)
    monkeypatch.setattr(image_jobs, "recover_stale_image_generation_jobs", recover)
    monkeypatch.setattr(image_jobs.time, "monotonic", lambda: 180.0)
    try:
        with TestingSessionLocal() as session:
            maybe_recover_stale_image_generation_jobs(session, min_interval_seconds=60, now_monotonic=100.0)
            maybe_recover_stale_image_generation_jobs(session, min_interval_seconds=60, now_monotonic=120.0)
            maybe_recover_stale_image_generation_jobs(session, min_interval_seconds=60, now_monotonic=161.0)
            list_image_generation_jobs(session)
    finally:
        monkeypatch.setattr(image_jobs, "_last_admin_stale_recovery_at", None)
        engine.dispose()

    assert calls == 2
