"""add image generation job heartbeat

Revision ID: b1c2d3e4f5a6
Revises: a9b8c7d6e5f4
Create Date: 2026-05-02 13:20:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "a9b8c7d6e5f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "image_generation_jobs",
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_image_generation_jobs_status_heartbeat",
        "image_generation_jobs",
        ["status", "last_heartbeat_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_image_generation_jobs_status_heartbeat", table_name="image_generation_jobs")
    op.drop_column("image_generation_jobs", "last_heartbeat_at")
