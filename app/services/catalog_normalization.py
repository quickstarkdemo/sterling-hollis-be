from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import CatalogProduct, Product, ProductVariant, StoreInventory
from app.services.inventory_status import is_in_stock, is_preorder


DEFAULT_SIZE = "One Size"


@dataclass(frozen=True)
class CatalogBackfillStats:
    legacy_products: int
    catalog_products: int
    product_variants: int
    store_inventory: int


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _display(value: object, *, fallback: str = DEFAULT_SIZE) -> str:
    text = str(value or "").strip()
    return text or fallback


def _hash_id(prefix: str, key: str) -> str:
    return f"{prefix}_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:20]}"


def catalog_key_for_values(*, brand: str | None, title: str | None, category: str | None) -> str:
    return "|".join([_clean(brand), _clean(title), _clean(category)])


def variant_key_for_values(
    *,
    brand: str | None,
    title: str | None,
    category: str | None,
    color: str | None,
    material: str | None,
    gender: str | None,
    season: str | None,
) -> str:
    catalog_key = catalog_key_for_values(brand=brand, title=title, category=category)
    return _variant_key_for_catalog_key(
        catalog_key,
        color=color,
        material=material,
        gender=gender,
        season=season,
    )


def _variant_key_for_catalog_key(
    catalog_key: str,
    *,
    color: str | None,
    material: str | None,
    gender: str | None,
    season: str | None,
) -> str:
    return "|".join(
        [
            catalog_key,
            _clean(color),
            _clean(material),
            _clean(gender),
            _clean(season),
        ]
    )


def catalog_product_id_for_key(catalog_key: str) -> str:
    return _hash_id("cat", catalog_key)


def product_variant_id_for_key(variant_key: str) -> str:
    return _hash_id("var", variant_key)


def store_inventory_id_for_values(*, store_id: str, variant_id: str, size: str) -> str:
    return _hash_id("inv", "|".join([_clean(store_id), _clean(variant_id), _clean(size)]))


def catalog_key_for_product(product: Product) -> str:
    metadata = product.metadata_json if isinstance(product.metadata_json, dict) else {}
    style_code = _clean(metadata.get("style_code"))
    if style_code:
        return "|".join(["source-family", _clean(product.seed_run_id), style_code])
    return catalog_key_for_values(brand=product.brand, title=product.title, category=product.category)


def variant_key_for_product(product: Product) -> str:
    return _variant_key_for_catalog_key(
        catalog_key_for_product(product),
        color=product.color,
        material=product.material,
        gender=product.gender,
        season=product.season,
    )


def _merged_availability(products: list[Product]) -> str:
    if any(is_in_stock(product.availability, product.inventory_qty) for product in products):
        return "in stock"
    if any(is_preorder(product.availability) for product in products):
        return "preorder"
    return products[0].availability if products else "out of stock"


def _image_set_from_products(products: list[Product]) -> dict:
    for product in products:
        metadata = product.metadata_json if isinstance(product.metadata_json, dict) else {}
        image_set = metadata.get("image_set")
        if isinstance(image_set, dict) and image_set:
            return image_set
    return {}


def backfill_catalog_from_legacy_products(db: Session, *, run_id: str | None = None) -> CatalogBackfillStats:
    query = select(Product).order_by(Product.id.asc())
    if run_id:
        query = query.where(Product.seed_run_id == run_id)
    products = db.scalars(query).all()

    if run_id:
        db.execute(delete(StoreInventory).where(StoreInventory.seed_run_id == run_id))
        db.execute(delete(ProductVariant).where(ProductVariant.seed_run_id == run_id))
        db.execute(delete(CatalogProduct).where(CatalogProduct.seed_run_id == run_id))
    else:
        db.execute(delete(StoreInventory))
        db.execute(delete(ProductVariant))
        db.execute(delete(CatalogProduct))
    db.flush()

    catalog_groups: dict[str, list[Product]] = defaultdict(list)
    variant_groups: dict[str, list[Product]] = defaultdict(list)
    inventory_groups: dict[tuple[str, str, str], list[Product]] = defaultdict(list)

    for product in products:
        catalog_key = catalog_key_for_product(product)
        variant_key = variant_key_for_product(product)
        variant_id = product_variant_id_for_key(variant_key)
        size = _display(product.size)
        catalog_groups[catalog_key].append(product)
        variant_groups[variant_key].append(product)
        inventory_groups[(product.store_id, variant_id, size)].append(product)

    catalog_rows = []
    for catalog_key, group in sorted(catalog_groups.items()):
        primary = group[0]
        source_ids = [product.id for product in group]
        primary_metadata = primary.metadata_json if isinstance(primary.metadata_json, dict) else {}
        catalog_rows.append(
            {
                "id": catalog_product_id_for_key(catalog_key),
                "seed_run_id": primary.seed_run_id,
                "catalog_key": catalog_key,
                "title": primary.title,
                "description": primary.description,
                "brand": primary.brand,
                "category": primary.category,
                "metadata_json": {
                    "source": "legacy_products",
                    "source_product_ids": source_ids,
                    "source_product_count": len(source_ids),
                    "source_style_code": primary_metadata.get("style_code"),
                },
            }
        )

    variant_rows = []
    for variant_key, group in sorted(variant_groups.items()):
        primary = group[0]
        prices = [Decimal(product.price) for product in group]
        source_ids = [product.id for product in group]
        catalog_key = catalog_key_for_product(primary)
        variant_rows.append(
            {
                "id": product_variant_id_for_key(variant_key),
                "seed_run_id": primary.seed_run_id,
                "catalog_product_id": catalog_product_id_for_key(catalog_key),
                "variant_key": variant_key,
                "color": primary.color,
                "material": primary.material,
                "gender": primary.gender,
                "season": primary.season,
                "price_min": min(prices),
                "price_max": max(prices),
                "link": primary.link,
                "image_link": primary.image_link,
                "image_set": _image_set_from_products(group),
                "metadata_json": {
                    "source": "legacy_products",
                    "source_product_ids": source_ids,
                    "source_product_count": len(source_ids),
                },
            }
        )

    inventory_rows = []
    for (store_id, variant_id, size), group in sorted(inventory_groups.items()):
        primary = group[0]
        source_ids = [product.id for product in group]
        inventory_rows.append(
            {
                "id": store_inventory_id_for_values(store_id=store_id, variant_id=variant_id, size=size),
                "seed_run_id": primary.seed_run_id,
                "store_id": store_id,
                "variant_id": variant_id,
                "size": size,
                "availability": _merged_availability(group),
                "inventory_qty": sum(int(product.inventory_qty or 0) for product in group),
                "objective_weight": max(Decimal(product.objective_weight) for product in group),
                "metadata_json": {
                    "source": "legacy_products",
                    "source_product_ids": source_ids,
                    "source_product_count": len(source_ids),
                },
            }
        )

    if catalog_rows:
        db.bulk_insert_mappings(CatalogProduct, catalog_rows)
    if variant_rows:
        db.bulk_insert_mappings(ProductVariant, variant_rows)
    if inventory_rows:
        db.bulk_insert_mappings(StoreInventory, inventory_rows)
    db.commit()

    return CatalogBackfillStats(
        legacy_products=len(products),
        catalog_products=len(catalog_rows),
        product_variants=len(variant_rows),
        store_inventory=len(inventory_rows),
    )
