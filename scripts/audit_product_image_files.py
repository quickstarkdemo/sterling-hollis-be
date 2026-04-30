#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.database import SessionLocal
from app.models import Product, ProductVariant, SupplierProductOffer


@dataclass
class AuditStats:
    referenced_files: int = 0
    existing_files: int = 0
    missing_files: int = 0


def _normalized_url_path(value: str) -> str:
    cleaned = value.strip() or "/product-images"
    return "/" + cleaned.strip("/")


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            yield from _iter_strings(item)


def _image_filename(value: str, *, url_path: str) -> str | None:
    parsed = urlparse(value)
    path = unquote(parsed.path if parsed.scheme or parsed.netloc else value)
    prefix = url_path.rstrip("/") + "/"
    if not path.startswith(prefix):
        return None
    filename = path[len(prefix) :].strip("/")
    if not filename or "/" in filename:
        return None
    return filename


def referenced_product_image_files(
    *,
    url_path: str,
    session_factory: sessionmaker = SessionLocal,
) -> set[str]:
    normalized_path = _normalized_url_path(url_path)
    filenames: set[str] = set()
    targets = (
        (ProductVariant, ("image_link", "image_set", "metadata_json")),
        (Product, ("image_link", "metadata_json")),
        (SupplierProductOffer, ("image_link", "metadata_json")),
    )
    with session_factory() as db:
        for model, attrs in targets:
            for row in db.scalars(select(model)).all():
                for attr in attrs:
                    for value in _iter_strings(getattr(row, attr)):
                        filename = _image_filename(value, url_path=normalized_path)
                        if filename:
                            filenames.add(filename)
    return filenames


def audit_product_image_files(
    *,
    image_dir: Path,
    url_path: str,
    session_factory: sessionmaker = SessionLocal,
) -> tuple[AuditStats, list[str]]:
    referenced = referenced_product_image_files(url_path=url_path, session_factory=session_factory)
    missing = sorted(filename for filename in referenced if not (image_dir / filename).is_file())
    stats = AuditStats(
        referenced_files=len(referenced),
        existing_files=len(referenced) - len(missing),
        missing_files=len(missing),
    )
    return stats, missing


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Audit product image files referenced by the database.")
    parser.add_argument(
        "--image-dir",
        default=settings.product_image_output_dir,
        help="Directory mounted by PRODUCT_IMAGE_URL_PATH. Defaults to PRODUCT_IMAGE_OUTPUT_DIR.",
    )
    parser.add_argument(
        "--url-path",
        default=settings.product_image_url_path,
        help="Image URL path to audit. Defaults to PRODUCT_IMAGE_URL_PATH.",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=50,
        help="Maximum missing filenames to include in output.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    stats, missing = audit_product_image_files(
        image_dir=Path(args.image_dir),
        url_path=args.url_path,
    )
    sample_limit = max(0, args.sample_limit)
    print(
        json.dumps(
            {
                **asdict(stats),
                "image_dir": str(Path(args.image_dir)),
                "url_path": _normalized_url_path(args.url_path),
                "missing_sample": missing[:sample_limit],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
