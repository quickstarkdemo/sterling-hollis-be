from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.catalog.admin_schemas import (
    AdminProductResponse,
    ArchiveRequest,
    DraftMutationRequest,
    DraftRevisionResponse,
    LifecycleMutationResponse,
    PublishRequest,
)
from app.config import Settings, get_settings
from app.database import get_db
from app.services.catalog_admin import (
    archive_product,
    create_draft,
    get_admin_product,
    publish_draft,
)
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


@router.post(
    "/catalog/products/drafts",
    response_model=DraftRevisionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a private catalog product draft",
)
def create_product_draft(
    request: DraftMutationRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> DraftRevisionResponse:
    result, _ = create_draft(
        db,
        request,
        idempotency_key=idempotency_key,
        principal=principal,
    )
    return result


@router.put(
    "/catalog/products/{product_id}/draft",
    response_model=DraftRevisionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Stage a private revision of a catalog product",
)
def revise_catalog_product(
    product_id: str,
    request: DraftMutationRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> DraftRevisionResponse:
    result, _ = create_draft(
        db,
        request,
        idempotency_key=idempotency_key,
        principal=principal,
        path_product_id=product_id,
    )
    return result


@router.post(
    "/catalog/products/{product_id}/publish",
    response_model=LifecycleMutationResponse,
    summary="Atomically publish an approved catalog draft",
)
def publish_catalog_product(
    product_id: str,
    request: PublishRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> LifecycleMutationResponse:
    result, _ = publish_draft(
        db,
        product_id=product_id,
        draft_id=request.draft_id,
        expected_version=request.expected_version,
        idempotency_key=idempotency_key,
        principal=principal,
    )
    return result


@router.post(
    "/catalog/products/{product_id}/archive",
    response_model=LifecycleMutationResponse,
    summary="Archive a published catalog product",
)
def archive_catalog_product(
    product_id: str,
    request: ArchiveRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> LifecycleMutationResponse:
    result, _ = archive_product(
        db,
        product_id=product_id,
        expected_version=request.expected_version,
        idempotency_key=idempotency_key,
        principal=principal,
    )
    return result


@router.get(
    "/catalog/products/{product_id}",
    response_model=AdminProductResponse,
    summary="Inspect catalog lifecycle and revision history",
)
def admin_catalog_product(
    product_id: str,
    db: Session = Depends(get_db),
    _: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> AdminProductResponse:
    product = get_admin_product(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Catalog product not found.")
    return product
