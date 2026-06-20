from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import logging
from typing import Any
from uuid import uuid4

from app.api_traces.context import (
    TraceCaptureContext,
    current_trace_capture_context,
)
from app.api_traces.schemas import (
    ApiTraceProjection,
    TraceEventProjection,
    TraceLinkProjection,
    TraceSpanProjection,
)
from app.api_traces.service import ApiTraceRecorder
from app.config import Settings


logger = logging.getLogger(__name__)


def _stable_hex(*parts: object, length: int) -> str:
    value = ":".join(str(part) for part in parts)
    return hashlib.sha256(value.encode()).hexdigest()[:length]


def _compact_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in attributes.items() if value not in (None, {}, [])
    }


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


@dataclass(slots=True)
class ApiTraceSpanHandle:
    span_id: str
    parent_span_id: str
    name: str
    operation: str
    service: str
    started_at: datetime
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "running"

    def annotate(self, attributes: Mapping[str, Any] | None = None, **values: Any) -> None:
        if attributes:
            self.attributes.update(attributes)
        self.attributes.update(values)


class ApiTraceSession:
    """Collect one authorized application trace without affecting business work."""

    def __init__(
        self,
        *,
        context: TraceCaptureContext,
        settings: Settings,
        name: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        self.context = context
        self.settings = settings
        self.name = str(name or "Application trace")[:128]
        self.trace_id = context.trace_id or _stable_hex(
            context.owner_provider_user_id,
            context.surface,
            uuid4().hex,
            length=32,
        )
        self.root_span_id = (
            context.parent_span_id
            or context.span_id
            or _stable_hex(self.trace_id, "browser", length=16)
        )
        self.server_span_id = (
            context.span_id
            if context.span_id and context.span_id != self.root_span_id
            else _stable_hex(self.trace_id, "server", length=16)
        )
        self.started_at = datetime.now(timezone.utc)
        self.status = "running"
        self.attributes = _compact_attributes(dict(attributes or {}))
        self.spans: list[TraceSpanProjection] = []
        self.links: list[TraceLinkProjection] = []
        self.events: list[TraceEventProjection] = []
        self._span_stack = [self.server_span_id]
        self._span_counter = 0
        self._event_sequence = 0
        self._finished = False
        self.add_event(
            name=f"{self.name} started"[:128],
            event_type="workflow.started",
            status="running",
            span_id=self.server_span_id,
            attributes=self.attributes,
        )

    @property
    def current_span_id(self) -> str:
        return self._span_stack[-1]

    def annotate(self, attributes: Mapping[str, Any] | None = None, **values: Any) -> None:
        if attributes:
            self.attributes.update(attributes)
        self.attributes.update(values)
        self.attributes = _compact_attributes(self.attributes)

    def add_event(
        self,
        *,
        name: str,
        event_type: str,
        status: str | None = None,
        span_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        sequence = self._event_sequence
        self._event_sequence += 1
        self.events.append(
            TraceEventProjection(
                event_id=_stable_hex(
                    self.trace_id,
                    "event",
                    sequence,
                    event_type,
                    length=32,
                ),
                span_id=span_id or self.current_span_id,
                sequence=sequence,
                name=str(name or "Trace event")[:128],
                event_type=str(event_type or "trace.event")[:64],
                status=status,
                occurred_at=occurred_at or datetime.now(timezone.utc),
                attributes=_compact_attributes(dict(attributes or {})),
            )
        )

    def add_link(
        self,
        *,
        linked_trace_id: str,
        linked_span_id: str | None = None,
        relationship: str,
        span_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        if not linked_trace_id or linked_trace_id == self.trace_id:
            return
        local_span_id = span_id or self.current_span_id
        self.links.append(
            TraceLinkProjection(
                link_id=_stable_hex(
                    self.trace_id,
                    local_span_id,
                    linked_trace_id,
                    relationship,
                    length=32,
                ),
                span_id=local_span_id,
                linked_trace_id=linked_trace_id,
                linked_span_id=linked_span_id,
                relationship=str(relationship or "related")[:32],
                attributes=_compact_attributes(dict(attributes or {})),
            )
        )

    @contextmanager
    def operation(
        self,
        *,
        name: str,
        operation: str,
        service: str = "sterling-hollis-be",
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[ApiTraceSpanHandle]:
        self._span_counter += 1
        handle = ApiTraceSpanHandle(
            span_id=_stable_hex(
                self.trace_id,
                "span",
                self._span_counter,
                operation,
                length=16,
            ),
            parent_span_id=self.current_span_id,
            name=str(name or "Application operation")[:128],
            operation=str(operation or "application.operation")[:64],
            service=str(service or "sterling-hollis-be")[:128],
            started_at=datetime.now(timezone.utc),
            attributes=_compact_attributes(dict(attributes or {})),
        )
        self._span_stack.append(handle.span_id)
        try:
            yield handle
        except Exception as exc:
            handle.status = "failed"
            handle.annotate(error_code=type(exc).__name__)
            raise
        else:
            if handle.status == "running":
                handle.status = "succeeded"
        finally:
            completed_at = datetime.now(timezone.utc)
            if self._span_stack[-1] == handle.span_id:
                self._span_stack.pop()
            duration_ms = max(
                0,
                int((completed_at - handle.started_at).total_seconds() * 1000),
            )
            self.spans.append(
                TraceSpanProjection(
                    span_id=handle.span_id,
                    parent_span_id=handle.parent_span_id,
                    name=handle.name,
                    operation=handle.operation,
                    service=handle.service,
                    status=handle.status,
                    started_at=handle.started_at,
                    completed_at=completed_at,
                    duration_ms=duration_ms,
                    attributes=_compact_attributes(handle.attributes),
                )
            )
            self.add_event(
                name=handle.name,
                event_type=f"{handle.operation}.{handle.status}",
                status=handle.status,
                span_id=handle.span_id,
                attributes=handle.attributes,
                occurred_at=completed_at,
            )

    def finish(self, *, status: str | None = None, error_code: str | None = None) -> bool:
        if self._finished:
            return False
        self._finished = True
        completed_at = datetime.now(timezone.utc)
        final_status = status or self.status
        if final_status == "running":
            final_status = "succeeded"
        self.status = final_status
        if error_code:
            self.annotate(error_code=error_code)
        duration_ms = max(
            0,
            int((completed_at - self.started_at).total_seconds() * 1000),
        )
        projection = ApiTraceProjection(
            trace_id=self.trace_id,
            surface=self.context.surface,
            name=self.name,
            root_span_id=self.root_span_id,
            status=final_status,
            started_at=self.started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            attributes=self.attributes,
            spans=[
                TraceSpanProjection(
                    span_id=self.root_span_id,
                    name=f"{self.context.surface} browser request",
                    operation="http.client",
                    service="sterling-hollis-fe",
                    status=final_status,
                    started_at=self.started_at,
                    completed_at=completed_at,
                    duration_ms=duration_ms,
                    attributes={"surface": self.context.surface},
                ),
                TraceSpanProjection(
                    span_id=self.server_span_id,
                    parent_span_id=self.root_span_id,
                    name=f"{self.name} API"[:128],
                    operation="http.server",
                    service="sterling-hollis-be",
                    status=final_status,
                    started_at=self.started_at,
                    completed_at=completed_at,
                    duration_ms=duration_ms,
                    attributes=self.attributes,
                ),
                *self.spans,
            ],
            links=self.links,
            events=[
                *self.events,
                TraceEventProjection(
                    event_id=_stable_hex(
                        self.trace_id,
                        "event",
                        self._event_sequence,
                        "workflow.completed",
                        length=32,
                    ),
                    span_id=self.server_span_id,
                    sequence=self._event_sequence,
                    name=f"{self.name} {final_status}"[:128],
                    event_type=f"workflow.{final_status}",
                    status=final_status,
                    occurred_at=completed_at,
                    attributes=self.attributes,
                ),
            ],
        )
        return ApiTraceRecorder(settings=self.settings).record(
            context=self.context,
            projection=projection,
        )


_CURRENT_API_TRACE_SESSION: ContextVar[ApiTraceSession | None] = ContextVar(
    "current_api_trace_session",
    default=None,
)


def current_api_trace_session() -> ApiTraceSession | None:
    return _CURRENT_API_TRACE_SESSION.get()


def current_api_trace_correlation() -> dict[str, str]:
    session = current_api_trace_session()
    if session is None:
        return {}
    return {
        "app.trace_id": session.trace_id,
        "app.span_id": session.current_span_id,
    }


def correlated_observability_kwargs(
    kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    projected = dict(kwargs)
    correlation = current_api_trace_correlation()
    if not correlation:
        return projected
    for field in ("metadata", "tags"):
        projected[field] = {**dict(projected.get(field) or {}), **correlation}
    return projected


@contextmanager
def api_trace_session(
    *,
    settings: Settings,
    name: str,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[ApiTraceSession | None]:
    context = current_trace_capture_context()
    if (
        not settings.api_trace_capture_enabled
        or context is None
        or not context.authorized
    ):
        yield None
        return
    try:
        session = ApiTraceSession(
            context=context,
            settings=settings,
            name=name,
            attributes=attributes,
        )
    except Exception as exc:
        logger.warning("API trace session initialization failed (%s).", type(exc).__name__)
        yield None
        return
    token = _CURRENT_API_TRACE_SESSION.set(session)
    try:
        yield session
    except Exception as exc:
        try:
            session.finish(status="failed", error_code=type(exc).__name__)
        except Exception as trace_exc:
            logger.warning(
                "API trace session finalization failed (%s).",
                type(trace_exc).__name__,
            )
        raise
    else:
        try:
            session.finish(status=session.status)
        except Exception as exc:
            logger.warning("API trace session finalization failed (%s).", type(exc).__name__)
    finally:
        _CURRENT_API_TRACE_SESSION.reset(token)


@contextmanager
def api_trace_operation(
    name: str,
    operation: str,
    *,
    service: str = "sterling-hollis-be",
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[ApiTraceSpanHandle | None]:
    session = current_api_trace_session()
    if session is None:
        yield None
        return
    with session.operation(
        name=name,
        operation=operation,
        service=service,
        attributes=attributes,
    ) as span:
        yield span


def api_trace_http_operation(
    name: str,
    *,
    service: str,
    attributes: Mapping[str, Any] | None = None,
):
    return api_trace_operation(
        name,
        "http.client",
        service=service,
        attributes=attributes,
    )


def api_trace_database_operation(
    name: str,
    *,
    attributes: Mapping[str, Any] | None = None,
):
    return api_trace_operation(
        name,
        "database.query",
        attributes=attributes,
    )


def api_trace_storage_operation(
    name: str,
    *,
    service: str = "storage",
    attributes: Mapping[str, Any] | None = None,
):
    return api_trace_operation(
        name,
        "storage.operation",
        service=service,
        attributes=attributes,
    )


def link_api_trace_replay(
    *,
    linked_trace_id: str | None,
    linked_span_id: str | None = None,
) -> None:
    session = current_api_trace_session()
    if session is None or not linked_trace_id:
        return
    session.add_link(
        linked_trace_id=linked_trace_id,
        linked_span_id=linked_span_id,
        relationship="replay_of",
        attributes={"duplicate_replay": True},
    )


__all__ = [
    "ApiTraceSession",
    "api_trace_database_operation",
    "api_trace_http_operation",
    "api_trace_operation",
    "api_trace_session",
    "api_trace_storage_operation",
    "correlated_observability_kwargs",
    "current_api_trace_correlation",
    "current_api_trace_session",
    "link_api_trace_replay",
    "new_openai_client_request_id",
    "openai_request_ids",
]
