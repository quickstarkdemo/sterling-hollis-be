from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.catalog.schemas import ImageAnalysisAttributes, ImageAnalysisResponse
from app.config import get_settings
from app.database import Base, get_db
from app.main import create_app
from app.models import CatalogProduct, CatalogProductEmbedding, Product, Store, SyntheticRun
from app.schemas import StyleConstraints
from app.services.catalog_normalization import backfill_catalog_from_legacy_products
from app.services.image_analysis import ImageAnalysisService, ImageUploadError, validate_image_bytes


_ONE_BY_ONE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _FakeEmbeddingService:
    enabled = True
    model = "fake-embedding"

    def embed_text(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]

    def embed_texts(self, texts) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]

    def _deterministic_vector(self, text: str) -> list[float]:
        return [0.0, 0.0, 1.0]


class _FakePineconeService:
    enabled = True
    calls: list[tuple[str, list[dict]]] = []
    query_matches: list[dict] = []

    def __init__(self) -> None:
        self.settings = get_settings()

    def upsert(self, namespace: str, vectors: list[dict]) -> None:
        self.calls.append((namespace, vectors))

    def query(self, namespace: str, vector: list[float], top_k: int, filters: dict | None = None) -> list[dict]:
        return list(self.query_matches)


class _DisabledPineconeService(_FakePineconeService):
    enabled = False


class _FakeAnalysisService:
    def analyze(self, image_bytes: bytes, mime_type: str, *, context: str | None = None) -> ImageAnalysisResponse:
        analysis = ImageAnalysisAttributes(
            summary="Rose silk occasion dress",
            target_categories=["womens_apparel"],
            target_genders=["female"],
            colors=["rose"],
            materials=["silk"],
            patterns=[],
            style_keywords=["occasion", "tailored"],
            occasion_keywords=["wedding"],
            confidence=0.91,
        )
        return ImageAnalysisResponse(
            analysis=analysis,
            style_constraints=StyleConstraints(
                constraint_source="consumer_image",
                target_categories=["womens_apparel"],
                target_genders=["female"],
                style_keywords=["occasion", "tailored", "rose", "silk"],
            ),
            model="fake-vision",
            image_discarded=True,
        )


def _product(product_id: str, **overrides) -> Product:
    defaults = {
        "id": product_id,
        "seed_run_id": "run_image_recs",
        "store_id": "1001",
        "title": "Valentino Rose Silk Dress",
        "description": "Event-ready silk dress with soft rose color",
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
        "metadata_json": {},
    }
    defaults.update(overrides)
    return Product(**defaults)


def _seed(session) -> None:
    now = datetime(2026, 3, 14, tzinfo=timezone.utc)
    session.add(SyntheticRun(id="run_image_recs", seed=1, status="loaded", started_at=now, config={}))
    session.add(
        Store(
            id="1001",
            seed_run_id="run_image_recs",
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
                material="satin",
                price=Decimal("595.00"),
                objective_weight=Decimal("0.7000"),
            ),
        ]
    )
    session.commit()
    backfill_catalog_from_legacy_products(session, run_id="run_image_recs")


def _session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    with TestingSessionLocal() as session:
        _seed(session)
    return engine, TestingSessionLocal


@contextmanager
def _client(monkeypatch):
    monkeypatch.setenv("ENABLE_MCP_ADAPTER", "false")
    monkeypatch.setenv("ENABLE_OPENAI_APPS_UI", "false")
    get_settings.cache_clear()
    engine, TestingSessionLocal = _session_factory()
    app = create_app()

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app), TestingSessionLocal
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
        get_settings.cache_clear()


def test_image_validation_rejects_invalid_inputs(tmp_path):
    assert validate_image_bytes(_ONE_BY_ONE_PNG, "image/png", max_bytes=1024) == "image/png"

    with pytest.raises(ImageUploadError) as type_error:
        validate_image_bytes(_ONE_BY_ONE_PNG, "text/plain", max_bytes=1024)
    assert type_error.value.status_code == 415

    with pytest.raises(ImageUploadError) as size_error:
        validate_image_bytes(_ONE_BY_ONE_PNG, "image/png", max_bytes=4)
    assert size_error.value.status_code == 413

    with pytest.raises(ImageUploadError):
        validate_image_bytes(b"not an image", "image/png", max_bytes=1024)
    with pytest.raises(ImageUploadError):
        validate_image_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
            ),
            "image/png",
            max_bytes=1024,
        )
    assert list(tmp_path.iterdir()) == []


def test_image_analysis_service_parses_openai_structured_output(monkeypatch):
    payload = {
        "summary": "A tailored rose silk dress for an event",
        "target_categories": ["womens_apparel", "not_a_category"],
        "exclude_categories": ["athleticwear"],
        "target_genders": ["women"],
        "colors": ["Rose"],
        "materials": ["Silk"],
        "patterns": ["solid"],
        "style_keywords": ["Tailored", "occasion"],
        "occasion_keywords": ["wedding"],
        "confidence": 0.88,
    }

    class _FakeResponses:
        def __init__(self) -> None:
            self.call = None

        def create(self, **kwargs):
            self.call = kwargs
            return SimpleNamespace(output_text=json.dumps(payload))

    fake_responses = _FakeResponses()
    fake_client = SimpleNamespace(responses=fake_responses)
    monkeypatch.setenv("IMAGE_ANALYSIS_MODEL", "fake-vision")
    get_settings.cache_clear()

    result = ImageAnalysisService(client=fake_client).analyze(_ONE_BY_ONE_PNG, "image/png")

    assert result.model == "fake-vision"
    assert result.image_discarded is True
    assert result.analysis.target_categories == ["womens_apparel"]
    assert result.style_constraints.constraint_source == "consumer_image"
    assert result.style_constraints.target_genders == ["female"]
    assert fake_responses.call["input"][0]["content"][1]["type"] == "input_image"
    get_settings.cache_clear()


def test_index_products_writes_global_catalog_vectors(monkeypatch):
    import app.services.indexing as indexing

    _FakePineconeService.calls = []
    monkeypatch.setattr(indexing, "EmbeddingService", _FakeEmbeddingService)
    monkeypatch.setattr(indexing, "PineconeService", _FakePineconeService)
    engine, TestingSessionLocal = _session_factory()
    try:
        with TestingSessionLocal() as session:
            result = indexing.index_products_for_run(session, "run_image_recs", batch_size=10)
            rows = session.scalars(select(CatalogProductEmbedding)).all()
    finally:
        engine.dispose()

    assert result["status_breakdown"]["catalog_attempted"] == 2
    assert result["status_breakdown"]["catalog_indexed"] == 2
    assert len(rows) == 2
    catalog_calls = [call for call in _FakePineconeService.calls if call[0] == "catalog_products_v1"]
    assert catalog_calls
    assert all(vector["id"].startswith("catalog:") for vector in catalog_calls[0][1])


def test_image_analysis_endpoint_returns_attributes(monkeypatch):
    import app.routers.catalog as catalog_router

    monkeypatch.setattr(catalog_router, "ImageAnalysisService", lambda: _FakeAnalysisService())
    with _client(monkeypatch) as (client, _):
        response = client.post(
            "/api/image-analysis",
            files={"image": ("style.png", _ONE_BY_ONE_PNG, "image/png")},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["image_discarded"] is True
    assert payload["analysis"]["target_categories"] == ["womens_apparel"]
    assert payload["style_constraints"]["constraint_source"] == "consumer_image"


def test_image_recommendations_use_catalog_vector_namespace(monkeypatch):
    import app.catalog.service as catalog_service
    import app.routers.catalog as catalog_router

    monkeypatch.setattr(catalog_router, "ImageAnalysisService", lambda: _FakeAnalysisService())
    monkeypatch.setattr(catalog_service, "EmbeddingService", _FakeEmbeddingService)
    monkeypatch.setattr(catalog_service, "PineconeService", _FakePineconeService)

    with _client(monkeypatch) as (client, TestingSessionLocal):
        with TestingSessionLocal() as session:
            catalog_id = session.scalar(select(CatalogProduct.id).where(CatalogProduct.title.ilike("%Rose Silk%")))
        _FakePineconeService.query_matches = [
            {"id": f"catalog:{catalog_id}", "score": 0.93, "metadata": {"catalog_product_id": catalog_id}}
        ]
        response = client.post(
            "/api/recommendations/image",
            files={"image": ("style.png", _ONE_BY_ONE_PNG, "image/png")},
            data={"top_k": "3"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["strategy"] == "catalog_vector_image"
    assert payload["recommendations"][0]["product"]["id"] == catalog_id
    assert payload["recommendations"][0]["strategy"] == "catalog_vector_image"


def test_image_recommendations_fall_back_to_sql_when_vectors_disabled(monkeypatch):
    import app.catalog.service as catalog_service
    import app.routers.catalog as catalog_router

    monkeypatch.setattr(catalog_router, "ImageAnalysisService", lambda: _FakeAnalysisService())
    monkeypatch.setattr(catalog_service, "EmbeddingService", _FakeEmbeddingService)
    monkeypatch.setattr(catalog_service, "PineconeService", _DisabledPineconeService)

    with _client(monkeypatch) as (client, _):
        response = client.post(
            "/api/recommendations/image",
            files={"image": ("style.png", _ONE_BY_ONE_PNG, "image/png")},
            data={"top_k": "3"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["strategy"] == "sql_catalog_image_rules"
    assert payload["recommendations"][0]["product"]["title"] == "Valentino Rose Silk Dress"
