from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import logging

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.database import Base, get_db
from app.main import create_app
from app.models import Product, Store, SyntheticRun
from app.services.catalog_normalization import backfill_catalog_from_legacy_products


@contextmanager
def _catalog_client(monkeypatch):
    monkeypatch.setenv("ENABLE_MCP_ADAPTER", "false")
    monkeypatch.setenv("ENABLE_OPENAI_APPS_UI", "false")
    get_settings.cache_clear()

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(engine)

    app = create_app()

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    session = TestingSessionLocal()
    try:
        _seed_catalog(session)
        yield TestClient(app)
    finally:
        session.close()
        app.dependency_overrides.clear()
        engine.dispose()
        get_settings.cache_clear()


def _product(product_id: str, **overrides) -> Product:
    defaults = {
        "id": product_id,
        "seed_run_id": "run_catalog",
        "store_id": "1001",
        "title": "Valentino Rose Dress",
        "description": "Event-ready silk dress for wedding guests",
        "link": f"https://fashion.example/products/{product_id}",
        "image_link": f"https://fashion.example/images/{product_id}.jpg",
        "price": Decimal("750.00"),
        "availability": "in stock",
        "brand": "Valentino",
        "category": "womens_apparel",
        "color": "Rose",
        "size": "M",
        "material": "silk",
        "gender": "women",
        "season": "spring",
        "margin_pct": Decimal("0.6200"),
        "inventory_qty": 12,
        "objective_weight": Decimal("0.9000"),
        "metadata_json": {
            "image_set": {
                "thumbnail_url": f"https://cdn.example/products/{product_id}-thumb.jpg",
                "primary_url": f"https://cdn.example/products/{product_id}-detail-1.jpg",
                "detail_urls": [
                    f"https://cdn.example/products/{product_id}-detail-1.jpg",
                    f"https://cdn.example/products/{product_id}-detail-2.jpg",
                ],
            }
        },
    }
    defaults.update(overrides)
    return Product(**defaults)


def _seed_catalog(session):
    now = datetime(2026, 3, 14, tzinfo=timezone.utc)
    session.add(SyntheticRun(id="run_catalog", seed=101, status="loaded", started_at=now, config={}))
    session.add(
        Store(
            id="1001",
            seed_run_id="run_catalog",
            name="Dallas Downtown",
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
    session.add_all(
        [
            _product("prod_1"),
            _product(
                "prod_2",
                title="Jimmy Choo Satin Pump",
                description="Occasion heel in gold satin",
                brand="Jimmy Choo",
                category="shoes",
                color="Gold",
                size="8",
                material="satin",
                price=Decimal("595.00"),
                inventory_qty=4,
                objective_weight=Decimal("0.7000"),
            ),
            _product(
                "prod_3",
                title="Valentino Silk Blouse",
                description="Tailored silk blouse",
                price=Decimal("425.00"),
                color="Ivory",
                size="S",
                inventory_qty=0,
                availability="out of stock",
                objective_weight=Decimal("0.6000"),
            ),
            _product(
                "prod_4",
                title="Akris Travel Trouser",
                description="Workwear trouser",
                brand="Akris",
                category="mens_apparel",
                gender="men",
                price=Decimal("390.00"),
                objective_weight=Decimal("0.8000"),
            ),
        ]
    )
    session.commit()
    backfill_catalog_from_legacy_products(session, run_id="run_catalog")


def test_categories_expose_taxonomy_and_counts(monkeypatch):
    with _catalog_client(monkeypatch) as client:
        response = client.get("/api/categories")
        catalog_response = client.get("/api/catalog/categories")
        store_response = client.get("/api/stores/1001/categories")

    assert response.status_code == 200
    categories = {row["id"]: row for row in response.json()["categories"]}
    assert categories["womens_apparel"]["label"] == "Women's Apparel"
    assert categories["womens_apparel"]["product_count"] == 2
    assert categories["shoes"]["available_units"] == 4
    assert catalog_response.status_code == 200
    assert catalog_response.json() == response.json()
    assert store_response.status_code == 200
    assert {row["id"] for row in store_response.json()["categories"]} == {"mens_apparel", "shoes", "womens_apparel"}


def test_catalog_index_and_products_do_not_require_store_id(monkeypatch):
    with _catalog_client(monkeypatch) as client:
        index = client.get("/api/catalog", params={"limit": 2})
        products = client.get("/api/catalog/products", params={"limit": 2})

    assert index.status_code == 200
    index_payload = index.json()
    assert len(index_payload["categories"]) == 3
    assert len(index_payload["products"]) == 2
    assert index_payload["products"][0]["id"].startswith("cat_")
    assert "inventory" not in index_payload["products"][0]
    assert products.status_code == 200
    assert products.json()["items"][0]["id"].startswith("cat_")


def test_product_list_filters_facets_and_inventory_shape(monkeypatch):
    with _catalog_client(monkeypatch) as client:
        response = client.get(
            "/api/products",
            params={"category": "womens_apparel", "brand": "Valentino", "in_stock_only": "true", "limit": 10},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["id"].startswith("cat_")
    assert item["catalog_id"].startswith("cat_")
    assert item["default_variant_id"].startswith("var_")
    assert item["price_min"] == 750.0
    assert item["price_max"] == 750.0
    assert item["image_url"] == "https://cdn.example/products/prod_1-thumb.jpg"
    assert item["images"] == {
        "thumbnail_url": "https://cdn.example/products/prod_1-thumb.jpg",
        "primary_url": "https://cdn.example/products/prod_1-detail-1.jpg",
        "detail_urls": [
            "https://cdn.example/products/prod_1-detail-1.jpg",
            "https://cdn.example/products/prod_1-detail-2.jpg",
        ],
    }
    assert item["inventory_summary"]["in_stock_units"] == 12
    assert "inventory" not in item
    assert {facet["name"] for facet in payload["facets"]} == {"brand", "category", "size", "color"}


def test_product_detail_and_related(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="app.catalog.legacy_projection")
    with _catalog_client(monkeypatch) as client:
        detail = client.get("/api/products/prod_1")
        related = client.get("/api/products/prod_1/related", params={"limit": 5})

    assert detail.status_code == 200
    assert detail.json()["attributes"]["material"] == "silk"
    assert detail.json()["inventory"][0]["store_id"] == "1001"
    assert detail.json()["inventory"][0]["inventory_qty"] == 12
    assert detail.json()["variants"][0]["id"].startswith("var_compat_")
    assert detail.json()["variants"][0]["inventory"][0]["store_id"] == "1001"
    assert detail.json()["variants"][0]["price_min"] == detail.json()["price_min"]
    assert detail.json()["variants"][0]["images"] == detail.json()["images"]
    assert detail.json()["reviews"] == []
    assert "moderation" not in detail.text
    assert "external_review_id" not in detail.text
    assert "catalog_legacy_variant_projection" in caplog.messages
    assert related.status_code == 200
    related_ids = {item["id"] for item in related.json()["items"]}
    assert detail.json()["id"] not in related_ids
    assert len(related_ids) >= 1


def test_openai_feed_uses_canonical_published_products(monkeypatch):
    with _catalog_client(monkeypatch) as client:
        response = client.get("/feeds/products/openai", params={"store_id": "1001"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 4
    assert all(item["id"].startswith("cat_") for item in payload["items"])
    dress = next(item for item in payload["items"] if item["title"] == "Valentino Rose Dress")
    assert dress["price"] == "750.00 USD"
    assert dress["availability"] == "in_stock"
    assert dress["image_link"] == "https://cdn.example/products/prod_1-detail-1.jpg"


def test_product_search_and_recommendations_do_not_require_embeddings(monkeypatch):
    with _catalog_client(monkeypatch) as client:
        search = client.get("/api/search/products", params={"q": "satin", "limit": 10})
        recs = client.post(
            "/api/recommendations/products",
            json={"store_id": "1001", "category": "womens_apparel", "budget_max": 800, "top_k": 3},
        )

    assert search.status_code == 200
    assert len(search.json()["items"]) == 1
    assert search.json()["items"][0]["title"] == "Jimmy Choo Satin Pump"
    assert recs.status_code == 200
    payload = recs.json()
    assert payload["strategy"] == "sql_catalog_rules"
    assert [row["product"]["title"] for row in payload["recommendations"]] == ["Valentino Rose Dress"]
