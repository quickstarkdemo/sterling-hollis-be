from __future__ import annotations

import base64
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import CatalogProduct, Product, ProductVariant, Store, SupplierProductOffer, SyntheticRun
from app.services.catalog_normalization import backfill_catalog_from_legacy_products
from app.services.product_images import (
    ProductImageGenerator,
    build_variant_image_prompt,
    product_image_options,
    product_variant_image_set,
    query_variants_for_image_generation,
)
from scripts.rewrite_product_image_urls import rewrite_product_image_urls

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


def test_rewrite_product_image_urls_updates_stored_urls():
    old_base = "https://products-api.quickstark.com"
    new_base = "https://sterling-hollis-be.quickstark.com"
    engine, session = _session()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    product = _product(
        image_link=f"{old_base}/product-images/prod_img_1-detail-1.jpg",
    )
    product.metadata_json = {
        "image_set": {
            "thumbnail_url": f"{old_base}/product-images/prod_img_1-thumb.jpg",
            "primary_url": f"{old_base}/product-images/prod_img_1-detail-1.jpg",
            "detail_urls": [f"{old_base}/product-images/prod_img_1-detail-1.jpg"],
        }
    }
    catalog = CatalogProduct(
        id="cat_img_1",
        seed_run_id="run_images",
        catalog_key="valentino|dress",
        title="Valentino Rose Silk Dress",
        description="Event-ready silk dress with soft rose color",
        brand="Valentino",
        category="womens_apparel",
        metadata_json={},
    )
    variant = ProductVariant(
        id="var_img_1",
        seed_run_id="run_images",
        catalog_product_id="cat_img_1",
        variant_key="valentino|dress|rose",
        color="Rose",
        material="silk",
        gender="women",
        season="spring",
        price_min=Decimal("750.00"),
        price_max=Decimal("750.00"),
        link="https://fashion.example/products/prod_img_1",
        image_link=f"{old_base}/product-images/var_img_1-detail-1.jpg",
        image_set={
            "thumbnail_url": f"{old_base}/product-images/var_img_1-thumb.jpg",
            "primary_url": f"{old_base}/product-images/var_img_1-detail-1.jpg",
            "detail_urls": [
                f"{old_base}/product-images/var_img_1-detail-1.jpg",
                "https://cdn.example/other.jpg",
            ],
        },
        metadata_json={"source_url": f"{old_base}/not-product-images/unchanged.jpg"},
    )
    supplier_offer = SupplierProductOffer(
        id="offer_img_1",
        seed_run_id="run_images",
        brand="Valentino",
        title="Supplier Dress",
        category="womens_apparel",
        price=Decimal("700.00"),
        size="M",
        season="spring",
        status="candidate",
        link="https://fashion.example/supplier/offer_img_1",
        image_link=f"{old_base}/product-images/offer_img_1-detail-1.jpg",
        metadata_json={"gallery": [f"{old_base}/product-images/offer_img_1-thumb.jpg"]},
    )
    session.add_all([product, catalog, variant, supplier_offer])
    session.commit()

    try:
        stats = rewrite_product_image_urls(
            old_base_url=old_base,
            new_base_url=new_base,
            url_path="/product-images",
            dry_run=False,
            session_factory=SessionLocal,
        )
        session.expire_all()
        rewritten_product = session.get(Product, "prod_img_1")
        rewritten_variant = session.get(ProductVariant, "var_img_1")
        rewritten_offer = session.get(SupplierProductOffer, "offer_img_1")
    finally:
        session.close()
        engine.dispose()

    assert stats.touched_rows == 3
    assert stats.rewritten_values == 10
    assert rewritten_product.image_link.startswith(f"{new_base}/product-images/")
    assert rewritten_product.metadata_json["image_set"]["thumbnail_url"].startswith(f"{new_base}/product-images/")
    assert rewritten_variant.image_link.startswith(f"{new_base}/product-images/")
    assert rewritten_variant.image_set["primary_url"].startswith(f"{new_base}/product-images/")
    assert rewritten_variant.image_set["detail_urls"][1] == "https://cdn.example/other.jpg"
    assert rewritten_variant.metadata_json["source_url"] == f"{old_base}/not-product-images/unchanged.jpg"
    assert rewritten_offer.image_link.startswith(f"{new_base}/product-images/")
    assert rewritten_offer.metadata_json["gallery"][0].startswith(f"{new_base}/product-images/")


def test_rewrite_product_image_urls_dry_run_rolls_back():
    old_base = "https://products-api.quickstark.com"
    new_base = "https://sterling-hollis-be.quickstark.com"
    engine, session = _session()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    product = _product(image_link=f"{old_base}/product-images/prod_img_1-detail-1.jpg")
    session.add(product)
    session.commit()

    try:
        stats = rewrite_product_image_urls(
            old_base_url=old_base,
            new_base_url=new_base,
            url_path="/product-images",
            dry_run=True,
            session_factory=SessionLocal,
        )
        session.expire_all()
        rewritten_product = session.get(Product, "prod_img_1")
    finally:
        session.close()
        engine.dispose()

    assert stats.touched_rows == 1
    assert rewritten_product.image_link == f"{old_base}/product-images/prod_img_1-detail-1.jpg"
