from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog.admin_schemas import ProductDraft
from app.catalog.image_schemas import (
    CatalogImageApprovalRequest,
    CatalogImageApprovalResponse,
    CatalogImageCommandRequest,
    CatalogImageJobResponse,
)
from app.catalog.workflow_schemas import WorkflowEventInput
from app.config import Settings
from app.models import CatalogDraftRevision, CatalogWorkflow, ImageGenerationJob
from app.schemas import IndexJobStatus
from app.services.auth.clerk import AuthenticatedPrincipal
from app.services.catalog_admin import draft_revision_version
from app.services.catalog_workflow import append_workflow_event
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
    return revision, ProductDraft.model_validate(revision.snapshot_json)


def _job_response(job: ImageGenerationJob) -> CatalogImageJobResponse:
    return CatalogImageJobResponse(
        id=job.id,
        workflow_id=job.workflow_id or "",
        draft_id=job.draft_revision_id or "",
        expected_draft_version=job.expected_draft_version or 0,
        action=job.requested_action or "generate",  # type: ignore[arg-type]
        variant_index=job.requested_variant_index or 0,
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
            image_job_id=job.id,
            started_at=started,
        ),
    )
    job = db.get(ImageGenerationJob, job.id)
    assert job is not None
    revision = db.get(CatalogDraftRevision, job.draft_revision_id)
    if revision is None:
        raise RuntimeError("Catalog draft no longer exists.")
    draft = ProductDraft.model_validate(revision.snapshot_json)
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
        }
        if job.requested_action == "refine":
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

        current = ProductDraft.model_validate(revision.snapshot_json)
        variant = current.variants[variant_index]
        previous = dict(variant.image_set) if variant.image_set else None
        history = list((previous or {}).get("history") or [])
        if previous:
            history.append({key: value for key, value in previous.items() if key != "history"})
        variant.image_link = detail_url
        variant.image_set = {
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
            "history": history,
        }
        revision.snapshot_json = current.model_dump(mode="json")
        job.status = IndexJobStatus.succeeded.value
        job.attempted = 1
        job.generated = 1
        job.status_breakdown = {"generated": 1}
        job.result_sample = [{"image_link": detail_url, "thumbnail_link": thumb_url}]
        job.error_message = None
        job.finished_at = datetime.now(timezone.utc)
        request_id = getattr(response, "_request_id", None) or getattr(response, "request_id", None)
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
                request_id=request_id,
                duration_ms=max(0, int((monotonic() - timer) * 1000)),
                usage=_response_usage(response),
                response_payload={
                    "image_url": detail_url,
                    "thumbnail_url": thumb_url,
                    "approval_status": "review",
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
    image_set = draft.variants[variant_index].image_set
    if image_set.get("job_id") != job.id:
        raise _conflict("This image is no longer the current result for the draft variant.")
    if image_set.get("approval_status") == "approved":
        return CatalogImageApprovalResponse(
            job_id=job.id,
            draft_id=revision.id,
            variant_index=variant_index,
        )
    image_set["approval_status"] = "approved"
    revision.snapshot_json = draft.model_dump(mode="json")
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
            response_payload={"approval_status": "approved", "variant_index": variant_index},
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
    )
