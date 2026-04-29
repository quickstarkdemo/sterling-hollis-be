from __future__ import annotations

import base64
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Product, Store, SyntheticRun
from app.services.catalog_normalization import backfill_catalog_from_legacy_products
from app.services.product_images import (
    ProductImageGenerator,
    build_variant_image_prompt,
    product_image_options,
    product_variant_image_set,
    query_variants_for_image_generation,
)

_ONE_BY_ONE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class _FakeImages:
    def __init__(self, image_bytes: bytes):
        self.image_bytes = image_bytes
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        count = int(kwargs.get("n") or 1)
        return SimpleNamespace(
            data=[SimpleNamespace(b64_json=base64.b64encode(self.image_bytes).decode("ascii")) for _ in range(count)]
        )


class _FakeOpenAIClient:
    def __init__(self, image_bytes: bytes = _ONE_BY_ONE_PNG):
        self.images = _FakeImages(image_bytes)


def _product(product_id: str = "prod_img_1", image_link: str = "https://fashion.example/images/prod_img_1.jpg") -> Product:
    return Product(
        id=product_id,
        seed_run_id="run_images",
        store_id="1001",
        title="Valentino Rose Silk Dress",
        description="Event-ready silk dress with soft rose color",
        link=f"https://fashion.example/products/{product_id}",
        image_link=image_link,
        price=Decimal("750.00"),
        availability="in stock",
        brand="Valentino",
        category="womens_apparel",
        color="Rose",
        size="M",
        material="silk",
        gender="women",
        season="spring",
        margin_pct=Decimal("0.6200"),
        inventory_qty=12,
        objective_weight=Decimal("0.9000"),
        metadata_json={},
    )


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
    session.add(SyntheticRun(id="run_images", seed=1, status="loaded", started_at=now, config={}))
    session.add(
        Store(
            id="1001",
            seed_run_id="run_images",
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
    return engine, session


def test_product_image_prompt_uses_catalog_fields():
    engine, session = _session()
    product = _product()
    session.add(product)
    session.commit()
    backfill_catalog_from_legacy_products(session, run_id="run_images")
    variant = query_variants_for_image_generation(session, category="womens_apparel", limit=1)[0]

    try:
        prompt = build_variant_image_prompt(variant.product, variant)
    finally:
        session.close()
        engine.dispose()

    assert "Valentino Rose Silk Dress" in prompt
    assert "Event-ready silk dress" in prompt
    assert "- Material: silk" in prompt
    assert "Do not include readable text" in prompt


def test_generator_writes_image_and_updates_image_link(tmp_path):
    engine, session = _session()
    product = _product()
    session.add(product)
    session.commit()
    backfill_catalog_from_legacy_products(session, run_id="run_images")
    variant = query_variants_for_image_generation(session, category="womens_apparel", limit=1)[0]
    fake_client = _FakeOpenAIClient()
    options = product_image_options(
        output_dir=tmp_path,
        public_base_url="https://products.example",
        detail_count=3,
        thumbnail_size=128,
        overwrite=False,
        dry_run=False,
    )

    try:
        result = ProductImageGenerator(options, client=fake_client).generate_for_variant(session, variant)
        session.refresh(variant)
    finally:
        session.close()
        engine.dispose()

    assert result.status == "generated"
    assert result.variant_id == variant.id
    assert result.image_link == variant.image_link
    assert result.thumbnail_link is not None
    assert len(result.detail_links or []) == 3
    assert variant.image_link.startswith("https://products.example/product-images/")
    for path in result.detail_paths or []:
        assert (tmp_path / path.split("/")[-1]).exists()
    assert result.thumbnail_path is not None
    assert (tmp_path / result.thumbnail_path.split("/")[-1]).exists()
    image_set = product_variant_image_set(variant)
    assert image_set is not None
    assert image_set["thumbnail_url"] == result.thumbnail_link
    assert image_set["primary_url"] == result.image_link
    assert image_set["detail_urls"] == result.detail_links
    assert fake_client.images.calls[0]["model"] == options.model
    assert "response_format" not in fake_client.images.calls[0]
    assert fake_client.images.calls[0]["n"] == 3


def test_generator_skips_existing_non_placeholder_image(tmp_path):
    engine, session = _session()
    product = _product(image_link="https://cdn.example/prod_img_1.jpg")
    session.add(product)
    session.commit()
    backfill_catalog_from_legacy_products(session, run_id="run_images")
    variant = query_variants_for_image_generation(session, category="womens_apparel", limit=1)[0]
    fake_client = _FakeOpenAIClient()
    options = product_image_options(output_dir=tmp_path, public_base_url="https://products.example")

    try:
        result = ProductImageGenerator(options, client=fake_client).generate_for_variant(session, variant)
    finally:
        session.close()
        engine.dispose()

    assert result.status == "skipped_existing_image"
    assert fake_client.images.calls == []


def test_query_variants_for_image_generation_filters_by_category():
    engine, session = _session()
    session.add_all([_product("prod_a"), _product("prod_b", image_link="https://fashion.example/images/prod_b.jpg")])
    session.commit()
    backfill_catalog_from_legacy_products(session, run_id="run_images")

    try:
        variants = query_variants_for_image_generation(session, category="womens_apparel", limit=10)
    finally:
        session.close()
        engine.dispose()

    assert len(variants) == 1
    assert variants[0].id.startswith("var_")
