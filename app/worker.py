from __future__ import annotations

import logging
import time

from app.config import get_settings
from app.database import SessionLocal
from app.observability.logging import configure_datadog_logging
from app.services.image_jobs import process_next_image_generation_job
from app.services.index_jobs import process_next_index_job


logger = logging.getLogger(__name__)


def run_background_worker() -> None:
    settings = get_settings()
    poll_seconds = getattr(settings, "index_worker_poll_seconds", 2.0)
    logger.info("background worker started with poll interval %.2fs", poll_seconds)
    while True:
        image_result = process_next_image_generation_job(SessionLocal)
        if image_result is not None:
            logger.info(
                "processed image generation job %s status=%s attempted=%s generated=%s skipped=%s failed=%s",
                image_result.id,
                image_result.status.value,
                image_result.attempted,
                image_result.generated,
                image_result.skipped,
                image_result.failed_count,
            )
            continue

        index_result = process_next_index_job(SessionLocal)
        if index_result is not None:
            logger.info(
                "processed index job %s status=%s run_id=%s",
                index_result.id,
                index_result.status.value,
                index_result.run_id,
            )
            continue

        time.sleep(poll_seconds)


def main() -> None:
    configure_datadog_logging()
    run_background_worker()


if __name__ == "__main__":
    main()
