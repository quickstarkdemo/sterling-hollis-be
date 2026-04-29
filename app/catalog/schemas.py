from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator


class ProductSort(str, Enum):
    relevance = "relevance"
    newest = "newest"
    price_asc = "price_asc"
    price_desc = "price_desc"
    inventory_desc = "inventory_desc"


class CatalogCategory(BaseModel):
    id: str
    label: str
    product_count: int
    min_price: float | None = None
    max_price: float | None = None
    available_units: int = 0


class CategoryListResponse(BaseModel):
    categories: list[CatalogCategory]


class ProductInventory(BaseModel):
    store_id: str
    availability: str
    stock_state: str
    inventory_qty: int
    size: str | None = None


class ProductImages(BaseModel):
    thumbnail_url: str | None = None
    primary_url: str | None = None
    detail_urls: list[str] = Field(default_factory=list)


class ProductInventorySummary(BaseModel):
    total_units: int = 0
    in_stock_units: int = 0
    preorder_units: int = 0
    store_count: int = 0
    in_stock_store_count: int = 0
    availability: str = "out_of_stock"


class CatalogProduct(BaseModel):
    id: str
    catalog_id: str
    title: str
    description: str
    brand: str
    category: str
    category_label: str
    price: float
    price_min: float
    price_max: float
    default_variant_id: str | None = None
    link: str | None = None
    image_url: str | None = None
    images: ProductImages | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    inventory_summary: ProductInventorySummary
    inventory: ProductInventory | None = None


class CatalogVariant(BaseModel):
    id: str
    product_id: str
    price_min: float
    price_max: float
    link: str | None = None
    image_url: str | None = None
    images: ProductImages | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    sizes: list[str] = Field(default_factory=list)
    inventory: list[ProductInventory] = Field(default_factory=list)


class ProductFacetValue(BaseModel):
    value: str
    count: int


class ProductFacetGroup(BaseModel):
    name: str
    values: list[ProductFacetValue] = Field(default_factory=list)


class ProductListResponse(BaseModel):
    items: list[CatalogProduct]
    total: int
    limit: int
    offset: int
    facets: list[ProductFacetGroup] = Field(default_factory=list)


class ProductDetailResponse(CatalogProduct):
    metadata: dict = Field(default_factory=dict)
    variants: list[CatalogVariant] = Field(default_factory=list)


class ProductRecommendationRequest(BaseModel):
    store_id: str | None = None
    customer_id: str | None = None
    category: str | None = None
    brand: str | None = None
    occasion: str | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    include_preorder: bool = True
    top_k: int = Field(default=12, ge=1, le=50)

    @model_validator(mode="after")
    def validate_budget(self) -> "ProductRecommendationRequest":
        if self.budget_min is not None and self.budget_max is not None and self.budget_min > self.budget_max:
            raise ValueError("budget_min cannot be greater than budget_max")
        return self


class RecommendedProduct(BaseModel):
    product: CatalogProduct
    score: float
    reasons: list[str] = Field(default_factory=list)
    strategy: str


class ProductRecommendationResponse(BaseModel):
    recommendations: list[RecommendedProduct]
    strategy: str
