from __future__ import annotations

from app.services.chat.schemas import ChatContext


def summarize_context(context: ChatContext) -> dict:
    return {
        "page_type": context.page_type,
        "route": context.route,
        "product_id": context.product_id,
        "current_product": context.current_product.model_dump(mode="json") if context.current_product else None,
        "category": context.category,
        "store_id": context.store_id,
    }
