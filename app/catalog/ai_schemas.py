from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.catalog.admin_schemas import (
    CatalogSuggestionSetResponse,
    DesignSpecificationDraft,
    ProductSpecificationDraft,
    ProductDraftV2,
)
from app.catalog.workflow_schemas import CatalogWorkflowResponse


CatalogCategory = Literal[
    "womens_apparel",
    "shoes",
    "handbags",
    "beauty",
    "mens_apparel",
    "kids",
    "home",
    "jewelry_accessories",
]
CatalogAvailability = Literal["in stock", "low stock", "preorder", "out of stock"]


class CatalogAIInventoryProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    store_id: str = Field(min_length=1, max_length=64)
    size: str | None = Field(default=None, max_length=64)
    availability: CatalogAvailability
    inventory_qty: int = Field(ge=0, le=10_000)


class CatalogAIProductProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=2000)
    brand_id: str = Field(min_length=1, max_length=64)
    brand: str = Field(min_length=1, max_length=128)
    category: CatalogCategory
    image_direction: str = Field(min_length=1, max_length=1000)
    design_specification: DesignSpecificationDraft
    price_min: float = Field(ge=0, le=1_000_000)
    price_max: float = Field(ge=0, le=1_000_000)
    link: str | None = Field(default=None, max_length=500)
    color: str | None = Field(default=None, max_length=64)
    material: str | None = Field(default=None, max_length=64)
    gender: Literal["women", "men", "girls", "boys", "unisex"] | None = None
    season: Literal["spring", "summer", "fall", "winter", "all-season"] | None = None
    inventory: list[CatalogAIInventoryProposal] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_product(self):
        if self.price_max < self.price_min:
            raise ValueError("price_max must be greater than or equal to price_min")
        inventory_keys = [
            (row.store_id.casefold(), (row.size or "").casefold())
            for row in self.inventory
        ]
        if len(inventory_keys) != len(set(inventory_keys)):
            raise ValueError("inventory store and optional size combinations must be unique")
        return self


class CatalogAICommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    instruction: str = Field(min_length=1, max_length=4000)
    current_draft_id: str | None = Field(default=None, max_length=64)
    expected_draft_version: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_refinement_state(self):
        if self.current_draft_id is None and self.expected_draft_version != 0:
            raise ValueError("A new AI draft requires expected_draft_version 0")
        if self.current_draft_id is not None and self.expected_draft_version < 1:
            raise ValueError("A draft refinement requires expected_draft_version 1 or greater")
        return self


class CatalogAIDraftResult(BaseModel):
    id: str
    product_id: str
    draft_version: int = Field(ge=1)
    base_version: int = Field(ge=0)
    moderation_state: Literal["approved"] = "approved"
    image_direction: str
    product: ProductDraftV2


class CatalogAICommandResult(BaseModel):
    status: Literal["succeeded", "blocked"]
    message: str
    retryable: bool = False
    replayed: bool = False
    draft: CatalogAIDraftResult | None = None


class CatalogAIWorkflowResponse(CatalogAICommandResult):
    workflow: CatalogWorkflowResponse


SuggestionCertainty = Literal["observed", "derived"]
SuggestionInputOrigin = Literal["supplier_analysis", "typed_action", "voice"]


class CatalogAIFieldProposal(BaseModel):
    """One grounded field proposal returned by Structured Outputs."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_path: str = Field(min_length=1, max_length=255)
    proposed_value: str | float | list[str] | list[ProductSpecificationDraft]
    evidence_asset_ids: list[str] = Field(default_factory=list, max_length=20)
    certainty_class: SuggestionCertainty

    @model_validator(mode="after")
    def validate_evidence(self):
        if len(self.evidence_asset_ids) != len(set(self.evidence_asset_ids)):
            raise ValueError("evidence_asset_ids must be unique")
        if self.certainty_class == "observed" and not self.evidence_asset_ids:
            raise ValueError("observed suggestions require source evidence")
        return self


class CatalogAIUnknownField(BaseModel):
    """An unresolved fact that must remain outside canonical product state."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_path: str = Field(min_length=1, max_length=255)
    question: str = Field(min_length=1, max_length=500)


class CatalogAISuggestionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggestions: list[CatalogAIFieldProposal] = Field(default_factory=list, max_length=80)
    unknown_fields: list[CatalogAIUnknownField] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def validate_unique_targets(self):
        paths = [item.target_path for item in self.suggestions]
        if len(paths) != len(set(paths)):
            raise ValueError("suggestion target paths must be unique")
        unknown_paths = [item.target_path for item in self.unknown_fields]
        if len(unknown_paths) != len(set(unknown_paths)):
            raise ValueError("unknown field target paths must be unique")
        if set(paths) & set(unknown_paths):
            raise ValueError("a target path cannot be both suggested and unknown")
        return self


class CatalogAISuggestionCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    draft_id: str = Field(min_length=1, max_length=64)
    expected_draft_version: int = Field(ge=1)
    workflow_id: str = Field(min_length=1, max_length=64)
    instruction: str = Field(min_length=1, max_length=4000)
    input_origin: SuggestionInputOrigin
    source_asset_ids: list[str] = Field(default_factory=list, max_length=20)
    target_paths: list[str] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def validate_command(self):
        if len(self.source_asset_ids) != len(set(self.source_asset_ids)):
            raise ValueError("source_asset_ids must be unique")
        if len(self.target_paths) != len(set(self.target_paths)):
            raise ValueError("target_paths must be unique")
        if self.input_origin == "supplier_analysis" and not self.source_asset_ids:
            raise ValueError("supplier analysis requires source assets")
        return self


class CatalogAIDirectSuggestionCommandRequest(CatalogAISuggestionCommandRequest):
    input_origin: Literal["supplier_analysis", "typed_action"]


class CatalogAISuggestionCommandResult(BaseModel):
    status: Literal["succeeded", "blocked"]
    message: str
    retryable: bool = False
    replayed: bool = False
    suggestion_set: CatalogSuggestionSetResponse | None = None
    follow_up_questions: list[CatalogAIUnknownField] = Field(default_factory=list)
