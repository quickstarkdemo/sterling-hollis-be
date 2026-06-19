"""add product review moderation

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-06-19 15:15:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "catalog_product_reviews",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("catalog_product_id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("external_review_id", sa.String(length=128), nullable=False),
        sa.Column("author_display_name", sa.String(length=128), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("rating >= 1 AND rating <= 5", name="ck_catalog_product_reviews_rating"),
        sa.ForeignKeyConstraint(["catalog_product_id"], ["catalog_products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_review_id", name="uq_catalog_product_reviews_source_external"),
    )
    op.create_index("ix_catalog_product_reviews_catalog_product_id", "catalog_product_reviews", ["catalog_product_id"])
    op.create_index("ix_catalog_product_reviews_created_at", "catalog_product_reviews", ["created_at"])
    op.create_index(
        "ix_catalog_product_reviews_product_submitted",
        "catalog_product_reviews",
        ["catalog_product_id", "submitted_at"],
    )

    op.create_table(
        "catalog_review_moderations",
        sa.Column("review_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("state", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("ai_categories_json", sa.JSON(), nullable=False),
        sa.Column("ai_theme_summary", sa.String(length=1000), nullable=True),
        sa.Column("ai_suggested_action", sa.String(length=32), nullable=True),
        sa.Column("ai_provider_metadata_json", sa.JSON(), nullable=False),
        sa.Column("response_draft", sa.String(length=2000), nullable=True),
        sa.Column("response_published", sa.String(length=2000), nullable=True),
        sa.Column("response_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(length=255), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.String(length=1000), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_catalog_review_moderations_version"),
        sa.CheckConstraint(
            "state IN ('pending', 'approved', 'flagged', 'rejected')",
            name="ck_catalog_review_moderations_state",
        ),
        sa.ForeignKeyConstraint(["review_id"], ["catalog_product_reviews.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("review_id"),
    )
    op.create_index("ix_catalog_review_moderations_state", "catalog_review_moderations", ["state"])

    op.create_table(
        "catalog_review_actions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("review_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("resulting_version", sa.Integer(), nullable=False),
        sa.Column("actor_provider", sa.String(length=32), nullable=False),
        sa.Column("actor_provider_user_id", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("expected_version > 0", name="ck_catalog_review_actions_expected_version"),
        sa.CheckConstraint("resulting_version > 0", name="ck_catalog_review_actions_resulting_version"),
        sa.ForeignKeyConstraint(["review_id"], ["catalog_product_reviews.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    for name, columns in (
        ("ix_catalog_review_actions_review_id", ["review_id"]),
        ("ix_catalog_review_actions_action", ["action"]),
        ("ix_catalog_review_actions_actor_provider_user_id", ["actor_provider_user_id"]),
        ("ix_catalog_review_actions_created_at", ["created_at"]),
    ):
        op.create_index(name, "catalog_review_actions", columns)


def downgrade() -> None:
    op.drop_table("catalog_review_actions")
    op.drop_table("catalog_review_moderations")
    op.drop_table("catalog_product_reviews")
