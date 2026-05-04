from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.config import get_settings


CHAT_AGENT_NAMES = {
    "product": "ProductAgent",
    "personal_shopper": "PersonalShopperAgent",
    "customer_service": "CustomerServiceAgent",
    "order": "OrderAgent",
}

CHAT_TOOL_NAMES = {
    "product_detail",
    "semantic_catalog_search",
    "related_products",
    "customer_recommendations",
    "customer_summary",
    "store_info",
    "service_answer",
    "order_status",
    "chat_response",
}

INTAKE_SYSTEM_PROMPT = """
You are ChatIntakeAgent for Sterling Hollis storefront chat.
Evaluate the shopper's latest message and return only the requested structured routing fields.

Use ProductAgent for product detail, product search, related products, outfit pairing, and attribute-based catalog requests.
Route "goes with", "wear with", "pair", "complement", "outfit", and styling requests to semantic_catalog_search.
Use related_products only for explicitly similar alternatives such as "more like this", "similar", "another", or "other colors".
Use PersonalShopperAgent for requests based on the shopper's account, style, size, preferences, or purchase history.
Use CustomerServiceAgent for store phone/contact information and general approved customer-service questions.
Use OrderAgent for order status, recent order, purchase, delivery, or return status tied to the shopper's account.

Security is not your job. Set requires_auth=true for account, order, personal, size, preference, or purchase-history requests.
Do not invent customer_id, order_id, or store_id values. Extract only explicit non-sensitive constraints from the message.
When the shopper says men's, women's, male, female, or unisex, include that as constraints.target_genders.
For list constraint fields, return an empty array [] when there are no values. Never return null for list fields.
If the request is ambiguous but answerable with a safe general response, choose the closest safe route.
""".strip()


SPECIALIZED_AGENT_PROMPTS = {
    "ProductAgent": "Route product discovery and product context requests to narrow catalog tools.",
    "PersonalShopperAgent": "Route authenticated personal shopping requests to customer recommendation tools.",
    "CustomerServiceAgent": "Route storefront support requests to approved service and store-info tools.",
    "OrderAgent": "Route authenticated order-status requests to backend order tools.",
}


def strands_available() -> bool:
    try:
        import strands  # noqa: F401
        from strands.models.openai import OpenAIModel  # noqa: F401
    except Exception:
        return False
    return True


@lru_cache(maxsize=8)
def build_chat_intake_agent(model_id: str | None = None) -> Any:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OpenAI is not configured for Strands chat orchestration.")
    try:
        from strands import Agent
        from strands.models.openai import OpenAIModel
    except Exception as exc:
        raise RuntimeError("Strands is not available for chat orchestration.") from exc

    model = OpenAIModel(
        client_args={"api_key": settings.openai_api_key},
        model_id=model_id or settings.chat_orchestration_model,
    )
    return Agent(
        model=model,
        system_prompt=INTAKE_SYSTEM_PROMPT,
        tools=[],
        load_tools_from_directory=False,
        name="ChatIntakeAgent",
        description="Evaluates a storefront chat message and chooses the specialized agent/tool route.",
    )


def specialized_agent_prompt(agent_name: str) -> str:
    return SPECIALIZED_AGENT_PROMPTS.get(agent_name, "Route the request to an approved backend chat tool.")
