"""add customer phone ui sessions and twilio smoke tests

Revision ID: c3d4e5f6a7b8
Revises: 8f7d5d1d3e42
Create Date: 2026-03-14 10:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = '8f7d5d1d3e42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('customers', sa.Column('phone_e164', sa.String(length=32), nullable=True))

    op.execute(
        """
        WITH ranked AS (
            SELECT id, '+1' || LPAD((2000000000 + ROW_NUMBER() OVER (ORDER BY id))::text, 10, '0') AS generated_phone
            FROM customers
        )
        UPDATE customers c
        SET phone_e164 = ranked.generated_phone
        FROM ranked
        WHERE c.id = ranked.id
        """
    )

    op.alter_column('customers', 'phone_e164', nullable=False)
    op.create_index(op.f('ix_customers_phone_e164'), 'customers', ['phone_e164'], unique=True)

    op.create_table(
        'ui_sessions',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('kind', sa.String(length=64), nullable=False),
        sa.Column('state_json', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_ui_sessions_kind'), 'ui_sessions', ['kind'], unique=False)
    op.create_index(op.f('ix_ui_sessions_created_at'), 'ui_sessions', ['created_at'], unique=False)
    op.create_index(op.f('ix_ui_sessions_expires_at'), 'ui_sessions', ['expires_at'], unique=False)

    op.create_table(
        'twilio_smoke_tests',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('destination_e164', sa.String(length=32), nullable=False),
        sa.Column('body_text', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('twilio_message_sid', sa.String(length=128), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_twilio_smoke_tests_created_at'), 'twilio_smoke_tests', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_twilio_smoke_tests_created_at'), table_name='twilio_smoke_tests')
    op.drop_table('twilio_smoke_tests')

    op.drop_index(op.f('ix_ui_sessions_expires_at'), table_name='ui_sessions')
    op.drop_index(op.f('ix_ui_sessions_created_at'), table_name='ui_sessions')
    op.drop_index(op.f('ix_ui_sessions_kind'), table_name='ui_sessions')
    op.drop_table('ui_sessions')

    op.drop_index(op.f('ix_customers_phone_e164'), table_name='customers')
    op.drop_column('customers', 'phone_e164')
