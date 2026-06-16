"""promote Catalog Studio workflows to production domain names

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-06-16 18:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4a5b6c7d8e9"
down_revision: Union[str, Sequence[str], None] = "e3f4a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_workflow_tables(*, legacy: bool) -> None:
    workflows = "openai_demo_runs" if legacy else "catalog_workflows"
    events = "openai_demo_events" if legacy else "catalog_workflow_events"
    event_parent_column = "run_id" if legacy else "workflow_id"
    workflow_prefix = "openai_demo_runs" if legacy else "catalog_workflows"
    event_prefix = "openai_demo_events" if legacy else "catalog_workflow_events"

    op.create_table(
        workflows,
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
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "next_event_sequence > 0",
            name=f"ck_{workflow_prefix}_next_sequence_positive",
        ),
        sa.ForeignKeyConstraint(
            ["draft_revision_id"], ["catalog_draft_revisions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["image_job_id"], ["image_generation_jobs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["published_product_id"], ["catalog_products.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_provider",
            "owner_provider_user_id",
            "idempotency_key_hash",
            name=f"uq_{workflow_prefix}_owner_idempotency",
        ),
    )
    for name, columns in (
        (f"ix_{workflow_prefix}_owner_provider_user_id", ["owner_provider_user_id"]),
        (f"ix_{workflow_prefix}_status", ["status"]),
        (f"ix_{workflow_prefix}_draft_revision_id", ["draft_revision_id"]),
        (f"ix_{workflow_prefix}_image_job_id", ["image_job_id"]),
        (f"ix_{workflow_prefix}_published_product_id", ["published_product_id"]),
        (f"ix_{workflow_prefix}_created_at", ["created_at"]),
        (f"ix_{workflow_prefix}_updated_at", ["updated_at"]),
        (f"ix_{workflow_prefix}_expires_at", ["expires_at"]),
        (f"ix_{workflow_prefix}_owner_created", ["owner_provider_user_id", "created_at"]),
    ):
        op.create_index(name, workflows, columns)

    op.create_table(
        events,
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(event_parent_column, sa.String(length=64), nullable=False),
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
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("sequence > 0", name=f"ck_{event_prefix}_sequence_positive"),
        sa.ForeignKeyConstraint([event_parent_column], [f"{workflows}.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            event_parent_column,
            "sequence",
            name=(
                "uq_openai_demo_events_run_sequence"
                if legacy
                else "uq_catalog_workflow_events_workflow_sequence"
            ),
        ),
        sa.UniqueConstraint(
            event_parent_column,
            "client_event_id",
            name=(
                "uq_openai_demo_events_run_client_event"
                if legacy
                else "uq_catalog_workflow_events_workflow_client_event"
            ),
        ),
    )
    for name, columns in (
        (f"ix_{event_prefix}_{event_parent_column}", [event_parent_column]),
        (f"ix_{event_prefix}_stage", ["stage"]),
        (f"ix_{event_prefix}_capability", ["capability"]),
        (f"ix_{event_prefix}_status", ["status"]),
        (f"ix_{event_prefix}_request_id", ["request_id"]),
        (f"ix_{event_prefix}_payload_expired", ["payload_expired"]),
        (f"ix_{event_prefix}_created_at", ["created_at"]),
        (
            f"ix_{event_prefix}_{'run' if legacy else 'workflow'}_created",
            [event_parent_column, "created_at"],
        ),
    ):
        op.create_index(name, events, columns)


def _copy_workflow_data(*, to_legacy: bool) -> None:
    source_workflows = "catalog_workflows" if to_legacy else "openai_demo_runs"
    target_workflows = "openai_demo_runs" if to_legacy else "catalog_workflows"
    source_events = "catalog_workflow_events" if to_legacy else "openai_demo_events"
    target_events = "openai_demo_events" if to_legacy else "catalog_workflow_events"
    source_event_column = "workflow_id" if to_legacy else "run_id"
    target_event_column = "run_id" if to_legacy else "workflow_id"
    workflow_columns = (
        "id, owner_provider, owner_provider_user_id, idempotency_key_hash, request_hash, "
        "title, business_summary, status, current_stage, next_event_sequence, "
        "draft_revision_id, image_job_id, published_product_id, created_at, updated_at, expires_at"
    )
    source_stage = "workflow" if to_legacy else "run"
    target_stage = "run" if to_legacy else "workflow"
    workflow_select = (
        "id, owner_provider, owner_provider_user_id, idempotency_key_hash, request_hash, "
        "title, business_summary, status, "
        f"CASE WHEN current_stage = '{source_stage}' THEN '{target_stage}' ELSE current_stage END, "
        "next_event_sequence, draft_revision_id, image_job_id, published_product_id, "
        "created_at, updated_at, expires_at"
    )
    event_columns = (
        "id, client_event_id, input_hash, sequence, stage, capability, status, business_summary, "
        "model, request_id, duration_ms, usage_json, moderation_json, request_json, response_json, "
        "error_code, retryable, payload_expired, started_at, completed_at, created_at"
    )
    event_select = (
        "id, client_event_id, input_hash, sequence, "
        f"CASE WHEN stage = '{source_stage}' THEN '{target_stage}' ELSE stage END, "
        f"CASE WHEN capability = '{source_stage}' THEN '{target_stage}' ELSE capability END, "
        "status, business_summary, model, request_id, duration_ms, usage_json, moderation_json, "
        "request_json, response_json, error_code, retryable, payload_expired, started_at, "
        "completed_at, created_at"
    )
    op.execute(
        sa.text(
            f"INSERT INTO {target_workflows} ({workflow_columns}) "
            f"SELECT {workflow_select} FROM {source_workflows}"
        )
    )
    op.execute(
        sa.text(
            f"INSERT INTO {target_events} ({target_event_column}, {event_columns}) "
            f"SELECT {source_event_column}, {event_select} FROM {source_events}"
        )
    )


def _drop_workflow_tables(*, legacy: bool) -> None:
    workflows = "openai_demo_runs" if legacy else "catalog_workflows"
    events = "openai_demo_events" if legacy else "catalog_workflow_events"
    op.drop_table(events)
    op.drop_table(workflows)


def upgrade() -> None:
    _create_workflow_tables(legacy=False)
    _copy_workflow_data(to_legacy=False)
    _drop_workflow_tables(legacy=True)


def downgrade() -> None:
    _create_workflow_tables(legacy=True)
    _copy_workflow_data(to_legacy=True)
    _drop_workflow_tables(legacy=False)
