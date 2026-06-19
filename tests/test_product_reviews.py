from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, select, text

from app.catalog.review_schemas import ReviewAIProposal
from app.config import get_settings
from app.models import CatalogProduct, CatalogProductReview, CatalogReviewAction
from app.routers.admin_catalog import get_product_review_ai_service
from app.services.auth.clerk import require_clerk_principal
from app.services.product_reviews import ProductReviewAIService, import_product_review
from tests.test_admin_catalog_api import _admin_catalog_client, _headers


def _moderation(*, flagged: bool = False):
    return SimpleNamespace(
        type="moderation_result",
        flagged=flagged,
        model="omni-moderation-latest",
        categories={"harassment": flagged},
        category_scores={"harassment": 0.95 if flagged else 0.01},
    )


class _Responses:
    def __init__(
        self,
        *,
        proposal: ReviewAIProposal | None = None,
        error: Exception | None = None,
        flagged: bool = False,
    ):
        self.proposal = proposal
        self.error = error
        self.flagged = flagged
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        moderation = _moderation(flagged=self.flagged)
        return SimpleNamespace(
            id="resp_review_1",
            model="gpt-5.5",
            status="completed",
            output_parsed=self.proposal,
            moderation=SimpleNamespace(input=moderation, output=moderation),
            usage=SimpleNamespace(model_dump=lambda **_: {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}),
        )


def _seed_review(sessions, *, suffix: str = "one"):
    with sessions() as db:
        product_id = db.scalar(select(CatalogProduct.id).order_by(CatalogProduct.id))
        review = import_product_review(
            db,
            product_id=product_id,
            source="synthetic_fixture",
            external_review_id=f"review-{suffix}",
            author_display_name="Maya R.",
            body="The material feels substantial and the finish is beautiful.",
            rating=5,
            submitted_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
        )
        return product_id, review.id


def test_assistance_and_decisions_preserve_authorship_and_publish_only_approved_content(monkeypatch):
    proposal = ReviewAIProposal(
        categories=["product_quality"],
        theme_summary="Positive feedback about material quality.",
        suggested_action="approve",
        response_draft="Thank you for sharing your experience.",
    )
    responses = _Responses(proposal=proposal)
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        product_id, review_id = _seed_review(sessions)
        settings = client.app.dependency_overrides[get_settings]()
        client.app.dependency_overrides[get_product_review_ai_service] = lambda: ProductReviewAIService(
            settings, client=SimpleNamespace(responses=responses)
        )

        assisted = client.post(
            f"/api/admin/catalog/products/{product_id}/reviews/{review_id}/assist",
            json={"expected_version": 1},
            headers=_headers("assist-review-one"),
        )
        assert assisted.status_code == 200, assisted.text
        assisted_body = assisted.json()
        assert assisted_body["body"] == "The material feels substantial and the finish is beautiful."
        assert assisted_body["rating"] == 5
        assert assisted_body["moderation"]["state"] == "pending"
        assert assisted_body["moderation"]["version"] == 2
        assert assisted_body["moderation"]["ai_categories"] == ["product_quality"]
        assert assisted_body["moderation"]["ai_provider_metadata"] == {
            "request_id": "resp_review_1",
            "model": "gpt-5.5",
            "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
            "moderation": {"decision": "approved", "categories": []},
        }
        assisted_replay = client.post(
            f"/api/admin/catalog/products/{product_id}/reviews/{review_id}/assist",
            json={"expected_version": 1},
            headers=_headers("assist-review-one"),
        )
        assert assisted_replay.json() == assisted_body
        assert len(responses.calls) == 1

        approved = client.post(
            f"/api/admin/catalog/products/{product_id}/reviews/{review_id}/decisions",
            json={"action": "approve", "expected_version": 2, "reason": "Verified customer feedback."},
            headers=_headers("approve-review-one"),
        )
        assert approved.status_code == 200
        assert approved.json()["moderation"]["state"] == "approved"
        replay = client.post(
            f"/api/admin/catalog/products/{product_id}/reviews/{review_id}/decisions",
            json={"action": "approve", "expected_version": 2, "reason": "Verified customer feedback."},
            headers=_headers("approve-review-one"),
        )
        assert replay.status_code == 200
        assert replay.json() == approved.json()

        stale = client.post(
            f"/api/admin/catalog/products/{product_id}/reviews/{review_id}/decisions",
            json={"action": "flag", "expected_version": 2, "reason": "Stale decision."},
            headers=_headers("stale-review-one"),
        )
        assert stale.status_code == 409

        saved_response = client.post(
            f"/api/admin/catalog/products/{product_id}/reviews/{review_id}/decisions",
            json={
                "action": "save_response",
                "expected_version": 3,
                "reason": "Merchant edited the response.",
                "response_text": "We appreciate your thoughtful review.",
            },
            headers=_headers("save-response-one"),
        )
        assert saved_response.status_code == 200
        published_response = client.post(
            f"/api/admin/catalog/products/{product_id}/reviews/{review_id}/decisions",
            json={"action": "publish_response", "expected_version": 4, "reason": "Approved response copy."},
            headers=_headers("publish-response-one"),
        )
        assert published_response.status_code == 200

        public = client.get(f"/api/products/{product_id}")
        assert public.status_code == 200
        public_review = public.json()["reviews"][0]
        assert public_review == {
            "id": review_id,
            "author_display_name": "Maya R.",
            "body": "The material feels substantial and the finish is beautiful.",
            "rating": 5,
            "submitted_at": "2026-06-19T00:00:00",
            "merchant_response": "We appreciate your thoughtful review.",
            "merchant_responded_at": public_review["merchant_responded_at"],
        }
        assert public_review["merchant_responded_at"]

        rejected = client.post(
            f"/api/admin/catalog/products/{product_id}/reviews/{review_id}/decisions",
            json={"action": "reject", "expected_version": 5, "reason": "Later trust review failed."},
            headers=_headers("reject-review-one"),
        )
        assert rejected.status_code == 200
        assert client.get(f"/api/products/{product_id}").json()["reviews"] == []

        with sessions() as db:
            stored = db.get(CatalogProductReview, review_id)
            assert stored.body == "The material feels substantial and the finish is beautiful."
            assert stored.rating == 5
            actions = db.scalars(
                select(CatalogReviewAction).where(CatalogReviewAction.review_id == review_id)
            ).all()
            assert [row.action for row in actions] == ["assist", "approve", "save_response", "publish_response", "reject"]
            assert all(row.actor_provider_user_id == "user_admin" for row in actions)


def test_provider_failure_and_blocked_output_preserve_review_state(monkeypatch):
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        product_id, review_id = _seed_review(sessions, suffix="failure")
        settings = client.app.dependency_overrides[get_settings]()
        failed = _Responses(error=TimeoutError("provider timeout"))
        client.app.dependency_overrides[get_product_review_ai_service] = lambda: ProductReviewAIService(
            settings, client=SimpleNamespace(responses=failed)
        )
        response = client.post(
            f"/api/admin/catalog/products/{product_id}/reviews/{review_id}/assist",
            json={"expected_version": 1},
            headers=_headers("assist-review-failure"),
        )
        assert response.status_code == 502

        blocked = _Responses(
            proposal=ReviewAIProposal(
                categories=["abuse"], theme_summary="Unsafe content.", suggested_action="reject"
            ),
            flagged=True,
        )
        client.app.dependency_overrides[get_product_review_ai_service] = lambda: ProductReviewAIService(
            settings, client=SimpleNamespace(responses=blocked)
        )
        response = client.post(
            f"/api/admin/catalog/products/{product_id}/reviews/{review_id}/assist",
            json={"expected_version": 1},
            headers=_headers("assist-review-blocked"),
        )
        assert response.status_code == 422
        current = client.get(f"/api/admin/catalog/products/{product_id}/reviews").json()["items"][0]
        assert current["moderation"]["version"] == 1
        assert current["moderation"]["state"] == "pending"
        assert current["moderation"]["ai_categories"] == []


def test_trusted_import_cannot_rewrite_existing_review_authorship(monkeypatch):
    with _admin_catalog_client(monkeypatch) as (_, sessions):
        product_id, _ = _seed_review(sessions, suffix="immutable")
        with sessions() as db, pytest.raises(HTTPException) as exc:
            import_product_review(
                db,
                product_id=product_id,
                source="synthetic_fixture",
                external_review_id="review-immutable",
                author_display_name="Changed author",
                body="Changed customer content.",
                rating=1,
                submitted_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
            )
        assert exc.value.status_code == 409


def test_review_admin_routes_require_authorization_and_reject_authorship_mutation(monkeypatch):
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        product_id, review_id = _seed_review(sessions, suffix="auth")
        invalid_assist = client.post(
            f"/api/admin/catalog/products/{product_id}/reviews/{review_id}/assist",
            json={"expected_version": 1, "body": "Changed customer words."},
            headers=_headers("invalid-assist-authorship-edit"),
        )
        assert invalid_assist.status_code == 422

        invalid = client.post(
            f"/api/admin/catalog/products/{product_id}/reviews/{review_id}/decisions",
            json={
                "action": "approve",
                "expected_version": 1,
                "reason": "Attempted edit.",
                "body": "Changed customer words.",
                "rating": 1,
            },
            headers=_headers("invalid-authorship-edit"),
        )
        assert invalid.status_code == 422

        client.app.dependency_overrides.pop(require_clerk_principal)
        unauthorized = client.get(f"/api/admin/catalog/products/{product_id}/reviews")
        assert unauthorized.status_code in {401, 403}


def test_product_review_migration_upgrade_and_downgrade(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'product-reviews.db'}", future=True)
    with engine.begin() as connection:
        connection.execute(text("pragma foreign_keys=on"))
        connection.execute(text("create table catalog_products (id varchar(64) primary key)"))

    migration_path = Path(__file__).parents[1] / "alembic/versions/c4d5e6f7a8b9_add_product_review_moderation.py"
    spec = importlib.util.spec_from_file_location("product_review_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
    assert {
        "catalog_product_reviews",
        "catalog_review_moderations",
        "catalog_review_actions",
    }.issubset(inspect(engine).get_table_names())

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()
    tables = inspect(engine).get_table_names()
    assert "catalog_product_reviews" not in tables
    assert "catalog_review_moderations" not in tables
    assert "catalog_review_actions" not in tables
    engine.dispose()
