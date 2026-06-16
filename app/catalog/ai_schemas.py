from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.catalog.admin_schemas import ProductDraft
from app.catalog.demo_schemas import DemoRunResponse


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
CatalogAvailability = Literal["in stock", "low stock", "preorder"]


class CatalogAIInventoryProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    size: str = Field(min_length=1, max_length=64)
    availability: CatalogAvailability
    inventory_qty: int = Field(ge=0, le=10_000)
    objective_weight: float = Field(ge=0, le=1)


class CatalogAIVariantProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    color: str = Field(min_length=1, max_length=64)
    material: str = Field(min_length=1, max_length=64)
    gender: Literal["women", "men", "girls", "boys", "unisex"]
    season: Literal["spring", "summer", "fall", "winter", "all-season"]
    price_min: float = Field(ge=0, le=1_000_000)
    price_max: float = Field(ge=0, le=1_000_000)
    inventory: list[CatalogAIInventoryProposal] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_price_range(self):
        if self.price_max < self.price_min:
            raise ValueError("price_max must be greater than or equal to price_min")
        return self


class CatalogAIProductProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=2000)
    brand: Literal["Sterling Hollis"]
    category: CatalogCategory
    image_direction: str = Field(min_length=1, max_length=1000)
    variants: list[CatalogAIVariantProposal] = Field(min_length=1, max_length=4)


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
    product: ProductDraft


class CatalogAICommandResult(BaseModel):
    status: Literal["succeeded", "blocked"]
    message: str
    retryable: bool = False
    replayed: bool = False
    draft: CatalogAIDraftResult | None = None


class CatalogAIWorkflowResponse(CatalogAICommandResult):
    run: DemoRunResponse
