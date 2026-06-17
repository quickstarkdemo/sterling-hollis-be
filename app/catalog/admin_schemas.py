from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ModerationState = Literal["pending", "approved", "blocked"]
LifecycleStatus = Literal["draft", "published", "archived"]
PublishedLifecycleStatus = Literal["published", "archived"]
DraftStatus = Literal["draft", "published"]
VariantAxis = Literal["color", "material"]


class DesignSpecificationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    product_type: str = Field(min_length=1, max_length=128)
    silhouette: str = Field(min_length=1, max_length=255)
    construction: str = Field(min_length=1, max_length=500)
    distinguishing_features: list[
        Annotated[str, Field(min_length=1, max_length=128)]
    ] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_distinguishing_features(self):
        normalized = [feature.strip().casefold() for feature in self.distinguishing_features]
        if any(not feature for feature in normalized):
            raise ValueError("distinguishing_features cannot contain blank values")
        if len(normalized) != len(set(normalized)):
            raise ValueError("distinguishing_features must be unique")
        return self


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
    design_specification: DesignSpecificationDraft | None = None
    variant_axes: list[VariantAxis] = Field(default_factory=list, max_length=2)
    primary_variant_index: int = Field(default=0, ge=0)
    variants: list[VariantDraft] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_variants(self):
        if self.primary_variant_index >= len(self.variants):
            raise ValueError("primary_variant_index must reference a product variant")
        if len(self.variant_axes) != len(set(self.variant_axes)):
            raise ValueError("variant_axes must be unique")
        if self.design_specification is not None or self.variant_axes:
            for attribute in ("color", "material"):
                values = {
                    (getattr(row, attribute) or "").casefold()
                    for row in self.variants
                }
                if len(values) > 1 and attribute not in self.variant_axes:
                    raise ValueError(
                        f"{attribute} changes require {attribute} to be a declared variant axis"
                    )
            stable_values = {
                ((row.gender or "").casefold(), (row.season or "").casefold())
                for row in self.variants
            }
            if len(stable_values) > 1:
                raise ValueError(
                    "gender and season must remain stable across product variants"
                )
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
