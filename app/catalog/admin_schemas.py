from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator


ModerationState = Literal["pending", "approved", "blocked"]
LifecycleStatus = Literal["draft", "published", "archived"]
PublishedLifecycleStatus = Literal["published", "archived"]
DraftStatus = Literal["draft", "published"]


class InventoryDraft(BaseModel):
    store_id: str = Field(min_length=1, max_length=64)
    size: str = Field(default="One Size", min_length=1, max_length=64)
    availability: str = Field(min_length=1, max_length=32)
    inventory_qty: int = Field(ge=0)
    objective_weight: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    metadata: dict = Field(default_factory=dict)


class VariantDraft(BaseModel):
    variant_id: str | None = Field(default=None, max_length=64)
    color: str | None = Field(default=None, max_length=64)
    material: str | None = Field(default=None, max_length=64)
    gender: str | None = Field(default=None, max_length=32)
    season: str | None = Field(default=None, max_length=32)
    price_min: Decimal = Field(ge=0)
    price_max: Decimal = Field(ge=0)
    link: str | None = Field(default=None, max_length=500)
    image_link: str | None = Field(default=None, max_length=500)
    image_set: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)
    inventory: list[InventoryDraft] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_price_range(self):
        if self.price_max < self.price_min:
            raise ValueError("price_max must be greater than or equal to price_min")
        inventory_keys = [(row.store_id, row.size.casefold()) for row in self.inventory]
        if len(inventory_keys) != len(set(inventory_keys)):
            raise ValueError("inventory store and size combinations must be unique within a variant")
        return self


class ProductDraft(BaseModel):
    product_id: str | None = Field(default=None, max_length=64)
    seed_run_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    brand: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=128)
    metadata: dict = Field(default_factory=dict)
    variants: list[VariantDraft] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_variants(self):
        variant_keys = [
            (
                row.color.casefold() if row.color else None,
                row.material.casefold() if row.material else None,
                row.gender.casefold() if row.gender else None,
                row.season.casefold() if row.season else None,
            )
            for row in self.variants
        ]
        if len(variant_keys) != len(set(variant_keys)):
            raise ValueError("variant attribute combinations must be unique within a product")
        explicit_ids = [row.variant_id for row in self.variants if row.variant_id]
        if len(explicit_ids) != len(set(explicit_ids)):
            raise ValueError("variant_id values must be unique within a product")
        return self


class DraftMutationRequest(BaseModel):
    expected_version: int = Field(ge=0)
    moderation_state: ModerationState = "pending"
    product: ProductDraft


class PublishRequest(BaseModel):
    draft_id: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=0)


class ArchiveRequest(BaseModel):
    expected_version: int = Field(ge=0)


class DraftRevisionResponse(BaseModel):
    id: str
    product_id: str
    base_version: int
    status: DraftStatus
    moderation_state: ModerationState
    created_by: str
    created_at: datetime


class AdminProductResponse(BaseModel):
    product_id: str
    lifecycle_status: LifecycleStatus
    version: int
    title: str
    description: str
    brand: str
    category: str
    metadata: dict
    drafts: list[DraftRevisionResponse] = Field(default_factory=list)


class LifecycleMutationResponse(BaseModel):
    product_id: str
    lifecycle_status: PublishedLifecycleStatus
    version: int
