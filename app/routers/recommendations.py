from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Product
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
    rows, strategy = customer_recommendations(db, req)
    return CustomerRecommendationResponse(store_id=req.store_id, strategy=strategy, recommendations=rows)


@router.post("/recommendations/merchandising", response_model=MerchandisingRecommendationResponse)
def recommend_merchandising(req: MerchandisingRecommendationRequest, db: Session = Depends(get_db)):
    rows = merchandising_recommendations(db, req)
    return MerchandisingRecommendationResponse(store_id=req.store_id, objective=req.objective, recommendations=rows)


@router.get("/feeds/products/openai")
def openai_product_feed(store_id: str | None = None, limit: int = 2000, db: Session = Depends(get_db)):
    query = select(Product)
    if store_id:
        query = query.where(Product.store_id == store_id)

    products = db.scalars(query.limit(limit)).all()

    feed_rows = []
    for p in products:
        feed_rows.append(
            {
                "id": p.id,
                "title": p.title,
                "description": p.description,
                "link": p.link,
                "image_link": p.image_link,
                "price": f"{p.price:.2f} USD",
                "availability": p.availability,
                "brand": p.brand,
                "category": p.category,
                "color": p.color,
                "size": p.size,
                "material": p.material,
                "gender": p.gender,
            }
        )

    return {"count": len(feed_rows), "items": feed_rows}
