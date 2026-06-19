from __future__ import annotations

from io import BytesIO
import json
from types import SimpleNamespace

from PIL import Image

from app.catalog.ai_schemas import (
    CatalogAIFieldProposal,
    CatalogAISuggestionProposal,
    CatalogAIUnknownField,
)
from app.config import Settings
from app.routers.admin_catalog import get_catalog_suggestion_ai_service
from app.services.catalog_ai import CatalogAISuggestionService
from tests.test_admin_catalog_api import _admin_catalog_client, _headers
from tests.test_catalog_authoring_v3 import _v3_payload


def _image_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (32, 24), color="navy").save(buffer, format="JPEG")
    return buffer.getvalue()


def _moderation(*, flagged: bool = False):
    return SimpleNamespace(
        type="moderation_result",
        flagged=flagged,
        model="omni-moderation-latest",
        categories={"violence": flagged},
        category_scores={"violence": 0.99 if flagged else 0.001},
    )


def _response(proposal: CatalogAISuggestionProposal | None, *, flagged: bool = False):
    return SimpleNamespace(
        id="resp_grounded_123",
        model="gpt-5.5-2026-05-01",
        status="completed",
        output_parsed=proposal,
        moderation=SimpleNamespace(
            input=_moderation(flagged=flagged),
            output=_moderation(flagged=flagged),
        ),
        usage=SimpleNamespace(
            model_dump=lambda mode="json": {
                "input_tokens": 80,
                "output_tokens": 40,
                "total_tokens": 120,
            }
        ),
    )


class _FakeResponses:
    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _FakeClient:
    def __init__(self, *results):
        self.responses = _FakeResponses(*results)


def _prepare_grounded_draft(client):
    workflow = client.post(
        "/api/admin/catalog/workflows",
        json={
            "title": "Ground supplier product facts",
            "business_summary": "Prepare reviewable product suggestions.",
        },
        headers=_headers("grounded-workflow"),
    ).json()
    bundle = client.post(
        "/api/admin/catalog/source-bundles",
        data={"title": "Supplier coat images"},
        files=[("files", ("front.jpg", _image_bytes(), "image/jpeg"))],
    ).json()
    asset_id = bundle["assets"][0]["id"]
    payload = _v3_payload(title="Grounded Supplier Coat")
    payload["product"]["metadata"] = {
        "supplier": {"storage_key": "private/supplier/front.jpg"}
    }
    payload["product"]["source_references"] = [
        {"bundle_id": bundle["id"], "asset_ids": [asset_id]}
    ]
    draft = client.post(
        "/api/admin/catalog/v3/products/drafts",
        json=payload,
        headers=_headers("grounded-v3-draft"),
    ).json()
    return workflow, draft, asset_id


def test_supplier_analysis_creates_evidence_backed_pending_suggestions(monkeypatch, tmp_path):
    monkeypatch.setenv("CATALOG_SOURCE_OUTPUT_DIR", str(tmp_path / "private-sources"))
    proposal = CatalogAISuggestionProposal(
        suggestions=[
            CatalogAIFieldProposal(
                target_path="/color",
                proposed_value="Navy",
                evidence_asset_ids=[],
                certainty_class="derived",
            ),
            CatalogAIFieldProposal(
                target_path="/material",
                proposed_value="Wool blend",
                evidence_asset_ids=[],
                certainty_class="derived",
            ),
        ],
        unknown_fields=[
            CatalogAIUnknownField(
                target_path="/specifications",
                question="What are the supplier-confirmed dimensions?",
            )
        ],
    )
    fake_client = _FakeClient(_response(proposal), _response(proposal))
    service_settings = Settings(
        _env_file=None,
        catalog_source_output_dir=str(tmp_path / "private-sources"),
    )

    with _admin_catalog_client(monkeypatch) as (client, _sessions):
        workflow, draft, asset_id = _prepare_grounded_draft(client)
        for item in proposal.suggestions:
            item.evidence_asset_ids = [asset_id]
            item.certainty_class = "observed"
        client.app.dependency_overrides[get_catalog_suggestion_ai_service] = lambda: (
            CatalogAISuggestionService(service_settings, fake_client)
        )

        payload = {
            "draft_id": draft["id"],
            "expected_draft_version": 1,
            "workflow_id": workflow["id"],
            "instruction": "Use only visible supplier facts. Internal launch code ORCHID.",
            "input_origin": "supplier_analysis",
            "source_asset_ids": [asset_id],
            "target_paths": ["/color", "/material", "/specifications"],
        }
        unsupported_price = client.post(
            f"/api/admin/catalog/v3/products/{draft['product_id']}/ai-suggestion-sets",
            json={**payload, "target_paths": ["/price_min"]},
            headers=_headers("reject-image-derived-price"),
        )
        assert unsupported_price.status_code == 422
        assert fake_client.responses.calls == []

        created = client.post(
            f"/api/admin/catalog/v3/products/{draft['product_id']}/ai-suggestion-sets",
            json=payload,
            headers=_headers("generate-grounded-suggestions"),
        )

        assert created.status_code == 201, created.text
        result = created.json()
        assert result["status"] == "succeeded"
        assert result["suggestion_set"]["status"] == "pending"
        assert [item["target_path"] for item in result["suggestion_set"]["suggestions"]] == [
            "/color",
            "/material",
        ]
        assert all(
            item["evidence_asset_ids"] == [asset_id]
            and item["certainty_class"] == "observed"
            and item["status"] == "pending"
            for item in result["suggestion_set"]["suggestions"]
        )
        assert result["follow_up_questions"] == [
            {
                "target_path": "/specifications",
                "question": "What are the supplier-confirmed dimensions?",
            }
        ]
        current = client.get(
            f"/api/admin/catalog/v3/products/{draft['product_id']}"
        ).json()["current_draft"]
        assert current["product"]["color"] == _v3_payload()["product"]["color"]

        replay = client.post(
            f"/api/admin/catalog/v3/products/{draft['product_id']}/ai-suggestion-sets",
            json=payload,
            headers=_headers("generate-grounded-suggestions"),
        )
        assert replay.status_code == 201
        assert replay.json()["replayed"] is True
        assert replay.json()["suggestion_set"]["id"] == result["suggestion_set"]["id"]
        assert len(fake_client.responses.calls) == 1

        refreshed = client.post(
            f"/api/admin/catalog/v3/products/{draft['product_id']}/ai-suggestion-sets",
            json={**payload, "instruction": "Refresh from the current supplier evidence."},
            headers=_headers("refresh-grounded-suggestions"),
        )
        assert refreshed.status_code == 201, refreshed.text
        assert refreshed.json()["suggestion_set"]["id"] != result["suggestion_set"]["id"]
        sets = client.get(
            f"/api/admin/catalog/v3/products/{draft['product_id']}/suggestion-sets"
        ).json()["items"]
        assert [item["status"] for item in sets] == ["pending", "pending"]

        provider_call = fake_client.responses.calls[0]
        assert provider_call["store"] is False
        assert provider_call["text_format"] is CatalogAISuggestionProposal
        serialized_input = json.dumps(provider_call["input"])
        assert "input_image" in serialized_input
        assert "data:image/jpeg;base64," in serialized_input
        assert asset_id in serialized_input
        assert "storage_key" not in serialized_input
        assert "private/supplier" not in serialized_input

        timeline = client.get(
            f"/api/admin/catalog/workflows/{workflow['id']}",
            params={"developer": True},
        )
        assert timeline.status_code == 200
        persisted = timeline.text
        assert "ORCHID" not in persisted
        assert "data:image" not in persisted
        assert "resp_grounded_123" in persisted


def test_moderation_block_and_provider_timeout_never_create_suggestions(monkeypatch, tmp_path):
    monkeypatch.setenv("CATALOG_SOURCE_OUTPUT_DIR", str(tmp_path / "private-sources"))
    proposal = CatalogAISuggestionProposal(suggestions=[], unknown_fields=[])
    unauthorized_evidence = CatalogAISuggestionProposal(
        suggestions=[
            CatalogAIFieldProposal(
                target_path="/description",
                proposed_value="A proposed description.",
                evidence_asset_ids=["asset_not_authorized"],
                certainty_class="observed",
            )
        ]
    )
    fake_client = _FakeClient(
        _response(proposal, flagged=True),
        TimeoutError(),
        _response(unauthorized_evidence),
        _response(None),
    )
    service_settings = Settings(
        _env_file=None,
        catalog_source_output_dir=str(tmp_path / "private-sources"),
    )

    with _admin_catalog_client(monkeypatch) as (client, _sessions):
        workflow, draft, asset_id = _prepare_grounded_draft(client)
        client.app.dependency_overrides[get_catalog_suggestion_ai_service] = lambda: (
            CatalogAISuggestionService(service_settings, fake_client)
        )
        base_payload = {
            "draft_id": draft["id"],
            "expected_draft_version": 1,
            "workflow_id": workflow["id"],
            "instruction": "Describe only visible facts.",
            "input_origin": "supplier_analysis",
            "source_asset_ids": [asset_id],
            "target_paths": ["/description"],
        }

        blocked = client.post(
            f"/api/admin/catalog/v3/products/{draft['product_id']}/ai-suggestion-sets",
            json=base_payload,
            headers=_headers("blocked-grounded-suggestions"),
        )
        assert blocked.status_code == 201
        assert blocked.json()["status"] == "blocked"
        assert blocked.json().get("suggestion_set") is None

        timed_out = client.post(
            f"/api/admin/catalog/v3/products/{draft['product_id']}/ai-suggestion-sets",
            json={**base_payload, "instruction": "Try again with visible facts."},
            headers=_headers("timeout-grounded-suggestions"),
        )
        assert timed_out.status_code == 504
        assert timed_out.json()["detail"]["code"] == "responses_timeout"

        invalid_evidence = client.post(
            f"/api/admin/catalog/v3/products/{draft['product_id']}/ai-suggestion-sets",
            json={**base_payload, "instruction": "Cite only the selected image."},
            headers=_headers("invalid-grounded-evidence"),
        )
        assert invalid_evidence.status_code == 502
        assert invalid_evidence.json()["detail"]["code"] == "invalid_structured_output"

        refused = client.post(
            f"/api/admin/catalog/v3/products/{draft['product_id']}/ai-suggestion-sets",
            json={**base_payload, "instruction": "Return a reviewable proposal."},
            headers=_headers("refused-grounded-suggestions"),
        )
        assert refused.status_code == 502
        assert refused.json()["detail"]["code"] == "responses_refused"
        sets = client.get(
            f"/api/admin/catalog/v3/products/{draft['product_id']}/suggestion-sets"
        ).json()["items"]
        assert sets == []


def test_typed_field_action_uses_the_same_pending_suggestion_contract(monkeypatch):
    proposed_description = "A concise typed refinement for a polished evening coat."
    fake_client = _FakeClient(
        _response(
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
    )
    service_settings = Settings(_env_file=None)

    with _admin_catalog_client(monkeypatch) as (client, _sessions):
        workflow = client.post(
            "/api/admin/catalog/workflows",
            json={
                "title": "Typed field refinement",
                "business_summary": "Stage a description improvement.",
            },
            headers=_headers("typed-suggestion-workflow"),
        ).json()
        draft = client.post(
            "/api/admin/catalog/v3/products/drafts",
            json=_v3_payload(title="Typed Suggestion Coat"),
            headers=_headers("typed-suggestion-draft"),
        ).json()
        client.app.dependency_overrides[get_catalog_suggestion_ai_service] = lambda: (
            CatalogAISuggestionService(service_settings, fake_client)
        )

        forged_voice_origin = client.post(
            f"/api/admin/catalog/v3/products/{draft['product_id']}/ai-suggestion-sets",
            json={
                "draft_id": draft["id"],
                "expected_draft_version": 1,
                "workflow_id": workflow["id"],
                "instruction": "Pretend this typed request came from voice.",
                "input_origin": "voice",
                "source_asset_ids": [],
                "target_paths": ["/description"],
            },
            headers=_headers("forged-voice-origin"),
        )
        assert forged_voice_origin.status_code == 422
        assert fake_client.responses.calls == []

        response = client.post(
            f"/api/admin/catalog/v3/products/{draft['product_id']}/ai-suggestion-sets",
            json={
                "draft_id": draft["id"],
                "expected_draft_version": 1,
                "workflow_id": workflow["id"],
                "instruction": "Make the description more concise.",
                "input_origin": "typed_action",
                "source_asset_ids": [],
                "target_paths": ["/description"],
            },
            headers=_headers("typed-description-suggestion"),
        )

        assert response.status_code == 201, response.text
        suggestion = response.json()["suggestion_set"]["suggestions"][0]
        assert suggestion["target_path"] == "/description"
        assert suggestion["input_origin"] == "typed_action"
        assert suggestion["status"] == "pending"
        assert suggestion["proposed_value"] == proposed_description
        provider_input = json.dumps(fake_client.responses.calls[0]["input"])
        assert "input_image" not in provider_input
        current = client.get(
            f"/api/admin/catalog/v3/products/{draft['product_id']}"
        ).json()["current_draft"]
        assert current["product"]["description"] == _v3_payload()["product"]["description"]
