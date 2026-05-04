from __future__ import annotations

from dataclasses import dataclass, field
import re

from app.services.chat.evaluator import ChatOrchestrationDecision
from app.services.chat.schemas import ChatRequest
from app.services.chat.triage import TriageDecision, gender_targets_from_text


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
    (("shoe", "shoes", "heel", "heels", "pump", "pumps", "boot", "boots", "sandal", "sandals", "loafer", "loafers", "sneaker", "sneakers"), ("shoes",)),
    (("bag", "bags", "handbag", "handbags", "purse", "purses", "clutch", "clutches"), ("handbags",)),
    (("jewelry", "jewellery", "accessory", "accessories", "watch", "bracelet", "necklace", "ring"), ("jewelry_accessories",)),
    (("beauty", "serum", "makeup", "skincare", "fragrance", "perfume"), ("beauty",)),
    (("home", "decor", "chair", "vase", "dinnerware"), ("home",)),
)


@dataclass(frozen=True)
class ChatIntentFrame:
    intent: str
    route: str
    reason: str
    selected_agent: str
    selected_tool: str
    requires_auth: bool
    query: str | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    colors: list[str] = field(default_factory=list)
    materials: list[str] = field(default_factory=list)
    target_categories: list[str] = field(default_factory=list)
    exclude_categories: list[str] = field(default_factory=list)
    target_genders: list[str] = field(default_factory=list)
    current_product_id: str | None = None
    use_current_product: bool = False
    requires_followup: bool = False
    clarifying_question: str | None = None

    def trace_decision(self) -> str:
        return (
            f"intent={self.intent}, tool={self.selected_tool}, "
            f"target_categories={','.join(self.target_categories) or 'any'}, "
            f"target_genders={','.join(self.target_genders) or 'any'}, "
            f"query={self.query or 'none'}"
        )


def _normalized_text(value: str | None) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (value or "").lower().replace("&", " and ")).split())


def _append_unique(values: list[str], next_values: list[str] | tuple[str, ...]) -> None:
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


def normalize_categories(raw_categories: list[str], query: str | None = None) -> list[str]:
    categories: list[str] = []
    for category in raw_categories:
        normalized = _normalized_text(category).replace(" ", "_")
        if normalized in VALID_CATEGORIES:
            _append_unique(categories, [normalized])
        else:
            _append_unique(categories, _categories_for_text(category))
    _append_unique(categories, _categories_for_text(query))
    return categories


def normalize_gender(value: str | None) -> str | None:
    clean = _normalized_text(value)
    if clean in {"women", "woman", "womens", "women s", "female", "girls"}:
        return "women"
    if clean in {"men", "man", "mens", "men s", "male", "boys"}:
        return "men"
    if clean == "unisex":
        return "unisex"
    return None


def normalize_genders(values: list[str]) -> list[str]:
    genders: list[str] = []
    for value in values:
        normalized = normalize_gender(value)
        if normalized and normalized not in genders:
            genders.append(normalized)
    return genders


def _current_product_id(req: ChatRequest) -> str | None:
    return req.context.current_product.id if req.context.current_product else req.context.product_id


def _current_product_genders(req: ChatRequest) -> list[str]:
    if not req.context.current_product:
        return []
    return normalize_genders([req.context.current_product.attributes.get("gender", "")])


def _message_genders(message: str) -> list[str]:
    return normalize_genders(gender_targets_from_text(message))


def build_chat_intent_frame(req: ChatRequest, orchestration: ChatOrchestrationDecision) -> ChatIntentFrame:
    decision: TriageDecision = orchestration.decision
    constraints = decision.constraints
    query = constraints.query
    message_genders = _message_genders(req.message)
    target_genders = message_genders or normalize_genders(constraints.target_genders) or _current_product_genders(req)
    target_categories = normalize_categories(decision.target_categories, query)
    exclude_categories = normalize_categories(decision.exclude_categories)

    return ChatIntentFrame(
        intent=decision.intent,
        route=decision.route,
        reason=decision.reason,
        selected_agent=orchestration.selected_agent,
        selected_tool=orchestration.selected_tool,
        requires_auth=orchestration.requires_auth,
        query=query,
        budget_min=constraints.budget_min,
        budget_max=constraints.budget_max,
        colors=list(constraints.colors),
        materials=list(constraints.materials),
        target_categories=target_categories,
        exclude_categories=exclude_categories,
        target_genders=target_genders,
        current_product_id=_current_product_id(req),
        use_current_product=decision.use_current_product,
        requires_followup=orchestration.requires_followup,
        clarifying_question=orchestration.clarifying_question,
    )
