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

from ddtrace.llmobs import LLMObs
from ddtrace.llmobs.decorators import tool

def _llmobs_annotate_safe(**kwargs) -> None:
    try:
        if not LLMObs.enabled:
            return
        LLMObs.annotate(**kwargs)
    except Exception:
        pass


VALID_CATEGORIES = {
    "beauty",
    "handbags",
    "home",
    "jewelry_accessories",
    "mens_apparel",
    "shoes",
    "womens_apparel",
}

CATEGORY_ALIASES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("men", "mens", "men s", "men's", "male", "workwear", "suiting"), ("mens_apparel",)),
    (("women", "womens", "women s", "women's", "female", "dress", "dresses", "apparel"), ("womens_apparel",)),
    (("evening", "occasion", "formal", "wedding", "cocktail"), ("womens_apparel", "shoes", "handbags", "jewelry_accessories")),
    (("shoe", "shoes", "heel", "heels", "pump", "pumps", "boot", "boots", "sneaker", "sneakers"), ("shoes",)),
    (("bag", "bags", "handbag", "handbags", "purse", "purses", "clutch", "clutches"), ("handbags",)),
    (("jewelry", "jewellery", "accessory", "accessories", "watch", "bracelet", "necklace", "ring"), ("jewelry_accessories",)),
    (("beauty", "serum", "makeup", "skincare", "fragrance", "perfume"), ("beauty",)),
    (("home", "decor", "chair", "vase", "dinnerware"), ("home",)),
)

QUERY_STOPWORDS = {
    "any",
    "do",
    "find",
    "for",
    "have",
    "looking",
    "me",
    "need",
    "piece",
    "pieces",
    "product",
    "products",
    "search",
    "show",
    "you",
}


def product_detail(db: Session, product_id: str, store_id: str | None = None) -> CatalogProduct | None:
    return get_product_detail(db, product_id, store_id=store_id)


def _normalized_text(value: str | None) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (value or "").lower().replace("&", " and ")).split())


def _append_unique(values: list[str], next_values: Iterable[str]) -> None:
    for value in next_values:
        if value and value not in values:
            values.append(value)


def _categories_for_text(value: str | None) -> list[str]:
    normalized = _normalized_text(value)
    compact = normalized.replace(" ", "_")
    categories: list[str] = []
    if compact in VALID_CATEGORIES:
        categories.append(compact)
    for aliases, mapped_categories in CATEGORY_ALIASES:
        if any(f" {alias} " in f" {normalized} " for alias in aliases):
            _append_unique(categories, mapped_categories)
    return categories


def _normalized_categories(raw_categories: list[str], query: str | None) -> list[str]:
    categories: list[str] = []
    for category in raw_categories:
        normalized = _normalized_text(category).replace(" ", "_")
        if normalized in VALID_CATEGORIES:
            _append_unique(categories, [normalized])
        else:
            _append_unique(categories, _categories_for_text(category))
    _append_unique(categories, _categories_for_text(query))
    return categories


def _query_variants(query: str | None) -> list[str]:
    clean = _normalized_text(query)
    if not clean:
        return []
    tokens = [token for token in clean.split() if token not in QUERY_STOPWORDS]
    variants = [clean]
    if tokens:
        compact = " ".join(tokens)
        if compact != clean:
            variants.append(compact)
        for token in tokens:
            if len(token) >= 4 and token not in variants:
                variants.append(token)
    return variants


def _normalized_gender(gender: str | None) -> str | None:
    clean = (gender or "").strip().lower()
    if clean in {"women", "woman", "womens", "female", "girls"}:
        return "female"
    if clean in {"men", "man", "mens", "male", "boys"}:
        return "male"
    if clean == "unisex":
        return "unisex"
    return clean or None


def _gender_allowed(card: CatalogProduct, target_genders: list[str]) -> bool:
    normalized_targets = {_normalized_gender(gender) for gender in target_genders}
    normalized_targets.discard(None)
    if not normalized_targets:
        return True
    card_gender = _normalized_gender(card.attributes.get("gender"))
    if not card_gender:
        return True
    return card_gender == "unisex" or card_gender in normalized_targets


def related_product_cards(db: Session, product_id: str, *, limit: int = 3) -> list[CatalogProduct]:
    response = related_products(db, product_id, limit=limit)
    return response.items if response else []


def store_scoped_related_product_cards(
    db: Session,
    product_id: str,
    *,
    store_id: str | None = None,
    target_genders: list[str] | None = None,
    limit: int = 3,
) -> list[CatalogProduct]:
    response = related_products(db, product_id, limit=max(limit * 4, 12))
    if not response:
        return []
    gender_filters = target_genders or []
    if not store_id:
        return [card for card in response.items if _gender_allowed(card, gender_filters)][:limit]

    scoped: list[CatalogProduct] = []
    for card in response.items:
        detail = product_detail(db, card.id, store_id=store_id)
        if detail and detail.inventory_summary.availability != "out_of_stock" and _gender_allowed(detail, gender_filters):
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
    target_genders: list[str],
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
    if not _gender_allowed(card, target_genders):
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
    target_genders: list[str],
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
            target_genders=target_genders,
            budget_max=budget_max,
            current_product_id=current_product_id,
            require_available=require_available,
        ):
            filtered.append(card)
            seen.add(card.id)
        if len(filtered) >= limit:
            break
    return filtered


@tool(name="semantic_vector_cards")
def _semantic_vector_cards(
    db: Session,
    *,
    query: str,
    target_categories: list[str],
    exclude_categories: list[str],
    target_genders: list[str],
    budget_max: float | None,
    colors: list[str],
    current_product_id: str | None,
    store_id: str | None,
    limit: int,
) -> list[CatalogProduct]:
    _llmobs_annotate_safe(
        input_data={
            "query": query,
            "target_categories": target_categories,
            "exclude_categories": exclude_categories,
            "target_genders": target_genders,
            "budget_max": budget_max,
            "colors": colors,
            "current_product_id": current_product_id,
            "store_id": store_id,
            "limit": limit,
        },
        tags={
            "workflow": "chat",
            "tool": "semantic_vector_cards",
        },
    )

    embedding_service = EmbeddingService()
    pinecone = PineconeService()

    if not (embedding_service.enabled and pinecone.enabled):
        _llmobs_annotate_safe(
            output_data={
                "card_count": 0,
                "disabled": True,
                "embedding_enabled": embedding_service.enabled,
                "pinecone_enabled": pinecone.enabled,
            },
        )
        return []

    vector = embedding_service.embed_text(query)

    pinecone_filter = (
        {"category": {"$in": target_categories}}
        if target_categories
        else None
    )

    matches = pinecone.query(
        namespace=pinecone.settings.pinecone_catalog_namespace,
        vector=vector,
        top_k=max(limit * 10, 50),
        filters=pinecone_filter,
    )

    product_ids = [
        str(
            match.get("metadata", {}).get("catalog_product_id")
            or match.get("id", "").replace("catalog:", "")
        )
        for match in matches
    ]

    cards = _ordered_catalog_cards(
        db,
        product_ids,
        store_id=store_id,
    )

    result_cards = _filter_cards(
        cards,
        target_categories=target_categories,
        exclude_categories=exclude_categories,
        target_genders=target_genders,
        budget_max=budget_max,
        colors=colors,
        current_product_id=current_product_id,
        require_available=bool(store_id),
        limit=limit,
    )

    _llmobs_annotate_safe(
        output_data={
            "match_count": len(matches),
            "candidate_product_ids": product_ids[:20],
            "card_count": len(result_cards),
            "product_ids": [card.id for card in result_cards],
            "pinecone_filter": pinecone_filter,
        },
    )

    return result_cards



@tool(name="sql_search_cards")
def _sql_search_cards(
    db: Session,
    *,
    query: str | None,
    target_categories: list[str],
    exclude_categories: list[str],
    target_genders: list[str],
    budget_max: float | None,
    colors: list[str],
    current_product_id: str | None,
    store_id: str | None,
    limit: int,
) -> list[CatalogProduct]:
    _llmobs_annotate_safe(
        input_data={
            "query": query,
            "target_categories": target_categories,
            "exclude_categories": exclude_categories,
            "target_genders": target_genders,
            "budget_max": budget_max,
            "colors": colors,
            "current_product_id": current_product_id,
            "store_id": store_id,
            "limit": limit,
        },
        tags={
            "workflow": "chat",
            "tool": "sql_search_cards",
        },
    )

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

    result_cards = _filter_cards(
        cards,
        target_categories=target_categories,
        exclude_categories=exclude_categories,
        target_genders=target_genders,
        budget_max=budget_max,
        colors=colors,
        current_product_id=current_product_id,
        require_available=bool(store_id),
        limit=limit,
    )

    _llmobs_annotate_safe(
        output_data={
            "candidate_count": len(cards),
            "card_count": len(result_cards),
            "product_ids": [card.id for card in result_cards],
            "top_cards": [
                {
                    "id": card.id,
                    "title": card.title,
                    "brand": card.brand,
                    "category": card.category,
                }
                for card in result_cards[:10]
            ],
        },
    )

    return result_cards


@tool(name="semantic_catalog_cards")
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
    target_genders: list[str] | None = None,
    limit: int = 3,
) -> tuple[list[CatalogProduct], str]:
    clean_query = " ".join((query or "").split())
    color_filters = colors or []
    gender_filters = target_genders or []
    normalized_target_categories = _normalized_categories(
        target_categories,
        clean_query,
    )
    normalized_exclude_categories = _normalized_categories(
        exclude_categories,
        None,
    )

    _llmobs_annotate_safe(
        input_data={
            "query": clean_query,
            "target_categories": normalized_target_categories,
            "exclude_categories": normalized_exclude_categories,
            "budget_max": budget_max,
            "colors": color_filters,
            "current_product_id": current_product_id,
            "store_id": store_id,
            "target_genders": gender_filters,
            "limit": limit,
        },
        tags={
            "workflow": "chat",
            "tool": "semantic_catalog_cards",
        },
    )

    result_cards: list[CatalogProduct] = []
    strategy = "category_browse_fallback"

    for query_variant in _query_variants(clean_query):
        try:
            cards = _semantic_vector_cards(
                db,
                query=query_variant,
                target_categories=normalized_target_categories,
                exclude_categories=normalized_exclude_categories,
                target_genders=gender_filters,
                budget_max=budget_max,
                colors=color_filters,
                current_product_id=current_product_id,
                store_id=store_id,
                limit=limit,
            )
            if cards:
                result_cards = cards
                strategy = "semantic_catalog_search"
                break
        except Exception:
            pass

        cards = _sql_search_cards(
            db,
            query=query_variant,
            target_categories=normalized_target_categories,
            exclude_categories=normalized_exclude_categories,
            target_genders=gender_filters,
            budget_max=budget_max,
            colors=color_filters,
            current_product_id=current_product_id,
            store_id=store_id,
            limit=limit,
        )
        if cards:
            result_cards = cards
            strategy = "sql_text_search_fallback"
            break

    if not result_cards:
        result_cards = _sql_search_cards(
            db,
            query=None,
            target_categories=normalized_target_categories,
            exclude_categories=normalized_exclude_categories,
            target_genders=gender_filters,
            budget_max=budget_max,
            colors=color_filters,
            current_product_id=current_product_id,
            store_id=store_id,
            limit=limit,
        )
        strategy = "category_browse_fallback"

    _llmobs_annotate_safe(
        output_data={
            "strategy": strategy,
            "card_count": len(result_cards),
            "product_ids": [card.id for card in result_cards],
            "top_cards": [
                {
                    "id": card.id,
                    "title": card.title,
                    "brand": card.brand,
                    "category": card.category,
                }
                for card in result_cards[:10]
            ],
        },
    )

    return result_cards, strategy



@tool(name="recommendation_cards")
def recommendation_cards(
    db: Session,
    *,
    customer_id: str | None = None,
    store_id: str | None = None,
    category: str | None = None,
    limit: int = 3,
) -> list[CatalogProduct]:
    _llmobs_annotate_safe(
        input_data={
            "customer_id_present": bool(customer_id),
            "store_id": store_id,
            "category": category,
            "limit": limit,
        },
        tags={
            "workflow": "chat",
            "tool": "recommendation_cards",
        },
    )

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

    cards = [row.product for row in response.recommendations]

    _llmobs_annotate_safe(
        output_data={
            "recommendation_count": len(cards),
            "product_ids": [card.id for card in cards],
            "top_cards": [
                {
                    "id": card.id,
                    "title": card.title,
                    "brand": card.brand,
                    "category": card.category,
                }
                for card in cards[:10]
            ],
        },
        metadata={
            "strategy": response.strategy,
        },
    )

    return cards



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
