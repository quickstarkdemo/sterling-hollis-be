from __future__ import annotations

from typing import Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.catalog.admin_schemas import (
    AdminDraftSnapshot,
    AdminDraftSnapshotV2,
    AdminDraftSnapshotV3,
    AdminProductListResponse,
    AdminProductResponse,
    AdminProductResponseV2,
    AdminProductResponseV3,
    ArchiveRequest,
    BrandCreateRequest,
    BrandReference,
    CatalogReferenceData,
    DraftMutationRequest,
    DraftMutationRequestV2,
    DraftMutationRequestV3,
    DraftRevisionResponse,
    LifecycleMutationResponse,
    PublishRequest,
    ProductDraftPreviewV3,
    ProductReadinessResponse,
    StartRevisionRequest,
    CatalogSuggestionDecisionRequest,
    CatalogSuggestionDecisionResponse,
    CatalogSuggestionSetCreateRequest,
    CatalogSuggestionSetListResponse,
    CatalogSuggestionSetResponse,
)
from app.catalog.ai_schemas import (
    CatalogAICommandRequest,
    CatalogAIDirectSuggestionCommandRequest,
    CatalogAIWorkflowResponse,
    CatalogAISuggestionCommandResult,
)
from app.catalog.image_schemas import (
    CatalogImageApprovalRequest,
    CatalogImageApprovalResponse,
    CatalogImageCommandRequest,
    CatalogImageJobResponse,
    CatalogImageVariantSetRequest,
    CatalogImageVariantSetResponse,
    CatalogMediaCommandRequest,
    CatalogMediaMutationRequest,
)
from app.catalog.source_schemas import (
    CatalogSourceBundleListResponse,
    CatalogSourceBundleResponse,
    CatalogSourcePromotionRequest,
    CatalogSourcePromotionResponse,
)
from app.catalog.review_schemas import (
    AdminProductReviewListResponse,
    AdminProductReviewResponse,
    ReviewAssistRequest,
    ReviewDecisionRequest,
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
    add_catalog_brand,
    archive_product,
    create_draft,
    create_draft_v2,
    create_draft_v3,
    get_admin_product,
    get_admin_product_v2,
    get_admin_product_v3,
    get_product_preview_v3,
    get_product_readiness_v3,
    list_admin_products,
    list_catalog_references,
    publish_draft,
    start_product_revision,
    start_product_revision_v2,
    start_product_revision_v3,
)
from app.services.catalog_suggestions import (
    create_suggestion_set,
    decide_suggestion_set,
    list_suggestion_sets,
)
from app.services.product_reviews import (
    ProductReviewAIService,
    assist_product_review,
    decide_product_review,
    list_admin_product_reviews,
)
from app.services.catalog_ai import (
    CatalogAICommandError,
    CatalogAIService,
    CatalogAISuggestionService,
)
from app.services.catalog_images import (
    approve_catalog_image,
    enqueue_catalog_image_job,
    enqueue_catalog_image_variant_set,
    enqueue_catalog_media_job,
    get_catalog_image_job,
    get_catalog_image_variant_set,
    mutate_catalog_media,
)
from app.services.catalog_realtime import (
    CatalogRealtimeError,
    CatalogRealtimeService,
    CatalogRealtimeSessionContextRequest,
    CatalogRealtimeSessionResponse,
    CatalogRealtimeToolCallRequest,
    CatalogRealtimeV3ToolCallRequest,
    reject_legacy_realtime_mutation_for_v3,
    record_realtime_tool_call,
)
from app.services.catalog_voice_tools import (
    CatalogVoiceToolResult,
    answer_catalog_question,
    execute_catalog_voice_tool,
)
from app.services.catalog_sources import (
    create_source_bundle,
    get_source_bundle,
    get_source_preview,
    list_source_bundles,
    promote_source_asset,
    remove_source_asset,
)
from app.services.image_analysis import ImageUploadError
from app.services.catalog_workflow import (
    append_workflow_event,
    get_catalog_workflow_projection,
    project_workflow_event,
    start_catalog_workflow,
)
from app.services.auth.admin import (
    bind_catalog_trace_capture,
    catalog_studio_capabilities,
    require_catalog_admin,
)
from app.services.auth.clerk import AuthenticatedPrincipal


router = APIRouter(
    prefix="/api/admin",
    tags=["catalog-studio-admin"],
    dependencies=[Depends(bind_catalog_trace_capture)],
)


def get_catalog_ai_service(
    settings: Settings = Depends(get_settings),
) -> CatalogAIService:
    return CatalogAIService(settings)


def get_catalog_suggestion_ai_service(
    settings: Settings = Depends(get_settings),
) -> CatalogAISuggestionService:
    return CatalogAISuggestionService(settings)


def get_catalog_realtime_service(
    settings: Settings = Depends(get_settings),
) -> CatalogRealtimeService:
    return CatalogRealtimeService(settings)


def get_product_review_ai_service(
    settings: Settings = Depends(get_settings),
) -> ProductReviewAIService:
    return ProductReviewAIService(settings)


class CapabilityStatus(BaseModel):
    configured: bool
    reason: Literal[
        "feature_disabled",
        "missing_api_key",
        "missing_safety_secret",
    ] | None = None


class CatalogCapabilityStatus(CapabilityStatus):
    authoring_schema_version: Literal[3]


class CatalogStudioCapabilities(BaseModel):
    responses: CapabilityStatus
    moderation: CapabilityStatus
    image_generation: CapabilityStatus
    realtime: CapabilityStatus
    worker_storage: CapabilityStatus
    api_traces: CapabilityStatus
    catalog: CatalogCapabilityStatus


class CatalogStudioSessionResponse(BaseModel):
    authorized: Literal[True] = True
    capabilities: CatalogStudioCapabilities


class CatalogAssistantQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    query_scopes: list[Literal["catalog", "inventory"]] | None = None


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
    "/catalog/source-bundles",
    response_model=CatalogSourceBundleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload private supplier images for catalog authoring",
)
def upload_catalog_source_bundle(
    response: Response,
    files: list[UploadFile] = File(...),
    title: str = Form(default="Supplier source bundle", min_length=1, max_length=255),
    catalog_product_id: str | None = Form(default=None, max_length=64),
    draft_revision_id: str | None = Form(default=None, max_length=64),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    settings: Settings = Depends(get_settings),
) -> CatalogSourceBundleResponse:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return create_source_bundle(
            db,
            files=files,
            title=title,
            catalog_product_id=catalog_product_id,
            draft_revision_id=draft_revision_id,
            principal=principal,
            settings=settings,
        )
    except ImageUploadError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get(
    "/catalog/source-bundles",
    response_model=CatalogSourceBundleListResponse,
    summary="List owned private supplier source bundles",
)
def catalog_source_bundles(
    response: Response,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> CatalogSourceBundleListResponse:
    response.headers["Cache-Control"] = "private, no-store"
    return list_source_bundles(db, principal=principal)


@router.get(
    "/catalog/source-bundles/{bundle_id}",
    response_model=CatalogSourceBundleResponse,
    summary="Read an owned private supplier source bundle",
)
def catalog_source_bundle_detail(
    bundle_id: str,
    response: Response,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> CatalogSourceBundleResponse:
    response.headers["Cache-Control"] = "private, no-store"
    return get_source_bundle(db, bundle_id=bundle_id, principal=principal)


@router.get(
    "/catalog/source-bundles/{bundle_id}/assets/{asset_id}/preview",
    response_class=FileResponse,
    summary="Read an authorized bounded supplier image preview",
)
def catalog_source_asset_preview(
    bundle_id: str,
    asset_id: str,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    preview_path, media_type = get_source_preview(
        db,
        bundle_id=bundle_id,
        asset_id=asset_id,
        principal=principal,
        settings=settings,
    )
    return FileResponse(
        preview_path,
        media_type=media_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete(
    "/catalog/source-bundles/{bundle_id}/assets/{asset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an unattached private supplier source image",
)
def delete_catalog_source_asset(
    bundle_id: str,
    asset_id: str,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    settings: Settings = Depends(get_settings),
) -> Response:
    remove_source_asset(
        db,
        bundle_id=bundle_id,
        asset_id=asset_id,
        principal=principal,
        settings=settings,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/catalog/source-bundles/{bundle_id}/assets/{asset_id}/promote",
    response_model=CatalogSourcePromotionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Promote a private supplier source into approved draft media",
)
def promote_catalog_source_asset(
    bundle_id: str,
    asset_id: str,
    request: CatalogSourcePromotionRequest,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    settings: Settings = Depends(get_settings),
) -> CatalogSourcePromotionResponse:
    response.headers["Cache-Control"] = "private, no-store"
    return promote_source_asset(
        db,
        bundle_id=bundle_id,
        asset_id=asset_id,
        request=request,
        idempotency_key=idempotency_key,
        principal=principal,
        settings=settings,
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
    settings: Settings = Depends(get_settings),
) -> LifecycleMutationResponse:
    result, _ = publish_draft(
        db,
        product_id=product_id,
        draft_id=request.draft_id,
        expected_version=request.expected_version,
        idempotency_key=idempotency_key,
        principal=principal,
        settings=settings,
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


@router.get(
    "/catalog/v2/references",
    response_model=CatalogReferenceData,
    summary="List canonical catalog authoring references",
)
def catalog_references_v2(
    db: Session = Depends(get_db),
    _: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> CatalogReferenceData:
    return list_catalog_references(db)


@router.post(
    "/catalog/v2/brands",
    response_model=BrandReference,
    status_code=status.HTTP_201_CREATED,
    summary="Add a canonical catalog brand",
)
def add_catalog_brand_v2(
    request: BrandCreateRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> BrandReference:
    result, _ = add_catalog_brand(
        db,
        request,
        idempotency_key=idempotency_key,
        principal=principal,
    )
    return result


@router.post(
    "/catalog/v2/products/drafts",
    response_model=DraftRevisionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a canonical product-level catalog draft",
)
def create_product_draft_v2(
    request: DraftMutationRequestV2,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> DraftRevisionResponse:
    result, _ = create_draft_v2(
        db,
        request,
        idempotency_key=idempotency_key,
        principal=principal,
    )
    return result


@router.get(
    "/catalog/v2/products",
    response_model=AdminProductListResponse,
    summary="Search canonical products across lifecycle states",
)
def admin_catalog_products_v2(
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
    "/catalog/v2/products/{product_id}/draft",
    response_model=DraftRevisionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Stage a canonical product-level revision",
)
def revise_catalog_product_v2(
    product_id: str,
    request: DraftMutationRequestV2,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> DraftRevisionResponse:
    result, _ = create_draft_v2(
        db,
        request,
        idempotency_key=idempotency_key,
        principal=principal,
        path_product_id=product_id,
    )
    return result


@router.post(
    "/catalog/v2/products/{product_id}/revisions",
    response_model=AdminDraftSnapshotV2,
    status_code=status.HTTP_201_CREATED,
    summary="Start a canonical product-level revision",
)
def start_catalog_product_revision_v2(
    product_id: str,
    request: StartRevisionRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> AdminDraftSnapshotV2:
    result, _ = start_product_revision_v2(
        db,
        product_id=product_id,
        request=request,
        idempotency_key=idempotency_key,
        principal=principal,
    )
    return result


@router.get(
    "/catalog/v2/products/{product_id}",
    response_model=AdminProductResponseV2,
    summary="Inspect canonical product-level authoring state",
)
def admin_catalog_product_v2(
    product_id: str,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> AdminProductResponseV2:
    product = get_admin_product_v2(db, product_id, principal=principal)
    if product is None:
        raise HTTPException(status_code=404, detail="Catalog product not found.")
    return product


@router.post(
    "/catalog/v2/products/{product_id}/publish",
    response_model=LifecycleMutationResponse,
    summary="Publish a canonical product-level draft",
)
def publish_catalog_product_v2(
    product_id: str,
    request: PublishRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    settings: Settings = Depends(get_settings),
) -> LifecycleMutationResponse:
    result, _ = publish_draft(
        db,
        product_id=product_id,
        draft_id=request.draft_id,
        expected_version=request.expected_version,
        idempotency_key=idempotency_key,
        principal=principal,
        settings=settings,
    )
    return result


@router.post(
    "/catalog/v2/products/{product_id}/archive",
    response_model=LifecycleMutationResponse,
    summary="Archive a canonical product",
)
def archive_catalog_product_v2(
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


@router.post(
    "/catalog/v3/products/drafts",
    response_model=DraftRevisionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a structured catalog authoring draft",
)
def create_product_draft_v3(
    request: DraftMutationRequestV3,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> DraftRevisionResponse:
    result, _ = create_draft_v3(
        db,
        request,
        idempotency_key=idempotency_key,
        principal=principal,
    )
    return result


@router.put(
    "/catalog/v3/products/{product_id}/draft",
    response_model=DraftRevisionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Stage a structured catalog authoring revision",
)
def revise_catalog_product_v3(
    product_id: str,
    request: DraftMutationRequestV3,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> DraftRevisionResponse:
    result, _ = create_draft_v3(
        db,
        request,
        idempotency_key=idempotency_key,
        principal=principal,
        path_product_id=product_id,
    )
    return result


@router.post(
    "/catalog/v3/products/{product_id}/revisions",
    response_model=AdminDraftSnapshotV3,
    status_code=status.HTTP_201_CREATED,
    summary="Start a structured catalog product revision",
)
def start_catalog_product_revision_v3(
    product_id: str,
    request: StartRevisionRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> AdminDraftSnapshotV3:
    result, _ = start_product_revision_v3(
        db,
        product_id=product_id,
        request=request,
        idempotency_key=idempotency_key,
        principal=principal,
    )
    return result


@router.get(
    "/catalog/v3/products/{product_id}",
    response_model=AdminProductResponseV3,
    summary="Inspect structured product authoring state",
)
def admin_catalog_product_v3(
    product_id: str,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> AdminProductResponseV3:
    product = get_admin_product_v3(db, product_id, principal=principal)
    if product is None:
        raise HTTPException(status_code=404, detail="Catalog product not found.")
    return product


@router.get(
    "/catalog/v3/products/{product_id}/drafts/{draft_id}/readiness",
    response_model=ProductReadinessResponse,
    summary="Evaluate blocking and recommended catalog readiness checks",
)
def catalog_product_readiness_v3(
    product_id: str,
    draft_id: str,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> ProductReadinessResponse:
    return get_product_readiness_v3(
        db,
        product_id=product_id,
        draft_id=draft_id,
        principal=principal,
    )


@router.get(
    "/catalog/v3/products/{product_id}/drafts/{draft_id}/preview",
    response_model=ProductDraftPreviewV3,
    summary="Preview a structured product without private source references",
)
def catalog_product_preview_v3(
    product_id: str,
    draft_id: str,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> ProductDraftPreviewV3:
    return get_product_preview_v3(
        db,
        product_id=product_id,
        draft_id=draft_id,
        principal=principal,
    )


@router.post(
    "/catalog/v3/products/{product_id}/suggestion-sets",
    response_model=CatalogSuggestionSetResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create reviewable product field suggestions",
)
def create_catalog_suggestion_set_v3(
    product_id: str,
    request: CatalogSuggestionSetCreateRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> CatalogSuggestionSetResponse:
    result, _ = create_suggestion_set(
        db,
        product_id=product_id,
        request=request,
        idempotency_key=idempotency_key,
        principal=principal,
    )
    return result


@router.post(
    "/catalog/v3/products/{product_id}/ai-suggestion-sets",
    response_model=CatalogAISuggestionCommandResult,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="Generate grounded, reviewable product field suggestions",
)
def generate_catalog_suggestion_set_v3(
    product_id: str,
    request: CatalogAIDirectSuggestionCommandRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    service: CatalogAISuggestionService = Depends(get_catalog_suggestion_ai_service),
) -> CatalogAISuggestionCommandResult:
    try:
        return service.execute(
            db,
            product_id=product_id,
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


@router.get(
    "/catalog/v3/products/{product_id}/suggestion-sets",
    response_model=CatalogSuggestionSetListResponse,
    summary="List private reviewable product suggestions",
)
def list_catalog_suggestion_sets_v3(
    product_id: str,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> CatalogSuggestionSetListResponse:
    return list_suggestion_sets(db, product_id=product_id, principal=principal)


@router.get(
    "/catalog/products/{product_id}/reviews",
    response_model=AdminProductReviewListResponse,
    summary="List private product review moderation state",
)
def list_catalog_product_reviews(
    product_id: str,
    response: Response,
    db: Session = Depends(get_db),
    _: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> AdminProductReviewListResponse:
    response.headers["Cache-Control"] = "private, no-store"
    return list_admin_product_reviews(db, product_id=product_id)


@router.post(
    "/catalog/products/{product_id}/reviews/{review_id}/assist",
    response_model=AdminProductReviewResponse,
    summary="Generate a reviewable moderation and response proposal",
)
def assist_catalog_product_review(
    product_id: str,
    review_id: str,
    request: ReviewAssistRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    service: ProductReviewAIService = Depends(get_product_review_ai_service),
) -> AdminProductReviewResponse:
    try:
        return assist_product_review(
            db,
            product_id=product_id,
            review_id=review_id,
            request=request,
            idempotency_key=idempotency_key,
            principal=principal,
            service=service,
        )
    except CatalogAICommandError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.detail, "retryable": exc.retryable},
        ) from exc


@router.post(
    "/catalog/products/{product_id}/reviews/{review_id}/decisions",
    response_model=AdminProductReviewResponse,
    summary="Record a versioned product review moderation decision",
)
def decide_catalog_product_review(
    product_id: str,
    review_id: str,
    request: ReviewDecisionRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> AdminProductReviewResponse:
    return decide_product_review(
        db,
        product_id=product_id,
        review_id=review_id,
        request=request,
        idempotency_key=idempotency_key,
        principal=principal,
    )


@router.post(
    "/catalog/v3/products/{product_id}/suggestion-sets/{suggestion_set_id}/decisions",
    response_model=CatalogSuggestionDecisionResponse,
    summary="Accept, reject, or supersede product field suggestions",
)
def decide_catalog_suggestion_set_v3(
    product_id: str,
    suggestion_set_id: str,
    request: CatalogSuggestionDecisionRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> CatalogSuggestionDecisionResponse:
    result, _ = decide_suggestion_set(
        db,
        product_id=product_id,
        suggestion_set_id=suggestion_set_id,
        request=request,
        idempotency_key=idempotency_key,
        principal=principal,
    )
    return result


@router.post(
    "/catalog/v3/products/{product_id}/publish",
    response_model=LifecycleMutationResponse,
    summary="Publish a ready structured catalog draft",
)
def publish_catalog_product_v3(
    product_id: str,
    request: PublishRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    settings: Settings = Depends(get_settings),
) -> LifecycleMutationResponse:
    result, _ = publish_draft(
        db,
        product_id=product_id,
        draft_id=request.draft_id,
        expected_version=request.expected_version,
        idempotency_key=idempotency_key,
        principal=principal,
        settings=settings,
    )
    return result


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
    "/catalog/assistant/query",
    response_model=CatalogVoiceToolResult,
    response_model_exclude_none=True,
    summary="Answer a bounded read-only catalog assistant question",
)
def query_catalog_assistant(
    request: CatalogAssistantQueryRequest,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> CatalogVoiceToolResult:
    _ = principal
    return answer_catalog_question(
        db,
        question=request.question,
        query_scopes=request.query_scopes,
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
    request: CatalogRealtimeSessionContextRequest | None = None,
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
            context=request,
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
    "/catalog/workflows/{workflow_id}/realtime/v3/tool-calls",
    response_model=CatalogVoiceToolResult,
    response_model_exclude_none=True,
    summary="Execute a bounded Realtime product query or field proposal",
)
def execute_catalog_realtime_v3_tool_call(
    workflow_id: str,
    request: CatalogRealtimeV3ToolCallRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
    settings: Settings = Depends(get_settings),
    suggestion_service: CatalogAISuggestionService = Depends(
        get_catalog_suggestion_ai_service
    ),
) -> CatalogVoiceToolResult:
    try:
        return execute_catalog_voice_tool(
            db,
            workflow_id=workflow_id,
            request=request,
            idempotency_key=idempotency_key,
            principal=principal,
            settings=settings,
            suggestion_service=suggestion_service,
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
    reject_legacy_realtime_mutation_for_v3(
        db,
        workflow_id=workflow_id,
        principal=principal,
    )
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
    "/catalog/workflows/{workflow_id}/media-mutations",
    response_model=DraftRevisionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Set main, reorder, remove, or restore product media",
)
def create_catalog_media_mutation(
    workflow_id: str,
    request: CatalogMediaMutationRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_catalog_admin),
) -> DraftRevisionResponse:
    return mutate_catalog_media(
        db,
        workflow_id=workflow_id,
        request=request,
        idempotency_key=idempotency_key,
        principal=principal,
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
