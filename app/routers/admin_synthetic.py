from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import Product, ProductEmbedding, SyntheticRun
from app.schemas import (
    ImageGenerationJobListResponse,
    ImageGenerationJobRequest,
    ImageGenerationJobResponse,
    IndexProductsRequest,
    IndexProductsResponse,
    IndexJobStatus,
    RunReportResponse,
    SyntheticGenerateRequest,
    SyntheticGenerateResponse,
    SyntheticLoadRequest,
    SyntheticLoadResponse,
    VectorStatusResponse,
)
from app.services.image_jobs import (
    enqueue_image_generation_job,
    get_image_generation_job,
    list_image_generation_jobs,
)
from app.services.indexing import index_products_for_run
from app.services.loader import (
    assert_synthetic_tables_empty,
    current_loaded_counts,
    finalize_run,
    load_entity_csv,
    normalize_loaded_catalog,
    read_generated_counts,
    reset_synthetic_tables,
)
from app.services.store_source import fetch_store_snapshot, normalize_stores
from app.services.synthetic_generator import GenerationVolumes, generate_synthetic_dataset, new_run_id
from app.services.system_status import vector_status_payload
from app.services.validation import run_validation_checks

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/synthetic/generate", response_model=SyntheticGenerateResponse)
def generate_synthetic(req: SyntheticGenerateRequest, db: Session = Depends(get_db)):
    settings = get_settings()

    run_id = new_run_id()
    run = SyntheticRun(
        id=run_id,
        seed=req.seed,
        status="generating",
        started_at=datetime.now(timezone.utc),
        config={
            "seed": req.seed,
            "trailing_months": req.trailing_months,
            "volumes": req.volumes.model_dump(),
            "profile_overrides": req.profile_overrides,
        },
    )
    db.add(run)
    db.commit()

    try:
        snapshot = fetch_store_snapshot()
        normalized_stores = normalize_stores(snapshot=snapshot, seed_run_id=run_id)

        volumes = GenerationVolumes(**req.volumes.model_dump())
        artifacts = generate_synthetic_dataset(
            seed=req.seed,
            run_id=run_id,
            stores=normalized_stores,
            volumes=volumes,
            trailing_months=req.trailing_months,
            output_root=Path(settings.data_dir),
            raw_snapshot=snapshot,
        )

        run.status = "generated"
        run.completed_at = datetime.now(timezone.utc)
        db.add(run)
        db.commit()
    except Exception as exc:
        run.status = "failed"
        run.notes = str(exc)[:2000]
        db.add(run)
        db.commit()
        raise HTTPException(status_code=500, detail=f"Synthetic generation failed: {exc}") from exc

    return SyntheticGenerateResponse(
        run_id=run_id,
        seed=req.seed,
        output_dir=str(artifacts.output_dir),
        row_counts=artifacts.row_counts,
        stores_discovered=len(normalized_stores),
    )


@router.post("/synthetic/load", response_model=SyntheticLoadResponse)
def load_synthetic(req: SyntheticLoadRequest, db: Session = Depends(get_db)):
    settings = get_settings()
    run = db.get(SyntheticRun, req.run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run_id not found")

    # Ensure repeated runs are idempotent by clearing old synthetic data first.
    reset_synthetic_tables(db)
    assert_synthetic_tables_empty(db)

    # Enforce parent->child insert order regardless of request ordering.
    ordered_entities = [
        "stores",
        "customers",
        "products",
        "orders",
        "order_items",
        "store_daily_metrics",
        "supplier_product_offers",
    ]
    requested = set(req.entities)
    entities = [e for e in ordered_entities if e in requested]

    loaded_rows: dict[str, int] = {}
    for entity in entities:
        try:
            loaded_rows[entity] = load_entity_csv(db, req.run_id, Path(settings.data_dir), entity)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed loading {entity}: {exc}") from exc

    if "products" in loaded_rows:
        loaded_rows.update(normalize_loaded_catalog(db, req.run_id))

    run.status = "loaded"
    run.completed_at = datetime.now(timezone.utc)
    db.add(run)
    db.commit()

    return SyntheticLoadResponse(run_id=req.run_id, loaded_rows=loaded_rows)


@router.post("/synthetic/index-products", response_model=IndexProductsResponse)
def index_products(req: IndexProductsRequest, db: Session = Depends(get_db)):
    run = db.get(SyntheticRun, req.run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run_id not found")

    stats = index_products_for_run(db, run_id=req.run_id, batch_size=req.batch_size)
    run.status = "indexed"
    run.completed_at = datetime.now(timezone.utc)
    db.add(run)
    db.commit()

    return IndexProductsResponse(run_id=req.run_id, **stats)


@router.post("/product-images/generate", response_model=ImageGenerationJobResponse)
def generate_product_images(req: ImageGenerationJobRequest, db: Session = Depends(get_db)):
    try:
        return enqueue_image_generation_job(db, req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/product-images/jobs/{job_id}", response_model=ImageGenerationJobResponse)
def image_generation_job(job_id: str, db: Session = Depends(get_db)):
    try:
        return get_image_generation_job(db, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/product-images/jobs", response_model=ImageGenerationJobListResponse)
def image_generation_jobs(
    status: IndexJobStatus | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return list_image_generation_jobs(db, status=status, limit=limit)


@router.get("/system/vector-status", response_model=VectorStatusResponse)
def vector_status(probe: bool = Query(False)):
    return VectorStatusResponse(**vector_status_payload(probe=probe))


@router.get("/synthetic/runs/{run_id}/report", response_model=RunReportResponse)
def run_report(run_id: str, db: Session = Depends(get_db)):
    settings = get_settings()
    run = db.get(SyntheticRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run_id not found")

    generated_counts = read_generated_counts(Path(settings.data_dir), run_id)
    loaded_counts = current_loaded_counts(db, run_id)

    failure_count = run_validation_checks(db, run_id)

    product_count = db.scalar(select(func.count()).select_from(Product).where(Product.seed_run_id == run_id)) or 0
    embedding_total = (
        db.scalar(select(func.count()).select_from(ProductEmbedding).where(ProductEmbedding.seed_run_id == run_id)) or 0
    )
    embedding_indexed = (
        db.scalar(
            select(func.count())
            .select_from(ProductEmbedding)
            .where(ProductEmbedding.seed_run_id == run_id, ProductEmbedding.status.in_(["indexed", "local_only"]))
        )
        or 0
    )

    return RunReportResponse(
        run_id=run_id,
        status=run.status,
        generated_counts=generated_counts,
        loaded_counts=loaded_counts,
        embedding_coverage={
            "products": product_count,
            "embeddings": embedding_total,
            "indexed_or_local": embedding_indexed,
            "coverage_pct": round((embedding_indexed / product_count) * 100.0, 2) if product_count else 0.0,
        },
        validation_failures=failure_count,
        generated_at=run.started_at,
    )


@router.post("/seed/load/{entity}")
def seed_load_entity(entity: str, run_id: str = Query(...), db: Session = Depends(get_db)):
    settings = get_settings()
    if entity == "stores":
        reset_synthetic_tables(db)
    try:
        loaded = load_entity_csv(db, run_id, Path(settings.data_dir), entity)
        normalized = normalize_loaded_catalog(db, run_id) if entity == "products" else {}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"run_id": run_id, "entity": entity, "loaded_rows": loaded, **normalized}


@router.post("/seed/run/{seed_run_id}/finalize")
def seed_finalize(seed_run_id: str, status: str = Query("loaded"), db: Session = Depends(get_db)):
    try:
        finalize_run(db, seed_run_id, status=status)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"run_id": seed_run_id, "status": status}
