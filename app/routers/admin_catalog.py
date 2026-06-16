from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.services.auth.admin import catalog_studio_capabilities, require_catalog_admin
from app.services.auth.clerk import AuthenticatedPrincipal


router = APIRouter(prefix="/api/admin", tags=["catalog-studio-admin"])


class CapabilityStatus(BaseModel):
    configured: bool


class CatalogStudioCapabilities(BaseModel):
    responses: CapabilityStatus
    moderation: CapabilityStatus
    image_generation: CapabilityStatus
    realtime: CapabilityStatus
    worker_storage: CapabilityStatus
    catalog: CapabilityStatus


class CatalogStudioSessionResponse(BaseModel):
    authorized: Literal[True] = True
    capabilities: CatalogStudioCapabilities


@router.get("/session", response_model=CatalogStudioSessionResponse)
def catalog_studio_session(
    response: Response,
    _: AuthenticatedPrincipal = Depends(require_catalog_admin),
    settings: Settings = Depends(get_settings),
) -> CatalogStudioSessionResponse:
    response.headers["Cache-Control"] = "no-store"
    return CatalogStudioSessionResponse(
        capabilities=CatalogStudioCapabilities(**catalog_studio_capabilities(settings)),
    )
