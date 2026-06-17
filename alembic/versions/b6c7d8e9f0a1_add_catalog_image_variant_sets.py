"""add Catalog Studio image variant sets

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-06-17 16:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b6c7d8e9f0a1"
down_revision: Union[str, Sequence[str], None] = "a5b6c7d8e9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("image_generation_jobs") as batch_op:
        batch_op.add_column(
            sa.Column("image_variant_set_id", sa.String(length=64), nullable=True)
        )
        batch_op.create_index(
            "ix_image_generation_jobs_image_variant_set_id",
            ["image_variant_set_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("image_generation_jobs") as batch_op:
        batch_op.drop_index("ix_image_generation_jobs_image_variant_set_id")
        batch_op.drop_column("image_variant_set_id")
