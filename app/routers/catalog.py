from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.catalog.schemas import (
    CategoryListResponse,
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
    recommend_products,
    related_products,
)
from app.database import get_db


router = APIRouter(prefix="/api", tags=["catalog"])


@router.get("/categories", response_model=CategoryListResponse)
def categories(store_id: str | None = None, db: Session = Depends(get_db)):
    return list_categories(db, store_id=store_id)


@router.get("/categories/{category}/products", response_model=ProductListResponse)
def category_products(
    category: str,
    store_id: str | None = None,
    brand: str | None = None,
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
    store_id: str | None = None,
    category: str | None = None,
    brand: str | None = None,
    size: str | None = None,
    color: str | None = None,
    availability: str | None = None,
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
def product_detail(product_id: str, store_id: str | None = None, db: Session = Depends(get_db)):
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
    store_id: str | None = None,
    category: str | None = None,
    brand: str | None = None,
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
