from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import logging
from typing import Any, TypeVar

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.api_traces.context import TraceCaptureContext
from app.api_traces.schemas import (
    ApiTraceProjection,
    TraceArtifactProjection,
    TraceEventProjection,
    TraceLinkProjection,
    TraceSpanProjection,
)
from app.config import Settings
from app.database import SessionLocal
from app.models import (
    ApiTrace,
    ApiTraceArtifact,
    ApiTraceEvent,
    ApiTraceLink,
    ApiTraceSpan,
)
from app.observability.redaction import (
    RETENTION_MARKER,
    RedactionPolicy,
    configured_redacted_keys,
    safe_observability_text,
    sanitize_observability_payload,
)


logger = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class TraceCleanupResult:
    payloads_expired: int = 0
    traces_deleted: int = 0


def _row_id(kind: str, trace_id: str, public_id: str) -> str:
    digest = hashlib.sha256(f"{kind}:{trace_id}:{public_id}".encode()).hexdigest()[:48]
    return f"{kind}_{digest}"


def _redaction_policy(settings: Settings) -> RedactionPolicy:
    return RedactionPolicy(
        max_depth=settings.api_trace_max_depth,
        max_string_length=settings.api_trace_max_string_length,
        max_array_length=settings.api_trace_max_array_length,
        max_object_keys=settings.api_trace_max_object_keys,
        max_bytes=settings.api_trace_max_bytes,
        redacted_keys=configured_redacted_keys(settings.api_trace_redacted_keys),
    )


def _select_new(
    items: Iterable[T],
    *,
    existing_ids: set[str],
    identity: Callable[[T], str],
    limit: int,
) -> tuple[list[T], int]:
    new_items = [item for item in items if identity(item) not in existing_ids]
    capacity = max(0, max(1, limit) - len(existing_ids))
    selected = new_items[:capacity]
    return selected, len(new_items) - len(selected)


def _ordered_spans(projection: ApiTraceProjection) -> list[TraceSpanProjection]:
    children: dict[str | None, list[TraceSpanProjection]] = {}
    for span in projection.spans:
        children.setdefault(span.parent_span_id, []).append(span)
    for rows in children.values():
        rows.sort(key=lambda span: (span.started_at, span.span_id))

    ordered: list[TraceSpanProjection] = []

    def visit(span: TraceSpanProjection) -> None:
        ordered.append(span)
        for child in children.get(span.span_id, []):
            visit(child)

    root = next(span for span in projection.spans if span.span_id == projection.root_span_id)
    visit(root)
    return ordered


def _payload(value: Any, *, policy: RedactionPolicy, expired: bool = False) -> dict:
    if expired:
        return dict(RETENTION_MARKER)
    return sanitize_observability_payload(value, policy=policy)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def cleanup_expired_api_traces(
    db: Session,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> TraceCleanupResult:
    now = _as_utc(now or datetime.now(timezone.utc))
    expired_payload_ids = list(
        db.scalars(
            select(ApiTrace.id).where(
                ApiTrace.payload_expires_at <= now,
                ApiTrace.payload_expired.is_(False),
            )
        ).all()
    )
    if expired_payload_ids:
        marker = dict(RETENTION_MARKER)
        db.execute(
            update(ApiTrace)
            .where(ApiTrace.id.in_(expired_payload_ids))
            .values(attributes_json=marker, payload_expired=True)
        )
        for model in (ApiTraceSpan, ApiTraceLink, ApiTraceEvent, ApiTraceArtifact):
            db.execute(
                update(model)
                .where(model.trace_id.in_(expired_payload_ids))
                .values(attributes_json=dict(RETENTION_MARKER))
            )

    expired_trace_ids = list(
        db.scalars(select(ApiTrace.id).where(ApiTrace.metadata_expires_at <= now)).all()
    )
    if expired_trace_ids:
        for model in (ApiTraceArtifact, ApiTraceEvent, ApiTraceLink, ApiTraceSpan):
            db.execute(delete(model).where(model.trace_id.in_(expired_trace_ids)))
        db.execute(delete(ApiTrace).where(ApiTrace.id.in_(expired_trace_ids)))

    if commit and (expired_payload_ids or expired_trace_ids):
        db.commit()
    return TraceCleanupResult(
        payloads_expired=len(expired_payload_ids),
        traces_deleted=len(expired_trace_ids),
    )


class ApiTraceRecorder:
    """Persist sanitized projections in an isolated, fail-open transaction."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: Callable[[], Session] = SessionLocal,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory

    def record(
        self,
        *,
        context: TraceCaptureContext,
        projection: ApiTraceProjection,
        now: datetime | None = None,
    ) -> bool:
        if not self.settings.api_trace_capture_enabled or not context.authorized:
            return False
        if not context.owner_provider or not context.owner_provider_user_id:
            return False
        if context.surface != projection.surface:
            return False

        try:
            with self.session_factory() as db:
                self._record(db, context=context, projection=projection, now=now)
                db.commit()
            return True
        except Exception as exc:  # recorder failures must never affect business behavior
            logger.warning("API trace recording failed (%s).", type(exc).__name__)
            return False

    def _record(
        self,
        db: Session,
        *,
        context: TraceCaptureContext,
        projection: ApiTraceProjection,
        now: datetime | None,
    ) -> None:
        recorded_at = _as_utc(now or datetime.now(timezone.utc))
        policy = _redaction_policy(self.settings)
        trace = db.get(ApiTrace, projection.trace_id)
        if trace and (
            trace.owner_provider != context.owner_provider
            or trace.owner_provider_user_id != context.owner_provider_user_id
        ):
            raise ValueError("trace ownership mismatch")

        existing_span_ids = set(
            db.scalars(
                select(ApiTraceSpan.span_id).where(
                    ApiTraceSpan.trace_id == projection.trace_id
                )
            ).all()
        )
        existing_link_ids = set(
            db.scalars(
                select(ApiTraceLink.link_id).where(
                    ApiTraceLink.trace_id == projection.trace_id
                )
            ).all()
        )
        existing_event_ids = set(
            db.scalars(
                select(ApiTraceEvent.event_id).where(
                    ApiTraceEvent.trace_id == projection.trace_id
                )
            ).all()
        )
        existing_artifact_ids = set(
            db.scalars(
                select(ApiTraceArtifact.artifact_id).where(
                    ApiTraceArtifact.trace_id == projection.trace_id
                )
            ).all()
        )

        spans, omitted_spans = _select_new(
            _ordered_spans(projection),
            existing_ids=existing_span_ids,
            identity=lambda span: span.span_id,
            limit=self.settings.api_trace_max_spans,
        )
        kept_span_ids = existing_span_ids | {span.span_id for span in spans}
        eligible_links = [
            item for item in projection.links if not item.span_id or item.span_id in kept_span_ids
        ]
        links, bounded_links = _select_new(
            sorted(eligible_links, key=lambda item: item.link_id),
            existing_ids=existing_link_ids,
            identity=lambda item: item.link_id,
            limit=self.settings.api_trace_max_links,
        )
        eligible_events = [
            item for item in projection.events if not item.span_id or item.span_id in kept_span_ids
        ]
        events, bounded_events = _select_new(
            sorted(eligible_events, key=lambda item: (item.sequence, item.event_id)),
            existing_ids=existing_event_ids,
            identity=lambda item: item.event_id,
            limit=self.settings.api_trace_max_events,
        )
        eligible_artifacts = [
            item for item in projection.artifacts if not item.span_id or item.span_id in kept_span_ids
        ]
        artifacts, bounded_artifacts = _select_new(
            sorted(eligible_artifacts, key=lambda item: item.artifact_id),
            existing_ids=existing_artifact_ids,
            identity=lambda item: item.artifact_id,
            limit=self.settings.api_trace_max_artifacts,
        )
        omitted_links = bounded_links + len(projection.links) - len(eligible_links)
        omitted_events = bounded_events + len(projection.events) - len(eligible_events)
        omitted_artifacts = (
            bounded_artifacts + len(projection.artifacts) - len(eligible_artifacts)
        )
        truncation = {
            key: count
            for key, count in (
                ("spans", omitted_spans),
                ("links", omitted_links),
                ("events", omitted_events),
                ("artifacts", omitted_artifacts),
            )
            if count
        }

        payload_expires_at = recorded_at + timedelta(
            hours=max(1, self.settings.api_trace_payload_retention_hours)
        )
        metadata_expires_at = max(
            payload_expires_at,
            recorded_at
            + timedelta(days=max(1, self.settings.api_trace_metadata_retention_days)),
        )
        if trace is None:
            trace = ApiTrace(
                id=projection.trace_id,
                projection_version=projection.projection_version,
                owner_provider=context.owner_provider,
                owner_provider_user_id=context.owner_provider_user_id,
                surface=projection.surface,
                name=safe_observability_text(projection.name, max_length=128),
                root_span_id=projection.root_span_id,
                status=projection.status,
                duration_ms=projection.duration_ms,
                attributes_json=_payload(projection.attributes, policy=policy),
                truncation_json=truncation,
                payload_expired=False,
                started_at=projection.started_at,
                completed_at=projection.completed_at,
                payload_expires_at=payload_expires_at,
                metadata_expires_at=metadata_expires_at,
                created_at=recorded_at,
                updated_at=recorded_at,
            )
            db.add(trace)
            db.flush()
        else:
            trace.status = projection.status
            trace.duration_ms = projection.duration_ms
            trace.completed_at = projection.completed_at
            trace.updated_at = recorded_at
            trace.metadata_expires_at = max(
                _as_utc(trace.metadata_expires_at), metadata_expires_at
            )
            if not trace.payload_expired:
                trace.attributes_json = _payload(projection.attributes, policy=policy)
                trace.truncation_json = truncation
                trace.payload_expires_at = max(
                    _as_utc(trace.payload_expires_at), payload_expires_at
                )

        payload_expired = trace.payload_expired
        for span in spans:
            db.add(
                ApiTraceSpan(
                    id=_row_id("span", trace.id, span.span_id),
                    trace_id=trace.id,
                    span_id=span.span_id,
                    parent_span_id=span.parent_span_id,
                    name=safe_observability_text(span.name, max_length=128),
                    operation=safe_observability_text(span.operation, max_length=64),
                    service=safe_observability_text(span.service, max_length=128),
                    status=span.status,
                    duration_ms=span.duration_ms,
                    attributes_json=_payload(
                        span.attributes, policy=policy, expired=payload_expired
                    ),
                    started_at=span.started_at,
                    completed_at=span.completed_at,
                    created_at=recorded_at,
                )
            )

        self._record_links(
            db, trace.id, links, policy=policy, payload_expired=payload_expired,
            recorded_at=recorded_at
        )
        self._record_events(
            db, trace.id, events, policy=policy, payload_expired=payload_expired,
            recorded_at=recorded_at
        )
        self._record_artifacts(
            db, trace.id, artifacts, policy=policy, payload_expired=payload_expired,
            recorded_at=recorded_at
        )

    def _record_links(
        self,
        db: Session,
        trace_id: str,
        links: list[TraceLinkProjection],
        *,
        policy: RedactionPolicy,
        payload_expired: bool,
        recorded_at: datetime,
    ) -> None:
        for link in links:
            db.add(
                ApiTraceLink(
                    id=_row_id("link", trace_id, link.link_id),
                    trace_id=trace_id,
                    link_id=link.link_id,
                    span_id=link.span_id,
                    linked_trace_id=link.linked_trace_id,
                    linked_span_id=link.linked_span_id,
                    relationship=link.relationship,
                    attributes_json=_payload(
                        link.attributes, policy=policy, expired=payload_expired
                    ),
                    created_at=recorded_at,
                )
            )

    def _record_events(
        self,
        db: Session,
        trace_id: str,
        events: list[TraceEventProjection],
        *,
        policy: RedactionPolicy,
        payload_expired: bool,
        recorded_at: datetime,
    ) -> None:
        for event in events:
            db.add(
                ApiTraceEvent(
                    id=_row_id("event", trace_id, event.event_id),
                    trace_id=trace_id,
                    event_id=event.event_id,
                    span_id=event.span_id,
                    sequence=event.sequence,
                    name=safe_observability_text(event.name, max_length=128),
                    event_type=event.event_type,
                    status=event.status,
                    attributes_json=_payload(
                        event.attributes, policy=policy, expired=payload_expired
                    ),
                    occurred_at=event.occurred_at,
                    created_at=recorded_at,
                )
            )

    def _record_artifacts(
        self,
        db: Session,
        trace_id: str,
        artifacts: list[TraceArtifactProjection],
        *,
        policy: RedactionPolicy,
        payload_expired: bool,
        recorded_at: datetime,
    ) -> None:
        for artifact in artifacts:
            db.add(
                ApiTraceArtifact(
                    id=_row_id("artifact", trace_id, artifact.artifact_id),
                    trace_id=trace_id,
                    artifact_id=artifact.artifact_id,
                    span_id=artifact.span_id,
                    artifact_type=artifact.artifact_type,
                    name=safe_observability_text(artifact.name, max_length=128),
                    media_type=artifact.media_type,
                    size_bytes=artifact.size_bytes,
                    attributes_json=_payload(
                        artifact.attributes, policy=policy, expired=payload_expired
                    ),
                    created_at=recorded_at,
                )
            )


def get_trace_projection(
    db: Session,
    *,
    trace_id: str,
    owner_provider: str,
    owner_provider_user_id: str,
    now: datetime | None = None,
) -> ApiTraceProjection | None:
    trace = db.get(ApiTrace, trace_id)
    if not trace or (
        trace.owner_provider != owner_provider
        or trace.owner_provider_user_id != owner_provider_user_id
    ):
        return None
    projected_at = _as_utc(now or datetime.now(timezone.utc))
    if _as_utc(trace.metadata_expires_at) <= projected_at:
        return None
    payload_expired = trace.payload_expired or (
        _as_utc(trace.payload_expires_at) <= projected_at
    )

    def attributes(value: dict | None) -> dict:
        if payload_expired:
            return dict(RETENTION_MARKER)
        return dict(value or {})

    spans = db.scalars(
        select(ApiTraceSpan)
        .where(ApiTraceSpan.trace_id == trace.id)
        .order_by(ApiTraceSpan.started_at, ApiTraceSpan.span_id)
    ).all()
    links = db.scalars(
        select(ApiTraceLink)
        .where(ApiTraceLink.trace_id == trace.id)
        .order_by(ApiTraceLink.link_id)
    ).all()
    events = db.scalars(
        select(ApiTraceEvent)
        .where(ApiTraceEvent.trace_id == trace.id)
        .order_by(ApiTraceEvent.sequence, ApiTraceEvent.event_id)
    ).all()
    artifacts = db.scalars(
        select(ApiTraceArtifact)
        .where(ApiTraceArtifact.trace_id == trace.id)
        .order_by(ApiTraceArtifact.artifact_id)
    ).all()
    return ApiTraceProjection(
        projection_version=trace.projection_version,
        trace_id=trace.id,
        surface=trace.surface,
        name=trace.name,
        root_span_id=trace.root_span_id,
        status=trace.status,
        started_at=_as_utc(trace.started_at),
        completed_at=_as_utc(trace.completed_at) if trace.completed_at else None,
        duration_ms=trace.duration_ms,
        attributes=attributes(trace.attributes_json),
        truncation=dict(trace.truncation_json or {}),
        payload_expired=payload_expired,
        spans=[
            TraceSpanProjection(
                span_id=row.span_id,
                parent_span_id=row.parent_span_id,
                name=row.name,
                operation=row.operation,
                service=row.service,
                status=row.status,
                started_at=_as_utc(row.started_at),
                completed_at=_as_utc(row.completed_at) if row.completed_at else None,
                duration_ms=row.duration_ms,
                attributes=attributes(row.attributes_json),
            )
            for row in spans
        ],
        links=[
            TraceLinkProjection(
                link_id=row.link_id,
                span_id=row.span_id,
                linked_trace_id=row.linked_trace_id,
                linked_span_id=row.linked_span_id,
                relationship=row.relationship,
                attributes=attributes(row.attributes_json),
            )
            for row in links
        ],
        events=[
            TraceEventProjection(
                event_id=row.event_id,
                span_id=row.span_id,
                sequence=row.sequence,
                name=row.name,
                event_type=row.event_type,
                status=row.status,
                occurred_at=_as_utc(row.occurred_at),
                attributes=attributes(row.attributes_json),
            )
            for row in events
        ],
        artifacts=[
            TraceArtifactProjection(
                artifact_id=row.artifact_id,
                span_id=row.span_id,
                artifact_type=row.artifact_type,
                name=row.name,
                media_type=row.media_type,
                size_bytes=row.size_bytes,
                attributes=attributes(row.attributes_json),
            )
            for row in artifacts
        ],
    )
