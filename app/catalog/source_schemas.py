from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.catalog.admin_schemas import DraftRevisionResponse


class CatalogSourceAssetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    display_order: int = Field(ge=0)
    original_filename: str
    content_type: Literal["image/jpeg", "image/png", "image/webp"]
    byte_size: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    checksum_sha256: str = Field(min_length=64, max_length=64)
    storage_provider: Literal["local_private"] = "local_private"
    status: Literal["ready", "promoted"]
    promoted_media_id: str | None = None
    preview_url: str
    created_at: datetime


class CatalogSourceBundleResponse(BaseModel):
    id: str
    title: str
    catalog_product_id: str | None = None
    draft_revision_id: str | None = None
    status: Literal["active"]
    assets: list[CatalogSourceAssetResponse]
    created_at: datetime
    updated_at: datetime


class CatalogSourceBundleListResponse(BaseModel):
    items: list[CatalogSourceBundleResponse]


class CatalogSourcePromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    draft_id: str = Field(min_length=1, max_length=64)
    expected_draft_version: int = Field(ge=1)


class CatalogSourcePromotionResponse(BaseModel):
    bundle_id: str
    media_id: str
    asset: CatalogSourceAssetResponse
    draft: DraftRevisionResponse
