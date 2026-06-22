from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from fastapi import APIRouter, FastAPI

from app.config import Settings
from app.main import create_app
from app.services.api_capabilities import annotate_api_capability_routes
from app.services.capabilities import REGISTRY_VERSION


PUBLIC_SHOPPER_CONTRACTS = {
    ("/api/catalog", "get"),
    ("/api/catalog/categories", "get"),
    ("/api/categories", "get"),
    ("/api/stores/{store_id}/categories", "get"),
    ("/api/catalog/products", "get"),
    ("/api/products", "get"),
    ("/api/categories/{category}/products", "get"),
    ("/api/products/{product_id}", "get"),
    ("/api/products/{product_id}/related", "get"),
    ("/api/search/products", "get"),
    ("/api/recommendations/products", "post"),
    ("/api/image-analysis", "post"),
    ("/api/recommendations/image", "post"),
    ("/api/chat", "post"),
}


def _schema(**overrides) -> dict:
    defaults = {
        "database_url": "postgresql+psycopg://postgres:postgres@localhost:5432/productdb",
        "enable_mcp_adapter": False,
        "enable_openai_apps_ui": False,
        "enable_legacy_admin_routes": True,
    }
    defaults.update(overrides)
    settings = Settings(
        _env_file=None,
        **defaults,
    )
    return create_app(settings=settings).openapi()


def _operation(schema: Mapping, path: str, method: str) -> Mapping:
    return schema["paths"][path][method.lower()]


def _schema_property_names(schema: Mapping, node: object) -> set[str]:
    if not isinstance(node, Mapping):
        return set()
    if "$ref" in node:
        name = str(node["$ref"]).rsplit("/", 1)[-1]
        return _schema_property_names(schema, schema["components"]["schemas"][name])

    names = set()
    properties = node.get("properties")
    if isinstance(properties, Mapping):
        names.update(properties)
        for child in properties.values():
            names.update(_schema_property_names(schema, child))
    items = node.get("items")
    if isinstance(items, Mapping):
        names.update(_schema_property_names(schema, items))
    for combinator in ("allOf", "anyOf", "oneOf"):
        entries = node.get(combinator)
        if isinstance(entries, list):
            for entry in entries:
                names.update(_schema_property_names(schema, entry))
    return names


def _request_property_names(schema: Mapping, operation: Mapping) -> set[str]:
    names = {parameter["name"] for parameter in operation.get("parameters", [])}
    request_body = operation.get("requestBody", {})
    content = request_body.get("content", {}) if isinstance(request_body, Mapping) else {}
    for media_type in content.values():
        if isinstance(media_type, Mapping):
            names.update(_schema_property_names(schema, media_type.get("schema")))
    return names


def test_openapi_marks_primary_shopper_routes_with_capability_contract():
    schema = _schema()

    expected = {
        ("/api/catalog", "get"): "public.catalog.search",
        ("/api/products/{product_id}", "get"): "public.catalog.product_detail",
        ("/api/recommendations/products", "post"): "public.catalog.recommendations",
        ("/api/chat", "post"): "shopper.chat.turn",
    }

    for (path, method), capability_id in expected.items():
        operation = _operation(schema, path, method)
        assert operation["x-sterling-capability-id"] == capability_id
        assert operation["x-sterling-capability-version"] == REGISTRY_VERSION
        assert operation["x-sterling-api-surface"] == "public_shopper"
        assert operation["x-sterling-current-frontend-contract"] is True
        assert operation["x-sterling-legacy-compatibility"] is False


def test_openapi_marks_admin_trace_and_admin_catalog_surfaces():
    schema = _schema()

    admin_session = _operation(schema, "/api/admin/session", "get")
    assert admin_session["x-sterling-capability-id"] == "catalog_admin.session"
    assert admin_session["x-sterling-api-surface"] == "catalog_admin"
    assert admin_session["x-sterling-current-frontend-contract"] is True
    assert {"ClerkBearer": []} in admin_session["security"]

    trace_list = _operation(schema, "/api/admin/traces", "get")
    assert trace_list["x-sterling-capability-id"] == "developer_trace.read"
    assert trace_list["x-sterling-api-surface"] == "developer_trace"
    assert trace_list["x-sterling-current-frontend-contract"] is True
    assert {"ClerkBearer": []} in trace_list["security"]

    expected_admin_capabilities = {
        ("/api/admin/catalog/products/drafts", "post"): "catalog_admin.product.draft",
        ("/api/admin/catalog/v2/products/drafts", "post"): "catalog_admin.product.draft",
        ("/api/admin/catalog/v3/products/drafts", "post"): "catalog_admin.product.draft",
        ("/api/admin/catalog/assistant/query", "post"): "catalog_admin.assistant.query",
        ("/api/admin/catalog/source-bundles", "get"): "catalog_admin.catalog.manage",
        ("/api/admin/catalog/products/{product_id}/reviews", "get"): "catalog_admin.catalog.manage",
        ("/api/admin/catalog/products/{product_id}/publish", "post"): "catalog_admin.product.publish",
        ("/api/admin/catalog/v2/products/{product_id}/archive", "post"): "catalog_admin.product.publish",
    }
    for (path, method), capability_id in expected_admin_capabilities.items():
        operation = _operation(schema, path, method)
        assert operation["x-sterling-capability-id"] == capability_id
        assert operation["x-sterling-api-surface"] == "catalog_admin"
        assert {"ClerkBearer": []} in operation["security"]


def test_operator_compatibility_routes_are_protected_or_omitted_in_production():
    protected_schema = _schema(environment="production", enable_legacy_admin_routes=True)

    for path, method in (
        ("/admin/system/vector-status", "get"),
        ("/recommendations/customer", "post"),
        ("/feeds/products/openai", "get"),
    ):
        operation = _operation(protected_schema, path, method)
        assert operation["x-sterling-api-surface"] == "operator_compatibility"
        assert operation["x-sterling-auth-posture"] == "local_or_protected_operator"
        assert operation["x-sterling-current-frontend-contract"] is False
        assert operation["x-sterling-legacy-compatibility"] is True
        assert {"ClerkBearer": []} in operation["security"]

    omitted_schema = _schema(environment="production", enable_legacy_admin_routes=False)
    assert "/admin/system/vector-status" not in omitted_schema["paths"]
    assert "/recommendations/customer" not in omitted_schema["paths"]
    assert "/feeds/products/openai" not in omitted_schema["paths"]


def test_api_capability_annotation_uses_effective_included_router_paths():
    router = APIRouter()

    @router.get("/catalog")
    def catalog_alias() -> dict:
        return {}

    app = FastAPI()
    app.include_router(router, prefix="/api")
    annotate_api_capability_routes(app)

    operation = app.openapi()["paths"]["/api/catalog"]["get"]
    assert operation["x-sterling-capability-id"] == "public.catalog.search"


def test_public_shopper_contracts_do_not_accept_customer_id_inputs():
    schema = _schema()

    for path, method in PUBLIC_SHOPPER_CONTRACTS:
        operation = _operation(schema, path, method)
        assert operation["x-sterling-api-surface"] == "public_shopper"
        request_names = _request_property_names(schema, operation)
        assert "customer_id" not in request_names, (method, path)


def test_curated_frontend_yaml_matches_public_recommendation_contract():
    text = Path("docs/frontend-openapi.yaml").read_text(encoding="utf-8")

    assert "$ref: \"#/components/schemas/PublicProductRecommendationRequest\"" in text
    assert "x-sterling-capability-id: public.catalog.recommendations" in text
    public_schema = text.split("PublicProductRecommendationRequest:", 1)[1].split(
        "ProductRecommendationResponse:",
        1,
    )[0]
    assert "additionalProperties: false" in public_schema
    assert "customer_id:" not in public_schema


def test_curated_frontend_yaml_includes_chat_capability_metadata():
    text = Path("docs/frontend-openapi.yaml").read_text(encoding="utf-8")
    chat_response = text.split("ChatResponse:", 1)[1].split("ChatIntent:", 1)[0]

    assert "capability_id:" in chat_response
    assert "capability_surface:" in chat_response
    assert "persona:" in chat_response
