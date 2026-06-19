"""add catalog authoring v3 suggestion state

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-06-19 13:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "catalog_suggestion_sets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_provider", sa.String(length=32), nullable=False),
        sa.Column("owner_provider_user_id", sa.String(length=255), nullable=False),
        sa.Column("catalog_product_id", sa.String(length=64), nullable=False),
        sa.Column("base_draft_revision_id", sa.String(length=64), nullable=False),
        sa.Column("base_draft_version", sa.Integer(), nullable=False),
        sa.Column("current_draft_revision_id", sa.String(length=64), nullable=False),
        sa.Column("current_draft_version", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "base_draft_version > 0",
            name="ck_catalog_suggestion_sets_base_version_positive",
        ),
        sa.CheckConstraint(
            "current_draft_version > 0",
            name="ck_catalog_suggestion_sets_current_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["base_draft_revision_id"],
            ["catalog_draft_revisions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["current_draft_revision_id"],
            ["catalog_draft_revisions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["catalog_workflows.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, columns in (
        ("ix_catalog_suggestion_sets_owner_provider_user_id", ["owner_provider_user_id"]),
        ("ix_catalog_suggestion_sets_catalog_product_id", ["catalog_product_id"]),
        ("ix_catalog_suggestion_sets_base_draft_revision_id", ["base_draft_revision_id"]),
        ("ix_catalog_suggestion_sets_current_draft_revision_id", ["current_draft_revision_id"]),
        ("ix_catalog_suggestion_sets_workflow_id", ["workflow_id"]),
        ("ix_catalog_suggestion_sets_status", ["status"]),
        ("ix_catalog_suggestion_sets_created_at", ["created_at"]),
        (
            "ix_catalog_suggestion_sets_owner_product_created",
            ["owner_provider", "owner_provider_user_id", "catalog_product_id", "created_at"],
        ),
    ):
        op.create_index(name, "catalog_suggestion_sets", columns)

    op.create_table(
        "catalog_field_suggestions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("suggestion_set_id", sa.String(length=64), nullable=False),
        sa.Column("section", sa.String(length=32), nullable=False),
        sa.Column("target_path", sa.String(length=255), nullable=False),
        sa.Column("proposed_value_json", sa.JSON(), nullable=False),
        sa.Column("baseline_value_json", sa.JSON(), nullable=False),
        sa.Column("prior_value_json", sa.JSON(), nullable=True),
        sa.Column("evidence_asset_ids_json", sa.JSON(), nullable=False),
        sa.Column("certainty_class", sa.String(length=32), nullable=False),
        sa.Column("input_origin", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.String(length=1000), nullable=True),
        sa.Column("applied_draft_revision_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["suggestion_set_id"], ["catalog_suggestion_sets.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["applied_draft_revision_id"],
            ["catalog_draft_revisions.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "suggestion_set_id",
            "target_path",
            name="uq_catalog_field_suggestions_set_target",
        ),
    )
    for name, columns in (
        ("ix_catalog_field_suggestions_suggestion_set_id", ["suggestion_set_id"]),
        ("ix_catalog_field_suggestions_section", ["section"]),
        ("ix_catalog_field_suggestions_status", ["status"]),
        (
            "ix_catalog_field_suggestions_applied_draft_revision_id",
            ["applied_draft_revision_id"],
        ),
        ("ix_catalog_field_suggestions_created_at", ["created_at"]),
    ):
        op.create_index(name, "catalog_field_suggestions", columns)

    op.create_table(
        "catalog_suggestion_reviews",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("suggestion_set_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("target_json", sa.JSON(), nullable=False),
        sa.Column("expected_draft_version", sa.Integer(), nullable=False),
        sa.Column("resulting_draft_revision_id", sa.String(length=64), nullable=True),
        sa.Column("actor_provider", sa.String(length=32), nullable=False),
        sa.Column("actor_provider_user_id", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "expected_draft_version > 0",
            name="ck_catalog_suggestion_reviews_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["suggestion_set_id"], ["catalog_suggestion_sets.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["resulting_draft_revision_id"],
            ["catalog_draft_revisions.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, columns in (
        ("ix_catalog_suggestion_reviews_suggestion_set_id", ["suggestion_set_id"]),
        (
            "ix_catalog_suggestion_reviews_resulting_draft_revision_id",
            ["resulting_draft_revision_id"],
        ),
        ("ix_catalog_suggestion_reviews_created_at", ["created_at"]),
    ):
        op.create_index(name, "catalog_suggestion_reviews", columns)


def downgrade() -> None:
    for name in (
        "ix_catalog_suggestion_reviews_created_at",
        "ix_catalog_suggestion_reviews_resulting_draft_revision_id",
        "ix_catalog_suggestion_reviews_suggestion_set_id",
    ):
        op.drop_index(name, table_name="catalog_suggestion_reviews")
    op.drop_table("catalog_suggestion_reviews")

    for name in (
        "ix_catalog_field_suggestions_created_at",
        "ix_catalog_field_suggestions_applied_draft_revision_id",
        "ix_catalog_field_suggestions_status",
        "ix_catalog_field_suggestions_section",
        "ix_catalog_field_suggestions_suggestion_set_id",
    ):
        op.drop_index(name, table_name="catalog_field_suggestions")
    op.drop_table("catalog_field_suggestions")

    for name in (
        "ix_catalog_suggestion_sets_owner_product_created",
        "ix_catalog_suggestion_sets_created_at",
        "ix_catalog_suggestion_sets_status",
        "ix_catalog_suggestion_sets_workflow_id",
        "ix_catalog_suggestion_sets_current_draft_revision_id",
        "ix_catalog_suggestion_sets_base_draft_revision_id",
        "ix_catalog_suggestion_sets_catalog_product_id",
        "ix_catalog_suggestion_sets_owner_provider_user_id",
    ):
        op.drop_index(name, table_name="catalog_suggestion_sets")
    op.drop_table("catalog_suggestion_sets")
