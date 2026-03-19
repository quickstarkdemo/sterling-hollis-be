"""add executive strategy packets

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-03-19 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "executive_strategy_packets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("email_status", sa.String(length=32), nullable=False),
        sa.Column("to_email", sa.String(length=255), nullable=True),
        sa.Column("email_subject", sa.String(length=255), nullable=True),
        sa.Column("email_body_text", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.String(length=128), nullable=True),
        sa.Column("email_error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_executive_strategy_packets_status"),
        "executive_strategy_packets",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_executive_strategy_packets_email_status"),
        "executive_strategy_packets",
        ["email_status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_executive_strategy_packets_to_email"),
        "executive_strategy_packets",
        ["to_email"],
        unique=False,
    )
    op.create_index(
        op.f("ix_executive_strategy_packets_created_at"),
        "executive_strategy_packets",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_executive_strategy_packets_created_at"), table_name="executive_strategy_packets")
    op.drop_index(op.f("ix_executive_strategy_packets_to_email"), table_name="executive_strategy_packets")
    op.drop_index(op.f("ix_executive_strategy_packets_email_status"), table_name="executive_strategy_packets")
    op.drop_index(op.f("ix_executive_strategy_packets_status"), table_name="executive_strategy_packets")
    op.drop_table("executive_strategy_packets")
