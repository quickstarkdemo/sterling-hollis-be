from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.services import catalog_cutover
from app.models import (
    CatalogDraftRevision,
    CatalogProduct,
    Customer,
    CustomerAuthIdentity,
    ImageGenerationJob,
    IndexJob,
    Product,
    ProductVariant,
    SyntheticRun,
)
from app.schemas import ImageGenerationJobRequest
from app.services.catalog_cutover import (
    CatalogCutoverBlockedError,
    cutover_synthetic_catalog,
    preflight_catalog_cutover,
)
from app.services.image_jobs import enqueue_image_generation_job
from app.services.synthetic_generator import GenerationVolumes, generate_synthetic_dataset


def _session():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    return engine, TestingSessionLocal()


def _stores(run_id: str) -> list[dict]:
    return [
        {
            "id": "1001",
            "seed_run_id": run_id,
            "name": "Dallas",
            "city": "Dallas",
            "state": "TX",
            "postal_code": "75201",
            "address_line1": "1 Main St",
            "address_line2": None,
            "phone": None,
            "latitude": 32.77,
            "longitude": -96.79,
            "profile_type": "texas_core",
            "services": [],
            "raw_source": {},
        },
        {
            "id": "1002",
            "seed_run_id": run_id,
            "name": "Miami",
            "city": "Miami",
            "state": "FL",
            "postal_code": "33131",
            "address_line1": "99 Ocean Dr",
            "address_line2": None,
            "phone": None,
            "latitude": 25.76,
            "longitude": -80.19,
            "profile_type": "resort_luxury",
            "services": [],
            "raw_source": {},
        },
    ]


def _generate_run(db, data_dir: Path, *, run_id: str, seed: int) -> None:
    now = datetime(2026, 6, 17, tzinfo=timezone.utc)
    db.add(SyntheticRun(id=run_id, seed=seed, status="generated", started_at=now, config={}))
    db.commit()
    generate_synthetic_dataset(
        seed=seed,
        run_id=run_id,
        stores=_stores(run_id),
        volumes=GenerationVolumes(
            stores=2,
            products=36,
            customers=12,
            orders=24,
            supplier_product_offers=8,
        ),
        trailing_months=3,
        output_root=data_dir,
        raw_snapshot={"stores": []},
        now=now,
    )


def test_cutover_dry_run_then_replaces_loaded_catalog_without_mixing_runs(tmp_path: Path):
    engine, db = _session()
    _generate_run(db, tmp_path, run_id="run_old", seed=101)
    _generate_run(db, tmp_path, run_id="run_new", seed=202)

    try:
        initial = cutover_synthetic_catalog(db, run_id="run_old", data_dir=tmp_path, execute=True)
        dry_run = cutover_synthetic_catalog(db, run_id="run_new", data_dir=tmp_path)
        before_ids = set(db.scalars(select(Product.seed_run_id).distinct()).all())

        completed = cutover_synthetic_catalog(db, run_id="run_new", data_dir=tmp_path, execute=True)
        after_ids = set(db.scalars(select(Product.seed_run_id).distinct()).all())
        catalog_count = db.scalar(select(func.count()).select_from(CatalogProduct))
        variant_count = db.scalar(select(func.count()).select_from(ProductVariant))
    finally:
        db.close()
        engine.dispose()

    assert initial.previous_run_id is None
    assert dry_run.dry_run is True
    assert dry_run.previous_run_id == "run_old"
    assert before_ids == {"run_old"}
    assert completed.dry_run is False
    assert completed.previous_run_id == "run_old"
    assert completed.rollback_command and "--run-id run_old" in completed.rollback_command
    assert "--allow-legacy-families" in completed.rollback_command
    assert after_ids == {"run_new"}
    assert catalog_count == completed.loaded_counts["catalog_products"]
    assert variant_count == completed.loaded_counts["product_variants"]


def test_cutover_refuses_catalog_studio_draft_history(tmp_path: Path):
    engine, db = _session()
    _generate_run(db, tmp_path, run_id="run_old", seed=101)
    _generate_run(db, tmp_path, run_id="run_new", seed=202)
    cutover_synthetic_catalog(db, run_id="run_old", data_dir=tmp_path, execute=True)
    product_id = db.scalar(select(CatalogProduct.id).limit(1))
    db.add(
        CatalogDraftRevision(
            id="draft_authored",
            catalog_product_id=product_id,
            base_version=1,
            status="draft",
            moderation_state="approved",
            snapshot_json={},
            created_by="admin_1",
        )
    )
    db.commit()

    try:
        preflight = preflight_catalog_cutover(db, run_id="run_new", data_dir=tmp_path)
        with pytest.raises(CatalogCutoverBlockedError, match="blocking product IDs"):
            cutover_synthetic_catalog(db, run_id="run_new", data_dir=tmp_path, execute=True)
        loaded_run_ids = set(db.scalars(select(Product.seed_run_id).distinct()).all())
    finally:
        db.close()
        engine.dispose()

    assert preflight.safe_to_execute is False
    assert preflight.blocking_product_ids == (product_id,)
    assert loaded_run_ids == {"run_old"}


def test_cutover_refuses_non_legacy_catalog_provenance(tmp_path: Path):
    engine, db = _session()
    _generate_run(db, tmp_path, run_id="run_old", seed=101)
    _generate_run(db, tmp_path, run_id="run_new", seed=202)
    cutover_synthetic_catalog(db, run_id="run_old", data_dir=tmp_path, execute=True)
    product = db.scalar(select(CatalogProduct).limit(1))
    product.metadata_json = {"source": "catalog_studio_responses"}
    db.add(product)
    db.commit()

    try:
        preflight = preflight_catalog_cutover(db, run_id="run_new", data_dir=tmp_path)
    finally:
        db.close()
        engine.dispose()

    assert preflight.safe_to_execute is False
    assert preflight.blocking_product_ids == (product.id,)


def test_cutover_blocks_active_legacy_image_jobs_and_detaches_completed_history(tmp_path: Path):
    engine, db = _session()
    _generate_run(db, tmp_path, run_id="run_old", seed=101)
    _generate_run(db, tmp_path, run_id="run_new", seed=202)
    cutover_synthetic_catalog(db, run_id="run_old", data_dir=tmp_path, execute=True)
    product = db.scalar(select(CatalogProduct).limit(1))
    variant = db.scalar(select(ProductVariant).where(ProductVariant.catalog_product_id == product.id).limit(1))
    inventory = variant.inventory[0]
    queued = enqueue_image_generation_job(
        db,
        ImageGenerationJobRequest(
            run_id="run_old",
            store_id=inventory.store_id,
            product_id=product.id,
            variant_id=variant.id,
            limit=1,
            overwrite=True,
            missing_images_only=False,
        ),
    )

    blocked = preflight_catalog_cutover(db, run_id="run_new", data_dir=tmp_path)
    job = db.get(ImageGenerationJob, queued.id)
    job.status = "succeeded"
    db.add(job)
    db.commit()

    try:
        completed = cutover_synthetic_catalog(db, run_id="run_new", data_dir=tmp_path, execute=True)
        historical_job = db.get(ImageGenerationJob, queued.id)
    finally:
        db.close()
        engine.dispose()

    assert blocked.safe_to_execute is False
    assert any(queued.id in blocker for blocker in blocked.blockers)
    assert completed.previous_run_id == "run_old"
    assert historical_job.run_id == "run_old"
    assert historical_job.store_id is None
    assert historical_job.product_id is None
    assert historical_job.variant_id is None


def test_cutover_can_enqueue_image_and_index_followup_jobs(tmp_path: Path):
    engine, db = _session()
    _generate_run(db, tmp_path, run_id="run_new", seed=202)

    try:
        result = cutover_synthetic_catalog(
            db,
            run_id="run_new",
            data_dir=tmp_path,
            execute=True,
            enqueue_images=True,
            enqueue_index=True,
        )
        image_jobs = db.scalars(select(ImageGenerationJob).order_by(ImageGenerationJob.id)).all()
        index_job = db.get(IndexJob, result.index_job_id)
        blocked_followup = preflight_catalog_cutover(db, run_id="run_new", data_dir=tmp_path)
    finally:
        db.close()
        engine.dispose()

    assert result.image_job_ids
    assert {job.id for job in image_jobs} == set(result.image_job_ids)
    assert all(job.run_id == "run_new" and job.overwrite for job in image_jobs)
    assert index_job is not None
    assert index_job.run_id == "run_new"
    assert index_job.status == "queued"
    assert any("active index jobs" in blocker for blocker in blocked_followup.blockers)


def test_cutover_failure_after_reset_reports_retained_run_rollback(tmp_path: Path, monkeypatch):
    engine, db = _session()
    _generate_run(db, tmp_path, run_id="run_old", seed=101)
    _generate_run(db, tmp_path, run_id="run_new", seed=202)
    cutover_synthetic_catalog(db, run_id="run_old", data_dir=tmp_path, execute=True)
    real_load = catalog_cutover.load_entity_csv

    def fail_on_products(session, run_id, data_dir, entity):
        if entity == "products":
            raise ValueError("simulated product load failure")
        return real_load(session, run_id, data_dir, entity)

    monkeypatch.setattr(catalog_cutover, "load_entity_csv", fail_on_products)

    try:
        with pytest.raises(RuntimeError, match=r"Rollback with: .*--run-id run_old"):
            cutover_synthetic_catalog(db, run_id="run_new", data_dir=tmp_path, execute=True)
    finally:
        db.close()
        engine.dispose()


def test_preflight_rejects_path_traversal_run_id(tmp_path: Path):
    engine, db = _session()
    try:
        preflight = preflight_catalog_cutover(db, run_id="../outside", data_dir=tmp_path)
    finally:
        db.close()
        engine.dispose()

    assert preflight.safe_to_execute is False
    assert "run_id must be one path-safe name" in preflight.blockers


def test_preflight_refuses_customer_identity_state_that_global_reset_cannot_preserve(tmp_path: Path):
    engine, db = _session()
    _generate_run(db, tmp_path, run_id="run_old", seed=101)
    _generate_run(db, tmp_path, run_id="run_new", seed=202)
    cutover_synthetic_catalog(db, run_id="run_old", data_dir=tmp_path, execute=True)
    customer_id = db.scalar(select(Customer.id).limit(1))
    db.add(
        CustomerAuthIdentity(
            id="identity_1",
            provider="clerk",
            provider_user_id="user_1",
            customer_id=customer_id,
            email="customer@example.com",
        )
    )
    db.commit()

    try:
        preflight = preflight_catalog_cutover(db, run_id="run_new", data_dir=tmp_path)
    finally:
        db.close()
        engine.dispose()

    assert preflight.safe_to_execute is False
    assert any("customer auth identities" in blocker for blocker in preflight.blockers)
