from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.schemas import (
    DemoObservabilityLogSendResponse,
    DemoObservabilityMode,
    DemoObservabilityStateResponse,
    DemoObservabilityUpdateRequest,
)
from app.services.auth.admin import require_catalog_admin
from app.services.auth.clerk import AuthenticatedPrincipal
from app.services.demo_observability import (
    get_demo_observability_state,
    reset_demo_observability_state,
    send_network_outage_snmp_trap_log,
    update_demo_observability_state,
)


router = APIRouter(prefix="/api/demo/observability", tags=["demo"])


@router.get("", response_model=DemoObservabilityStateResponse)
def clerk_demo_observability_state(
    _: AuthenticatedPrincipal = Depends(require_catalog_admin),
):
    return get_demo_observability_state()


@router.post("", response_model=DemoObservabilityStateResponse)
def set_clerk_demo_observability_state(
    req: DemoObservabilityUpdateRequest,
    _: AuthenticatedPrincipal = Depends(require_catalog_admin),
):
    if req.mode == DemoObservabilityMode.network_outage and req.enabled is not False:
        try:
            send_network_outage_snmp_trap_log(event_count=req.network_event_count)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return update_demo_observability_state(req)


@router.post("/reset", response_model=DemoObservabilityStateResponse)
def reset_clerk_demo_observability(
    _: AuthenticatedPrincipal = Depends(require_catalog_admin),
):
    return reset_demo_observability_state()


@router.post("/network-outage-log", response_model=DemoObservabilityLogSendResponse)
def send_clerk_demo_network_outage_log(
    _: AuthenticatedPrincipal = Depends(require_catalog_admin),
):
    try:
        return send_network_outage_snmp_trap_log()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
