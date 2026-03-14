from __future__ import annotations

import logging

from app.database import SessionLocal
from app.services.index_jobs import run_index_worker


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_index_worker(SessionLocal)


if __name__ == "__main__":
    main()
