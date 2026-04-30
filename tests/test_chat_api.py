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
from app.models import Customer, CustomerAuthIdentity, Store, SyntheticRun
from app.services.auth.clerk import AuthenticatedPrincipal, ClerkAuthError
from app.services.catalog_normalization import backfill_catalog_from_legacy_products
from tests.test_catalog_api import _product


@contextmanager
def _chat_client(monkeypatch):
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
            phone=None,
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
        ]
    )
    session.commit()
    backfill_catalog_from_legacy_products(session, run_id="run_chat")


def _chat_payload(message: str, **context):
    return {
        "message": message,
        "context": {
            "page_type": "product",
            "route": "/product/prod_1",
            "product_id": "prod_1",
            "store_id": "1001",
            **context,
        },
    }


def test_product_context_question_works_anonymously(monkeypatch):
    with _chat_client(monkeypatch) as (client, _):
        response = client.post("/api/chat", json=_chat_payload("What goes with this jacket?"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["identity_status"] == "anonymous"
    assert payload["route"] == "simple_tool"
    assert 1 <= len(payload["cards"]) <= 3
    assert payload["cards"][0]["id"].startswith("cat_")
    assert payload["actions"][0]["type"] == "view_product"


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
