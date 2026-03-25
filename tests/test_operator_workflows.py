from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import (
    Customer,
    CustomerCommunication,
    ExecutiveStrategyPacket,
    Order,
    OrderItem,
    Product,
    Store,
    SupplierProductOffer,
    SyntheticRun,
)
from app.schemas import (
    CompareMode,
    CustomerRecommendationRequest,
    Objective,
    PeerMode,
    PriceBand,
    ProductRecommendation,
    ProductPerformanceDimension,
    RetrievalMode,
    StyleConstraints,
)
from app.services.communications import (
    customer_message_history,
    get_customer_email_draft,
    prepare_customer_email_draft,
    prepare_customer_sms,
    send_customer_email_draft,
    send_customer_recommendations_email,
    send_customer_sms,
    twilio_smoke_test,
    update_customer_email_draft,
    update_customer_sms_draft,
)
from app.services.index_jobs import process_next_index_job
from app.services.lookup import find_customers, resolve_customer, resolve_store
from app.services.customer_value import customer_value_summary
from app.services.executive import auto_optimize_strategy, apply_execution_tags_for_store
from app.services.merchandising import merchandising_action_recommendations, merchandising_diagnostics, merchandising_trend_summary
from app.services.demo_customer import DEMO_CUSTOMER_ID


class _DummyPineconeService:
    enabled = False


class _BoomEmbeddingService:
    def embed_text(self, text):
        raise AssertionError("embedding path should not run in fast mode")


def test_customer_communication_destination_supports_email_length():
    assert CustomerCommunication.__table__.c.destination_e164.type.length == 255


def test_style_constraints_normalize_fields():
    request = CustomerRecommendationRequest(
        store_id="1001",
        style_constraints=StyleConstraints(
            constraint_source=" Chat_Image ",
            target_categories=["Women's Apparel", " womens_apparel ", "MENS_APPAREL"],
            exclude_categories=["Handbags", "handbags"],
            target_genders=["Women", " female ", "UNISEX"],
            style_keywords=["Tailored", "tailored", "Minimal"],
        ),
    )

    assert request.style_constraints is not None
    assert request.style_constraints.constraint_source == "chat_image"
    assert request.style_constraints.target_categories == ["women's apparel", "womens_apparel", "mens_apparel"]
    assert request.style_constraints.exclude_categories == ["handbags"]
    assert request.style_constraints.target_genders == ["female", "unisex"]
    assert request.style_constraints.style_keywords == ["tailored", "minimal"]


@contextmanager
def _patched_runtime(monkeypatch):
    import app.mcp_server as mcp_server
    import app.services.apps_ui as apps_ui
    import app.services.recommendations as recommendations

    engine = create_engine(
        "sqlite+pysqlite:///:memory:", future=True, connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(mcp_server, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(apps_ui, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(recommendations, "PineconeService", lambda: _DummyPineconeService())

    session = TestingSessionLocal()
    try:
        yield session, mcp_server
    finally:
        session.close()
        engine.dispose()


def _seed_data(session):
    now = datetime(2026, 3, 14, tzinfo=timezone.utc)
    session.add(SyntheticRun(id="run_test", seed=42, status="indexed", started_at=now, config={}))
    stores = [
        Store(
            id="1001",
            seed_run_id="run_test",
            name="Dallas Downtown",
            city="Dallas",
            state="TX",
            postal_code="75201",
            address_line1="1 Main St",
            address_line2=None,
            phone="555-111-1111",
            latitude=Decimal("32.770000"),
            longitude=Decimal("-96.790000"),
            profile_type="texas_core",
            services=["Personal Shopping"],
            raw_source={},
        ),
        Store(
            id="1002",
            seed_run_id="run_test",
            name="Austin Boutique",
            city="Austin",
            state="TX",
            postal_code="78701",
            address_line1="2 Congress Ave",
            address_line2=None,
            phone="555-222-2222",
            latitude=Decimal("30.260000"),
            longitude=Decimal("-97.740000"),
            profile_type="texas_core",
            services=["Styling"],
            raw_source={},
        ),
    ]
    session.add_all(stores)

    customer = Customer(
        id="cust_000001",
        seed_run_id="run_test",
        home_store_id="1001",
        first_name="Avery",
        last_name="Parker",
        email="avery.parker.1@example-fashion.test",
        phone_e164="+12145551234",
        city="Dallas",
        state="TX",
        joined_at=now - timedelta(days=365),
        loyalty_tier="gold",
        sex="male",
        price_sensitivity=Decimal("0.4200"),
        occasion_affinity={"wedding": 0.8, "workwear": 0.9, "vacation": 0.6},
        style_vector={"womens_apparel": 0.99, "beauty": 0.92, "mens_apparel": 0.85, "shoes": 0.7},
        size_preferences={"top": "M", "bottom": "8", "shoe": "8"},
        channel_preference="hybrid",
        pii_token="token-1",
    )
    customer_two = Customer(
        id="cust_000002",
        seed_run_id="run_test",
        home_store_id="1002",
        first_name="Avery",
        last_name="Coleman",
        email="avery.coleman.2@example-fashion.test",
        phone_e164="+13035557654",
        city="Austin",
        state="TX",
        joined_at=now - timedelta(days=210),
        loyalty_tier="silver",
        sex="female",
        price_sensitivity=Decimal("0.5100"),
        occasion_affinity={"wedding": 0.5},
        style_vector={"mens_apparel": 0.98, "womens_apparel": 0.75, "handbags": 0.7, "beauty": 0.66},
        size_preferences={"top": "S", "bottom": "6", "shoe": "7"},
        channel_preference="in_store",
        pii_token="token-2",
    )
    session.add_all([customer, customer_two])

    products = [
        Product(
            id="prod_1",
            seed_run_id="run_test",
            store_id="1001",
            title="Valentino Rose Dress",
            description="Event-ready evening dress",
            link="https://fashion.example/products/prod_1",
            image_link="https://fashion.example/images/prod_1.jpg",
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
            inventory_qty=18,
            objective_weight=Decimal("0.9000"),
            metadata_json={},
        ),
        Product(
            id="prod_2",
            seed_run_id="run_test",
            store_id="1001",
            title="Jimmy Choo Satin Pump",
            description="Occasion heel",
            link="https://fashion.example/products/prod_2",
            image_link="https://fashion.example/images/prod_2.jpg",
            price=Decimal("595.00"),
            availability="in stock",
            brand="Jimmy Choo",
            category="shoes",
            color="Gold",
            size="8",
            material="satin",
            gender="women",
            season="spring",
            margin_pct=Decimal("0.5400"),
            inventory_qty=30,
            objective_weight=Decimal("0.7000"),
            metadata_json={},
        ),
        Product(
            id="prod_3",
            seed_run_id="run_test",
            store_id="1001",
            title="Atelier Veridian Leather Tote",
            description="Large day bag",
            link="https://fashion.example/products/prod_3",
            image_link="https://fashion.example/images/prod_3.jpg",
            price=Decimal("850.00"),
            availability="in stock",
            brand="Atelier Veridian",
            category="handbags",
            color="Black",
            size="One Size",
            material="leather",
            gender="women",
            season="all-season",
            margin_pct=Decimal("0.4800"),
            inventory_qty=40,
            objective_weight=Decimal("0.4000"),
            metadata_json={},
        ),
        Product(
            id="peer_prod_1",
            seed_run_id="run_test",
            store_id="1002",
            title="Peer Dress",
            description="Comparable dress",
            link="https://fashion.example/products/peer_prod_1",
            image_link="https://fashion.example/images/peer_prod_1.jpg",
            price=Decimal("680.00"),
            availability="in stock",
            brand="Valentino",
            category="womens_apparel",
            color="Ivory",
            size="M",
            material="silk",
            gender="women",
            season="spring",
            margin_pct=Decimal("0.5100"),
            inventory_qty=12,
            objective_weight=Decimal("0.8500"),
            metadata_json={},
        ),
        Product(
            id="peer_prod_2",
            seed_run_id="run_test",
            store_id="1002",
            title="Peer Pump",
            description="Comparable heel",
            link="https://fashion.example/products/peer_prod_2",
            image_link="https://fashion.example/images/peer_prod_2.jpg",
            price=Decimal("525.00"),
            availability="in stock",
            brand="Jimmy Choo",
            category="shoes",
            color="Silver",
            size="8",
            material="leather",
            gender="women",
            season="spring",
            margin_pct=Decimal("0.5000"),
            inventory_qty=8,
            objective_weight=Decimal("0.7000"),
            metadata_json={},
        ),
        Product(
            id="prod_4",
            seed_run_id="run_test",
            store_id="1001",
            title="Tom Ford Midnight Sport Coat",
            description="Tailored jacket for formal work events",
            link="https://fashion.example/products/prod_4",
            image_link="https://fashion.example/images/prod_4.jpg",
            price=Decimal("820.00"),
            availability="in stock",
            brand="Tom Ford",
            category="mens_apparel",
            color="Navy",
            size="M",
            material="wool",
            gender="men",
            season="all-season",
            margin_pct=Decimal("0.5800"),
            inventory_qty=20,
            objective_weight=Decimal("0.8800"),
            metadata_json={},
        ),
        Product(
            id="prod_pre_1",
            seed_run_id="run_test",
            store_id="1001",
            title="Theory Preorder Linen Trouser",
            description="Upcoming tailored trouser drop",
            link="https://fashion.example/products/prod_pre_1",
            image_link="https://fashion.example/images/prod_pre_1.jpg",
            price=Decimal("420.00"),
            availability="preorder",
            brand="Theory",
            category="mens_apparel",
            color="Stone",
            size="M",
            material="linen",
            gender="men",
            season="spring",
            margin_pct=Decimal("0.5100"),
            inventory_qty=6,
            objective_weight=Decimal("0.9500"),
            metadata_json={},
        ),
        Product(
            id="prod_oos_1",
            seed_run_id="run_test",
            store_id="1001",
            title="Zegna Out-of-Stock Suit",
            description="Sold out tailored suit",
            link="https://fashion.example/products/prod_oos_1",
            image_link="https://fashion.example/images/prod_oos_1.jpg",
            price=Decimal("980.00"),
            availability="out of stock",
            brand="Zegna",
            category="mens_apparel",
            color="Charcoal",
            size="M",
            material="wool",
            gender="men",
            season="all-season",
            margin_pct=Decimal("0.7000"),
            inventory_qty=0,
            objective_weight=Decimal("0.9900"),
            metadata_json={},
        ),
    ]
    session.add_all(products)
    session.add_all(
        [
            SupplierProductOffer(
                id="offer_1",
                seed_run_id="run_test",
                brand="Valentino",
                title="Valentino Capsule Evening Dress",
                category="womens_apparel",
                price=Decimal("890.00"),
                size="M",
                season="summer",
                available_on=(now + timedelta(days=40)).date(),
                status="potential",
                link="https://fashion.example/supplier-offers/offer_1",
                image_link="https://fashion.example/images/supplier/offer_1.jpg",
                metadata_json={"source_product_id": "prod_1"},
            ),
            SupplierProductOffer(
                id="offer_2",
                seed_run_id="run_test",
                brand="Brunello Cucinelli",
                title="Brunello Cucinelli Preview Trouser",
                category="mens_apparel",
                price=Decimal("760.00"),
                size="M",
                season="fall",
                available_on=(now + timedelta(days=75)).date(),
                status="committed",
                link="https://fashion.example/supplier-offers/offer_2",
                image_link="https://fashion.example/images/supplier/offer_2.jpg",
                metadata_json={"source_product_id": "prod_4"},
            ),
            SupplierProductOffer(
                id="offer_3",
                seed_run_id="run_test",
                brand="Jimmy Choo",
                title="Jimmy Choo Future Pump",
                category="shoes",
                price=Decimal("610.00"),
                size="8",
                season="all-season",
                available_on=(now + timedelta(days=20)).date(),
                status="launched",
                link="https://fashion.example/supplier-offers/offer_3",
                image_link="https://fashion.example/images/supplier/offer_3.jpg",
                metadata_json={"source_product_id": "prod_2"},
            ),
        ]
    )

    order_current = Order(
        id="order_1",
        seed_run_id="run_test",
        customer_id="cust_000001",
        store_id="1001",
        ordered_at=now - timedelta(days=15),
        status="completed",
        occasion="wedding",
        channel="in_store",
        subtotal=Decimal("1345.00"),
        discount_amount=Decimal("0.00"),
        tax_amount=Decimal("100.00"),
        total_amount=Decimal("1445.00"),
        returned=False,
    )
    order_prior = Order(
        id="order_2",
        seed_run_id="run_test",
        customer_id="cust_000001",
        store_id="1001",
        ordered_at=now - timedelta(days=120),
        status="completed",
        occasion="wedding",
        channel="in_store",
        subtotal=Decimal("750.00"),
        discount_amount=Decimal("0.00"),
        tax_amount=Decimal("50.00"),
        total_amount=Decimal("800.00"),
        returned=False,
    )
    peer_order = Order(
        id="order_3",
        seed_run_id="run_test",
        customer_id="cust_000002",
        store_id="1002",
        ordered_at=now - timedelta(days=18),
        status="completed",
        occasion="wedding",
        channel="in_store",
        subtotal=Decimal("1205.00"),
        discount_amount=Decimal("0.00"),
        tax_amount=Decimal("90.00"),
        total_amount=Decimal("1295.00"),
        returned=False,
    )
    session.add_all([order_current, order_prior, peer_order])

    session.add_all(
        [
            OrderItem(id="item_1", order_id="order_1", product_id="prod_1", quantity=1, unit_price=Decimal("750.00"), discount_amount=Decimal("0.00"), line_total=Decimal("750.00")),
            OrderItem(id="item_2", order_id="order_1", product_id="prod_2", quantity=1, unit_price=Decimal("595.00"), discount_amount=Decimal("0.00"), line_total=Decimal("595.00")),
            OrderItem(id="item_3", order_id="order_2", product_id="prod_1", quantity=1, unit_price=Decimal("750.00"), discount_amount=Decimal("0.00"), line_total=Decimal("750.00")),
            OrderItem(id="item_4", order_id="order_3", product_id="peer_prod_1", quantity=1, unit_price=Decimal("680.00"), discount_amount=Decimal("0.00"), line_total=Decimal("680.00")),
            OrderItem(id="item_5", order_id="order_3", product_id="peer_prod_2", quantity=1, unit_price=Decimal("525.00"), discount_amount=Decimal("0.00"), line_total=Decimal("525.00")),
        ]
    )
    session.commit()


def _set_strategy_flags(
    monkeypatch,
    mcp_server,
    *,
    exec_auto=False,
    packet=False,
    merch_context=False,
    associate_tags=False,
):
    monkeypatch.setattr(mcp_server.settings, "exec_auto_optimize_enabled", exec_auto)
    monkeypatch.setattr(mcp_server.settings, "strategy_packet_enabled", packet)
    monkeypatch.setattr(mcp_server.settings, "merch_strategy_context_enabled", merch_context)
    monkeypatch.setattr(mcp_server.settings, "associate_priority_tags_enabled", associate_tags)


def test_customer_lookup_supports_name_email_and_phone(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, _):
        _seed_data(session)

        search = find_customers(session, query="Avery", limit=10)
        assert len(search.results) == 2
        assert search.results[0].full_name.startswith("Avery")

        resolved_by_email = resolve_customer(session, email="avery.parker.1@example-fashion.test").resolved
        resolved_by_phone = resolve_customer(session, phone_e164="+12145551234").resolved
        resolved_by_last4 = resolve_customer(session, phone_last4="1234").resolved
        resolved_customer_two = resolve_customer(session, customer_id="cust_000002").resolved
        store = resolve_store(session, store_query="Dallas").resolved

        assert resolved_by_email.id == "cust_000001"
        assert resolved_by_phone.id == "cust_000001"
        assert resolved_by_last4.phone_e164 == "+12145551234"
        assert resolved_by_email.sex == "male"
        assert "womens_apparel" not in resolved_by_email.preferred_categories
        assert resolved_by_email.preferred_categories[:2] == ["beauty", "mens_apparel"]
        assert resolved_by_email.preferred_occasions[:2] == ["workwear", "wedding"]
        assert resolved_by_email.size_preferences["top"] == "M"
        assert resolved_customer_two.sex == "female"
        assert "mens_apparel" not in resolved_customer_two.preferred_categories
        assert resolved_customer_two.preferred_categories[:2] == ["womens_apparel", "handbags"]
        assert store.id == "1001"


def test_customer_value_summary_handles_empty_history(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, _):
        _seed_data(session)
        now = datetime(2026, 3, 14, tzinfo=timezone.utc)
        session.add(
            Customer(
                id="cust_000003",
                seed_run_id="run_test",
                home_store_id="1001",
                first_name="No",
                last_name="Orders",
                email="no.orders@example-fashion.test",
                phone_e164="+12145557777",
                city="Dallas",
                state="TX",
                joined_at=now - timedelta(days=30),
                loyalty_tier="bronze",
                sex="male",
                price_sensitivity=Decimal("0.5000"),
                occasion_affinity={},
                style_vector={},
                size_preferences={},
                channel_preference="in_store",
                pii_token="token-3",
            )
        )
        session.commit()

        summary = customer_value_summary(session, customer_id="cust_000003", lookback_days=180, forecast_weeks=8)

        assert summary.metrics.value_score == 0
        assert summary.metrics.value_tier == "low"
        assert summary.metrics.lifetime_spend == 0
        assert summary.metrics.lookback_spend == 0
        assert summary.metrics.lifetime_orders == 0
        assert summary.metrics.lookback_orders == 0
        assert summary.metrics.recency_days is None
        assert summary.purchase_series
        assert all(point.spend == 0 and point.orders == 0 for point in summary.purchase_series)
        assert len(summary.forecast_series) == 8
        assert all(point.projected_spend == 0 for point in summary.forecast_series)


def test_customer_value_summary_excludes_returned_orders_and_uses_all_store_scope(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, _):
        _seed_data(session)
        now = datetime(2026, 3, 14, tzinfo=timezone.utc)
        session.add_all(
            [
                Order(
                    id="order_cross_store",
                    seed_run_id="run_test",
                    customer_id="cust_000001",
                    store_id="1002",
                    ordered_at=now - timedelta(days=10),
                    status="completed",
                    occasion="workwear",
                    channel="in_store",
                    subtotal=Decimal("500.00"),
                    discount_amount=Decimal("0.00"),
                    tax_amount=Decimal("40.00"),
                    total_amount=Decimal("540.00"),
                    returned=False,
                ),
                Order(
                    id="order_returned_should_skip",
                    seed_run_id="run_test",
                    customer_id="cust_000001",
                    store_id="1001",
                    ordered_at=now - timedelta(days=9),
                    status="completed",
                    occasion="workwear",
                    channel="in_store",
                    subtotal=Decimal("9000.00"),
                    discount_amount=Decimal("0.00"),
                    tax_amount=Decimal("0.00"),
                    total_amount=Decimal("9000.00"),
                    returned=True,
                ),
            ]
        )
        session.commit()

        summary = customer_value_summary(session, customer_id="cust_000001", lookback_days=180, forecast_weeks=8)

        assert summary.purchase_scope.value == "all_stores"
        assert round(summary.metrics.lookback_spend, 2) == 2785.00
        assert summary.metrics.lookback_orders == 3
        assert round(summary.metrics.lifetime_spend, 2) == 2785.00
        assert summary.metrics.lifetime_orders == 3


def test_customer_value_summary_forecast_falls_back_with_sparse_history(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, _):
        _seed_data(session)

        summary = customer_value_summary(session, customer_id="cust_000002", lookback_days=90, forecast_weeks=4)

        assert len(summary.forecast_series) == 4
        projected_values = {point.projected_spend for point in summary.forecast_series}
        assert len(projected_values) == 1
        assert all(point.low_spend == point.projected_spend for point in summary.forecast_series)
        assert all(point.high_spend == point.projected_spend for point in summary.forecast_series)


def test_recommendations_respect_customer_sex_and_preferences(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)

        response = mcp_server.fashion_store_associate_recommend(
            store_id="1001",
            customer_id="cust_000001",
            occasion="workwear",
            budget_max=900,
            retrieval_mode=RetrievalMode.fast,
            top_k=4,
        )

        recs = response.recommendation.recommendations
        assert recs
        assert recs[0].product_id == "prod_4"
        assert any("matched male profile" in reason for reason in recs[0].reasons)
        product_ids = [item.product_id for item in recs]
        products = session.scalars(select(Product).where(Product.id.in_(product_ids))).all()
        assert products
        assert all((product.gender or "").lower() not in {"women", "female", "girls"} for product in products)


def test_recommendations_surface_occasion_match_reasons(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)

        response = mcp_server.fashion_store_associate_recommend(
            store_id="1001",
            customer_id="cust_000001",
            occasion="workwear",
            retrieval_mode=RetrievalMode.fast,
            top_k=6,
        )

        recs = response.recommendation.recommendations
        assert recs
        assert any(
            any("matched workwear occasion" in reason.lower() for reason in item.reasons)
            for item in recs
        )


def test_recommendations_exclude_out_of_stock_and_keep_preorder(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)

        response = mcp_server.fashion_store_associate_recommend(
            store_id="1001",
            customer_id="cust_000001",
            retrieval_mode=RetrievalMode.fast,
            top_k=10,
        )

        recs = response.recommendation.recommendations
        rec_ids = {item.product_id for item in recs}
        assert "prod_oos_1" not in rec_ids
        assert "prod_pre_1" in rec_ids

        preorder_row = next(item for item in recs if item.product_id == "prod_pre_1")
        assert preorder_row.availability.lower() == "preorder"
        assert any("preorder" in reason.lower() for reason in preorder_row.reasons)


def test_semantic_recommendations_exclude_out_of_stock_and_keep_preorder(monkeypatch):
    import app.services.recommendations as recommendations

    class _FakeEmbeddingService:
        def embed_text(self, text):  # pragma: no cover - deterministic test double
            return [0.1, 0.2, 0.3]

    class _FakePineconeService:
        enabled = True

        def query(self, namespace, vector, top_k, filters):  # pragma: no cover - deterministic test double
            return [
                {"id": "product:prod_oos_1", "score": 0.99, "metadata": {"product_id": "prod_oos_1"}},
                {"id": "product:prod_pre_1", "score": 0.95, "metadata": {"product_id": "prod_pre_1"}},
                {"id": "product:prod_4", "score": 0.9, "metadata": {"product_id": "prod_4"}},
            ]

    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        monkeypatch.setattr(recommendations, "EmbeddingService", lambda: _FakeEmbeddingService())
        monkeypatch.setattr(recommendations, "PineconeService", lambda: _FakePineconeService())

        response = mcp_server.fashion_store_associate_recommend(
            store_id="1001",
            customer_id="cust_000001",
            retrieval_mode=RetrievalMode.semantic,
            top_k=5,
        )

        recs = response.recommendation.recommendations
        rec_ids = {item.product_id for item in recs}
        assert response.recommendation.strategy == "hybrid_vector_rules"
        assert "prod_oos_1" not in rec_ids
        assert "prod_pre_1" in rec_ids


def test_demo_customer_uses_sex_fallback_for_filtering(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        session.add(
            Customer(
                id=DEMO_CUSTOMER_ID,
                seed_run_id="run_test",
                home_store_id="1001",
                first_name="Demo",
                last_name="User",
                email="demo.user.sex-fallback@example-fashion.test",
                phone_e164="+12145559999",
                city="Dallas",
                state="TX",
                joined_at=datetime.now(timezone.utc) - timedelta(days=90),
                loyalty_tier="gold",
                sex=None,
                price_sensitivity=Decimal("0.4100"),
                occasion_affinity={"wedding": 0.8},
                style_vector={"womens_apparel": 0.99, "mens_apparel": 0.5, "beauty": 0.7},
                size_preferences={"top": "M"},
                channel_preference="hybrid",
                pii_token="demo-sex-fallback",
            )
        )
        session.commit()

        response = mcp_server.fashion_store_associate_recommend(
            store_id="1001",
            customer_id=DEMO_CUSTOMER_ID,
            retrieval_mode=RetrievalMode.fast,
            top_k=5,
        )

        recs = response.recommendation.recommendations
        assert recs
        product_ids = [item.product_id for item in recs]
        products = session.scalars(select(Product).where(Product.id.in_(product_ids))).all()
        assert products
        assert all((product.gender or "").lower() not in {"women", "female", "girls"} for product in products)


def test_style_constraints_relaxation_stage_is_reported(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)

        response = mcp_server.fashion_store_associate_recommend(
            store_id="1001",
            customer_id="cust_000001",
            retrieval_mode=RetrievalMode.fast,
            top_k=5,
            style_constraints=StyleConstraints(
                constraint_source="chat_image",
                target_categories=["shoes"],
                target_genders=["female"],
                style_keywords=["tailored", "modern"],
            ),
        )

        assert response.recommendation.recommendations
        assert response.recommendation.applied_style_constraints is not None
        assert response.recommendation.constraint_source == "chat_image"
        assert response.recommendation.constraint_stage == "relaxed_drop_target_genders"
        product_ids = [item.product_id for item in response.recommendation.recommendations]
        products = session.scalars(select(Product).where(Product.id.in_(product_ids))).all()
        assert all((product.gender or "").lower() not in {"women", "female", "girls"} for product in products)


def test_prepare_update_send_history_and_smoke(monkeypatch):
    import app.services.communications as communications

    with _patched_runtime(monkeypatch) as (session, _):
        _seed_data(session)
        monkeypatch.setattr(communications.TwilioService, "send_sms", lambda self, body, to_number=None: {"sid": "SM123"})
        monkeypatch.setattr(
            communications.SesEmailService,
            "send_email",
            lambda self, to_email, subject, text_body, html_body=None: {"message_id": "SES123"},
        )

        draft = prepare_customer_sms(
            session,
            store_id="1001",
            customer_phone_e164="+12145551234",
            occasion="wedding",
            budget_max=900,
            top_k=3,
            selected_product_ids=["prod_2", "prod_1"],
        )
        updated = update_customer_sms_draft(
            session,
            message_id=draft.message.id,
            body_text="Curated picks just for you.",
            selected_product_ids=["prod_1", "prod_2"],
        )
        sent = send_customer_sms(session, draft.message.id)
        email_sent = send_customer_recommendations_email(
            session,
            store_id="1001",
            customer_id="cust_000001",
            selected_product_ids=["prod_1", "prod_2"],
            to_email="buyer@example.com",
            subject="Curated picks for you",
        )
        history = customer_message_history(session, customer_id="cust_000001", status=sent.status)
        smoke = twilio_smoke_test(session, body_text="Smoke test")

        assert "https://fashion.example/products/prod_2" in draft.message.body_text
        assert draft.message.product_ids == ["prod_2", "prod_1"]
        assert updated.message.product_ids == ["prod_1", "prod_2"]
        assert updated.message.body_text == "Curated picks just for you."
        assert sent.status.value == "sent"
        assert sent.twilio_message_sid == "SM123"
        assert email_sent.message.status.value == "sent"
        assert email_sent.message.channel == "email"
        assert email_sent.message.subject == "Curated picks for you"
        assert email_sent.destination_email == "buyer@example.com"
        assert email_sent.provider_message_id == "SES123"
        assert len(history.messages) == 2
        assert smoke.result.status.value == "sent"


def test_email_draft_lifecycle_service(monkeypatch):
    import app.services.communications as communications

    with _patched_runtime(monkeypatch) as (session, _):
        _seed_data(session)
        monkeypatch.setattr(
            communications.SesEmailService,
            "send_email",
            lambda self, to_email, subject, text_body, html_body=None: {"message_id": "SES_DRAFT_123"},
        )

        draft = prepare_customer_email_draft(
            session,
            store_id="1001",
            customer_id="cust_000001",
            selected_product_ids=["prod_4"],
            to_email="draft@example.com",
            subject="Initial Draft Subject",
        )
        updated = update_customer_email_draft(
            session,
            message_id=draft.message.id,
            subject="Updated Draft Subject",
            body_text="Updated draft body",
            to_email="final@example.com",
            selected_product_ids=["prod_4", "prod_2"],
        )
        fetched = get_customer_email_draft(session, draft.message.id)
        sent = send_customer_email_draft(session, draft.message.id)

        assert draft.message.channel == "email"
        assert draft.message.status.value == "draft"
        assert draft.destination_email == "draft@example.com"
        assert draft.message.subject == "Initial Draft Subject"
        assert updated.message.subject == "Updated Draft Subject"
        assert updated.message.body_text == "Updated draft body"
        assert updated.destination_email == "final@example.com"
        assert updated.message.product_ids == ["prod_4", "prod_2"]
        assert fetched.message.id == draft.message.id
        assert fetched.message.body_text == "Updated draft body"
        assert sent.message.status.value == "sent"
        assert sent.destination_email == "final@example.com"
        assert sent.subject == "Updated Draft Subject"
        assert sent.provider_message_id == "SES_DRAFT_123"


def test_merchandising_supports_expanded_slices(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, _):
        _seed_data(session)

        actions = merchandising_action_recommendations(
            session,
            store_query="Dallas",
            question="What should we feature for wedding shoppers under $1000?",
            objective=Objective.margin,
            category="womens_apparel",
            price_band=PriceBand.band_500_1000,
            occasion="wedding",
            compare_mode=CompareMode.peer_and_prior_period,
            peer_mode=PeerMode.profile_type,
            top_k=6,
        )
        diagnostics = merchandising_diagnostics(
            session,
            store_query="Dallas",
            question="Why is womens apparel moving here?",
            category="womens_apparel",
            compare_mode=CompareMode.peer_and_prior_period,
            peer_mode=PeerMode.profile_type,
            compare_store_id="1002",
        )
        trends = merchandising_trend_summary(
            session,
            store_query="Dallas",
            question="Summarize recent womens apparel trends",
            category="womens_apparel",
            compare_mode=CompareMode.peer_and_prior_period,
            peer_mode=PeerMode.profile_type,
            compare_store_id="1002",
        )

        assert actions.category == "womens_apparel"
        assert actions.price_band == PriceBand.band_500_1000
        assert actions.compare_mode == CompareMode.peer_and_prior_period
        assert actions.peer_mode == PeerMode.profile_type
        assert actions.recommendations
        assert all(item.category == "womens_apparel" for item in actions.recommendations)
        assert all(item.image_url for item in actions.recommendations)
        assert diagnostics.insights
        assert trends.highlights
        assert diagnostics.compare_store_id == "1002"
        assert trends.compare_store_id == "1002"
        assert trends.time_series


def test_merch_action_recommendations_do_not_repeat_products_across_actions(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, _):
        _seed_data(session)

        actions = merchandising_action_recommendations(
            session,
            store_query="Dallas",
            objective=Objective.margin,
            compare_mode=CompareMode.peer_and_prior_period,
            top_k=9,
        )

        product_ids = [item.product_id for item in actions.recommendations]
        assert len(product_ids) == len(set(product_ids))
        assert any(item.action.value == "feature" for item in actions.recommendations)
        assert any("full-price" in item.rationale for item in actions.recommendations if item.action.value == "feature")
        promote_items = [item for item in actions.recommendations if item.action.value == "promote"]
        if promote_items:
            assert any("campaign or offer" in item.rationale for item in promote_items)


def test_merch_compare_store_overrides_peer_set(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, _):
        _seed_data(session)

        actions = merchandising_action_recommendations(
            session,
            store_query="Dallas",
            objective=Objective.margin,
            compare_mode=CompareMode.peer_and_prior_period,
            compare_store_id="1002",
            top_k=6,
        )
        diagnostics = merchandising_diagnostics(
            session,
            store_query="Dallas",
            compare_mode=CompareMode.peer_and_prior_period,
            compare_store_id="1002",
        )
        trends = merchandising_trend_summary(
            session,
            store_query="Dallas",
            compare_mode=CompareMode.peer_and_prior_period,
            compare_store_id="1002",
        )

        assert actions.compare_store_id == "1002"
        assert diagnostics.compare_store_id == "1002"
        assert trends.compare_store_id == "1002"
        assert actions.peer_store_ids == ["1002"]
        assert diagnostics.peer_store_ids == ["1002"]
        assert trends.peer_store_ids == ["1002"]


def test_merch_compare_store_supports_multiple_explicit_peers(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, _):
        _seed_data(session)
        session.add(
            Store(
                id="1003",
                seed_run_id="run_test",
                name="Houston Heights",
                city="Houston",
                state="TX",
                postal_code="77008",
                address_line1="3 Heights Blvd",
                address_line2=None,
                phone="555-333-3333",
                latitude=Decimal("29.790000"),
                longitude=Decimal("-95.400000"),
                profile_type="texas_core",
                services=["Styling"],
                raw_source={},
            )
        )
        session.commit()

        actions = merchandising_action_recommendations(
            session,
            store_query="Dallas",
            objective=Objective.margin,
            compare_mode=CompareMode.peer_and_prior_period,
            compare_store_id="1002, 1003",
            top_k=6,
        )
        diagnostics = merchandising_diagnostics(
            session,
            store_query="Dallas",
            compare_mode=CompareMode.peer_and_prior_period,
            compare_store_id="1002,1003",
        )
        trends = merchandising_trend_summary(
            session,
            store_query="Dallas",
            compare_mode=CompareMode.peer_and_prior_period,
            compare_store_id="1002,1003",
        )

        assert actions.compare_store_id == "1002"
        assert diagnostics.compare_store_id == "1002"
        assert trends.compare_store_id == "1002"
        assert actions.peer_store_ids == ["1002", "1003"]
        assert diagnostics.peer_store_ids == ["1002", "1003"]
        assert trends.peer_store_ids == ["1002", "1003"]


def test_product_margin_sales_opportunities_supports_enterprise_and_store_scope(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        now = datetime(2026, 3, 14, tzinfo=timezone.utc)
        session.add(
            Order(
                id="order_prior_decline",
                seed_run_id="run_test",
                customer_id="cust_000001",
                store_id="1001",
                ordered_at=now - timedelta(days=95),
                status="completed",
                occasion="workwear",
                channel="in_store",
                subtotal=Decimal("820.00"),
                discount_amount=Decimal("0.00"),
                tax_amount=Decimal("65.00"),
                total_amount=Decimal("885.00"),
                returned=False,
            )
        )
        session.add(
            OrderItem(
                id="item_prior_decline",
                order_id="order_prior_decline",
                product_id="prod_4",
                quantity=1,
                unit_price=Decimal("820.00"),
                discount_amount=Decimal("0.00"),
                line_total=Decimal("820.00"),
            )
        )
        session.commit()

        enterprise = mcp_server.fashion_product_margin_sales_opportunities(
            dimension=ProductPerformanceDimension.product,
            lookback_days=90,
            min_margin_rate=0.50,
            min_revenue_drop_pct=10.0,
            top_k=10,
        )
        store_scoped = mcp_server.fashion_product_margin_sales_opportunities(
            dimension=ProductPerformanceDimension.product,
            store_query="Dallas",
            lookback_days=90,
            min_margin_rate=0.50,
            min_revenue_drop_pct=10.0,
            top_k=10,
        )
        brand_scoped = mcp_server.fashion_product_margin_sales_opportunities(
            dimension=ProductPerformanceDimension.brand,
            store_query="Dallas",
            lookback_days=90,
            min_margin_rate=0.50,
            min_revenue_drop_pct=10.0,
            top_k=10,
        )

        assert enterprise.rows
        assert any(row.product_id == "prod_4" for row in enterprise.rows)
        assert store_scoped.scope_label == "Dallas Downtown"
        assert any(row.product_id == "prod_4" for row in store_scoped.rows)
        assert brand_scoped.rows
        assert any(row.brand == "Tom Ford" for row in brand_scoped.rows)
        assert all(row.margin_rate >= 0.50 for row in enterprise.rows)
        assert all((row.revenue_delta_pct or 0.0) <= -10.0 for row in enterprise.rows)

        with pytest.raises(ValidationError):
            mcp_server.fashion_product_margin_sales_opportunities(
                lookback_days=7,
            )


def test_exec_auto_optimize_strategy_is_deterministic(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, _):
        _seed_data(session)

        first = auto_optimize_strategy(
            session,
            store_id="1001",
            lookback_days=90,
            objective=Objective.revenue,
            from_category="womens_apparel",
            to_category="shoes",
            discount_min_pct=0.0,
            discount_max_pct=20.0,
            discount_step_pct=10.0,
            shift_min_pct=0.0,
            shift_max_pct=20.0,
            shift_step_pct=10.0,
            top_k_scenarios=3,
            min_margin_rate=0.30,
            max_discount_pct=25.0,
        )
        second = auto_optimize_strategy(
            session,
            store_id="1001",
            lookback_days=90,
            objective=Objective.revenue,
            from_category="womens_apparel",
            to_category="shoes",
            discount_min_pct=0.0,
            discount_max_pct=20.0,
            discount_step_pct=10.0,
            shift_min_pct=0.0,
            shift_max_pct=20.0,
            shift_step_pct=10.0,
            top_k_scenarios=3,
            min_margin_rate=0.30,
            max_discount_pct=25.0,
        )

        assert first.scenarios
        assert second.scenarios
        assert first.scope_store_ids == ["1001"]
        assert [(row.scenario_id, row.objective_score) for row in first.scenarios] == [
            (row.scenario_id, row.objective_score) for row in second.scenarios
        ]
        assert [row.guardrail_passed for row in first.scenarios] == [row.guardrail_passed for row in second.scenarios]


def test_exec_strategy_packet_lifecycle_and_email_gate(monkeypatch):
    import app.services.executive as executive_service

    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        _set_strategy_flags(monkeypatch, mcp_server, exec_auto=True, packet=True)
        monkeypatch.setattr(
            executive_service.SesEmailService,
            "send_email",
            lambda self, to_email, subject, text_body, html_body=None: {"message_id": "SES_STRAT_001"},
        )

        optimize = mcp_server.fashion_exec_auto_optimize_strategy(
            store_id="1001",
            lookback_days=90,
            objective=Objective.revenue,
            from_category="womens_apparel",
            to_category="shoes",
            discount_min_pct=0.0,
            discount_max_pct=20.0,
            discount_step_pct=10.0,
            shift_min_pct=0.0,
            shift_max_pct=20.0,
            shift_step_pct=10.0,
            top_k_scenarios=3,
            min_margin_rate=0.30,
            max_discount_pct=25.0,
        )
        assert optimize.scenarios
        chosen = optimize.scenarios[0]

        packet = mcp_server.fashion_exec_publish_strategy_packet(
            scenario=chosen,
            objective=Objective.revenue,
            lookback_days=90,
            store_id="1001",
            brands=["Valentino"],
            from_category="womens_apparel",
            to_category="shoes",
            min_margin_rate=0.30,
            max_discount_pct=25.0,
            title="Demo packet",
            summary="Apply to Dallas first.",
        )
        assert packet.packet_id.startswith("stratpkt_")
        assert packet.status.value == "published"
        assert packet.scope_store_ids == ["1001"]
        assert packet.strategy_core.category == "shoes"
        assert packet.strategy_core.brands == ["valentino"]
        assert packet.tag_intensity.value == "medium"

        draft = mcp_server.fashion_exec_prepare_strategy_packet_email(
            packet_id=packet.packet_id,
            to_email="manager@example.com",
        )
        assert draft.packet_id == packet.packet_id
        assert draft.email_status.value == "draft"
        assert draft.to_email == "manager@example.com"
        assert "Strategy Packet" in draft.subject

        with pytest.raises(ValueError):
            mcp_server.fashion_exec_send_strategy_packet_email(packet_id=packet.packet_id, approved=False)

        sent = mcp_server.fashion_exec_send_strategy_packet_email(packet_id=packet.packet_id, approved=True)
        assert sent.packet_id == packet.packet_id
        assert sent.email_status.value == "sent"
        assert sent.provider_message_id == "SES_STRAT_001"

        fetched = mcp_server.fashion_exec_get_strategy_packet(packet.packet_id)
        assert fetched.email_status.value == "sent"
        assert fetched.provider_message_id == "SES_STRAT_001"


def test_exec_strategy_packet_enforces_feature_flags(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        _set_strategy_flags(monkeypatch, mcp_server, exec_auto=False, packet=False)

        with pytest.raises(ValueError):
            mcp_server.fashion_exec_auto_optimize_strategy(store_id="1001")

        with pytest.raises(ValueError):
            mcp_server.fashion_exec_get_strategy_packet(packet_id="stratpkt_missing")

        with pytest.raises(ValueError):
            mcp_server.fashion_merch_get_effective_strategy(store_id="1001")


def test_merch_workspace_hydrates_strategy_context_from_packet(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        _set_strategy_flags(monkeypatch, mcp_server, exec_auto=True, packet=True, merch_context=True)

        optimize = mcp_server.fashion_exec_auto_optimize_strategy(
            store_id="1001",
            lookback_days=180,
            objective=Objective.revenue,
            from_category="womens_apparel",
            to_category="shoes",
            top_k_scenarios=2,
            min_margin_rate=0.30,
            max_discount_pct=25.0,
        )
        packet = mcp_server.fashion_exec_publish_strategy_packet(
            scenario=optimize.scenarios[0],
            objective=Objective.revenue,
            lookback_days=180,
            store_id="1001",
            brands=["Valentino", "Jimmy Choo"],
            from_category="womens_apparel",
            to_category="shoes",
            min_margin_rate=0.30,
            max_discount_pct=25.0,
        )

        workspace = mcp_server.fashion_render_merch_workspace(
            store_id="1001",
            objective=Objective.margin,
            lookback_days=90,
            strategy_packet_id=packet.packet_id,
        )
        payload = workspace.structuredContent["payload"]

        assert payload["strategy_context"]["packet_id"] == packet.packet_id
        assert payload["filters"]["objective"] == "revenue"
        assert payload["filters"]["lookback_days"] == 180
        assert payload["filters"]["category"] == "shoes"
        assert payload["filters"]["brand"] == "valentino, jimmy choo"
        assert payload["strategy_context"]["strategy_core"]["category"] == "shoes"
        assert payload["strategy_context"]["effective_tag_intensity"] == "medium"
        assert payload["uiHints"]["features"]["merchStrategyContextEnabled"] is True


def test_merch_strategy_override_persists_and_hydrates_effective_strategy(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        _set_strategy_flags(monkeypatch, mcp_server, exec_auto=True, packet=True, merch_context=True)

        optimize = mcp_server.fashion_exec_auto_optimize_strategy(
            store_id="1001",
            lookback_days=180,
            objective=Objective.revenue,
            from_category="womens_apparel",
            to_category="shoes",
            top_k_scenarios=2,
            min_margin_rate=0.30,
            max_discount_pct=25.0,
        )
        packet = mcp_server.fashion_exec_publish_strategy_packet(
            scenario=optimize.scenarios[0],
            objective=Objective.revenue,
            lookback_days=180,
            store_id="1001",
            brands=["Jimmy Choo"],
            from_category="womens_apparel",
            to_category="shoes",
            min_margin_rate=0.30,
            max_discount_pct=25.0,
        )

        baseline = mcp_server.fashion_merch_get_effective_strategy(store_id="1001", strategy_packet_id=packet.packet_id)
        assert baseline.source == "packet"
        assert baseline.override_active is False
        assert baseline.tag_intensity.value == "medium"

        updated = mcp_server.fashion_merch_save_strategy_override(
            packet_id=packet.packet_id,
            store_id="1001",
            strategy_core={
                "objective": "revenue",
                "lookback_days": 180,
                "category": "mens_apparel",
                "brands": ["Tom Ford"],
                "discount_pct": 12.0,
                "floor_space_shift_pct": 5.0,
                "min_margin_rate": 0.45,
                "max_discount_pct": 20.0,
            },
            tag_intensity="high",
        )
        assert updated.source == "override"
        assert updated.override_active is True
        assert updated.strategy_core is not None
        assert updated.strategy_core.category == "mens_apparel"
        assert updated.tag_intensity.value == "high"

        workspace = mcp_server.fashion_render_merch_workspace(
            store_id="1001",
            objective=Objective.margin,
            lookback_days=90,
            strategy_packet_id=packet.packet_id,
        )
        payload = workspace.structuredContent["payload"]
        assert payload["filters"]["objective"] == "revenue"
        assert payload["filters"]["category"] == "mens_apparel"
        assert payload["strategy_context"]["override_active"] is True
        assert payload["strategy_context"]["effective_tag_intensity"] == "high"

        reset = mcp_server.fashion_merch_save_strategy_override(
            packet_id=packet.packet_id,
            store_id="1001",
            use_packet_defaults=True,
        )
        assert reset.source == "packet"
        assert reset.override_active is False
        assert reset.tag_intensity.value == "medium"


def test_associate_recommendations_apply_priority_tags_when_enabled(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        _set_strategy_flags(monkeypatch, mcp_server, exec_auto=True, packet=True, associate_tags=True)

        optimize = mcp_server.fashion_exec_auto_optimize_strategy(
            store_id="1001",
            lookback_days=180,
            objective=Objective.revenue,
            from_category="womens_apparel",
            to_category="shoes",
            top_k_scenarios=2,
            min_margin_rate=0.30,
            max_discount_pct=25.0,
        )
        packet = mcp_server.fashion_exec_publish_strategy_packet(
            scenario=optimize.scenarios[0],
            objective=Objective.revenue,
            lookback_days=180,
            store_id="1001",
            brands=["Valentino", "Jimmy Choo"],
            from_category="womens_apparel",
            to_category="shoes",
            min_margin_rate=0.30,
            max_discount_pct=25.0,
        )

        response = mcp_server.fashion_store_associate_recommend(
            store_id="1001",
            customer_id="cust_000001",
            retrieval_mode=RetrievalMode.fast,
            top_k=6,
        )

        assert response.recommendation.strategy_packet_id == packet.packet_id
        assert response.recommendation.strategy_tag_intensity is not None
        assert response.recommendation.recommendations
        allowed_tags = {"Focus This Week", "Margin Priority", "Campaign Assist"}
        assert any(item.execution_tags for item in response.recommendation.recommendations)
        assert all(set(item.execution_tags).issubset(allowed_tags) for item in response.recommendation.recommendations)


def test_strategy_tag_intensity_changes_tag_coverage(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        _set_strategy_flags(monkeypatch, mcp_server, exec_auto=True, packet=True, merch_context=True, associate_tags=True)

        optimize = mcp_server.fashion_exec_auto_optimize_strategy(
            store_id="1001",
            lookback_days=180,
            objective=Objective.revenue,
            from_category="womens_apparel",
            to_category="shoes",
            top_k_scenarios=2,
            min_margin_rate=0.50,
            max_discount_pct=25.0,
        )
        packet = mcp_server.fashion_exec_publish_strategy_packet(
            scenario=optimize.scenarios[0],
            objective=Objective.revenue,
            lookback_days=180,
            store_id="1001",
            brands=["Jimmy Choo"],
            from_category="womens_apparel",
            to_category="shoes",
            min_margin_rate=0.50,
            max_discount_pct=25.0,
        )

        mcp_server.fashion_merch_save_strategy_override(
            packet_id=packet.packet_id,
            store_id="1001",
            strategy_core={
                "objective": "revenue",
                "lookback_days": 180,
                "category": "shoes",
                "brands": ["Jimmy Choo"],
                "discount_pct": 10.0,
                "floor_space_shift_pct": 0.0,
                "min_margin_rate": 0.50,
                "max_discount_pct": 20.0,
            },
            tag_intensity="low",
        )
        low_rows = [
            ProductRecommendation(
                product_id="prod_2",
                title="Jimmy Choo Satin Pump",
                brand="Jimmy Choo",
                category="shoes",
                price=595.0,
                availability="in stock",
                score=0.93,
                reasons=["High affinity match"],
            ),
            ProductRecommendation(
                product_id="prod_4",
                title="Tom Ford Midnight Sport Coat",
                brand="Tom Ford",
                category="mens_apparel",
                price=820.0,
                availability="in stock",
                score=0.84,
                reasons=["Cross-sell option"],
            ),
        ]
        _, low_intensity, low_tagged = apply_execution_tags_for_store(session, store_id="1001", recommendations=low_rows)

        mcp_server.fashion_merch_save_strategy_override(
            packet_id=packet.packet_id,
            store_id="1001",
            strategy_core={
                "objective": "revenue",
                "lookback_days": 180,
                "category": "shoes",
                "brands": ["Jimmy Choo"],
                "discount_pct": 10.0,
                "floor_space_shift_pct": 0.0,
                "min_margin_rate": 0.50,
                "max_discount_pct": 20.0,
            },
            tag_intensity="high",
        )
        high_rows = [
            ProductRecommendation(
                product_id="prod_2",
                title="Jimmy Choo Satin Pump",
                brand="Jimmy Choo",
                category="shoes",
                price=595.0,
                availability="in stock",
                score=0.93,
                reasons=["High affinity match"],
            ),
            ProductRecommendation(
                product_id="prod_4",
                title="Tom Ford Midnight Sport Coat",
                brand="Tom Ford",
                category="mens_apparel",
                price=820.0,
                availability="in stock",
                score=0.84,
                reasons=["Cross-sell option"],
            ),
        ]
        _, high_intensity, high_tagged = apply_execution_tags_for_store(session, store_id="1001", recommendations=high_rows)

        assert low_intensity is not None and low_intensity.value == "low"
        assert high_intensity is not None and high_intensity.value == "high"
        low_count = sum(len(item.execution_tags) for item in low_tagged)
        high_count = sum(len(item.execution_tags) for item in high_tagged)
        assert high_count >= low_count
        assert "Margin Priority" in high_tagged[0].execution_tags
        assert "Campaign Assist" in high_tagged[1].execution_tags


def test_apply_execution_tags_uses_latest_published_packet(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, _):
        _seed_data(session)

        recommendation_rows = [
            ProductRecommendation(
                product_id="prod_2",
                title="Jimmy Choo Satin Pump",
                brand="Jimmy Choo",
                category="shoes",
                price=595.0,
                availability="in stock",
                score=0.93,
                reasons=["High affinity match"],
            )
        ]

        # no packet -> no tags
        packet_id, intensity, tagged = apply_execution_tags_for_store(
            session,
            store_id="1001",
            recommendations=recommendation_rows,
        )
        assert packet_id is None
        assert intensity is None
        assert tagged[0].execution_tags == []

        first = ExecutiveStrategyPacket(
            id="stratpkt_old",
            status="published",
            title="Old",
            summary="Old packet",
            payload_json={
                "objective": "revenue",
                "lookback_days": 90,
                "scope_label": "Dallas Downtown",
                "scope_store_ids": ["1001"],
                "brands": ["valentino"],
                "from_category": "womens_apparel",
                "to_category": "womens_apparel",
                "min_margin_rate": 0.20,
                "max_discount_pct": 20.0,
                "scenario": {
                    "scenario_id": "opt_001",
                    "discount_pct": 5.0,
                    "floor_space_shift_pct": 5.0,
                    "from_category": "womens_apparel",
                    "to_category": "womens_apparel",
                    "expected_revenue": 1000.0,
                    "expected_margin_rate": 0.5,
                    "revenue_delta": 100.0,
                    "margin_rate_delta": 0.01,
                    "confidence_interval_low": 900.0,
                    "confidence_interval_high": 1100.0,
                    "objective_score": 100.0,
                    "guardrail_passed": True,
                    "guardrail_reasons": [],
                    "rationale": "Old scenario",
                },
            },
            email_status="draft",
            created_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        )
        second = ExecutiveStrategyPacket(
            id="stratpkt_new",
            status="published",
            title="New",
            summary="New packet",
            payload_json={
                "objective": "revenue",
                "lookback_days": 90,
                "scope_label": "Dallas Downtown",
                "scope_store_ids": ["1001"],
                "brands": ["jimmy choo"],
                "from_category": "womens_apparel",
                "to_category": "shoes",
                "min_margin_rate": 0.20,
                "max_discount_pct": 20.0,
                "scenario": {
                    "scenario_id": "opt_002",
                    "discount_pct": 10.0,
                    "floor_space_shift_pct": 8.0,
                    "from_category": "womens_apparel",
                    "to_category": "shoes",
                    "expected_revenue": 1100.0,
                    "expected_margin_rate": 0.5,
                    "revenue_delta": 110.0,
                    "margin_rate_delta": 0.01,
                    "confidence_interval_low": 950.0,
                    "confidence_interval_high": 1200.0,
                    "objective_score": 110.0,
                    "guardrail_passed": True,
                    "guardrail_reasons": [],
                    "rationale": "New scenario",
                },
            },
            email_status="draft",
            created_at=datetime(2026, 3, 2, tzinfo=timezone.utc),
            updated_at=datetime(2026, 3, 2, tzinfo=timezone.utc),
        )
        session.add_all([first, second])
        session.commit()

        packet_id, intensity, tagged = apply_execution_tags_for_store(
            session,
            store_id="1001",
            recommendations=recommendation_rows,
        )
        assert packet_id == "stratpkt_new"
        assert intensity is not None
        assert tagged[0].execution_tags


def test_render_customer_search_workspace_returns_template_and_payload(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        seeded_recommendation = mcp_server.fashion_store_associate_recommend(
            store_id="1001",
            customer_id="cust_000001",
            top_k=3,
            retrieval_mode=RetrievalMode.semantic,
        )
        result = mcp_server.fashion_render_customer_search_workspace(
            query="avery.parker.1@example-fashion.test",
            limit=10,
            selected_customer_id="cust_000001",
            initial_style_constraints=StyleConstraints(
                constraint_source="chat_image",
                target_categories=["mens_apparel"],
                target_genders=["male"],
                style_keywords=["tailored", "minimal"],
            ),
            initial_notice="Image guidance loaded from this chat turn.",
            initial_email_draft_id="msg_demo_draft_001",
            initial_email_subject="A few picks from your stylist",
            initial_email_body="Hi Avery,\n\nHere are a few tailored options.",
            initial_recommendation_response=seeded_recommendation,
            initial_selected_product_ids=["prod_a", "prod_b", "prod_a"],
        )
        html = mcp_server.customer_search_widget_resource()
        template_uri = result.meta["openai/outputTemplate"]

        assert template_uri.startswith("ui://widgets/customer-search/workspace-")
        assert template_uri.endswith(".html")
        assert result.meta["ui"]["resourceUri"] == template_uri
        assert result.structuredContent["kind"] == "customer_search_workspace"
        assert result.structuredContent["payload"]["query"] == "avery.parker.1@example-fashion.test"
        assert result.structuredContent["payload"]["mode"] == "resolved"
        assert result.structuredContent["payload"]["resolved"]["id"] == "cust_000001"
        assert result.structuredContent["payload"]["results"][0]["id"] == "cust_000001"
        assert result.structuredContent["payload"]["selected_customer_id"] == "cust_000001"
        assert result.structuredContent["payload"]["initial_notice"] == "Image guidance loaded from this chat turn."
        assert result.structuredContent["payload"]["initial_style_constraints"]["constraint_source"] == "chat_image"
        assert result.structuredContent["payload"]["initial_style_constraints"]["target_categories"] == ["mens_apparel"]
        assert result.structuredContent["payload"]["initial_email_draft_id"] == "msg_demo_draft_001"
        assert result.structuredContent["payload"]["initial_email_subject"] == "A few picks from your stylist"
        assert result.structuredContent["payload"]["initial_email_body"] == "Hi Avery,\n\nHere are a few tailored options."
        assert result.structuredContent["payload"]["initial_recommendation_response"]["customer"]["id"] == "cust_000001"
        assert result.structuredContent["payload"]["initial_recommendation_response"]["store"]["id"] == "1001"
        assert result.structuredContent["payload"]["initial_selected_product_ids"] == ["prod_a", "prod_b"]
        assert "<style>" in html
        assert "Customer Workspace" in html
        assert "window.__FASHION_WIDGET__" in html
        assert "chart.umd.min.js" in html


def test_render_merch_workspace_returns_template_and_payload(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        result = mcp_server.fashion_render_merch_workspace(
            store_query="Dallas",
            question="What should we feature this week?",
            objective=Objective.margin,
            top_k=6,
            compare_mode=CompareMode.peer_and_prior_period,
            peer_mode=PeerMode.profile_type,
        )
        html = mcp_server.merch_workspace_resource()
        template_uri = result.meta["openai/outputTemplate"]

        assert template_uri.startswith("ui://widgets/merch/workspace-")
        assert template_uri.endswith(".html")
        assert result.meta["ui"]["resourceUri"] == template_uri
        assert result.structuredContent["kind"] == "merch_workspace"
        payload = result.structuredContent["payload"]
        assert payload["store"]["id"] == "1001"
        assert payload["filters"]["objective"] == "margin"
        assert payload["filters"]["lookback_days"] == 90
        assert payload["filters"]["top_k"] == 6
        assert payload["filters"]["compare_mode"] == "peer_and_prior_period"
        assert payload["filters"]["peer_mode"] == "profile_type"
        assert payload["filters"]["occasion"] is None
        assert payload["uiHints"]["categoryOptions"]
        assert payload["uiHints"]["brandOptions"]
        assert payload["initial_result"]["rows"]
        assert payload["inventory_check"]["current_store"]["store_id"] == "1001"
        assert payload["inventory_products"]["rows"]
        assert payload["last_tool"] == "fashion_merch_inventory_view"
        assert "<style>" in html
        assert "Merchandising Workspace" in html
        assert "window.__FASHION_WIDGET__" in html


def test_open_merch_workspace_orchestrates_store_resolution(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        result = mcp_server.fashion_open_merch_workspace(
            store_query="Dallas",
            question="Why are shoes underperforming here?",
            compare_mode=CompareMode.peer_and_prior_period,
        )

        payload = result.structuredContent["payload"]
        assert result.structuredContent["kind"] == "unified_workspace"
        assert payload["active_view"] == "inventory"
        assert payload["filters"]["active_store_id"] == "1001"
        assert "Resolved store Dallas Downtown from 'Dallas'." == payload["initial_notice"]


def test_open_merch_workspace_defaults_to_all_stores_when_no_store(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        result = mcp_server.fashion_open_merch_workspace()
        payload = result.structuredContent["payload"]

        assert result.structuredContent["kind"] == "unified_workspace"
        assert payload["active_view"] == "inventory"
        assert payload["filters"]["store_ids"] == ["1001", "1002"]
        assert payload["initial_notice"] == "Opened merchandising workspace across all stores. Use Store Search + Stores selector to focus scope."


def test_open_exec_workspace_wraps_to_unified(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        result = mcp_server.fashion_open_exec_workspace(lookback_days=56, top_k_stores=8)
        payload = result.structuredContent["payload"]

        assert result.structuredContent["kind"] == "unified_workspace"
        assert payload["active_view"] == "executive_overview"
        assert payload["filters"]["lookback_days"] == 56
        assert payload["last_tool"] == "fashion_unified_overview"


def test_unified_core_tabs_and_export_parity(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)

        opened = mcp_server.fashion_open_unified_workspace(
            store_ids=["1001", "1002"],
            active_store_id="1001",
            initial_view="inventory",
            row_mode="store_product",
            lookback_days=90,
            top_k=10,
        )
        assert opened.structuredContent["kind"] == "unified_workspace"
        assert opened.structuredContent["payload"]["active_view"] == "inventory"
        assert opened.structuredContent["payload"]["filters"]["store_ids"] == ["1001", "1002"]

        inventory = mcp_server.fashion_unified_inventory_view(
            store_ids=["1001", "1002"],
            active_store_id="1001",
            row_mode="store_product",
            inventory_scope="combined",
            lookback_days=90,
            limit=300,
        )
        assert inventory.rows
        assert all(row.store_id for row in inventory.rows)

        inventory_export = mcp_server.fashion_unified_export_csv(
            view="inventory",
            store_ids=["1001", "1002"],
            active_store_id="1001",
            row_mode="store_product",
            inventory_scope="combined",
            lookback_days=90,
        )
        assert inventory_export.row_count == len(inventory.rows)
        assert all(row.values.get("view") == "inventory" for row in inventory_export.rows)
        assert all(row.values.get("store_id") for row in inventory_export.rows)

        baseline_actions = mcp_server.fashion_unified_action_recommendations(
            store_ids=["1001", "1002"],
            active_store_id="1001",
            row_mode="store_product",
            override_scope="global",
            top_k=6,
            objective="margin",
        )
        assert baseline_actions.recommendations
        target = baseline_actions.recommendations[0]

        overridden = mcp_server.fashion_unified_action_recommendations(
            store_ids=["1001", "1002"],
            active_store_id="1001",
            row_mode="store_product",
            override_scope="global",
            top_k=6,
            objective="margin",
            recommendation_overrides=[
                {
                    "product_id": target.product_id,
                    "final_action": "drop",
                    "priority_tier": "low",
                    "override_note": "global demo override",
                }
            ],
        )
        target_rows = [row for row in overridden.recommendations if row.product_id == target.product_id]
        assert target_rows
        assert any(row.final_action.value == "drop" for row in target_rows)
        assert any(row.final_priority_tier.value == "low" for row in target_rows)

        mix = mcp_server.fashion_unified_product_mix_recommendations(
            store_ids=["1001", "1002"],
            active_store_id="1001",
            row_mode="aggregated",
            override_scope="global",
            inventory_scope="combined",
            top_k=8,
            lookback_days=90,
        )
        assert mix.rows
        assert all((row.store_count or 0) >= 1 for row in mix.rows)


def test_merch_export_csv_supports_all_views(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)

        actions = mcp_server.fashion_merch_export_csv(
            view="actions",
            store_id="1001",
            question="What should we feature for margin?",
            objective=Objective.margin,
            top_k=5,
        )
        diagnostics = mcp_server.fashion_merch_export_csv(
            view="diagnostics",
            store_id="1001",
            question="Why is womens apparel moving here?",
            category="womens_apparel",
        )
        trends = mcp_server.fashion_merch_export_csv(
            view="trends",
            store_id="1001",
            question="Summarize recent womens apparel trends",
            category="womens_apparel",
        )

        assert actions.row_count > 0
        assert actions.csv_text.splitlines()[0].startswith("store_id,store_name,view,compare_store_id,compare_store_name,action,product_id")
        assert actions.view.value == "actions"
        assert "inventory_product_id" in actions.headers
        assert any(row.values.get("export_row_type") == "inventory_product_snapshot" for row in actions.rows)
        assert diagnostics.row_count > 0
        assert diagnostics.csv_text.splitlines()[0].startswith("store_id,store_name,view,compare_store_id,compare_store_name,dimension,subject")
        assert diagnostics.view.value == "diagnostics"
        assert "inventory_product_id" in diagnostics.headers
        assert any(row.values.get("export_row_type") == "inventory_product_snapshot" for row in diagnostics.rows)
        assert trends.row_count > 0
        assert trends.csv_text.splitlines()[0].startswith("store_id,store_name,view,row_type,compare_store_id,compare_store_name,subject,period_start")
        assert trends.view.value == "trends"
        assert "inventory_product_id" in trends.headers
        assert any(row.values.get("export_row_type") == "inventory_product_snapshot" for row in trends.rows)


def test_merch_inventory_view_supports_combined_scope_and_filters(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)

        response = mcp_server.fashion_merch_inventory_view(
            store_id="1001",
            category="womens_apparel",
            brand="Valentino",
            inventory_scope="combined",
            future_window_days=120,
            lookback_days=90,
            limit=100,
        )

        assert response.store.id == "1001"
        assert response.inventory_scope.value == "combined"
        assert response.rows
        assert any(row.row_type.value == "current_inventory" for row in response.rows)
        assert any(row.row_type.value == "potential_offer" for row in response.rows)
        assert all((row.category or "").lower() == "womens_apparel" for row in response.rows)
        assert all((row.brand or "").lower() == "valentino" for row in response.rows)
        assert response.current_rows >= 1
        assert response.potential_rows >= 1


def test_merch_mix_recommendations_are_deterministic_and_include_actions(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)

        baseline_actions = mcp_server.fashion_merch_action_recommendations(store_id="1001", top_k=6)
        assert baseline_actions.recommendations
        override_target = baseline_actions.recommendations[0].product_id
        overrides = [
            {
                "product_id": override_target,
                "final_action": "drop",
                "priority_tier": "low",
                "override_note": "demo override",
            }
        ]

        first = mcp_server.fashion_merch_product_mix_recommendations(
            store_id="1001",
            top_k=8,
            inventory_scope="combined",
            recommendation_overrides=overrides,
        )
        second = mcp_server.fashion_merch_product_mix_recommendations(
            store_id="1001",
            top_k=8,
            inventory_scope="combined",
            recommendation_overrides=overrides,
        )

        assert first.summary
        assert first.rows
        assert [row.model_dump(mode="json") for row in first.rows] == [row.model_dump(mode="json") for row in second.rows]
        actions = {row.action.value for row in first.rows}
        assert actions.issubset({"add", "hold", "reduce", "swap"})
        assert "add" in actions or "swap" in actions


def test_merch_export_view_only_matches_active_inventory_view_and_applies_overrides(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)

        inventory_view = mcp_server.fashion_merch_inventory_view(
            store_id="1001",
            inventory_scope="combined",
            future_window_days=120,
            lookback_days=90,
            limit=200,
        )
        inventory_export = mcp_server.fashion_merch_export_csv(
            view="inventory",
            export_mode="view_only",
            store_id="1001",
            inventory_scope="combined",
            future_window_days=120,
            lookback_days=90,
            top_k=9,
        )

        assert inventory_export.view.value == "inventory"
        assert inventory_export.row_count == len(inventory_view.rows)
        assert "export_row_type" not in inventory_export.headers
        assert all(row.values.get("view") == "inventory" for row in inventory_export.rows)

        baseline_actions = mcp_server.fashion_merch_action_recommendations(store_id="1001", top_k=6)
        assert baseline_actions.recommendations
        target_product = baseline_actions.recommendations[0].product_id
        actions_export = mcp_server.fashion_merch_export_csv(
            view="actions",
            export_mode="view_only",
            store_id="1001",
            top_k=6,
            recommendation_overrides=[
                {
                    "product_id": target_product,
                    "final_action": "drop",
                    "priority_tier": "low",
                    "override_note": "demo flow override",
                }
            ],
        )

        assert "final_action" in actions_export.headers
        assert "final_priority_tier" in actions_export.headers
        assert not any(row.values.get("export_row_type") == "inventory_product_snapshot" for row in actions_export.rows)
        target_rows = [row for row in actions_export.rows if row.values.get("product_id") == target_product]
        assert target_rows
        assert target_rows[0].values.get("final_action") == "drop"
        assert target_rows[0].values.get("final_priority_tier") == "low"
        assert target_rows[0].values.get("override_note") == "demo flow override"


def test_inventory_by_store_and_facets_tools(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)

        by_store = mcp_server.fashion_inventory_by_store(category="shoes")
        assert by_store.rows
        assert by_store.total_units_in_stock > 0
        assert {row.store_id for row in by_store.rows} == {"1001", "1002"}

        facets = mcp_server.fashion_inventory_facets(
            facet="size",
            store_query="Dallas",
            category="womens_apparel",
        )
        assert facets.store is not None
        assert facets.store.id == "1001"

        products = mcp_server.fashion_inventory_products(
            store_id="1001",
            category="womens_apparel",
            limit=20,
        )
        assert products.store is not None
        assert products.store.id == "1001"
        assert products.row_count >= 1
        assert all(row.category == "womens_apparel" for row in products.rows)
        assert all(row.product_id for row in products.rows)
        assert all(row.stock_state in {"in_stock", "preorder", "out_of_stock", "not_in_stock"} for row in products.rows)
        assert facets.rows
        assert facets.total_units_in_stock > 0


def test_inventory_check_by_store_tool(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)

        check = mcp_server.fashion_inventory_check_by_store(category="mens_apparel")
        assert check.rows
        assert check.total_skus > 0
        assert check.total_not_in_stock_skus > 0
        assert check.total_preorder_skus > 0
        assert check.total_out_of_stock_skus > 0
        by_store = {row.store_id: row for row in check.rows}
        assert "1001" in by_store
        assert by_store["1001"].preorder_skus >= 1
        assert by_store["1001"].out_of_stock_skus >= 1
        assert by_store["1001"].not_in_stock_skus >= 2
        assert by_store["1001"].not_in_stock_rate_pct > 0

        product_check = mcp_server.fashion_inventory_check_by_store(product_id="prod_pre_1")
        assert len(product_check.rows) == 1
        assert product_check.rows[0].store_id == "1001"
        assert product_check.rows[0].preorder_skus == 1
        assert product_check.rows[0].out_of_stock_skus == 0


def test_open_customer_workspace_orchestrates_resolution_and_hydration(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        result = mcp_server.fashion_open_customer_workspace(
            customer_query="avery.parker.1@example-fashion.test",
            style_constraints=StyleConstraints(
                constraint_source="chat_image",
                target_categories=["mens_apparel"],
                target_genders=["male"],
                style_keywords=["tailored"],
            ),
            initial_email_draft_id="msg_canvas_001",
            initial_email_subject="Draft from Canvas",
            initial_email_body="Hi Avery,\n\nThis draft started in Canvas.",
            limit=10,
        )

        payload = result.structuredContent["payload"]
        assert result.structuredContent["kind"] == "customer_search_workspace"
        assert payload["selected_customer_id"] == "cust_000001"
        assert payload["resolved"]["id"] == "cust_000001"
        assert payload["initial_style_constraints"]["constraint_source"] == "chat_image"
        assert payload["initial_notice"] == "Image guidance loaded from this chat turn."
        assert payload["initial_email_draft_id"] == "msg_canvas_001"
        assert payload["initial_email_subject"] == "Draft from Canvas"
        assert payload["initial_email_body"] == "Hi Avery,\n\nThis draft started in Canvas."
        assert payload["initial_recommendation_response"]["customer"]["id"] == "cust_000001"
        assert payload["initial_recommendation_response"]["store"]["id"] == "1001"
        assert payload["initial_recommendation_response"]["retrieval_mode"] == "semantic"
        assert payload["initial_recommendation_response"]["recommendation"]["recommendations"]
        assert payload["initial_selected_product_ids"]


def test_open_customer_workspace_with_email_draft_prepares_and_hydrates_workspace(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        result = mcp_server.fashion_open_customer_workspace_with_email_draft(
            customer_query="avery.parker.1@example-fashion.test",
            style_constraints=StyleConstraints(
                constraint_source="chat_image",
                target_categories=["mens_apparel"],
                target_genders=["male"],
                style_keywords=["tailored", "luxury"],
            ),
            top_k=4,
            retrieval_mode=RetrievalMode.semantic,
        )

        payload = result.structuredContent["payload"]
        assert result.structuredContent["kind"] == "customer_search_workspace"
        assert payload["selected_customer_id"] == "cust_000001"
        assert payload["initial_email_draft_id"]
        assert payload["initial_email_subject"]
        assert payload["initial_email_body"]
        assert payload["initial_recommendation_response"]["customer"]["id"] == "cust_000001"
        assert payload["initial_recommendation_response"]["recommendation"]["recommendations"]
        assert payload["initial_selected_product_ids"]


def test_open_customer_workspace_keeps_candidates_when_query_is_ambiguous(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        result = mcp_server.fashion_open_customer_workspace(customer_query="Avery", limit=10)

        payload = result.structuredContent["payload"]
        assert payload["mode"] == "candidates"
        assert payload["query"] == "Avery"
        assert payload["selected_customer_id"] is None
        assert len(payload["results"]) == 2


def test_customer_value_summary_tool_returns_expected_payload_and_bounds(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)

        response = mcp_server.fashion_customer_value_summary(
            customer_id="cust_000001",
            lookback_days=180,
            forecast_weeks=8,
        )

        assert response.customer.id == "cust_000001"
        assert response.purchase_scope.value == "all_stores"
        assert response.lookback_days == 180
        assert response.forecast_weeks == 8
        assert response.metrics.value_tier in {"low", "medium", "high"}
        assert response.purchase_series
        assert response.value_series
        assert len(response.forecast_series) == 8

        with pytest.raises(ValidationError):
            mcp_server.fashion_customer_value_summary(
                customer_id="cust_000001",
                lookback_days=20,
                forecast_weeks=8,
            )


def test_workspace_refactor_removes_legacy_tools_and_resources(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        tool_names = set(mcp_server.mcp._tool_manager._tools.keys())
        assert "fashion_render_customer_search_workspace" in tool_names
        assert "fashion_open_customer_workspace" in tool_names
        assert "fashion_open_customer_workspace_with_email_draft" in tool_names
        assert "fashion_render_merch_workspace" in tool_names
        assert "fashion_open_merch_workspace" in tool_names
        assert "fashion_render_exec_workspace" in tool_names
        assert "fashion_open_exec_workspace" in tool_names
        assert "fashion_exec_overview" in tool_names
        assert "fashion_exec_event_readiness_radar" in tool_names
        assert "fashion_exec_what_if_simulator" in tool_names
        assert "fashion_exec_auto_optimize_strategy" in tool_names
        assert "fashion_exec_publish_strategy_packet" in tool_names
        assert "fashion_exec_get_strategy_packet" in tool_names
        assert "fashion_exec_prepare_strategy_packet_email" in tool_names
        assert "fashion_exec_send_strategy_packet_email" in tool_names
        assert "fashion_merch_get_effective_strategy" in tool_names
        assert "fashion_merch_save_strategy_override" in tool_names
        assert "fashion_exec_campaign_autopilot_prepare" in tool_names
        assert "fashion_exec_campaign_autopilot_send" in tool_names
        assert "fashion_exec_export_csv" in tool_names
        assert "fashion_inventory_products" in tool_names
        assert "fashion_product_margin_sales_opportunities" in tool_names
        assert "fashion_merch_export_csv" in tool_names
        assert "fashion_merch_inventory_view" in tool_names
        assert "fashion_merch_product_mix_recommendations" in tool_names
        assert "fashion_prepare_customer_email_draft" in tool_names
        assert "fashion_update_customer_email_draft" in tool_names
        assert "fashion_get_customer_email_draft" in tool_names
        assert "fashion_send_customer_email_draft" in tool_names
        assert "fashion_customer_value_summary" in tool_names

        removed_tools = {
            "fashion_associate_workspace_bootstrap",
            "fashion_sms_review_bootstrap",
            "fashion_merch_workspace_bootstrap",
            "fashion_render_associate_workspace",
            "fashion_render_associate_board",
            "fashion_render_sms_review",
            "fashion_render_merch_board",
        }
        assert tool_names.isdisjoint(removed_tools)

        resources = mcp_server.mcp._resource_manager._resources
        customer_workspace_keys = [
            key for key in resources.keys() if key.startswith("ui://widgets/customer-search/workspace-")
        ]
        merch_workspace_keys = [
            key for key in resources.keys() if key.startswith("ui://widgets/merch/workspace-")
        ]
        exec_workspace_keys = [
            key for key in resources.keys() if key.startswith("ui://widgets/exec/workspace-")
        ]
        assert customer_workspace_keys
        assert merch_workspace_keys
        assert exec_workspace_keys
        assert resources[customer_workspace_keys[0]].mime_type == "text/html;profile=mcp-app"
        assert resources[merch_workspace_keys[0]].mime_type == "text/html;profile=mcp-app"
        assert resources[exec_workspace_keys[0]].mime_type == "text/html;profile=mcp-app"
        assert "ui://widgets/associate/workspace.html" not in resources
        assert "ui://widgets/sms/review.html" not in resources
        assert "ui://widgets/merch/board.html" not in resources


def test_send_customer_recommendations_email_tool(monkeypatch):
    import app.services.communications as communications

    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        monkeypatch.setattr(
            communications.SesEmailService,
            "send_email",
            lambda self, to_email, subject, text_body, html_body=None: {"message_id": "SES_TOOL_123"},
        )

        response = mcp_server.fashion_send_customer_recommendations_email(
            store_id="1001",
            customer_id="cust_000001",
            selected_product_ids=["prod_1", "prod_2"],
            to_email="buyer@example.com",
            subject="Looks for this week",
        )

        assert response.message.channel == "email"
        assert response.message.status.value == "sent"
        assert response.destination_email == "buyer@example.com"
        assert response.provider_message_id == "SES_TOOL_123"
        assert response.selected_products


def test_customer_email_draft_tools(monkeypatch):
    import app.services.communications as communications

    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        monkeypatch.setattr(
            communications.SesEmailService,
            "send_email",
            lambda self, to_email, subject, text_body, html_body=None: {"message_id": "SES_TOOL_DRAFT_123"},
        )

        draft = mcp_server.fashion_prepare_customer_email_draft(
            store_id="1001",
            customer_id="cust_000001",
            selected_product_ids=["prod_4", "prod_2"],
            to_email="draft-tool@example.com",
            subject="Draft Subject",
        )
        updated = mcp_server.fashion_update_customer_email_draft(
            message_id=draft.message.id,
            subject="Updated Subject",
            body_text="Updated body from tool",
            to_email="updated-tool@example.com",
            selected_product_ids=["prod_4"],
        )
        fetched = mcp_server.fashion_get_customer_email_draft(message_id=draft.message.id)
        sent = mcp_server.fashion_send_customer_email_draft(message_id=draft.message.id)

        assert draft.message.status.value == "draft"
        assert draft.destination_email == "draft-tool@example.com"
        assert draft.message.subject == "Draft Subject"
        assert updated.message.subject == "Updated Subject"
        assert updated.message.body_text == "Updated body from tool"
        assert fetched.message.id == draft.message.id
        assert fetched.message.body_text == "Updated body from tool"
        assert sent.message.status.value == "sent"
        assert sent.destination_email == "updated-tool@example.com"
        assert sent.subject == "Updated Subject"
        assert sent.provider_message_id == "SES_TOOL_DRAFT_123"


def test_fast_mode_skips_embedding(monkeypatch):
    import app.services.recommendations as recommendations

    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        monkeypatch.setattr(recommendations, "EmbeddingService", lambda: _BoomEmbeddingService())

        response = mcp_server.fashion_store_associate_recommend(
            store_id="1001",
            customer_id="cust_000001",
            occasion="wedding",
            budget_max=900,
            retrieval_mode=RetrievalMode.fast,
        )

        assert response.retrieval_mode == RetrievalMode.fast
        assert response.recommendation.strategy == "sql_rules_fast_path"
        assert response.recommendation.recommendations
        assert response.recommendation.recommendations[0].image_url


def test_image_guidance_forces_semantic_mode_even_with_budget(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)

        response = mcp_server.fashion_store_associate_recommend(
            store_id="1001",
            customer_id="cust_000001",
            budget_max=900,
            retrieval_mode=RetrievalMode.auto,
            style_constraints=StyleConstraints(
                constraint_source="chat_image",
                target_categories=["mens_apparel"],
                style_keywords=["luxury", "evening"],
            ),
        )

        assert response.retrieval_mode == RetrievalMode.semantic
        assert response.recommendation.recommendations


def test_prepare_customer_sms_defaults_to_customer_home_store(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, _):
        _seed_data(session)

        draft = prepare_customer_sms(
            session,
            customer_phone_e164="+12145551234",
            occasion="wedding",
            budget_max=900,
            top_k=3,
        )

        assert draft.store.id == "1001"
        assert draft.customer.id == "cust_000001"


def test_lookup_customer_returns_candidates_for_ambiguous_last4(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)

        response = mcp_server.fashion_lookup_customer("Avery")
        assert response.mode == "candidates"
        assert len(response.candidates) == 2


def test_start_and_process_index_job(monkeypatch):
    import app.services.index_jobs as index_jobs

    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)

        monkeypatch.setattr(
            index_jobs,
            "index_products_for_run",
            lambda db, run_id, batch_size=128: {
                "attempted": 4,
                "indexed": 4,
                "failed": 0,
                "status_breakdown": {"indexed": 4},
            },
        )

        queued = mcp_server.fashion_start_index_products("run_test", batch_size=64)
        assert queued.status.value == "queued"
        assert queued.run_id == "run_test"

        processed = process_next_index_job(mcp_server.SessionLocal)
        fetched = mcp_server.fashion_get_index_job(queued.id)
        listed = mcp_server.fashion_list_index_jobs(run_id="run_test", limit=10)

        assert processed is not None
        assert processed.status.value == "succeeded"
        assert processed.indexed == 4
        assert fetched.status.value == "succeeded"
        assert listed.jobs
        assert listed.jobs[0].id == queued.id


def test_render_exec_workspace_returns_template_and_payload(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)

        result = mcp_server.fashion_render_exec_workspace(
            lookback_days=84,
            objective=Objective.revenue,
            top_k_stores=10,
            to_email="manager@example.com",
        )
        html = mcp_server.exec_workspace_resource()

        template_uri = result.meta["openai/outputTemplate"]
        assert template_uri.startswith("ui://widgets/exec/workspace-")
        assert result.structuredContent["kind"] == "exec_workspace"
        payload = result.structuredContent["payload"]
        assert payload["filters"]["lookback_days"] == 84
        assert payload["filters"]["objective"] == "revenue"
        assert payload["filters"]["to_email"] == "manager@example.com"
        assert payload["initial_result"]["store_count"] > 0
        assert payload["initial_result"]["stores"]
        assert payload["last_tool"] == "fashion_exec_overview"
        assert "Executive Overview Workspace" in html
        assert "window.__FASHION_WIDGET__" in html


def test_exec_overview_radar_and_simulator_tools(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)

        overview = mcp_server.fashion_exec_overview(lookback_days=90, top_k_stores=5)
        radar = mcp_server.fashion_exec_event_readiness_radar(lookback_days=56, events=["wedding", "workwear"])
        simulation = mcp_server.fashion_exec_what_if_simulator(
            lookback_days=90,
            discount_pct=12,
            floor_space_shift_pct=6,
            from_category="womens_apparel",
            to_category="shoes",
        )
        scoped_overview = mcp_server.fashion_exec_overview(store_ids=["1001"], lookback_days=90, top_k_stores=5)
        scoped_radar = mcp_server.fashion_exec_event_readiness_radar(
            store_ids=["1001"], lookback_days=56, events=["wedding", "workwear"]
        )
        scoped_simulation = mcp_server.fashion_exec_what_if_simulator(
            store_ids=["1001"],
            lookback_days=90,
            discount_pct=12,
            floor_space_shift_pct=6,
            from_category="womens_apparel",
            to_category="shoes",
        )
        branded_radar = mcp_server.fashion_exec_event_readiness_radar(
            store_ids=["1001"],
            lookback_days=56,
            events=["wedding"],
            brands=["Valentino"],
        )
        branded_simulation = mcp_server.fashion_exec_what_if_simulator(
            store_ids=["1001"],
            lookback_days=90,
            discount_pct=12,
            floor_space_shift_pct=6,
            from_category="womens_apparel",
            to_category="shoes",
            brands=["Valentino"],
        )

        assert overview.total_revenue > 0
        assert overview.store_count >= 2
        assert overview.stores
        assert radar.rows
        assert {row.event for row in radar.rows}.issubset({"wedding", "workwear"})
        assert simulation.expected_revenue > 0
        assert simulation.category_allocations
        assert scoped_overview.store_count == 1
        assert "1 selected store" in scoped_overview.summary
        assert scoped_radar.rows
        assert all(row.store_id == "1001" for row in scoped_radar.rows)
        assert "1 selected store" in scoped_radar.summary
        assert "1 selected store" in scoped_simulation.summary
        assert scoped_simulation.store_allocations
        assert all(row.store_id == "1001" for row in branded_radar.rows)
        assert branded_simulation.expected_revenue > 0


def test_exec_export_csv_returns_raw_store_performance(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)

        scoped = mcp_server.fashion_exec_export_csv(
            store_ids=["1001"],
            lookback_days=90,
            objective=Objective.revenue,
        )
        network = mcp_server.fashion_exec_export_csv(
            lookback_days=90,
            objective=Objective.revenue,
        )

        assert scoped.view.value == "store_performance"
        assert scoped.row_count > 1
        assert scoped.csv_text.splitlines()[0].startswith(
            "data_mode,view,row_type,store_id,store_name,city,state,rank,category,revenue,units,margin_rate,revenue_share_pct"
        )
        store_rows = [row.values for row in scoped.rows if row.values.get("row_type") == "store_summary"]
        category_rows = [row.values for row in scoped.rows if row.values.get("row_type") == "category_performance"]
        assert len(store_rows) == 1
        scoped_store_row = store_rows[0]
        assert scoped_store_row["data_mode"] == "raw"
        assert scoped_store_row["store_id"] == "1001"
        assert scoped_store_row["objective"] == "revenue"
        assert "expected_revenue" not in scoped_store_row
        assert category_rows
        assert all(row["store_id"] == "1001" for row in category_rows)
        assert all(row["category"] for row in category_rows)
        assert network.row_count >= 2


def test_exec_campaign_autopilot_prepare_and_send(monkeypatch):
    import app.services.executive as executive

    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        monkeypatch.setattr(
            executive.SesEmailService,
            "send_email",
            lambda self, to_email, subject, text_body, html_body=None: {"message_id": "SES_EXEC_123"},
        )

        draft = mcp_server.fashion_exec_campaign_autopilot_prepare(
            to_email="store.manager@example.com",
            lookback_days=56,
            top_k=4,
            events=["wedding", "holiday_party", "workwear"],
        )
        scoped_draft = mcp_server.fashion_exec_campaign_autopilot_prepare(
            store_ids=["1001"],
            to_email="store.manager@example.com",
            lookback_days=56,
            top_k=4,
            events=["wedding", "holiday_party", "workwear"],
            brands=["Valentino"],
        )
        defaulted = mcp_server.fashion_exec_campaign_autopilot_prepare(
            lookback_days=56,
            top_k=3,
            events=["wedding", "holiday_party", "workwear"],
        )
        fetched = mcp_server.fashion_exec_get_campaign_autopilot_draft(draft.draft_id)

        assert draft.status.value == "draft"
        assert draft.to_email == "store.manager@example.com"
        assert all(candidate.store_id == "1001" for candidate in scoped_draft.candidates)
        assert defaulted.to_email == "djn12313@gmail.com"
        assert fetched.draft_id == draft.draft_id

        with pytest.raises(ValueError):
            mcp_server.fashion_exec_campaign_autopilot_send(draft_id=draft.draft_id, approved=False)

        sent = mcp_server.fashion_exec_campaign_autopilot_send(draft_id=draft.draft_id, approved=True)
        assert sent.status.value == "sent"
        assert sent.provider_message_id == "SES_EXEC_123"
