from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class TraceProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TraceSpanProjection(TraceProjectionModel):
    span_id: str = Field(min_length=1, max_length=64)
    parent_span_id: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    operation: str = Field(min_length=1, max_length=64)
    service: str = Field(min_length=1, max_length=128)
    status: str = Field(min_length=1, max_length=32)
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    attributes: dict[str, Any] = Field(default_factory=dict)


class TraceLinkProjection(TraceProjectionModel):
    link_id: str = Field(min_length=1, max_length=64)
    span_id: str | None = Field(default=None, max_length=64)
    linked_trace_id: str = Field(min_length=1, max_length=64)
    linked_span_id: str | None = Field(default=None, max_length=64)
    relationship: str = Field(min_length=1, max_length=32)
    attributes: dict[str, Any] = Field(default_factory=dict)


class TraceEventProjection(TraceProjectionModel):
    event_id: str = Field(min_length=1, max_length=64)
    span_id: str | None = Field(default=None, max_length=64)
    sequence: int = Field(ge=0)
    name: str = Field(min_length=1, max_length=128)
    event_type: str = Field(min_length=1, max_length=64)
    status: str | None = Field(default=None, max_length=32)
    occurred_at: AwareDatetime
    attributes: dict[str, Any] = Field(default_factory=dict)


class TraceArtifactProjection(TraceProjectionModel):
    artifact_id: str = Field(min_length=1, max_length=64)
    span_id: str | None = Field(default=None, max_length=64)
    artifact_type: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    media_type: str | None = Field(default=None, max_length=128)
    size_bytes: int | None = Field(default=None, ge=0)
    attributes: dict[str, Any] = Field(default_factory=dict)


class ApiTraceProjection(TraceProjectionModel):
    projection_version: Literal["1.0"] = "1.0"
    trace_id: str = Field(min_length=1, max_length=64)
    surface: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    root_span_id: str = Field(min_length=1, max_length=64)
    status: str = Field(min_length=1, max_length=32)
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    attributes: dict[str, Any] = Field(default_factory=dict)
    truncation: dict[str, int] = Field(default_factory=dict)
    payload_expired: bool = False
    spans: list[TraceSpanProjection] = Field(default_factory=list)
    links: list[TraceLinkProjection] = Field(default_factory=list)
    events: list[TraceEventProjection] = Field(default_factory=list)
    artifacts: list[TraceArtifactProjection] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_topology(self) -> ApiTraceProjection:
        span_ids = [span.span_id for span in self.spans]
        if len(span_ids) != len(set(span_ids)):
            raise ValueError("span_id values must be unique within a trace")
        if self.root_span_id not in set(span_ids):
            raise ValueError("root_span_id must identify a span in the trace")

        local_span_ids = set(span_ids)
        for span in self.spans:
            if span.parent_span_id and span.parent_span_id not in local_span_ids:
                raise ValueError("parent_span_id must identify a span in the trace")
            if span.completed_at and span.completed_at < span.started_at:
                raise ValueError("span completed_at must not precede started_at")

        children: dict[str, list[str]] = {}
        for span in self.spans:
            if span.parent_span_id:
                children.setdefault(span.parent_span_id, []).append(span.span_id)
        reachable: set[str] = set()
        pending = [self.root_span_id]
        while pending:
            span_id = pending.pop()
            if span_id in reachable:
                raise ValueError("span topology must not contain cycles")
            reachable.add(span_id)
            pending.extend(children.get(span_id, []))
        if reachable != local_span_ids:
            raise ValueError("every span must descend from root_span_id")

        for item in [*self.links, *self.events, *self.artifacts]:
            if item.span_id and item.span_id not in local_span_ids:
                raise ValueError("span_id must identify a span in the trace")

        for label, identifiers in (
            ("link_id", [item.link_id for item in self.links]),
            ("event_id", [item.event_id for item in self.events]),
            ("artifact_id", [item.artifact_id for item in self.artifacts]),
        ):
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"{label} values must be unique within a trace")
        event_sequences = [item.sequence for item in self.events]
        if len(event_sequences) != len(set(event_sequences)):
            raise ValueError("event sequence values must be unique within a trace")
        if self.completed_at and self.completed_at < self.started_at:
            raise ValueError("trace completed_at must not precede started_at")
        return self


class TraceSummaryProjection(TraceProjectionModel):
    trace_id: str
    surface: str
    name: str
    status: str
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    duration_ms: int | None = None
    payload_expired: bool = False


class TraceListResponse(TraceProjectionModel):
    items: list[TraceSummaryProjection]
    next_cursor: str | None = None


class TraceEventPage(TraceProjectionModel):
    items: list[TraceEventProjection]
    next_cursor: int


ClientTraceEventType = Literal[
    "ui.started",
    "ui.completed",
    "ui.failed",
    "http.started",
    "http.completed",
    "http.failed",
    "realtime.connected",
    "realtime.disconnected",
    "realtime.error",
]


class ClientTraceEventInput(TraceProjectionModel):
    event_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    span_id: str | None = Field(default=None, min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    event_type: ClientTraceEventType
    status: str | None = Field(default=None, max_length=32)
    occurred_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("attributes")
    @classmethod
    def bound_input_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, default=str, separators=(",", ":")).encode()) > 65_536:
            raise ValueError("attributes payload exceeds 65536 bytes")
        return value
