from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.services.chat.schemas import ChatContext


ChatIntent = Literal["account", "customer_recommendation", "product_context", "catalog_search"]
ChatRoute = Literal["simple_tool", "agentic_response", "pinecone_lookup", "blocked"]


@dataclass(frozen=True)
class TriageDecision:
    intent: ChatIntent
    route: ChatRoute
    reason: str


ACCOUNT_TERMS = {
    "account",
    "order",
    "orders",
    "purchase",
    "purchases",
    "profile",
    "loyalty",
    "points",
    "my size",
    "my sizes",
    "my info",
    "my information",
}

STYLE_TERMS = {
    "go with",
    "goes with",
    "pair",
    "pairs",
    "complement",
    "match",
    "matches",
    "style",
    "wear with",
    "recommend",
    "recommendation",
}

SEARCH_TERMS = {"find", "search", "show me", "looking for", "do you have"}


def triage_chat(message: str, context: ChatContext) -> TriageDecision:
    normalized = " ".join(message.lower().split())
    if any(term in normalized for term in ACCOUNT_TERMS):
        return TriageDecision(intent="account", route="simple_tool", reason="account-specific wording")
    if any(term in normalized for term in STYLE_TERMS):
        route: ChatRoute = "pinecone_lookup" if not context.product_id else "simple_tool"
        return TriageDecision(intent="customer_recommendation", route=route, reason="style or recommendation wording")
    if context.product_id:
        return TriageDecision(intent="product_context", route="simple_tool", reason="product detail page context")
    if any(term in normalized for term in SEARCH_TERMS) or context.category:
        return TriageDecision(intent="catalog_search", route="pinecone_lookup", reason="catalog/category search")
    return TriageDecision(intent="catalog_search", route="agentic_response", reason="general shopping question")
