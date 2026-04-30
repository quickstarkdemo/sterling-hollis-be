#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.database import SessionLocal
from app.models import Product, ProductVariant, SupplierProductOffer


@dataclass
class RewriteStats:
    scanned_rows: int = 0
    touched_rows: int = 0
    rewritten_values: int = 0


def _normalized_base_url(value: str) -> str:
    cleaned = value.strip().rstrip("/")
    if not cleaned:
        raise ValueError("Base URL cannot be empty.")
    return cleaned


def _normalized_url_path(value: str) -> str:
    cleaned = value.strip() or "/product-images"
    return "/" + cleaned.strip("/")


def _rewrite_string(value: str, *, old_prefix: str, new_prefix: str) -> tuple[str, int]:
    if value.startswith(old_prefix):
        return new_prefix + value[len(old_prefix) :], 1
    return value, 0


def _rewrite_json(value: Any, *, old_prefix: str, new_prefix: str) -> tuple[Any, int]:
    if isinstance(value, str):
        return _rewrite_string(value, old_prefix=old_prefix, new_prefix=new_prefix)
    if isinstance(value, Mapping):
        rewritten: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            new_item, item_count = _rewrite_json(item, old_prefix=old_prefix, new_prefix=new_prefix)
            rewritten[str(key)] = new_item
            count += item_count
        return rewritten, count
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        rewritten_items: list[Any] = []
        count = 0
        for item in value:
            new_item, item_count = _rewrite_json(item, old_prefix=old_prefix, new_prefix=new_prefix)
            rewritten_items.append(new_item)
            count += item_count
        return rewritten_items, count
    return value, 0


def _rewrite_attr(row: Any, attr: str, *, old_prefix: str, new_prefix: str) -> int:
    value = getattr(row, attr)
    if value is None:
        return 0
    if isinstance(value, str):
        new_value, count = _rewrite_string(value, old_prefix=old_prefix, new_prefix=new_prefix)
    else:
        new_value, count = _rewrite_json(value, old_prefix=old_prefix, new_prefix=new_prefix)
    if count:
        setattr(row, attr, new_value)
    return count


def rewrite_product_image_urls(
    *,
    old_base_url: str,
    new_base_url: str,
    url_path: str,
    dry_run: bool,
    session_factory: sessionmaker = SessionLocal,
) -> RewriteStats:
    old_base = _normalized_base_url(old_base_url)
    new_base = _normalized_base_url(new_base_url)
    path = _normalized_url_path(url_path)
    old_prefix = f"{old_base}{path}"
    new_prefix = f"{new_base}{path}"
    stats = RewriteStats()

    with session_factory() as db:
        targets = (
            (ProductVariant, ("image_link", "image_set", "metadata_json")),
            (Product, ("image_link", "metadata_json")),
            (SupplierProductOffer, ("image_link", "metadata_json")),
        )
        for model, attrs in targets:
            for row in db.scalars(select(model)).all():
                stats.scanned_rows += 1
                row_rewrites = 0
                for attr in attrs:
                    row_rewrites += _rewrite_attr(row, attr, old_prefix=old_prefix, new_prefix=new_prefix)
                if row_rewrites:
                    stats.touched_rows += 1
                    stats.rewritten_values += row_rewrites
                    db.add(row)

        if dry_run:
            db.rollback()
        else:
            db.commit()

    return stats


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Rewrite stored product image URLs after a PUBLIC_BASE_URL hostname change."
    )
    parser.add_argument(
        "--old-base-url",
        default="https://products-api.quickstark.com",
        help="Previous public base URL to replace.",
    )
    parser.add_argument(
        "--new-base-url",
        default=settings.public_base_url,
        help="New public base URL. Defaults to PUBLIC_BASE_URL.",
    )
    parser.add_argument(
        "--url-path",
        default=settings.product_image_url_path,
        help="Image URL path to rewrite. Defaults to PRODUCT_IMAGE_URL_PATH.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report counts without committing changes.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    stats = rewrite_product_image_urls(
        old_base_url=args.old_base_url,
        new_base_url=args.new_base_url,
        url_path=args.url_path,
        dry_run=args.dry_run,
    )
    print(
        json.dumps(
            {
                **asdict(stats),
                "dry_run": bool(args.dry_run),
                "old_base_url": _normalized_base_url(args.old_base_url),
                "new_base_url": _normalized_base_url(args.new_base_url),
                "url_path": _normalized_url_path(args.url_path),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
