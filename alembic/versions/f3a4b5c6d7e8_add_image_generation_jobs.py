"""add image generation jobs

Revision ID: f3a4b5c6d7e8
Revises: f2a3b4c5d6e7
Create Date: 2026-04-29 18:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3a4b5c6d7e8"
down_revision: Union[str, Sequence[str], None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "image_generation_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("store_id", sa.String(length=64), nullable=True),
        sa.Column("product_id", sa.String(length=64), nullable=True),
        sa.Column("variant_id", sa.String(length=64), nullable=True),
        sa.Column("category", sa.String(length=128), nullable=True),
        sa.Column("brand", sa.String(length=128), nullable=True),
        sa.Column("requested_limit", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("detail_count", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("thumbnail_size", sa.Integer(), nullable=False, server_default="320"),
        sa.Column("overwrite", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("missing_images_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("size", sa.String(length=32), nullable=False),
        sa.Column("quality", sa.String(length=32), nullable=False),
        sa.Column("output_format", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("attempted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("generated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status_breakdown", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("result_sample", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["product_id"], ["catalog_products.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["synthetic_runs.id"]),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"]),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_image_generation_jobs_run_id", "image_generation_jobs", ["run_id"])
    op.create_index("ix_image_generation_jobs_store_id", "image_generation_jobs", ["store_id"])
    op.create_index("ix_image_generation_jobs_product_id", "image_generation_jobs", ["product_id"])
    op.create_index("ix_image_generation_jobs_variant_id", "image_generation_jobs", ["variant_id"])
    op.create_index("ix_image_generation_jobs_category", "image_generation_jobs", ["category"])
    op.create_index("ix_image_generation_jobs_brand", "image_generation_jobs", ["brand"])
    op.create_index("ix_image_generation_jobs_status", "image_generation_jobs", ["status"])
    op.create_index("ix_image_generation_jobs_created_at", "image_generation_jobs", ["created_at"])
    op.create_index("ix_image_generation_jobs_status_created", "image_generation_jobs", ["status", "created_at"])
    op.create_index("ix_image_generation_jobs_category_brand", "image_generation_jobs", ["category", "brand"])


def downgrade() -> None:
    op.drop_index("ix_image_generation_jobs_category_brand", table_name="image_generation_jobs")
    op.drop_index("ix_image_generation_jobs_status_created", table_name="image_generation_jobs")
    op.drop_index("ix_image_generation_jobs_created_at", table_name="image_generation_jobs")
    op.drop_index("ix_image_generation_jobs_status", table_name="image_generation_jobs")
    op.drop_index("ix_image_generation_jobs_brand", table_name="image_generation_jobs")
    op.drop_index("ix_image_generation_jobs_category", table_name="image_generation_jobs")
    op.drop_index("ix_image_generation_jobs_variant_id", table_name="image_generation_jobs")
    op.drop_index("ix_image_generation_jobs_product_id", table_name="image_generation_jobs")
    op.drop_index("ix_image_generation_jobs_store_id", table_name="image_generation_jobs")
    op.drop_index("ix_image_generation_jobs_run_id", table_name="image_generation_jobs")
    op.drop_table("image_generation_jobs")
