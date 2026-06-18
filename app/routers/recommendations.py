from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.catalog.schemas import ProductSort
from app.catalog.service import ProductFilters, list_products
from app.database import get_db
from app.schemas import (
    CustomerRecommendationRequest,
    CustomerRecommendationResponse,
    MerchandisingRecommendationRequest,
    MerchandisingRecommendationResponse,
)
from app.services.recommendations import customer_recommendations, merchandising_recommendations

router = APIRouter(tags=["recommendations"])


@router.post("/recommendations/customer", response_model=CustomerRecommendationResponse)
def recommend_customer(req: CustomerRecommendationRequest, db: Session = Depends(get_db)):
    rows, strategy, applied_constraints, constraint_stage = customer_recommendations(db, req)
    return CustomerRecommendationResponse(
        store_id=req.store_id,
        strategy=strategy,
        recommendations=rows,
        applied_style_constraints=applied_constraints,
        constraint_source=applied_constraints.constraint_source if applied_constraints else None,
        constraint_stage=constraint_stage,
    )


@router.post("/recommendations/merchandising", response_model=MerchandisingRecommendationResponse)
def recommend_merchandising(req: MerchandisingRecommendationRequest, db: Session = Depends(get_db)):
    rows = merchandising_recommendations(db, req)
    return MerchandisingRecommendationResponse(store_id=req.store_id, objective=req.objective, recommendations=rows)


@router.get("/feeds/products/openai", deprecated=True)
def openai_product_feed(store_id: str | None = None, limit: int = 2000, db: Session = Depends(get_db)):
    """Compatibility export for OpenAI-commerce-style feed consumers.

    Retail frontend product discovery should use the declarative `/api/*` catalog endpoints.
    """
    products = list_products(
        db,
        ProductFilters(
            store_id=store_id,
            include_preorder=True,
            sort=ProductSort.relevance,
            limit=max(1, min(limit, 2000)),
        ),
        include_facets=False,
    ).items

    feed_rows = []
    for product in products:
        feed_rows.append(
            {
                "id": product.id,
                "title": product.title,
                "description": product.description,
                "link": product.link,
                "image_link": (
                    product.images.primary_url
                    if product.images and product.images.primary_url
                    else product.image_url
                ),
                "price": f"{product.price:.2f} USD",
                "availability": product.inventory_summary.availability,
                "brand": product.brand,
                "category": product.category,
                "color": product.attributes.get("color"),
                "size": None,
                "material": product.attributes.get("material"),
                "gender": product.attributes.get("gender"),
            }
        )

    return {"count": len(feed_rows), "items": feed_rows}
