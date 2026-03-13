from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Order, OrderItem, Product, SyntheticValidationFailure


def run_validation_checks(db: Session, run_id: str) -> int:
    db.execute(delete(SyntheticValidationFailure).where(SyntheticValidationFailure.seed_run_id == run_id))
    db.commit()

    failures: list[dict] = []

    # 1) orphan items
    orphan_items = db.scalars(
        select(OrderItem).outerjoin(Order, OrderItem.order_id == Order.id).where(Order.id.is_(None))
    ).all()
    for row in orphan_items:
        failures.append(
            {
                "seed_run_id": run_id,
                "check_name": "orphan_order_items",
                "entity": "order_items",
                "entity_id": row.id,
                "detail": f"order_id {row.order_id} not found for item",
            }
        )

    # 2) negative price checks
    for product in db.scalars(select(Product).where(Product.seed_run_id == run_id, Product.price < 0)).all():
        failures.append(
            {
                "seed_run_id": run_id,
                "check_name": "negative_product_price",
                "entity": "products",
                "entity_id": product.id,
                "detail": f"price is negative: {product.price}",
            }
        )

    for order in db.scalars(select(Order).where(Order.seed_run_id == run_id, Order.total_amount < 0)).all():
        failures.append(
            {
                "seed_run_id": run_id,
                "check_name": "negative_order_total",
                "entity": "orders",
                "entity_id": order.id,
                "detail": f"total_amount is negative: {order.total_amount}",
            }
        )

    # 3) feed required field checks
    required = ["title", "description", "link", "image_link", "availability", "brand", "category"]
    products = db.scalars(select(Product).where(Product.seed_run_id == run_id)).all()
    for p in products:
        missing = [field for field in required if not getattr(p, field)]
        if p.price is None:
            missing.append("price")
        if missing:
            failures.append(
                {
                    "seed_run_id": run_id,
                    "check_name": "feed_required_missing",
                    "entity": "products",
                    "entity_id": p.id,
                    "detail": f"missing fields: {', '.join(missing)}",
                }
            )

    if failures:
        db.bulk_insert_mappings(SyntheticValidationFailure, failures)
    db.commit()
    return len(failures)
