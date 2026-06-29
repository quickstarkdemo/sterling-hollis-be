from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CatalogProduct,
    Customer,
    Order,
    OrderItem,
    Product,
    ProductInventory,
    Store,
)
from app.services.taxonomy import CATEGORY_TAXONOMY


LOW_STOCK_THRESHOLD = 5

_STOPWORDS = {
    "about",
    "across",
    "and",
    "are",
    "associated",
    "available",
    "bought",
    "catalog",
    "categories",
    "category",
    "currently",
    "customers",
    "data",
    "for",
    "from",
    "have",
    "inventory",
    "lifecycle",
    "loaded",
    "name",
    "order",
    "orders",
    "product",
    "products",
    "purchase",
    "purchased",
    "question",
    "status",
    "statuses",
    "store",
    "stores",
    "tell",
    "the",
    "their",
    "two",
    "what",
    "which",
    "with",
}

_CATEGORY_ALIASES: dict[str, str] = {
    "accessories": "jewelry_accessories",
    "apparel": "womens_apparel",
    "beauty": "beauty",
    "designer shoes": "shoes",
    "handbag": "handbags",
    "handbags": "handbags",
    "home": "home",
    "jewelry": "jewelry_accessories",
    "jewelry accessories": "jewelry_accessories",
    "kids": "kids",
    "men": "mens_apparel",
    "mens": "mens_apparel",
    "mens apparel": "mens_apparel",
    "shoe": "shoes",
    "shoes": "shoes",
    "women": "womens_apparel",
    "womens": "womens_apparel",
    "womens apparel": "womens_apparel",
    "women apparel": "womens_apparel",
}


@dataclass
class CatalogAssistantToolResult:
    message: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def as_tool_payload(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "citations": self.citations,
            **self.data,
        }


def _norm(value: str | None) -> str:
    return " ".join(str(value or "").casefold().replace("-", " ").replace("_", " ").split())


def _tokens(value: str | None) -> list[str]:
    normalized = _norm(value)
    if not normalized:
        return []
    return [
        token
        for token in re.findall(r"[a-z0-9]+", normalized)
        if len(token) >= 3 and token not in _STOPWORDS
    ]


def _money(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    return None


def _score(tokens: list[str], *values: str | None) -> int:
    if not tokens:
        return 1
    haystack = _norm(" ".join(str(value or "") for value in values))
    if not haystack:
        return 0
    score = sum(1 for token in tokens if token in haystack)
    phrase = " ".join(tokens)
    if phrase and phrase in haystack:
        score += len(tokens) * 2
    return score


def _category_from_text(value: str | None) -> str | None:
    normalized = _norm(value)
    if not normalized:
        return None
    for key, config in CATEGORY_TAXONOMY.items():
        if key.replace("_", " ") in normalized:
            return key
        label = _norm(str(config.get("label") or ""))
        if label and label in normalized:
            return key
    for alias, category in sorted(
        _CATEGORY_ALIASES.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if alias in normalized:
            return category
    return None


def _lifecycle_from_text(value: str | None) -> str | None:
    normalized = _norm(value)
    for lifecycle in ("published", "draft", "archived"):
        if lifecycle in normalized:
            return lifecycle
    return None


def _store_matches(db: Session, query: str | None) -> list[Store]:
    tokens = _tokens(query)
    stores = db.scalars(select(Store).order_by(Store.name.asc(), Store.id.asc())).all()
    scored = [
        (
            _score(tokens, store.id, store.name, store.city, store.state),
            store,
        )
        for store in stores
    ]
    return [store for score, store in sorted(scored, key=lambda item: (-item[0], item[1].name)) if score > 0]


def _catalog_product_inventory_summary(db: Session, product_id: str) -> dict[str, Any]:
    rows = db.scalars(
        select(ProductInventory).where(ProductInventory.catalog_product_id == product_id)
    ).all()
    units = sum(int(row.inventory_qty or 0) for row in rows)
    low_stock = sum(
        1
        for row in rows
        if int(row.inventory_qty or 0) <= LOW_STOCK_THRESHOLD
        or _norm(row.availability) in {"low stock", "out of stock"}
    )
    store_ids = {row.store_id for row in rows}
    return {
        "inventory_rows": len(rows),
        "inventory_qty": units,
        "store_count": len(store_ids),
        "low_stock_rows": low_stock,
    }


def _catalog_product_citation(db: Session, product: CatalogProduct) -> dict[str, Any]:
    inventory_summary = _catalog_product_inventory_summary(db, product.id)
    return {
        "kind": "product",
        "source_id": product.id,
        "label": product.title,
        "value": {
            "source_system": "catalog_products",
            "product_id": product.id,
            "title": product.title,
            "brand": product.brand,
            "category": product.category,
            "lifecycle_status": product.lifecycle_status,
            "price_min": _money(product.price_min),
            "price_max": _money(product.price_max),
            **inventory_summary,
        },
    }


def _legacy_product_citation(product: Product, store: Store | None = None) -> dict[str, Any]:
    return {
        "kind": "product",
        "source_id": product.id,
        "label": product.title,
        "value": {
            "source_system": "products",
            "product_id": product.id,
            "title": product.title,
            "brand": product.brand,
            "category": product.category,
            "store_id": product.store_id,
            "store_name": store.name if store else product.store_id,
            "availability": product.availability,
            "inventory_qty": int(product.inventory_qty or 0),
            "price": _money(product.price),
            "margin_pct": _money(product.margin_pct),
        },
    }


def search_catalog_products(
    db: Session,
    *,
    query: str | None = None,
    brand: str | None = None,
    category: str | None = None,
    lifecycle_status: str | None = None,
    limit: int = 6,
    include_legacy: bool = True,
) -> CatalogAssistantToolResult:
    bounded_limit = max(1, min(int(limit or 6), 12))
    normalized_category = _category_from_text(category) or _category_from_text(query)
    normalized_lifecycle = lifecycle_status or _lifecycle_from_text(query)
    product_tokens = _tokens(" ".join(part for part in [query, brand] if part))

    catalog_query = select(CatalogProduct)
    if normalized_category:
        catalog_query = catalog_query.where(CatalogProduct.category == normalized_category)
    if normalized_lifecycle:
        catalog_query = catalog_query.where(CatalogProduct.lifecycle_status == normalized_lifecycle)
    if brand:
        catalog_query = catalog_query.where(func.lower(CatalogProduct.brand) == brand.casefold())
    catalog_rows = db.scalars(catalog_query.order_by(CatalogProduct.updated_at.desc(), CatalogProduct.id.asc()).limit(2000)).all()
    scored_catalog = [
        (_score(product_tokens, product.title, product.brand, product.category), product)
        for product in catalog_rows
    ]
    catalog_matches = [
        product
        for score, product in sorted(
            scored_catalog,
            key=lambda item: (-item[0], item[1].brand, item[1].title, item[1].id),
        )
        if score > 0
    ]

    citations = [_catalog_product_citation(db, product) for product in catalog_matches[:bounded_limit]]

    if include_legacy and len(citations) < bounded_limit and not normalized_lifecycle:
        stores = {store.id: store for store in db.scalars(select(Store)).all()}
        legacy_rows = db.scalars(select(Product).order_by(Product.title.asc()).limit(3000)).all()
        if normalized_category:
            legacy_rows = [row for row in legacy_rows if _norm(row.category) == _norm(normalized_category)]
        if brand:
            legacy_rows = [row for row in legacy_rows if _norm(row.brand) == _norm(brand)]
        scored_legacy = [
            (_score(product_tokens, product.title, product.brand, product.category), product)
            for product in legacy_rows
        ]
        for score, product in sorted(
            scored_legacy,
            key=lambda item: (-item[0], item[1].brand, item[1].title, item[1].id),
        ):
            if score <= 0:
                continue
            if any(citation["value"].get("title") == product.title for citation in citations):
                continue
            citations.append(_legacy_product_citation(product, stores.get(product.store_id)))
            if len(citations) >= bounded_limit:
                break

    if not citations:
        return CatalogAssistantToolResult(
            message="No matching catalog products were found.",
            data={"products": [], "count": 0},
        )

    product_preview = "; ".join(
        f"{item['value']['title']} ({item['value']['brand']}, {item['value']['category']}, {item['value'].get('lifecycle_status') or item['value'].get('availability')})"
        for item in citations[:3]
    )
    suffix = " More matching products are cited." if len(citations) > 3 else ""
    return CatalogAssistantToolResult(
        message=f"Matching catalog products: {product_preview}.{suffix}",
        citations=citations,
        data={"products": [citation["value"] for citation in citations], "count": len(citations)},
    )


def _low_stock(row: dict[str, Any]) -> bool:
    return int(row.get("inventory_qty") or 0) <= LOW_STOCK_THRESHOLD or _norm(row.get("availability")) in {
        "low stock",
        "out of stock",
    }


def _inventory_rows(
    db: Session,
    *,
    question: str | None = None,
    product_query: str | None = None,
    store_query: str | None = None,
    category: str | None = None,
    lifecycle_status: str | None = None,
    limit: int = 300,
) -> list[dict[str, Any]]:
    normalized_category = _category_from_text(category) or _category_from_text(question)
    normalized_lifecycle = lifecycle_status or _lifecycle_from_text(question)
    stores = _store_matches(db, store_query or question)
    store_ids = {store.id for store in stores} if stores else set()
    product_tokens = _tokens(product_query)

    rows: list[dict[str, Any]] = []
    query = (
        select(ProductInventory, CatalogProduct, Store)
        .join(CatalogProduct, CatalogProduct.id == ProductInventory.catalog_product_id)
        .join(Store, Store.id == ProductInventory.store_id)
    )
    if normalized_category:
        query = query.where(CatalogProduct.category == normalized_category)
    if normalized_lifecycle:
        query = query.where(CatalogProduct.lifecycle_status == normalized_lifecycle)
    if store_ids:
        query = query.where(ProductInventory.store_id.in_(store_ids))
    for inventory, product, store in db.execute(query.limit(limit)).all():
        if product_tokens and _score(product_tokens, product.title, product.brand, product.category) <= 0:
            continue
        rows.append(
            {
                "source_system": "product_inventory",
                "product_id": product.id,
                "title": product.title,
                "brand": product.brand,
                "category": product.category,
                "lifecycle_status": product.lifecycle_status,
                "store_id": store.id,
                "store_name": store.name,
                "city": store.city,
                "state": store.state,
                "size": inventory.size,
                "availability": inventory.availability,
                "inventory_qty": int(inventory.inventory_qty or 0),
            }
        )

    legacy_query = select(Product, Store).join(Store, Store.id == Product.store_id)
    if store_ids:
        legacy_query = legacy_query.where(Product.store_id.in_(store_ids))
    for product, store in db.execute(legacy_query.limit(limit)).all():
        if normalized_category and _norm(product.category) != _norm(normalized_category):
            continue
        if product_tokens and _score(product_tokens, product.title, product.brand, product.category) <= 0:
            continue
        rows.append(
            {
                "source_system": "products",
                "product_id": product.id,
                "title": product.title,
                "brand": product.brand,
                "category": product.category,
                "lifecycle_status": "legacy",
                "store_id": store.id,
                "store_name": store.name,
                "city": store.city,
                "state": store.state,
                "size": product.size,
                "availability": product.availability,
                "inventory_qty": int(product.inventory_qty or 0),
                "price": _money(product.price),
                "margin_pct": _money(product.margin_pct),
            }
        )
    return rows


def summarize_inventory(
    db: Session,
    *,
    question: str | None = None,
    product_query: str | None = None,
    store_query: str | None = None,
    category: str | None = None,
    lifecycle_status: str | None = None,
    low_stock_only: bool | None = None,
    limit: int = 8,
) -> CatalogAssistantToolResult:
    bounded_limit = max(1, min(int(limit or 8), 12))
    rows = _inventory_rows(
        db,
        question=question,
        product_query=product_query,
        store_query=store_query,
        category=category,
        lifecycle_status=lifecycle_status,
    )
    normalized_question = _norm(question)
    wants_product_candidate = any(token in normalized_question for token in ("candidate", "discount", "item", "markdown", "product", "promote", "sale", "sku"))
    wants_low_stock = low_stock_only if low_stock_only is not None else any(token in normalized_question for token in ("low", "out of stock", "risk"))
    filtered = [row for row in rows if _low_stock(row)] if wants_low_stock else rows

    if wants_product_candidate:
        candidates = sorted(
            filtered or rows,
            key=lambda row: (
                int(row.get("inventory_qty") or 0),
                float(row.get("margin_pct") or 0.0),
                row.get("title") or "",
            ),
            reverse=True,
        )[:bounded_limit]
        citations = [
            {
                "kind": "inventory",
                "source_id": str(row["product_id"]),
                "label": f"{row['store_name']}: {row['title']}",
                "value": row,
            }
            for row in candidates
        ]
        if not citations:
            return CatalogAssistantToolResult(message="No product-level inventory rows were found.", data={"rows": [], "count": 0})
        lead = citations[0]["value"]
        margin_pct = float(lead.get("margin_pct") or 0.0) * 100.0
        discount_context = (
            " A targeted 10% to 15% discount offer is a reasonable starting point for review because it has inventory to work through and cited margin headroom."
            if any(token in normalized_question for token in ("discount", "markdown", "sale"))
            else ""
        )
        suffix = " Additional product-level candidates are cited." if len(citations) > 1 else ""
        return CatalogAssistantToolResult(
            message=(
                f"{lead['store_name']}: the strongest product-level candidate is {lead['title']} "
                f"({lead['brand']}, {lead['category']}) with {lead['inventory_qty']} unit(s), "
                f"{margin_pct:.1f}% margin, {lead['availability']}."
                f"{discount_context}{suffix}"
            ),
            citations=citations,
            data={"rows": [citation["value"] for citation in citations], "count": len(citations)},
        )

    by_store: dict[str, dict[str, Any]] = {}
    for row in filtered:
        store = by_store.setdefault(
            row["store_id"],
            {
                "store_id": row["store_id"],
                "store_name": row["store_name"],
                "city": row["city"],
                "state": row["state"],
                "sku_count": 0,
                "low_stock_skus": 0,
                "out_of_stock_skus": 0,
                "units_in_stock": 0,
                "product_titles": [],
            },
        )
        store["sku_count"] += 1
        if _low_stock(row):
            store["low_stock_skus"] += 1
        if _norm(row.get("availability")) == "out of stock" or int(row.get("inventory_qty") or 0) <= 0:
            store["out_of_stock_skus"] += 1
        if _norm(row.get("availability")) in {"in stock", "low stock"}:
            store["units_in_stock"] += int(row.get("inventory_qty") or 0)
        if row["title"] not in store["product_titles"]:
            store["product_titles"].append(row["title"])

    store_rows = sorted(
        by_store.values(),
        key=lambda row: (-int(row["low_stock_skus"]), -int(row["out_of_stock_skus"]), row["store_name"]),
    )[:bounded_limit]
    citations = [
        {
            "kind": "inventory",
            "source_id": row["store_id"],
            "label": row["store_name"],
            "value": row,
        }
        for row in store_rows
    ]
    if not citations:
        return CatalogAssistantToolResult(message="No matching inventory rows were found.", data={"stores": [], "count": 0})
    preview = "; ".join(
        f"{row['store_name']} has {row['low_stock_skus']}/{row['sku_count']} SKU(s) low or out of stock and {row['units_in_stock']} unit(s) in stock"
        for row in store_rows[:3]
    )
    suffix = " More store inventory rows are cited." if len(citations) > 3 else ""
    return CatalogAssistantToolResult(
        message=f"Store inventory readout: {preview}.{suffix}",
        citations=citations,
        data={"stores": [citation["value"] for citation in citations], "count": len(citations)},
    )


def lookup_customer_purchases(
    db: Session,
    *,
    product_query: str,
    customer_query: str | None = None,
    store_query: str | None = None,
    limit: int = 8,
) -> CatalogAssistantToolResult:
    bounded_limit = max(1, min(int(limit or 8), 12))
    product_tokens = _tokens(product_query)
    products = db.scalars(select(Product).order_by(Product.title.asc()).limit(3000)).all()
    ranked_products = [
        (score, product)
        for product in products
        if (score := _score(product_tokens, product.title, product.brand, product.category)) > 0
    ]
    ranked_products.sort(key=lambda item: (-item[0], item[1].title, item[1].id))
    product_ids = [product.id for _, product in ranked_products[:25]]
    if not product_ids:
        return CatalogAssistantToolResult(
            message=f"No order-linked products matched {product_query!r}.",
            data={"orders": [], "count": 0},
        )

    query = (
        select(Order, OrderItem, Product, Customer, Store)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(Product, Product.id == OrderItem.product_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Store, Store.id == Order.store_id)
        .where(OrderItem.product_id.in_(product_ids))
    )
    if store_query:
        stores = _store_matches(db, store_query)
        store_ids = [store.id for store in stores]
        if store_ids:
            query = query.where(Order.store_id.in_(store_ids))
    customer_tokens = _tokens(customer_query)
    rows = []
    for order, item, product, customer, store in db.execute(query.order_by(Order.ordered_at.desc()).limit(100)).all():
        if customer_tokens and _score(customer_tokens, customer.first_name, customer.last_name, customer.id) <= 0:
            continue
        rows.append((order, item, product, customer, store))
        if len(rows) >= bounded_limit:
            break

    citations = [
        {
            "kind": "order",
            "source_id": f"{order.id}:{item.id}",
            "label": f"{customer.first_name} {customer.last_name}: {product.title}",
            "value": {
                "order_id": order.id,
                "order_item_id": item.id,
                "customer_id": customer.id,
                "customer_name": f"{customer.first_name} {customer.last_name}",
                "loyalty_tier": customer.loyalty_tier,
                "home_store_id": customer.home_store_id,
                "order_store_id": store.id,
                "order_store_name": store.name,
                "product_id": product.id,
                "product_title": product.title,
                "brand": product.brand,
                "category": product.category,
                "quantity": int(item.quantity or 0),
                "ordered_at": _date(order.ordered_at),
                "order_status": order.status,
                "order_total": _money(order.total_amount),
            },
        }
        for order, item, product, customer, store in rows
    ]
    if not citations:
        return CatalogAssistantToolResult(
            message=f"No customer purchases were found for {product_query!r}.",
            data={"orders": [], "count": 0},
        )

    preview = "; ".join(
        f"{item['value']['customer_name']} bought {item['value']['quantity']} {item['value']['product_title']} item(s) through {item['value']['order_store_name']}"
        for item in citations[:3]
    )
    suffix = " More customer purchase rows are cited." if len(citations) > 3 else ""
    return CatalogAssistantToolResult(
        message=f"Customer purchase matches: {preview}.{suffix}",
        citations=citations,
        data={"orders": [citation["value"] for citation in citations], "count": len(citations)},
    )
