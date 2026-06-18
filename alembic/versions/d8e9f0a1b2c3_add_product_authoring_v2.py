"""add canonical product authoring and inventory

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-06-18 15:45:00.000000
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from decimal import Decimal
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8e9f0a1b2c3"
down_revision: Union[str, Sequence[str], None] = "c7d8e9f0a1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _inventory_id(product_id: str, store_id: str, size_key: str) -> str:
    key = "|".join([_clean(product_id), _clean(store_id), _clean(size_key)])
    return f"pinv_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:20]}"


def _normalized_size(value: object) -> tuple[str | None, str]:
    display = str(value or "").strip()
    if not display or _clean(display) == "one size":
        return None, ""
    return display, _clean(display)


def _availability(rows: list[dict]) -> str:
    if any(
        _clean(row["availability"]) in {"in stock", "in_stock", "available"}
        and int(row["inventory_qty"] or 0) > 0
        for row in rows
    ):
        return "in stock"
    if any(
        _clean(row["availability"]) in {"preorder", "pre-order", "pre order"}
        for row in rows
    ):
        return "preorder"
    return str(rows[0]["availability"] if rows else "out of stock")


def _backfill() -> None:
    bind = op.get_bind()
    products = sa.table(
        "catalog_products",
        sa.column("id", sa.String),
        sa.column("seed_run_id", sa.String),
        sa.column("metadata_json", sa.JSON),
        sa.column("price_min", sa.Numeric),
        sa.column("price_max", sa.Numeric),
        sa.column("link", sa.String),
        sa.column("color", sa.String),
        sa.column("material", sa.String),
        sa.column("gender", sa.String),
        sa.column("season", sa.String),
    )
    variants = sa.table(
        "product_variants",
        sa.column("id", sa.String),
        sa.column("catalog_product_id", sa.String),
        sa.column("price_min", sa.Numeric),
        sa.column("price_max", sa.Numeric),
        sa.column("link", sa.String),
        sa.column("color", sa.String),
        sa.column("material", sa.String),
        sa.column("gender", sa.String),
        sa.column("season", sa.String),
    )
    inventory = sa.table(
        "store_inventory",
        sa.column("id", sa.String),
        sa.column("seed_run_id", sa.String),
        sa.column("store_id", sa.String),
        sa.column("variant_id", sa.String),
        sa.column("size", sa.String),
        sa.column("availability", sa.String),
        sa.column("inventory_qty", sa.Integer),
    )
    canonical_inventory = sa.table(
        "product_inventory",
        sa.column("id", sa.String),
        sa.column("seed_run_id", sa.String),
        sa.column("catalog_product_id", sa.String),
        sa.column("store_id", sa.String),
        sa.column("size", sa.String),
        sa.column("size_key", sa.String),
        sa.column("availability", sa.String),
        sa.column("inventory_qty", sa.Integer),
        sa.column("metadata_json", sa.JSON),
    )

    product_rows = bind.execute(sa.select(products)).mappings().all()
    variant_rows = bind.execute(sa.select(variants).order_by(variants.c.catalog_product_id, variants.c.id)).mappings().all()
    variants_by_product: dict[str, list[dict]] = defaultdict(list)
    product_by_variant: dict[str, str] = {}
    for row in variant_rows:
        payload = dict(row)
        variants_by_product[payload["catalog_product_id"]].append(payload)
        product_by_variant[payload["id"]] = payload["catalog_product_id"]

    for product in product_rows:
        product_variants = variants_by_product.get(product["id"], [])
        if not product_variants:
            continue
        metadata = product["metadata_json"] if isinstance(product["metadata_json"], dict) else {}
        primary_id = (metadata.get("_catalog_studio_authoring") or {}).get("primary_variant_id")
        primary = next((row for row in product_variants if row["id"] == primary_id), product_variants[0])
        bind.execute(
            products.update()
            .where(products.c.id == product["id"])
            .values(
                price_min=min(Decimal(row["price_min"]) for row in product_variants),
                price_max=max(Decimal(row["price_max"]) for row in product_variants),
                link=primary["link"],
                color=primary["color"],
                material=primary["material"],
                gender=primary["gender"],
                season=primary["season"],
            )
        )

    inventory_rows = bind.execute(sa.select(inventory).order_by(inventory.c.id)).mappings().all()
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in inventory_rows:
        product_id = product_by_variant.get(row["variant_id"])
        if product_id is None:
            continue
        _, size_key = _normalized_size(row["size"])
        groups[(product_id, row["store_id"], size_key)].append(dict(row))
    if groups:
        bind.execute(
            canonical_inventory.insert(),
            [
                {
                    "id": _inventory_id(product_id, store_id, size_key),
                    "seed_run_id": rows[0]["seed_run_id"],
                    "catalog_product_id": product_id,
                    "store_id": store_id,
                    "size": _normalized_size(rows[0]["size"])[0],
                    "size_key": size_key,
                    "availability": _availability(rows),
                    "inventory_qty": sum(int(row["inventory_qty"] or 0) for row in rows),
                    "metadata_json": {
                        "source": "legacy_variant_inventory",
                        "source_inventory_ids": [row["id"] for row in rows],
                        "source_variant_ids": sorted({row["variant_id"] for row in rows}),
                    },
                }
                for (product_id, store_id, size_key), rows in sorted(groups.items())
            ],
        )


def upgrade() -> None:
    with op.batch_alter_table("catalog_products") as batch_op:
        batch_op.add_column(sa.Column("price_min", sa.Numeric(10, 2), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("price_max", sa.Numeric(10, 2), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("link", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("color", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("material", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("gender", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("season", sa.String(length=32), nullable=True))
        batch_op.create_check_constraint("ck_catalog_products_price_min_non_negative", "price_min >= 0")
        batch_op.create_check_constraint("ck_catalog_products_price_range_valid", "price_max >= price_min")
        batch_op.create_index("ix_catalog_products_price", ["price_min", "price_max"])
    op.create_table(
        "product_inventory",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("seed_run_id", sa.String(length=64), nullable=False),
        sa.Column("catalog_product_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("size", sa.String(length=64), nullable=True),
        sa.Column("size_key", sa.String(length=64), nullable=False),
        sa.Column("availability", sa.String(length=32), nullable=False),
        sa.Column("inventory_qty", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.CheckConstraint("inventory_qty >= 0", name="ck_product_inventory_qty_non_negative"),
        sa.ForeignKeyConstraint(["catalog_product_id"], ["catalog_products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["seed_run_id"], ["synthetic_runs.id"]),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("catalog_product_id", "store_id", "size_key", name="uq_product_inventory_product_store_size_key"),
    )
    op.create_index("ix_product_inventory_seed_run_id", "product_inventory", ["seed_run_id"])
    op.create_index("ix_product_inventory_catalog_product_id", "product_inventory", ["catalog_product_id"])
    op.create_index("ix_product_inventory_store_id", "product_inventory", ["store_id"])
    op.create_index("ix_product_inventory_store_availability", "product_inventory", ["store_id", "availability"])
    op.create_index("ix_product_inventory_product_size", "product_inventory", ["catalog_product_id", "size_key"])
    _backfill()


def downgrade() -> None:
    op.drop_index("ix_product_inventory_product_size", table_name="product_inventory")
    op.drop_index("ix_product_inventory_store_availability", table_name="product_inventory")
    op.drop_index("ix_product_inventory_store_id", table_name="product_inventory")
    op.drop_index("ix_product_inventory_catalog_product_id", table_name="product_inventory")
    op.drop_index("ix_product_inventory_seed_run_id", table_name="product_inventory")
    op.drop_table("product_inventory")
    with op.batch_alter_table("catalog_products") as batch_op:
        batch_op.drop_index("ix_catalog_products_price")
        batch_op.drop_constraint("ck_catalog_products_price_range_valid", type_="check")
        batch_op.drop_constraint("ck_catalog_products_price_min_non_negative", type_="check")
        batch_op.drop_column("season")
        batch_op.drop_column("gender")
        batch_op.drop_column("material")
        batch_op.drop_column("color")
        batch_op.drop_column("link")
        batch_op.drop_column("price_max")
        batch_op.drop_column("price_min")
