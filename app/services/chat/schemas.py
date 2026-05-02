from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.catalog.schemas import CatalogProduct


class ChatCurrentProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str | None = None
    category: str | None = None
    brand: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)


class ChatContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_type: str | None = None
    route: str | None = None
    product_id: str | None = None
    current_product: ChatCurrentProduct | None = None
    category: str | None = None
    store_id: str | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, max_length=64)
    context: ChatContext = Field(default_factory=ChatContext)


class ChatAction(BaseModel):
    type: Literal["view_product", "sign_in"]
    label: str
    href: str | None = None
    product_id: str | None = None


class ChatToolTrace(BaseModel):
    name: str
    decision: str


class ChatResponse(BaseModel):
    conversation_id: str
    message: str
    identity_status: Literal["anonymous", "authenticated_unlinked", "authenticated_customer"]
    intent: Literal[
        "catalog_search",
        "complementary_products",
        "product_question",
        "account_question",
        "customer_recommendation",
        "general_style",
    ]
    route: Literal["simple_tool", "agentic_response", "semantic_catalog_search", "blocked"]
    cards: list[CatalogProduct] = Field(default_factory=list)
    actions: list[ChatAction] = Field(default_factory=list)
    tool_trace: list[ChatToolTrace] = Field(default_factory=list)
    evaluator_confidence: float | None = None
    selected_agent: str | None = None
    selected_tool: str | None = None
    requires_followup: bool = False
    clarifying_question: str | None = None
