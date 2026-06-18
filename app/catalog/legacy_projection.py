from __future__ import annotations

import hashlib
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.authoring import authoring_metadata
from app.catalog.schemas import CatalogVariant, ProductImages, ProductInventory
from app.models import CatalogProduct, ProductVariant
from app.services.demo_assets import demo_image_url
from app.services.product_images import product_variant_image_set


logger = logging.getLogger(__name__)


def legacy_variant_id(product_id: str) -> str:
    digest = hashlib.sha1(product_id.encode("utf-8")).hexdigest()[:20]
    return f"var_compat_{digest}"


def legacy_main_image_fallback(
    db: Session, product: CatalogProduct
) -> ProductImages | None:
    """Read-only bridge for published products not yet republished with managed media."""
    variants = db.scalars(
        select(ProductVariant)
        .where(ProductVariant.catalog_product_id == product.id)
        .order_by(ProductVariant.id.asc())
    ).all()
    if not variants:
        return None
    primary_variant_id = authoring_metadata(product.metadata_json).get("primary_variant_id")
    variant = next(
        (row for row in variants if row.id == primary_variant_id),
        variants[0],
    )
    image_set = product_variant_image_set(variant)
    fallback = demo_image_url(product.category, variant.id, variant_hint=product.brand)
    logger.info(
        "catalog_legacy_media_projection",
        extra={"catalog_product_id": product.id, "legacy_variant_id": variant.id},
    )
    return ProductImages(
        thumbnail_url=(image_set or {}).get("thumbnail_url") or fallback,
        primary_url=(image_set or {}).get("primary_url") or fallback,
        detail_urls=(image_set or {}).get("detail_urls") or [fallback],
    )


def legacy_variant_projection(
    product: CatalogProduct,
    *,
    images: ProductImages,
    attributes: dict[str, str],
    inventory: list[ProductInventory],
) -> CatalogVariant:
    """Emit the single deprecated outward variant derived from canonical state."""
    compatibility_id = legacy_variant_id(product.id)
    logger.info(
        "catalog_legacy_variant_projection",
        extra={"catalog_product_id": product.id, "compatibility_variant_id": compatibility_id},
    )
    return CatalogVariant(
        id=compatibility_id,
        product_id=product.id,
        price_min=float(product.price_min),
        price_max=float(product.price_max),
        link=product.link,
        image_url=images.thumbnail_url,
        images=images,
        attributes=attributes,
        sizes=sorted({row.size for row in inventory if row.size}),
        inventory=inventory,
    )
