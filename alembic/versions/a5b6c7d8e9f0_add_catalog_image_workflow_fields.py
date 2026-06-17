"""add Catalog Studio image workflow fields

Revision ID: a5b6c7d8e9f0
Revises: f4a5b6c7d8e9
Create Date: 2026-06-17 12:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a5b6c7d8e9f0"
down_revision: Union[str, Sequence[str], None] = "f4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("image_generation_jobs") as batch_op:
        batch_op.add_column(sa.Column("workflow_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("draft_revision_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("expected_draft_version", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("requested_action", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("requested_variant_index", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("idempotency_key_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("request_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("refinement_prompt", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("source_image_path", sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            "fk_image_generation_jobs_workflow_id_catalog_workflows",
            "catalog_workflows",
            ["workflow_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_image_generation_jobs_draft_revision_id_catalog_draft_revisions",
            "catalog_draft_revisions",
            ["draft_revision_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_image_generation_jobs_workflow_id", ["workflow_id"])
        batch_op.create_index("ix_image_generation_jobs_draft_revision_id", ["draft_revision_id"])
        batch_op.create_unique_constraint(
            "uq_image_generation_jobs_workflow_idempotency",
            ["workflow_id", "idempotency_key_hash"],
        )


def downgrade() -> None:
    with op.batch_alter_table("image_generation_jobs") as batch_op:
        batch_op.drop_constraint(
            "uq_image_generation_jobs_workflow_idempotency", type_="unique"
        )
        batch_op.drop_index("ix_image_generation_jobs_draft_revision_id")
        batch_op.drop_index("ix_image_generation_jobs_workflow_id")
        batch_op.drop_constraint(
            "fk_image_generation_jobs_draft_revision_id_catalog_draft_revisions",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_image_generation_jobs_workflow_id_catalog_workflows", type_="foreignkey"
        )
        for column in (
            "source_image_path",
            "refinement_prompt",
            "request_hash",
            "idempotency_key_hash",
            "requested_variant_index",
            "requested_action",
            "expected_draft_version",
            "draft_revision_id",
            "workflow_id",
        ):
            batch_op.drop_column(column)
