from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Customer, Order, OrderItem, Product, Store, SyntheticRun
from app.schemas import Objective
from app.services.communications import customer_message_history, prepare_customer_sms, send_customer_sms
from app.services.lookup import resolve_customer, resolve_store
from app.services.merchandising import merchandising_action_recommendations, merchandising_diagnostics, merchandising_trend_summary


@contextmanager
def _patched_sessionlocal(monkeypatch, module):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", future=True, connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    session = TestingSessionLocal()
    monkeypatch.setattr(module, "SessionLocal", TestingSessionLocal)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_data(session):
    now = datetime(2026, 3, 13, tzinfo=timezone.utc)
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
    session.add(customer)

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
        customer_id="cust_000001",
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


def test_lookup_resolves_store_and_customer(monkeypatch):
    import app.mcp_server as mcp_server

    with _patched_sessionlocal(monkeypatch, mcp_server) as session:
        _seed_data(session)
        store_match = resolve_store(session, store_query="Dallas")
        customer_match = resolve_customer(session, email="avery.parker.1@example-fashion.test")

        assert store_match.resolved.id == "1001"
        assert customer_match.resolved.id == "cust_000001"
        assert customer_match.resolved.home_store_id == "1001"


def test_prepare_send_and_history_sms(monkeypatch):
    import app.mcp_server as mcp_server
    import app.services.communications as communications

    with _patched_sessionlocal(monkeypatch, mcp_server) as session:
        _seed_data(session)
        monkeypatch.setattr(communications.TwilioService, "send_sms", lambda self, body: {"sid": "SM123"})

        draft = prepare_customer_sms(
            session,
            store_id="1001",
            customer_email="avery.parker.1@example-fashion.test",
            occasion="wedding",
            budget_max=900,
            top_k=3,
        )
        sent = send_customer_sms(session, draft.message.id)
        history = customer_message_history(session, customer_id="cust_000001")

        assert draft.message.status == "draft"
        assert "Valentino Rose Dress" in draft.message.body_text
        assert sent.status == "sent"
        assert sent.twilio_message_sid == "SM123"
        assert history.messages[0].id == draft.message.id


def test_merchandising_human_workflows(monkeypatch):
    import app.mcp_server as mcp_server

    with _patched_sessionlocal(monkeypatch, mcp_server) as session:
        _seed_data(session)
        actions = merchandising_action_recommendations(
            session,
            store_query="Dallas",
            question="What should we feature and promote this week for margin?",
            objective=Objective.margin,
            top_k=6,
        )
        diagnostics = merchandising_diagnostics(session, store_id="1001", lookback_days=90)
        trend = merchandising_trend_summary(session, store_id="1001", lookback_days=90)

        returned_actions = {item.action.value for item in actions.recommendations}
        assert actions.store.id == "1001"
        assert actions.peer_store_ids == ["1002"]
        assert "feature" in returned_actions or "promote" in returned_actions
        assert diagnostics.insights
        assert trend.highlights


def test_render_associate_board_returns_widget_metadata(monkeypatch):
    import app.mcp_server as mcp_server

    with _patched_sessionlocal(monkeypatch, mcp_server) as session:
        _seed_data(session)
        result = mcp_server.fashion_render_associate_board(
            store_query="Dallas",
            customer_email="avery.parker.1@example-fashion.test",
            occasion="wedding",
            budget_max=900,
            top_k=3,
        )

        template_uri = result.meta["openai/outputTemplate"]
        token = template_uri.split("/")[-1].replace(".html", "")
        html = mcp_server.associate_widget_resource(token)

        assert template_uri.startswith("ui://widgets/associate/")
        assert result.structuredContent["customer"]["id"] == "cust_000001"
        assert "Avery Parker" in html
