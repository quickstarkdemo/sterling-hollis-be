from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.config import get_settings
from app.database import SessionLocal
from app.observability.logging import configure_datadog_logging
from app.services.image_jobs import process_next_image_generation_job, recover_stale_image_generation_jobs
from app.services.index_jobs import process_next_index_job


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerIntervals:
    poll_seconds: float
    max_idle_seconds: float
    stale_recovery_seconds: float


def _worker_intervals() -> WorkerIntervals:
    settings = get_settings()
    poll_seconds = max(0.1, float(getattr(settings, "index_worker_poll_seconds", 2.0)))
    max_idle_seconds = max(poll_seconds, float(getattr(settings, "index_worker_max_idle_seconds", 30.0)))
    stale_recovery_seconds = max(poll_seconds, float(getattr(settings, "index_worker_stale_recovery_seconds", 60.0)))
    return WorkerIntervals(
        poll_seconds=poll_seconds,
        max_idle_seconds=max_idle_seconds,
        stale_recovery_seconds=stale_recovery_seconds,
    )


def _idle_sleep_with_jitter(
    idle_sleep_seconds: float,
    *,
    random_fn: Callable[[float, float], float] = random.uniform,
) -> float:
    jitter_ceiling = min(0.25, max(0.0, idle_sleep_seconds * 0.1))
    return idle_sleep_seconds + random_fn(0.0, jitter_ceiling)


def _run_stale_image_job_recovery(session_factory) -> int:
    with session_factory() as db:
        recovered = recover_stale_image_generation_jobs(db)
    if recovered:
        logger.warning(
            "recovered stale image generation jobs",
            extra={"job_type": "image_generation", "job_status": "failed", "stale_recovered_count": recovered},
        )
    return recovered


def run_background_worker(
    *,
    session_factory=SessionLocal,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
    random_fn: Callable[[float, float], float] = random.uniform,
    stop_after_iterations: int | None = None,
) -> None:
    intervals = _worker_intervals()
    logger.info(
        "background worker started",
        extra={
            "poll_seconds": intervals.poll_seconds,
            "max_idle_seconds": intervals.max_idle_seconds,
            "stale_recovery_seconds": intervals.stale_recovery_seconds,
        },
    )
    idle_sleep_seconds = intervals.poll_seconds
    next_stale_recovery_at = 0.0
    iterations = 0

    while True:
        if stop_after_iterations is not None and iterations >= stop_after_iterations:
            return
        iterations += 1

        now = monotonic_fn()
        if now >= next_stale_recovery_at:
            _run_stale_image_job_recovery(session_factory)
            next_stale_recovery_at = now + intervals.stale_recovery_seconds

        image_result = process_next_image_generation_job(session_factory)
        if image_result is not None:
            idle_sleep_seconds = intervals.poll_seconds
            logger.info(
                "processed image generation job %s status=%s attempted=%s generated=%s skipped=%s failed=%s",
                image_result.id,
                image_result.status.value,
                image_result.attempted,
                image_result.generated,
                image_result.skipped,
                image_result.failed_count,
                extra={
                    "job_type": "image_generation",
                    "job_id": image_result.id,
                    "job_status": image_result.status.value,
                },
            )
            continue

        index_result = process_next_index_job(session_factory)
        if index_result is not None:
            idle_sleep_seconds = intervals.poll_seconds
            logger.info(
                "processed index job %s status=%s run_id=%s",
                index_result.id,
                index_result.status.value,
                index_result.run_id,
                extra={
                    "job_type": "index",
                    "job_id": index_result.id,
                    "job_status": index_result.status.value,
                },
            )
            continue

        sleep_seconds = _idle_sleep_with_jitter(idle_sleep_seconds, random_fn=random_fn)
        logger.info(
            "worker idle; backing off",
            extra={"idle_backoff_seconds": sleep_seconds},
        )
        sleep_fn(sleep_seconds)
        idle_sleep_seconds = min(intervals.max_idle_seconds, idle_sleep_seconds * 2)


def main() -> None:
    configure_datadog_logging()
    run_background_worker()


if __name__ == "__main__":
    main()
