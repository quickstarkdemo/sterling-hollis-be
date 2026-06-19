#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from app.database import SessionLocal
from app.services.product_reviews import import_product_review


DEFAULT_REVIEWS = [
    {
        "external_review_id": "demo-quality-1",
        "author_display_name": "Maya R.",
        "body": "The material feels substantial and the finish is even better in person.",
        "rating": 5,
    },
    {
        "external_review_id": "demo-fit-1",
        "author_display_name": "Jordan T.",
        "body": "Beautiful product, but the fit ran smaller than I expected.",
        "rating": 3,
    },
    {
        "external_review_id": "demo-service-1",
        "author_display_name": "Alex P.",
        "body": "The store team helped me find the right size and the exchange was easy.",
        "rating": 4,
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import trusted product-review fixtures.")
    parser.add_argument("--product-id", required=True)
    parser.add_argument("--input", type=Path, help="Optional JSON array of trusted review fixtures.")
    parser.add_argument("--source", default="synthetic_fixture")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    reviews = json.loads(args.input.read_text()) if args.input else DEFAULT_REVIEWS
    imported = []
    with SessionLocal() as db:
        for item in reviews:
            submitted_at = (
                datetime.fromisoformat(item["submitted_at"])
                if item.get("submitted_at")
                else datetime.now(timezone.utc)
            )
            result = import_product_review(
                db,
                product_id=args.product_id,
                source=args.source,
                external_review_id=item["external_review_id"],
                author_display_name=item["author_display_name"],
                body=item["body"],
                rating=int(item["rating"]),
                submitted_at=submitted_at,
            )
            imported.append(result.model_dump(mode="json"))
    print(json.dumps({"product_id": args.product_id, "reviews": imported}, indent=2))


if __name__ == "__main__":
    main()
