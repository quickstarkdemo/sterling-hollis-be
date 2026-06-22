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


@dataclass(frozen=True)
class RouteCapability:
    capability_id: str
    api_surface: ApiSurface
    auth_posture: AuthPosture
    current_frontend_contract: bool
    legacy_compatibility: bool = False

    @property
    def openapi_extra(self) -> dict[str, object]:
        capability = get_capability(self.capability_id)
        return {
            "x-sterling-capability-id": capability.id,
            "x-sterling-capability-version": REGISTRY_VERSION,
            "x-sterling-api-surface": self.api_surface.value,
            "x-sterling-auth-posture": self.auth_posture.value,
            "x-sterling-current-frontend-contract": self.current_frontend_contract,
            "x-sterling-legacy-compatibility": self.legacy_compatibility,
            "x-sterling-personas": [persona.value for persona in capability.allowed_personas],
            "x-sterling-capability-surface": Surface.REST.value,
        }


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
    if path == "/api/chat":
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
        )
    if path.startswith("/api/admin/traces"):
        return RouteCapability(
            "developer_trace.read",
            ApiSurface.DEVELOPER_TRACE,
            AuthPosture.CATALOG_ADMIN_CLERK,
            current_frontend_contract=True,
        )
    if path in _CATALOG_ADMIN_DRAFT_PATHS:
        return RouteCapability(
            "catalog_admin.product.draft",
            ApiSurface.CATALOG_ADMIN,
            AuthPosture.CATALOG_ADMIN_CLERK,
            current_frontend_contract=True,
        )
    if path in _CATALOG_ADMIN_PUBLISH_PATHS:
        return RouteCapability(
            "catalog_admin.product.publish",
            ApiSurface.CATALOG_ADMIN,
            AuthPosture.CATALOG_ADMIN_CLERK,
            current_frontend_contract=True,
        )
    if path == "/api/admin/catalog/assistant/query":
        return RouteCapability(
            "catalog_admin.assistant.query",
            ApiSurface.CATALOG_ADMIN,
            AuthPosture.CATALOG_ADMIN_CLERK,
            current_frontend_contract=True,
        )
    if path.startswith("/api/admin/catalog"):
        return RouteCapability(
            "catalog_admin.catalog.manage",
            ApiSurface.CATALOG_ADMIN,
            AuthPosture.CATALOG_ADMIN_CLERK,
            current_frontend_contract=True,
        )
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
