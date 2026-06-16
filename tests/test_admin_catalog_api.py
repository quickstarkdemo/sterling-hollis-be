from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings, get_settings
from app.catalog.ai_schemas import (
    CatalogAIInventoryProposal,
    CatalogAIProductProposal,
    CatalogAIVariantProposal,
)
from app.database import Base, get_db
from app.main import create_app
from app.routers.admin_catalog import get_catalog_ai_service
from app.models import (
    CatalogDraftRevision,
    CatalogProduct,
    CatalogWorkflowEvent,
    Product,
    Store,
    SyntheticRun,
)
from app.services.auth.clerk import AuthenticatedPrincipal, require_clerk_principal
from app.services.catalog_ai import CatalogAIService
from app.services.catalog_normalization import backfill_catalog_from_legacy_products


@contextmanager
def _admin_catalog_client(monkeypatch):
    monkeypatch.setenv("ENABLE_MCP_ADAPTER", "false")
    monkeypatch.setenv("ENABLE_OPENAI_APPS_UI", "false")
    get_settings.cache_clear()
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        catalog_studio_clerk_authorized_subjects="user_admin",
        enable_mcp_adapter=False,
        enable_openai_apps_ui=False,
    )
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    app = create_app(settings=settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[require_clerk_principal] = lambda: AuthenticatedPrincipal(
        provider="clerk", provider_user_id="user_admin", email="admin@example.com", claims={}
    )

    def override_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    db = testing_session()
    try:
        now = datetime(2026, 6, 16, tzinfo=timezone.utc)
        db.add(SyntheticRun(id="run_catalog", seed=101, status="loaded", started_at=now, config={}))
        db.add(
            Store(
                id="1001",
                seed_run_id="run_catalog",
                name="Dallas Downtown",
                city="Dallas",
                state="TX",
                postal_code="75201",
                address_line1="1 Main St",
                profile_type="texas_core",
                services=[],
                raw_source={},
            )
        )
        db.add(
            Product(
                id="prod_existing",
                seed_run_id="run_catalog",
                store_id="1001",
                title="Published Dress",
                description="The public description",
                link="https://example.com/published",
                image_link="https://example.com/published.jpg",
                price=Decimal("100.00"),
                availability="in stock",
                brand="Sterling Hollis",
                category="womens_apparel",
                color="Blue",
                size="M",
                material="silk",
                gender="women",
                season="spring",
                margin_pct=Decimal("0.5000"),
                inventory_qty=5,
                objective_weight=Decimal("0.8000"),
                metadata_json={},
            )
        )
        db.commit()
        backfill_catalog_from_legacy_products(db, run_id="run_catalog")
        yield TestClient(app), testing_session
    finally:
        db.close()
        app.dependency_overrides.clear()
        engine.dispose()
        get_settings.cache_clear()


def _snapshot(*, title="Studio Coat", product_id=None, moderation_state="approved"):
    product = {
        "seed_run_id": "run_catalog",
        "title": title,
        "description": "A structured catalog studio product.",
        "brand": "Sterling Hollis",
        "category": "womens_apparel",
        "metadata": {"source": "catalog_studio"},
        "variants": [
            {
                "color": "Black",
                "material": "wool",
                "gender": "women",
                "season": "fall",
                "price_min": 250,
                "price_max": 250,
                "link": "https://example.com/studio-coat",
                "image_link": "https://example.com/studio-coat.jpg",
                "image_set": {"primary_url": "https://example.com/studio-coat.jpg"},
                "inventory": [
                    {
                        "store_id": "1001",
                        "size": "M",
                        "availability": "in stock",
                        "inventory_qty": 8,
                        "objective_weight": 0.9,
                    }
                ],
            }
        ],
    }
    if product_id:
        product["product_id"] = product_id
    return {"expected_version": 0, "moderation_state": moderation_state, "product": product}


def _headers(key: str):
    return {"Idempotency-Key": key}


def test_catalog_mutations_require_catalog_admin(monkeypatch):
    with _admin_catalog_client(monkeypatch) as (client, _):
        client.app.dependency_overrides.pop(require_clerk_principal)
        response = client.post(
            "/api/admin/catalog/products/drafts",
            json=_snapshot(),
            headers=_headers("unauthorized-draft"),
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Clerk session token is required."}


def test_openapi_exposes_clerk_bearer_authorization_for_admin_routes(monkeypatch):
    with _admin_catalog_client(monkeypatch) as (client, _):
        schema = client.get("/openapi.json").json()

    security_scheme = schema["components"]["securitySchemes"]["ClerkBearer"]
    assert security_scheme["type"] == "http"
    assert security_scheme["scheme"] == "bearer"
    for path in (
        "/api/admin/session",
        "/api/admin/catalog/products/drafts",
        "/api/admin/catalog/workflows",
        "/api/admin/catalog/workflows/{workflow_id}",
        "/api/admin/catalog/workflows/{workflow_id}/draft-commands",
    ):
        operations = schema["paths"][path]
        assert all(
            {"ClerkBearer": []} in operation.get("security", [])
            for operation in operations.values()
            if isinstance(operation, dict)
        )
    assert not any(path.startswith("/api/admin/catalog/demo-runs") for path in schema["paths"])


def test_catalog_ai_command_api_returns_draft_and_business_timeline(monkeypatch):
    proposal = CatalogAIProductProposal(
        title="Midnight Atelier Coat",
        description="A sculpted wool evening coat.",
        brand="Sterling Hollis",
        category="womens_apparel",
        image_direction="Editorial studio photograph on a neutral backdrop.",
        variants=[
            CatalogAIVariantProposal(
                color="Black",
                material="wool",
                gender="women",
                season="fall",
                price_min=895,
                price_max=895,
                inventory=[
                    CatalogAIInventoryProposal(
                        size="M",
                        availability="in stock",
                        inventory_qty=8,
                        objective_weight=0.9,
                    )
                ],
            )
        ],
    )
    moderation = SimpleNamespace(
        type="moderation_result",
        flagged=False,
        model="omni-moderation-latest",
        categories={"violence": False},
        category_scores={"violence": 0.001},
        category_applied_input_types={"violence": ["text"]},
    )
    response = SimpleNamespace(
        id="resp_api_catalog_1",
        model="gpt-5.5-2026-05-01",
        status="completed",
        output_parsed=proposal,
        moderation=SimpleNamespace(input=moderation, output=moderation),
        usage=SimpleNamespace(
            model_dump=lambda mode="json": {
                "input_tokens": 20,
                "output_tokens": 15,
                "total_tokens": 35,
            }
        ),
    )

    class FakeResponses:
        def parse(self, **_kwargs):
            return response

    fake_client = SimpleNamespace(responses=FakeResponses())
    ai_settings = Settings(
        _env_file=None,
        openai_api_key="test-key",
        catalog_studio_responses_model="gpt-5.5",
        catalog_studio_moderation_model="omni-moderation-latest",
    )

    with _admin_catalog_client(monkeypatch) as (client, sessions):
        client.app.dependency_overrides[get_catalog_ai_service] = lambda: CatalogAIService(
            ai_settings, fake_client
        )
        started = client.post(
            "/api/admin/catalog/workflows",
            headers=_headers("api-ai-workflow"),
            json={
                "title": "Create a customer-facing coat",
                "business_summary": "Preparing the product draft.",
            },
        )
        workflow_id = started.json()["id"]

        created = client.post(
            f"/api/admin/catalog/workflows/{workflow_id}/draft-commands",
            headers=_headers("api-ai-command"),
            json={
                "instruction": "Create a black wool evening coat.",
                "expected_draft_version": 0,
            },
        )

        assert created.status_code == 200
        body = created.json()
        assert body["status"] == "succeeded"
        assert body["draft"]["draft_version"] == 1
        assert body["draft"]["product"]["title"] == "Midnight Atelier Coat"
        assert body["workflow"]["draft_id"] == body["draft"]["id"]
        assert [event["capability"] for event in body["workflow"]["events"]] == [
            "workflow",
            "moderation",
            "responses",
        ]
        assert all("developer" not in event for event in body["workflow"]["events"])
        with sessions() as db:
            revision = db.get(CatalogDraftRevision, body["draft"]["id"])
            assert revision is not None
            assert revision.moderation_state == "approved"


def test_catalog_ai_command_api_reports_unavailable_responses_without_a_draft(monkeypatch):
    unavailable_settings = Settings(_env_file=None, openai_api_key=None)

    with _admin_catalog_client(monkeypatch) as (client, sessions):
        client.app.dependency_overrides[get_catalog_ai_service] = lambda: CatalogAIService(
            unavailable_settings
        )
        started = client.post(
            "/api/admin/catalog/workflows",
            headers=_headers("api-ai-unavailable-workflow"),
            json={
                "title": "Create a customer-facing coat",
                "business_summary": "Preparing the product draft.",
            },
        )
        workflow_id = started.json()["id"]

        response = client.post(
            f"/api/admin/catalog/workflows/{workflow_id}/draft-commands",
            headers=_headers("api-ai-unavailable-command"),
            json={
                "instruction": "Create a black wool evening coat.",
                "expected_draft_version": 0,
            },
        )

        assert response.status_code == 503
        assert response.json() == {
            "detail": {
                "code": "responses_unavailable",
                "message": "The Responses capability is not configured.",
                "retryable": False,
            }
        }
        with sessions() as db:
            assert db.scalars(select(CatalogDraftRevision)).all() == []
            failure = db.scalar(
                select(CatalogWorkflowEvent).where(
                    CatalogWorkflowEvent.workflow_id == workflow_id,
                    CatalogWorkflowEvent.error_code == "responses_unavailable",
                )
            )
            assert failure is not None


def test_catalog_workflow_api_defaults_to_business_view_and_sanitizes_developer_view(monkeypatch):
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        started = client.post(
            "/api/admin/catalog/workflows",
            headers=_headers("start-demo-workflow-1"),
            json={
                "title": "Create a demo coat",
                "business_summary": "Preparing a product draft.",
            },
        )
        assert started.status_code == 201
        workflow_id = started.json()["id"]
        assert "developer" not in started.json()["events"][0]

        event = client.post(
            f"/api/admin/catalog/workflows/{workflow_id}/events",
            json={
                "client_event_id": "responses-api-1",
                "stage": "draft",
                "capability": "responses",
                "status": "succeeded",
                "business_summary": "The product draft is ready.",
                "model": "gpt-5.4",
                "request_id": "req_api_123",
                "usage": {"input_tokens": 12, "output_tokens": 8, "ignored": 99},
                "request_payload": {
                    "headers": {"authorization": "Bearer browser-secret"},
                    "product": {"title": "Demo Coat", "category": "outerwear"},
                },
                "response_payload": {"draft_id": "draft_demo", "status": "ready"},
            },
        )
        business = client.get(f"/api/admin/catalog/workflows/{workflow_id}")
        developer = client.get(
            f"/api/admin/catalog/workflows/{workflow_id}", params={"developer": "true"}
        )

        assert event.status_code == 201
        assert business.status_code == developer.status_code == 200
        assert "developer" not in business.json()["events"][-1]
        detail = developer.json()["events"][-1]["developer"]
        assert detail["model"] == "gpt-5.4"
        assert detail["usage"] == {"input_tokens": 12, "output_tokens": 8}
        assert detail["request_payload"]["product"]["title"] == "Demo Coat"
        assert "browser-secret" not in developer.text
        with sessions() as db:
            persisted = db.scalar(
                select(CatalogWorkflowEvent).where(
                    CatalogWorkflowEvent.workflow_id == workflow_id,
                    CatalogWorkflowEvent.sequence == 2,
                )
            )
            assert persisted is not None
            assert "browser-secret" not in str(persisted.request_json)


def test_new_draft_is_private_until_atomic_publish(monkeypatch):
    with _admin_catalog_client(monkeypatch) as (client, _):
        before = client.get("/api/products").json()
        created = client.post(
            "/api/admin/catalog/products/drafts",
            json=_snapshot(),
            headers=_headers("draft-new-1"),
        )
        assert created.status_code == 201
        product_id = created.json()["product_id"]
        draft_id = created.json()["id"]

        private = client.get("/api/products").json()
        detail = client.get(f"/api/products/{product_id}")
        search = client.get("/api/search/products", params={"q": "Studio Coat"}).json()
        assert private["total"] == before["total"]
        assert detail.status_code == 404
        assert search["total"] == 0

        published = client.post(
            f"/api/admin/catalog/products/{product_id}/publish",
            json={"draft_id": draft_id, "expected_version": 0},
            headers=_headers("publish-new-1"),
        )
        assert published.status_code == 200
        assert published.json()["version"] == 1

        public = client.get(f"/api/products/{product_id}")
        assert public.status_code == 200
        assert public.json()["title"] == "Studio Coat"
        assert public.json()["variants"][0]["inventory"][0]["inventory_qty"] == 8


def test_revision_keeps_existing_snapshot_public_until_publish(monkeypatch):
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        with sessions() as db:
            product_id = db.scalar(select(CatalogProduct.id).where(CatalogProduct.title == "Published Dress"))

        payload = _snapshot(title="Revised Dress", product_id=product_id)
        payload["expected_version"] = 1
        draft = client.put(
            f"/api/admin/catalog/products/{product_id}/draft",
            json=payload,
            headers=_headers("draft-existing-1"),
        )
        assert draft.status_code == 201
        assert client.get(f"/api/products/{product_id}").json()["title"] == "Published Dress"

        published = client.post(
            f"/api/admin/catalog/products/{product_id}/publish",
            json={"draft_id": draft.json()["id"], "expected_version": 1},
            headers=_headers("publish-existing-1"),
        )
        assert published.status_code == 200
        assert published.json()["version"] == 2
        assert client.get(f"/api/products/{product_id}").json()["title"] == "Revised Dress"


def test_idempotency_replays_result_and_rejects_key_reuse(monkeypatch):
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        first = client.post(
            "/api/admin/catalog/products/drafts",
            json=_snapshot(),
            headers=_headers("same-key"),
        )
        replay = client.post(
            "/api/admin/catalog/products/drafts",
            json=_snapshot(),
            headers=_headers("same-key"),
        )
        mismatch = client.post(
            "/api/admin/catalog/products/drafts",
            json=_snapshot(title="Different"),
            headers=_headers("same-key"),
        )

        assert first.status_code == replay.status_code == 201
        assert first.json() == replay.json()
        assert mismatch.status_code == 409
        with sessions() as db:
            assert len(db.scalars(select(CatalogDraftRevision)).all()) == 1


def test_stale_version_and_blocked_moderation_preserve_public_state(monkeypatch):
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        with sessions() as db:
            product_id = db.scalar(select(CatalogProduct.id).where(CatalogProduct.title == "Published Dress"))

        payload = _snapshot(title="Blocked Dress", product_id=product_id, moderation_state="blocked")
        payload["expected_version"] = 1
        draft = client.put(
            f"/api/admin/catalog/products/{product_id}/draft",
            json=payload,
            headers=_headers("blocked-draft"),
        )
        blocked = client.post(
            f"/api/admin/catalog/products/{product_id}/publish",
            json={"draft_id": draft.json()["id"], "expected_version": 1},
            headers=_headers("blocked-publish"),
        )
        stale = client.post(
            f"/api/admin/catalog/products/{product_id}/archive",
            json={"expected_version": 0},
            headers=_headers("stale-archive"),
        )

        assert blocked.status_code == 409
        assert stale.status_code == 409
        detail = client.get(f"/api/products/{product_id}")
        assert detail.status_code == 200
        assert detail.json()["title"] == "Published Dress"


def test_incomplete_draft_cannot_partially_publish(monkeypatch):
    with _admin_catalog_client(monkeypatch) as (client, _):
        payload = _snapshot()
        payload["product"]["variants"][0]["image_link"] = None
        payload["product"]["variants"][0]["image_set"] = {}
        draft = client.post(
            "/api/admin/catalog/products/drafts",
            json=payload,
            headers=_headers("incomplete-draft"),
        )

        response = client.post(
            f"/api/admin/catalog/products/{draft.json()['product_id']}/publish",
            json={"draft_id": draft.json()["id"], "expected_version": 0},
            headers=_headers("incomplete-publish"),
        )

        assert response.status_code == 409
        assert client.get(f"/api/products/{draft.json()['product_id']}").status_code == 404


def test_archive_removes_public_visibility_but_retains_admin_history(monkeypatch):
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        with sessions() as db:
            product_id = db.scalar(select(CatalogProduct.id).where(CatalogProduct.title == "Published Dress"))

        archived = client.post(
            f"/api/admin/catalog/products/{product_id}/archive",
            json={"expected_version": 1},
            headers=_headers("archive-1"),
        )

        assert archived.status_code == 200
        assert archived.json()["lifecycle_status"] == "archived"
        assert client.get(f"/api/products/{product_id}").status_code == 404
        assert client.get("/api/search/products", params={"q": "Published Dress"}).json()["total"] == 0
        assert product_id not in {
            row["id"] for row in client.get("/api/products", params={"limit": 100}).json()["items"]
        }
        recommendations = client.post(
            "/api/recommendations/products",
            json={"category": "womens_apparel", "top_k": 50},
        ).json()["recommendations"]
        assert product_id not in {row["product"]["id"] for row in recommendations}
        history = client.get(f"/api/admin/catalog/products/{product_id}")
        assert history.status_code == 200
        assert history.json()["lifecycle_status"] == "archived"
        assert history.json()["version"] == 2
