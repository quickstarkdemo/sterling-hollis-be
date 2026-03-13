from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class SyntheticRun(Base):
    __tablename__ = "synthetic_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="generated", nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seed_run_id: Mapped[str] = mapped_column(ForeignKey("synthetic_runs.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    city: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    postal_code: Mapped[str] = mapped_column(String(16), nullable=False)
    address_line1: Mapped[str] = mapped_column(String(255), nullable=False)
    address_line2: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32))
    latitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    profile_type: Mapped[str] = mapped_column(String(64), nullable=False)
    services: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    raw_source: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seed_run_id: Mapped[str] = mapped_column(ForeignKey("synthetic_runs.id"), nullable=False, index=True)
    home_store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False, index=True)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    city: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    loyalty_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    price_sensitivity: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    occasion_affinity: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    style_vector: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    size_preferences: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    channel_preference: Mapped[str] = mapped_column(String(32), nullable=False)
    pii_token: Mapped[str] = mapped_column(String(128), nullable=False)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seed_run_id: Mapped[str] = mapped_column(ForeignKey("synthetic_runs.id"), nullable=False, index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    link: Mapped[str] = mapped_column(String(500), nullable=False)
    image_link: Mapped[str] = mapped_column(String(500), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    availability: Mapped[str] = mapped_column(String(32), nullable=False)
    brand: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False)

    color: Mapped[str | None] = mapped_column(String(64))
    size: Mapped[str | None] = mapped_column(String(64))
    material: Mapped[str | None] = mapped_column(String(64))
    gender: Mapped[str | None] = mapped_column(String(32))
    season: Mapped[str | None] = mapped_column(String(32))

    margin_pct: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    inventory_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    objective_weight: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_products_price_non_negative"),
        CheckConstraint("inventory_qty >= 0", name="ck_products_inventory_non_negative"),
    )


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seed_run_id: Mapped[str] = mapped_column(ForeignKey("synthetic_runs.id"), nullable=False, index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False, index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False, index=True)

    ordered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    occasion: Mapped[str | None] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    returned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    items: Mapped[list[OrderItem]] = relationship(back_populates="order", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("subtotal >= 0", name="ck_orders_subtotal_non_negative"),
        CheckConstraint("total_amount >= 0", name="ck_orders_total_non_negative"),
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False, index=True)

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_order_items_price_non_negative"),
    )


class ProductEmbedding(Base):
    __tablename__ = "product_embeddings"

    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)
    seed_run_id: Mapped[str] = mapped_column(ForeignKey("synthetic_runs.id"), nullable=False, index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False, index=True)
    namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    vector_id: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="indexed")
    error: Mapped[str | None] = mapped_column(Text)
    embedded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class SyntheticValidationFailure(Base):
    __tablename__ = "synthetic_validation_failures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    seed_run_id: Mapped[str] = mapped_column(ForeignKey("synthetic_runs.id"), nullable=False, index=True)
    check_name: Mapped[str] = mapped_column(String(128), nullable=False)
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class StoreDailyMetric(Base):
    __tablename__ = "store_daily_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    seed_run_id: Mapped[str] = mapped_column(ForeignKey("synthetic_runs.id"), nullable=False, index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False, index=True)
    metric_date: Mapped[Date] = mapped_column(Date, nullable=False)
    revenue: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    units_sold: Mapped[int] = mapped_column(Integer, nullable=False)
    sell_through: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    aov: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    margin_rate: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)

    __table_args__ = (
        UniqueConstraint("seed_run_id", "store_id", "metric_date", name="uq_store_daily_metric_run_store_date"),
    )
