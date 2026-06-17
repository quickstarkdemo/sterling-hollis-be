#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from app.config import get_settings
from app.database import SessionLocal
from app.services.catalog_cutover import CatalogCutoverBlockedError, cutover_synthetic_catalog


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preflight or replace the loaded synthetic catalog with one generated run."
    )
    parser.add_argument("--run-id", required=True, help="Generated synthetic run to validate and load.")
    parser.add_argument("--data-dir", help="Synthetic artifact root. Defaults to DATA_DIR.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform the destructive reset and load. Without this flag, only preflight runs.",
    )
    parser.add_argument(
        "--enqueue-images",
        action="store_true",
        help="After cutover, enqueue overwrite image jobs grouped by category and brand.",
    )
    parser.add_argument(
        "--enqueue-index",
        action="store_true",
        help="After cutover, enqueue semantic indexing for the replacement run.",
    )
    parser.add_argument(
        "--allow-legacy-families",
        action="store_true",
        help="Permit a retained pre-U14 run without multi-color families. Intended only for rollback.",
    )
    args = parser.parse_args()
    if not args.execute and (args.enqueue_images or args.enqueue_index):
        parser.error("--enqueue-images and --enqueue-index require --execute")
    return args


def main() -> None:
    args = parse_args()
    settings = get_settings()
    data_dir = Path(args.data_dir or settings.data_dir).expanduser().resolve()
    try:
        with SessionLocal() as db:
            result = cutover_synthetic_catalog(
                db,
                run_id=args.run_id,
                data_dir=data_dir,
                execute=args.execute,
                enqueue_images=args.enqueue_images,
                enqueue_index=args.enqueue_index,
                require_coherent_families=not args.allow_legacy_families,
            )
    except CatalogCutoverBlockedError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, indent=2))
        raise SystemExit(2) from exc

    print(json.dumps({"status": "ready" if result.dry_run else "completed", **asdict(result)}, indent=2))


if __name__ == "__main__":
    main()
