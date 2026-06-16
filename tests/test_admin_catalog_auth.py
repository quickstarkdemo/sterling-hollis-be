from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app
from app.services.auth.admin import catalog_studio_capabilities, require_catalog_admin
from app.services.auth.clerk import (
    AuthenticatedPrincipal,
    ClerkAuthError,
    require_clerk_principal,
)


def _settings(**overrides) -> Settings:
    defaults = {
        "database_url": "postgresql+psycopg://postgres:postgres@localhost:5432/productdb",
        "catalog_studio_clerk_authorized_emails": "",
        "catalog_studio_clerk_authorized_subjects": "",
        "catalog_studio_admin_claim_path": "",
        "catalog_studio_admin_claim_value": "admin",
        "demo_observability_clerk_authorized_emails": "",
        "demo_observability_clerk_authorized_subjects": "",
        "clerk_demo_customer_email": None,
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def _principal(
    *,
    subject: str = "user_test",
    email: str | None = "presenter@example.com",
    claims: dict | None = None,
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        provider="clerk",
        provider_user_id=subject,
        email=email,
        claims=claims or {},
    )


def _client(settings: Settings, principal: AuthenticatedPrincipal | None = None) -> TestClient:
    app = create_app(settings=settings)
    app.dependency_overrides[get_settings] = lambda: settings
    if principal is not None:
        app.dependency_overrides[require_clerk_principal] = lambda: principal
    return TestClient(app)


def test_catalog_admin_accepts_allowlisted_subject():
    settings = _settings(catalog_studio_clerk_authorized_subjects="user_admin,user_other")

    authorized = require_catalog_admin(_principal(subject="user_admin"), settings)

    assert authorized.provider_user_id == "user_admin"


def test_catalog_admin_accepts_normalized_allowlisted_email():
    settings = _settings(catalog_studio_clerk_authorized_emails=" Presenter@Example.COM ")

    authorized = require_catalog_admin(_principal(email="presenter@example.com"), settings)

    assert authorized.email == "presenter@example.com"


def test_catalog_admin_preserves_existing_demo_email_allowlist():
    settings = _settings(clerk_demo_customer_email="presenter@example.com")

    authorized = require_catalog_admin(_principal(email="PRESENTER@example.com"), settings)

    assert authorized.email == "PRESENTER@example.com"


def test_catalog_admin_accepts_configured_nested_claim():
    settings = _settings(
        catalog_studio_admin_claim_path="metadata.role",
        catalog_studio_admin_claim_value="admin",
    )

    authorized = require_catalog_admin(
        _principal(claims={"metadata": {"role": "ADMIN"}}),
        settings,
    )

    assert authorized.provider_user_id == "user_test"


def test_catalog_admin_rejects_authenticated_non_admin():
    settings = _settings(catalog_studio_clerk_authorized_subjects="user_admin")

    client = _client(settings, _principal(subject="user_customer"))
    response = client.get("/api/admin/session")

    assert response.status_code == 403
    assert response.json() == {"detail": "Clerk user is not a Catalog Studio administrator."}


def test_catalog_admin_session_requires_clerk_token():
    settings = _settings(catalog_studio_clerk_authorized_subjects="user_admin")

    client = _client(settings)
    response = client.get("/api/admin/session")

    assert response.status_code == 401
    assert response.json() == {"detail": "Clerk session token is required."}


def test_catalog_admin_session_rejects_invalid_clerk_token_before_policy(monkeypatch):
    settings = _settings(catalog_studio_clerk_authorized_subjects="user_admin")

    def reject_token(token: str, settings: Settings | None = None):
        raise ClerkAuthError("Invalid Clerk session token.")

    monkeypatch.setattr("app.services.auth.clerk.verify_clerk_token", reject_token)
    client = _client(settings)
    response = client.get(
        "/api/admin/session",
        headers={"Authorization": "Bearer invalid-token"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid Clerk session token."}


def test_catalog_admin_session_reports_capabilities_without_secrets_or_probes():
    settings = _settings(
        catalog_studio_clerk_authorized_subjects="user_admin",
        openai_api_key="sk-secret-value",
        product_image_output_dir="data/product-images",
        catalog_studio_realtime_enabled=False,
    )

    client = _client(settings, _principal(subject="user_admin"))
    response = client.get("/api/admin/session")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "authorized": True,
        "capabilities": {
            "responses": {"configured": True},
            "moderation": {"configured": True},
            "image_generation": {"configured": True},
            "realtime": {"configured": False},
            "worker_storage": {"configured": True},
            "catalog": {"configured": True},
        },
    }
    assert "sk-secret-value" not in response.text
    assert settings.database_url not in response.text


def test_capability_status_reports_unconfigured_openai_without_probing():
    settings = _settings(openai_api_key=None, product_image_output_dir="")

    assert catalog_studio_capabilities(settings) == {
        "responses": {"configured": False},
        "moderation": {"configured": False},
        "image_generation": {"configured": False},
        "realtime": {"configured": False},
        "worker_storage": {"configured": False},
        "catalog": {"configured": True},
    }


def test_production_legacy_admin_route_requires_catalog_admin():
    settings = _settings(
        environment="production",
        enable_legacy_admin_routes=True,
        catalog_studio_clerk_authorized_subjects="user_admin",
    )

    client = _client(settings, _principal(subject="user_customer"))
    response = client.get("/admin/system/vector-status")

    assert response.status_code == 403


def test_disabled_legacy_admin_routes_are_not_mounted():
    settings = _settings(environment="production", enable_legacy_admin_routes=False)

    client = _client(settings, _principal(subject="user_admin"))
    response = client.get("/admin/system/vector-status")

    assert response.status_code == 404
