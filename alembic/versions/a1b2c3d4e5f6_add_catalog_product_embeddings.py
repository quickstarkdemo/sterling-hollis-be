"""add catalog product embeddings

Revision ID: a1b2c3d4e5f6
Revises: f3a4b5c6d7e8
Create Date: 2026-04-30 12:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "f3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "catalog_product_embeddings",
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("seed_run_id", sa.String(length=64), nullable=False),
        sa.Column("namespace", sa.String(length=128), nullable=False),
        sa.Column("vector_id", sa.String(length=128), nullable=False),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["catalog_products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["seed_run_id"], ["synthetic_runs.id"]),
        sa.PrimaryKeyConstraint("product_id"),
    )
    op.create_index("ix_catalog_product_embeddings_seed_run_id", "catalog_product_embeddings", ["seed_run_id"])
    op.create_index("ix_catalog_product_embeddings_namespace", "catalog_product_embeddings", ["namespace"])
    op.create_index("ix_catalog_product_embeddings_status", "catalog_product_embeddings", ["status"])


def downgrade() -> None:
    op.drop_index("ix_catalog_product_embeddings_status", table_name="catalog_product_embeddings")
    op.drop_index("ix_catalog_product_embeddings_namespace", table_name="catalog_product_embeddings")
    op.drop_index("ix_catalog_product_embeddings_seed_run_id", table_name="catalog_product_embeddings")
    op.drop_table("catalog_product_embeddings")
