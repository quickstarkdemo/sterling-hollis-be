from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.catalog.authoring import authoring_metadata
from app.catalog.references import (
    catalog_brand_id_for_name,
    display_brand_name,
    normalized_brand_name,
)
from app.models import (
    CatalogBrand,
    CatalogProduct,
    Product,
    ProductInventory,
    ProductVariant,
    StoreInventory,
)
from app.services.inventory_status import is_in_stock, is_preorder


DEFAULT_SIZE = "One Size"


@dataclass(frozen=True)
class CatalogBackfillStats:
    legacy_products: int
    catalog_products: int
    product_variants: int
    store_inventory: int


@dataclass(frozen=True)
class ProductAuthoringBackfillReport:
    source_products: int
    translated_products: int
    skipped_products: int
    conflicting_inventory_groups: int
    source_inventory_qty: int
    translated_inventory_qty: int


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


def product_inventory_id_for_values(
    *, product_id: str, store_id: str, size_key: str
) -> str:
    return _hash_id("pinv", "|".join([_clean(product_id), _clean(store_id), _clean(size_key)]))


def normalized_size(value: object) -> tuple[str | None, str]:
    display = str(value or "").strip()
    if not display or _clean(display) == _clean(DEFAULT_SIZE):
        return None, ""
    return display, _clean(display)


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


def _merged_inventory_availability(rows: list[StoreInventory]) -> str:
    if any(is_in_stock(row.availability, row.inventory_qty) for row in rows):
        return "in stock"
    if any(is_preorder(row.availability) for row in rows):
        return "preorder"
    return rows[0].availability if rows else "out of stock"


def backfill_product_authoring_v2(
    db: Session,
    *,
    run_id: str | None = None,
    dry_run: bool = True,
) -> ProductAuthoringBackfillReport:
    product_query = select(CatalogProduct).order_by(CatalogProduct.id.asc())
    if run_id:
        product_query = product_query.where(CatalogProduct.seed_run_id == run_id)
    products = db.scalars(product_query).all()
    product_ids = [product.id for product in products]
    variants = (
        db.scalars(
            select(ProductVariant)
            .where(ProductVariant.catalog_product_id.in_(product_ids))
            .order_by(ProductVariant.catalog_product_id.asc(), ProductVariant.id.asc())
        ).all()
        if product_ids
        else []
    )
    variant_ids = [variant.id for variant in variants]
    inventory = (
        db.scalars(
            select(StoreInventory)
            .where(StoreInventory.variant_id.in_(variant_ids))
            .order_by(StoreInventory.variant_id.asc(), StoreInventory.store_id.asc(), StoreInventory.size.asc())
        ).all()
        if variant_ids
        else []
    )

    variants_by_product: dict[str, list[ProductVariant]] = defaultdict(list)
    product_id_by_variant: dict[str, str] = {}
    for variant in variants:
        variants_by_product[variant.catalog_product_id].append(variant)
        product_id_by_variant[variant.id] = variant.catalog_product_id

    inventory_groups: dict[tuple[str, str, str], list[StoreInventory]] = defaultdict(list)
    for row in inventory:
        product_id = product_id_by_variant[row.variant_id]
        _, size_key = normalized_size(row.size)
        inventory_groups[(product_id, row.store_id, size_key)].append(row)

    translated_products = 0
    skipped_products = 0
    updates: list[tuple[CatalogProduct, ProductVariant, Decimal, Decimal]] = []
    for product in products:
        product_variants = variants_by_product.get(product.id, [])
        if not product_variants:
            skipped_products += 1
            continue
        primary_id = authoring_metadata(product.metadata_json).get("primary_variant_id")
        primary = next(
            (variant for variant in product_variants if variant.id == primary_id),
            product_variants[0],
        )
        updates.append(
            (
                product,
                primary,
                min(Decimal(variant.price_min) for variant in product_variants),
                max(Decimal(variant.price_max) for variant in product_variants),
            )
        )
        translated_products += 1

    source_total = sum(int(row.inventory_qty or 0) for row in inventory)
    translated_total = sum(
        sum(int(row.inventory_qty or 0) for row in group)
        for group in inventory_groups.values()
    )
    conflicting_groups = sum(
        1
        for group in inventory_groups.values()
        if len(
            {
                _clean(value)
                for row in group
                for value in (
                    (row.metadata_json or {}).get("source_availabilities")
                    or [row.availability]
                )
            }
        )
        > 1
    )
    report = ProductAuthoringBackfillReport(
        source_products=len(products),
        translated_products=translated_products,
        skipped_products=skipped_products,
        conflicting_inventory_groups=conflicting_groups,
        source_inventory_qty=source_total,
        translated_inventory_qty=translated_total,
    )
    if dry_run:
        return report

    if product_ids:
        db.execute(delete(ProductInventory).where(ProductInventory.catalog_product_id.in_(product_ids)))
    for product, primary, price_min, price_max in updates:
        product.price_min = price_min
        product.price_max = price_max
        product.link = primary.link
        product.color = primary.color
        product.material = primary.material
        product.gender = primary.gender
        product.season = primary.season
    for (product_id, store_id, size_key), group in sorted(inventory_groups.items()):
        display_size, _ = normalized_size(group[0].size)
        source_ids = [row.id for row in group]
        db.add(
            ProductInventory(
                id=product_inventory_id_for_values(
                    product_id=product_id, store_id=store_id, size_key=size_key
                ),
                seed_run_id=group[0].seed_run_id,
                catalog_product_id=product_id,
                store_id=store_id,
                size=display_size,
                size_key=size_key,
                availability=_merged_inventory_availability(group),
                inventory_qty=sum(int(row.inventory_qty or 0) for row in group),
                metadata_json={
                    "source": "legacy_variant_inventory",
                    "source_inventory_ids": source_ids,
                    "source_variant_ids": sorted({row.variant_id for row in group}),
                },
            )
        )
    db.commit()
    return report


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

    brand_names: dict[str, list[str]] = defaultdict(list)
    for product in products:
        normalized = normalized_brand_name(product.brand)
        if normalized:
            brand_names[normalized].append(display_brand_name(product.brand))
    for normalized, names in sorted(brand_names.items()):
        brand_id = catalog_brand_id_for_name(normalized)
        if db.get(CatalogBrand, brand_id) is None:
            canonical_name = sorted(set(names), key=lambda value: (value.casefold(), value))[0]
            db.add(
                CatalogBrand(
                    id=brand_id,
                    name=canonical_name,
                    normalized_name=normalized,
                    active=True,
                )
            )
    db.flush()

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
        prices = [Decimal(product.price) for product in group]
        source_ids = [product.id for product in group]
        primary_metadata = primary.metadata_json if isinstance(primary.metadata_json, dict) else {}
        catalog_rows.append(
            {
                "id": catalog_product_id_for_key(catalog_key),
                "seed_run_id": primary.seed_run_id,
                "catalog_key": catalog_key,
                "title": primary.title,
                "description": primary.description,
                "brand_id": catalog_brand_id_for_name(primary.brand),
                "brand": primary.brand,
                "category": primary.category,
                "price_min": min(prices),
                "price_max": max(prices),
                "link": primary.link,
                "color": primary.color,
                "material": primary.material,
                "gender": primary.gender,
                "season": primary.season,
                "metadata_json": {
                    "source": "legacy_products",
                    "source_product_ids": source_ids,
                    "source_product_count": len(source_ids),
                    "source_style_code": primary_metadata.get("style_code"),
                    "_catalog_studio_authoring": {
                        "primary_variant_id": product_variant_id_for_key(
                            variant_key_for_product(primary)
                        )
                    },
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
                    "source_availabilities": sorted(
                        {product.availability for product in group}
                    ),
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
    backfill_product_authoring_v2(db, run_id=run_id, dry_run=False)

    return CatalogBackfillStats(
        legacy_products=len(products),
        catalog_products=len(catalog_rows),
        product_variants=len(variant_rows),
        store_inventory=len(inventory_rows),
    )
