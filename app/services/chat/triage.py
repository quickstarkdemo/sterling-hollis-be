from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from app.services.chat.schemas import ChatContext


ChatIntent = Literal[
    "catalog_search",
    "complementary_products",
    "product_question",
    "account_question",
    "customer_recommendation",
    "general_style",
]
ChatRoute = Literal["simple_tool", "agentic_response", "semantic_catalog_search", "blocked"]


@dataclass(frozen=True)
class SearchConstraints:
    query: str | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    colors: list[str] = field(default_factory=list)
    materials: list[str] = field(default_factory=list)
    target_genders: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TriageDecision:
    intent: ChatIntent
    route: ChatRoute
    reason: str
    requires_customer: bool = False
    target_categories: list[str] = field(default_factory=list)
    exclude_categories: list[str] = field(default_factory=list)
    constraints: SearchConstraints = field(default_factory=SearchConstraints)
    use_current_product: bool = False
    tool: str = "semantic_catalog_search"


ACCOUNT_TERMS = {
    "account",
    "profile",
    "loyalty",
    "points",
    "my size",
    "my sizes",
    "my info",
    "my information",
}

ORDER_TERMS = {
    "order",
    "orders",
    "order status",
    "purchase",
    "purchases",
    "delivery",
    "delivered",
    "shipment",
    "shipping status",
    "return status",
}

STORE_INFO_TERMS = {
    "phone",
    "phone number",
    "call",
    "contact",
    "store address",
    "address",
    "where are you",
    "location",
}

SERVICE_TERMS = {
    "return policy",
    "returns",
    "shipping",
    "ship",
    "exchange",
    "exchanges",
    "customer service",
    "support",
    "hours",
}

CUSTOMER_TERMS = {
    "for me",
    "my style",
    "my preferences",
    "my usual",
    "based on my",
}

PAIRING_TERMS = {
    "go with",
    "goes with",
    "pair",
    "pairs",
    "pairing",
    "match",
    "matches",
    "matching",
    "complement",
    "wear with",
    "style with",
    "around this",
}

OUTFIT_TERMS = {
    "outfit",
    "outfits",
    "look",
    "looks",
    "ensemble",
    "ensembles",
}

SEARCH_TERMS = {
    "provide",
    "find",
    "search",
    "show me",
    "looking for",
    "look for",
    "do you have",
    "have any",
    "need",
}

PRODUCT_QUESTION_TERMS = {
    "available",
    "availability",
    "in stock",
    "stock",
    "inventory",
    "size",
    "sizes",
    "price",
    "cost",
    "material",
    "color",
}

PRODUCT_TERMS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("blouse", "blouses", "top", "tops", "shirt", "shirts", "dress", "dresses", "skirt", "skirts", "pants", "trousers", "coat", "coats", "jacket", "jackets", "blazer", "blazers", "sweater", "sweaters", "cardigan", "cardigans", "jeans"), "womens_apparel", "apparel"),
    (("moisturizer", "moisturizers", "serum", "serums", "palette", "palettes", "lip color", "lipstick", "fragrance", "fragrances", "perfume", "perfumes", "skincare", "makeup"), "beauty", "beauty"),
    (("purse", "purses", "handbag", "handbags", "bag", "bags", "tote", "totes", "clutch", "clutches"), "handbags", "handbag"),
    (("shoe", "shoes", "heel", "heels", "pump", "pumps", "boot", "boots", "sandal", "sandals", "sneaker", "sneakers"), "shoes", "shoes"),
    (("earring", "earrings", "necklace", "necklaces", "bracelet", "bracelets", "ring", "rings", "jewelry", "accessories"), "jewelry_accessories", "accessories"),
    (("dinnerware", "chair", "chairs", "vase", "vases", "home", "decor"), "home", "home"),
)

COMPLEMENTARY_CATEGORIES = {
    "handbags": ["womens_apparel", "shoes", "jewelry_accessories"],
    "shoes": ["womens_apparel", "handbags", "jewelry_accessories"],
    "jewelry_accessories": ["womens_apparel", "shoes", "handbags"],
    "womens_apparel": ["shoes", "handbags", "jewelry_accessories"],
    "mens_apparel": ["shoes", "jewelry_accessories"],
    "beauty": ["jewelry_accessories", "womens_apparel"],
}

COLORS = {
    "black",
    "blue",
    "burgundy",
    "camel",
    "chocolate",
    "gold",
    "green",
    "ivory",
    "navy",
    "rose",
    "sage",
    "silver",
    "white",
}

GREETING_TERMS = {"hi", "hello", "hey", "good morning", "good afternoon", "good evening"}


def _normalized(message: str) -> str:
    return " ".join(re.sub(r"[^\w\s$.]", " ", message.lower()).split())


def _contains_any(message: str, terms: set[str]) -> bool:
    return any(term in message for term in terms)


def _budget_max(message: str) -> float | None:
    tokens = message.replace("$", " $").replace(",", "").split()
    for idx, token in enumerate(tokens):
        if token in {"under", "below", "max", "maximum"} and idx + 1 < len(tokens):
            raw = tokens[idx + 1].lstrip("$")
            try:
                return float(raw)
            except ValueError:
                continue
        if token == "less" and idx + 2 < len(tokens) and tokens[idx + 1] == "than":
            raw = tokens[idx + 2].lstrip("$")
            try:
                return float(raw)
            except ValueError:
                continue
    return None


def _current_product_category(context: ChatContext) -> str | None:
    return context.current_product.category if context.current_product and context.current_product.category else context.category


def _current_product_attributes(context: ChatContext) -> dict[str, str]:
    return context.current_product.attributes if context.current_product else {}


def _product_term(message: str) -> tuple[str, str] | None:
    for terms, category, _label in PRODUCT_TERMS:
        for term in terms:
            if f" {term} " in f" {message} ":
                query = term[:-1] if term.endswith("s") and not term.endswith("ss") else term
                return query, category
    return None


def _colors(message: str, context: ChatContext, *, include_current: bool) -> list[str]:
    found = [color for color in sorted(COLORS) if f" {color} " in f" {message} "]
    current_color = _current_product_attributes(context).get("color")
    if include_current and current_color and current_color.lower() not in found:
        found.insert(0, current_color.lower())
    return found


def gender_targets_from_text(message: str) -> list[str]:
    normalized = _normalized(message)
    targets: list[str] = []
    if any(term in f" {normalized} " for term in [" men ", " mens ", " men s ", " male ", " boys "]):
        targets.append("men")
    if any(term in f" {normalized} " for term in [" women ", " womens ", " women s ", " female ", " girls "]):
        targets.append("women")
    if " unisex " in f" {normalized} ":
        targets.append("unisex")
    return targets


def _current_product_gender(context: ChatContext) -> str | None:
    gender = _current_product_attributes(context).get("gender")
    return gender.lower() if gender else None


def _outfit_categories(context: ChatContext, current_category: str | None) -> list[str]:
    gender = _current_product_gender(context)
    apparel_category = "mens_apparel" if gender in {"men", "mens", "male"} else "womens_apparel"
    if apparel_category == "mens_apparel":
        categories = [apparel_category, "shoes"]
        if current_category == apparel_category:
            return categories
        return [category for category in categories if category != current_category]
    categories = [apparel_category, "shoes", "handbags", "jewelry_accessories"]
    if current_category == apparel_category:
        return categories
    return [category for category in categories if category != current_category]


def _outfit_exclude_categories(context: ChatContext, current_category: str | None) -> list[str]:
    gender = _current_product_gender(context)
    apparel_category = "mens_apparel" if gender in {"men", "mens", "male"} else "womens_apparel"
    if not current_category or current_category == apparel_category:
        return []
    return [current_category]


def _complementary_categories(context: ChatContext, current_category: str) -> list[str]:
    gender = _current_product_gender(context)
    categories = COMPLEMENTARY_CATEGORIES.get(current_category, [])
    if current_category == "shoes" and gender in {"men", "mens", "male"}:
        categories = ["mens_apparel", "jewelry_accessories"]
    return [category for category in categories if category != current_category]


def _semantic_decision(
    *,
    intent: ChatIntent,
    reason: str,
    query: str | None,
    budget_max: float | None,
    colors: list[str],
    target_categories: list[str],
    target_genders: list[str] | None = None,
    exclude_categories: list[str] | None = None,
    use_current_product: bool = False,
) -> TriageDecision:
    return TriageDecision(
        intent=intent,
        route="semantic_catalog_search",
        reason=reason,
        target_categories=target_categories,
        exclude_categories=exclude_categories or [],
        constraints=SearchConstraints(
            query=query,
            budget_max=budget_max,
            colors=colors,
            target_genders=target_genders or [],
        ),
        use_current_product=use_current_product,
        tool="semantic_catalog_search",
    )


def triage_chat(message: str, context: ChatContext) -> TriageDecision:
    normalized = _normalized(message)
    current_category = _current_product_category(context)
    current_product_id = context.current_product.id if context.current_product else context.product_id
    product_term = _product_term(normalized)
    pairing = _contains_any(normalized, PAIRING_TERMS) or (
        bool(current_product_id) and _contains_any(normalized, OUTFIT_TERMS)
    )
    search = _contains_any(normalized, SEARCH_TERMS)
    budget_max = _budget_max(normalized)
    target_genders = gender_targets_from_text(normalized)

    if normalized in GREETING_TERMS:
        return TriageDecision(intent="general_style", route="agentic_response", reason="greeting", tool="chat_response")

    if _contains_any(normalized, ORDER_TERMS):
        return TriageDecision(
            intent="account_question",
            route="simple_tool",
            reason="order-status wording",
            requires_customer=True,
            tool="order_status",
        )

    if _contains_any(normalized, STORE_INFO_TERMS):
        return TriageDecision(intent="general_style", route="simple_tool", reason="store-info wording", tool="store_info")

    if _contains_any(normalized, SERVICE_TERMS):
        return TriageDecision(intent="general_style", route="simple_tool", reason="customer-service wording", tool="service_answer")

    if _contains_any(normalized, ACCOUNT_TERMS):
        return TriageDecision(intent="account_question", route="simple_tool", reason="account-specific wording", tool="customer_summary")

    if _contains_any(normalized, CUSTOMER_TERMS):
        return TriageDecision(
            intent="customer_recommendation",
            route="simple_tool",
            reason="customer-specific recommendation wording",
            requires_customer=True,
            target_categories=[current_category] if current_category else [],
            tool="customer_recommendations",
        )

    if pairing and not current_product_id:
        return TriageDecision(
            intent="general_style",
            route="agentic_response",
            reason="ambiguous pairing request",
            tool="chat_response",
        )

    if not current_product_id and (_contains_any(normalized, PRODUCT_QUESTION_TERMS) or "this" in normalized or "it" in normalized):
        return TriageDecision(
            intent="general_style",
            route="agentic_response",
            reason="ambiguous product question",
            tool="chat_response",
        )

    if product_term and (search or pairing or budget_max is not None):
        query, category = product_term
        colors = _colors(normalized, context, include_current=pairing)
        return _semantic_decision(
            intent="catalog_search",
            reason="explicit product search with pairing context" if pairing else "explicit product search",
            query=query,
            budget_max=budget_max,
            colors=colors,
            target_categories=[category],
            target_genders=target_genders,
            exclude_categories=[current_category] if pairing and current_category else [],
            use_current_product=pairing,
        )

    if pairing and current_category:
        outfit_pairing = _contains_any(normalized, OUTFIT_TERMS)
        target_categories = (
            _outfit_categories(context, current_category)
            if outfit_pairing
            else _complementary_categories(context, current_category)
        )
        colors = _colors(normalized, context, include_current=not outfit_pairing)
        return _semantic_decision(
            intent="complementary_products",
            reason="outfit pairing request" if outfit_pairing else "complementary product request",
            query=None if outfit_pairing else " ".join([*colors, "complementary pieces"]).strip() or None,
            budget_max=budget_max,
            colors=colors,
            target_categories=target_categories,
            target_genders=target_genders,
            exclude_categories=(
                _outfit_exclude_categories(context, current_category)
                if outfit_pairing
                else [current_category]
            ),
            use_current_product=bool(current_product_id),
        )

    if current_product_id and (_contains_any(normalized, PRODUCT_QUESTION_TERMS) or "this" in normalized or "it" in normalized):
        return TriageDecision(intent="product_question", route="simple_tool", reason="current product question", use_current_product=True, tool="product_detail")

    if product_term:
        query, category = product_term
        colors = _colors(normalized, context, include_current=False)
        return _semantic_decision(
            intent="catalog_search",
            reason="catalog product term",
            query=query,
            budget_max=budget_max,
            colors=colors,
            target_categories=[category],
            target_genders=target_genders,
        )

    if search or context.category:
        return _semantic_decision(
            intent="catalog_search",
            reason="catalog/category search",
            query=normalized,
            budget_max=budget_max,
            colors=_colors(normalized, context, include_current=False),
            target_categories=[context.category] if context.category else [],
            target_genders=target_genders,
        )

    return TriageDecision(intent="general_style", route="agentic_response", reason="general shopping question", tool="catalog_recommendations")
