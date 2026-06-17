from __future__ import annotations

from collections.abc import Mapping, Sequence

from fastapi import Depends, HTTPException, status

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


def catalog_studio_capabilities(settings: Settings) -> dict[str, dict[str, bool]]:
    openai_configured = bool((settings.openai_api_key or "").strip())
    image_storage_configured = bool((settings.product_image_output_dir or "").strip())
    catalog_configured = bool((settings.database_url or "").strip())
    return {
        "responses": {"configured": openai_configured},
        "moderation": {"configured": openai_configured},
        "image_generation": {"configured": openai_configured and image_storage_configured},
        "realtime": {
            "configured": (
                openai_configured
                and settings.catalog_studio_realtime_enabled
                and bool(settings.catalog_studio_realtime_safety_identifier_secret.strip())
            )
        },
        "worker_storage": {"configured": image_storage_configured},
        "catalog": {"configured": catalog_configured},
    }
