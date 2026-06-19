from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


WorkflowCapability = Literal[
    "workflow",
    "responses",
    "moderation",
    "image_generation",
    "realtime",
    "catalog",
    "publication",
]
WorkflowEventStatus = Literal[
    "queued",
    "started",
    "running",
    "succeeded",
    "blocked",
    "failed",
    "retrying",
    "completed",
]


class CatalogWorkflowStartRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    business_summary: str = Field(min_length=1, max_length=1000)
    draft_id: str | None = Field(default=None, max_length=64)
    image_job_id: str | None = Field(default=None, max_length=64)
    published_product_id: str | None = Field(default=None, max_length=64)


class WorkflowEventInput(BaseModel):
    client_event_id: str = Field(min_length=1, max_length=128)
    stage: str = Field(min_length=1, max_length=64)
    capability: WorkflowCapability
    status: WorkflowEventStatus
    business_summary: str = Field(min_length=1, max_length=1000)
    model: str | None = Field(default=None, max_length=128)
    request_id: str | None = Field(default=None, max_length=128)
    duration_ms: int | None = Field(default=None, ge=0, le=2_147_483_647)
    usage: dict = Field(default_factory=dict)
    moderation: dict = Field(default_factory=dict)
    request_payload: dict = Field(default_factory=dict)
    response_payload: dict = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=128)
    retryable: bool = False
    draft_id: str | None = Field(default=None, max_length=64)
    image_job_id: str | None = Field(default=None, max_length=64)
    published_product_id: str | None = Field(default=None, max_length=64)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class WorkflowDeveloperEvent(BaseModel):
    model: str | None = None
    request_id: str | None = None
    duration_ms: int | None = None
    usage: dict = Field(default_factory=dict)
    moderation: dict = Field(default_factory=dict)
    request_payload: dict = Field(default_factory=dict)
    response_payload: dict = Field(default_factory=dict)
    error_code: str | None = None
    payload_expired: bool = False


class WorkflowEventResponse(BaseModel):
    id: str
    sequence: int
    stage: str
    capability: WorkflowCapability
    status: WorkflowEventStatus
    business_summary: str
    retryable: bool
    created_at: datetime
    developer: WorkflowDeveloperEvent | None = None


class CatalogWorkflowResponse(BaseModel):
    id: str
    title: str
    business_summary: str
    status: str
    current_stage: str
    draft_id: str | None = None
    image_job_id: str | None = None
    published_product_id: str | None = None
    suggestion_set_ids: list[str] = Field(default_factory=list)
    is_owner: bool
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    events: list[WorkflowEventResponse]
