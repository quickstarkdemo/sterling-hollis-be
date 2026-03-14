"""add index jobs

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-03-14 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "index_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("batch_size", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempted", sa.Integer(), nullable=False),
        sa.Column("indexed", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("status_breakdown", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["synthetic_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_index_jobs_run_id"), "index_jobs", ["run_id"], unique=False)
    op.create_index(op.f("ix_index_jobs_status"), "index_jobs", ["status"], unique=False)
    op.create_index(op.f("ix_index_jobs_created_at"), "index_jobs", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_index_jobs_created_at"), table_name="index_jobs")
    op.drop_index(op.f("ix_index_jobs_status"), table_name="index_jobs")
    op.drop_index(op.f("ix_index_jobs_run_id"), table_name="index_jobs")
    op.drop_table("index_jobs")
