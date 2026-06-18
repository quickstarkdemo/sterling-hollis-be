"""add canonical catalog brands

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-06-18 16:35:00.000000
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e9f0a1b2c3d4"
down_revision: Union[str, Sequence[str], None] = "d8e9f0a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _normalized_name(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _display_name(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _brand_id(normalized_name: str) -> str:
    digest = hashlib.sha1(normalized_name.encode("utf-8")).hexdigest()[:20]
    return f"brand_{digest}"


def _backfill() -> None:
    bind = op.get_bind()
    brands = sa.table(
        "catalog_brands",
        sa.column("id", sa.String),
        sa.column("name", sa.String),
        sa.column("normalized_name", sa.String),
        sa.column("active", sa.Boolean),
    )
    products = sa.table(
        "catalog_products",
        sa.column("id", sa.String),
        sa.column("brand_id", sa.String),
        sa.column("brand", sa.String),
    )
    product_rows = bind.execute(
        sa.select(products.c.id, products.c.brand).order_by(products.c.id)
    ).mappings().all()
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in product_rows:
        normalized = _normalized_name(row["brand"])
        if normalized:
            grouped[normalized].append(dict(row))

    if grouped:
        bind.execute(
            brands.insert(),
            [
                {
                    "id": _brand_id(normalized),
                    "name": sorted(
                        {_display_name(row["brand"]) for row in rows},
                        key=lambda value: (value.casefold(), value),
                    )[0],
                    "normalized_name": normalized,
                    "active": True,
                }
                for normalized, rows in sorted(grouped.items())
            ],
        )
        for normalized, rows in grouped.items():
            bind.execute(
                products.update()
                .where(products.c.id.in_([row["id"] for row in rows]))
                .values(brand_id=_brand_id(normalized))
            )


def upgrade() -> None:
    op.create_table(
        "catalog_brands",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("normalized_name", sa.String(length=128), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "normalized_name", name="uq_catalog_brands_normalized_name"
        ),
    )
    op.create_index("ix_catalog_brands_normalized_name", "catalog_brands", ["normalized_name"])
    op.create_index("ix_catalog_brands_active", "catalog_brands", ["active"])
    with op.batch_alter_table("catalog_products") as batch_op:
        batch_op.add_column(sa.Column("brand_id", sa.String(length=64), nullable=True))
    _backfill()
    with op.batch_alter_table("catalog_products") as batch_op:
        batch_op.create_foreign_key(
            "fk_catalog_products_brand_id_catalog_brands",
            "catalog_brands",
            ["brand_id"],
            ["id"],
        )
        batch_op.create_index("ix_catalog_products_brand_id", ["brand_id"])


def downgrade() -> None:
    with op.batch_alter_table("catalog_products") as batch_op:
        batch_op.drop_index("ix_catalog_products_brand_id")
        batch_op.drop_constraint(
            "fk_catalog_products_brand_id_catalog_brands", type_="foreignkey"
        )
        batch_op.drop_column("brand_id")
    op.drop_index("ix_catalog_brands_active", table_name="catalog_brands")
    op.drop_index("ix_catalog_brands_normalized_name", table_name="catalog_brands")
    op.drop_table("catalog_brands")
