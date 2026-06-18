from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    func,
    Index,
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
    phone_e164: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    city: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    loyalty_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    sex: Mapped[str | None] = mapped_column(String(16))
    price_sensitivity: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    occasion_affinity: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    style_vector: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    size_preferences: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    channel_preference: Mapped[str] = mapped_column(String(32), nullable=False)
    pii_token: Mapped[str] = mapped_column(String(128), nullable=False)


class CustomerAuthIdentity(Base):
    __tablename__ = "customer_auth_identities"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="clerk")
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True
    )

    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id", name="uq_customer_auth_identities_provider_user"),
    )


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
        Index("ix_products_store_availability_category", "store_id", "availability", "category"),
        Index("ix_products_store_brand", "store_id", "brand"),
    )


class CatalogProduct(Base):
    __tablename__ = "catalog_products"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seed_run_id: Mapped[str] = mapped_column(ForeignKey("synthetic_runs.id"), nullable=False, index=True)
    catalog_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    brand: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(
        String(32), default="published", server_default="published", nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )

    variants: Mapped[list["ProductVariant"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    media_assets: Mapped[list["ProductMediaAsset"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_catalog_products_category_brand", "category", "brand"),
    )


class ProductVariant(Base):
    __tablename__ = "product_variants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seed_run_id: Mapped[str] = mapped_column(ForeignKey("synthetic_runs.id"), nullable=False, index=True)
    catalog_product_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    variant_key: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    color: Mapped[str | None] = mapped_column(String(64))
    material: Mapped[str | None] = mapped_column(String(64))
    gender: Mapped[str | None] = mapped_column(String(32))
    season: Mapped[str | None] = mapped_column(String(32))
    price_min: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    price_max: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    link: Mapped[str | None] = mapped_column(String(500))
    image_link: Mapped[str | None] = mapped_column(String(500))
    image_set: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    product: Mapped[CatalogProduct] = relationship(back_populates="variants")
    inventory: Mapped[list["StoreInventory"]] = relationship(back_populates="variant", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("price_min >= 0", name="ck_product_variants_price_min_non_negative"),
        CheckConstraint("price_max >= price_min", name="ck_product_variants_price_range_valid"),
        Index("ix_product_variants_product_price", "catalog_product_id", "price_min", "price_max"),
    )


class ProductMediaAsset(Base):
    __tablename__ = "product_media_assets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    catalog_product_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    intent: Mapped[str] = mapped_column(String(32), nullable=False)
    source_media_id: Mapped[str | None] = mapped_column(String(64))
    image_set: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)

    product: Mapped[CatalogProduct] = relationship(back_populates="media_assets")

    __table_args__ = (
        UniqueConstraint(
            "catalog_product_id",
            "display_order",
            name="uq_product_media_assets_product_display_order",
        ),
        Index("ix_product_media_assets_product_role", "catalog_product_id", "role"),
    )


class StoreInventory(Base):
    __tablename__ = "store_inventory"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seed_run_id: Mapped[str] = mapped_column(ForeignKey("synthetic_runs.id"), nullable=False, index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False, index=True)
    variant_id: Mapped[str] = mapped_column(
        ForeignKey("product_variants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    size: Mapped[str] = mapped_column(String(64), nullable=False, default="One Size")
    availability: Mapped[str] = mapped_column(String(32), nullable=False)
    inventory_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    objective_weight: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    variant: Mapped[ProductVariant] = relationship(back_populates="inventory")

    __table_args__ = (
        CheckConstraint("inventory_qty >= 0", name="ck_store_inventory_qty_non_negative"),
        UniqueConstraint("store_id", "variant_id", "size", name="uq_store_inventory_store_variant_size"),
        Index("ix_store_inventory_store_availability", "store_id", "availability"),
        Index("ix_store_inventory_variant_size", "variant_id", "size"),
    )


class CatalogDraftRevision(Base):
    __tablename__ = "catalog_draft_revisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    catalog_product_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    base_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False, index=True)
    moderation_state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_catalog_draft_product_created", "catalog_product_id", "created_at"),
    )


class CatalogAdminMutation(Base):
    __tablename__ = "catalog_admin_mutations"

    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class CatalogWorkflow(Base):
    __tablename__ = "catalog_workflows"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="clerk")
    owner_provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    business_summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="started", index=True)
    current_stage: Mapped[str] = mapped_column(String(64), nullable=False, default="workflow")
    next_event_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    draft_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("catalog_draft_revisions.id", ondelete="SET NULL"), index=True
    )
    image_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("image_generation_jobs.id", ondelete="SET NULL"), index=True
    )
    published_product_id: Mapped[str | None] = mapped_column(
        ForeignKey("catalog_products.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (
        CheckConstraint("next_event_sequence > 0", name="ck_catalog_workflows_next_sequence_positive"),
        UniqueConstraint(
            "owner_provider",
            "owner_provider_user_id",
            "idempotency_key_hash",
            name="uq_catalog_workflows_owner_idempotency",
        ),
        Index("ix_catalog_workflows_owner_created", "owner_provider_user_id", "created_at"),
    )


class CatalogWorkflowEvent(Base):
    __tablename__ = "catalog_workflow_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    capability: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    business_summary: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    request_id: Mapped[str | None] = mapped_column(String(128), index=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    usage_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    moderation_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    request_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    response_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128))
    retryable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payload_expired: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    __table_args__ = (
        CheckConstraint("sequence > 0", name="ck_catalog_workflow_events_sequence_positive"),
        UniqueConstraint("workflow_id", "sequence", name="uq_catalog_workflow_events_workflow_sequence"),
        UniqueConstraint(
            "workflow_id",
            "client_event_id",
            name="uq_catalog_workflow_events_workflow_client_event",
        ),
        Index("ix_catalog_workflow_events_workflow_created", "workflow_id", "created_at"),
    )


class SupplierProductOffer(Base):
    __tablename__ = "supplier_product_offers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seed_run_id: Mapped[str] = mapped_column(ForeignKey("synthetic_runs.id"), nullable=False, index=True)
    brand: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    size: Mapped[str | None] = mapped_column(String(64))
    season: Mapped[str | None] = mapped_column(String(32))
    available_on: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    link: Mapped[str | None] = mapped_column(String(500))
    image_link: Mapped[str | None] = mapped_column(String(500))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_supplier_product_offers_brand_category", "brand", "category"),
        Index("ix_supplier_product_offers_available_on", "available_on"),
        Index("ix_supplier_product_offers_status", "status"),
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
        Index("ix_orders_store_ordered_at", "store_id", "ordered_at"),
        Index("ix_orders_customer_ordered_at", "customer_id", "ordered_at"),
        Index("ix_orders_store_occasion_ordered_at", "store_id", "occasion", "ordered_at"),
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


class CatalogProductEmbedding(Base):
    __tablename__ = "catalog_product_embeddings"

    product_id: Mapped[str] = mapped_column(ForeignKey("catalog_products.id", ondelete="CASCADE"), primary_key=True)
    seed_run_id: Mapped[str] = mapped_column(ForeignKey("synthetic_runs.id"), nullable=False, index=True)
    namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    vector_id: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="indexed")
    error: Mapped[str | None] = mapped_column(Text)
    embedded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    __table_args__ = (
        Index("ix_catalog_product_embeddings_namespace", "namespace"),
        Index("ix_catalog_product_embeddings_status", "status"),
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


class CustomerCommunication(Base):
    __tablename__ = "customer_communications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False, index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="sms")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    destination_e164: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(255))
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    product_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    recommendation_context: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    twilio_message_sid: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_customer_communications_customer_created_at", "customer_id", "created_at"),
    )


class UiSession(Base):
    __tablename__ = "ui_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    state_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.id"), index=True)
    provider: Mapped[str | None] = mapped_column(String(32))
    provider_user_id: Mapped[str | None] = mapped_column(String(255), index=True)
    context_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True
    )


class ChatTurn(Base):
    __tablename__ = "chat_turns"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    client_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False, default="user_submit")
    parent_turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    user_message_id: Mapped[str | None] = mapped_column(ForeignKey("chat_messages.id", ondelete="SET NULL"), index=True)
    assistant_message_id: Mapped[str | None] = mapped_column(ForeignKey("chat_messages.id", ondelete="SET NULL"), index=True)
    response_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    __table_args__ = (
        UniqueConstraint("session_id", "client_request_id", name="uq_chat_turns_session_client_request"),
    )


class ChatToolCall(Base):
    __tablename__ = "chat_tool_calls"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    message_id: Mapped[str | None] = mapped_column(ForeignKey("chat_messages.id", ondelete="SET NULL"), index=True)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    input_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    output_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True
    )


class TwilioSmokeTest(Base):
    __tablename__ = "twilio_smoke_tests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    destination_e164: Mapped[str] = mapped_column(String(32), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    twilio_message_sid: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IndexJob(Base):
    __tablename__ = "index_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("synthetic_runs.id"), nullable=False, index=True)
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=128)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    attempted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    indexed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status_breakdown: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ImageGenerationJob(Base):
    __tablename__ = "image_generation_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("synthetic_runs.id"), index=True)
    store_id: Mapped[str | None] = mapped_column(ForeignKey("stores.id"), index=True)
    product_id: Mapped[str | None] = mapped_column(ForeignKey("catalog_products.id"), index=True)
    variant_id: Mapped[str | None] = mapped_column(ForeignKey("product_variants.id"), index=True)
    workflow_id: Mapped[str | None] = mapped_column(
        ForeignKey("catalog_workflows.id", ondelete="SET NULL"), index=True
    )
    draft_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("catalog_draft_revisions.id", ondelete="SET NULL"), index=True
    )
    expected_draft_version: Mapped[int | None] = mapped_column(Integer)
    requested_action: Mapped[str | None] = mapped_column(String(32))
    requested_variant_index: Mapped[int | None] = mapped_column(Integer)
    image_variant_set_id: Mapped[str | None] = mapped_column(String(64), index=True)
    source_media_id: Mapped[str | None] = mapped_column(String(64), index=True)
    target_media_id: Mapped[str | None] = mapped_column(String(64), index=True)
    requested_intent: Mapped[str | None] = mapped_column(String(32))
    idempotency_key_hash: Mapped[str | None] = mapped_column(String(64))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    refinement_prompt: Mapped[str | None] = mapped_column(Text)
    source_image_path: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(128), index=True)
    brand: Mapped[str | None] = mapped_column(String(128), index=True)
    limit: Mapped[int] = mapped_column("requested_limit", Integer, nullable=False, default=20)
    detail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    thumbnail_size: Mapped[int] = mapped_column(Integer, nullable=False, default=320)
    overwrite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    missing_images_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    size: Mapped[str] = mapped_column(String(32), nullable=False)
    quality: Mapped[str] = mapped_column(String(32), nullable=False)
    output_format: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    attempted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    generated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status_breakdown: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    result_sample: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_image_generation_jobs_status_created", "status", "created_at"),
        Index("ix_image_generation_jobs_category_brand", "category", "brand"),
        Index("ix_image_generation_jobs_status_heartbeat", "status", "last_heartbeat_at"),
        UniqueConstraint(
            "workflow_id",
            "idempotency_key_hash",
            name="uq_image_generation_jobs_workflow_idempotency",
        ),
    )


class ExecutiveCampaignDraft(Base):
    __tablename__ = "executive_campaign_drafts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    to_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExecutiveStrategyPacket(Base):
    __tablename__ = "executive_strategy_packets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="published", index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    email_status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    to_email: Mapped[str | None] = mapped_column(String(255), index=True)
    email_subject: Mapped[str | None] = mapped_column(String(255))
    email_body_text: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    email_error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MerchStrategyStoreOverride(Base):
    __tablename__ = "merch_strategy_store_overrides"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    packet_id: Mapped[str] = mapped_column(
        ForeignKey("executive_strategy_packets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    store_id: Mapped[str] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("packet_id", "store_id", name="uq_merch_strategy_override_packet_store"),
    )
