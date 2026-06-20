from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any
from uuid import uuid4

from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.orm import Session

from app.api_traces.context import (
    TraceCaptureContext,
    current_trace_capture_context,
)
from app.api_traces.schemas import (
    ApiTraceProjection,
    TraceArtifactProjection,
    TraceEventProjection,
    TraceLinkProjection,
    TraceSpanProjection,
)
from app.api_traces.service import ApiTraceRecorder
from app.config import Settings
from app.models import CatalogWorkflow, CatalogWorkflowEvent, ImageGenerationJob


_PENDING_RECORDINGS_KEY = "api_trace.pending_recordings"
_IN_FLIGHT_STATUSES = {"queued", "started", "running", "retrying"}


@dataclass(frozen=True, slots=True)
class _PendingRecording:
    settings: Settings
    context: TraceCaptureContext
    projection: ApiTraceProjection


def _stable_hex(*parts: object, length: int) -> str:
    value = ":".join(str(part) for part in parts)
    return hashlib.sha256(value.encode()).hexdigest()[:length]


def new_openai_client_request_id(capability: str) -> str:
    label = "".join(char for char in capability.casefold() if char.isalnum())[:16]
    return f"sh-{label or 'openai'}-{uuid4().hex}"


def openai_request_ids(response: Any) -> tuple[str | None, str | None]:
    response_id = getattr(response, "id", None)
    provider_request_id = getattr(response, "_request_id", None) or getattr(
        response, "request_id", None
    )
    return (
        str(response_id) if response_id else None,
        str(provider_request_id) if provider_request_id else None,
    )


def current_image_trace_lineage() -> tuple[str | None, str | None]:
    context = current_trace_capture_context()
    if not context or not context.authorized:
        return None, None
    return context.trace_id, context.parent_span_id or context.span_id


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _event_timing(event: CatalogWorkflowEvent) -> tuple[datetime, datetime, int]:
    started_at = _as_utc(event.started_at or event.created_at)
    if event.completed_at:
        completed_at = _as_utc(event.completed_at)
    elif event.duration_ms is not None:
        completed_at = started_at + timedelta(milliseconds=max(0, event.duration_ms))
    else:
        completed_at = max(started_at, _as_utc(event.created_at))
    duration_ms = event.duration_ms
    if duration_ms is None:
        duration_ms = max(0, int((completed_at - started_at).total_seconds() * 1000))
    return started_at, completed_at, duration_ms


def _trace_status(status: str) -> str:
    if status == "failed":
        return "failed"
    if status == "blocked":
        return "blocked"
    return "completed"


def _event_type(event: CatalogWorkflowEvent) -> str:
    namespace = {
        "responses": "openai.responses",
        "moderation": "openai.moderation",
        "image_generation": "openai.images",
        "realtime": "openai.realtime",
        "publication": "catalog.publication",
        "catalog": "catalog.persistence",
        "workflow": "catalog.workflow",
    }.get(event.capability, "catalog.workflow")
    return f"{namespace}.{event.status}"


def _compact_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in attributes.items() if value not in (None, {}, [])
    }


def _event_attributes(event: CatalogWorkflowEvent) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "stage": event.stage,
        "capability": event.capability,
        "status": event.status,
        "retryable": event.retryable,
        "model": event.model,
        "provider_request_id": event.request_id,
        "usage": dict(event.usage_json or {}),
        "moderation": dict(event.moderation_json or {}),
        "request": dict(event.request_json or {}),
        "response": dict(event.response_json or {}),
        "error_code": event.error_code,
    }
    return _compact_attributes(attributes)


def _publication_spans(
    *,
    trace_id: str,
    parent_span_id: str,
    event: CatalogWorkflowEvent,
    attributes: dict[str, Any],
    started_at: datetime,
    completed_at: datetime,
) -> list[TraceSpanProjection]:
    spans: list[TraceSpanProjection] = []
    for operation, name in (
        ("catalog.validation", "Validate product readiness"),
        ("catalog.persistence", "Persist canonical product"),
        ("catalog.publication", "Publish catalog product"),
    ):
        spans.append(
            TraceSpanProjection(
                span_id=_stable_hex(trace_id, event.id, operation, length=16),
                parent_span_id=parent_span_id,
                name=name,
                operation=operation,
                service="sterling-hollis-be",
                status=event.status,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=max(
                    0, int((completed_at - started_at).total_seconds() * 1000)
                ),
                attributes=attributes,
            )
        )
    return spans


def _draft_success_spans(
    *,
    trace_id: str,
    parent_span_id: str,
    event: CatalogWorkflowEvent,
    attributes: dict[str, Any],
    started_at: datetime,
    completed_at: datetime,
    duration_ms: int,
) -> list[TraceSpanProjection]:
    response_span = TraceSpanProjection(
        span_id=_stable_hex(trace_id, event.id, "responses", length=16),
        parent_span_id=parent_span_id,
        name=event.business_summary,
        operation="openai.responses",
        service="openai",
        status=event.status,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        attributes=attributes,
    )
    completion_spans = [
        TraceSpanProjection(
            span_id=_stable_hex(trace_id, event.id, operation, length=16),
            parent_span_id=parent_span_id,
            name=name,
            operation=operation,
            service="sterling-hollis-be",
            status="succeeded",
            started_at=completed_at,
            completed_at=completed_at,
            duration_ms=0,
            attributes={"workflow_id": attributes.get("workflow_id")},
        )
        for operation, name in (
            ("catalog.validation", "Validate generated product draft"),
            ("catalog.persistence", "Persist private draft revision"),
            ("ui.ready", "Prepare draft for Catalog Studio"),
        )
    ]
    return [response_span, *completion_spans]


def catalog_workflow_event_projection(
    *,
    workflow: CatalogWorkflow,
    event: CatalogWorkflowEvent,
    context: TraceCaptureContext,
    image_job: ImageGenerationJob | None = None,
) -> ApiTraceProjection:
    started_at, completed_at, duration_ms = _event_timing(event)
    async_parent_trace_id = image_job.api_trace_id if image_job else None
    async_parent_span_id = image_job.api_trace_span_id if image_job else None
    is_async_worker = bool(async_parent_trace_id and context.trace_id != async_parent_trace_id)

    trace_id = context.trace_id or _stable_hex(workflow.id, event.id, length=32)
    trace_status = _trace_status(event.status)
    event_attributes = _event_attributes(event)
    attributes = {
        "workflow_id": workflow.id,
        "stage": event.stage,
        "capability": event.capability,
        "image_job_id": image_job.id if image_job else None,
    }
    attributes = _compact_attributes(attributes)

    links: list[TraceLinkProjection] = []
    if is_async_worker:
        worker_in_flight = event.status in _IN_FLIGHT_STATUSES
        if worker_in_flight:
            trace_status = "running"
        root_span_id = context.span_id or _stable_hex(trace_id, "worker", length=16)
        server_span_id = root_span_id
        spans = [
            TraceSpanProjection(
                span_id=root_span_id,
                name="Catalog image worker",
                operation="worker.image_generation",
                service="sterling-hollis-be",
                status=trace_status,
                started_at=started_at,
                completed_at=None if worker_in_flight else completed_at,
                duration_ms=None if worker_in_flight else duration_ms,
                attributes=attributes,
            )
        ]
        links.append(
            TraceLinkProjection(
                link_id=_stable_hex(trace_id, "initiated_by", length=32),
                span_id=root_span_id,
                linked_trace_id=async_parent_trace_id,
                linked_span_id=async_parent_span_id,
                relationship="initiated_by",
                attributes={"image_job_id": image_job.id, "attempt": image_job.attempted},
            )
        )
        retry_of_job_id = getattr(image_job, "api_trace_retry_of_job_id", None)
        if retry_of_job_id:
            links.append(
                TraceLinkProjection(
                    link_id=_stable_hex(trace_id, "retry_of", length=32),
                    span_id=root_span_id,
                    linked_trace_id=_stable_hex(
                        retry_of_job_id, "worker", length=32
                    ),
                    linked_span_id=_stable_hex(
                        retry_of_job_id, "worker-root", length=16
                    ),
                    relationship="retry_of",
                    attributes={"image_job_id": retry_of_job_id},
                )
            )
        trace_name = "Generate catalog image"
    else:
        root_span_id = (
            context.parent_span_id
            or context.span_id
            or _stable_hex(trace_id, "browser", length=16)
        )
        server_span_id = (
            context.span_id
            if context.span_id and context.span_id != root_span_id
            else _stable_hex(trace_id, "api", length=16)
        )
        spans = [
            TraceSpanProjection(
                span_id=root_span_id,
                name="Catalog Studio browser request",
                operation="http.client",
                service="sterling-hollis-fe",
                status=trace_status,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                attributes={"surface": context.surface},
            ),
            TraceSpanProjection(
                span_id=server_span_id,
                parent_span_id=root_span_id,
                name="Catalog Studio API",
                operation="http.server",
                service="sterling-hollis-be",
                status=trace_status,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                attributes=attributes,
            ),
        ]
        trace_name = workflow.title

    if event.capability == "publication":
        event_spans = _publication_spans(
            trace_id=trace_id,
            parent_span_id=server_span_id,
            event=event,
            attributes={**event_attributes, "workflow_id": workflow.id},
            started_at=started_at,
            completed_at=completed_at,
        )
        event_span_id = event_spans[-1].span_id
    elif (
        event.capability == "responses"
        and event.stage == "draft"
        and event.status == "succeeded"
    ):
        event_spans = _draft_success_spans(
            trace_id=trace_id,
            parent_span_id=server_span_id,
            event=event,
            attributes={**event_attributes, "workflow_id": workflow.id},
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
        )
        event_span_id = event_spans[0].span_id
    else:
        event_spans = [
            TraceSpanProjection(
                span_id=_stable_hex(trace_id, event.id, length=16),
                parent_span_id=server_span_id,
                name=event.business_summary,
                operation=_event_type(event).rsplit(".", 1)[0],
                service=(
                    "openai"
                    if event.capability
                    in {"responses", "moderation", "image_generation", "realtime"}
                    else "sterling-hollis-be"
                ),
                status=event.status,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                attributes=event_attributes,
            )
        ]
        event_span_id = event_spans[0].span_id
    spans.extend(event_spans)

    artifacts: list[TraceArtifactProjection] = []
    response = dict(event.response_json or {})
    if event.capability == "image_generation" and event.status == "succeeded":
        artifacts.append(
            TraceArtifactProjection(
                artifact_id=_stable_hex(trace_id, event.id, "image", length=32),
                span_id=event_span_id,
                artifact_type="image",
                name="Generated catalog image",
                media_type="image/*",
                attributes=_compact_attributes(
                    {
                        "image_job_id": image_job.id if image_job else None,
                        "image_url": response.get("image_url"),
                        "thumbnail_url": response.get("thumbnail_url"),
                        "approval_status": response.get("approval_status"),
                    }
                ),
            )
        )

    return ApiTraceProjection(
        trace_id=trace_id,
        surface=context.surface,
        name=trace_name,
        root_span_id=root_span_id,
        status=trace_status,
        started_at=started_at,
        completed_at=(
            None
            if is_async_worker
            and event.status in _IN_FLIGHT_STATUSES
            else completed_at
        ),
        duration_ms=(
            None
            if is_async_worker
            and event.status in _IN_FLIGHT_STATUSES
            else duration_ms
        ),
        attributes=attributes,
        spans=spans,
        links=links,
        events=[
            TraceEventProjection(
                event_id=_stable_hex(trace_id, event.id, "event", length=32),
                span_id=event_span_id,
                sequence=max(0, event.sequence),
                name=event.business_summary,
                event_type=_event_type(event),
                status=event.status,
                occurred_at=completed_at,
                attributes=event_attributes,
            )
        ],
        artifacts=artifacts,
    )


def _worker_context(
    workflow: CatalogWorkflow,
    image_job: ImageGenerationJob,
) -> TraceCaptureContext | None:
    if not image_job.api_trace_id:
        return None
    worker_trace_id = _stable_hex(image_job.id, "worker", length=32)
    worker_span_id = _stable_hex(image_job.id, "worker-root", length=16)
    return TraceCaptureContext(
        owner_provider=workflow.owner_provider,
        owner_provider_user_id=workflow.owner_provider_user_id,
        surface="catalog-studio",
        authorized=True,
        trace_id=worker_trace_id,
        span_id=worker_span_id,
    )


def queue_catalog_workflow_event(
    db: Session,
    *,
    workflow: CatalogWorkflow,
    event: CatalogWorkflowEvent,
    settings: Settings,
    image_job: ImageGenerationJob | None = None,
) -> bool:
    if not settings.api_trace_capture_enabled:
        return False
    context = current_trace_capture_context()
    if (not context or not context.authorized) and image_job is not None:
        context = _worker_context(workflow, image_job)
    if not context or not context.authorized:
        return False
    projection = catalog_workflow_event_projection(
        workflow=workflow,
        event=event,
        context=context,
        image_job=image_job,
    )
    pending = db.info.setdefault(_PENDING_RECORDINGS_KEY, [])
    pending.append(
        _PendingRecording(
            settings=settings,
            context=context,
            projection=projection,
        )
    )
    return True


@sqlalchemy_event.listens_for(Session, "after_commit")
def _flush_pending_recordings(db: Session) -> None:
    pending = db.info.pop(_PENDING_RECORDINGS_KEY, [])
    for item in pending:
        ApiTraceRecorder(settings=item.settings).record(
            context=item.context,
            projection=item.projection,
        )


@sqlalchemy_event.listens_for(Session, "after_rollback")
def _discard_pending_recordings(db: Session) -> None:
    db.info.pop(_PENDING_RECORDINGS_KEY, None)


__all__ = [
    "catalog_workflow_event_projection",
    "current_image_trace_lineage",
    "new_openai_client_request_id",
    "openai_request_ids",
    "queue_catalog_workflow_event",
]
