from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import socket
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import HTTPException, status
import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog.admin_schemas import (
    DraftMutationRequestV2,
    DraftRevisionResponse,
    ProductDraft,
    ProductMediaDraft,
    product_draft_snapshot_from_v1,
    product_draft_v1_from_snapshot,
    product_draft_v2_from_snapshot,
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
from app.catalog.workflow_schemas import WorkflowEventInput
from app.api_traces.adapters import (
    current_image_trace_lineage,
    new_openai_client_request_id,
    openai_request_ids,
)
from app.config import Settings
from app.models import CatalogDraftRevision, CatalogWorkflow, ImageGenerationJob
from app.schemas import IndexJobStatus
from app.services.auth.clerk import AuthenticatedPrincipal
from app.services.catalog_admin import (
    create_draft_from_v2_compatibility,
    draft_revision_version,
)
from app.services.catalog_workflow import append_workflow_event
from app.services.image_analysis import ImageUploadError, validate_image_bytes
from app.services.product_images import _write_thumbnail, product_image_options

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _request_hash(workflow_id: str, request: CatalogImageCommandRequest) -> str:
    payload = json.dumps(
        {"workflow_id": workflow_id, **request.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
    )
    return _hash(payload)


def _principal(workflow: CatalogWorkflow) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        provider=workflow.owner_provider,
        provider_user_id=workflow.owner_provider_user_id,
    )


def _owned_workflow(
    db: Session,
    workflow_id: str,
    principal: AuthenticatedPrincipal,
    *,
    lock: bool = True,
) -> CatalogWorkflow:
    statement = select(CatalogWorkflow).where(CatalogWorkflow.id == workflow_id)
    if lock:
        statement = statement.with_for_update()
    workflow = db.scalar(statement)
    if (
        workflow is None
        or workflow.owner_provider != principal.provider
        or workflow.owner_provider_user_id != principal.provider_user_id
    ):
        raise HTTPException(status_code=404, detail="Catalog Studio catalog workflow not found.")
    return workflow


def _latest_failed_trace_job(
    db: Session,
    *,
    workflow_id: str,
    draft_revision_id: str,
    requested_action: str,
    requested_variant_index: int,
    source_media_id: str | None = None,
    requested_intent: str | None = None,
) -> ImageGenerationJob | None:
    return db.scalar(
        select(ImageGenerationJob)
        .where(
            ImageGenerationJob.workflow_id == workflow_id,
            ImageGenerationJob.draft_revision_id == draft_revision_id,
            ImageGenerationJob.requested_action == requested_action,
            ImageGenerationJob.requested_variant_index == requested_variant_index,
            ImageGenerationJob.source_media_id == source_media_id,
            ImageGenerationJob.requested_intent == requested_intent,
            ImageGenerationJob.status == IndexJobStatus.failed.value,
            ImageGenerationJob.api_trace_id.is_not(None),
        )
        .order_by(ImageGenerationJob.created_at.desc(), ImageGenerationJob.id.desc())
        .limit(1)
    )


def _current_draft(
    db: Session,
    *,
    workflow: CatalogWorkflow,
    draft_id: str,
    expected_version: int,
    principal: AuthenticatedPrincipal,
) -> tuple[CatalogDraftRevision, ProductDraft]:
    if workflow.draft_revision_id != draft_id:
        raise _conflict("The requested draft is no longer current for this catalog workflow.")
    revision = db.get(CatalogDraftRevision, draft_id)
    if revision is None or revision.created_by != principal.provider_user_id:
        raise HTTPException(status_code=404, detail="Catalog draft revision not found.")
    actual_version = draft_revision_version(db, revision)
    if actual_version != expected_version:
        raise _conflict(
            f"Expected image draft version {expected_version}, but current version is {actual_version}."
        )
    return revision, product_draft_v1_from_snapshot(revision.snapshot_json)


def _job_response(job: ImageGenerationJob) -> CatalogImageJobResponse:
    return CatalogImageJobResponse(
        id=job.id,
        workflow_id=job.workflow_id or "",
        draft_id=job.draft_revision_id or "",
        expected_draft_version=job.expected_draft_version or 0,
        action=job.requested_action or "generate",  # type: ignore[arg-type]
        variant_index=job.requested_variant_index or 0,
        image_variant_set_id=job.image_variant_set_id,
        source_media_id=job.source_media_id,
        target_media_id=job.target_media_id,
        intent=job.requested_intent,
        model=job.model,
        size=job.size,
        quality=job.quality,
        output_format=job.output_format,
        status=job.status,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _allowed_media_host(hostname: str, settings: Settings) -> bool:
    configured = {
        value.strip().lower()
        for value in settings.catalog_studio_media_allowed_hosts.split(",")
        if value.strip()
    }
    public_host = (urlparse(settings.public_base_url).hostname or "").lower()
    if public_host:
        configured.add(public_host)
    hostname = hostname.lower().rstrip(".")
    return any(
        hostname == allowed
        or (allowed.startswith("*.") and hostname.endswith(allowed[1:]))
        for allowed in configured
    )


def _validate_public_media_url(value: str, settings: Settings) -> str:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
        raise _conflict("Remote media sources must use an approved HTTPS origin.")
    if parsed.port not in (None, 443) or not _allowed_media_host(hostname, settings):
        raise _conflict("Remote media source origin is not approved.")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise _conflict("Remote media source address could not be verified.") from exc
    if not addresses:
        raise _conflict("Remote media source address could not be verified.")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise _conflict("Remote media source resolved to a blocked network address.")
    return value


def _materialize_remote_source(primary_url: str, settings: Settings) -> Path:
    current_url = primary_url
    max_redirects = settings.catalog_studio_media_fetch_max_redirects
    _validate_public_media_url(current_url, settings)
    with httpx.Client(
        follow_redirects=False,
        timeout=settings.catalog_studio_media_fetch_timeout_seconds,
        trust_env=False,
    ) as client:
        for redirect_count in range(max_redirects + 1):
            _validate_public_media_url(current_url, settings)
            try:
                with client.stream("GET", current_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if redirect_count == max_redirects:
                            raise _conflict("Remote media source exceeded the redirect limit.")
                        location = response.headers.get("location")
                        if not location:
                            raise _conflict("Remote media source returned an invalid redirect.")
                        next_url = str(httpx.URL(current_url).join(location))
                        current_url = next_url
                        continue
                    if response.status_code != 200:
                        raise _conflict("Remote media source could not be downloaded.")
                    content_length = response.headers.get("content-length")
                    limit = settings.catalog_studio_media_fetch_max_bytes
                    if content_length and int(content_length) > limit:
                        raise _conflict("Remote media source exceeds the allowed byte limit.")
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > limit:
                            raise _conflict("Remote media source exceeds the allowed byte limit.")
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    content_type = response.headers.get("content-type")
            except (httpx.HTTPError, ValueError) as exc:
                raise _conflict("Remote media source could not be downloaded safely.") from exc
            break
        else:  # pragma: no cover - loop always exits or raises
            raise _conflict("Remote media source could not be downloaded.")

    try:
        mime_type = validate_image_bytes(content, content_type, max_bytes=limit)
    except ImageUploadError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[mime_type]
    output_dir = Path(settings.product_image_output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"media-source-{_hash(primary_url)[:24]}.{extension}"
    destination = (output_dir / filename).resolve()
    if destination.parent != output_dir:
        raise _conflict("Remote media source could not be stored safely.")
    if not destination.exists():
        destination.write_bytes(content)
    return destination


def _editable_source_path(image_set: dict[str, Any], settings: Settings) -> Path | None:
    output_dir = Path(settings.product_image_output_dir).resolve()
    stored_path = str(image_set.get("file_path") or "").strip()
    if stored_path:
        candidate = Path(stored_path).resolve()
        if candidate.parent == output_dir and candidate.is_file():
            return candidate

    primary_url = str(image_set.get("primary_url") or "").strip()
    if not primary_url:
        return None
    parsed_url = urlparse(primary_url)
    request_path = parsed_url.path
    url_prefix = f"/{settings.product_image_url_path.strip('/')}".rstrip("/")
    expected_prefix = f"{url_prefix}/"
    public_origin = urlparse(settings.public_base_url)
    same_public_origin = (
        not parsed_url.scheme
        and not parsed_url.netloc
        or (
            parsed_url.scheme == public_origin.scheme
            and parsed_url.hostname == public_origin.hostname
            and (parsed_url.port or (443 if parsed_url.scheme == "https" else 80))
            == (public_origin.port or (443 if public_origin.scheme == "https" else 80))
        )
    )
    if not request_path.startswith(expected_prefix) or not same_public_origin:
        if urlparse(primary_url).scheme == "https":
            return _materialize_remote_source(primary_url, settings)
        return None

    filename = Path(request_path).name
    if not filename or request_path != f"{expected_prefix}{filename}":
        return None
    candidate = (output_dir / filename).resolve()
    if candidate.parent != output_dir or not candidate.is_file():
        return None
    return candidate


def enqueue_catalog_image_job(
    db: Session,
    *,
    workflow_id: str,
    request: CatalogImageCommandRequest,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
    settings: Settings,
) -> CatalogImageJobResponse:
    if not idempotency_key.strip():
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank.")
    workflow = _owned_workflow(db, workflow_id, principal)
    key_hash = _hash(idempotency_key.strip())
    fingerprint = _request_hash(workflow_id, request)
    existing = db.scalar(
        select(ImageGenerationJob).where(
            ImageGenerationJob.workflow_id == workflow_id,
            ImageGenerationJob.idempotency_key_hash == key_hash,
        )
    )
    if existing:
        if existing.request_hash != fingerprint:
            raise _conflict("Idempotency-Key was already used for a different image command.")
        return _job_response(existing)

    _, draft = _current_draft(
        db,
        workflow=workflow,
        draft_id=request.draft_id,
        expected_version=request.expected_draft_version,
        principal=principal,
    )
    if request.variant_index >= len(draft.variants):
        raise HTTPException(status_code=422, detail="variant_index is outside the draft variants.")

    source_path = None
    if request.action == "refine":
        image_set = draft.variants[request.variant_index].image_set
        if image_set.get("approval_status") != "approved" or not image_set.get("file_path"):
            raise _conflict("Image refinement requires an approved Catalog Studio source image.")
        source_path = str(image_set["file_path"])
        if not Path(source_path).is_file():
            raise _conflict("The approved source image is no longer available to refine.")

    options = product_image_options(detail_count=1, settings=settings)
    api_trace_id, api_trace_span_id = current_image_trace_lineage()
    retry_of = _latest_failed_trace_job(
        db,
        workflow_id=workflow.id,
        draft_revision_id=request.draft_id,
        requested_action=request.action,
        requested_variant_index=request.variant_index,
    )
    now = datetime.now(timezone.utc)
    job = ImageGenerationJob(
        id=f"imgjob_{uuid4().hex[:12]}",
        workflow_id=workflow.id,
        draft_revision_id=request.draft_id,
        expected_draft_version=request.expected_draft_version,
        requested_action=request.action,
        requested_variant_index=request.variant_index,
        idempotency_key_hash=key_hash,
        request_hash=fingerprint,
        api_trace_id=api_trace_id,
        api_trace_span_id=api_trace_span_id,
        api_trace_retry_of_job_id=retry_of.id if retry_of else None,
        refinement_prompt=request.refinement_prompt,
        source_image_path=source_path,
        limit=1,
        detail_count=1,
        thumbnail_size=options.thumbnail_size,
        overwrite=True,
        missing_images_only=False,
        model=options.model,
        size=options.size,
        quality=options.quality,
        output_format=options.output_format,
        status=IndexJobStatus.queued.value,
        attempted=0,
        generated=0,
        skipped=0,
        failed_count=0,
        status_breakdown={},
        result_sample=[],
        created_at=now,
    )
    db.add(job)
    db.flush()
    append_workflow_event(
        db,
        workflow_id=workflow.id,
        principal=principal,
        settings=settings,
        commit=False,
        event=WorkflowEventInput(
            client_event_id=f"image-{job.id}-queued",
            stage="image",
            capability="image_generation",
            status="queued",
            business_summary=(
                "Queued a product image refinement." if request.action == "refine"
                else "Queued a product image generation."
            ),
            model=job.model,
            request_payload={
                "action": request.action,
                "draft_id": request.draft_id,
                "draft_version": request.expected_draft_version,
                "variant_index": request.variant_index,
            },
            draft_id=request.draft_id,
            image_job_id=job.id,
        ),
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        concurrent = db.scalar(
            select(ImageGenerationJob).where(
                ImageGenerationJob.workflow_id == workflow_id,
                ImageGenerationJob.idempotency_key_hash == key_hash,
            )
        )
        if concurrent and concurrent.request_hash == fingerprint:
            return _job_response(concurrent)
        raise _conflict("Catalog image state changed; retry with fresh state.") from exc
    db.refresh(job)
    return _job_response(job)


def enqueue_catalog_media_job(
    db: Session,
    *,
    workflow_id: str,
    request: CatalogMediaCommandRequest,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
    settings: Settings,
    moderation_client: Any | None = None,
) -> CatalogImageJobResponse:
    if not idempotency_key.strip():
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank.")
    workflow = _owned_workflow(db, workflow_id, principal)
    key_hash = _hash(idempotency_key.strip())
    fingerprint = _request_hash(workflow_id, request)
    existing = db.scalar(
        select(ImageGenerationJob).where(
            ImageGenerationJob.workflow_id == workflow_id,
            ImageGenerationJob.idempotency_key_hash == key_hash,
        )
    )
    if existing:
        if existing.request_hash != fingerprint:
            raise _conflict("Idempotency-Key was already used for a different media command.")
        return _job_response(existing)

    revision, draft = _current_draft(
        db,
        workflow=workflow,
        draft_id=request.draft_id,
        expected_version=request.expected_draft_version,
        principal=principal,
    )
    _moderate_media_instruction(request, settings=settings, client=moderation_client)
    source = next(
        (asset for asset in draft.media if asset.media_id == request.source_media_id),
        None,
    )
    if source is None or source.approval_status != "approved":
        raise _conflict("Media variations require an approved source image.")
    source_file = _editable_source_path(source.image_set, settings)
    if source_file is None:
        raise _conflict("The approved source image is no longer available for editing.")
    source_path = str(source_file)

    target_media_id = f"media_{uuid4().hex[:20]}"
    display_order = max((asset.display_order for asset in draft.media), default=-1) + 1
    draft.media.append(
        ProductMediaDraft(
            media_id=target_media_id,
            role="variation",
            intent=request.intent,
            source_media_id=source.media_id,
            parameters=request.parameters,
            image_set={},
            approval_status="pending",
            display_order=display_order,
            provenance={},
        )
    )
    revision.snapshot_json = product_draft_snapshot_from_v1(
        draft, revision.snapshot_json
    )
    options = product_image_options(detail_count=1, settings=settings)
    api_trace_id, api_trace_span_id = current_image_trace_lineage()
    retry_of = _latest_failed_trace_job(
        db,
        workflow_id=workflow.id,
        draft_revision_id=revision.id,
        requested_action="refine",
        requested_variant_index=0,
        source_media_id=source.media_id,
        requested_intent=request.intent,
    )
    job = ImageGenerationJob(
        id=f"imgjob_{uuid4().hex[:12]}",
        workflow_id=workflow.id,
        draft_revision_id=revision.id,
        expected_draft_version=request.expected_draft_version,
        requested_action="refine",
        requested_variant_index=0,
        source_media_id=source.media_id,
        target_media_id=target_media_id,
        requested_intent=request.intent,
        idempotency_key_hash=key_hash,
        request_hash=fingerprint,
        api_trace_id=api_trace_id,
        api_trace_span_id=api_trace_span_id,
        api_trace_retry_of_job_id=retry_of.id if retry_of else None,
        refinement_prompt=_media_refinement_prompt(request),
        source_image_path=source_path,
        limit=1,
        detail_count=1,
        thumbnail_size=options.thumbnail_size,
        overwrite=True,
        missing_images_only=False,
        model=options.model,
        size=options.size,
        quality=options.quality,
        output_format=options.output_format,
        status=IndexJobStatus.queued.value,
        attempted=0,
        generated=0,
        skipped=0,
        failed_count=0,
        status_breakdown={},
        result_sample=[],
        created_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.flush()
    append_workflow_event(
        db,
        workflow_id=workflow.id,
        principal=principal,
        settings=settings,
        commit=False,
        event=WorkflowEventInput(
            client_event_id=f"media-{job.id}-queued",
            stage="image",
            capability="image_generation",
            status="queued",
            business_summary=f"Queued a {request.intent} product media variation.",
            model=job.model,
            request_payload={
                "draft_id": revision.id,
                "source_media_id": source.media_id,
                "target_media_id": target_media_id,
                "intent": request.intent,
            },
            draft_id=revision.id,
            image_job_id=job.id,
        ),
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        concurrent = db.scalar(
            select(ImageGenerationJob).where(
                ImageGenerationJob.workflow_id == workflow_id,
                ImageGenerationJob.idempotency_key_hash == key_hash,
            )
        )
        if concurrent and concurrent.request_hash == fingerprint:
            return _job_response(concurrent)
        raise _conflict("Catalog media state changed; retry with fresh state.") from exc
    db.refresh(job)
    return _job_response(job)


def _media_refinement_prompt(request: CatalogMediaCommandRequest) -> str:
    parameter_name = {
        "color": "color",
        "angle": "angle",
        "scene": "scene",
        "scale": "scale",
        "people": "people",
    }.get(request.intent)
    parameter_value = request.parameters.get(parameter_name) if parameter_name else None
    instruction = request.instruction or (
        f"Use this {request.intent}: {parameter_value}"
        if parameter_value is not None
        else f"Create a new {request.intent} presentation"
    )
    return (
        "Preserve the exact product identity, materials, construction, logos, and proportions "
        f"from the approved source image. Create a {request.intent} presentation change only. "
        f"Requested change: {instruction}. Do not add readable text, prices, or unrelated products."
    )


def mutate_catalog_media(
    db: Session,
    *,
    workflow_id: str,
    request: CatalogMediaMutationRequest,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
) -> DraftRevisionResponse:
    if not idempotency_key.strip():
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank.")
    workflow = _owned_workflow(db, workflow_id, principal)
    if workflow.draft_revision_id == request.draft_id:
        revision, _ = _current_draft(
            db,
            workflow=workflow,
            draft_id=request.draft_id,
            expected_version=request.expected_draft_version,
            principal=principal,
        )
    else:
        revision = db.get(CatalogDraftRevision, request.draft_id)
        if revision is None or revision.created_by != principal.provider_user_id:
            raise HTTPException(status_code=404, detail="Catalog draft revision not found.")
    product = product_draft_v2_from_snapshot(revision.snapshot_json)
    media_by_id = {asset.media_id: asset for asset in product.media}

    if request.action == "set_main":
        selected = media_by_id.get(request.media_id or "")
        if selected is None or selected.approval_status != "approved":
            raise _conflict("The main image must be a current approved media asset.")
        product.media = [selected, *(asset for asset in product.media if asset is not selected)]
        for display_order, asset in enumerate(product.media):
            asset.role = "core" if asset is selected else "variation"
            asset.display_order = display_order
    elif request.action == "reorder":
        if set(request.ordered_media_ids) != set(media_by_id):
            raise _conflict("Reorder must include every current media asset exactly once.")
        current_core = next(asset for asset in product.media if asset.role == "core")
        if request.ordered_media_ids[0] != current_core.media_id:
            raise _conflict("Reorder must keep the current main image first.")
        product.media = [media_by_id[media_id] for media_id in request.ordered_media_ids]
        for display_order, asset in enumerate(product.media):
            asset.display_order = display_order
    elif request.action == "remove":
        selected = media_by_id.get(request.media_id or "")
        if selected is None:
            raise _conflict("The media asset is no longer in the current draft.")
        if len(product.media) == 1:
            raise _conflict("The last media asset cannot be removed.")
        if selected.role == "core":
            raise _conflict("Set another main image before removing the current main image.")
        active_job = db.scalar(
            select(ImageGenerationJob.id).where(
                ImageGenerationJob.draft_revision_id == revision.id,
                ImageGenerationJob.source_media_id == selected.media_id,
                ImageGenerationJob.status.in_([
                    IndexJobStatus.queued.value,
                    IndexJobStatus.running.value,
                ]),
            )
        )
        if active_job:
            raise _conflict("Media used by an active image job cannot be removed.")
        product.media = [asset for asset in product.media if asset is not selected]
        for display_order, asset in enumerate(product.media):
            asset.display_order = display_order
    else:
        if request.media_id in media_by_id:
            raise _conflict("The media asset is already present in the current draft.")
        historical = None
        older_revisions = db.scalars(
            select(CatalogDraftRevision)
            .where(
                CatalogDraftRevision.catalog_product_id == revision.catalog_product_id,
                CatalogDraftRevision.created_by == principal.provider_user_id,
                CatalogDraftRevision.id != revision.id,
            )
            .order_by(CatalogDraftRevision.created_at.desc(), CatalogDraftRevision.id.desc())
        ).all()
        for older_revision in older_revisions:
            older_product = product_draft_v2_from_snapshot(older_revision.snapshot_json)
            historical = next(
                (
                    asset
                    for asset in older_product.media
                    if asset.media_id == request.media_id
                    and asset.approval_status == "approved"
                ),
                None,
            )
            if historical is not None:
                break
        if historical is None:
            raise HTTPException(status_code=404, detail="Removed media history was not found.")
        restored = historical.model_copy(deep=True)
        restored.role = "variation"
        restored.display_order = len(product.media)
        product.media.append(restored)

    response, _ = create_draft_from_v2_compatibility(
        db,
        DraftMutationRequestV2(
            expected_version=revision.base_version,
            current_draft_id=revision.id,
            expected_draft_version=request.expected_draft_version,
            moderation_state=revision.moderation_state,
            product=product,
        ),
        idempotency_key=idempotency_key,
        principal=principal,
        path_product_id=revision.catalog_product_id,
    )
    return response


def _moderate_media_instruction(
    request: CatalogMediaCommandRequest,
    *,
    settings: Settings,
    client: Any | None = None,
) -> None:
    instruction = request.instruction
    if not instruction:
        return
    if client is None:
        if not settings.openai_api_key:
            raise HTTPException(status_code=503, detail="Media instruction moderation is unavailable.")
        if OpenAI is None:
            raise HTTPException(status_code=503, detail="Media instruction moderation is unavailable.")
        client = OpenAI(
            api_key=settings.openai_api_key,
            timeout=settings.catalog_studio_responses_timeout_seconds,
        )
    try:
        client_request_id = new_openai_client_request_id("moderation")
        response = client.moderations.create(
            model=settings.catalog_studio_moderation_model,
            input=instruction,
            extra_headers={"X-Client-Request-Id": client_request_id},
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Media instruction moderation failed.") from exc
    results = list(getattr(response, "results", []) or [])
    if not results or bool(getattr(results[0], "flagged", False)):
        raise HTTPException(status_code=422, detail="Media instruction was blocked by safety policy.")


def _variant_set_id(
    *, workflow_id: str, draft_id: str, draft_version: int, primary_job_id: str
) -> str:
    fingerprint = _hash(
        f"{workflow_id}:{draft_id}:{draft_version}:{primary_job_id}"
    )[:20]
    return f"imgset_{fingerprint}"


def _latest_variant_set_jobs(
    db: Session, *, image_variant_set_id: str
) -> list[ImageGenerationJob]:
    jobs = list(
        db.scalars(
            select(ImageGenerationJob)
            .where(ImageGenerationJob.image_variant_set_id == image_variant_set_id)
            .order_by(ImageGenerationJob.created_at, ImageGenerationJob.id)
        ).all()
    )
    latest: dict[int, ImageGenerationJob] = {}
    for job in jobs:
        latest[job.requested_variant_index or 0] = job
    return [latest[index] for index in sorted(latest)]


def _variant_set_response(
    db: Session,
    *,
    workflow: CatalogWorkflow,
    revision: CatalogDraftRevision,
    draft: ProductDraft,
    image_variant_set_id: str,
) -> CatalogImageVariantSetResponse:
    jobs = _latest_variant_set_jobs(db, image_variant_set_id=image_variant_set_id)
    statuses = {job.status for job in jobs}
    if "running" in statuses:
        family_status = "running"
    elif "queued" in statuses:
        family_status = "queued"
    elif statuses == {"failed"}:
        family_status = "failed"
    elif "failed" in statuses:
        family_status = "partially_failed"
    else:
        primary_image = draft.variants[draft.primary_variant_index].image_set
        current_set_id = None
        if primary_image.get("job_id"):
            current_set_id = _variant_set_id(
                workflow_id=workflow.id,
                draft_id=revision.id,
                draft_version=draft_revision_version(db, revision),
                primary_job_id=str(primary_image["job_id"]),
            )
        primary_approved = (
            primary_image.get("approval_status") == "approved"
            and current_set_id == image_variant_set_id
        )
        children_approved = all(
            draft.variants[job.requested_variant_index or 0].image_set.get(
                "approval_status"
            )
            == "approved"
            and draft.variants[job.requested_variant_index or 0].image_set.get("job_id")
            == job.id
            for job in jobs
        )
        family_status = "complete" if primary_approved and children_approved else "review"
    return CatalogImageVariantSetResponse(
        id=image_variant_set_id,
        workflow_id=workflow.id,
        draft_id=revision.id,
        expected_draft_version=draft_revision_version(db, revision),
        primary_variant_index=draft.primary_variant_index,
        status=family_status,
        jobs=[_job_response(job) for job in jobs],
    )


def enqueue_catalog_image_variant_set(
    db: Session,
    *,
    workflow_id: str,
    request: CatalogImageVariantSetRequest,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
    settings: Settings,
) -> CatalogImageVariantSetResponse:
    if not idempotency_key.strip():
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank.")
    workflow = _owned_workflow(db, workflow_id, principal)
    revision, draft = _current_draft(
        db,
        workflow=workflow,
        draft_id=request.draft_id,
        expected_version=request.expected_draft_version,
        principal=principal,
    )
    if len(draft.variants) < 2 or not draft.variant_axes:
        raise HTTPException(
            status_code=422,
            detail="A coordinated image variant set requires multiple declared variants.",
        )
    primary_index = draft.primary_variant_index
    primary_image = draft.variants[primary_index].image_set
    if (
        primary_image.get("approval_status") != "approved"
        or not primary_image.get("file_path")
        or not primary_image.get("job_id")
    ):
        raise _conflict(
            "Coordinated variant generation requires an approved primary image."
        )
    source_path = str(primary_image["file_path"])
    if not Path(source_path).is_file():
        raise _conflict("The approved primary image is no longer available.")

    set_id = _variant_set_id(
        workflow_id=workflow.id,
        draft_id=revision.id,
        draft_version=request.expected_draft_version,
        primary_job_id=str(primary_image["job_id"]),
    )
    existing = _latest_variant_set_jobs(db, image_variant_set_id=set_id)
    latest_by_variant = {job.requested_variant_index or 0: job for job in existing}
    command_key_hash = _hash(idempotency_key.strip())
    options = product_image_options(detail_count=1, settings=settings)
    api_trace_id, api_trace_span_id = current_image_trace_lineage()
    now = datetime.now(timezone.utc)
    created: list[ImageGenerationJob] = []
    primary = draft.variants[primary_index]
    for variant_index, variant in enumerate(draft.variants):
        if variant_index == primary_index:
            continue
        latest = latest_by_variant.get(variant_index)
        if latest is not None and latest.status in {"queued", "running", "succeeded"}:
            continue
        child_key = _hash(f"{set_id}:{variant_index}:{command_key_hash}")
        if db.scalar(
            select(ImageGenerationJob.id).where(
                ImageGenerationJob.workflow_id == workflow.id,
                ImageGenerationJob.idempotency_key_hash == child_key,
            )
        ):
            continue
        changes: list[str] = []
        if "color" in draft.variant_axes:
            changes.append(
                f"color from {primary.color or 'unspecified'} "
                f"to {variant.color or 'unspecified'}"
            )
        if "material" in draft.variant_axes:
            changes.append(
                f"material from {primary.material or 'unspecified'} "
                f"to {variant.material or 'unspecified'}"
            )
        refinement = (
            "Preserve the approved primary product design, silhouette, construction, "
            "camera angle, lighting, and composition exactly. Change only the declared "
            "variant attributes: " + "; ".join(changes) + "."
        )
        child = ImageGenerationJob(
            id=f"imgjob_{uuid4().hex[:12]}",
            workflow_id=workflow.id,
            draft_revision_id=revision.id,
            expected_draft_version=request.expected_draft_version,
            requested_action="refine",
            requested_variant_index=variant_index,
            image_variant_set_id=set_id,
            idempotency_key_hash=child_key,
            request_hash=_hash(refinement),
            api_trace_id=api_trace_id,
            api_trace_span_id=api_trace_span_id,
            api_trace_retry_of_job_id=(
                latest.id
                if latest is not None
                and latest.status == IndexJobStatus.failed.value
                and latest.api_trace_id
                else None
            ),
            refinement_prompt=refinement,
            source_image_path=source_path,
            limit=1,
            detail_count=1,
            thumbnail_size=options.thumbnail_size,
            overwrite=True,
            missing_images_only=False,
            model=options.model,
            size=options.size,
            quality=options.quality,
            output_format=options.output_format,
            status=IndexJobStatus.queued.value,
            attempted=0,
            generated=0,
            skipped=0,
            failed_count=0,
            status_breakdown={},
            result_sample=[],
            created_at=now,
        )
        db.add(child)
        created.append(child)
    if created:
        db.flush()
        append_workflow_event(
            db,
            workflow_id=workflow.id,
            principal=principal,
            settings=settings,
            commit=False,
            event=WorkflowEventInput(
                client_event_id=f"image-variant-set-{set_id}-{command_key_hash[:12]}",
                stage="image",
                capability="image_generation",
                status="queued",
                business_summary=f"Queued {len(created)} coherent product variant image(s).",
                model=options.model,
                request_payload={
                    "image_variant_set_id": set_id,
                    "draft_id": revision.id,
                    "draft_version": request.expected_draft_version,
                    "primary_variant_index": primary_index,
                    "variant_axes": draft.variant_axes,
                    "variant_indexes": [job.requested_variant_index for job in created],
                },
                draft_id=revision.id,
            ),
        )
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            concurrent = _latest_variant_set_jobs(
                db, image_variant_set_id=set_id
            )
            if not concurrent:
                raise _conflict(
                    "Catalog image variant-set state changed; retry with fresh state."
                ) from exc
    else:
        db.rollback()
    revision = db.get(CatalogDraftRevision, revision.id)
    assert revision is not None
    return _variant_set_response(
        db,
        workflow=workflow,
        revision=revision,
        draft=product_draft_v1_from_snapshot(revision.snapshot_json),
        image_variant_set_id=set_id,
    )


def get_catalog_image_variant_set(
    db: Session,
    *,
    workflow_id: str,
    image_variant_set_id: str,
    principal: AuthenticatedPrincipal,
) -> CatalogImageVariantSetResponse:
    workflow = _owned_workflow(db, workflow_id, principal, lock=False)
    jobs = _latest_variant_set_jobs(db, image_variant_set_id=image_variant_set_id)
    if not jobs or any(job.workflow_id != workflow.id for job in jobs):
        raise HTTPException(status_code=404, detail="Catalog image variant set not found.")
    revision = db.get(CatalogDraftRevision, jobs[0].draft_revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="Catalog draft revision not found.")
    draft = product_draft_v1_from_snapshot(revision.snapshot_json)
    return _variant_set_response(
        db,
        workflow=workflow,
        revision=revision,
        draft=draft,
        image_variant_set_id=image_variant_set_id,
    )


def get_catalog_image_job(
    db: Session,
    *,
    workflow_id: str,
    job_id: str,
    principal: AuthenticatedPrincipal,
) -> CatalogImageJobResponse:
    _owned_workflow(db, workflow_id, principal, lock=False)
    job = db.get(ImageGenerationJob, job_id)
    if job is None or job.workflow_id != workflow_id:
        raise HTTPException(status_code=404, detail="Catalog image job not found.")
    return _job_response(job)


def record_stale_catalog_image_job(
    db: Session,
    *,
    job: ImageGenerationJob,
    settings: Settings,
    completed_at: datetime,
) -> None:
    workflow = db.get(CatalogWorkflow, job.workflow_id) if job.workflow_id else None
    if workflow is None:
        return
    append_workflow_event(
        db,
        workflow_id=workflow.id,
        principal=_principal(workflow),
        settings=settings,
        commit=False,
        event=WorkflowEventInput(
            client_event_id=f"image-{job.id}-worker-stale",
            stage="image",
            capability="image_generation",
            status="failed",
            business_summary="The image worker stopped before completing this request.",
            model=job.model,
            error_code="image_worker_stale",
            retryable=True,
            image_job_id=job.id,
            completed_at=completed_at,
        ),
    )


def _image_prompt(draft: ProductDraft, variant_index: int, refinement: str | None) -> str:
    variant = draft.variants[variant_index]
    direction = str(draft.metadata.get("image_direction") or "").strip()
    lines = [
        "Create a clean luxury ecommerce catalog photograph of one product on a neutral studio background.",
        "Use realistic lighting and accurate materials. Do not include people, readable text, logos, watermarks, price tags, or extra props.",
        f"Product: {draft.title}",
        f"Description: {draft.description}",
        f"Category: {draft.category}",
        f"Color: {variant.color or 'unspecified'}",
        f"Material: {variant.material or 'unspecified'}",
    ]
    if draft.design_specification is not None:
        design = draft.design_specification
        lines.extend(
            [
                f"Product type: {design.product_type}",
                f"Silhouette: {design.silhouette}",
                f"Construction: {design.construction}",
                "Distinguishing features: " + "; ".join(design.distinguishing_features),
            ]
        )
    if direction:
        lines.append(f"Art direction: {direction}")
    if refinement:
        lines.append(f"Refinement: {refinement}")
    return "\n".join(lines)


def _response_usage(response: Any) -> dict:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        return usage.model_dump(mode="json", exclude_none=True)
    return dict(usage) if isinstance(usage, dict) else {}


def _error_details(exc: Exception) -> tuple[str, bool]:
    error = getattr(exc, "error", None)
    code = str(
        getattr(error, "code", None)
        or getattr(exc, "code", None)
        or exc.__class__.__name__
    )[:128]
    status_code = getattr(exc, "status_code", None)
    retryable = (
        status_code == 429
        or (isinstance(status_code, int) and status_code >= 500)
        or isinstance(exc, TimeoutError)
        or type(exc).__name__ in {"APITimeoutError", "APIConnectionError", "ConnectError"}
    )
    if code == "moderation_blocked":
        retryable = False
    return code, retryable


def _remove_files(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _stale_catalog_image_result(
    db: Session,
    *,
    job: ImageGenerationJob,
    workflow: CatalogWorkflow,
    principal: AuthenticatedPrincipal,
    settings: Settings,
    written: list[Path],
) -> CatalogImageJobResponse:
    _remove_files(written)
    job.status = IndexJobStatus.failed.value
    job.attempted = 1
    job.failed_count = 1
    job.status_breakdown = {"stale": 1}
    job.error_message = "Image result was discarded because the catalog draft changed."
    job.finished_at = datetime.now(timezone.utc)
    append_workflow_event(
        db,
        workflow_id=workflow.id,
        principal=principal,
        settings=settings,
        commit=False,
        event=WorkflowEventInput(
            client_event_id=f"image-{job.id}-stale",
            stage="image",
            capability="image_generation",
            status="failed",
            business_summary="Discarded an image generated for an older draft version.",
            model=job.model,
            response_payload={
                "image_variant_set_id": job.image_variant_set_id,
                "variant_index": job.requested_variant_index,
            },
            error_code="stale_draft",
            retryable=True,
            image_job_id=job.id,
            completed_at=job.finished_at,
        ),
    )
    db.commit()
    return _job_response(job)


def _process_catalog_image_job(
    db: Session,
    *,
    job: ImageGenerationJob,
    settings: Settings,
    client: Any | None = None,
) -> CatalogImageJobResponse:
    workflow = db.get(CatalogWorkflow, job.workflow_id) if job.workflow_id else None
    if workflow is None:
        raise RuntimeError("Catalog workflow no longer exists.")
    principal = _principal(workflow)
    started = datetime.now(timezone.utc)
    timer = monotonic()
    append_workflow_event(
        db,
        workflow_id=workflow.id,
        principal=principal,
        settings=settings,
        event=WorkflowEventInput(
            client_event_id=f"image-{job.id}-running",
            stage="image",
            capability="image_generation",
            status="running",
            business_summary="Generating the staged catalog image.",
            model=job.model,
            request_payload={
                "image_variant_set_id": job.image_variant_set_id,
                "variant_index": job.requested_variant_index,
            },
            image_job_id=job.id,
            started_at=started,
        ),
    )
    job = db.get(ImageGenerationJob, job.id)
    assert job is not None
    revision = db.get(CatalogDraftRevision, job.draft_revision_id)
    if revision is None:
        raise RuntimeError("Catalog draft no longer exists.")
    draft = product_draft_v1_from_snapshot(revision.snapshot_json)
    variant_index = job.requested_variant_index or 0
    prompt = _image_prompt(draft, variant_index, job.refinement_prompt)
    options = product_image_options(
        model=job.model,
        size=job.size,
        quality=job.quality,
        output_format=job.output_format,
        detail_count=1,
        thumbnail_size=job.thumbnail_size,
        settings=settings,
    )
    extension = "jpg" if options.output_format == "jpeg" else options.output_format
    detail_path = options.output_dir / f"{job.id}-variant-{variant_index}-detail-1.{extension}"
    thumb_path = options.output_dir / f"{job.id}-variant-{variant_index}-thumb.{extension}"
    url_base = f"{options.public_base_url}/{options.url_path.strip('/')}"
    detail_url = f"{url_base}/{detail_path.name}"
    thumb_url = f"{url_base}/{thumb_path.name}"
    written: list[Path] = []

    db.expire_all()
    job = db.get(ImageGenerationJob, job.id)
    workflow = db.get(CatalogWorkflow, job.workflow_id if job else None)
    revision = db.get(CatalogDraftRevision, job.draft_revision_id if job else None)
    if job is None or workflow is None or revision is None:
        raise RuntimeError("Catalog image state disappeared before generation started.")
    if (
        workflow.draft_revision_id != revision.id
        or draft_revision_version(db, revision) != job.expected_draft_version
    ):
        return _stale_catalog_image_result(
            db,
            job=job,
            workflow=workflow,
            principal=principal,
            settings=settings,
            written=written,
        )

    client_request_id = new_openai_client_request_id("images")
    try:
        if client is None:
            if OpenAI is None:
                raise RuntimeError("The openai package is not available.")
            client = OpenAI(timeout=options.request_timeout_seconds)
        params = {
            "model": options.model,
            "prompt": prompt,
            "size": options.size,
            "quality": options.quality,
            "output_format": options.output_format,
            "n": 1,
            "extra_headers": {"X-Client-Request-Id": client_request_id},
        }
        if job.requested_action == "refine":
            if job.target_media_id:
                params["input_fidelity"] = "high"
            with Path(job.source_image_path or "").open("rb") as source:
                response = client.images.edit(image=source, **params)
        else:
            response = client.images.generate(**params)
        image_base64 = next(
            (item.b64_json for item in response.data if getattr(item, "b64_json", None)), None
        )
        if not image_base64:
            raise RuntimeError("OpenAI image response did not include b64_json data.")
        image_bytes = base64.b64decode(image_base64)
        options.output_dir.mkdir(parents=True, exist_ok=True)
        detail_path.write_bytes(image_bytes)
        written.append(detail_path)
        written.append(thumb_path)
        _write_thumbnail(
            image_bytes,
            thumb_path,
            output_format=options.output_format,
            size=options.thumbnail_size,
        )

        db.expire_all()
        job = db.get(ImageGenerationJob, job.id)
        workflow = db.scalar(
            select(CatalogWorkflow)
            .where(CatalogWorkflow.id == (job.workflow_id if job else None))
            .with_for_update()
        )
        revision = db.get(CatalogDraftRevision, job.draft_revision_id if job else None)
        if job is None or workflow is None or revision is None:
            raise RuntimeError("Catalog image state disappeared while the image was generated.")
        actual_version = draft_revision_version(db, revision)
        if (
            workflow.draft_revision_id != revision.id
            or actual_version != job.expected_draft_version
        ):
            return _stale_catalog_image_result(
                db,
                job=job,
                workflow=workflow,
                principal=principal,
                settings=settings,
                written=written,
            )

        current = product_draft_v1_from_snapshot(revision.snapshot_json)
        generated_image_set = {
            "thumbnail_url": thumb_url,
            "primary_url": detail_url,
            "detail_urls": [detail_url],
            "generated_by": job.model,
            "size": job.size,
            "quality": job.quality,
            "output_format": job.output_format,
            "source": "catalog_studio",
            "approval_status": "review",
            "job_id": job.id,
            "file_path": str(detail_path),
        }
        if job.target_media_id:
            media_asset = next(
                (asset for asset in current.media if asset.media_id == job.target_media_id),
                None,
            )
            if media_asset is None or media_asset.source_media_id != job.source_media_id:
                raise RuntimeError("The target product media asset is no longer current.")
            generated_image_set["history"] = []
            media_asset.image_set = generated_image_set
            media_asset.provenance = {
                "model": job.model,
                "job_id": job.id,
                "source_media_id": job.source_media_id,
                "intent": job.requested_intent,
            }
        else:
            variant = current.variants[variant_index]
            previous = dict(variant.image_set) if variant.image_set else None
            history = list((previous or {}).get("history") or [])
            if previous:
                history.append({key: value for key, value in previous.items() if key != "history"})
            generated_image_set["history"] = history
            variant.image_link = detail_url
            variant.image_set = generated_image_set
        revision.snapshot_json = product_draft_snapshot_from_v1(
            current, revision.snapshot_json
        )
        job.status = IndexJobStatus.succeeded.value
        job.attempted = 1
        job.generated = 1
        job.status_breakdown = {"generated": 1}
        job.result_sample = [{"image_link": detail_url, "thumbnail_link": thumb_url}]
        job.error_message = None
        job.finished_at = datetime.now(timezone.utc)
        response_id, provider_request_id = openai_request_ids(response)
        append_workflow_event(
            db,
            workflow_id=workflow.id,
            principal=principal,
            settings=settings,
            commit=False,
            event=WorkflowEventInput(
                client_event_id=f"image-{job.id}-succeeded",
                stage="image",
                capability="image_generation",
                status="succeeded",
                business_summary="Generated an image for review in the catalog draft.",
                model=job.model,
                request_id=provider_request_id or response_id,
                duration_ms=max(0, int((monotonic() - timer) * 1000)),
                usage=_response_usage(response),
                request_payload={"client_request_id": client_request_id},
                response_payload={
                    "response_id": response_id,
                    "provider_request_id": provider_request_id,
                    "image_url": detail_url,
                    "thumbnail_url": thumb_url,
                    "approval_status": "review",
                    "image_variant_set_id": job.image_variant_set_id,
                    "variant_index": variant_index,
                    "target_media_id": job.target_media_id,
                    "intent": job.requested_intent,
                },
                draft_id=revision.id,
                image_job_id=job.id,
                started_at=started,
                completed_at=job.finished_at,
            ),
        )
        db.commit()
        return _job_response(job)
    except Exception as exc:
        db.rollback()
        _remove_files(written)
        job = db.get(ImageGenerationJob, job.id)
        workflow = db.get(CatalogWorkflow, job.workflow_id if job else None)
        if job is None or workflow is None:
            raise
        code, retryable = _error_details(exc)
        job.status = IndexJobStatus.failed.value
        job.attempted = 1
        job.failed_count = 1
        job.status_breakdown = {"failed": 1}
        job.error_message = str(exc)[:2000]
        job.finished_at = datetime.now(timezone.utc)
        append_workflow_event(
            db,
            workflow_id=workflow.id,
            principal=principal,
            settings=settings,
            commit=False,
            event=WorkflowEventInput(
                client_event_id=f"image-{job.id}-failed",
                stage="image",
                capability="image_generation",
                status="failed",
                business_summary="The catalog image request failed.",
                model=job.model,
                duration_ms=max(0, int((monotonic() - timer) * 1000)),
                error_code=code,
                retryable=retryable,
                request_payload={"client_request_id": client_request_id},
                response_payload={
                    "image_variant_set_id": job.image_variant_set_id,
                    "variant_index": job.requested_variant_index,
                },
                image_job_id=job.id,
                started_at=started,
                completed_at=job.finished_at,
            ),
        )
        db.commit()
        return _job_response(job)


def process_catalog_image_job(
    db: Session,
    *,
    job: ImageGenerationJob,
    settings: Settings,
    client: Any | None = None,
) -> CatalogImageJobResponse:
    try:
        return _process_catalog_image_job(
            db,
            job=job,
            settings=settings,
            client=client,
        )
    except Exception as exc:
        db.rollback()
        current = db.get(ImageGenerationJob, job.id)
        if current is None:
            raise
        current.status = IndexJobStatus.failed.value
        current.attempted = max(1, current.attempted)
        current.failed_count = max(1, current.failed_count)
        current.status_breakdown = {"failed": 1}
        current.error_message = str(exc)[:2000]
        current.finished_at = datetime.now(timezone.utc)
        workflow = (
            db.get(CatalogWorkflow, current.workflow_id) if current.workflow_id else None
        )
        if workflow is not None:
            code, retryable = _error_details(exc)
            append_workflow_event(
                db,
                workflow_id=workflow.id,
                principal=_principal(workflow),
                settings=settings,
                commit=False,
                event=WorkflowEventInput(
                    client_event_id=f"image-{current.id}-failed",
                    stage="image",
                    capability="image_generation",
                    status="failed",
                    business_summary="The catalog image request failed.",
                    model=current.model,
                    error_code=code,
                    retryable=retryable,
                    image_job_id=current.id,
                    completed_at=current.finished_at,
                ),
            )
        db.commit()
        return _job_response(current)
def approve_catalog_image(
    db: Session,
    *,
    workflow_id: str,
    job_id: str,
    request: CatalogImageApprovalRequest,
    principal: AuthenticatedPrincipal,
    settings: Settings,
) -> CatalogImageApprovalResponse:
    workflow = _owned_workflow(db, workflow_id, principal)
    revision, draft = _current_draft(
        db,
        workflow=workflow,
        draft_id=request.draft_id,
        expected_version=request.expected_draft_version,
        principal=principal,
    )
    job = db.get(ImageGenerationJob, job_id)
    if job is None or job.workflow_id != workflow.id or job.draft_revision_id != revision.id:
        raise HTTPException(status_code=404, detail="Catalog image job not found.")
    if job.status != IndexJobStatus.succeeded.value:
        raise _conflict("Only a successfully generated image can be approved.")
    variant_index = job.requested_variant_index or 0
    media_asset = next(
        (asset for asset in draft.media if asset.media_id == job.target_media_id),
        None,
    ) if job.target_media_id else None
    image_set = media_asset.image_set if media_asset else draft.variants[variant_index].image_set
    if image_set.get("job_id") != job.id:
        raise _conflict("This image is no longer the current result for the catalog draft.")
    if image_set.get("approval_status") == "approved":
        recorded_intent = (
            str(media_asset.provenance.get("approval_intent") or "add")
            if media_asset
            else "add"
        )
        recorded_predecessor = (
            media_asset.predecessor_media_id if media_asset else None
        )
        if (
            recorded_intent != request.approval_intent
            or recorded_predecessor != request.replace_media_id
        ):
            raise _conflict("This image was already approved with a different intent.")
        return CatalogImageApprovalResponse(
            job_id=job.id,
            draft_id=revision.id,
            variant_index=variant_index,
            media_id=job.target_media_id,
            approval_intent=recorded_intent,  # type: ignore[arg-type]
            predecessor_media_id=recorded_predecessor,
        )
    predecessor = None
    if request.approval_intent == "replace":
        predecessor = next(
            (
                asset
                for asset in draft.media
                if asset.media_id == request.replace_media_id
                and asset.approval_status == "approved"
            ),
            None,
        )
        if predecessor is None or predecessor is media_asset:
            raise _conflict("Replacement approval requires a current approved media asset.")
    image_set["approval_status"] = "approved"
    if media_asset:
        media_asset.approval_status = "approved"
        media_asset.provenance = {
            **media_asset.provenance,
            "approval_intent": request.approval_intent,
        }
        if predecessor is not None:
            media_asset.role = predecessor.role
            media_asset.display_order = predecessor.display_order
            media_asset.predecessor_media_id = predecessor.media_id
            media_asset.provenance = {
                **media_asset.provenance,
                "predecessor_media_id": predecessor.media_id,
            }
            draft.media = [asset for asset in draft.media if asset is not predecessor]
            draft.media.sort(key=lambda asset: (asset.display_order, asset.media_id))
            core = next((asset for asset in draft.media if asset.role == "core"), None)
            if core is not None:
                draft.media = [core, *(asset for asset in draft.media if asset is not core)]
            for display_order, asset in enumerate(draft.media):
                asset.display_order = display_order
    elif not draft.media and variant_index == draft.primary_variant_index:
        draft.media.append(
            ProductMediaDraft(
                media_id=f"media_{uuid4().hex[:20]}",
                role="core",
                intent="manual",
                parameters={},
                image_set=dict(image_set),
                approval_status="approved",
                display_order=0,
                provenance={"model": job.model, "job_id": job.id},
            )
        )
    revision.snapshot_json = product_draft_snapshot_from_v1(
        draft, revision.snapshot_json
    )
    append_workflow_event(
        db,
        workflow_id=workflow.id,
        principal=principal,
        settings=settings,
        commit=False,
        event=WorkflowEventInput(
            client_event_id=f"image-{job.id}-approved",
            stage="image",
            capability="image_generation",
            status="completed",
            business_summary="Approved the generated image for catalog publication.",
            model=job.model,
            response_payload={
                "approval_status": "approved",
                "approval_intent": request.approval_intent,
                "predecessor_media_id": predecessor.media_id if predecessor else None,
                "variant_index": variant_index,
                "image_variant_set_id": job.image_variant_set_id,
                "media_id": job.target_media_id,
            },
            draft_id=revision.id,
            image_job_id=job.id,
            completed_at=datetime.now(timezone.utc),
        ),
    )
    db.commit()
    return CatalogImageApprovalResponse(
        job_id=job.id,
        draft_id=revision.id,
        variant_index=variant_index,
        media_id=job.target_media_id,
        approval_intent=request.approval_intent,
        predecessor_media_id=predecessor.media_id if predecessor else None,
    )
