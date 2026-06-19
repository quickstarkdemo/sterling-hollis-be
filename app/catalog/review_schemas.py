from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ReviewState = Literal["pending", "approved", "flagged", "rejected"]
ReviewDecisionAction = Literal[
    "approve",
    "flag",
    "reject",
    "save_response",
    "publish_response",
]
ReviewCategory = Literal[
    "product_quality",
    "fit",
    "shipping",
    "service",
    "spam",
    "abuse",
    "safety",
    "other",
]


class ReviewAIProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    categories: list[ReviewCategory] = Field(default_factory=list, max_length=8)
    theme_summary: str = Field(min_length=1, max_length=1000)
    suggested_action: Literal["approve", "flag", "reject"]
    response_draft: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_categories(self):
        if len(self.categories) != len(set(self.categories)):
            raise ValueError("review categories must be unique")
        return self


class ReviewAssistRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


class ReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: ReviewDecisionAction
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)
    response_text: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_response(self):
        if self.action == "save_response" and not self.response_text:
            raise ValueError("save_response requires response_text")
        if self.action not in {"save_response", "publish_response"} and self.response_text:
            raise ValueError("response_text is only valid for response actions")
        return self


class ReviewActionResponse(BaseModel):
    id: str
    action: str
    expected_version: int
    resulting_version: int
    actor_provider_user_id: str
    reason: str | None = None
    created_at: datetime


class ReviewModerationResponse(BaseModel):
    version: int
    state: ReviewState
    ai_categories: list[ReviewCategory] = Field(default_factory=list)
    ai_theme_summary: str | None = None
    ai_suggested_action: Literal["approve", "flag", "reject"] | None = None
    ai_provider_metadata: dict = Field(default_factory=dict)
    response_draft: str | None = None
    response_published: str | None = None
    response_published_at: datetime | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None


class AdminProductReviewResponse(BaseModel):
    id: str
    product_id: str
    source: str
    external_review_id: str
    author_display_name: str
    body: str
    rating: int = Field(ge=1, le=5)
    submitted_at: datetime
    moderation: ReviewModerationResponse
    actions: list[ReviewActionResponse] = Field(default_factory=list)


class AdminProductReviewListResponse(BaseModel):
    items: list[AdminProductReviewResponse]


class PublicProductReview(BaseModel):
    id: str
    author_display_name: str
    body: str
    rating: int = Field(ge=1, le=5)
    submitted_at: datetime
    merchant_response: str | None = None
    merchant_responded_at: datetime | None = None
