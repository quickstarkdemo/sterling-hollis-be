from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
from pathlib import Path

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app.catalog.service import product_to_catalog
from app.database import Base
from app.models import (
    CatalogProduct,
    Product,
    ProductInventory,
    ProductVariant,
    Store,
    StoreInventory,
    SyntheticRun,
)
from app.services.catalog_normalization import (
    backfill_catalog_from_legacy_products,
    backfill_product_authoring_v2,
)


def _legacy_product(product_id: str, **overrides) -> Product:
    values = {
        "id": product_id,
        "seed_run_id": "run_v2",
        "store_id": "1001",
        "title": "Travel Blazer",
        "description": "A structured travel blazer.",
        "link": "https://example.com/travel-blazer",
        "image_link": "https://example.com/travel-blazer.jpg",
        "price": Decimal("495.00"),
        "availability": "out of stock",
        "brand": "Sterling Hollis",
        "category": "mens_apparel",
        "color": "Navy",
        "size": "M",
        "material": "wool",
        "gender": "men",
        "season": "fall",
        "margin_pct": Decimal("0.5000"),
        "inventory_qty": 0,
        "objective_weight": Decimal("0.7000"),
        "metadata_json": {"style_code": "travel-blazer"},
    }
    values.update(overrides)
    return Product(**values)


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.info["test_engine"] = engine
    session.add(
        SyntheticRun(
            id="run_v2",
            seed=2026,
            status="loaded",
            started_at=datetime(2026, 6, 18, tzinfo=timezone.utc),
            config={},
        )
    )
    session.add(
        Store(
            id="1001",
            seed_run_id="run_v2",
            name="Dallas Downtown",
            city="Dallas",
            state="TX",
            postal_code="75201",
            address_line1="1 Main St",
            profile_type="texas_core",
            services=[],
            raw_source={},
        )
    )
    session.commit()
    return session


def test_legacy_multi_variant_backfill_characterizes_public_price_and_inventory() -> None:
    db = _session()
    try:
        db.add_all(
            [
                _legacy_product(
                    "prod_navy_m", availability="in stock", inventory_qty=3
                ),
                _legacy_product(
                    "prod_navy_m_restock",
                    price=Decimal("525.00"),
                    availability="preorder",
                    inventory_qty=2,
                ),
                _legacy_product(
                    "prod_ivory_s",
                    color="Ivory",
                    size="S",
                    price=Decimal("550.00"),
                    inventory_qty=4,
                ),
            ]
        )
        db.commit()

        backfill_catalog_from_legacy_products(db, run_id="run_v2")

        product = db.scalar(select(CatalogProduct))
        assert product is not None
        variants = db.scalars(
            select(ProductVariant)
            .where(ProductVariant.catalog_product_id == product.id)
            .order_by(ProductVariant.color)
        ).all()
        inventory = db.scalars(select(StoreInventory).order_by(StoreInventory.size)).all()
        product_inventory = db.scalars(
            select(ProductInventory).order_by(ProductInventory.size_key)
        ).all()
        public = product_to_catalog(db, product, include_variants=True)

        assert [(row.color, row.price_min, row.price_max) for row in variants] == [
            ("Ivory", Decimal("550.00"), Decimal("550.00")),
            ("Navy", Decimal("495.00"), Decimal("525.00")),
        ]
        assert [(row.size, row.inventory_qty, row.availability) for row in inventory] == [
            ("M", 5, "in stock"),
            ("S", 4, "out of stock"),
        ]
        assert public.price_min == 495.0
        assert public.price_max == 550.0
        assert len(public.variants) == 2
        assert sum(row.inventory_qty for variant in public.variants for row in variant.inventory) == 9
        assert product.price_min == Decimal("495.00")
        assert product.price_max == Decimal("550.00")
        assert product.color == "Ivory"
        assert [(row.size, row.size_key, row.inventory_qty) for row in product_inventory] == [
            ("M", "m", 5),
            ("S", "s", 4),
        ]
        assert sum(row.inventory_qty for row in product_inventory) == 9
    finally:
        engine = db.info["test_engine"]
        db.close()
        engine.dispose()


def test_legacy_blank_sizes_collapse_to_one_inventory_row_with_priority() -> None:
    db = _session()
    try:
        db.add_all(
            [
                _legacy_product("prod_blank", size="", inventory_qty=0),
                _legacy_product(
                    "prod_none",
                    size=None,
                    availability="preorder",
                    inventory_qty=2,
                ),
            ]
        )
        db.commit()

        backfill_catalog_from_legacy_products(db, run_id="run_v2")

        inventory = db.scalars(select(StoreInventory)).all()
        assert len(inventory) == 1
        assert inventory[0].size == "One Size"
        assert inventory[0].inventory_qty == 2
        assert inventory[0].availability == "preorder"
        product_inventory = db.scalars(select(ProductInventory)).all()
        assert len(product_inventory) == 1
        assert product_inventory[0].size is None
        assert product_inventory[0].size_key == ""
        assert product_inventory[0].inventory_qty == 2
        assert product_inventory[0].availability == "preorder"

        before = backfill_product_authoring_v2(db, run_id="run_v2", dry_run=True)
        applied = backfill_product_authoring_v2(db, run_id="run_v2", dry_run=False)
        rerun = backfill_product_authoring_v2(db, run_id="run_v2", dry_run=False)
        assert before == applied == rerun
        assert before.source_inventory_qty == before.translated_inventory_qty == 2
        assert before.conflicting_inventory_groups == 1
        assert db.scalar(select(ProductInventory.inventory_qty)) == 2
    finally:
        engine = db.info["test_engine"]
        db.close()
        engine.dispose()


def test_product_authoring_v2_migration_backfills_and_preserves_legacy_rows(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'catalog-v2.db'}", future=True)
    with engine.begin() as connection:
        connection.execute(text("create table synthetic_runs (id varchar(64) primary key)"))
        connection.execute(text("create table stores (id varchar(64) primary key)"))
        connection.execute(
            text(
                "create table catalog_products ("
                "id varchar(64) primary key, seed_run_id varchar(64) not null, "
                "metadata_json json not null)"
            )
        )
        connection.execute(
            text(
                "create table product_variants ("
                "id varchar(64) primary key, catalog_product_id varchar(64) not null, "
                "price_min numeric not null, price_max numeric not null, link varchar(500), "
                "color varchar(64), material varchar(64), gender varchar(32), season varchar(32))"
            )
        )
        connection.execute(
            text(
                "create table store_inventory ("
                "id varchar(64) primary key, seed_run_id varchar(64) not null, "
                "store_id varchar(64) not null, variant_id varchar(64) not null, "
                "size varchar(64) not null, availability varchar(32) not null, "
                "inventory_qty integer not null)"
            )
        )
        connection.execute(text("insert into synthetic_runs values ('run_v2')"))
        connection.execute(text("insert into stores values ('1001')"))
        connection.execute(
            text(
                "insert into catalog_products values "
                "('cat_1', 'run_v2', '{\"_catalog_studio_authoring\": "
                "{\"primary_variant_id\": \"var_navy\"}}')"
            )
        )
        connection.execute(
            text(
                "insert into product_variants values "
                "('var_ivory', 'cat_1', 550, 550, 'https://example.com/ivory', "
                "'Ivory', 'wool', 'men', 'fall'), "
                "('var_navy', 'cat_1', 495, 525, 'https://example.com/navy', "
                "'Navy', 'wool', 'men', 'fall')"
            )
        )
        connection.execute(
            text(
                "insert into store_inventory values "
                "('inv_1', 'run_v2', '1001', 'var_ivory', 'M', 'out of stock', 0), "
                "('inv_2', 'run_v2', '1001', 'var_navy', 'M', 'in stock', 5)"
            )
        )

    migration_path = (
        Path(__file__).parents[1]
        / "alembic/versions/d8e9f0a1b2c3_add_product_authoring_v2.py"
    )
    spec = importlib.util.spec_from_file_location("product_authoring_v2_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        product = connection.execute(
            text(
                "select price_min, price_max, link, color from catalog_products "
                "where id = 'cat_1'"
            )
        ).one()
        canonical_inventory = connection.execute(
            text(
                "select size, size_key, availability, inventory_qty "
                "from product_inventory"
            )
        ).one()

    assert tuple(product) == (495, 550, "https://example.com/navy", "Navy")
    assert tuple(canonical_inventory) == ("M", "m", "in stock", 5)
    assert "product_inventory" in inspect(engine).get_table_names()

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()
        assert connection.execute(text("select count(*) from product_variants")).scalar_one() == 2
        assert connection.execute(text("select count(*) from store_inventory")).scalar_one() == 2

    inspector = inspect(engine)
    assert "product_inventory" not in inspector.get_table_names()
    assert "price_min" not in {
        column["name"] for column in inspector.get_columns("catalog_products")
    }
    engine.dispose()
