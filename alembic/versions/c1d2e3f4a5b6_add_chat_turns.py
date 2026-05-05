"""add chat turns

Revision ID: c1d2e3f4a5b6
Revises: b1c2d3e4f5a6
Create Date: 2026-05-05 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_turns",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("client_request_id", sa.String(length=128), nullable=True),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("parent_turn_id", sa.String(length=64), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("user_message_id", sa.String(length=64), nullable=True),
        sa.Column("assistant_message_id", sa.String(length=64), nullable=True),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["assistant_message_id"], ["chat_messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_message_id"], ["chat_messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "client_request_id", name="uq_chat_turns_session_client_request"),
    )
    op.create_index("ix_chat_turns_assistant_message_id", "chat_turns", ["assistant_message_id"])
    op.create_index("ix_chat_turns_created_at", "chat_turns", ["created_at"])
    op.create_index("ix_chat_turns_parent_turn_id", "chat_turns", ["parent_turn_id"])
    op.create_index("ix_chat_turns_request_fingerprint", "chat_turns", ["request_fingerprint"])
    op.create_index("ix_chat_turns_session_id", "chat_turns", ["session_id"])
    op.create_index("ix_chat_turns_status", "chat_turns", ["status"])
    op.create_index("ix_chat_turns_updated_at", "chat_turns", ["updated_at"])
    op.create_index("ix_chat_turns_user_message_id", "chat_turns", ["user_message_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_turns_user_message_id", table_name="chat_turns")
    op.drop_index("ix_chat_turns_updated_at", table_name="chat_turns")
    op.drop_index("ix_chat_turns_status", table_name="chat_turns")
    op.drop_index("ix_chat_turns_session_id", table_name="chat_turns")
    op.drop_index("ix_chat_turns_request_fingerprint", table_name="chat_turns")
    op.drop_index("ix_chat_turns_parent_turn_id", table_name="chat_turns")
    op.drop_index("ix_chat_turns_created_at", table_name="chat_turns")
    op.drop_index("ix_chat_turns_assistant_message_id", table_name="chat_turns")
    op.drop_table("chat_turns")
