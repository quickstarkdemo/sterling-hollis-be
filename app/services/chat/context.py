from __future__ import annotations

from app.services.chat.schemas import ChatContext


def summarize_context(context: ChatContext) -> dict:
    return {
        "page_type": context.page_type,
        "route": context.route,
        "product_id": context.product_id,
        "category": context.category,
        "store_id": context.store_id,
    }
