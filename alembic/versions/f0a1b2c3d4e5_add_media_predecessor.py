"""add media predecessor lineage

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-06-18 17:30:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f0a1b2c3d4e5"
down_revision: Union[str, Sequence[str], None] = "e9f0a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("product_media_assets") as batch_op:
        batch_op.add_column(
            sa.Column("predecessor_media_id", sa.String(length=64), nullable=True)
        )
        batch_op.create_index(
            "ix_product_media_assets_predecessor_media_id",
            ["predecessor_media_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("product_media_assets") as batch_op:
        batch_op.drop_index("ix_product_media_assets_predecessor_media_id")
        batch_op.drop_column("predecessor_media_id")
