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
from app.services.chat.evaluator import ChatEvaluation, ChatOrchestrationDecision
from app.services.chat.evaluator import ChatEvaluationConstraints
from app.services.chat.triage import SearchConstraints, TriageDecision, triage_chat
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
            _product(
                "prod_6",
                seed_run_id="run_chat",
                title="Solenne Studio Satin Gown",
                description="Out-of-stock satin evening gown",
                brand="Solenne Studio",
                category="womens_apparel",
                color="Silver",
                size="M",
                material="satin",
                price=Decimal("900.00"),
                inventory_qty=0,
                availability="out of stock",
                objective_weight=Decimal("0.9900"),
            ),
            _product(
                "prod_7",
                seed_run_id="run_chat",
                title="Jimmy Choo Silver Pump",
                description="Occasion heel in silver satin",
                brand="Jimmy Choo",
                category="shoes",
                color="Silver",
                size="8",
                material="satin",
                price=Decimal("625.00"),
                inventory_qty=6,
                objective_weight=Decimal("0.8500"),
            ),
            _product(
                "prod_8",
                seed_run_id="run_chat",
                title="Jimmy Choo Black Lace Up",
                description="Formal lace-up shoe in black leather",
                brand="Jimmy Choo",
                category="shoes",
                color="Black",
                size="10",
                material="leather",
                gender="men",
                price=Decimal("650.00"),
                inventory_qty=7,
                objective_weight=Decimal("0.9000"),
            ),
            _product(
                "prod_9",
                seed_run_id="run_chat",
                title="Noir Harbor Wool Work Shirt",
                description="Men's Apparel workwear shirt in navy wool",
                brand="Noir Harbor",
                category="mens_apparel",
                color="Navy",
                size="M",
                material="wool",
                gender="men",
                price=Decimal("350.00"),
                inventory_qty=9,
                objective_weight=Decimal("0.8800"),
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


def test_pairing_policy_overrides_related_products_for_womens_shoe(monkeypatch):
    def fake_evaluate_chat(message, context, *, history=None):
        return ChatOrchestrationDecision(
            decision=TriageDecision(
                intent="general_style",
                route="simple_tool",
                reason="strands chose related products",
                use_current_product=True,
                tool="related_products",
            ),
            selected_agent="ProductAgent",
            selected_tool="related_products",
            evaluator_confidence=0.97,
            evaluator_source="test",
            requires_auth=False,
        )

    monkeypatch.setattr("app.services.chat.orchestrator.evaluate_chat", fake_evaluate_chat)
    with _chat_client(monkeypatch) as (client, _):
        response = client.post("/api/chat", json=_chat_payload("What goes with this?", current_product_id="prod_2"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["intent"] == "complementary_products"
    assert payload["selected_tool"] == "semantic_catalog_search"
    assert payload["route"] == "semantic_catalog_search"
    assert payload["cards"]
    assert "shoes" not in {card["category"] for card in payload["cards"]}
    assert all(card["attributes"].get("gender") != "men" for card in payload["cards"])
    assert any("policy_override=pairing_semantic" in trace["decision"] for trace in payload["tool_trace"])


def test_outfit_starter_routes_to_complementary_search(monkeypatch):
    with _chat_client(monkeypatch) as (client, _):
        response = client.post(
            "/api/chat",
            json=_chat_payload("Build an outfit around this", current_product_id="prod_2"),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["intent"] == "complementary_products"
    assert payload["selected_tool"] == "semantic_catalog_search"
    assert payload["route"] == "semantic_catalog_search"
    assert payload["cards"]
    assert "shoes" not in {card["category"] for card in payload["cards"]}


def test_explicit_similar_request_can_use_gender_filtered_related_products(monkeypatch):
    def fake_evaluate_chat(message, context, *, history=None):
        return ChatOrchestrationDecision(
            decision=TriageDecision(
                intent="product_question",
                route="simple_tool",
                reason="explicit similar products",
                use_current_product=True,
                tool="related_products",
            ),
            selected_agent="ProductAgent",
            selected_tool="related_products",
            evaluator_confidence=0.92,
            evaluator_source="test",
            requires_auth=False,
        )

    monkeypatch.setattr("app.services.chat.orchestrator.evaluate_chat", fake_evaluate_chat)
    with _chat_client(monkeypatch) as (client, _):
        response = client.post("/api/chat", json=_chat_payload("Show me similar shoes", current_product_id="prod_2"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["selected_tool"] == "related_products"
    assert payload["cards"]
    assert {card["category"] for card in payload["cards"]} == {"shoes"}
    assert all(card["attributes"].get("gender") == "women" for card in payload["cards"])


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


def test_contextless_pairing_shortcut_asks_for_product(monkeypatch):
    with _chat_client(monkeypatch) as (client, _):
        response = client.post(
            "/api/chat",
            json={
                "message": "What goes with this?",
                "context": {"page_type": "home", "route": "/", "store_id": "1001"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["route"] == "agentic_response"
    assert payload["requires_followup"] is True
    assert payload["selected_tool"] == "chat_response"
    assert payload["actions"] == []
    assert "Which item" in payload["message"]


def test_contextless_availability_shortcut_asks_for_product(monkeypatch):
    with _chat_client(monkeypatch) as (client, _):
        response = client.post(
            "/api/chat",
            json={
                "message": "Is this available?",
                "context": {"page_type": "home", "route": "/", "store_id": "1001"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["requires_followup"] is True
    assert payload["selected_tool"] == "chat_response"
    assert "Which product" in payload["message"]


def test_contextless_pairing_guard_overrides_personalized_evaluator_route(monkeypatch):
    class Result:
        structured_output = ChatEvaluation(
            intent="customer_recommendation",
            target_agent="PersonalShopperAgent",
            tool="customer_recommendations",
            confidence=0.98,
            requires_auth=True,
            clarifying_question=None,
            rationale="Treating contextless styling as personal shopping.",
        )

    def fake_agent(prompt, structured_output_model=None):
        return Result()

    monkeypatch.setattr("app.services.chat.evaluator.build_chat_intake_agent", lambda model_id=None: fake_agent)
    with _chat_client(monkeypatch) as (client, _):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        get_settings.cache_clear()
        response = client.post(
            "/api/chat",
            json={
                "message": "What goes with this?",
                "context": {"page_type": "home", "route": "/", "store_id": "1001"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["selected_tool"] == "chat_response"
    assert payload["requires_followup"] is True
    assert payload["actions"] == []
    assert "Which item" in payload["message"]


def test_chat_evaluation_constraints_normalize_null_lists():
    constraints = ChatEvaluationConstraints(
        colors=None,
        exclude_categories=None,
        materials=None,
        target_categories=None,
    )

    assert constraints.colors == []
    assert constraints.exclude_categories == []
    assert constraints.materials == []
    assert constraints.target_categories == []


def test_selected_tool_dispatch_overrides_intent(monkeypatch):
    def fake_evaluate_chat(message, context, *, history=None):
        return ChatOrchestrationDecision(
            decision=TriageDecision(
                intent="general_style",
                route="semantic_catalog_search",
                reason="forced semantic tool",
                constraints=SearchConstraints(query="moisturizer"),
                target_categories=["beauty"],
                tool="semantic_catalog_search",
            ),
            selected_agent="ProductAgent",
            selected_tool="semantic_catalog_search",
            evaluator_confidence=0.91,
            evaluator_source="test",
            requires_auth=False,
        )

    monkeypatch.setattr("app.services.chat.orchestrator.evaluate_chat", fake_evaluate_chat)
    with _chat_client(monkeypatch) as (client, _):
        response = client.post(
            "/api/chat",
            json={
                "message": "General wording that should still use semantic tool",
                "context": {"page_type": "home", "route": "/", "store_id": "1001"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["selected_tool"] == "semantic_catalog_search"
    assert payload["route"] == "semantic_catalog_search"
    assert payload["cards"]
    assert {card["category"] for card in payload["cards"]} == {"beauty"}


def test_store_scoped_chat_search_excludes_out_of_stock_cards(monkeypatch):
    with _chat_client(monkeypatch) as (client, _):
        response = client.post(
            "/api/chat",
            json={
                "message": "find satin pieces",
                "context": {"page_type": "home", "route": "/", "store_id": "1001"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["cards"]
    assert all(card["inventory_summary"]["availability"] != "out_of_stock" for card in payload["cards"])


def test_chat_search_normalizes_evaluator_category_aliases(monkeypatch):
    def fake_evaluate_chat(message, context, *, history=None):
        return ChatOrchestrationDecision(
            decision=TriageDecision(
                intent="catalog_search",
                route="semantic_catalog_search",
                reason="llm chose retail category label",
                constraints=SearchConstraints(query="satin evening pieces"),
                target_categories=["evening wear"],
                tool="semantic_catalog_search",
            ),
            selected_agent="ProductAgent",
            selected_tool="semantic_catalog_search",
            evaluator_confidence=0.98,
            evaluator_source="test",
            requires_auth=False,
        )

    monkeypatch.setattr("app.services.chat.orchestrator.evaluate_chat", fake_evaluate_chat)
    with _chat_client(monkeypatch) as (client, _):
        response = client.post(
            "/api/chat",
            json={
                "message": "Find satin evening pieces",
                "context": {"page_type": "home", "route": "/", "store_id": "1001"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["cards"]
    assert any("satin" in card["title"].lower() or "satin" in card["description"].lower() for card in payload["cards"])
    assert all(card["inventory_summary"]["availability"] != "out_of_stock" for card in payload["cards"])


def test_chat_search_workwear_alias_browses_mens_apparel(monkeypatch):
    def fake_evaluate_chat(message, context, *, history=None):
        return ChatOrchestrationDecision(
            decision=TriageDecision(
                intent="catalog_search",
                route="semantic_catalog_search",
                reason="llm chose workwear label",
                constraints=SearchConstraints(query="men's workwear"),
                target_categories=["workwear"],
                tool="semantic_catalog_search",
            ),
            selected_agent="ProductAgent",
            selected_tool="semantic_catalog_search",
            evaluator_confidence=0.98,
            evaluator_source="test",
            requires_auth=False,
        )

    monkeypatch.setattr("app.services.chat.orchestrator.evaluate_chat", fake_evaluate_chat)
    with _chat_client(monkeypatch) as (client, _):
        response = client.post(
            "/api/chat",
            json={
                "message": "Men's workwear?",
                "context": {"page_type": "home", "route": "/", "store_id": "1001"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["cards"]
    assert {card["category"] for card in payload["cards"]} == {"mens_apparel"}


def test_evaluator_error_trace_includes_exception_class(monkeypatch):
    def explode(model_id=None):
        raise RuntimeError("broken evaluator")

    monkeypatch.setattr("app.services.chat.evaluator.build_chat_intake_agent", explode)
    with _chat_client(monkeypatch) as (client, _):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        get_settings.cache_clear()
        response = client.post("/api/chat", json=_chat_payload("Hello"))

    assert response.status_code == 200
    payload = response.json()
    assert any(
        trace["name"] == "ChatIntakeAgent" and "RuntimeError" in trace["decision"]
        for trace in payload["tool_trace"]
    )
    assert any(trace["name"] == "evaluator_error" and trace["decision"] == "RuntimeError" for trace in payload["tool_trace"])


def test_evaluator_receives_recent_chat_history(monkeypatch):
    captured = {}
    with _chat_client(monkeypatch) as (client, _):
        first_response = client.post(
            "/api/chat",
            json={
                "message": "What goes with this?",
                "context": {"page_type": "home", "route": "/", "store_id": "1001"},
            },
        )
        conversation_id = first_response.json()["conversation_id"]

        def fake_evaluate_chat(message, context, *, history=None):
            captured["history"] = history or []
            decision = triage_chat("find a blouse", context)
            return ChatOrchestrationDecision(
                decision=decision,
                selected_agent="ProductAgent",
                selected_tool="semantic_catalog_search",
                evaluator_confidence=0.9,
                evaluator_source="test",
                requires_auth=False,
            )

        monkeypatch.setattr("app.services.chat.orchestrator.evaluate_chat", fake_evaluate_chat)
        response = client.post(
            "/api/chat",
            json={
                "message": "The ivory bag",
                "conversation_id": conversation_id,
                "context": {"page_type": "home", "route": "/", "store_id": "1001"},
            },
        )

    assert response.status_code == 200
    assert any(turn["role"] == "assistant" and "Which item" in turn["content"] for turn in captured["history"])


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
    assert "same email address" in payload["message"]
    assert payload["actions"] == []


def test_spoofed_customer_id_in_request_body_is_rejected(monkeypatch):
    with _chat_client(monkeypatch) as (client, _):
        payload = _chat_payload("What would you recommend for me?")
        payload["customer_id"] = "cust_1"
        response = client.post("/api/chat", json=payload)

    assert response.status_code == 422
