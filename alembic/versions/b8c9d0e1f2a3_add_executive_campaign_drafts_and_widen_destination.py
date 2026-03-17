"""add executive campaign drafts and widen destination email column

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-03-17 15:05:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("customer_communications", "destination_e164", type_=sa.String(length=255), nullable=False)

    op.create_table(
        "executive_campaign_drafts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("to_email", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("provider_message_id", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_executive_campaign_drafts_status"), "executive_campaign_drafts", ["status"], unique=False)
    op.create_index(op.f("ix_executive_campaign_drafts_to_email"), "executive_campaign_drafts", ["to_email"], unique=False)
    op.create_index(
        op.f("ix_executive_campaign_drafts_created_at"), "executive_campaign_drafts", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_executive_campaign_drafts_created_at"), table_name="executive_campaign_drafts")
    op.drop_index(op.f("ix_executive_campaign_drafts_to_email"), table_name="executive_campaign_drafts")
    op.drop_index(op.f("ix_executive_campaign_drafts_status"), table_name="executive_campaign_drafts")
    op.drop_table("executive_campaign_drafts")
    op.alter_column("customer_communications", "destination_e164", type_=sa.String(length=32), nullable=False)
