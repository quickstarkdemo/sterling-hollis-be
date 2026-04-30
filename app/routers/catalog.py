from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.catalog.schemas import (
    CatalogIndexResponse,
    CategoryListResponse,
    ImageAnalysisResponse,
    ImageRecommendationResponse,
    ProductDetailResponse,
    ProductListResponse,
    ProductRecommendationRequest,
    ProductRecommendationResponse,
    ProductSort,
)
from app.catalog.service import (
    ProductFilters,
    get_product_detail,
    list_categories,
    list_products,
    recommend_products_from_image_analysis,
    recommend_products,
    related_products,
)
from app.database import get_db
from app.services.image_analysis import ImageAnalysisService, ImageUploadError, read_validated_image


router = APIRouter(prefix="/api", tags=["catalog"])


@router.get(
    "/catalog",
    response_model=CatalogIndexResponse,
    summary="Catalog Index",
    description="Store-independent catalog entry point for retail frontends. Returns category IDs and product IDs without requiring a store.",
)
def catalog_index(
    category: str | None = Query(default=None, description="Optional catalog category id."),
    brand: str | None = Query(default=None, description="Optional product brand filter."),
    q: str | None = Query(default=None, min_length=1, description="Optional text search over catalog product fields."),
    sort: ProductSort = Query(default=ProductSort.relevance, description="Catalog product sort order."),
    limit: int = Query(default=24, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    products = list_products(
        db,
        ProductFilters(q=q, category=category, brand=brand, sort=sort, limit=limit, offset=offset),
    )
    return CatalogIndexResponse(
        categories=list_categories(db).categories,
        products=products.items,
        total_products=products.total,
        limit=products.limit,
        offset=products.offset,
    )


@router.get(
    "/catalog/categories",
    response_model=CategoryListResponse,
    summary="Catalog Categories",
    description="Store-independent category list. Use these category IDs for product browsing.",
)
def catalog_categories(db: Session = Depends(get_db)):
    return list_categories(db)


@router.get(
    "/catalog/products",
    response_model=ProductListResponse,
    summary="Catalog Products",
    description="Store-independent product browsing. Returns catalog product IDs; store-specific inventory is summarized, not used as product identity.",
)
def catalog_products(
    category: str | None = Query(default=None, description="Optional catalog category id."),
    brand: str | None = Query(default=None, description="Optional product brand filter."),
    q: str | None = Query(default=None, min_length=1, description="Optional text search over catalog product fields."),
    min_price: float | None = Query(default=None, ge=0),
    max_price: float | None = Query(default=None, ge=0),
    sort: ProductSort = Query(default=ProductSort.relevance),
    limit: int = Query(default=24, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return list_products(
        db,
        ProductFilters(
            q=q,
            category=category,
            brand=brand,
            min_price=min_price,
            max_price=max_price,
            sort=sort,
            limit=limit,
            offset=offset,
        ),
    )


@router.get(
    "/categories",
    response_model=CategoryListResponse,
    summary="Categories",
    description="Store-independent category list. This is an alias of /api/catalog/categories.",
)
def categories(db: Session = Depends(get_db)):
    return list_categories(db)


@router.get(
    "/stores/{store_id}/categories",
    response_model=CategoryListResponse,
    summary="Store Categories",
    description="Inventory-scoped category availability for one store. Store ID is inventory context, not category identity.",
)
def store_categories(store_id: str, db: Session = Depends(get_db)):
    return list_categories(db, store_id=store_id)


@router.get("/categories/{category}/products", response_model=ProductListResponse)
def category_products(
    category: str,
    store_id: str | None = Query(default=None, description="Optional inventory filter. Does not change product identity."),
    brand: str | None = Query(default=None, description="Optional product brand filter."),
    min_price: float | None = Query(default=None, ge=0),
    max_price: float | None = Query(default=None, ge=0),
    in_stock_only: bool = False,
    sort: ProductSort = ProductSort.relevance,
    limit: int = Query(default=24, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return list_products(
        db,
        ProductFilters(
            store_id=store_id,
            category=category,
            brand=brand,
            min_price=min_price,
            max_price=max_price,
            in_stock_only=in_stock_only,
            sort=sort,
            limit=limit,
            offset=offset,
        ),
    )


@router.get("/products", response_model=ProductListResponse)
def products(
    store_id: str | None = Query(default=None, description="Optional inventory filter. Does not change product identity."),
    category: str | None = Query(default=None, description="Optional catalog category id."),
    brand: str | None = Query(default=None, description="Optional product brand filter."),
    size: str | None = Query(default=None, description="Optional inventory size filter."),
    color: str | None = Query(default=None, description="Optional variant color filter."),
    availability: str | None = Query(default=None, description="Optional inventory availability filter."),
    min_price: float | None = Query(default=None, ge=0),
    max_price: float | None = Query(default=None, ge=0),
    in_stock_only: bool = False,
    include_preorder: bool = True,
    sort: ProductSort = ProductSort.relevance,
    limit: int = Query(default=24, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return list_products(
        db,
        ProductFilters(
            store_id=store_id,
            category=category,
            brand=brand,
            size=size,
            color=color,
            availability=availability,
            min_price=min_price,
            max_price=max_price,
            in_stock_only=in_stock_only,
            include_preorder=include_preorder,
            sort=sort,
            limit=limit,
            offset=offset,
        ),
    )


@router.get("/products/{product_id}", response_model=ProductDetailResponse)
def product_detail(
    product_id: str,
    store_id: str | None = Query(default=None, description="Optional inventory filter for variant availability."),
    db: Session = Depends(get_db),
):
    product = get_product_detail(db, product_id, store_id=store_id)
    if not product:
        raise HTTPException(status_code=404, detail="product_id not found")
    return product


@router.get("/products/{product_id}/related", response_model=ProductListResponse)
def product_related(
    product_id: str,
    limit: int = Query(default=12, ge=1, le=50),
    db: Session = Depends(get_db),
):
    products = related_products(db, product_id, limit=limit)
    if products is None:
        raise HTTPException(status_code=404, detail="product_id not found")
    return products


@router.get("/search/products", response_model=ProductListResponse)
def search_products(
    q: str = Query(min_length=1),
    store_id: str | None = Query(default=None, description="Optional inventory filter. Does not change product identity."),
    category: str | None = Query(default=None, description="Optional catalog category id."),
    brand: str | None = Query(default=None, description="Optional product brand filter."),
    min_price: float | None = Query(default=None, ge=0),
    max_price: float | None = Query(default=None, ge=0),
    limit: int = Query(default=24, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return list_products(
        db,
        ProductFilters(
            q=q,
            store_id=store_id,
            category=category,
            brand=brand,
            min_price=min_price,
            max_price=max_price,
            limit=limit,
            offset=offset,
        ),
    )


@router.post("/recommendations/products", response_model=ProductRecommendationResponse)
def product_recommendations(req: ProductRecommendationRequest, db: Session = Depends(get_db)):
    return recommend_products(db, req)


def _http_error_for_image_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, ImageUploadError):
        return HTTPException(status_code=exc.status_code, detail=str(exc))
    if isinstance(exc, RuntimeError):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


@router.post("/image-analysis", response_model=ImageAnalysisResponse)
async def image_analysis(
    image: UploadFile = File(..., description="Consumer image to analyze. JPEG, PNG, or WebP."),
    context: str | None = Form(default=None, description="Optional frontend context to guide analysis."),
):
    try:
        image_bytes, mime_type = await read_validated_image(image)
        return ImageAnalysisService().analyze(image_bytes, mime_type, context=context)
    except Exception as exc:
        raise _http_error_for_image_exception(exc) from exc


@router.post("/recommendations/image", response_model=ImageRecommendationResponse)
async def image_recommendations(
    image: UploadFile = File(..., description="Consumer image to analyze for recommendations. JPEG, PNG, or WebP."),
    context: str | None = Form(default=None, description="Optional frontend context to guide analysis."),
    store_id: str | None = Form(default=None),
    category: str | None = Form(default=None),
    brand: str | None = Form(default=None),
    budget_min: float | None = Form(default=None, ge=0),
    budget_max: float | None = Form(default=None, ge=0),
    include_preorder: bool = Form(default=True),
    top_k: int = Form(default=12, ge=1, le=50),
    db: Session = Depends(get_db),
):
    try:
        req = ProductRecommendationRequest(
            store_id=store_id,
            category=category,
            brand=brand,
            budget_min=budget_min,
            budget_max=budget_max,
            include_preorder=include_preorder,
            top_k=top_k,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    try:
        image_bytes, mime_type = await read_validated_image(image)
        analysis_response = ImageAnalysisService().analyze(image_bytes, mime_type, context=context)
    except Exception as exc:
        raise _http_error_for_image_exception(exc) from exc

    return recommend_products_from_image_analysis(db, req, analysis_response.analysis)
