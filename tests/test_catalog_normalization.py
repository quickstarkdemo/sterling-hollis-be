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


def _product(product_id: str, store_id: str, size: str, inventory_qty: int) -> Product:
    return Product(
        id=product_id,
        seed_run_id="run_norm",
        store_id=store_id,
        title="Valentino Rose Silk Dress",
        description="Event-ready silk dress with soft rose color",
        link=f"https://fashion.example/products/{product_id}",
        image_link=f"https://fashion.example/images/{product_id}.jpg",
        price=Decimal("750.00"),
        availability="in stock" if inventory_qty else "out of stock",
        brand="Valentino",
        category="womens_apparel",
        color="Rose",
        size=size,
        material="silk",
        gender="women",
        season="spring",
        margin_pct=Decimal("0.6200"),
        inventory_qty=inventory_qty,
        objective_weight=Decimal("0.9000"),
        metadata_json={},
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
