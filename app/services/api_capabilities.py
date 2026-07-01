from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts

from app.services.capabilities import REGISTRY_VERSION, Surface, get_capability


class ApiSurface(StrEnum):
    PUBLIC_SHOPPER = "public_shopper"
    CATALOG_ADMIN = "catalog_admin"
    DEVELOPER_TRACE = "developer_trace"
    OPERATOR_COMPATIBILITY = "operator_compatibility"


class AuthPosture(StrEnum):
    PUBLIC = "public"
    OPTIONAL_CLERK = "optional_clerk"
    CATALOG_ADMIN_CLERK = "catalog_admin_clerk"
    LOCAL_OR_PROTECTED_OPERATOR = "local_or_protected_operator"


class ContractStatus(StrEnum):
    CURRENT = "current"
    COMPATIBILITY = "compatibility"
    INTERNAL = "internal"
    DEPRECATED = "deprecated"


@dataclass(frozen=True)
class RouteCapability:
    capability_id: str
    api_surface: ApiSurface
    auth_posture: AuthPosture
    current_frontend_contract: bool
    legacy_compatibility: bool = False
    contract_status: ContractStatus = ContractStatus.CURRENT
    migration_target: str | None = None
    admin_generation: str | None = None

    @property
    def openapi_extra(self) -> dict[str, object]:
        capability = get_capability(self.capability_id)
        extras: dict[str, object] = {
            "x-sterling-capability-id": capability.id,
            "x-sterling-capability-version": REGISTRY_VERSION,
            "x-sterling-api-surface": self.api_surface.value,
            "x-sterling-auth-posture": self.auth_posture.value,
            "x-sterling-current-frontend-contract": self.current_frontend_contract,
            "x-sterling-legacy-compatibility": self.legacy_compatibility,
            "x-sterling-contract-status": self.contract_status.value,
            "x-sterling-personas": [persona.value for persona in capability.allowed_personas],
            "x-sterling-capability-surface": Surface.REST.value,
        }
        if self.migration_target:
            extras["x-sterling-migration-target"] = self.migration_target
        if self.admin_generation:
            extras["x-sterling-admin-generation"] = self.admin_generation
        return extras


_PUBLIC_CATALOG_SEARCH_PATHS = frozenset(
    {
        "/api/catalog",
        "/api/catalog/categories",
        "/api/categories",
        "/api/stores/{store_id}/categories",
        "/api/catalog/products",
        "/api/products",
        "/api/categories/{category}/products",
        "/api/search/products",
    }
)
_PUBLIC_PRODUCT_DETAIL_PATHS = frozenset({"/api/products/{product_id}"})
_PUBLIC_RECOMMENDATION_PATHS = frozenset(
    {
        "/api/products/{product_id}/related",
        "/api/recommendations/products",
    }
)
_PUBLIC_IMAGE_RECOMMENDATION_PATHS = frozenset(
    {
        "/api/image-analysis",
        "/api/recommendations/image",
    }
)
_CATALOG_ADMIN_DRAFT_PATHS = frozenset(
    {
        "/api/admin/catalog/products/drafts",
        "/api/admin/catalog/products/{product_id}/draft",
        "/api/admin/catalog/products/{product_id}/revisions",
        "/api/admin/catalog/v2/products/drafts",
        "/api/admin/catalog/v2/products/{product_id}/draft",
        "/api/admin/catalog/v2/products/{product_id}/revisions",
        "/api/admin/catalog/v3/products/drafts",
        "/api/admin/catalog/v3/products/{product_id}/draft",
        "/api/admin/catalog/v3/products/{product_id}/revisions",
        "/api/admin/catalog/workflows/{workflow_id}/draft-commands",
        "/api/admin/catalog/workflows/{workflow_id}/realtime/tool-calls",
        "/api/admin/catalog/workflows/{workflow_id}/realtime/v3/tool-calls",
    }
)
_CATALOG_ADMIN_PUBLISH_PATHS = frozenset(
    {
        "/api/admin/catalog/products/{product_id}/publish",
        "/api/admin/catalog/products/{product_id}/archive",
        "/api/admin/catalog/v2/products/{product_id}/publish",
        "/api/admin/catalog/v2/products/{product_id}/archive",
        "/api/admin/catalog/v3/products/{product_id}/publish",
    }
)

_CATALOG_ADMIN_COMPATIBILITY_TARGETS = {
    "/api/admin/catalog/products": "/api/admin/catalog/v3/products/{product_id}",
    "/api/admin/catalog/products/drafts": "/api/admin/catalog/v3/products/drafts",
    "/api/admin/catalog/products/{product_id}": "/api/admin/catalog/v3/products/{product_id}",
    "/api/admin/catalog/products/{product_id}/draft": "/api/admin/catalog/v3/products/{product_id}/draft",
    "/api/admin/catalog/products/{product_id}/revisions": "/api/admin/catalog/v3/products/{product_id}/revisions",
    "/api/admin/catalog/products/{product_id}/publish": "/api/admin/catalog/v3/products/{product_id}/publish",
    "/api/admin/catalog/products/{product_id}/archive": "/api/admin/catalog/v2/products/{product_id}/archive",
    "/api/admin/catalog/v2/products": "/api/admin/catalog/v3/products/{product_id}",
    "/api/admin/catalog/v2/products/drafts": "/api/admin/catalog/v3/products/drafts",
    "/api/admin/catalog/v2/products/{product_id}": "/api/admin/catalog/v3/products/{product_id}",
    "/api/admin/catalog/v2/products/{product_id}/draft": "/api/admin/catalog/v3/products/{product_id}/draft",
    "/api/admin/catalog/v2/products/{product_id}/revisions": "/api/admin/catalog/v3/products/{product_id}/revisions",
    "/api/admin/catalog/v2/products/{product_id}/publish": "/api/admin/catalog/v3/products/{product_id}/publish",
    "/api/admin/catalog/workflows/{workflow_id}/realtime/tool-calls": "/api/admin/catalog/workflows/{workflow_id}/realtime/v3/tool-calls",
}


def _catalog_admin_generation(path: str) -> str:
    if "/v3/" in path:
        return "v3"
    if "/v2/" in path:
        return "v2"
    if "/workflows/" in path or path.endswith("/workflows"):
        return "workflow"
    if "/source-bundles" in path:
        return "source-bundle"
    if "/reviews" in path:
        return "review"
    if path.endswith("/assistant/query"):
        return "assistant"
    return "legacy"


def _catalog_admin_route(capability_id: str, path: str) -> RouteCapability:
    migration_target = _CATALOG_ADMIN_COMPATIBILITY_TARGETS.get(path)
    is_compatibility = migration_target is not None
    return RouteCapability(
        capability_id,
        ApiSurface.CATALOG_ADMIN,
        AuthPosture.CATALOG_ADMIN_CLERK,
        current_frontend_contract=not is_compatibility,
        legacy_compatibility=is_compatibility,
        contract_status=(
            ContractStatus.COMPATIBILITY
            if is_compatibility
            else ContractStatus.CURRENT
        ),
        migration_target=migration_target,
        admin_generation=_catalog_admin_generation(path),
    )


def route_capability_for(path: str, method: str) -> RouteCapability | None:
    normalized_method = method.upper()
    if normalized_method in {"HEAD", "OPTIONS"}:
        return None

    if path in _PUBLIC_CATALOG_SEARCH_PATHS:
        return RouteCapability(
            "public.catalog.search",
            ApiSurface.PUBLIC_SHOPPER,
            AuthPosture.PUBLIC,
            current_frontend_contract=True,
        )
    if path in _PUBLIC_PRODUCT_DETAIL_PATHS:
        return RouteCapability(
            "public.catalog.product_detail",
            ApiSurface.PUBLIC_SHOPPER,
            AuthPosture.PUBLIC,
            current_frontend_contract=True,
        )
    if path in _PUBLIC_RECOMMENDATION_PATHS:
        return RouteCapability(
            "public.catalog.recommendations",
            ApiSurface.PUBLIC_SHOPPER,
            AuthPosture.PUBLIC,
            current_frontend_contract=True,
        )
    if path in _PUBLIC_IMAGE_RECOMMENDATION_PATHS:
        return RouteCapability(
            "public.catalog.image_recommendations",
            ApiSurface.PUBLIC_SHOPPER,
            AuthPosture.PUBLIC,
            current_frontend_contract=True,
        )
    if path in {
        "/api/chat",
        "/api/chat/realtime/capability",
        "/api/chat/realtime/sessions",
        "/api/chat/realtime/tool-calls",
    }:
        return RouteCapability(
            "shopper.chat.turn",
            ApiSurface.PUBLIC_SHOPPER,
            AuthPosture.OPTIONAL_CLERK,
            current_frontend_contract=True,
        )
    if path == "/api/admin/session":
        return RouteCapability(
            "catalog_admin.session",
            ApiSurface.CATALOG_ADMIN,
            AuthPosture.CATALOG_ADMIN_CLERK,
            current_frontend_contract=True,
            admin_generation="session",
        )
    if path.startswith("/api/admin/traces"):
        return RouteCapability(
            "developer_trace.read",
            ApiSurface.DEVELOPER_TRACE,
            AuthPosture.CATALOG_ADMIN_CLERK,
            current_frontend_contract=True,
        )
    if path in _CATALOG_ADMIN_DRAFT_PATHS:
        return _catalog_admin_route("catalog_admin.product.draft", path)
    if path in _CATALOG_ADMIN_PUBLISH_PATHS:
        return _catalog_admin_route("catalog_admin.product.publish", path)
    if path == "/api/admin/catalog/assistant/query":
        return _catalog_admin_route("catalog_admin.assistant.query", path)
    if path.startswith("/api/admin/catalog"):
        return _catalog_admin_route("catalog_admin.catalog.manage", path)
    if path.startswith("/api/demo/observability"):
        return RouteCapability(
            "operator_compatibility.demo_observability",
            ApiSurface.OPERATOR_COMPATIBILITY,
            AuthPosture.CATALOG_ADMIN_CLERK,
            current_frontend_contract=False,
            legacy_compatibility=True,
        )
    if path.startswith("/admin"):
        return RouteCapability(
            "operator_compatibility.admin",
            ApiSurface.OPERATOR_COMPATIBILITY,
            AuthPosture.LOCAL_OR_PROTECTED_OPERATOR,
            current_frontend_contract=False,
            legacy_compatibility=True,
        )
    if path.startswith("/recommendations"):
        return RouteCapability(
            "operator_compatibility.recommendations",
            ApiSurface.OPERATOR_COMPATIBILITY,
            AuthPosture.LOCAL_OR_PROTECTED_OPERATOR,
            current_frontend_contract=False,
            legacy_compatibility=True,
        )
    if path == "/feeds/products/openai":
        return RouteCapability(
            "operator_compatibility.product_feed",
            ApiSurface.OPERATOR_COMPATIBILITY,
            AuthPosture.LOCAL_OR_PROTECTED_OPERATOR,
            current_frontend_contract=False,
            legacy_compatibility=True,
        )
    return None


def annotate_api_capability_routes(app: FastAPI) -> None:
    for path, route, effective_context in _iter_effective_api_routes(app.routes):
        extras: dict[str, object] = {}
        for method in sorted(route.methods):
            route_capability = route_capability_for(path, method)
            if route_capability is not None:
                extras.update(route_capability.openapi_extra)
                break
        if not extras:
            continue
        route.openapi_extra = {**(route.openapi_extra or {}), **extras}
        if effective_context is not None:
            effective_context.openapi_extra = {
                **(effective_context.openapi_extra or {}),
                **extras,
            }


def _iter_effective_api_routes(routes: Iterable[object]) -> Iterable[tuple[str, APIRoute, object | None]]:
    for context in iter_route_contexts(list(routes)):
        route = context.original_route
        if isinstance(route, APIRoute):
            yield context.path, route, getattr(context, "_route_context", None)
