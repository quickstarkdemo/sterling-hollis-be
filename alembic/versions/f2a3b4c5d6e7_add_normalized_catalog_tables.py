"""add normalized catalog tables

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-04-29 10:00:00.000000
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from decimal import Decimal
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, Sequence[str], None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEFAULT_SIZE = "One Size"


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _display(value: object, *, fallback: str = DEFAULT_SIZE) -> str:
    text = str(value or "").strip()
    return text or fallback


def _hash_id(prefix: str, key: str) -> str:
    return f"{prefix}_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:20]}"


def _catalog_key(row) -> str:
    return "|".join([_clean(row["brand"]), _clean(row["title"]), _clean(row["category"])])


def _variant_key(row) -> str:
    return "|".join(
        [
            _catalog_key(row),
            _clean(row["color"]),
            _clean(row["material"]),
            _clean(row["gender"]),
            _clean(row["season"]),
        ]
    )


def _catalog_id(key: str) -> str:
    return _hash_id("cat", key)


def _variant_id(key: str) -> str:
    return _hash_id("var", key)


def _inventory_id(store_id: str, variant_id: str, size: str) -> str:
    return _hash_id("inv", "|".join([_clean(store_id), _clean(variant_id), _clean(size)]))


def _is_in_stock(row) -> bool:
    return str(row["availability"] or "").strip().lower() == "in stock" and int(row["inventory_qty"] or 0) > 0


def _is_preorder(row) -> bool:
    return str(row["availability"] or "").strip().lower() == "preorder"


def _merged_availability(rows) -> str:
    if any(_is_in_stock(row) for row in rows):
        return "in stock"
    if any(_is_preorder(row) for row in rows):
        return "preorder"
    return rows[0]["availability"] if rows else "out of stock"


def _image_set(rows) -> dict:
    for row in rows:
        metadata = row["metadata_json"] if isinstance(row["metadata_json"], dict) else {}
        image_set = metadata.get("image_set")
        if isinstance(image_set, dict) and image_set:
            return image_set
    return {}


def upgrade() -> None:
    op.create_table(
        "catalog_products",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("seed_run_id", sa.String(length=64), nullable=False),
        sa.Column("catalog_key", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("brand", sa.String(length=128), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.ForeignKeyConstraint(["seed_run_id"], ["synthetic_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("catalog_key", name="uq_catalog_products_catalog_key"),
    )
    op.create_index("ix_catalog_products_seed_run_id", "catalog_products", ["seed_run_id"])
    op.create_index("ix_catalog_products_catalog_key", "catalog_products", ["catalog_key"])
    op.create_index("ix_catalog_products_brand", "catalog_products", ["brand"])
    op.create_index("ix_catalog_products_category", "catalog_products", ["category"])
    op.create_index("ix_catalog_products_category_brand", "catalog_products", ["category", "brand"])

    op.create_table(
        "product_variants",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("seed_run_id", sa.String(length=64), nullable=False),
        sa.Column("catalog_product_id", sa.String(length=64), nullable=False),
        sa.Column("variant_key", sa.String(length=320), nullable=False),
        sa.Column("color", sa.String(length=64), nullable=True),
        sa.Column("material", sa.String(length=64), nullable=True),
        sa.Column("gender", sa.String(length=32), nullable=True),
        sa.Column("season", sa.String(length=32), nullable=True),
        sa.Column("price_min", sa.Numeric(10, 2), nullable=False),
        sa.Column("price_max", sa.Numeric(10, 2), nullable=False),
        sa.Column("link", sa.String(length=500), nullable=True),
        sa.Column("image_link", sa.String(length=500), nullable=True),
        sa.Column("image_set", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.CheckConstraint("price_min >= 0", name="ck_product_variants_price_min_non_negative"),
        sa.CheckConstraint("price_max >= price_min", name="ck_product_variants_price_range_valid"),
        sa.ForeignKeyConstraint(["catalog_product_id"], ["catalog_products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["seed_run_id"], ["synthetic_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("variant_key", name="uq_product_variants_variant_key"),
    )
    op.create_index("ix_product_variants_seed_run_id", "product_variants", ["seed_run_id"])
    op.create_index("ix_product_variants_catalog_product_id", "product_variants", ["catalog_product_id"])
    op.create_index("ix_product_variants_variant_key", "product_variants", ["variant_key"])
    op.create_index("ix_product_variants_product_price", "product_variants", ["catalog_product_id", "price_min", "price_max"])

    op.create_table(
        "store_inventory",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("seed_run_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("variant_id", sa.String(length=64), nullable=False),
        sa.Column("size", sa.String(length=64), nullable=False),
        sa.Column("availability", sa.String(length=32), nullable=False),
        sa.Column("inventory_qty", sa.Integer(), nullable=False),
        sa.Column("objective_weight", sa.Numeric(5, 4), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.CheckConstraint("inventory_qty >= 0", name="ck_store_inventory_qty_non_negative"),
        sa.ForeignKeyConstraint(["seed_run_id"], ["synthetic_runs.id"]),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"]),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_id", "variant_id", "size", name="uq_store_inventory_store_variant_size"),
    )
    op.create_index("ix_store_inventory_seed_run_id", "store_inventory", ["seed_run_id"])
    op.create_index("ix_store_inventory_store_id", "store_inventory", ["store_id"])
    op.create_index("ix_store_inventory_variant_id", "store_inventory", ["variant_id"])
    op.create_index("ix_store_inventory_store_availability", "store_inventory", ["store_id", "availability"])
    op.create_index("ix_store_inventory_variant_size", "store_inventory", ["variant_id", "size"])

    _backfill()


def _backfill() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            select id, seed_run_id, store_id, title, description, link, image_link, price,
                   availability, brand, category, color, size, material, gender, season,
                   inventory_qty, objective_weight, metadata_json
            from products
            order by id
            """
        )
    ).mappings().all()
    if not rows:
        return

    catalog_groups = defaultdict(list)
    variant_groups = defaultdict(list)
    inventory_groups = defaultdict(list)
    for row in rows:
        catalog_key = _catalog_key(row)
        variant_key = _variant_key(row)
        variant_id = _variant_id(variant_key)
        size = _display(row["size"])
        catalog_groups[catalog_key].append(row)
        variant_groups[variant_key].append(row)
        inventory_groups[(row["store_id"], variant_id, size)].append(row)

    catalog_table = sa.table(
        "catalog_products",
        sa.column("id"),
        sa.column("seed_run_id"),
        sa.column("catalog_key"),
        sa.column("title"),
        sa.column("description"),
        sa.column("brand"),
        sa.column("category"),
        sa.column("metadata_json", sa.JSON()),
    )
    variant_table = sa.table(
        "product_variants",
        sa.column("id"),
        sa.column("seed_run_id"),
        sa.column("catalog_product_id"),
        sa.column("variant_key"),
        sa.column("color"),
        sa.column("material"),
        sa.column("gender"),
        sa.column("season"),
        sa.column("price_min"),
        sa.column("price_max"),
        sa.column("link"),
        sa.column("image_link"),
        sa.column("image_set", sa.JSON()),
        sa.column("metadata_json", sa.JSON()),
    )
    inventory_table = sa.table(
        "store_inventory",
        sa.column("id"),
        sa.column("seed_run_id"),
        sa.column("store_id"),
        sa.column("variant_id"),
        sa.column("size"),
        sa.column("availability"),
        sa.column("inventory_qty"),
        sa.column("objective_weight"),
        sa.column("metadata_json", sa.JSON()),
    )

    op.bulk_insert(
        catalog_table,
        [
            {
                "id": _catalog_id(key),
                "seed_run_id": group[0]["seed_run_id"],
                "catalog_key": key,
                "title": group[0]["title"],
                "description": group[0]["description"],
                "brand": group[0]["brand"],
                "category": group[0]["category"],
                "metadata_json": {
                    "source": "legacy_products",
                    "source_product_ids": [row["id"] for row in group],
                    "source_product_count": len(group),
                },
            }
            for key, group in sorted(catalog_groups.items())
        ],
    )
    op.bulk_insert(
        variant_table,
        [
            {
                "id": _variant_id(key),
                "seed_run_id": group[0]["seed_run_id"],
                "catalog_product_id": _catalog_id(_catalog_key(group[0])),
                "variant_key": key,
                "color": group[0]["color"],
                "material": group[0]["material"],
                "gender": group[0]["gender"],
                "season": group[0]["season"],
                "price_min": min(Decimal(row["price"]) for row in group),
                "price_max": max(Decimal(row["price"]) for row in group),
                "link": group[0]["link"],
                "image_link": group[0]["image_link"],
                "image_set": _image_set(group),
                "metadata_json": {
                    "source": "legacy_products",
                    "source_product_ids": [row["id"] for row in group],
                    "source_product_count": len(group),
                },
            }
            for key, group in sorted(variant_groups.items())
        ],
    )
    op.bulk_insert(
        inventory_table,
        [
            {
                "id": _inventory_id(store_id, variant_id, size),
                "seed_run_id": group[0]["seed_run_id"],
                "store_id": store_id,
                "variant_id": variant_id,
                "size": size,
                "availability": _merged_availability(group),
                "inventory_qty": sum(int(row["inventory_qty"] or 0) for row in group),
                "objective_weight": max(Decimal(row["objective_weight"]) for row in group),
                "metadata_json": {
                    "source": "legacy_products",
                    "source_product_ids": [row["id"] for row in group],
                    "source_product_count": len(group),
                },
            }
            for (store_id, variant_id, size), group in sorted(inventory_groups.items())
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_store_inventory_variant_size", table_name="store_inventory")
    op.drop_index("ix_store_inventory_store_availability", table_name="store_inventory")
    op.drop_index("ix_store_inventory_variant_id", table_name="store_inventory")
    op.drop_index("ix_store_inventory_store_id", table_name="store_inventory")
    op.drop_index("ix_store_inventory_seed_run_id", table_name="store_inventory")
    op.drop_table("store_inventory")
    op.drop_index("ix_product_variants_product_price", table_name="product_variants")
    op.drop_index("ix_product_variants_variant_key", table_name="product_variants")
    op.drop_index("ix_product_variants_catalog_product_id", table_name="product_variants")
    op.drop_index("ix_product_variants_seed_run_id", table_name="product_variants")
    op.drop_table("product_variants")
    op.drop_index("ix_catalog_products_category_brand", table_name="catalog_products")
    op.drop_index("ix_catalog_products_category", table_name="catalog_products")
    op.drop_index("ix_catalog_products_brand", table_name="catalog_products")
    op.drop_index("ix_catalog_products_catalog_key", table_name="catalog_products")
    op.drop_index("ix_catalog_products_seed_run_id", table_name="catalog_products")
    op.drop_table("catalog_products")
