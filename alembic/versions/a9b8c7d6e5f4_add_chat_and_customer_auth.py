"""add chat and customer auth identity tables

Revision ID: a9b8c7d6e5f4
Revises: a1b2c3d4e5f6
Create Date: 2026-04-30 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a9b8c7d6e5f4"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_auth_identities",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_user_id", sa.String(length=255), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "provider_user_id", name="uq_customer_auth_identities_provider_user"),
    )
    op.create_index("ix_customer_auth_identities_customer_id", "customer_auth_identities", ["customer_id"])
    op.create_index("ix_customer_auth_identities_email", "customer_auth_identities", ["email"])
    op.create_index("ix_customer_auth_identities_last_seen_at", "customer_auth_identities", ["last_seen_at"])

    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("provider_user_id", sa.String(length=255), nullable=True),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_sessions_created_at", "chat_sessions", ["created_at"])
    op.create_index("ix_chat_sessions_customer_id", "chat_sessions", ["customer_id"])
    op.create_index("ix_chat_sessions_provider_user_id", "chat_sessions", ["provider_user_id"])
    op.create_index("ix_chat_sessions_updated_at", "chat_sessions", ["updated_at"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_messages_created_at", "chat_messages", ["created_at"])
    op.create_index("ix_chat_messages_session_id", "chat_messages", ["session_id"])

    op.create_table(
        "chat_tool_calls",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.String(length=64), nullable=True),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["chat_messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_tool_calls_created_at", "chat_tool_calls", ["created_at"])
    op.create_index("ix_chat_tool_calls_message_id", "chat_tool_calls", ["message_id"])
    op.create_index("ix_chat_tool_calls_session_id", "chat_tool_calls", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_tool_calls_session_id", table_name="chat_tool_calls")
    op.drop_index("ix_chat_tool_calls_message_id", table_name="chat_tool_calls")
    op.drop_index("ix_chat_tool_calls_created_at", table_name="chat_tool_calls")
    op.drop_table("chat_tool_calls")

    op.drop_index("ix_chat_messages_session_id", table_name="chat_messages")
    op.drop_index("ix_chat_messages_created_at", table_name="chat_messages")
    op.drop_table("chat_messages")

    op.drop_index("ix_chat_sessions_updated_at", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_provider_user_id", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_customer_id", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_created_at", table_name="chat_sessions")
    op.drop_table("chat_sessions")

    op.drop_index("ix_customer_auth_identities_last_seen_at", table_name="customer_auth_identities")
    op.drop_index("ix_customer_auth_identities_email", table_name="customer_auth_identities")
    op.drop_index("ix_customer_auth_identities_customer_id", table_name="customer_auth_identities")
    op.drop_table("customer_auth_identities")
