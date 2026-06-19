"""add catalog source bundles

Revision ID: a2b3c4d5e6f7
Revises: f0a1b2c3d4e5
Create Date: 2026-06-19 12:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "f0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "catalog_source_bundles",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_provider", sa.String(length=32), nullable=False),
        sa.Column("owner_provider_user_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("catalog_product_id", sa.String(length=64), nullable=True),
        sa.Column("draft_revision_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["draft_revision_id"], ["catalog_draft_revisions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_catalog_source_bundles_owner_provider_user_id",
        "catalog_source_bundles",
        ["owner_provider_user_id"],
    )
    op.create_index(
        "ix_catalog_source_bundles_catalog_product_id",
        "catalog_source_bundles",
        ["catalog_product_id"],
    )
    op.create_index(
        "ix_catalog_source_bundles_draft_revision_id",
        "catalog_source_bundles",
        ["draft_revision_id"],
    )
    op.create_index(
        "ix_catalog_source_bundles_status",
        "catalog_source_bundles",
        ["status"],
    )
    op.create_index(
        "ix_catalog_source_bundles_created_at",
        "catalog_source_bundles",
        ["created_at"],
    )
    op.create_index(
        "ix_catalog_source_bundles_owner_created",
        "catalog_source_bundles",
        ["owner_provider", "owner_provider_user_id", "created_at"],
    )

    op.create_table(
        "catalog_source_assets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("bundle_id", sa.String(length=64), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "storage_provider",
            sa.String(length=32),
            server_default="local_private",
            nullable=False,
        ),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("preview_storage_key", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="ready", nullable=False),
        sa.Column("promoted_media_id", sa.String(length=64), nullable=True),
        sa.Column("promoted_draft_revision_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "byte_size > 0", name="ck_catalog_source_assets_byte_size_positive"
        ),
        sa.CheckConstraint("width > 0", name="ck_catalog_source_assets_width_positive"),
        sa.CheckConstraint("height > 0", name="ck_catalog_source_assets_height_positive"),
        sa.ForeignKeyConstraint(
            ["bundle_id"], ["catalog_source_bundles.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["promoted_draft_revision_id"],
            ["catalog_draft_revisions.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bundle_id",
            "display_order",
            name="uq_catalog_source_assets_bundle_display_order",
        ),
    )
    op.create_index(
        "ix_catalog_source_assets_bundle_id", "catalog_source_assets", ["bundle_id"]
    )
    op.create_index(
        "ix_catalog_source_assets_checksum_sha256",
        "catalog_source_assets",
        ["checksum_sha256"],
    )
    op.create_index(
        "ix_catalog_source_assets_status", "catalog_source_assets", ["status"]
    )
    op.create_index(
        "ix_catalog_source_assets_promoted_draft_revision_id",
        "catalog_source_assets",
        ["promoted_draft_revision_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_catalog_source_assets_promoted_draft_revision_id",
        table_name="catalog_source_assets",
    )
    op.drop_index("ix_catalog_source_assets_status", table_name="catalog_source_assets")
    op.drop_index(
        "ix_catalog_source_assets_checksum_sha256", table_name="catalog_source_assets"
    )
    op.drop_index("ix_catalog_source_assets_bundle_id", table_name="catalog_source_assets")
    op.drop_table("catalog_source_assets")
    op.drop_index(
        "ix_catalog_source_bundles_owner_created", table_name="catalog_source_bundles"
    )
    op.drop_index("ix_catalog_source_bundles_created_at", table_name="catalog_source_bundles")
    op.drop_index("ix_catalog_source_bundles_status", table_name="catalog_source_bundles")
    op.drop_index(
        "ix_catalog_source_bundles_draft_revision_id", table_name="catalog_source_bundles"
    )
    op.drop_index(
        "ix_catalog_source_bundles_catalog_product_id", table_name="catalog_source_bundles"
    )
    op.drop_index(
        "ix_catalog_source_bundles_owner_provider_user_id",
        table_name="catalog_source_bundles",
    )
    op.drop_table("catalog_source_bundles")
