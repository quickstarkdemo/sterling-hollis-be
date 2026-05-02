from __future__ import annotations

from collections.abc import Iterable
import re

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.catalog.schemas import CatalogProduct, ProductRecommendationRequest, ProductSort
from app.catalog.service import ProductFilters, get_product_detail, list_products, product_to_catalog, recommend_products, related_products
from app.models import CatalogProduct as CatalogProductModel
from app.models import Customer, Order, OrderItem, Product, Store
from app.services.embeddings import EmbeddingService
from app.services.pinecone_service import PineconeService


def product_detail(db: Session, product_id: str, store_id: str | None = None) -> CatalogProduct | None:
    return get_product_detail(db, product_id, store_id=store_id)


def related_product_cards(db: Session, product_id: str, *, limit: int = 3) -> list[CatalogProduct]:
    response = related_products(db, product_id, limit=limit)
    return response.items if response else []


def store_scoped_related_product_cards(
    db: Session,
    product_id: str,
    *,
    store_id: str | None = None,
    limit: int = 3,
) -> list[CatalogProduct]:
    response = related_products(db, product_id, limit=max(limit * 4, 12))
    if not response:
        return []
    if not store_id:
        return response.items[:limit]

    scoped: list[CatalogProduct] = []
    for card in response.items:
        detail = product_detail(db, card.id, store_id=store_id)
        if detail and detail.inventory_summary.availability != "out_of_stock":
            scoped.append(detail)
        if len(scoped) >= limit:
            break
    return scoped


def catalog_cards(
    db: Session,
    *,
    category: str | None = None,
    store_id: str | None = None,
    query: str | None = None,
    max_price: float | None = None,
    color: str | None = None,
    limit: int = 3,
) -> list[CatalogProduct]:
    response = list_products(
        db,
        ProductFilters(
            q=query,
            category=category,
            store_id=store_id,
            max_price=max_price,
            color=color,
            include_preorder=True,
            sort=ProductSort.relevance,
            limit=limit,
        ),
        include_facets=False,
    )
    return response.items


def _ordered_catalog_cards(
    db: Session,
    product_ids: Iterable[str],
    *,
    store_id: str | None,
) -> list[CatalogProduct]:
    ids = [product_id for product_id in product_ids if product_id]
    if not ids:
        return []
    product_map = {
        product.id: product
        for product in db.scalars(select(CatalogProductModel).where(CatalogProductModel.id.in_(ids))).all()
    }
    cards: list[CatalogProduct] = []
    seen: set[str] = set()
    for product_id in ids:
        if product_id in seen:
            continue
        product = product_map.get(product_id)
        if not product:
            continue
        card = product_to_catalog(db, product, store_id=store_id)
        assert isinstance(card, CatalogProduct)
        cards.append(card)
        seen.add(product_id)
    return cards


def _card_allowed(
    card: CatalogProduct,
    *,
    target_categories: list[str],
    exclude_categories: list[str],
    budget_max: float | None,
    current_product_id: str | None,
    require_available: bool,
) -> bool:
    if current_product_id and card.id == current_product_id:
        return False
    if target_categories and card.category not in target_categories:
        return False
    if exclude_categories and card.category in exclude_categories:
        return False
    if budget_max is not None and card.price_min > budget_max:
        return False
    if require_available and card.inventory_summary.availability == "out_of_stock":
        return False
    return True


def _filter_cards(
    cards: list[CatalogProduct],
    *,
    target_categories: list[str],
    exclude_categories: list[str],
    budget_max: float | None,
    colors: list[str],
    current_product_id: str | None,
    require_available: bool,
    limit: int,
) -> list[CatalogProduct]:
    filtered = []
    seen: set[str] = set()
    for card in cards:
        if card.id in seen:
            continue
        if _card_allowed(
            card,
            target_categories=target_categories,
            exclude_categories=exclude_categories,
            budget_max=budget_max,
            current_product_id=current_product_id,
            require_available=require_available,
        ):
            filtered.append(card)
            seen.add(card.id)
        if len(filtered) >= limit:
            break
    return filtered


def _semantic_vector_cards(
    db: Session,
    *,
    query: str,
    target_categories: list[str],
    exclude_categories: list[str],
    budget_max: float | None,
    colors: list[str],
    current_product_id: str | None,
    store_id: str | None,
    limit: int,
) -> list[CatalogProduct]:
    embedding_service = EmbeddingService()
    pinecone = PineconeService()
    if not (embedding_service.enabled and pinecone.enabled):
        return []
    vector = embedding_service.embed_text(query)
    pinecone_filter = {"category": {"$in": target_categories}} if target_categories else None
    matches = pinecone.query(
        namespace=pinecone.settings.pinecone_catalog_namespace,
        vector=vector,
        top_k=max(limit * 10, 50),
        filters=pinecone_filter,
    )
    product_ids = [
        str(match.get("metadata", {}).get("catalog_product_id") or match.get("id", "").replace("catalog:", ""))
        for match in matches
    ]
    cards = _ordered_catalog_cards(db, product_ids, store_id=store_id)
    return _filter_cards(
        cards,
        target_categories=target_categories,
        exclude_categories=exclude_categories,
        budget_max=budget_max,
        colors=colors,
        current_product_id=current_product_id,
        require_available=bool(store_id),
        limit=limit,
    )


def _sql_search_cards(
    db: Session,
    *,
    query: str | None,
    target_categories: list[str],
    exclude_categories: list[str],
    budget_max: float | None,
    colors: list[str],
    current_product_id: str | None,
    store_id: str | None,
    limit: int,
) -> list[CatalogProduct]:
    categories = target_categories or [None]
    cards: list[CatalogProduct] = []
    for category in categories:
        color_attempts = colors or [None]
        category_cards: list[CatalogProduct] = []
        for color in color_attempts:
            category_cards.extend(
                catalog_cards(
                    db,
                    category=category,
                    store_id=store_id,
                    query=query,
                    max_price=budget_max,
                    color=color,
                    limit=max(limit * 4, 12),
                )
            )
        if not category_cards and colors:
            category_cards.extend(
                catalog_cards(
                    db,
                    category=category,
                    store_id=store_id,
                    query=query,
                    max_price=budget_max,
                    limit=max(limit * 4, 12),
                )
            )
        cards.extend(category_cards)
    return _filter_cards(
        cards,
        target_categories=target_categories,
        exclude_categories=exclude_categories,
        budget_max=budget_max,
        colors=colors,
        current_product_id=current_product_id,
        require_available=bool(store_id),
        limit=limit,
    )


def semantic_catalog_cards(
    db: Session,
    *,
    query: str | None,
    target_categories: list[str],
    exclude_categories: list[str],
    budget_max: float | None,
    colors: list[str] | None,
    current_product_id: str | None,
    store_id: str | None,
    limit: int = 3,
) -> tuple[list[CatalogProduct], str]:
    clean_query = " ".join((query or "").split())
    color_filters = colors or []
    if clean_query:
        try:
            cards = _semantic_vector_cards(
                db,
                query=clean_query,
                target_categories=target_categories,
                exclude_categories=exclude_categories,
                budget_max=budget_max,
                colors=color_filters,
                current_product_id=current_product_id,
                store_id=store_id,
                limit=limit,
            )
            if cards:
                return cards, "semantic_catalog_search"
        except Exception:
            pass

        cards = _sql_search_cards(
            db,
            query=clean_query,
            target_categories=target_categories,
            exclude_categories=exclude_categories,
            budget_max=budget_max,
            colors=color_filters,
            current_product_id=current_product_id,
            store_id=store_id,
            limit=limit,
        )
        if cards:
            return cards, "sql_text_search_fallback"

    cards = _sql_search_cards(
        db,
        query=None,
        target_categories=target_categories,
        exclude_categories=exclude_categories,
        budget_max=budget_max,
        colors=color_filters,
        current_product_id=current_product_id,
        store_id=store_id,
        limit=limit,
    )
    return cards, "category_browse_fallback"


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


def store_info(db: Session, store_id: str | None = None) -> dict:
    store = db.get(Store, store_id) if store_id else None
    if store is None:
        store = db.scalars(select(Store).order_by(Store.name.asc())).first()
    if store is None:
        return {"found": False}
    address = ", ".join(
        part
        for part in [
            store.address_line1,
            store.address_line2,
            f"{store.city}, {store.state} {store.postal_code}",
        ]
        if part
    )
    return {
        "found": True,
        "id": store.id,
        "name": store.name,
        "phone": store.phone,
        "address": address,
        "city": store.city,
        "state": store.state,
        "services": store.services or [],
    }


def service_answer(message: str) -> dict:
    normalized = " ".join(message.lower().split())
    if any(term in normalized for term in ["return", "returns", "exchange", "exchanges"]):
        return {
            "topic": "returns_exchanges",
            "answer": (
                "For returns or exchanges, bring the item and receipt or order confirmation to a Sterling Hollis store. "
                "For order-specific eligibility, sign in so I can check the purchase."
            ),
        }
    if any(term in normalized for term in ["shipping", "ship", "delivery"]):
        return {
            "topic": "shipping",
            "answer": "I can help with general shipping questions. For a specific delivery status, please sign in so I can check your order.",
        }
    if "hours" in normalized:
        return {
            "topic": "hours",
            "answer": "Store hours vary by location. Ask for a store phone number or address and I can pull the store contact details.",
        }
    return {
        "topic": "customer_service",
        "answer": "I can help with product questions, store contact information, returns, shipping, and signed-in order status.",
    }


def order_status(db: Session, customer_id: str, message: str, *, limit: int = 1) -> dict:
    explicit_order_ids = re.findall(r"\border[_-][A-Za-z0-9_-]+|\bord[_-][A-Za-z0-9_-]+", message, flags=re.IGNORECASE)
    query = select(Order).where(Order.customer_id == customer_id)
    if explicit_order_ids:
        query = query.where(Order.id.in_(explicit_order_ids))
    orders = db.scalars(query.order_by(desc(Order.ordered_at)).limit(max(1, min(limit, 5)))).all()
    if not orders:
        return {"found": False, "orders": []}

    product_ids = []
    order_ids = [order.id for order in orders]
    items = db.scalars(select(OrderItem).where(OrderItem.order_id.in_(order_ids))).all()
    for item in items:
        product_ids.append(item.product_id)
    products = {product.id: product for product in db.scalars(select(Product).where(Product.id.in_(product_ids))).all()}
    items_by_order: dict[str, list[dict]] = {order_id: [] for order_id in order_ids}
    for item in items:
        product = products.get(item.product_id)
        items_by_order.setdefault(item.order_id, []).append(
            {
                "product_id": item.product_id,
                "title": product.title if product else item.product_id,
                "quantity": item.quantity,
                "line_total": float(item.line_total),
            }
        )

    return {
        "found": True,
        "orders": [
            {
                "id": order.id,
                "status": order.status,
                "ordered_at": order.ordered_at.isoformat(),
                "channel": order.channel,
                "total_amount": float(order.total_amount),
                "returned": bool(order.returned),
                "items": items_by_order.get(order.id, []),
            }
            for order in orders
        ],
    }


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
