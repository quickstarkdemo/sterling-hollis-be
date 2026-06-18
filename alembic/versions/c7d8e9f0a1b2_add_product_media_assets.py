"""add product media assets

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-06-18 12:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7d8e9f0a1b2"
down_revision: Union[str, Sequence[str], None] = "b6c7d8e9f0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_media_assets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("catalog_product_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("intent", sa.String(length=32), nullable=False),
        sa.Column("source_media_id", sa.String(length=64), nullable=True),
        sa.Column("image_set", sa.JSON(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["catalog_product_id"], ["catalog_products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "catalog_product_id",
            "display_order",
            name="uq_product_media_assets_product_display_order",
        ),
    )
    op.create_index(
        "ix_product_media_assets_catalog_product_id",
        "product_media_assets",
        ["catalog_product_id"],
    )
    op.create_index(
        "ix_product_media_assets_product_role",
        "product_media_assets",
        ["catalog_product_id", "role"],
    )
    with op.batch_alter_table("image_generation_jobs") as batch_op:
        batch_op.add_column(sa.Column("source_media_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("target_media_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("requested_intent", sa.String(length=32), nullable=True))
        batch_op.create_index("ix_image_generation_jobs_source_media_id", ["source_media_id"])
        batch_op.create_index("ix_image_generation_jobs_target_media_id", ["target_media_id"])


def downgrade() -> None:
    with op.batch_alter_table("image_generation_jobs") as batch_op:
        batch_op.drop_index("ix_image_generation_jobs_target_media_id")
        batch_op.drop_index("ix_image_generation_jobs_source_media_id")
        batch_op.drop_column("requested_intent")
        batch_op.drop_column("target_media_id")
        batch_op.drop_column("source_media_id")
    op.drop_index("ix_product_media_assets_product_role", table_name="product_media_assets")
    op.drop_index("ix_product_media_assets_catalog_product_id", table_name="product_media_assets")
    op.drop_table("product_media_assets")
