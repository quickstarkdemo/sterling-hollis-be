from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
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
from app.catalog.ai_schemas import (
    CatalogAICommandRequest,
    CatalogAIWorkflowResponse,
)
from app.catalog.workflow_schemas import (
    CatalogWorkflowResponse,
    CatalogWorkflowStartRequest,
    WorkflowEventInput,
    WorkflowEventResponse,
)
from app.config import Settings, get_settings
from app.database import get_db
from app.services.catalog_admin import (
    archive_product,
    create_draft,
    get_admin_product,
    publish_draft,
)
from app.services.catalog_ai import CatalogAICommandError, CatalogAIService
from app.services.catalog_workflow import (
    append_workflow_event,
    get_catalog_workflow_projection,
    project_workflow_event,
    start_catalog_workflow,
)
from app.services.auth.admin import catalog_studio_capabilities, require_catalog_admin
from app.services.auth.clerk import AuthenticatedPrincipal


router = APIRouter(prefix="/api/admin", tags=["catalog-studio-admin"])


def get_catalog_ai_service(
    settings: Settings = Depends(get_settings),
) -> CatalogAIService:
    return CatalogAIService(settings)


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


@router.post(
    "/catalog/workflows",
    response_model=CatalogWorkflowResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="Start a sanitized OpenAI catalog workflow",
)
def create_catalog_workflow(
    request: CatalogWorkflowStartRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    settings: Settings = Depends(get_settings),
) -> CatalogWorkflowResponse:
    workflow = start_catalog_workflow(
        db,
        principal=principal,
        title=request.title,
        business_summary=request.business_summary,
        settings=settings,
        idempotency_key=idempotency_key,
        draft_id=request.draft_id,
        image_job_id=request.image_job_id,
        published_product_id=request.published_product_id,
    )
    return get_catalog_workflow_projection(
        db,
        workflow_id=workflow.id,
        principal=principal,
        developer=False,
        settings=settings,
    )


@router.post(
    "/catalog/workflows/{workflow_id}/events",
    response_model=WorkflowEventResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="Append a sanitized OpenAI workflow event",
)
def create_workflow_event(
    workflow_id: str,
    request: WorkflowEventInput,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    settings: Settings = Depends(get_settings),
) -> WorkflowEventResponse:
    event = append_workflow_event(
        db,
        workflow_id=workflow_id,
        principal=principal,
        event=request,
        settings=settings,
    )
    return project_workflow_event(event, developer=True)


@router.get(
    "/catalog/workflows/{workflow_id}",
    response_model=CatalogWorkflowResponse,
    response_model_exclude_none=True,
    summary="Read a Catalog Studio workflow timeline",
)
def catalog_workflow_detail(
    workflow_id: str,
    developer: bool = Query(
        default=False,
        description="Include sanitized developer metadata. Available only to the workflow owner.",
    ),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    settings: Settings = Depends(get_settings),
) -> CatalogWorkflowResponse:
    return get_catalog_workflow_projection(
        db,
        workflow_id=workflow_id,
        principal=principal,
        developer=developer,
        settings=settings,
    )


@router.post(
    "/catalog/workflows/{workflow_id}/draft-commands",
    response_model=CatalogAIWorkflowResponse,
    response_model_exclude_none=True,
    summary="Generate or refine a moderated product draft",
)
def create_catalog_ai_draft(
    workflow_id: str,
    request: CatalogAICommandRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    settings: Settings = Depends(get_settings),
    service: CatalogAIService = Depends(get_catalog_ai_service),
) -> CatalogAIWorkflowResponse:
    try:
        result = service.execute(
            db,
            workflow_id=workflow_id,
            command=request,
            idempotency_key=idempotency_key,
            principal=principal,
        )
    except CatalogAICommandError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "message": exc.detail,
                "retryable": exc.retryable,
            },
        ) from exc
    workflow = get_catalog_workflow_projection(
        db,
        workflow_id=workflow_id,
        principal=principal,
        developer=False,
        settings=settings,
    )
    return CatalogAIWorkflowResponse(**result.model_dump(mode="python"), workflow=workflow)
