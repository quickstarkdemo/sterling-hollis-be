from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
from typing import Any, Mapping
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog.workflow_schemas import (
    CatalogWorkflowResponse,
    WorkflowDeveloperEvent,
    WorkflowEventInput,
    WorkflowEventResponse,
)
from app.config import Settings
from app.models import (
    CatalogDraftRevision,
    CatalogProduct,
    CatalogSuggestionSet,
    CatalogWorkflow,
    CatalogWorkflowEvent,
    ImageGenerationJob,
)
from app.services.auth.clerk import AuthenticatedPrincipal


REDACTED = "[REDACTED]"
RETENTION_MARKER = {"_retention": "expired"}

_BUILT_IN_REDACTED_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "credentials",
    "customer",
    "customer_id",
    "address",
    "email",
    "first_name",
    "full_name",
    "headers",
    "identity",
    "image_bytes",
    "instructions",
    "password",
    "phone",
    "ip_address",
    "last_name",
    "private_reasoning",
    "provider_user_id",
    "raw_audio",
    "reasoning",
    "secret",
    "system",
    "system_prompt",
    "token",
    "user_id",
}

_ALLOWED_KEYS = {
    "action",
    "approval_status",
    "attributes",
    "availability",
    "base_version",
    "blocked",
    "brand",
    "capability",
    "categories",
    "category",
    "category_scores",
    "citation_ids",
    "color",
    "context",
    "decision",
    "description",
    "detail_urls",
    "draft",
    "draft_id",
    "draft_version",
    "error",
    "error_code",
    "expires_at",
    "expected_draft_version",
    "flagged",
    "gender",
    "id",
    "image_direction",
    "image_job_id",
    "image_link",
    "image_set",
    "image_url",
    "input",
    "input_origin",
    "inventory",
    "inventory_qty",
    "items",
    "link",
    "material",
    "metadata",
    "model",
    "mode",
    "moderation",
    "mutation",
    "name",
    "objective_weight",
    "output",
    "presenter_input",
    "price",
    "price_max",
    "price_min",
    "primary_url",
    "query_scopes",
    "product",
    "product_id",
    "products",
    "published_product_id",
    "request",
    "request_id",
    "response",
    "result",
    "retryable",
    "season",
    "safety_identifier_attached",
    "session_id",
    "size",
    "stage",
    "status",
    "store_id",
    "summary",
    "source_asset_count",
    "source_asset_ids",
    "suggestion_count",
    "suggestion_set_id",
    "thumbnail_url",
    "title",
    "tool",
    "tool_name",
    "tool_names",
    "tools",
    "target_path",
    "target_paths",
    "usage",
    "unknown_count",
    "variant_index",
    "variant",
    "variant_id",
    "variants",
}

_SECRET_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
)


def _normalized_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().casefold()).strip("_")


def _redacted_keys(settings: Settings) -> set[str]:
    configured = {
        _normalized_key(key)
        for key in settings.catalog_studio_trace_redacted_keys.split(",")
        if key.strip()
    }
    return _BUILT_IN_REDACTED_KEYS | configured


def _key_is_redacted(key: str, redacted_keys: set[str]) -> bool:
    if key in redacted_keys:
        return True
    parts = set(key.split("_"))
    return bool(parts & {"authorization", "password", "secret", "token"})


def _safe_text(value: object, *, max_length: int) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    if len(text) <= max_length:
        return text
    omitted = len(text) - max_length
    marker = f"...[truncated {omitted} chars]"
    if len(marker) >= max_length:
        return marker[:max_length]
    return f"{text[: max_length - len(marker)]}{marker}"


def _sanitize_value(
    value: Any,
    *,
    settings: Settings,
    redacted_keys: set[str],
    depth: int,
) -> Any:
    if depth > settings.catalog_studio_trace_max_depth:
        return {"_truncated": "maximum depth reached"}
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _safe_text(
            value, max_length=max(1, settings.catalog_studio_trace_max_string_length)
        )
    if isinstance(value, bytes | bytearray | memoryview):
        return REDACTED
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        omitted = 0
        truncated = 0
        allowed_count = 0
        max_keys = max(1, settings.catalog_studio_trace_max_object_keys)
        for raw_key in sorted(value, key=lambda item: str(item).casefold()):
            key = _normalized_key(raw_key)
            if not key:
                omitted += 1
                continue
            if _key_is_redacted(key, redacted_keys):
                projected[key] = REDACTED
                continue
            if key not in _ALLOWED_KEYS:
                omitted += 1
                continue
            if allowed_count >= max_keys:
                truncated += 1
                continue
            projected[key] = _sanitize_value(
                value[raw_key],
                settings=settings,
                redacted_keys=redacted_keys,
                depth=depth + 1,
            )
            allowed_count += 1
        if omitted:
            projected["_omitted_fields"] = omitted
        if truncated:
            projected["_truncated_fields"] = truncated
        return projected
    if isinstance(value, (list, tuple, set)):
        rows = sorted(value, key=repr) if isinstance(value, set) else list(value)
        limit = max(1, settings.catalog_studio_trace_max_array_length)
        projected = [
            _sanitize_value(
                row,
                settings=settings,
                redacted_keys=redacted_keys,
                depth=depth + 1,
            )
            for row in rows[:limit]
        ]
        if len(rows) > limit:
            projected.append({"_truncated_items": len(rows) - limit})
        return projected
    return REDACTED


def sanitize_workflow_payload(value: Any, *, settings: Settings) -> dict:
    projected = _sanitize_value(
        value,
        settings=settings,
        redacted_keys=_redacted_keys(settings),
        depth=0,
    )
    if not isinstance(projected, dict):
        projected = {"value": projected}
    return _enforce_total_bytes(projected, settings=settings)


def _enforce_total_bytes(projected: dict, *, settings: Settings) -> dict:
    encoded = json.dumps(
        projected, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    max_bytes = max(96, settings.catalog_studio_trace_max_bytes)
    if len(encoded) > max_bytes:
        return {
            "_truncated": f"payload exceeded {max_bytes} bytes",
            "_projected_bytes": len(encoded),
        }
    return projected


def normalize_usage(value: Mapping[str, Any] | None) -> dict[str, int]:
    raw_usage = dict(value or {})
    input_details = raw_usage.get("input_tokens_details")
    output_details = raw_usage.get("output_tokens_details")
    if isinstance(input_details, Mapping):
        raw_usage.setdefault("cached_tokens", input_details.get("cached_tokens"))
        raw_usage.setdefault("audio_input_tokens", input_details.get("audio_tokens"))
    if isinstance(output_details, Mapping):
        raw_usage.setdefault("reasoning_tokens", output_details.get("reasoning_tokens"))
        raw_usage.setdefault("audio_output_tokens", output_details.get("audio_tokens"))
    allowed = (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "audio_input_tokens",
        "audio_output_tokens",
        "images_generated",
    )
    normalized: dict[str, int] = {}
    for key in allowed:
        raw = raw_usage.get(key)
        if isinstance(raw, bool):
            continue
        if (
            isinstance(raw, int | float)
            and not isinstance(raw, bool)
            and math.isfinite(raw)
            and raw >= 0
        ):
            normalized[key] = int(raw)
    return normalized


def normalize_moderation(value: Mapping[str, Any] | None, *, settings: Settings) -> dict:
    raw = value or {}
    normalized: dict[str, Any] = {}
    for key in ("flagged", "blocked"):
        if isinstance(raw.get(key), bool):
            normalized[key] = raw[key]
    if isinstance(raw.get("decision"), str):
        normalized["decision"] = _safe_text(
            raw["decision"], max_length=settings.catalog_studio_trace_max_string_length
        )
    categories = raw.get("categories")
    if isinstance(categories, Mapping):
        normalized["categories"] = sorted(
            _safe_text(key, max_length=80) for key, flagged in categories.items() if flagged
        )[: settings.catalog_studio_trace_max_array_length]
    elif isinstance(categories, list):
        normalized["categories"] = [
            _safe_text(item, max_length=80) for item in categories
        ][: settings.catalog_studio_trace_max_array_length]
    scores = raw.get("category_scores")
    if isinstance(scores, Mapping):
        normalized["category_scores"] = {
            _safe_text(key, max_length=80): round(float(score), 6)
            for key, score in sorted(scores.items())[
                : max(1, settings.catalog_studio_trace_max_object_keys)
            ]
            if isinstance(score, int | float)
            and not isinstance(score, bool)
            and math.isfinite(score)
        }
    return _enforce_total_bytes(normalized, settings=settings)


def _event_hash(event: WorkflowEventInput) -> str:
    payload = json.dumps(
        event.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _event_key(client_event_id: str) -> str:
    return hashlib.sha256(client_event_id.encode()).hexdigest()


def _workflow_request_hash(
    *,
    title: str,
    business_summary: str,
    draft_id: str | None,
    image_job_id: str | None,
    published_product_id: str | None,
) -> str:
    payload = json.dumps(
        {
            "title": title,
            "business_summary": business_summary,
            "draft_id": draft_id,
            "image_job_id": image_job_id,
            "published_product_id": published_product_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _workflow_for_idempotency_key(
    db: Session,
    *,
    principal: AuthenticatedPrincipal,
    key_hash: str,
) -> CatalogWorkflow | None:
    return db.scalar(
        select(CatalogWorkflow).where(
            CatalogWorkflow.owner_provider == principal.provider,
            CatalogWorkflow.owner_provider_user_id == principal.provider_user_id,
            CatalogWorkflow.idempotency_key_hash == key_hash,
        )
    )


def _validate_links(
    db: Session,
    *,
    draft_id: str | None = None,
    image_job_id: str | None = None,
    published_product_id: str | None = None,
) -> None:
    checks = (
        (CatalogDraftRevision, draft_id, "draft"),
        (ImageGenerationJob, image_job_id, "image job"),
        (CatalogProduct, published_product_id, "published product"),
    )
    for model, record_id, label in checks:
        if record_id and db.get(model, record_id) is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Linked Catalog Studio {label} was not found.",
            )


def _owner_workflow(db: Session, workflow_id: str, principal: AuthenticatedPrincipal) -> CatalogWorkflow:
    workflow = db.scalar(select(CatalogWorkflow).where(CatalogWorkflow.id == workflow_id).with_for_update())
    if not workflow or workflow.owner_provider_user_id != principal.provider_user_id:
        raise HTTPException(status_code=404, detail="Catalog Studio catalog workflow not found.")
    return workflow


def cleanup_expired_workflow_payloads(
    db: Session,
    *,
    settings: Settings,
    now: datetime | None = None,
    commit: bool = True,
) -> int:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, settings.catalog_studio_trace_retention_days))
    result = db.execute(
        update(CatalogWorkflowEvent)
        .where(
            CatalogWorkflowEvent.created_at < cutoff,
            CatalogWorkflowEvent.payload_expired.is_(False),
        )
        .values(
            usage_json=dict(RETENTION_MARKER),
            moderation_json=dict(RETENTION_MARKER),
            request_json=dict(RETENTION_MARKER),
            response_json=dict(RETENTION_MARKER),
            payload_expired=True,
        )
        .execution_options(synchronize_session="fetch")
    )
    scrubbed = int(result.rowcount or 0)
    if scrubbed and commit:
        db.commit()
    return scrubbed


def start_catalog_workflow(
    db: Session,
    *,
    principal: AuthenticatedPrincipal,
    title: str,
    business_summary: str,
    settings: Settings,
    idempotency_key: str,
    draft_id: str | None = None,
    image_job_id: str | None = None,
    published_product_id: str | None = None,
    now: datetime | None = None,
) -> CatalogWorkflow:
    now = now or datetime.now(timezone.utc)
    idempotency_key = idempotency_key.strip()
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank.")
    scrubbed = cleanup_expired_workflow_payloads(
        db, settings=settings, now=now, commit=False
    )
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
    request_hash = _workflow_request_hash(
        title=title,
        business_summary=business_summary,
        draft_id=draft_id,
        image_job_id=image_job_id,
        published_product_id=published_product_id,
    )
    existing = _workflow_for_idempotency_key(
        db, principal=principal, key_hash=key_hash
    )
    if existing:
        if existing.request_hash != request_hash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency-Key was already used for a different catalog workflow.",
            )
        if scrubbed:
            db.commit()
        return existing
    _validate_links(
        db,
        draft_id=draft_id,
        image_job_id=image_job_id,
        published_product_id=published_product_id,
    )
    safe_summary = _safe_text(
        business_summary, max_length=settings.catalog_studio_trace_max_string_length
    )
    workflow = CatalogWorkflow(
        id=f"workflow_{uuid4().hex[:24]}",
        owner_provider=principal.provider,
        owner_provider_user_id=principal.provider_user_id,
        idempotency_key_hash=key_hash,
        request_hash=request_hash,
        title=_safe_text(title, max_length=255),
        business_summary=safe_summary,
        status="started",
        current_stage="workflow",
        next_event_sequence=2,
        draft_revision_id=draft_id,
        image_job_id=image_job_id,
        published_product_id=published_product_id,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=max(1, settings.catalog_studio_trace_retention_days)),
    )
    initial = CatalogWorkflowEvent(
        id=f"event_{uuid4().hex[:24]}",
        workflow_id=workflow.id,
        client_event_id="workflow-started",
        input_hash=hashlib.sha256(f"{workflow.id}:started".encode()).hexdigest(),
        sequence=1,
        stage="workflow",
        capability="workflow",
        status="started",
        business_summary=safe_summary,
        usage_json={},
        moderation_json={},
        request_json={},
        response_json={},
        retryable=False,
        payload_expired=False,
        started_at=now,
        created_at=now,
    )
    db.add_all([workflow, initial])
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        concurrent = _workflow_for_idempotency_key(
            db, principal=principal, key_hash=key_hash
        )
        if concurrent and concurrent.request_hash == request_hash:
            return concurrent
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Catalog Studio catalog workflow state changed; retry with fresh state.",
        ) from exc
    return workflow


def append_workflow_event(
    db: Session,
    *,
    workflow_id: str,
    principal: AuthenticatedPrincipal,
    event: WorkflowEventInput,
    settings: Settings,
    now: datetime | None = None,
    commit: bool = True,
) -> CatalogWorkflowEvent:
    now = now or datetime.now(timezone.utc)
    scrubbed = cleanup_expired_workflow_payloads(
        db, settings=settings, now=now, commit=False
    )
    workflow = _owner_workflow(db, workflow_id, principal)
    fingerprint = _event_hash(event)
    client_event_key = _event_key(event.client_event_id)
    existing = db.scalar(
        select(CatalogWorkflowEvent).where(
            CatalogWorkflowEvent.workflow_id == workflow_id,
            CatalogWorkflowEvent.client_event_id == client_event_key,
        )
    )
    if existing:
        if existing.input_hash != fingerprint:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="client_event_id was already used for a different workflow event.",
            )
        if scrubbed and commit:
            db.commit()
        return existing

    _validate_links(
        db,
        draft_id=event.draft_id,
        image_job_id=event.image_job_id,
        published_product_id=event.published_product_id,
    )
    duration_ms = event.duration_ms
    if duration_ms is None and event.started_at and event.completed_at:
        duration_ms = max(
            0,
            int((event.completed_at - event.started_at).total_seconds() * 1000),
        )

    row = CatalogWorkflowEvent(
        id=f"event_{uuid4().hex[:24]}",
        workflow_id=workflow.id,
        client_event_id=client_event_key,
        input_hash=fingerprint,
        sequence=workflow.next_event_sequence,
        stage=event.stage,
        capability=event.capability,
        status=event.status,
        business_summary=_safe_text(
            event.business_summary,
            max_length=settings.catalog_studio_trace_max_string_length,
        ),
        model=_safe_text(event.model, max_length=128) if event.model else None,
        request_id=(
            _safe_text(event.request_id, max_length=128) if event.request_id else None
        ),
        duration_ms=duration_ms,
        usage_json=normalize_usage(event.usage),
        moderation_json=normalize_moderation(event.moderation, settings=settings),
        request_json=sanitize_workflow_payload(event.request_payload, settings=settings),
        response_json=sanitize_workflow_payload(event.response_payload, settings=settings),
        error_code=(
            _safe_text(event.error_code, max_length=128) if event.error_code else None
        ),
        retryable=event.retryable,
        payload_expired=False,
        started_at=event.started_at,
        completed_at=event.completed_at,
        created_at=now,
    )
    workflow.next_event_sequence += 1
    workflow.current_stage = event.stage
    workflow.status = event.status
    workflow.updated_at = now
    workflow.expires_at = now + timedelta(days=max(1, settings.catalog_studio_trace_retention_days))
    if event.draft_id:
        workflow.draft_revision_id = event.draft_id
    if event.image_job_id:
        workflow.image_job_id = event.image_job_id
    if event.published_product_id:
        workflow.published_product_id = event.published_product_id
    db.add(row)
    if commit:
        db.commit()
    else:
        db.flush()
    return row


def project_workflow_event(event: CatalogWorkflowEvent, *, developer: bool) -> WorkflowEventResponse:
    details = None
    if developer:
        details = WorkflowDeveloperEvent(
            model=event.model,
            request_id=event.request_id,
            duration_ms=event.duration_ms,
            usage=dict(event.usage_json or {}),
            moderation=dict(event.moderation_json or {}),
            request_payload=dict(event.request_json or {}),
            response_payload=dict(event.response_json or {}),
            error_code=event.error_code,
            payload_expired=event.payload_expired,
        )
    return WorkflowEventResponse(
        id=event.id,
        sequence=event.sequence,
        stage=event.stage,
        capability=event.capability,  # type: ignore[arg-type]
        status=event.status,  # type: ignore[arg-type]
        business_summary=event.business_summary,
        retryable=event.retryable,
        created_at=event.created_at,
        developer=details,
    )


def get_catalog_workflow_projection(
    db: Session,
    *,
    workflow_id: str,
    principal: AuthenticatedPrincipal,
    developer: bool,
    settings: Settings,
) -> CatalogWorkflowResponse:
    cleanup_expired_workflow_payloads(db, settings=settings)
    workflow = db.get(CatalogWorkflow, workflow_id)
    is_owner = bool(workflow and workflow.owner_provider_user_id == principal.provider_user_id)
    if not workflow or (not is_owner and not settings.catalog_studio_shared_workflows):
        raise HTTPException(status_code=404, detail="Catalog Studio catalog workflow not found.")
    if developer and not is_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Developer details are available only to the catalog workflow owner.",
        )
    events = db.scalars(
        select(CatalogWorkflowEvent)
        .where(CatalogWorkflowEvent.workflow_id == workflow.id)
        .order_by(CatalogWorkflowEvent.sequence)
    ).all()
    suggestion_set_ids = []
    if is_owner:
        suggestion_set_ids = list(
            db.scalars(
                select(CatalogSuggestionSet.id)
                .where(
                    CatalogSuggestionSet.workflow_id == workflow.id,
                    CatalogSuggestionSet.owner_provider == workflow.owner_provider,
                    CatalogSuggestionSet.owner_provider_user_id
                    == workflow.owner_provider_user_id,
                )
                .order_by(CatalogSuggestionSet.created_at, CatalogSuggestionSet.id)
            ).all()
        )
    return CatalogWorkflowResponse(
        id=workflow.id,
        title=workflow.title,
        business_summary=workflow.business_summary,
        status=workflow.status,
        current_stage=workflow.current_stage,
        draft_id=workflow.draft_revision_id,
        image_job_id=workflow.image_job_id,
        published_product_id=workflow.published_product_id,
        suggestion_set_ids=suggestion_set_ids,
        is_owner=is_owner,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
        expires_at=workflow.expires_at,
        events=[project_workflow_event(event, developer=developer) for event in events],
    )
