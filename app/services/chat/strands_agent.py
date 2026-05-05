from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings


class ShoppingAgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    intent: Literal[
        "catalog_search",
        "complementary_products",
        "product_question",
        "account_question",
        "customer_recommendation",
        "general_style",
    ]
    route: Literal["simple_tool", "agentic_response", "semantic_catalog_search"]
    primary_tool: str = "strands_agent"
    product_ids: list[str] = Field(default_factory=list)
    requires_followup: bool = False
    clarifying_question: str | None = None
    rationale: str = ""


STORE_FRONT_SHOPPING_PROMPT = """
You are StorefrontShoppingAgent for Sterling Hollis public storefront chat.
Use only the provided public tools. Never request or infer customer records, order status, private account details, credentials, or internal system data.
You may call multiple tools when needed: inspect the current product, search complementary products, compare related products, or fetch store contact info.
Return a concise shopper-facing answer and include product_ids for the products you want rendered as cards.
Do not include the current product id in product_ids for outfit, pairing, or complementary-product requests; it is the anchor item, not a recommendation.
For pairing/outfit requests, prefer semantic_catalog_search and avoid recommending the same category as the current product unless the shopper explicitly asks for similar items.
When building around an apparel anchor, do not suggest replacement garments. For example, if the current product is a skirt, dress, gown, pant, or trouser, recommend tops, layers, shoes, bags, or jewelry instead of another dress, skirt, pant, or trouser.
For explicitly similar requests, use find_related_products.
For store phone/address questions, use get_store_info.
""".strip()


def build_storefront_shopping_agent(tools: list[Callable[..., Any]]) -> Any:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OpenAI is not configured for Strands storefront shopping orchestration.")
    try:
        from strands import Agent
        from strands.models.openai import OpenAIModel
    except Exception as exc:  # pragma: no cover - depends on runtime Strands install.
        raise RuntimeError("Strands is not available for storefront shopping orchestration.") from exc

    model = OpenAIModel(
        client_args={"api_key": settings.openai_api_key},
        model_id=settings.chat_orchestration_model,
    )
    return Agent(
        model=model,
        system_prompt=STORE_FRONT_SHOPPING_PROMPT,
        tools=tools,
        load_tools_from_directory=False,
        name="StorefrontShoppingAgent",
        description="Orchestrates public product, styling, and store-info tools for storefront chat.",
        structured_output_model=ShoppingAgentResult,
    )


def coerce_shopping_agent_result(raw: Any) -> ShoppingAgentResult:
    if isinstance(raw, ShoppingAgentResult):
        return raw
    for attr in ("structured_output", "structured_response", "output", "result"):
        value = getattr(raw, attr, None)
        if isinstance(value, ShoppingAgentResult):
            return value
        if isinstance(value, dict):
            return ShoppingAgentResult.model_validate(value)
    if isinstance(raw, dict):
        return ShoppingAgentResult.model_validate(raw)
    if hasattr(raw, "model_dump"):
        return ShoppingAgentResult.model_validate(raw.model_dump())
    raise RuntimeError(f"Strands storefront shopping agent returned unsupported result type: {type(raw).__name__}")


def invoke_storefront_shopping_agent(prompt: str, tools: list[Callable[..., Any]]) -> ShoppingAgentResult:
    agent = build_storefront_shopping_agent(tools)
    return coerce_shopping_agent_result(agent(prompt))
