from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import IndexJob, SyntheticRun
from app.schemas import IndexJobListResponse, IndexJobResponse, IndexJobStatus
from app.services.indexing import index_products_for_run

logger = logging.getLogger(__name__)


def _to_response(job: IndexJob) -> IndexJobResponse:
    return IndexJobResponse(
        id=job.id,
        run_id=job.run_id,
        batch_size=job.batch_size,
        status=IndexJobStatus(job.status),
        attempted=job.attempted,
        indexed=job.indexed,
        failed_count=job.failed_count,
        status_breakdown=dict(job.status_breakdown or {}),
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def enqueue_index_job(db: Session, run_id: str, batch_size: int = 128) -> IndexJobResponse:
    run = db.get(SyntheticRun, run_id)
    if not run:
        raise ValueError("run_id not found")

    existing = db.scalar(
        select(IndexJob)
        .where(IndexJob.run_id == run_id, IndexJob.status.in_([IndexJobStatus.queued.value, IndexJobStatus.running.value]))
        .order_by(IndexJob.created_at.desc())
        .limit(1)
    )
    if existing:
        return _to_response(existing)

    job = IndexJob(
        id=f"idxjob_{uuid.uuid4().hex[:12]}",
        run_id=run_id,
        batch_size=batch_size,
        status=IndexJobStatus.queued.value,
        attempted=0,
        indexed=0,
        failed_count=0,
        status_breakdown={},
        created_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return _to_response(job)


def get_index_job(db: Session, job_id: str) -> IndexJobResponse:
    job = db.get(IndexJob, job_id)
    if not job:
        raise ValueError(f"Index job {job_id} was not found.")
    return _to_response(job)


def list_index_jobs(db: Session, run_id: str | None = None, limit: int = 20) -> IndexJobListResponse:
    query = select(IndexJob)
    if run_id:
        query = query.where(IndexJob.run_id == run_id)
    jobs = db.scalars(query.order_by(IndexJob.created_at.desc()).limit(limit)).all()
    return IndexJobListResponse(jobs=[_to_response(job) for job in jobs])


def claim_next_index_job(db: Session) -> IndexJob | None:
    candidate_id = db.scalar(
        select(IndexJob.id)
        .where(IndexJob.status == IndexJobStatus.queued.value)
        .order_by(IndexJob.created_at.asc())
        .limit(1)
    )
    if not candidate_id:
        return None

    started_at = datetime.now(timezone.utc)
    updated = db.execute(
        update(IndexJob)
        .where(IndexJob.id == candidate_id, IndexJob.status == IndexJobStatus.queued.value)
        .values(status=IndexJobStatus.running.value, started_at=started_at, error_message=None)
    )
    db.commit()
    if not updated.rowcount:
        return None
    return db.get(IndexJob, candidate_id)


def process_index_job(SessionLocal: sessionmaker, job_id: str) -> IndexJobResponse:
    with SessionLocal() as db:
        job = db.get(IndexJob, job_id)
        if not job:
            raise ValueError(f"Index job {job_id} was not found.")

        run = db.get(SyntheticRun, job.run_id)
        if not run:
            job.status = IndexJobStatus.failed.value
            job.error_message = f"run_id {job.run_id} not found"
            job.finished_at = datetime.now(timezone.utc)
            db.add(job)
            db.commit()
            return _to_response(job)

        try:
            stats = index_products_for_run(db, run_id=job.run_id, batch_size=job.batch_size)
            job.status = IndexJobStatus.succeeded.value
            job.attempted = stats["attempted"]
            job.indexed = stats["indexed"]
            job.failed_count = stats["failed"]
            job.status_breakdown = stats["status_breakdown"]
            job.error_message = None
            job.finished_at = datetime.now(timezone.utc)

            run.status = "indexed"
            run.completed_at = job.finished_at
            db.add(run)
            db.add(job)
            db.commit()
            return _to_response(job)
        except Exception as exc:
            job.status = IndexJobStatus.failed.value
            job.error_message = str(exc)[:2000]
            job.finished_at = datetime.now(timezone.utc)
            db.add(job)
            db.commit()
            logger.exception("index job %s failed", job.id)
            return _to_response(job)


def process_next_index_job(SessionLocal: sessionmaker) -> IndexJobResponse | None:
    with SessionLocal() as db:
        job = claim_next_index_job(db)
        if not job:
            return None
        job_id = job.id
    return process_index_job(SessionLocal, job_id)


def run_index_worker(SessionLocal: sessionmaker) -> None:
    settings = get_settings()
    poll_seconds = getattr(settings, "index_worker_poll_seconds", 2.0)
    logger.info("index worker started with poll interval %.2fs", poll_seconds)
    while True:
        result = process_next_index_job(SessionLocal)
        if result is None:
            time.sleep(poll_seconds)
            continue
        logger.info("processed index job %s status=%s run_id=%s", result.id, result.status.value, result.run_id)
