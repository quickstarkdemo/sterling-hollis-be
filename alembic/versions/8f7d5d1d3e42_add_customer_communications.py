"""add customer communications

Revision ID: 8f7d5d1d3e42
Revises: f790a40c397b
Create Date: 2026-03-13 16:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8f7d5d1d3e42'
down_revision: Union[str, Sequence[str], None] = 'f790a40c397b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'customer_communications',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('customer_id', sa.String(length=64), nullable=False),
        sa.Column('store_id', sa.String(length=64), nullable=False),
        sa.Column('channel', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('destination_e164', sa.String(length=32), nullable=False),
        sa.Column('body_text', sa.Text(), nullable=False),
        sa.Column('product_ids', sa.JSON(), nullable=False),
        sa.Column('recommendation_context', sa.JSON(), nullable=False),
        sa.Column('twilio_message_sid', sa.String(length=128), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id']),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_customer_communications_customer_id'), 'customer_communications', ['customer_id'], unique=False)
    op.create_index(op.f('ix_customer_communications_store_id'), 'customer_communications', ['store_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_customer_communications_store_id'), table_name='customer_communications')
    op.drop_index(op.f('ix_customer_communications_customer_id'), table_name='customer_communications')
    op.drop_table('customer_communications')
