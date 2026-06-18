from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings, get_settings
from app.database import Base, get_db
from app.main import create_app
from app.models import CatalogBrand, CatalogProduct, Product, Store, SyntheticRun
from app.services.auth.clerk import AuthenticatedPrincipal, require_clerk_principal
from app.services.catalog_normalization import backfill_catalog_from_legacy_products


@contextmanager
def _reference_client(monkeypatch):
    monkeypatch.setenv("ENABLE_MCP_ADAPTER", "false")
    monkeypatch.setenv("ENABLE_OPENAI_APPS_UI", "false")
    get_settings.cache_clear()
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        catalog_studio_clerk_authorized_subjects="user_admin",
        enable_mcp_adapter=False,
        enable_openai_apps_ui=False,
    )
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sessions = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    Base.metadata.create_all(engine)
    app = create_app(settings=settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[require_clerk_principal] = lambda: AuthenticatedPrincipal(
        provider="clerk",
        provider_user_id="user_admin",
        email="admin@example.com",
        claims={},
    )

    def override_db():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    db = sessions()
    try:
        now = datetime(2026, 6, 18, tzinfo=timezone.utc)
        db.add(
            SyntheticRun(
                id="run_references",
                seed=103,
                status="loaded",
                started_at=now,
                config={},
            )
        )
        db.add_all(
            [
                Store(
                    id="1002",
                    seed_run_id="run_references",
                    name="Oak Brook",
                    city="Oak Brook",
                    state="IL",
                    postal_code="60523",
                    address_line1="2 Commerce Dr",
                    profile_type="suburban_affluent",
                    services=[],
                    raw_source={"private": "must-not-leak"},
                ),
                Store(
                    id="1001",
                    seed_run_id="run_references",
                    name="Dallas Downtown",
                    city="Dallas",
                    state="TX",
                    postal_code="75201",
                    address_line1="1 Main St",
                    profile_type="texas_core",
                    services=[],
                    raw_source={"private": "must-not-leak"},
                ),
            ]
        )
        for product_id, title, brand in (
            ("prod_pillow", "Black Pillow", "August & Mercer"),
            ("prod_throw", "Ivory Throw", " august  &  mercer "),
        ):
            db.add(
                Product(
                    id=product_id,
                    seed_run_id="run_references",
                    store_id="1001",
                    title=title,
                    description="A reference-data fixture.",
                    link=f"https://example.com/{product_id}",
                    image_link=f"https://example.com/{product_id}.jpg",
                    price=Decimal("250.00"),
                    availability="in stock",
                    brand=brand,
                    category="home",
                    color="Black",
                    size="One Size",
                    material="linen",
                    gender="unisex",
                    season="all-season",
                    margin_pct=Decimal("0.5000"),
                    inventory_qty=4,
                    objective_weight=Decimal("0.8000"),
                    metadata_json={"style_code": product_id},
                )
            )
        db.commit()
        backfill_catalog_from_legacy_products(db, run_id="run_references")
        yield TestClient(app), sessions, app
    finally:
        db.close()
        app.dependency_overrides.clear()
        engine.dispose()
        get_settings.cache_clear()


def _v2_payload(*, brand_id: str, store_id: str = "1001") -> dict:
    return {
        "expected_version": 0,
        "moderation_state": "approved",
        "product": {
            "schema_version": 2,
            "seed_run_id": "run_references",
            "title": "Reference Coat",
            "description": "A product using canonical references.",
            "brand_id": brand_id,
            "brand": "August & Mercer",
            "category": "womens_apparel",
            "price_min": 250,
            "price_max": 300,
            "inventory": [
                {
                    "store_id": store_id,
                    "size": None,
                    "availability": "in stock",
                    "inventory_qty": 8,
                }
            ],
        },
    }


def test_reference_data_is_sorted_stable_and_safe(monkeypatch) -> None:
    with _reference_client(monkeypatch) as (client, sessions, _):
        response = client.get("/api/admin/catalog/v2/references")

        assert response.status_code == 200
        body = response.json()
        assert body["brands"] == sorted(body["brands"], key=lambda row: row["name"].casefold())
        assert body["brands"] == [
            {
                "id": body["brands"][0]["id"],
                "name": "August & Mercer",
            }
        ]
        assert body["stores"] == [
            {
                "id": "1001",
                "name": "Dallas Downtown",
                "city": "Dallas",
                "state": "TX",
                "label": "Dallas Downtown — Dallas, TX",
            },
            {
                "id": "1002",
                "name": "Oak Brook",
                "city": "Oak Brook",
                "state": "IL",
                "label": "Oak Brook — Oak Brook, IL",
            },
        ]
        assert "raw_source" not in response.text
        assert {row["id"] for row in body["categories"]} == {
            "beauty",
            "handbags",
            "home",
            "jewelry_accessories",
            "kids",
            "mens_apparel",
            "shoes",
            "womens_apparel",
        }
        assert body["categories"] == sorted(
            body["categories"], key=lambda row: (row["label"].casefold(), row["id"])
        )
        assert [row["id"] for row in body["availability"]] == [
            "in stock",
            "low stock",
            "preorder",
            "out of stock",
        ]
        with sessions() as db:
            brand_ids = db.scalars(select(CatalogProduct.brand_id)).all()
            assert len(set(brand_ids)) == 1
            assert None not in brand_ids


def test_add_brand_is_idempotent_and_normalized_unique(monkeypatch) -> None:
    with _reference_client(monkeypatch) as (client, sessions, _):
        created = client.post(
            "/api/admin/catalog/v2/brands",
            headers={"Idempotency-Key": "add-lune-ledger"},
            json={"name": "Lune & Ledger"},
        )
        replay = client.post(
            "/api/admin/catalog/v2/brands",
            headers={"Idempotency-Key": "add-lune-ledger"},
            json={"name": "Lune & Ledger"},
        )
        collision = client.post(
            "/api/admin/catalog/v2/brands",
            headers={"Idempotency-Key": "add-lune-ledger-again"},
            json={"name": "  lune   & ledger  "},
        )
        unicode_created = client.post(
            "/api/admin/catalog/v2/brands",
            headers={"Idempotency-Key": "add-strasse-atelier"},
            json={"name": "Straße Atelier"},
        )
        unicode_collision = client.post(
            "/api/admin/catalog/v2/brands",
            headers={"Idempotency-Key": "add-strasse-atelier-again"},
            json={"name": "STRASSE ATELIER"},
        )

        assert created.status_code == replay.status_code == 201
        assert created.json() == replay.json()
        assert collision.status_code == 409
        assert unicode_created.status_code == 201
        assert unicode_collision.status_code == 409
        with sessions() as db:
            count = db.scalar(
                select(func.count()).select_from(CatalogBrand).where(
                    CatalogBrand.id == created.json()["id"]
                )
            )
            assert count == 1


def test_v2_draft_requires_active_brand_pair_and_known_store(monkeypatch) -> None:
    with _reference_client(monkeypatch) as (client, sessions, _):
        brand = client.get("/api/admin/catalog/v2/references").json()["brands"][0]

        accepted = client.post(
            "/api/admin/catalog/v2/products/drafts",
            headers={"Idempotency-Key": "reference-draft-valid"},
            json=_v2_payload(brand_id=brand["id"]),
        )
        unknown_brand = client.post(
            "/api/admin/catalog/v2/products/drafts",
            headers={"Idempotency-Key": "reference-draft-brand"},
            json=_v2_payload(brand_id="brand_missing"),
        )
        mismatched_name_payload = _v2_payload(brand_id=brand["id"])
        mismatched_name_payload["product"]["brand"] = "Different Brand"
        mismatched_name = client.post(
            "/api/admin/catalog/v2/products/drafts",
            headers={"Idempotency-Key": "reference-draft-brand-name"},
            json=mismatched_name_payload,
        )
        unknown_store = client.post(
            "/api/admin/catalog/v2/products/drafts",
            headers={"Idempotency-Key": "reference-draft-store"},
            json=_v2_payload(brand_id=brand["id"], store_id="missing-store"),
        )
        unknown_category_payload = _v2_payload(brand_id=brand["id"])
        unknown_category_payload["product"]["category"] = "technical_free_text"
        unknown_category = client.post(
            "/api/admin/catalog/v2/products/drafts",
            headers={"Idempotency-Key": "reference-draft-category"},
            json=unknown_category_payload,
        )
        unknown_availability_payload = _v2_payload(brand_id=brand["id"])
        unknown_availability_payload["product"]["inventory"][0]["availability"] = (
            "arriving eventually"
        )
        unknown_availability = client.post(
            "/api/admin/catalog/v2/products/drafts",
            headers={"Idempotency-Key": "reference-draft-availability"},
            json=unknown_availability_payload,
        )

        assert accepted.status_code == 201
        assert unknown_brand.status_code == 422
        assert mismatched_name.status_code == 422
        assert unknown_store.status_code == 422
        assert unknown_category.status_code == 422
        assert unknown_availability.status_code == 422

        with sessions() as db:
            db.get(CatalogBrand, brand["id"]).active = False
            db.commit()
        inactive = client.post(
            "/api/admin/catalog/v2/products/drafts",
            headers={"Idempotency-Key": "reference-draft-inactive-brand"},
            json=_v2_payload(brand_id=brand["id"]),
        )
        assert inactive.status_code == 422


def test_reference_routes_require_catalog_admin(monkeypatch) -> None:
    with _reference_client(monkeypatch) as (client, _, app):
        app.dependency_overrides.pop(require_clerk_principal)

        references = client.get("/api/admin/catalog/v2/references")
        add_brand = client.post(
            "/api/admin/catalog/v2/brands",
            headers={"Idempotency-Key": "unauthorized-brand"},
            json={"name": "Unauthorized Brand"},
        )

        assert references.status_code == 401
        assert add_brand.status_code == 401


def test_catalog_brand_migration_collapses_spellings_and_preserves_products(tmp_path) -> None:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'catalog-brands.db'}", future=True
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "create table catalog_products ("
                "id varchar(64) primary key, brand varchar(128) not null)"
            )
        )
        connection.execute(
            text(
                "insert into catalog_products values "
                "('cat_1', 'August & Mercer'), "
                "('cat_2', ' august  &  mercer '), "
                "('cat_3', 'Lune & Ledger')"
            )
        )

    migration_path = (
        Path(__file__).parents[1]
        / "alembic/versions/e9f0a1b2c3d4_add_catalog_brands.py"
    )
    spec = importlib.util.spec_from_file_location(
        "catalog_brand_migration", migration_path
    )
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        brands = connection.execute(
            text(
                "select id, name, normalized_name from catalog_brands "
                "order by normalized_name"
            )
        ).all()
        products = connection.execute(
            text("select id, brand, brand_id from catalog_products order by id")
        ).all()

    assert [(row.name, row.normalized_name) for row in brands] == [
        ("August & Mercer", "august & mercer"),
        ("Lune & Ledger", "lune & ledger"),
    ]
    assert products[0].brand_id == products[1].brand_id
    assert products[2].brand_id != products[0].brand_id
    assert [row.brand for row in products] == [
        "August & Mercer",
        " august  &  mercer ",
        "Lune & Ledger",
    ]

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()
        assert connection.execute(
            text("select count(*) from catalog_products")
        ).scalar_one() == 3

    inspector = inspect(engine)
    assert "catalog_brands" not in inspector.get_table_names()
    assert "brand_id" not in {
        column["name"] for column in inspector.get_columns("catalog_products")
    }
    engine.dispose()
