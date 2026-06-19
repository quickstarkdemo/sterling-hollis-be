from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
import re

from fastapi import Depends, HTTPException, Request, status

from app.api_traces.context import (
    ProvisionalTraceContext,
    TraceCaptureContext,
    bind_trace_capture_context,
    provisional_trace_context,
)
from app.config import Settings, get_settings
from app.services.auth.clerk import AuthenticatedPrincipal, require_clerk_principal


def _normalized_csv(raw: str | None) -> set[str]:
    return {item.strip().casefold() for item in (raw or "").split(",") if item.strip()}


def _claim_value(claims: Mapping[str, object], path: str) -> object | None:
    current: object = claims
    for segment in (part.strip() for part in path.split(".")):
        if not segment or not isinstance(current, Mapping):
            return None
        current = current.get(segment)
    return current


def _claim_matches(value: object, expected: str) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() == expected.casefold()
    if isinstance(value, bool):
        return str(value).casefold() == expected.casefold()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_claim_matches(item, expected) for item in value)
    return False


def principal_is_catalog_admin(principal: AuthenticatedPrincipal, settings: Settings) -> bool:
    allowed_emails = _normalized_csv(settings.catalog_studio_clerk_authorized_emails)
    allowed_emails.update(_normalized_csv(settings.demo_observability_clerk_authorized_emails))
    if settings.clerk_demo_customer_email:
        allowed_emails.add(settings.clerk_demo_customer_email.strip().casefold())

    allowed_subjects = _normalized_csv(settings.catalog_studio_clerk_authorized_subjects)
    allowed_subjects.update(_normalized_csv(settings.demo_observability_clerk_authorized_subjects))

    principal_email = (principal.email or "").strip().casefold()
    principal_subject = principal.provider_user_id.strip().casefold()
    if principal_email and principal_email in allowed_emails:
        return True
    if principal_subject and principal_subject in allowed_subjects:
        return True

    claim_path = settings.catalog_studio_admin_claim_path.strip()
    claim_expected = settings.catalog_studio_admin_claim_value.strip()
    if not claim_path or not claim_expected or not isinstance(principal.claims, Mapping):
        return False
    claim_value = _claim_value(principal.claims, claim_path)
    return _claim_matches(claim_value, claim_expected)


def require_catalog_admin(
    principal: AuthenticatedPrincipal = Depends(require_clerk_principal),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedPrincipal:
    if principal_is_catalog_admin(principal, settings):
        return principal
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Clerk user is not a Catalog Studio administrator.",
    )


_TRACE_SURFACE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


async def require_api_trace_capture(
    request: Request,
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    settings: Settings = Depends(get_settings),
) -> AsyncIterator[TraceCaptureContext]:
    """Authorize trace access and bind owner identity for this request only."""

    if not settings.api_trace_capture_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API trace capture is disabled.",
        )
    provisional = getattr(request.state, "api_trace_provisional", None)
    if not isinstance(provisional, ProvisionalTraceContext):
        provisional = provisional_trace_context(
            request.headers.get("traceparent"),
            request.headers.get("tracestate"),
        )
    requested_surface = (
        request.headers.get("x-trace-surface", "developer").strip().lower()
    )
    surface = (
        requested_surface
        if _TRACE_SURFACE_RE.fullmatch(requested_surface)
        else "developer"
    )
    context = TraceCaptureContext.authorized_for(
        owner_provider=principal.provider,
        owner_provider_user_id=principal.provider_user_id,
        surface=surface,
        provisional=provisional,
    )
    request.state.api_trace_capture = context
    with bind_trace_capture_context(context):
        yield context


def _realtime_capability(settings: Settings, *, openai_configured: bool) -> dict[str, object]:
    if not settings.catalog_studio_realtime_enabled:
        return {"configured": False, "reason": "feature_disabled"}
    if not openai_configured:
        return {"configured": False, "reason": "missing_api_key"}
    if not settings.catalog_studio_realtime_safety_identifier_secret.strip():
        return {"configured": False, "reason": "missing_safety_secret"}
    return {"configured": True}


def catalog_studio_capabilities(settings: Settings) -> dict[str, dict[str, object]]:
    openai_configured = bool((settings.openai_api_key or "").strip())
    image_storage_configured = bool((settings.product_image_output_dir or "").strip())
    catalog_configured = bool((settings.database_url or "").strip())
    return {
        "responses": {"configured": openai_configured},
        "moderation": {"configured": openai_configured},
        "image_generation": {"configured": openai_configured and image_storage_configured},
        "realtime": _realtime_capability(settings, openai_configured=openai_configured),
        "worker_storage": {"configured": image_storage_configured},
        "api_traces": (
            {"configured": True}
            if settings.api_trace_capture_enabled
            else {"configured": False, "reason": "feature_disabled"}
        ),
        "catalog": {
            "configured": catalog_configured,
            "authoring_schema_version": 3,
        },
    }
