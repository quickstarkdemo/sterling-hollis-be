from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Customer, Order, OrderItem, Product, Store, SyntheticRun
from app.schemas import CompareMode, Objective, PeerMode, PriceBand, RetrievalMode
from app.services.apps_ui import get_widget_state
from app.services.communications import (
    customer_message_history,
    prepare_customer_sms,
    send_customer_sms,
    twilio_smoke_test,
    update_customer_sms_draft,
)
from app.services.index_jobs import process_next_index_job
from app.services.lookup import find_customers, resolve_customer, resolve_store
from app.services.merchandising import merchandising_action_recommendations, merchandising_diagnostics, merchandising_trend_summary


class _DummyPineconeService:
    enabled = False


class _BoomEmbeddingService:
    def embed_text(self, text):
        raise AssertionError("embedding path should not run in fast mode")


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
        price_sensitivity=Decimal("0.4200"),
        occasion_affinity={"wedding": 0.8},
        style_vector={"womens_apparel": 0.9, "shoes": 0.5},
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
        price_sensitivity=Decimal("0.5100"),
        occasion_affinity={"wedding": 0.5},
        style_vector={"womens_apparel": 0.6, "handbags": 0.7},
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
    ]
    session.add_all(products)

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


def test_customer_lookup_supports_name_email_and_phone(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, _):
        _seed_data(session)

        search = find_customers(session, query="Avery", limit=10)
        assert len(search.results) == 2
        assert search.results[0].full_name.startswith("Avery")

        resolved_by_email = resolve_customer(session, email="avery.parker.1@example-fashion.test").resolved
        resolved_by_phone = resolve_customer(session, phone_e164="+12145551234").resolved
        resolved_by_last4 = resolve_customer(session, phone_last4="1234").resolved
        store = resolve_store(session, store_query="Dallas").resolved

        assert resolved_by_email.id == "cust_000001"
        assert resolved_by_phone.id == "cust_000001"
        assert resolved_by_last4.phone_e164 == "+12145551234"
        assert store.id == "1001"


def test_prepare_update_send_history_and_smoke(monkeypatch):
    import app.services.communications as communications

    with _patched_runtime(monkeypatch) as (session, _):
        _seed_data(session)
        monkeypatch.setattr(communications.TwilioService, "send_sms", lambda self, body, to_number=None: {"sid": "SM123"})

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
        history = customer_message_history(session, customer_id="cust_000001", status=sent.status)
        smoke = twilio_smoke_test(session, body_text="Smoke test")

        assert "https://fashion.example/products/prod_2" in draft.message.body_text
        assert draft.message.product_ids == ["prod_2", "prod_1"]
        assert updated.message.product_ids == ["prod_1", "prod_2"]
        assert updated.message.body_text == "Curated picks just for you."
        assert sent.status.value == "sent"
        assert sent.twilio_message_sid == "SM123"
        assert len(history.messages) == 1
        assert smoke.result.status.value == "sent"


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
        )
        trends = merchandising_trend_summary(
            session,
            store_query="Dallas",
            question="Summarize recent womens apparel trends",
            category="womens_apparel",
            compare_mode=CompareMode.peer_and_prior_period,
            peer_mode=PeerMode.profile_type,
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


def test_render_associate_workspace_persists_widget_state(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        result = mcp_server.fashion_render_associate_workspace(
            customer_email="avery.parker.1@example-fashion.test",
            occasion="wedding",
            budget_max=900,
            top_k=3,
        )

        token = result.meta["openai/widgetSessionId"]
        persisted = get_widget_state(token)
        html = mcp_server.associate_widget_session_resource(token)

        assert result.meta["openai/widgetSessionId"] == token
        assert result.meta["openai/outputTemplate"] == f"ui://widgets/associate/workspace/{token}.html"
        assert result.meta["ui"]["resourceUri"] == f"ui://widgets/associate/workspace/{token}.html"
        assert persisted["kind"] == "associate_workspace"
        assert result.structuredContent["kind"] == "associate_workspace"
        assert result.structuredContent["payload"]["widgetSessionId"] == token
        assert result.structuredContent["payload"]["selectedCustomer"]["id"] == "cust_000001"
        assert result.structuredContent["payload"]["store"]["id"] == "1001"
        assert persisted["payload"]["selectedCustomer"]["id"] == "cust_000001"
        assert persisted["payload"]["store"]["id"] == "1001"
        assert persisted["payload"]["widgetSessionId"] == token
        assert persisted["payload"]["selectedProductIds"]
        assert "<style>" in html
        assert "attachHostListeners();" in html
        assert f'widgetSessionId: "{token}"' in html
        assert "initialPayload:" in html
        assert '"selectedCustomer"' in html


def test_render_sms_review_and_merch_board_return_widget_templates(monkeypatch):
    import app.services.communications as communications

    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)
        monkeypatch.setattr(communications.TwilioService, "send_sms", lambda self, body, to_number=None: {"sid": "SM456"})
        draft = prepare_customer_sms(session, store_id="1001", customer_id="cust_000001", occasion="wedding", budget_max=900)

        sms_result = mcp_server.fashion_render_sms_review(draft.message.id)
        merch_result = mcp_server.fashion_render_merch_board(
            store_query="Dallas",
            question="What should this store feature this week if we care about margin?",
            category="womens_apparel",
            compare_mode=CompareMode.peer_and_prior_period,
            peer_mode=PeerMode.profile_type,
            top_k=6,
        )

        sms_token = sms_result.meta["openai/widgetSessionId"]
        merch_token = merch_result.meta["openai/widgetSessionId"]
        assert sms_result.meta["openai/outputTemplate"] == f"ui://widgets/sms/review/{sms_token}.html"
        assert sms_result.structuredContent["kind"] == "sms"
        assert sms_result.structuredContent["payload"]["widgetSessionId"] == sms_token
        assert sms_result.structuredContent["payload"]["message"]["id"] == draft.message.id
        assert sms_token
        assert merch_result.meta["openai/outputTemplate"] == f"ui://widgets/merch/board/{merch_token}.html"
        assert merch_result.structuredContent["kind"] == "merch"
        assert merch_result.structuredContent["payload"]["widgetSessionId"] == merch_token
        assert merch_result.structuredContent["payload"]["store"]["id"] == "1001"
        assert merch_token


def test_bootstrap_tools_return_full_payloads(monkeypatch):
    with _patched_runtime(monkeypatch) as (session, mcp_server):
        _seed_data(session)

        associate = mcp_server.fashion_associate_workspace_bootstrap(
            customer_phone_e164="+12145551234",
            occasion="wedding",
            budget_max=900,
            retrieval_mode=RetrievalMode.auto,
        )
        sms_draft = prepare_customer_sms(
            session,
            store_id="1001",
            customer_id="cust_000001",
            occasion="wedding",
            budget_max=900,
        )
        sms = mcp_server.fashion_sms_review_bootstrap(sms_draft.message.id)
        merch = mcp_server.fashion_merch_workspace_bootstrap(
            store_query="Dallas",
            question="What should we feature for wedding shoppers under $1000?",
            category="womens_apparel",
            price_band=PriceBand.band_500_1000,
        )

        assert associate.store.id == "1001"
        assert associate.selected_customer.id == "cust_000001"
        assert associate.recommendation is not None
        assert associate.selected_product_ids
        assert sms.message.id == sms_draft.message.id
        assert sms.selected_products
        assert sms.history
        assert merch.store.id == "1001"
        assert merch.initial_result.recommendations


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
