from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.database import Base, get_db
from app.main import create_app
from app.models import Customer, CustomerAuthIdentity, Order, OrderItem, Store, SyntheticRun
from app.services.auth.clerk import AuthenticatedPrincipal, ClerkAuthError
from app.services.catalog_normalization import backfill_catalog_from_legacy_products
from tests.test_catalog_api import _product


@contextmanager
def _chat_client(monkeypatch):
    monkeypatch.setenv("ENABLE_MCP_ADAPTER", "false")
    monkeypatch.setenv("ENABLE_OPENAI_APPS_UI", "false")
    monkeypatch.setenv("OPENAI_API_KEY", "")
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
        _seed_chat_data(session)
        yield TestClient(app), TestingSessionLocal
    finally:
        session.close()
        app.dependency_overrides.clear()
        engine.dispose()
        get_settings.cache_clear()


def _seed_chat_data(session):
    now = datetime(2026, 3, 14, tzinfo=timezone.utc)
    session.add(SyntheticRun(id="run_chat", seed=202, status="loaded", started_at=now, config={}))
    session.add(
        Store(
            id="1001",
            seed_run_id="run_chat",
            name="Dallas Downtown",
            city="Dallas",
            state="TX",
            postal_code="75201",
            address_line1="1 Main St",
            address_line2=None,
            phone="555-111-2222",
            latitude=Decimal("32.770000"),
            longitude=Decimal("-96.790000"),
            profile_type="texas_core",
            services=[],
            raw_source={},
        )
    )
    session.add(
        Customer(
            id="cust_1",
            seed_run_id="run_chat",
            home_store_id="1001",
            first_name="Avery",
            last_name="Parker",
            email="avery@example.com",
            phone_e164="+12145550100",
            city="Dallas",
            state="TX",
            joined_at=now,
            loyalty_tier="gold",
            sex="women",
            price_sensitivity=Decimal("0.3000"),
            occasion_affinity={"wedding": 0.8},
            style_vector={"womens_apparel": 0.9, "shoes": 0.7},
            size_preferences={"dress": "M", "shoe": "8"},
            channel_preference="email",
            pii_token="tok_customer",
        )
    )
    session.add(
        Order(
            id="order_1",
            seed_run_id="run_chat",
            customer_id="cust_1",
            store_id="1001",
            ordered_at=now,
            status="processing",
            occasion="wedding",
            channel="web",
            subtotal=Decimal("595.00"),
            discount_amount=Decimal("0.00"),
            tax_amount=Decimal("49.09"),
            total_amount=Decimal("644.09"),
            returned=False,
        )
    )
    session.add(
        OrderItem(
            id="oi_1",
            order_id="order_1",
            product_id="prod_2",
            quantity=1,
            unit_price=Decimal("595.00"),
            discount_amount=Decimal("0.00"),
            line_total=Decimal("595.00"),
        )
    )
    session.add_all(
        [
            _product("prod_1", seed_run_id="run_chat", title="Valentino Rose Jacket", category="womens_apparel"),
            _product(
                "prod_2",
                seed_run_id="run_chat",
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
                seed_run_id="run_chat",
                title="Valentino Silk Blouse",
                description="Tailored silk blouse",
                price=Decimal("425.00"),
                color="Ivory",
                size="S",
                objective_weight=Decimal("0.6000"),
            ),
            _product(
                "prod_4",
                seed_run_id="run_chat",
                title="Riviera Foundry Black Moisturizer",
                description="Hydrating moisturizer for daily skincare",
                brand="Riviera Foundry",
                category="beauty",
                color="Black",
                size="One Size",
                material="botanical blend",
                price=Decimal("90.00"),
                inventory_qty=8,
                objective_weight=Decimal("0.9500"),
            ),
            _product(
                "prod_5",
                seed_run_id="run_chat",
                title="Example Brand Ivory Leather Shoulder Bag",
                description="Ivory leather shoulder bag",
                brand="Example Brand",
                category="handbags",
                color="Ivory",
                size="One Size",
                material="leather",
                price=Decimal("1400.00"),
                inventory_qty=5,
                objective_weight=Decimal("0.8000"),
            ),
        ]
    )
    session.commit()
    backfill_catalog_from_legacy_products(session, run_id="run_chat")


def _chat_payload(message: str, current_product_id: str = "prod_1", **context):
    return {
        "message": message,
        "context": {
            "page_type": "product",
            "route": f"/product/{current_product_id}",
            "store_id": "1001",
            "current_product": {"id": current_product_id},
            **context,
        },
    }


def test_product_question_works_anonymously(monkeypatch):
    with _chat_client(monkeypatch) as (client, _):
        response = client.post("/api/chat", json=_chat_payload("Is this jacket available?"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["identity_status"] == "anonymous"
    assert payload["intent"] == "product_question"
    assert payload["route"] == "simple_tool"
    assert len(payload["cards"]) == 1
    assert payload["cards"][0]["id"].startswith("cat_")
    assert payload["actions"][0]["type"] == "view_product"


def test_catalog_search_on_pdp_ignores_current_product_category(monkeypatch):
    with _chat_client(monkeypatch) as (client, _):
        response = client.post(
            "/api/chat",
            json=_chat_payload("do you have a moisturizer under $150", current_product_id="prod_5"),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["identity_status"] == "anonymous"
    assert payload["intent"] == "catalog_search"
    assert payload["route"] == "semantic_catalog_search"
    assert payload["cards"]
    assert {card["category"] for card in payload["cards"]} == {"beauty"}
    assert all(card["price_min"] <= 150 for card in payload["cards"])
    assert payload["tool_trace"][1]["name"] == "semantic_catalog_search"


def test_pairing_search_excludes_current_category(monkeypatch):
    with _chat_client(monkeypatch) as (client, _):
        response = client.post(
            "/api/chat",
            json=_chat_payload(
                "find a blouse that goes with this purse",
                current_product_id="prod_5",
                current_product={
                    "id": "prod_5",
                    "title": "Ivory Leather Shoulder Bag",
                    "category": "handbags",
                    "brand": "Example Brand",
                    "attributes": {"color": "ivory", "material": "leather"},
                },
                category="handbags",
            ),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["intent"] == "catalog_search"
    assert payload["route"] == "semantic_catalog_search"
    assert payload["cards"]
    assert {card["category"] for card in payload["cards"]} == {"womens_apparel"}


def test_outfit_pairing_prioritizes_apparel_not_current_category(monkeypatch):
    with _chat_client(monkeypatch) as (client, _):
        response = client.post(
            "/api/chat",
            json=_chat_payload(
                "What outfit goes with this?",
                current_product_id="prod_5",
                current_product={
                    "id": "prod_5",
                    "title": "Ivory Leather Shoulder Bag",
                    "category": "handbags",
                    "brand": "Example Brand",
                    "attributes": {"color": "ivory", "material": "leather", "gender": "women"},
                },
                category="handbags",
            ),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["intent"] == "complementary_products"
    assert payload["route"] == "semantic_catalog_search"
    assert payload["cards"]
    assert "handbags" not in {card["category"] for card in payload["cards"]}
    assert payload["cards"][0]["category"] == "womens_apparel"
    assert payload["tool_trace"][0]["decision"] == "outfit pairing request"


def test_greeting_does_not_return_product_detail(monkeypatch):
    with _chat_client(monkeypatch) as (client, _):
        response = client.post("/api/chat", json=_chat_payload("Hello", current_product_id="prod_5"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["intent"] == "general_style"
    assert payload["route"] == "agentic_response"
    assert payload["cards"] == []
    assert "Hello" in payload["message"]
    assert payload["tool_trace"][0]["decision"] == "greeting"


def test_store_phone_question_is_open_chat(monkeypatch):
    with _chat_client(monkeypatch) as (client, _):
        response = client.post("/api/chat", json=_chat_payload("What phone number can I call?"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["identity_status"] == "anonymous"
    assert payload["intent"] == "general_style"
    assert payload["route"] == "simple_tool"
    assert payload["selected_agent"] == "CustomerServiceAgent"
    assert payload["selected_tool"] == "store_info"
    assert "555-111-2222" in payload["message"]
    assert any(trace["name"] == "ChatIntakeAgent" for trace in payload["tool_trace"])


def test_service_question_uses_approved_answer(monkeypatch):
    with _chat_client(monkeypatch) as (client, _):
        response = client.post("/api/chat", json=_chat_payload("What is your return policy?"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["identity_status"] == "anonymous"
    assert payload["selected_agent"] == "CustomerServiceAgent"
    assert payload["selected_tool"] == "service_answer"
    assert "returns or exchanges" in payload["message"]


def test_order_status_requires_authentication(monkeypatch):
    with _chat_client(monkeypatch) as (client, _):
        response = client.post("/api/chat", json=_chat_payload("What is my order status?"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["identity_status"] == "anonymous"
    assert payload["route"] == "blocked"
    assert payload["selected_agent"] == "OrderAgent"
    assert payload["selected_tool"] == "order_status"
    assert payload["actions"][0]["type"] == "sign_in"


def test_authenticated_order_status_uses_backend_customer_id(monkeypatch):
    def verify_token(token, settings=None):
        return AuthenticatedPrincipal(provider="clerk", provider_user_id="user_123", email="avery@example.com")

    monkeypatch.setattr("app.services.auth.clerk.verify_clerk_token", verify_token)
    with _chat_client(monkeypatch) as (client, _):
        response = client.post(
            "/api/chat",
            json=_chat_payload("What is my order status?"),
            headers={"Authorization": "Bearer valid"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["identity_status"] == "authenticated_customer"
    assert payload["selected_agent"] == "OrderAgent"
    assert payload["selected_tool"] == "order_status"
    assert "order_1 is processing" in payload["message"]
    assert any(trace["name"] == "auth_gate" and "backend-derived customer_id" in trace["decision"] for trace in payload["tool_trace"])


def test_invalid_token_is_rejected(monkeypatch):
    def reject_token(token, settings=None):
        raise ClerkAuthError("bad token")

    monkeypatch.setattr("app.services.auth.clerk.verify_clerk_token", reject_token)
    with _chat_client(monkeypatch) as (client, _):
        response = client.post(
            "/api/chat",
            json=_chat_payload("What goes with this jacket?"),
            headers={"Authorization": "Bearer invalid"},
        )

    assert response.status_code == 401


def test_valid_clerk_token_links_by_email_and_uses_backend_customer_id(monkeypatch):
    def verify_token(token, settings=None):
        return AuthenticatedPrincipal(provider="clerk", provider_user_id="user_123", email="avery@example.com")

    monkeypatch.setattr("app.services.auth.clerk.verify_clerk_token", verify_token)
    with _chat_client(monkeypatch) as (client, SessionLocal):
        response = client.post(
            "/api/chat",
            json=_chat_payload("What would you recommend for me?"),
            headers={"Authorization": "Bearer valid"},
        )
        with SessionLocal() as session:
            identity = session.scalar(select(CustomerAuthIdentity))

    assert response.status_code == 200
    payload = response.json()
    assert payload["identity_status"] == "authenticated_customer"
    assert len(payload["cards"]) >= 1
    assert identity is not None
    assert identity.provider_user_id == "user_123"
    assert identity.customer_id == "cust_1"


def test_unlinked_user_cannot_call_account_tools(monkeypatch):
    def verify_token(token, settings=None):
        return AuthenticatedPrincipal(provider="clerk", provider_user_id="user_999", email="unknown@example.com")

    monkeypatch.setattr("app.services.auth.clerk.verify_clerk_token", verify_token)
    with _chat_client(monkeypatch) as (client, _):
        response = client.post(
            "/api/chat",
            json=_chat_payload("What is my loyalty account status?"),
            headers={"Authorization": "Bearer valid"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["identity_status"] == "authenticated_unlinked"
    assert payload["route"] == "blocked"
    assert payload["actions"][0]["type"] == "link_account"


def test_spoofed_customer_id_in_request_body_is_rejected(monkeypatch):
    with _chat_client(monkeypatch) as (client, _):
        payload = _chat_payload("What would you recommend for me?")
        payload["customer_id"] = "cust_1"
        response = client.post("/api/chat", json=payload)

    assert response.status_code == 422
