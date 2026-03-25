"""add supplier product offers

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-03-25 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "d0e1f2a3b4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "supplier_product_offers",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("seed_run_id", sa.String(length=64), nullable=False),
        sa.Column("brand", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=False),
        sa.Column("price", sa.Numeric(10, 2), nullable=True),
        sa.Column("size", sa.String(length=64), nullable=True),
        sa.Column("season", sa.String(length=32), nullable=True),
        sa.Column("available_on", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("link", sa.String(length=500), nullable=True),
        sa.Column("image_link", sa.String(length=500), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["seed_run_id"], ["synthetic_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_supplier_product_offers_seed_run_id", "supplier_product_offers", ["seed_run_id"])
    op.create_index(
        "ix_supplier_product_offers_brand_category",
        "supplier_product_offers",
        ["brand", "category"],
    )
    op.create_index("ix_supplier_product_offers_available_on", "supplier_product_offers", ["available_on"])
    op.create_index("ix_supplier_product_offers_status", "supplier_product_offers", ["status"])


def downgrade() -> None:
    op.drop_index("ix_supplier_product_offers_status", table_name="supplier_product_offers")
    op.drop_index("ix_supplier_product_offers_available_on", table_name="supplier_product_offers")
    op.drop_index("ix_supplier_product_offers_brand_category", table_name="supplier_product_offers")
    op.drop_index("ix_supplier_product_offers_seed_run_id", table_name="supplier_product_offers")
    op.drop_table("supplier_product_offers")
