"""add sanitized OpenAI demo runs and events

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-06-16 16:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e3f4a5b6c7d8"
down_revision: Union[str, Sequence[str], None] = "d2e3f4a5b6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "openai_demo_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_provider", sa.String(length=32), nullable=False),
        sa.Column("owner_provider_user_id", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("business_summary", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_stage", sa.String(length=64), nullable=False),
        sa.Column("next_event_sequence", sa.Integer(), nullable=False),
        sa.Column("draft_revision_id", sa.String(length=64), nullable=True),
        sa.Column("image_job_id", sa.String(length=64), nullable=True),
        sa.Column("published_product_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "next_event_sequence > 0",
            name="ck_openai_demo_runs_next_sequence_positive",
        ),
        sa.ForeignKeyConstraint(
            ["draft_revision_id"],
            ["catalog_draft_revisions.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["image_job_id"],
            ["image_generation_jobs.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["published_product_id"],
            ["catalog_products.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_provider",
            "owner_provider_user_id",
            "idempotency_key_hash",
            name="uq_openai_demo_runs_owner_idempotency",
        ),
    )
    op.create_index(
        "ix_openai_demo_runs_owner_provider_user_id",
        "openai_demo_runs",
        ["owner_provider_user_id"],
    )
    op.create_index("ix_openai_demo_runs_status", "openai_demo_runs", ["status"])
    op.create_index("ix_openai_demo_runs_draft_revision_id", "openai_demo_runs", ["draft_revision_id"])
    op.create_index("ix_openai_demo_runs_image_job_id", "openai_demo_runs", ["image_job_id"])
    op.create_index(
        "ix_openai_demo_runs_published_product_id",
        "openai_demo_runs",
        ["published_product_id"],
    )
    op.create_index("ix_openai_demo_runs_created_at", "openai_demo_runs", ["created_at"])
    op.create_index("ix_openai_demo_runs_updated_at", "openai_demo_runs", ["updated_at"])
    op.create_index("ix_openai_demo_runs_expires_at", "openai_demo_runs", ["expires_at"])
    op.create_index(
        "ix_openai_demo_runs_owner_created",
        "openai_demo_runs",
        ["owner_provider_user_id", "created_at"],
    )

    op.create_table(
        "openai_demo_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("client_event_id", sa.String(length=128), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("capability", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("business_summary", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("usage_json", sa.JSON(), nullable=False),
        sa.Column("moderation_json", sa.JSON(), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("payload_expired", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("sequence > 0", name="ck_openai_demo_events_sequence_positive"),
        sa.ForeignKeyConstraint(["run_id"], ["openai_demo_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_openai_demo_events_run_sequence"),
        sa.UniqueConstraint(
            "run_id",
            "client_event_id",
            name="uq_openai_demo_events_run_client_event",
        ),
    )
    op.create_index("ix_openai_demo_events_run_id", "openai_demo_events", ["run_id"])
    op.create_index("ix_openai_demo_events_stage", "openai_demo_events", ["stage"])
    op.create_index("ix_openai_demo_events_capability", "openai_demo_events", ["capability"])
    op.create_index("ix_openai_demo_events_status", "openai_demo_events", ["status"])
    op.create_index("ix_openai_demo_events_request_id", "openai_demo_events", ["request_id"])
    op.create_index(
        "ix_openai_demo_events_payload_expired",
        "openai_demo_events",
        ["payload_expired"],
    )
    op.create_index("ix_openai_demo_events_created_at", "openai_demo_events", ["created_at"])
    op.create_index(
        "ix_openai_demo_events_run_created",
        "openai_demo_events",
        ["run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_openai_demo_events_run_created", table_name="openai_demo_events")
    op.drop_index("ix_openai_demo_events_created_at", table_name="openai_demo_events")
    op.drop_index("ix_openai_demo_events_payload_expired", table_name="openai_demo_events")
    op.drop_index("ix_openai_demo_events_request_id", table_name="openai_demo_events")
    op.drop_index("ix_openai_demo_events_status", table_name="openai_demo_events")
    op.drop_index("ix_openai_demo_events_capability", table_name="openai_demo_events")
    op.drop_index("ix_openai_demo_events_stage", table_name="openai_demo_events")
    op.drop_index("ix_openai_demo_events_run_id", table_name="openai_demo_events")
    op.drop_table("openai_demo_events")
    op.drop_index("ix_openai_demo_runs_owner_created", table_name="openai_demo_runs")
    op.drop_index("ix_openai_demo_runs_expires_at", table_name="openai_demo_runs")
    op.drop_index("ix_openai_demo_runs_updated_at", table_name="openai_demo_runs")
    op.drop_index("ix_openai_demo_runs_created_at", table_name="openai_demo_runs")
    op.drop_index("ix_openai_demo_runs_published_product_id", table_name="openai_demo_runs")
    op.drop_index("ix_openai_demo_runs_image_job_id", table_name="openai_demo_runs")
    op.drop_index("ix_openai_demo_runs_draft_revision_id", table_name="openai_demo_runs")
    op.drop_index("ix_openai_demo_runs_status", table_name="openai_demo_runs")
    op.drop_index(
        "ix_openai_demo_runs_owner_provider_user_id",
        table_name="openai_demo_runs",
    )
    op.drop_table("openai_demo_runs")
