from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.catalog.schemas import CatalogProduct


class ChatContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_type: str | None = None
    route: str | None = None
    product_id: str | None = None
    category: str | None = None
    store_id: str | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, max_length=64)
    context: ChatContext = Field(default_factory=ChatContext)


class ChatAction(BaseModel):
    type: Literal["view_product", "sign_in", "link_account"]
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
    route: Literal["simple_tool", "agentic_response", "pinecone_lookup", "blocked"]
    cards: list[CatalogProduct] = Field(default_factory=list)
    actions: list[ChatAction] = Field(default_factory=list)
    tool_trace: list[ChatToolTrace] = Field(default_factory=list)
