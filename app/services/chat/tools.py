from __future__ import annotations

from sqlalchemy.orm import Session

from app.catalog.schemas import CatalogProduct, ProductRecommendationRequest, ProductSort
from app.catalog.service import ProductFilters, get_product_detail, list_products, recommend_products, related_products
from app.models import Customer


def product_detail(db: Session, product_id: str, store_id: str | None = None) -> CatalogProduct | None:
    return get_product_detail(db, product_id, store_id=store_id)


def related_product_cards(db: Session, product_id: str, *, limit: int = 3) -> list[CatalogProduct]:
    response = related_products(db, product_id, limit=limit)
    return response.items if response else []


def catalog_cards(
    db: Session,
    *,
    category: str | None = None,
    store_id: str | None = None,
    query: str | None = None,
    limit: int = 3,
) -> list[CatalogProduct]:
    response = list_products(
        db,
        ProductFilters(
            q=query,
            category=category,
            store_id=store_id,
            include_preorder=True,
            sort=ProductSort.relevance,
            limit=limit,
        ),
        include_facets=False,
    )
    return response.items


def recommendation_cards(
    db: Session,
    *,
    customer_id: str | None = None,
    store_id: str | None = None,
    category: str | None = None,
    limit: int = 3,
) -> list[CatalogProduct]:
    response = recommend_products(
        db,
        ProductRecommendationRequest(
            customer_id=customer_id,
            store_id=store_id,
            category=category,
            top_k=limit,
            include_preorder=True,
        ),
    )
    return [row.product for row in response.recommendations]


def customer_summary(db: Session, customer_id: str) -> dict:
    customer = db.get(Customer, customer_id)
    if not customer:
        return {}
    return {
        "id": customer.id,
        "first_name": customer.first_name,
        "last_name": customer.last_name,
        "email": customer.email,
        "loyalty_tier": customer.loyalty_tier,
        "home_store_id": customer.home_store_id,
        "size_preferences": customer.size_preferences or {},
    }
