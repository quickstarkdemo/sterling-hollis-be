from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.catalog.schemas import CatalogProduct
from app.services.chat.context import summarize_context
from app.services.chat.intent_frame import ChatIntentFrame
from app.services.chat.schemas import ChatRequest
from app.services.chat.tools import (
    catalog_cards,
    product_detail,
    semantic_catalog_cards,
    store_info,
    store_scoped_related_product_cards,
)

try:  # pragma: no cover - exercised only when Strands is installed in runtime.
    from strands import tool as strands_tool
except Exception:  # pragma: no cover - local test env can run without Strands.

    def strands_tool(func: Callable | None = None, **_kwargs):
        def decorator(inner: Callable) -> Callable:
            return inner

        return decorator(func) if func is not None else decorator


def card_to_json(card: CatalogProduct) -> dict[str, Any]:
    return card.model_dump(mode="json")


def cards_payload(cards: list[CatalogProduct], *, strategy: str | None = None) -> dict[str, Any]:
    return {
        "cards": [card_to_json(card) for card in cards],
        "product_ids": [card.id for card in cards],
        "count": len(cards),
        "strategy": strategy,
    }


def _safe_exclude_categories(frame: ChatIntentFrame, requested: list[str] | None) -> list[str]:
    excluded = list(requested if requested is not None else frame.exclude_categories)
    if frame.intent != "complementary_products":
        return excluded
    if frame.current_product_id and len(frame.exclude_categories) == 0:
        return [category for category in excluded if category not in {"womens_apparel", "mens_apparel"}]
    return excluded


def build_storefront_tools(
    db: Session,
    req: ChatRequest,
    frame: ChatIntentFrame,
    record_tool_call: Callable[[str, dict[str, Any], dict[str, Any]], None],
) -> list[Callable[..., Any]]:
    store_id = req.context.store_id

    @strands_tool(name="search_catalog")
    def search_catalog(query: str | None = None, category: str | None = None, limit: int = 3) -> dict[str, Any]:
        """Search public catalog products using text, category, and the current store context."""
        cards = catalog_cards(
            db,
            category=category or req.context.category,
            store_id=store_id,
            query=query or frame.query,
            limit=max(1, min(limit, 6)),
        )
        output = cards_payload(cards, strategy="catalog_search")
        record_tool_call("search_catalog", {"query": query, "category": category, "limit": limit}, output)
        return output

    @strands_tool(name="semantic_catalog_search")
    def semantic_catalog_search(
        query: str | None = None,
        target_categories: list[str] | None = None,
        exclude_categories: list[str] | None = None,
        target_genders: list[str] | None = None,
        budget_max: float | None = None,
        colors: list[str] | None = None,
        limit: int = 3,
    ) -> dict[str, Any]:
        """Search the catalog for product or styling matches with normalized retail constraints."""
        safe_exclude_categories = _safe_exclude_categories(frame, exclude_categories)
        cards, strategy = semantic_catalog_cards(
            db,
            query=query or frame.query,
            target_categories=target_categories if target_categories is not None else frame.target_categories,
            exclude_categories=safe_exclude_categories,
            target_genders=target_genders if target_genders is not None else frame.target_genders,
            budget_max=budget_max if budget_max is not None else frame.budget_max,
            colors=colors if colors is not None else frame.colors,
            current_product_id=frame.current_product_id,
            store_id=store_id,
            limit=max(1, min(limit, 6)),
        )
        output = cards_payload(cards, strategy=strategy)
        record_tool_call(
            "semantic_catalog_search",
            {
                "query": query,
                "target_categories": target_categories,
                "exclude_categories": safe_exclude_categories,
                "target_genders": target_genders,
                "budget_max": budget_max,
                "colors": colors,
                "limit": limit,
            },
            output,
        )
        return output

    @strands_tool(name="get_current_product")
    def get_current_product() -> dict[str, Any]:
        """Return the product currently in the shopper's page context, if available."""
        current_product_id = frame.current_product_id
        card = product_detail(db, current_product_id or "", store_id=store_id) if current_product_id else None
        output = {
            "found": bool(card),
            "context": summarize_context(req.context),
            "card": card_to_json(card) if card else None,
            "product_ids": [card.id] if card else [],
        }
        record_tool_call("get_current_product", {}, output)
        return output

    @strands_tool(name="get_product_detail")
    def get_product_detail(product_id: str | None = None) -> dict[str, Any]:
        """Return public product detail for a specific product id."""
        resolved_id = product_id or frame.current_product_id
        card = product_detail(db, resolved_id or "", store_id=store_id) if resolved_id else None
        output = {"found": bool(card), "card": card_to_json(card) if card else None, "product_ids": [card.id] if card else []}
        record_tool_call("get_product_detail", {"product_id": product_id}, output)
        return output

    @strands_tool(name="find_related_products")
    def find_related_products(product_id: str | None = None, limit: int = 3) -> dict[str, Any]:
        """Find public related products for the current or supplied product."""
        resolved_id = product_id or frame.current_product_id
        cards = (
            store_scoped_related_product_cards(
                db,
                resolved_id,
                store_id=store_id,
                target_genders=frame.target_genders,
                limit=max(1, min(limit, 6)),
            )
            if resolved_id
            else []
        )
        output = cards_payload(cards, strategy="related_products")
        record_tool_call("find_related_products", {"product_id": product_id, "limit": limit}, output)
        return output

    @strands_tool(name="get_store_info")
    def get_store_info() -> dict[str, Any]:
        """Return public contact information for the current or default store."""
        output = store_info(db, store_id=store_id)
        record_tool_call("get_store_info", {"store_id": store_id}, output)
        return output

    return [
        search_catalog,
        semantic_catalog_search,
        get_current_product,
        get_product_detail,
        find_related_products,
        get_store_info,
    ]
