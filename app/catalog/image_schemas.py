from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CatalogImageCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: Literal["generate", "refine"] = "generate"
    draft_id: str = Field(min_length=1, max_length=64)
    expected_draft_version: int = Field(ge=1)
    variant_index: int = Field(default=0, ge=0, le=99)
    refinement_prompt: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_refinement_prompt(self):
        if self.action == "refine" and not self.refinement_prompt:
            raise ValueError("refinement_prompt is required when action is refine")
        if self.action == "generate" and self.refinement_prompt:
            raise ValueError("refinement_prompt is only valid when action is refine")
        return self


class CatalogImageJobResponse(BaseModel):
    id: str
    workflow_id: str
    draft_id: str
    expected_draft_version: int
    action: Literal["generate", "refine"]
    variant_index: int
    image_variant_set_id: str | None = None
    model: str
    size: str
    quality: str
    output_format: str
    status: str
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class CatalogImageApprovalRequest(BaseModel):
    draft_id: str = Field(min_length=1, max_length=64)
    expected_draft_version: int = Field(ge=1)


class CatalogImageApprovalResponse(BaseModel):
    job_id: str
    draft_id: str
    variant_index: int
    approval_status: Literal["approved"] = "approved"


class CatalogImageVariantSetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    draft_id: str = Field(min_length=1, max_length=64)
    expected_draft_version: int = Field(ge=1)


class CatalogImageVariantSetResponse(BaseModel):
    id: str
    workflow_id: str
    draft_id: str
    expected_draft_version: int
    primary_variant_index: int
    status: Literal[
        "queued",
        "running",
        "review",
        "partially_failed",
        "failed",
        "complete",
    ]
    jobs: list[CatalogImageJobResponse]
