"""add safe API trace projection

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-06-19 22:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, Sequence[str], None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_traces",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("projection_version", sa.String(length=16), server_default="1.0", nullable=False),
        sa.Column("owner_provider", sa.String(length=32), nullable=False),
        sa.Column("owner_provider_user_id", sa.String(length=255), nullable=False),
        sa.Column("surface", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("root_span_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("attributes_json", sa.JSON(), nullable=False),
        sa.Column("truncation_json", sa.JSON(), nullable=False),
        sa.Column("payload_expired", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_api_traces_duration"),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, columns in (
        ("ix_api_traces_owner_provider_user_id", ["owner_provider_user_id"]),
        ("ix_api_traces_surface", ["surface"]),
        ("ix_api_traces_status", ["status"]),
        ("ix_api_traces_payload_expired", ["payload_expired"]),
        ("ix_api_traces_payload_expires_at", ["payload_expires_at"]),
        ("ix_api_traces_metadata_expires_at", ["metadata_expires_at"]),
        ("ix_api_traces_created_at", ["created_at"]),
        ("ix_api_traces_owner_created", ["owner_provider_user_id", "created_at"]),
    ):
        op.create_index(name, "api_traces", columns)

    op.create_table(
        "api_trace_spans",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("span_id", sa.String(length=64), nullable=False),
        sa.Column("parent_span_id", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("service", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("attributes_json", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_api_trace_spans_duration"),
        sa.ForeignKeyConstraint(["trace_id"], ["api_traces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id", "span_id", name="uq_api_trace_spans_trace_span"),
    )
    for name, columns in (
        ("ix_api_trace_spans_trace_id", ["trace_id"]),
        ("ix_api_trace_spans_parent_span_id", ["parent_span_id"]),
        ("ix_api_trace_spans_operation", ["operation"]),
        ("ix_api_trace_spans_status", ["status"]),
        ("ix_api_trace_spans_trace_started", ["trace_id", "started_at"]),
    ):
        op.create_index(name, "api_trace_spans", columns)

    op.create_table(
        "api_trace_links",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("link_id", sa.String(length=64), nullable=False),
        sa.Column("span_id", sa.String(length=64), nullable=True),
        sa.Column("linked_trace_id", sa.String(length=64), nullable=False),
        sa.Column("linked_span_id", sa.String(length=64), nullable=True),
        sa.Column("relationship", sa.String(length=32), nullable=False),
        sa.Column("attributes_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["trace_id"], ["api_traces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id", "link_id", name="uq_api_trace_links_trace_link"),
    )
    for name, columns in (
        ("ix_api_trace_links_trace_id", ["trace_id"]),
        ("ix_api_trace_links_span_id", ["span_id"]),
        ("ix_api_trace_links_linked_trace_id", ["linked_trace_id"]),
    ):
        op.create_index(name, "api_trace_links", columns)

    op.create_table(
        "api_trace_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("span_id", sa.String(length=64), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("attributes_json", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("sequence >= 0", name="ck_api_trace_events_sequence"),
        sa.ForeignKeyConstraint(["trace_id"], ["api_traces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id", "event_id", name="uq_api_trace_events_trace_event"),
    )
    for name, columns in (
        ("ix_api_trace_events_trace_id", ["trace_id"]),
        ("ix_api_trace_events_span_id", ["span_id"]),
        ("ix_api_trace_events_event_type", ["event_type"]),
        ("ix_api_trace_events_status", ["status"]),
        ("ix_api_trace_events_trace_sequence", ["trace_id", "sequence"]),
    ):
        op.create_index(name, "api_trace_events", columns)

    op.create_table(
        "api_trace_artifacts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column("span_id", sa.String(length=64), nullable=True),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("attributes_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_api_trace_artifacts_size"),
        sa.ForeignKeyConstraint(["trace_id"], ["api_traces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id", "artifact_id", name="uq_api_trace_artifacts_trace_artifact"),
    )
    for name, columns in (
        ("ix_api_trace_artifacts_trace_id", ["trace_id"]),
        ("ix_api_trace_artifacts_span_id", ["span_id"]),
        ("ix_api_trace_artifacts_artifact_type", ["artifact_type"]),
    ):
        op.create_index(name, "api_trace_artifacts", columns)


def downgrade() -> None:
    op.drop_table("api_trace_artifacts")
    op.drop_table("api_trace_events")
    op.drop_table("api_trace_links")
    op.drop_table("api_trace_spans")
    op.drop_table("api_traces")
