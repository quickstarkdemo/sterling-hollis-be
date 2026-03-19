"""add merch strategy store overrides

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-03-19 18:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d0e1f2a3b4c5"
down_revision: Union[str, Sequence[str], None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "merch_strategy_store_overrides",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("packet_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["packet_id"], ["executive_strategy_packets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("packet_id", "store_id", name="uq_merch_strategy_override_packet_store"),
    )
    op.create_index(
        op.f("ix_merch_strategy_store_overrides_packet_id"),
        "merch_strategy_store_overrides",
        ["packet_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_merch_strategy_store_overrides_store_id"),
        "merch_strategy_store_overrides",
        ["store_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_merch_strategy_store_overrides_status"),
        "merch_strategy_store_overrides",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_merch_strategy_store_overrides_created_at"),
        "merch_strategy_store_overrides",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_merch_strategy_store_overrides_created_at"), table_name="merch_strategy_store_overrides")
    op.drop_index(op.f("ix_merch_strategy_store_overrides_status"), table_name="merch_strategy_store_overrides")
    op.drop_index(op.f("ix_merch_strategy_store_overrides_store_id"), table_name="merch_strategy_store_overrides")
    op.drop_index(op.f("ix_merch_strategy_store_overrides_packet_id"), table_name="merch_strategy_store_overrides")
    op.drop_table("merch_strategy_store_overrides")
