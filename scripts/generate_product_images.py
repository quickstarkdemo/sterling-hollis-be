#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from app.database import SessionLocal
from app.services.product_images import (
    ProductImageGenerator,
    product_image_options,
    query_variants_for_image_generation,
)


def _product_ids(raw_values: list[str]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        for value in raw.replace(";", ",").split(","):
            product_id = value.strip()
            if product_id and product_id not in seen:
                seen.add(product_id)
                ids.append(product_id)
    return ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate product variant images with OpenAI.")
    parser.add_argument("--run-id", help="Restrict to one synthetic run.")
    parser.add_argument("--store-id", help="Restrict to variants stocked by one store.")
    parser.add_argument("--product-id", action="append", default=[], help="Catalog product id to generate. Repeat or comma-separate.")
    parser.add_argument("--variant-id", action="append", default=[], help="Variant id to generate. Repeat or comma-separate.")
    parser.add_argument("--category", help="Restrict to one product category.")
    parser.add_argument("--brand", help="Restrict to one product brand.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum variants to process.")
    parser.add_argument("--model", help="OpenAI image model. Defaults to PRODUCT_IMAGE_MODEL.")
    parser.add_argument("--size", help="Image size. Defaults to PRODUCT_IMAGE_SIZE.")
    parser.add_argument("--quality", help="Image quality. Defaults to PRODUCT_IMAGE_QUALITY.")
    parser.add_argument("--output-format", choices=["png", "jpeg", "webp"], help="Generated file format.")
    parser.add_argument("--output-dir", help="Directory where image files are written.")
    parser.add_argument("--public-base-url", help="Base URL written into product_variants.image_link.")
    parser.add_argument("--url-path", help="URL path mounted by the API for generated product images.")
    parser.add_argument("--detail-count", type=int, help="Number of full-size detail images to generate per variant.")
    parser.add_argument("--thumbnail-size", type=int, help="Maximum thumbnail width/height in pixels.")
    parser.add_argument("--overwrite", action="store_true", help="Regenerate even when image_link is already non-placeholder.")
    parser.add_argument("--dry-run", action="store_true", help="Print prompts and planned updates without calling OpenAI.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    options = product_image_options(
        model=args.model,
        size=args.size,
        quality=args.quality,
        output_format=args.output_format,
        output_dir=args.output_dir,
        public_base_url=args.public_base_url,
        url_path=args.url_path,
        detail_count=args.detail_count,
        thumbnail_size=args.thumbnail_size,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    generator = ProductImageGenerator(options)
    with SessionLocal() as db:
        variants = query_variants_for_image_generation(
            db,
            run_id=args.run_id,
            store_id=args.store_id,
            product_ids=_product_ids(args.product_id),
            variant_ids=_product_ids(args.variant_id),
            category=args.category,
            brand=args.brand,
            limit=args.limit,
        )
        results = [asdict(generator.generate_for_variant(db, variant)) for variant in variants]

    print(
        json.dumps(
            {
                "attempted": len(results),
                "generated": sum(1 for item in results if item["status"] == "generated"),
                "skipped": sum(1 for item in results if item["status"].startswith("skipped")),
                "failed": sum(1 for item in results if item["status"] == "failed"),
                "dry_run": bool(args.dry_run),
                "results": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
