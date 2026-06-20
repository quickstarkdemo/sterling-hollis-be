"""add image job API trace lineage

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-06-19 23:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, Sequence[str], None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "image_generation_jobs",
        sa.Column("api_trace_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "image_generation_jobs",
        sa.Column("api_trace_span_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "image_generation_jobs",
        sa.Column("api_trace_retry_of_job_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_image_generation_jobs_api_trace_id",
        "image_generation_jobs",
        ["api_trace_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_image_generation_jobs_api_trace_id",
        table_name="image_generation_jobs",
    )
    op.drop_column("image_generation_jobs", "api_trace_span_id")
    op.drop_column("image_generation_jobs", "api_trace_retry_of_job_id")
    op.drop_column("image_generation_jobs", "api_trace_id")
