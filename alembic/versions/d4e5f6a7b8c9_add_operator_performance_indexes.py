"""add operator performance indexes

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-03-14 16:45:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_orders_store_ordered_at", "orders", ["store_id", "ordered_at"], unique=False)
    op.create_index("ix_orders_customer_ordered_at", "orders", ["customer_id", "ordered_at"], unique=False)
    op.create_index("ix_orders_store_occasion_ordered_at", "orders", ["store_id", "occasion", "ordered_at"], unique=False)
    op.create_index("ix_products_store_availability_category", "products", ["store_id", "availability", "category"], unique=False)
    op.create_index("ix_products_store_brand", "products", ["store_id", "brand"], unique=False)
    op.create_index(
        "ix_customer_communications_customer_created_at",
        "customer_communications",
        ["customer_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_customer_communications_customer_created_at", table_name="customer_communications")
    op.drop_index("ix_products_store_brand", table_name="products")
    op.drop_index("ix_products_store_availability_category", table_name="products")
    op.drop_index("ix_orders_store_occasion_ordered_at", table_name="orders")
    op.drop_index("ix_orders_customer_ordered_at", table_name="orders")
    op.drop_index("ix_orders_store_ordered_at", table_name="orders")
