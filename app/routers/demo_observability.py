from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.config import Settings, get_settings
from app.schemas import DemoObservabilityMode, DemoObservabilityStateResponse, DemoObservabilityUpdateRequest
from app.services.auth.clerk import AuthenticatedPrincipal, require_clerk_principal
from app.services.demo_observability import (
    get_demo_observability_state,
    reset_demo_observability_state,
    send_network_outage_snmp_trap_log,
    update_demo_observability_state,
)


router = APIRouter(prefix="/api/demo/observability", tags=["demo"])


def _split_csv(raw: str | None) -> set[str]:
    return {item.strip().lower() for item in (raw or "").split(",") if item.strip()}


def require_demo_toggle_principal(
    principal: AuthenticatedPrincipal = Depends(require_clerk_principal),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedPrincipal:
    allowed_emails = _split_csv(settings.demo_observability_clerk_authorized_emails)
    allowed_subjects = _split_csv(settings.demo_observability_clerk_authorized_subjects)
    demo_email = (settings.clerk_demo_customer_email or "").strip().lower()
    if demo_email and not allowed_emails and not allowed_subjects:
        allowed_emails.add(demo_email)

    principal_email = (principal.email or "").strip().lower()
    principal_subject = principal.provider_user_id.strip().lower()
    if (principal_email and principal_email in allowed_emails) or principal_subject in allowed_subjects:
        return principal

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Clerk user is not allowed to control demo observability toggles.",
    )


@router.get("", response_model=DemoObservabilityStateResponse)
def clerk_demo_observability_state(
    _: AuthenticatedPrincipal = Depends(require_demo_toggle_principal),
):
    return get_demo_observability_state()


@router.post("", response_model=DemoObservabilityStateResponse)
def set_clerk_demo_observability_state(
    req: DemoObservabilityUpdateRequest,
    _: AuthenticatedPrincipal = Depends(require_demo_toggle_principal),
):
    if req.mode == DemoObservabilityMode.network_outage and req.enabled is not False:
        try:
            send_network_outage_snmp_trap_log()
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return update_demo_observability_state(req)


@router.post("/reset", response_model=DemoObservabilityStateResponse)
def reset_clerk_demo_observability(
    _: AuthenticatedPrincipal = Depends(require_demo_toggle_principal),
):
    return reset_demo_observability_state()
