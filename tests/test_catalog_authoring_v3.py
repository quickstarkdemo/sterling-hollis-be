from __future__ import annotations

import importlib.util
from io import BytesIO
from pathlib import Path

from PIL import Image
from sqlalchemy import create_engine, inspect, text

from app.models import CatalogDraftRevision
from app.services.auth.admin import require_catalog_admin
from app.services.auth.clerk import AuthenticatedPrincipal
from tests.test_admin_catalog_api import _admin_catalog_client, _headers, _snapshot_v2


def _image_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (32, 24), color="navy").save(buffer, format="JPEG")
    return buffer.getvalue()


def _v3_payload(*, title: str = "Authoring V3 Coat", media: bool = True) -> dict:
    payload = _snapshot_v2(title=title)
    product = payload["product"]
    product["schema_version"] = 3
    product["benefits"] = ["Weather-ready warmth", "Layer-friendly fit"]
    product["specifications"] = [
        {"name": "material", "value": "Wool blend"},
        {"name": "closure", "value": "Button front"},
    ]
    product["care_instructions"] = ["Dry clean only"]
    product["content_details"] = ["Fully lined", "Interior pocket"]
    product["seo"] = {
        "title": "Sterling Hollis wool coat",
        "description": "A polished wool coat for cool-weather layering.",
        "keywords": ["wool coat", "women's outerwear"],
    }
    product["source_references"] = []
    product["readiness_inputs"] = {
        "required_specifications": ["material", "closure"],
    }
    if media:
        for item in product["media"]:
            item["alt_text"] = "Black wool coat on a neutral background"
    else:
        product["media"] = []
    return payload


def _create_v3_draft(client, *, key: str, payload: dict | None = None):
    return client.post(
        "/api/admin/catalog/v3/products/drafts",
        json=payload or _v3_payload(),
        headers=_headers(key),
    )


def _create_suggestion_set(
    client,
    *,
    product_id: str,
    draft_id: str,
    expected_draft_version: int,
    suggestions: list[dict],
    key: str,
    workflow_id: str | None = None,
):
    payload = {
        "draft_id": draft_id,
        "expected_draft_version": expected_draft_version,
        "suggestions": suggestions,
    }
    if workflow_id:
        payload["workflow_id"] = workflow_id
    return client.post(
        f"/api/admin/catalog/v3/products/{product_id}/suggestion-sets",
        json=payload,
        headers=_headers(key),
    )


def test_v3_round_trip_v2_projection_and_replacement_write_guard(monkeypatch, tmp_path):
    monkeypatch.setenv("CATALOG_SOURCE_OUTPUT_DIR", str(tmp_path / "private-sources"))
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        source_response = client.post(
            "/api/admin/catalog/source-bundles",
            data={"title": "Supplier evidence"},
            files=[("files", ("front.jpg", _image_bytes(), "image/jpeg"))],
        )
        assert source_response.status_code == 201
        source = source_response.json()

        payload = _v3_payload()
        payload["product"]["source_references"] = [
            {
                "bundle_id": source["id"],
                "asset_ids": [source["assets"][0]["id"]],
            }
        ]
        created = _create_v3_draft(client, key="v3-round-trip", payload=payload)

        assert created.status_code == 201, created.text
        draft = created.json()
        product_id = draft["product_id"]
        v3_product = client.get(f"/api/admin/catalog/v3/products/{product_id}")
        assert v3_product.status_code == 200
        current_v3 = v3_product.json()["current_draft"]
        assert current_v3["product"]["schema_version"] == 3
        assert current_v3["product"]["benefits"] == payload["product"]["benefits"]
        assert current_v3["product"]["seo"] == payload["product"]["seo"]
        assert current_v3["product"]["source_references"] == payload["product"]["source_references"]

        v2_product = client.get(f"/api/admin/catalog/v2/products/{product_id}")
        assert v2_product.status_code == 200
        current_v2 = v2_product.json()["current_draft"]
        assert current_v2["product"]["schema_version"] == 2
        assert "benefits" not in current_v2["product"]
        assert "seo" not in current_v2["product"]
        assert "source_references" not in current_v2["product"]

        v2_payload = _snapshot_v2(title="Older client overwrite")
        v2_payload["current_draft_id"] = draft["id"]
        v2_payload["expected_draft_version"] = 1
        rejected = client.put(
            f"/api/admin/catalog/v2/products/{product_id}/draft",
            json=v2_payload,
            headers=_headers("v2-must-not-erase-v3"),
        )
        assert rejected.status_code == 409
        assert "v3" in rejected.json()["detail"].lower()

        duplicate_v2_create = client.post(
            "/api/admin/catalog/v2/products/drafts",
            json=_snapshot_v2(title="Authoring V3 Coat"),
            headers=_headers("v2-create-must-not-shadow-v3"),
        )
        assert duplicate_v2_create.status_code == 409
        assert "v3" in duplicate_v2_create.json()["detail"].lower()

        with sessions() as db:
            revisions = db.execute(
                text(
                    "select id, snapshot_json from catalog_draft_revisions "
                    "where catalog_product_id = :product_id order by created_at"
                ),
                {"product_id": product_id},
            ).all()
            assert len(revisions) == 1

        published = client.post(
            f"/api/admin/catalog/v3/products/{product_id}/publish",
            json={"draft_id": draft["id"], "expected_version": 0},
            headers=_headers("publish-v3-authoring"),
        )
        assert published.status_code == 200, published.text
        published_product = client.get(
            f"/api/admin/catalog/v3/products/{product_id}"
        ).json()["published_snapshot"]
        assert published_product["benefits"] == payload["product"]["benefits"]
        assert published_product["seo"] == payload["product"]["seo"]
        assert published_product["source_references"] == []

        rejected_v2_revision = client.post(
            f"/api/admin/catalog/v2/products/{product_id}/revisions",
            json={"expected_version": 1},
            headers=_headers("v2-revision-must-not-erase-published-v3"),
        )
        assert rejected_v2_revision.status_code == 409
        assert "v3" in rejected_v2_revision.json()["detail"].lower()

        session = client.get("/api/admin/session")
        assert session.status_code == 200
        assert session.json()["capabilities"]["catalog"]["authoring_schema_version"] == 3


def test_field_accept_reject_and_idempotent_replay_share_one_contract(monkeypatch):
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        created = _create_v3_draft(client, key="v3-suggestion-draft")
        assert created.status_code == 201
        draft = created.json()
        product_id = draft["product_id"]

        suggestion_response = _create_suggestion_set(
            client,
            product_id=product_id,
            draft_id=draft["id"],
            expected_draft_version=1,
            key="create-voice-suggestions",
            suggestions=[
                {
                    "target_path": "/description",
                    "proposed_value": "A warmer, polished description.",
                    "certainty_class": "derived",
                    "input_origin": "voice",
                    "evidence_asset_ids": [],
                },
                {
                    "target_path": "/benefits",
                    "proposed_value": ["Warm without bulk", "Easy to layer"],
                    "certainty_class": "derived",
                    "input_origin": "typed_action",
                    "evidence_asset_ids": [],
                },
            ],
        )
        assert suggestion_response.status_code == 201, suggestion_response.text
        suggestion_set = suggestion_response.json()
        description_suggestion, benefits_suggestion = suggestion_set["suggestions"]
        assert description_suggestion["status"] == "pending"
        assert description_suggestion["input_origin"] == "voice"

        competing_set = _create_suggestion_set(
            client,
            product_id=product_id,
            draft_id=draft["id"],
            expected_draft_version=1,
            key="create-competing-suggestion-set",
            suggestions=[
                {
                    "target_path": "/seo/description",
                    "proposed_value": "A competing search description.",
                    "certainty_class": "derived",
                    "input_origin": "supplier_analysis",
                    "evidence_asset_ids": [],
                }
            ],
        ).json()

        accept_payload = {
            "action": "accept",
            "scope": "suggestion",
            "suggestion_id": description_suggestion["id"],
            "expected_draft_version": 1,
            "reason": "Merchant approved the voice refinement.",
        }
        accepted = client.post(
            f"/api/admin/catalog/v3/products/{product_id}/suggestion-sets/"
            f"{suggestion_set['id']}/decisions",
            json=accept_payload,
            headers=_headers("accept-one-voice-suggestion"),
        )
        assert accepted.status_code == 200, accepted.text
        accepted_body = accepted.json()
        assert accepted_body["suggestion_set"]["current_draft_version"] == 2
        accepted_suggestion = next(
            item
            for item in accepted_body["suggestion_set"]["suggestions"]
            if item["id"] == description_suggestion["id"]
        )
        assert accepted_suggestion["status"] == "accepted"
        assert accepted_suggestion["prior_value"] == _v3_payload()["product"]["description"]
        assert accepted_suggestion["reviewed_by"] == "user_admin"
        assert accepted_body["draft"]["product"]["description"] == accept_payload.get(
            "proposed_value", "A warmer, polished description."
        )
        assert accepted_body["draft"]["product"]["benefits"] == _v3_payload()["product"]["benefits"]
        superseded_set = next(
            item
            for item in client.get(
                f"/api/admin/catalog/v3/products/{product_id}/suggestion-sets"
            ).json()["items"]
            if item["id"] == competing_set["id"]
        )
        assert superseded_set["status"] == "superseded"
        assert superseded_set["suggestions"][0]["status"] == "superseded"

        replay = client.post(
            f"/api/admin/catalog/v3/products/{product_id}/suggestion-sets/"
            f"{suggestion_set['id']}/decisions",
            json=accept_payload,
            headers=_headers("accept-one-voice-suggestion"),
        )
        assert replay.status_code == 200
        assert replay.json() == accepted_body

        rejected = client.post(
            f"/api/admin/catalog/v3/products/{product_id}/suggestion-sets/"
            f"{suggestion_set['id']}/decisions",
            json={
                "action": "reject",
                "scope": "suggestion",
                "suggestion_id": benefits_suggestion["id"],
                "expected_draft_version": 2,
                "reason": "Use the original benefits.",
            },
            headers=_headers("reject-benefits-suggestion"),
        )
        assert rejected.status_code == 200
        assert rejected.json()["draft"] is None
        rejected_item = next(
            item
            for item in rejected.json()["suggestion_set"]["suggestions"]
            if item["id"] == benefits_suggestion["id"]
        )
        assert rejected_item["status"] == "rejected"

        with sessions() as db:
            revision_count = db.execute(
                text(
                    "select count(*) from catalog_draft_revisions "
                    "where catalog_product_id = :product_id"
                ),
                {"product_id": product_id},
            ).scalar_one()
            assert revision_count == 2


def test_section_acceptance_is_atomic_and_stale_or_unauthorized_actions_preserve_draft(
    monkeypatch,
):
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        created = _create_v3_draft(client, key="v3-atomic-draft")
        draft = created.json()
        product_id = draft["product_id"]
        suggestions = _create_suggestion_set(
            client,
            product_id=product_id,
            draft_id=draft["id"],
            expected_draft_version=1,
            key="v3-atomic-set",
            suggestions=[
                {
                    "target_path": "/description",
                    "proposed_value": "Atomic section description",
                    "certainty_class": "derived",
                    "input_origin": "typed_action",
                    "evidence_asset_ids": [],
                },
                {
                    "target_path": "/benefits",
                    "proposed_value": ["Atomic benefit"],
                    "certainty_class": "derived",
                    "input_origin": "typed_action",
                    "evidence_asset_ids": [],
                },
            ],
        ).json()

        second_id = suggestions["suggestions"][1]["id"]
        with sessions() as db:
            db.execute(
                text(
                    "update catalog_field_suggestions set proposed_value_json = :value "
                    "where id = :suggestion_id"
                ),
                    {"value": '"not-a-list"', "suggestion_id": second_id},
            )
            db.commit()

        failed = client.post(
            f"/api/admin/catalog/v3/products/{product_id}/suggestion-sets/"
            f"{suggestions['id']}/decisions",
            json={
                "action": "accept",
                "scope": "section",
                "section": "content",
                "expected_draft_version": 1,
            },
            headers=_headers("atomic-section-failure"),
        )
        assert failed.status_code == 422
        product_after = client.get(f"/api/admin/catalog/v3/products/{product_id}").json()
        assert product_after["current_draft"]["product"]["description"] == _v3_payload()["product"]["description"]
        assert all(
            item["status"] == "pending"
            for item in client.get(
                f"/api/admin/catalog/v3/products/{product_id}/suggestion-sets"
            ).json()["items"][0]["suggestions"]
        )

        client.app.dependency_overrides[require_catalog_admin] = lambda: AuthenticatedPrincipal(
            provider="clerk",
            provider_user_id="other_admin",
            email="other@example.com",
            claims={},
        )
        unauthorized = client.post(
            f"/api/admin/catalog/v3/products/{product_id}/suggestion-sets/"
            f"{suggestions['id']}/decisions",
            json={
                "action": "reject",
                "scope": "remaining",
                "expected_draft_version": 1,
            },
            headers=_headers("other-admin-reject"),
        )
        assert unauthorized.status_code == 404
        assert client.get(f"/api/admin/catalog/v3/products/{product_id}").status_code == 404
        assert client.get(
            f"/api/admin/catalog/v3/products/{product_id}/suggestion-sets"
        ).json()["items"] == []
        unauthorized_publish = client.post(
            f"/api/admin/catalog/v3/products/{product_id}/publish",
            json={"draft_id": draft["id"], "expected_version": 0},
            headers=_headers("other-admin-publish"),
        )
        assert unauthorized_publish.status_code == 404

        cross_owner_replay = _create_suggestion_set(
            client,
            product_id=product_id,
            draft_id=draft["id"],
            expected_draft_version=1,
            key="v3-atomic-set",
            suggestions=[
                {
                    "target_path": "/description",
                    "proposed_value": "Atomic section description",
                    "certainty_class": "derived",
                    "input_origin": "typed_action",
                    "evidence_asset_ids": [],
                },
                {
                    "target_path": "/benefits",
                    "proposed_value": ["Atomic benefit"],
                    "certainty_class": "derived",
                    "input_origin": "typed_action",
                    "evidence_asset_ids": [],
                },
            ],
        )
        assert cross_owner_replay.status_code == 409
        client.app.dependency_overrides[require_catalog_admin] = lambda: AuthenticatedPrincipal(
            provider="clerk",
            provider_user_id="user_admin",
            email="admin@example.com",
            claims={},
        )

        pending = _create_suggestion_set(
            client,
            product_id=product_id,
            draft_id=draft["id"],
            expected_draft_version=1,
            key="v3-pending-before-manual-edit",
            suggestions=[
                {
                    "target_path": "/seo/title",
                    "proposed_value": "Pending search title",
                    "certainty_class": "derived",
                    "input_origin": "typed_action",
                    "evidence_asset_ids": [],
                }
            ],
        ).json()
        manual_payload = _v3_payload()
        manual_payload["current_draft_id"] = draft["id"]
        manual_payload["expected_draft_version"] = 1
        manual_payload["product"]["description"] = "A manual edit superseded prior suggestions."
        manual_revision = client.put(
            f"/api/admin/catalog/v3/products/{product_id}/draft",
            json=manual_payload,
            headers=_headers("v3-manual-edit-supersedes"),
        )
        assert manual_revision.status_code == 201
        stale_decision = client.post(
            f"/api/admin/catalog/v3/products/{product_id}/suggestion-sets/"
            f"{pending['id']}/decisions",
            json={
                "action": "accept",
                "scope": "remaining",
                "expected_draft_version": 2,
            },
            headers=_headers("v3-stale-decision"),
        )
        assert stale_decision.status_code == 409
        pending_after = next(
            item
            for item in client.get(
                f"/api/admin/catalog/v3/products/{product_id}/suggestion-sets"
            ).json()["items"]
            if item["id"] == pending["id"]
        )
        assert pending_after["status"] == "superseded"

        supersede_candidate = _create_suggestion_set(
            client,
            product_id=product_id,
            draft_id=manual_revision.json()["id"],
            expected_draft_version=2,
            key="v3-explicit-supersede-set",
            suggestions=[
                {
                    "target_path": "/seo/title",
                    "proposed_value": "Unused search title",
                    "certainty_class": "unknown",
                    "input_origin": "voice",
                    "evidence_asset_ids": [],
                }
            ],
        ).json()
        explicitly_superseded = client.post(
            f"/api/admin/catalog/v3/products/{product_id}/suggestion-sets/"
            f"{supersede_candidate['id']}/decisions",
            json={
                "action": "supersede",
                "scope": "remaining",
                "expected_draft_version": 2,
            },
            headers=_headers("v3-explicit-supersede"),
        )
        assert explicitly_superseded.status_code == 200
        assert explicitly_superseded.json()["draft"] is None
        assert explicitly_superseded.json()["suggestion_set"]["status"] == "superseded"

def test_suggestion_targets_evidence_and_stale_versions_are_rejected(monkeypatch):
    with _admin_catalog_client(monkeypatch) as (client, _sessions):
        created = _create_v3_draft(client, key="v3-validation-draft")
        draft = created.json()
        product_id = draft["product_id"]

        unsupported = _create_suggestion_set(
            client,
            product_id=product_id,
            draft_id=draft["id"],
            expected_draft_version=1,
            key="v3-unsupported-target",
            suggestions=[
                {
                    "target_path": "/inventory",
                    "proposed_value": [],
                    "certainty_class": "derived",
                    "input_origin": "voice",
                    "evidence_asset_ids": [],
                }
            ],
        )
        assert unsupported.status_code == 422

        unattached_evidence = _create_suggestion_set(
            client,
            product_id=product_id,
            draft_id=draft["id"],
            expected_draft_version=1,
            key="v3-unattached-evidence",
            suggestions=[
                {
                    "target_path": "/description",
                    "proposed_value": "Observed supplier description",
                    "certainty_class": "observed",
                    "input_origin": "supplier_analysis",
                    "evidence_asset_ids": ["asset_not_attached"],
                }
            ],
        )
        assert unattached_evidence.status_code == 422

        stale = _create_suggestion_set(
            client,
            product_id=product_id,
            draft_id=draft["id"],
            expected_draft_version=2,
            key="v3-stale-suggestion-version",
            suggestions=[
                {
                    "target_path": "/description",
                    "proposed_value": "Stale description",
                    "certainty_class": "derived",
                    "input_origin": "typed_action",
                    "evidence_asset_ids": [],
                }
            ],
        )
        assert stale.status_code == 409
        assert client.get(
            f"/api/admin/catalog/v3/products/{product_id}/suggestion-sets"
        ).json()["items"] == []


def test_supplier_media_promotion_preserves_v3_authoring(monkeypatch, tmp_path):
    monkeypatch.setenv("CATALOG_SOURCE_OUTPUT_DIR", str(tmp_path / "private-sources"))
    monkeypatch.setenv("PRODUCT_IMAGE_OUTPUT_DIR", str(tmp_path / "product-images"))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://catalog.example")

    with _admin_catalog_client(monkeypatch) as (client, _sessions):
        payload = _v3_payload(title="V3 Supplier Promotion Coat")
        created = _create_v3_draft(
            client,
            key="v3-source-promotion-draft",
            payload=payload,
        ).json()
        bundle = client.post(
            "/api/admin/catalog/source-bundles",
            data={
                "title": "V3 supplier media",
                "draft_revision_id": created["id"],
            },
            files=[("files", ("supplier.jpg", _image_bytes(), "image/jpeg"))],
        ).json()
        source_asset = bundle["assets"][0]

        revised_payload = _v3_payload(title="V3 Supplier Promotion Coat")
        revised_payload["current_draft_id"] = created["id"]
        revised_payload["expected_draft_version"] = 1
        revised_payload["product"]["source_references"] = [
            {"bundle_id": bundle["id"], "asset_ids": [source_asset["id"]]}
        ]
        revised = client.put(
            f"/api/admin/catalog/v3/products/{created['product_id']}/draft",
            json=revised_payload,
            headers=_headers("attach-v3-source-reference"),
        )
        assert revised.status_code == 201, revised.text

        promoted = client.post(
            f"/api/admin/catalog/source-bundles/{bundle['id']}/assets/"
            f"{source_asset['id']}/promote",
            json={"draft_id": revised.json()["id"], "expected_draft_version": 2},
            headers=_headers("promote-source-into-v3"),
        )
        assert promoted.status_code == 201, promoted.text

        current = client.get(
            f"/api/admin/catalog/v3/products/{created['product_id']}"
        ).json()["current_draft"]
        assert current["revision"]["id"] == promoted.json()["draft"]["id"]
        assert current["product"]["schema_version"] == 3
        assert current["product"]["benefits"] == payload["product"]["benefits"]
        assert current["product"]["seo"] == payload["product"]["seo"]
        assert current["product"]["source_references"] == revised_payload["product"][
            "source_references"
        ]
        assert [
            (item["store_id"], item["size"], item["availability"], item["inventory_qty"])
            for item in current["product"]["inventory"]
        ] == [("1001", None, "in stock", 8)]
        original_media = next(
            item
            for item in current["product"]["media"]
            if item["media_id"] == "media_studio_coat"
        )
        assert original_media["alt_text"] == "Black wool coat on a neutral background"
        promoted_media = next(
            item
            for item in current["product"]["media"]
            if item["media_id"] == promoted.json()["media_id"]
        )
        assert promoted_media["approval_status"] == "approved"
        assert promoted_media["alt_text"] is None


def test_observed_suggestion_keeps_owned_source_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("CATALOG_SOURCE_OUTPUT_DIR", str(tmp_path / "private-sources"))
    with _admin_catalog_client(monkeypatch) as (client, _sessions):
        bundle = client.post(
            "/api/admin/catalog/source-bundles",
            data={"title": "Observed suggestion evidence"},
            files=[("files", ("supplier.jpg", _image_bytes(), "image/jpeg"))],
        ).json()
        asset_id = bundle["assets"][0]["id"]
        payload = _v3_payload(title="Evidence-backed V3 Coat")
        payload["product"]["source_references"] = [
            {"bundle_id": bundle["id"], "asset_ids": [asset_id]}
        ]
        draft = _create_v3_draft(
            client,
            key="v3-evidence-draft",
            payload=payload,
        ).json()
        suggestion_set = _create_suggestion_set(
            client,
            product_id=draft["product_id"],
            draft_id=draft["id"],
            expected_draft_version=1,
            key="v3-observed-suggestion",
            suggestions=[
                {
                    "target_path": "/description",
                    "proposed_value": "A supplier-observed wool coat.",
                    "certainty_class": "observed",
                    "input_origin": "supplier_analysis",
                    "evidence_asset_ids": [asset_id],
                }
            ],
        ).json()
        suggestion = suggestion_set["suggestions"][0]
        assert suggestion["evidence_asset_ids"] == [asset_id]

        accepted = client.post(
            f"/api/admin/catalog/v3/products/{draft['product_id']}/suggestion-sets/"
            f"{suggestion_set['id']}/decisions",
            json={
                "action": "accept",
                "scope": "suggestion",
                "suggestion_id": suggestion["id"],
                "expected_draft_version": 1,
            },
            headers=_headers("accept-v3-observed-suggestion"),
        )
        assert accepted.status_code == 200
        accepted_suggestion = accepted.json()["suggestion_set"]["suggestions"][0]
        assert accepted_suggestion["status"] == "accepted"
        assert accepted_suggestion["evidence_asset_ids"] == [asset_id]
        assert accepted.json()["draft"]["product"]["description"] == (
            "A supplier-observed wool coat."
        )


def test_readiness_preview_and_workflow_projection_are_bounded(monkeypatch, tmp_path):
    monkeypatch.setenv("CATALOG_SOURCE_OUTPUT_DIR", str(tmp_path / "private-sources"))
    with _admin_catalog_client(monkeypatch) as (client, _sessions):
        workflow = client.post(
            "/api/admin/catalog/workflows",
            json={
                "title": "V3 authoring workflow",
                "business_summary": "Prepare structured product content.",
            },
            headers=_headers("v3-workflow"),
        ).json()
        payload = _v3_payload(media=False)
        payload["product"]["price_min"] = 0
        payload["product"]["price_max"] = 0
        payload["product"]["specifications"] = []
        payload["product"]["seo"] = {
            "title": None,
            "description": None,
            "keywords": [],
        }
        payload["product"]["metadata"] = {
            "supplier": {"storage_key": "private/supplier/front.jpg"}
        }
        created = _create_v3_draft(client, key="v3-readiness-draft", payload=payload)
        draft = created.json()
        product_id = draft["product_id"]

        suggestion_set = _create_suggestion_set(
            client,
            product_id=product_id,
            draft_id=draft["id"],
            expected_draft_version=1,
            workflow_id=workflow["id"],
            key="v3-workflow-suggestions",
            suggestions=[
                {
                    "target_path": "/seo/title",
                    "proposed_value": "A bounded SEO title",
                    "certainty_class": "derived",
                    "input_origin": "voice",
                    "evidence_asset_ids": [],
                }
            ],
        )
        assert suggestion_set.status_code == 201

        readiness = client.get(
            f"/api/admin/catalog/v3/products/{product_id}/drafts/{draft['id']}/readiness"
        )
        assert readiness.status_code == 200
        readiness_body = readiness.json()
        assert readiness_body["ready"] is False
        blocker_codes = {item["code"] for item in readiness_body["blocking_errors"]}
        assert {"missing_approved_media", "missing_price", "missing_required_specification"}.issubset(
            blocker_codes
        )
        recommendation_codes = {item["code"] for item in readiness_body["recommendations"]}
        assert {"missing_seo_title", "missing_seo_description"}.issubset(recommendation_codes)

        preview = client.get(
            f"/api/admin/catalog/v3/products/{product_id}/drafts/{draft['id']}/preview"
        )
        assert preview.status_code == 200
        assert "source_references" not in preview.text
        assert "readiness_inputs" not in preview.text
        assert "storage_key" not in preview.text

        workflow_projection = client.get(
            f"/api/admin/catalog/workflows/{workflow['id']}",
            params={"developer": True},
        )
        assert workflow_projection.status_code == 200
        assert workflow_projection.json()["suggestion_set_ids"] == [
            suggestion_set.json()["id"]
        ]
        assert "proposed_value" not in workflow_projection.text


def test_catalog_authoring_v3_migration_upgrade_and_downgrade(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'catalog-v3.db'}", future=True)
    with engine.begin() as connection:
        connection.execute(text("pragma foreign_keys=on"))
        connection.execute(text("create table catalog_draft_revisions (id varchar(64) primary key)"))
        connection.execute(text("create table catalog_workflows (id varchar(64) primary key)"))

    migration_path = (
        Path(__file__).parents[1]
        / "alembic/versions/b3c4d5e6f7a8_add_catalog_authoring_v3.py"
    )
    spec = importlib.util.spec_from_file_location("catalog_authoring_v3_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as connection:
        connection.execute(text("pragma foreign_keys=on"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

    assert {
        "catalog_suggestion_sets",
        "catalog_field_suggestions",
        "catalog_suggestion_reviews",
    }.issubset(inspect(engine).get_table_names())

    with engine.begin() as connection:
        connection.execute(text("pragma foreign_keys=on"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()

    tables = inspect(engine).get_table_names()
    assert "catalog_suggestion_sets" not in tables
    assert "catalog_field_suggestions" not in tables
    assert "catalog_suggestion_reviews" not in tables
    engine.dispose()
