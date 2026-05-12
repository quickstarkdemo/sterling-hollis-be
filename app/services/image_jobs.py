from __future__ import annotations

import logging
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import CatalogProduct, ImageGenerationJob, ProductVariant, Store, SyntheticRun
from app.schemas import (
    ImageGenerationJobListResponse,
    ImageGenerationJobRequest,
    ImageGenerationJobResponse,
    IndexJobStatus,
)
from app.services.product_images import (
    ProductImageGenerationResult,
    ProductImageGenerator,
    product_image_options,
    query_variants_for_image_generation,
)

logger = logging.getLogger(__name__)
_last_admin_stale_recovery_at: float | None = None


def _to_response(job: ImageGenerationJob) -> ImageGenerationJobResponse:
    return ImageGenerationJobResponse(
        id=job.id,
        run_id=job.run_id,
        store_id=job.store_id,
        product_id=job.product_id,
        variant_id=job.variant_id,
        category=job.category,
        brand=job.brand,
        limit=job.limit,
        detail_count=job.detail_count,
        thumbnail_size=job.thumbnail_size,
        overwrite=job.overwrite,
        missing_images_only=job.missing_images_only,
        model=job.model,
        size=job.size,
        quality=job.quality,
        output_format=job.output_format,
        status=IndexJobStatus(job.status),
        attempted=job.attempted,
        generated=job.generated,
        skipped=job.skipped,
        failed_count=job.failed_count,
        status_breakdown=dict(job.status_breakdown or {}),
        result_sample=list(job.result_sample or []),
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        last_heartbeat_at=job.last_heartbeat_at,
        finished_at=job.finished_at,
    )


def _clean(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _validate_request(db: Session, req: ImageGenerationJobRequest) -> None:
    if req.run_id and not db.get(SyntheticRun, req.run_id):
        raise ValueError("run_id not found")
    if req.store_id and not db.get(Store, req.store_id):
        raise ValueError("store_id not found")
    if req.product_id and not db.get(CatalogProduct, req.product_id):
        raise ValueError("product_id not found")
    if req.variant_id and not db.get(ProductVariant, req.variant_id):
        raise ValueError("variant_id not found")


def enqueue_image_generation_job(db: Session, req: ImageGenerationJobRequest) -> ImageGenerationJobResponse:
    _validate_request(db, req)
    options = product_image_options(
        model=req.model,
        size=req.size,
        quality=req.quality,
        output_format=req.output_format,
        detail_count=req.detail_count,
        thumbnail_size=req.thumbnail_size,
        overwrite=req.overwrite,
        dry_run=False,
    )

    job = ImageGenerationJob(
        id=f"imgjob_{uuid.uuid4().hex[:12]}",
        run_id=_clean(req.run_id),
        store_id=_clean(req.store_id),
        product_id=_clean(req.product_id),
        variant_id=_clean(req.variant_id),
        category=_clean(req.category),
        brand=_clean(req.brand),
        limit=req.limit,
        detail_count=options.detail_count,
        thumbnail_size=options.thumbnail_size,
        overwrite=options.overwrite,
        missing_images_only=bool(req.missing_images_only and not req.overwrite),
        model=options.model,
        size=options.size,
        quality=options.quality,
        output_format=options.output_format,
        status=IndexJobStatus.queued.value,
        attempted=0,
        generated=0,
        skipped=0,
        failed_count=0,
        status_breakdown={},
        result_sample=[],
        created_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return _to_response(job)


def get_image_generation_job(db: Session, job_id: str) -> ImageGenerationJobResponse:
    maybe_recover_stale_image_generation_jobs(db)
    job = db.get(ImageGenerationJob, job_id)
    if not job:
        raise ValueError(f"Image generation job {job_id} was not found.")
    return _to_response(job)


def list_image_generation_jobs(
    db: Session,
    *,
    status: IndexJobStatus | None = None,
    limit: int = 20,
) -> ImageGenerationJobListResponse:
    maybe_recover_stale_image_generation_jobs(db)
    query = select(ImageGenerationJob)
    if status:
        query = query.where(ImageGenerationJob.status == status.value)
    jobs = db.scalars(query.order_by(ImageGenerationJob.created_at.desc()).limit(limit)).all()
    return ImageGenerationJobListResponse(jobs=[_to_response(job) for job in jobs])


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def recover_stale_image_generation_jobs(
    db: Session,
    *,
    stale_after_seconds: float | None = None,
    now: datetime | None = None,
) -> int:
    settings = get_settings()
    stale_after = (
        float(stale_after_seconds)
        if stale_after_seconds is not None
        else float(settings.product_image_job_stale_seconds)
    )
    if stale_after <= 0:
        return 0

    current_time = now or datetime.now(timezone.utc)
    deadline = current_time - timedelta(seconds=stale_after)
    running_jobs = db.scalars(
        select(ImageGenerationJob).where(ImageGenerationJob.status == IndexJobStatus.running.value)
    ).all()
    recovered = 0
    for job in running_jobs:
        activity_at = _aware_utc(job.last_heartbeat_at) or _aware_utc(job.started_at)
        if activity_at is None or activity_at >= deadline:
            continue
        job.status = IndexJobStatus.failed.value
        job.error_message = (
            "Image generation job became stale while running. "
            f"No heartbeat since {activity_at.isoformat()}."
        )
        job.finished_at = current_time
        db.add(job)
        recovered += 1
    if recovered:
        db.commit()
    return recovered


def maybe_recover_stale_image_generation_jobs(
    db: Session,
    *,
    min_interval_seconds: float | None = None,
    now_monotonic: float | None = None,
) -> int:
    global _last_admin_stale_recovery_at

    settings = get_settings()
    min_interval = (
        float(min_interval_seconds)
        if min_interval_seconds is not None
        else float(settings.image_job_admin_stale_recovery_seconds)
    )
    current_monotonic = now_monotonic if now_monotonic is not None else time.monotonic()
    if (
        min_interval > 0
        and _last_admin_stale_recovery_at is not None
        and current_monotonic - _last_admin_stale_recovery_at < min_interval
    ):
        return 0

    recovered = recover_stale_image_generation_jobs(db)
    _last_admin_stale_recovery_at = current_monotonic
    if recovered:
        logger.warning(
            "recovered stale image generation jobs during admin read",
            extra={"job_type": "image_generation", "job_status": "failed", "stale_recovered_count": recovered},
        )
    return recovered


def claim_next_image_generation_job(db: Session) -> ImageGenerationJob | None:
    candidate_id = db.scalar(
        select(ImageGenerationJob.id)
        .where(ImageGenerationJob.status == IndexJobStatus.queued.value)
        .order_by(ImageGenerationJob.created_at.asc())
        .limit(1)
    )
    if not candidate_id:
        return None

    started_at = datetime.now(timezone.utc)
    updated = db.execute(
        update(ImageGenerationJob)
        .where(ImageGenerationJob.id == candidate_id, ImageGenerationJob.status == IndexJobStatus.queued.value)
        .values(
            status=IndexJobStatus.running.value,
            started_at=started_at,
            last_heartbeat_at=started_at,
            error_message=None,
        )
    )
    db.commit()
    if not updated.rowcount:
        return None
    job = db.get(ImageGenerationJob, candidate_id)
    if job:
        logger.info(
            "claimed image generation job",
            extra={"job_type": "image_generation", "job_id": job.id, "job_status": job.status},
        )
    return job


def _sample_result(result: ProductImageGenerationResult) -> dict:
    return {
        "product_id": result.product_id,
        "variant_id": result.variant_id,
        "title": result.title,
        "status": result.status,
        "image_link": result.image_link,
        "thumbnail_link": result.thumbnail_link,
        "detail_links": result.detail_links or [],
        "error": result.error,
    }


def _final_status(results: list[ProductImageGenerationResult]) -> str:
    if results and all(result.status == "failed" for result in results):
        return IndexJobStatus.failed.value
    return IndexJobStatus.succeeded.value


def _status_counts(results: list[ProductImageGenerationResult]) -> Counter:
    return Counter(result.status for result in results)


def _update_running_job_progress(
    db: Session,
    *,
    job_id: str,
    results: list[ProductImageGenerationResult],
) -> ImageGenerationJob | None:
    job = db.scalar(
        select(ImageGenerationJob)
        .where(ImageGenerationJob.id == job_id)
        .execution_options(populate_existing=True)
    )
    if not job or job.status != IndexJobStatus.running.value:
        return job
    status_counts = _status_counts(results)
    job.last_heartbeat_at = datetime.now(timezone.utc)
    job.attempted = len(results)
    job.generated = status_counts.get("generated", 0)
    job.skipped = sum(count for status, count in status_counts.items() if status.startswith("skipped"))
    job.failed_count = status_counts.get("failed", 0)
    job.status_breakdown = dict(status_counts)
    job.result_sample = [_sample_result(result) for result in results[:25]]
    db.add(job)
    db.commit()
    return job


def process_image_generation_job(SessionLocal: sessionmaker, job_id: str) -> ImageGenerationJobResponse:
    with SessionLocal() as db:
        job = db.get(ImageGenerationJob, job_id)
        if not job:
            raise ValueError(f"Image generation job {job_id} was not found.")

        try:
            variants = query_variants_for_image_generation(
                db,
                run_id=job.run_id,
                store_id=job.store_id,
                product_ids=[job.product_id] if job.product_id else None,
                variant_ids=[job.variant_id] if job.variant_id else None,
                category=job.category,
                brand=job.brand,
                limit=job.limit,
                missing_images_only=job.missing_images_only and not job.overwrite,
            )
            results: list[ProductImageGenerationResult] = []
            if variants:
                options = product_image_options(
                    model=job.model,
                    size=job.size,
                    quality=job.quality,
                    output_format=job.output_format,
                    detail_count=job.detail_count,
                    thumbnail_size=job.thumbnail_size,
                    overwrite=job.overwrite,
                    dry_run=False,
                )
                generator = ProductImageGenerator(options)
                _update_running_job_progress(db, job_id=job_id, results=results)
                for variant in variants:
                    _update_running_job_progress(db, job_id=job_id, results=results)
                    results.append(generator.generate_for_variant(db, variant))
                    job = _update_running_job_progress(db, job_id=job_id, results=results)
                    if job and job.status != IndexJobStatus.running.value:
                        return _to_response(job)

            status_counts = _status_counts(results)
            failed_count = status_counts.get("failed", 0)
            job = db.scalar(
                select(ImageGenerationJob)
                .where(ImageGenerationJob.id == job_id)
                .execution_options(populate_existing=True)
            )
            if not job:
                raise ValueError(f"Image generation job {job_id} was not found.")
            if job.status != IndexJobStatus.running.value:
                return _to_response(job)
            job.status = _final_status(results)
            job.attempted = len(results)
            job.generated = status_counts.get("generated", 0)
            job.skipped = sum(count for status, count in status_counts.items() if status.startswith("skipped"))
            job.failed_count = failed_count
            job.status_breakdown = dict(status_counts)
            job.result_sample = [_sample_result(result) for result in results[:25]]
            job.error_message = "All image generations failed." if results and failed_count == len(results) else None
            job.last_heartbeat_at = datetime.now(timezone.utc)
            job.finished_at = datetime.now(timezone.utc)
            db.add(job)
            db.commit()
            logger.info(
                "image generation job completed",
                extra={"job_type": "image_generation", "job_id": job.id, "job_status": job.status},
            )
            return _to_response(job)
        except Exception as exc:
            job = db.get(ImageGenerationJob, job_id)
            if not job:
                raise
            job.status = IndexJobStatus.failed.value
            job.error_message = str(exc)[:2000]
            job.finished_at = datetime.now(timezone.utc)
            db.add(job)
            db.commit()
            logger.exception(
                "image generation job %s failed",
                job_id,
                extra={"job_type": "image_generation", "job_id": job_id, "job_status": job.status},
            )
            return _to_response(job)


def process_next_image_generation_job(SessionLocal: sessionmaker) -> ImageGenerationJobResponse | None:
    with SessionLocal() as db:
        job = claim_next_image_generation_job(db)
        if not job:
            return None
        job_id = job.id
    return process_image_generation_job(SessionLocal, job_id)
