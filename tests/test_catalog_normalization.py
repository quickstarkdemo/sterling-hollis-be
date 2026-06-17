from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import CatalogProduct, Product, ProductVariant, Store, StoreInventory, SyntheticRun
from app.services.catalog_normalization import backfill_catalog_from_legacy_products


def _session():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    session = TestingSessionLocal()
    now = datetime(2026, 3, 14, tzinfo=timezone.utc)
    session.add(SyntheticRun(id="run_norm", seed=1, status="loaded", started_at=now, config={}))
    for store_id in ["1001", "1002"]:
        session.add(
            Store(
                id=store_id,
                seed_run_id="run_norm",
                name=f"Store {store_id}",
                city="Dallas",
                state="TX",
                postal_code="75201",
                address_line1="1 Main St",
                address_line2=None,
                phone=None,
                latitude=Decimal("32.770000"),
                longitude=Decimal("-96.790000"),
                profile_type="texas_core",
                services=[],
                raw_source={},
            )
        )
    return engine, session


def _product(
    product_id: str,
    store_id: str,
    size: str,
    inventory_qty: int,
    *,
    title: str = "Valentino Rose Silk Dress",
    color: str = "Rose",
    style_code: str | None = None,
) -> Product:
    metadata = {"style_code": style_code} if style_code else {}
    return Product(
        id=product_id,
        seed_run_id="run_norm",
        store_id=store_id,
        title=title,
        description="Event-ready silk dress with soft rose color",
        link=f"https://fashion.example/products/{product_id}",
        image_link=f"https://fashion.example/images/{product_id}.jpg",
        price=Decimal("750.00"),
        availability="in stock" if inventory_qty else "out of stock",
        brand="Valentino",
        category="womens_apparel",
        color=color,
        size=size,
        material="silk",
        gender="women",
        season="spring",
        margin_pct=Decimal("0.6200"),
        inventory_qty=inventory_qty,
        objective_weight=Decimal("0.9000"),
        metadata_json=metadata,
    )


def test_catalog_backfill_collapses_store_rows_and_is_idempotent():
    engine, session = _session()
    session.add_all(
        [
            _product("prod_1", "1001", "M", 12),
            _product("prod_2", "1002", "M", 8),
            _product("prod_3", "1001", "L", 0),
        ]
    )
    session.commit()

    try:
        first = backfill_catalog_from_legacy_products(session, run_id="run_norm")
        second = backfill_catalog_from_legacy_products(session, run_id="run_norm")
        catalog_count = session.scalar(select(func.count()).select_from(CatalogProduct))
        variant_count = session.scalar(select(func.count()).select_from(ProductVariant))
        inventory_rows = session.scalars(select(StoreInventory).order_by(StoreInventory.store_id, StoreInventory.size)).all()
    finally:
        session.close()
        engine.dispose()

    assert first.legacy_products == 3
    assert first.catalog_products == 1
    assert first.product_variants == 1
    assert first.store_inventory == 3
    assert second == first
    assert catalog_count == 1
    assert variant_count == 1
    assert [(row.store_id, row.size, row.inventory_qty) for row in inventory_rows] == [
        ("1001", "L", 0),
        ("1001", "M", 12),
        ("1002", "M", 8),
    ]


def test_catalog_backfill_groups_colors_by_source_style_code():
    engine, session = _session()
    session.add_all(
        [
            _product(
                "prod_rose",
                "1001",
                "M",
                12,
                title="Valentino Rose Silk Dress",
                color="Rose",
                style_code="style_000001",
            ),
            _product(
                "prod_navy",
                "1002",
                "L",
                8,
                title="Valentino Navy Silk Dress",
                color="Navy",
                style_code="style_000001",
            ),
        ]
    )
    session.commit()

    try:
        stats = backfill_catalog_from_legacy_products(session, run_id="run_norm")
        products = session.scalars(select(CatalogProduct)).all()
        variants = session.scalars(select(ProductVariant).order_by(ProductVariant.color)).all()
    finally:
        session.close()
        engine.dispose()

    assert stats.catalog_products == 1
    assert stats.product_variants == 2
    assert len(products) == 1
    assert [variant.color for variant in variants] == ["Navy", "Rose"]


def test_catalog_backfill_does_not_merge_distinct_source_styles_with_same_display_fields():
    engine, session = _session()
    session.add_all(
        [
            _product(
                "prod_style_a",
                "1001",
                "M",
                12,
                title="Valentino Silk Dress",
                style_code="style_000001",
            ),
            _product(
                "prod_style_b",
                "1002",
                "M",
                8,
                title="Valentino Silk Dress",
                style_code="style_000002",
            ),
        ]
    )
    session.commit()

    try:
        stats = backfill_catalog_from_legacy_products(session, run_id="run_norm")
        catalog_keys = session.scalars(select(CatalogProduct.catalog_key).order_by(CatalogProduct.catalog_key)).all()
    finally:
        session.close()
        engine.dispose()

    assert stats.catalog_products == 2
    assert len(catalog_keys) == 2
    assert all("style_00000" in key for key in catalog_keys)
