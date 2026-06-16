"""add catalog draft lifecycle

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-06-16 14:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("catalog_products") as batch_op:
        batch_op.add_column(
            sa.Column(
                "lifecycle_status",
                sa.String(length=32),
                nullable=False,
                server_default="published",
            )
        )
        batch_op.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            )
        )
    op.create_index("ix_catalog_products_lifecycle_status", "catalog_products", ["lifecycle_status"])

    op.create_table(
        "catalog_draft_revisions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("catalog_product_id", sa.String(length=64), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("moderation_state", sa.String(length=32), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_catalog_draft_revisions_catalog_product_id",
        "catalog_draft_revisions",
        ["catalog_product_id"],
    )
    op.create_index("ix_catalog_draft_revisions_status", "catalog_draft_revisions", ["status"])
    op.create_index(
        "ix_catalog_draft_revisions_moderation_state",
        "catalog_draft_revisions",
        ["moderation_state"],
    )
    op.create_index("ix_catalog_draft_revisions_created_by", "catalog_draft_revisions", ["created_by"])
    op.create_index(
        "ix_catalog_draft_product_created",
        "catalog_draft_revisions",
        ["catalog_product_id", "created_at"],
    )

    op.create_table(
        "catalog_admin_mutations",
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_catalog_admin_mutations_created_by",
        "catalog_admin_mutations",
        ["created_by"],
    )


def downgrade() -> None:
    op.drop_index("ix_catalog_admin_mutations_created_by", table_name="catalog_admin_mutations")
    op.drop_table("catalog_admin_mutations")
    op.drop_index("ix_catalog_draft_product_created", table_name="catalog_draft_revisions")
    op.drop_index("ix_catalog_draft_revisions_created_by", table_name="catalog_draft_revisions")
    op.drop_index("ix_catalog_draft_revisions_moderation_state", table_name="catalog_draft_revisions")
    op.drop_index("ix_catalog_draft_revisions_status", table_name="catalog_draft_revisions")
    op.drop_index("ix_catalog_draft_revisions_catalog_product_id", table_name="catalog_draft_revisions")
    op.drop_table("catalog_draft_revisions")
    op.drop_index("ix_catalog_products_lifecycle_status", table_name="catalog_products")
    with op.batch_alter_table("catalog_products") as batch_op:
        batch_op.drop_column("updated_at")
        batch_op.drop_column("version")
        batch_op.drop_column("lifecycle_status")
