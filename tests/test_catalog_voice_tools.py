from __future__ import annotations

import time
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select

from app.catalog.ai_schemas import (
    CatalogAIFieldProposal,
    CatalogAISuggestionProposal,
)
from app.config import Settings
from app.models import CatalogWorkflowEvent, Product, Store
from app.routers.admin_catalog import (
    get_catalog_realtime_service,
    get_catalog_suggestion_ai_service,
)
from app.services.catalog_ai import CatalogAISuggestionService
from app.services.catalog_realtime import CatalogRealtimeService
from tests.test_admin_catalog_api import _admin_catalog_client, _headers
from tests.test_catalog_authoring_v3 import _v3_payload


class _FakeClientSecrets:
    def __init__(self):
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            value=f"ek_short_lived_{len(self.calls)}",
            expires_at=int(time.time()) + 600,
        )


def _fake_client(secrets: _FakeClientSecrets):
    return SimpleNamespace(realtime=SimpleNamespace(client_secrets=secrets))


class _FakeResponses:
    def __init__(self, proposal: CatalogAISuggestionProposal):
        self.proposal = proposal
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        moderation = SimpleNamespace(
            type="moderation_result",
            flagged=False,
            model="omni-moderation-latest",
            categories={"violence": False},
            category_scores={"violence": 0.001},
        )
        return SimpleNamespace(
            id="resp_voice_field_123",
            model="gpt-5.5-2026-05-01",
            status="completed",
            output_parsed=self.proposal,
            moderation=SimpleNamespace(input=moderation, output=moderation),
            usage=SimpleNamespace(
                model_dump=lambda mode="json": {
                    "input_tokens": 40,
                    "output_tokens": 20,
                    "total_tokens": 60,
                }
            ),
        )


def _prepare_voice_draft(client):
    workflow = client.post(
        "/api/admin/catalog/workflows",
        json={
            "title": "Voice product workbench",
            "business_summary": "Ask bounded catalog questions and stage copy.",
        },
        headers=_headers("voice-v3-workflow"),
    ).json()
    payload = _v3_payload(title="Voice Workbench Coat")
    payload["product"]["inventory"][0]["availability"] = "low stock"
    payload["product"]["inventory"][0]["inventory_qty"] = 2
    draft = client.post(
        "/api/admin/catalog/v3/products/drafts",
        json=payload,
        headers=_headers("voice-v3-draft"),
    ).json()
    return workflow, draft, payload


def _context(draft: dict, *, mode: str, target_path: str | None = None) -> dict:
    payload = {
        "mode": mode,
        "product_id": draft["product_id"],
        "draft_id": draft["id"],
        "expected_draft_version": 1,
    }
    if target_path:
        payload["target_path"] = target_path
    return payload


def _storewide_context() -> dict:
    return {
        "mode": "workbench",
        "query_scopes": ["catalog", "inventory"],
    }


def test_v3_voice_reads_inventory_and_stages_only_the_server_pinned_field(monkeypatch):
    settings = Settings(
        _env_file=None,
        openai_api_key="server-only-openai-key",
        catalog_studio_realtime_enabled=True,
        catalog_studio_realtime_safety_identifier_secret="stable-voice-secret",
    )
    secrets = _FakeClientSecrets()
    proposed_description = "A warmer, more editorial description for evening layering."
    suggestion_responses = _FakeResponses(
        CatalogAISuggestionProposal(
            suggestions=[
                CatalogAIFieldProposal(
                    target_path="/description",
                    proposed_value=proposed_description,
                    evidence_asset_ids=[],
                    certainty_class="derived",
                )
            ]
        )
    )

    with _admin_catalog_client(monkeypatch) as (client, sessions):
        workflow, draft, original_payload = _prepare_voice_draft(client)
        client.app.dependency_overrides[get_catalog_realtime_service] = lambda: (
            CatalogRealtimeService(settings, _fake_client(secrets))
        )
        client.app.dependency_overrides[get_catalog_suggestion_ai_service] = lambda: (
            CatalogAISuggestionService(
                settings,
                SimpleNamespace(responses=suggestion_responses),
            )
        )

        query_session = client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/realtime/sessions",
            json=_context(draft, mode="workbench"),
        )
        assert query_session.status_code == 201, query_session.text
        query_context = query_session.json()
        assert query_context["tool_names"] == [
            "read_product_summary",
            "read_catalog_summary",
            "read_inventory_status",
            "read_publish_readiness",
        ]
        assert query_context["session_id"].startswith("realtime_session_")
        with sessions() as db:
            session_event = db.scalar(
                select(CatalogWorkflowEvent)
                .where(CatalogWorkflowEvent.workflow_id == workflow["id"])
                .order_by(CatalogWorkflowEvent.sequence.desc())
                .limit(1)
            )
            assert session_event is not None
            assert session_event.response_json["session_id"] == query_context["session_id"]
        configured_tools = secrets.calls[-1]["session"]["tools"]
        serialized_tools = repr(configured_tools)
        assert "publish_catalog" not in serialized_tools
        assert "archive_catalog" not in serialized_tools
        assert "update_inventory" not in serialized_tools
        assert "draft_id" not in serialized_tools
        assert "product_id" not in serialized_tools

        inventory = client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/realtime/v3/tool-calls",
            json={
                "session_id": query_context["session_id"],
                "call_id": "call-low-stock",
                "name": "read_inventory_status",
                "arguments": {"question": "Which store is low on stock?"},
            },
            headers=_headers("voice-read-inventory"),
        )
        assert inventory.status_code == 200, inventory.text
        inventory_result = inventory.json()
        assert inventory_result["mutation"] is False
        assert "Dallas Downtown" in inventory_result["message"]
        assert inventory_result["citations"] == [
            {
                "kind": "inventory",
                "source_id": "1001",
                "label": "Dallas Downtown",
                "value": {
                    "size": None,
                    "availability": "low stock",
                    "inventory_qty": 2,
                },
            }
        ]
        assert client.get(
            f"/api/admin/catalog/v3/products/{draft['product_id']}/suggestion-sets"
        ).json()["items"] == []

        field_session = client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/realtime/sessions",
            json=_context(draft, mode="field", target_path="/description"),
        )
        assert field_session.status_code == 201, field_session.text
        field_context = field_session.json()
        assert field_context["tool_names"] == ["propose_product_field"]
        field_parameters = secrets.calls[-1]["session"]["tools"][0]["parameters"]
        assert set(field_parameters["properties"]) == {"instruction"}
        assert "target_path" not in repr(field_parameters)

        inventory_field_session = client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/realtime/sessions",
            json=_context(draft, mode="field", target_path="/price_min"),
        )
        assert inventory_field_session.status_code == 422

        empty_workbench = client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/realtime/sessions",
            json={**_context(draft, mode="workbench"), "query_scopes": []},
        )
        assert empty_workbench.status_code == 422

        missing_media_field = client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/realtime/sessions",
            json=_context(draft, mode="field", target_path="/media/missing/alt_text"),
        )
        assert missing_media_field.status_code == 422

        legacy_mutation = client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/realtime/tool-calls",
            json={
                "call_id": "call-legacy-v3-mutation",
                "name": "refine_catalog_draft",
                "arguments": {
                    "instruction": "Directly rewrite the v3 draft.",
                    "current_draft_id": draft["id"],
                    "expected_draft_version": 1,
                },
            },
            headers=_headers("voice-legacy-v3-mutation"),
        )
        assert legacy_mutation.status_code == 409

        stale_query = client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/realtime/v3/tool-calls",
            json={
                "session_id": query_context["session_id"],
                "call_id": "call-stale-query",
                "name": "read_product_summary",
                "arguments": {"question": "What product is active?"},
            },
            headers=_headers("voice-stale-query"),
        )
        assert stale_query.status_code == 409

        proposal = client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/realtime/v3/tool-calls",
            json={
                "session_id": field_context["session_id"],
                "call_id": "call-description-refinement",
                "name": "propose_product_field",
                "arguments": {
                    "instruction": "Make the description warmer.",
                },
            },
            headers=_headers("voice-description-proposal"),
        )
        assert proposal.status_code == 200, proposal.text
        result = proposal.json()
        assert result["mutation"] is False
        suggestion = result["suggestion_set"]["suggestions"][0]
        assert suggestion["target_path"] == "/description"
        assert suggestion["input_origin"] == "voice"
        assert suggestion["status"] == "pending"
        current = client.get(
            f"/api/admin/catalog/v3/products/{draft['product_id']}"
        ).json()["current_draft"]
        assert current["product"]["description"] == original_payload["product"]["description"]

        second_workflow = client.post(
            "/api/admin/catalog/workflows",
            json={
                "title": "Switched voice product",
                "business_summary": "Bind voice to a different active product.",
            },
            headers=_headers("voice-second-workflow"),
        ).json()
        second_draft = client.post(
            "/api/admin/catalog/v3/products/drafts",
            json=_v3_payload(title="Second Voice Product"),
            headers=_headers("voice-second-draft"),
        ).json()
        second_session = client.post(
            f"/api/admin/catalog/workflows/{second_workflow['id']}/realtime/sessions",
            json=_context(second_draft, mode="workbench"),
        )
        assert second_session.status_code == 201, second_session.text

        stale_product_context = client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/realtime/v3/tool-calls",
            json={
                "session_id": field_context["session_id"],
                "call_id": "call-stale-product-context",
                "name": "propose_product_field",
                "arguments": {"instruction": "This old product context must fail."},
            },
            headers=_headers("voice-stale-product-context"),
        )
        assert stale_product_context.status_code == 409

        with sessions() as db:
            latest_second_session = db.scalar(
                select(CatalogWorkflowEvent)
                .where(CatalogWorkflowEvent.workflow_id == second_workflow["id"])
                .order_by(CatalogWorkflowEvent.sequence.desc())
                .limit(1)
            )
            assert latest_second_session is not None
            response_payload = dict(latest_second_session.response_json)
            response_payload["expires_at"] = 1
            latest_second_session.response_json = response_payload
            db.commit()

        expired_session = client.post(
            f"/api/admin/catalog/workflows/{second_workflow['id']}/realtime/v3/tool-calls",
            json={
                "session_id": second_session.json()["session_id"],
                "call_id": "call-expired-session",
                "name": "read_product_summary",
                "arguments": {"question": "What product is active?"},
            },
            headers=_headers("voice-expired-session"),
        )
        assert expired_session.status_code == 409

        forged_target = client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/realtime/v3/tool-calls",
            json={
                "session_id": field_context["session_id"],
                "call_id": "call-forged-target",
                "name": "propose_product_field",
                "arguments": {
                    "instruction": "Change another field.",
                    "target_path": "/title",
                },
            },
            headers=_headers("voice-forged-target"),
        )
        assert forged_target.status_code == 422

        publish_attempt = client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/realtime/v3/tool-calls",
            json={
                "session_id": field_context["session_id"],
                "call_id": "call-publish",
                "name": "publish_catalog_product",
                "arguments": {"question": "Publish this product."},
            },
            headers=_headers("voice-publish-attempt"),
        )
        assert publish_attempt.status_code == 422

        timeline = client.get(
            f"/api/admin/catalog/workflows/{workflow['id']}",
            params={"developer": True},
        )
        assert timeline.status_code == 200
        persisted = timeline.text
        assert proposed_description not in persisted
        assert "server-only-openai-key" not in persisted
        assert "ek_short_lived" not in persisted
        assert "raw_audio" not in persisted


def test_storewide_text_and_voice_queries_work_without_active_product(monkeypatch):
    settings = Settings(
        _env_file=None,
        openai_api_key="server-only-openai-key",
        catalog_studio_realtime_enabled=True,
        catalog_studio_realtime_safety_identifier_secret="stable-voice-secret",
    )
    secrets = _FakeClientSecrets()

    with _admin_catalog_client(monkeypatch) as (client, _sessions):
        workflow = client.post(
            "/api/admin/catalog/workflows",
            json={
                "title": "Store-wide catalog assistant",
                "business_summary": "Ask bounded catalog and inventory questions.",
            },
            headers=_headers("storewide-voice-workflow"),
        ).json()
        payload = _v3_payload(title="Storewide Low Stock Coat")
        payload["product"]["inventory"][0]["availability"] = "low stock"
        payload["product"]["inventory"][0]["inventory_qty"] = 2
        draft = client.post(
            "/api/admin/catalog/v3/products/drafts",
            json=payload,
            headers=_headers("storewide-v3-draft"),
        ).json()
        published = client.post(
            f"/api/admin/catalog/v3/products/{draft['product_id']}/publish",
            json={"draft_id": draft["id"], "expected_version": 0},
            headers=_headers("storewide-v3-publish"),
        )
        assert published.status_code == 200, published.text

        client.app.dependency_overrides[get_catalog_realtime_service] = lambda: (
            CatalogRealtimeService(settings, _fake_client(secrets))
        )

        text_query = client.post(
            "/api/admin/catalog/assistant/query",
            json={
                "question": "Which stores have low stock across the catalog?",
                "query_scopes": ["catalog", "inventory"],
            },
            headers=_headers("storewide-text-query"),
        )
        assert text_query.status_code == 200, text_query.text
        text_result = text_query.json()
        assert text_result["mutation"] is False
        assert text_result["capability_id"] == "catalog_admin.assistant.query"
        assert text_result["capability_surface"] == "admin_assistant"
        assert text_result["persona"] == "catalog_admin"
        assert text_result["selected_tool"] == "catalog_assistant_query"
        assert "Dallas Downtown" in text_result["message"]
        assert text_result["citations"][0]["kind"] == "inventory"
        assert text_result["citations"][0]["label"] == "Dallas Downtown"
        assert text_result["citations"][0]["value"]["low_stock_skus"] == 1

        session = client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/realtime/sessions",
            json=_storewide_context(),
        )
        assert session.status_code == 201, session.text
        session_payload = session.json()
        assert session_payload["tool_names"] == [
            "read_catalog_summary",
            "read_inventory_status",
        ]
        serialized_tools = repr(secrets.calls[-1]["session"]["tools"])
        assert "draft_id" not in serialized_tools
        assert "product_id" not in serialized_tools
        assert "publish_catalog" not in serialized_tools
        assert "update_inventory" not in serialized_tools

        voice_query = client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/realtime/v3/tool-calls",
            json={
                "session_id": session_payload["session_id"],
                "call_id": "call-storewide-low-stock",
                "name": "read_inventory_status",
                "arguments": {"question": "Which stores are low stock?"},
            },
            headers=_headers("storewide-voice-query"),
        )
        assert voice_query.status_code == 200, voice_query.text
        voice_result = voice_query.json()
        assert voice_result["mutation"] is False
        assert "Dallas Downtown" in voice_result["message"]
        assert voice_result["citations"][0]["label"] == "Dallas Downtown"
        assert voice_result["citations"][0]["value"]["low_stock_skus"] == 1

        voice_discount = client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/realtime/v3/tool-calls",
            json={
                "session_id": session_payload["session_id"],
                "call_id": "call-storewide-discount-candidate",
                "name": "read_inventory_status",
                "arguments": {"question": "What item should I discount in Dallas?"},
            },
            headers=_headers("storewide-voice-discount-query"),
        )
        assert voice_discount.status_code == 200, voice_discount.text
        voice_discount_result = voice_discount.json()
        assert voice_discount_result["mutation"] is False
        assert "Published Dress" in voice_discount_result["message"]
        assert "discount" in voice_discount_result["message"]
        assert voice_discount_result["citations"][0]["source_id"] == "prod_existing"
        assert voice_discount_result["citations"][0]["value"]["title"] == "Published Dress"
        assert voice_discount_result["citations"][0]["value"]["store_name"] == "Dallas Downtown"
        assert voice_discount_result["citations"][0]["value"]["margin_pct"] == 0.5


def test_storewide_assistant_uses_inventory_data_for_store_status(monkeypatch):
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        with sessions() as db:
            db.add(
                Store(
                    id="1002",
                    seed_run_id="run_catalog",
                    name="Atlanta",
                    city="Atlanta",
                    state="GA",
                    postal_code="30303",
                    address_line1="2 Peachtree St",
                    profile_type="southeast_core",
                    services=[],
                    raw_source={},
                )
            )
            db.add(
                Product(
                    id="prod_atlanta_low",
                    seed_run_id="run_catalog",
                    store_id="1002",
                    title="Atlanta Low Stock Bag",
                    description="A product with inventory risk.",
                    link="https://example.com/atlanta-low",
                    image_link="https://example.com/atlanta-low.jpg",
                    price=Decimal("250.00"),
                    availability="out of stock",
                    brand="Sterling Hollis",
                    category="handbags",
                    color="Black",
                    size="One Size",
                    material="leather",
                    gender="women",
                    season="fall",
                    margin_pct=Decimal("0.5000"),
                    inventory_qty=0,
                    objective_weight=Decimal("0.8000"),
                    metadata_json={},
                )
            )
            db.commit()

        dallas_status = client.post(
            "/api/admin/catalog/assistant/query",
            json={
                "question": "Dallas status?",
                "query_scopes": ["catalog", "inventory"],
            },
            headers=_headers("store-status-query"),
        )
        assert dallas_status.status_code == 200, dallas_status.text
        dallas_result = dallas_status.json()
        assert dallas_result["mutation"] is False
        assert "Dallas Downtown" in dallas_result["message"]
        assert dallas_result["citations"][0]["source_id"] == "1001"
        assert dallas_result["citations"][0]["value"]["store_name"] == "Dallas Downtown"

        low_stock = client.post(
            "/api/admin/catalog/assistant/query",
            json={
                "question": "Which stores have low stock?",
                "query_scopes": ["catalog", "inventory"],
            },
            headers=_headers("store-low-stock-query"),
        )
        assert low_stock.status_code == 200, low_stock.text
        low_stock_result = low_stock.json()
        labels = [citation["label"] for citation in low_stock_result["citations"]]
        assert "Atlanta" in labels
        assert "Dallas Downtown" in labels
        assert len(labels) == len(set(labels))

        discount_candidate = client.post(
            "/api/admin/catalog/assistant/query",
            json={
                "question": "What item should I discount in Dallas?",
                "query_scopes": ["catalog", "inventory"],
            },
            headers=_headers("store-discount-candidate-query"),
        )
        assert discount_candidate.status_code == 200, discount_candidate.text
        discount_result = discount_candidate.json()
        assert discount_result["mutation"] is False
        assert "Published Dress" in discount_result["message"]
        assert "10% to 15%" in discount_result["message"]
        assert discount_result["citations"][0]["source_id"] == "prod_existing"
        assert discount_result["citations"][0]["label"] == "Dallas Downtown: Published Dress"
        assert discount_result["citations"][0]["value"]["inventory_qty"] == 5
        assert discount_result["citations"][0]["value"]["margin_pct"] == 0.5
