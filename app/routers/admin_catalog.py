from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.catalog.admin_schemas import (
    AdminDraftSnapshot,
    AdminProductListResponse,
    AdminProductResponse,
    ArchiveRequest,
    DraftMutationRequest,
    DraftRevisionResponse,
    LifecycleMutationResponse,
    PublishRequest,
    StartRevisionRequest,
)
from app.catalog.ai_schemas import (
    CatalogAICommandRequest,
    CatalogAIWorkflowResponse,
)
from app.catalog.image_schemas import (
    CatalogImageApprovalRequest,
    CatalogImageApprovalResponse,
    CatalogImageCommandRequest,
    CatalogImageJobResponse,
    CatalogImageVariantSetRequest,
    CatalogImageVariantSetResponse,
    CatalogMediaCommandRequest,
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
    list_admin_products,
    publish_draft,
    start_product_revision,
)
from app.services.catalog_ai import CatalogAICommandError, CatalogAIService
from app.services.catalog_images import (
    approve_catalog_image,
    enqueue_catalog_image_job,
    enqueue_catalog_image_variant_set,
    enqueue_catalog_media_job,
    get_catalog_image_job,
    get_catalog_image_variant_set,
)
from app.services.catalog_realtime import (
    CatalogRealtimeError,
    CatalogRealtimeService,
    CatalogRealtimeSessionResponse,
    CatalogRealtimeToolCallRequest,
    record_realtime_tool_call,
)
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


def get_catalog_realtime_service(
    settings: Settings = Depends(get_settings),
) -> CatalogRealtimeService:
    return CatalogRealtimeService(settings)


class CapabilityStatus(BaseModel):
    configured: bool
    reason: Literal[
        "feature_disabled",
        "missing_api_key",
        "missing_safety_secret",
    ] | None = None


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


@router.get(
    "/session",
    response_model=CatalogStudioSessionResponse,
    response_model_exclude_none=True,
)
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


@router.get(
    "/catalog/products",
    response_model=AdminProductListResponse,
    summary="Search products across administrative lifecycle states",
)
def admin_catalog_products(
    q: str | None = Query(default=None, max_length=255),
    lifecycle_status: Literal["draft", "published", "archived"] | None = Query(
        default=None
    ),
    category: str | None = Query(default=None, max_length=128),
    brand: str | None = Query(default=None, max_length=128),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=100),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> AdminProductListResponse:
    return list_admin_products(
        db,
        principal=principal,
        q=q,
        lifecycle_status=lifecycle_status,
        category=category,
        brand=brand,
        page=page,
        page_size=page_size,
    )


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
    "/catalog/products/{product_id}/revisions",
    response_model=AdminDraftSnapshot,
    status_code=status.HTTP_201_CREATED,
    summary="Start a private revision from the current published snapshot",
)
def start_catalog_product_revision(
    product_id: str,
    request: StartRevisionRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> AdminDraftSnapshot:
    result, _ = start_product_revision(
        db,
        product_id=product_id,
        request=request,
        idempotency_key=idempotency_key,
        principal=principal,
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
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> AdminProductResponse:
    product = get_admin_product(db, product_id, principal=principal)
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


@router.post(
    "/catalog/workflows/{workflow_id}/realtime/sessions",
    response_model=CatalogRealtimeSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a workflow-bound Realtime voice session",
)
def create_catalog_realtime_session(
    workflow_id: str,
    response: Response,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    service: CatalogRealtimeService = Depends(get_catalog_realtime_service),
) -> CatalogRealtimeSessionResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.create_session(
            db,
            workflow_id=workflow_id,
            principal=principal,
        )
    except CatalogRealtimeError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "message": exc.detail,
                "retryable": exc.retryable,
            },
        ) from exc


@router.post(
    "/catalog/workflows/{workflow_id}/realtime/tool-calls",
    response_model=CatalogAIWorkflowResponse,
    response_model_exclude_none=True,
    summary="Execute an approved Realtime catalog draft tool call",
)
def execute_catalog_realtime_tool_call(
    workflow_id: str,
    request: CatalogRealtimeToolCallRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    settings: Settings = Depends(get_settings),
    service: CatalogAIService = Depends(get_catalog_ai_service),
) -> CatalogAIWorkflowResponse:
    if not settings.catalog_studio_realtime_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "realtime_disabled",
                "message": "The Realtime capability is disabled.",
                "retryable": False,
            },
        )
    record_realtime_tool_call(
        db,
        workflow_id=workflow_id,
        request=request,
        idempotency_key=idempotency_key,
        principal=principal,
        settings=settings,
    )
    try:
        result = service.execute(
            db,
            workflow_id=workflow_id,
            command=request.to_catalog_command(),
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


@router.post(
    "/catalog/workflows/{workflow_id}/image-commands",
    response_model=CatalogImageJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Generate or refine an image for the current catalog draft",
)
def create_catalog_image_command(
    workflow_id: str,
    request: CatalogImageCommandRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    settings: Settings = Depends(get_settings),
) -> CatalogImageJobResponse:
    return enqueue_catalog_image_job(
        db,
        workflow_id=workflow_id,
        request=request,
        idempotency_key=idempotency_key,
        principal=principal,
        settings=settings,
    )


@router.post(
    "/catalog/workflows/{workflow_id}/media-commands",
    response_model=CatalogImageJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create a product media variation from the approved core image",
)
def create_catalog_media_command(
    workflow_id: str,
    request: CatalogMediaCommandRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    settings: Settings = Depends(get_settings),
) -> CatalogImageJobResponse:
    return enqueue_catalog_media_job(
        db,
        workflow_id=workflow_id,
        request=request,
        idempotency_key=idempotency_key,
        principal=principal,
        settings=settings,
    )


@router.post(
    "/catalog/workflows/{workflow_id}/image-variant-sets",
    response_model=CatalogImageVariantSetResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Generate coherent images for the remaining catalog variants",
)
def create_catalog_image_variant_set(
    workflow_id: str,
    request: CatalogImageVariantSetRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    settings: Settings = Depends(get_settings),
) -> CatalogImageVariantSetResponse:
    return enqueue_catalog_image_variant_set(
        db,
        workflow_id=workflow_id,
        request=request,
        idempotency_key=idempotency_key,
        principal=principal,
        settings=settings,
    )


@router.get(
    "/catalog/workflows/{workflow_id}/image-variant-sets/{image_variant_set_id}",
    response_model=CatalogImageVariantSetResponse,
    summary="Read aggregate and child status for a catalog image variant set",
)
def catalog_image_variant_set_detail(
    workflow_id: str,
    image_variant_set_id: str,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> CatalogImageVariantSetResponse:
    return get_catalog_image_variant_set(
        db,
        workflow_id=workflow_id,
        image_variant_set_id=image_variant_set_id,
        principal=principal,
    )


@router.get(
    "/catalog/workflows/{workflow_id}/image-jobs/{job_id}",
    response_model=CatalogImageJobResponse,
    summary="Read a Catalog Studio image job",
)
def catalog_image_job_detail(
    workflow_id: str,
    job_id: str,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> CatalogImageJobResponse:
    return get_catalog_image_job(
        db,
        workflow_id=workflow_id,
        job_id=job_id,
        principal=principal,
    )


@router.post(
    "/catalog/workflows/{workflow_id}/image-jobs/{job_id}/approve",
    response_model=CatalogImageApprovalResponse,
    summary="Approve a generated image for catalog publication",
)
def approve_catalog_image_job(
    workflow_id: str,
    job_id: str,
    request: CatalogImageApprovalRequest,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    settings: Settings = Depends(get_settings),
) -> CatalogImageApprovalResponse:
    return approve_catalog_image(
        db,
        workflow_id=workflow_id,
        job_id=job_id,
        request=request,
        principal=principal,
        settings=settings,
    )
