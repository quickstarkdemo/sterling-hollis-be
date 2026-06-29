from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.config import Settings
from app.services.catalog_assistant_tools import (
    CatalogAssistantToolResult,
    lookup_customer_purchases,
    search_catalog_products,
    summarize_inventory,
)
from app.services.catalog_voice_tools import CatalogVoiceCitation, CatalogVoiceToolResult

try:  # pragma: no cover - exercised when Strands is installed in runtime.
    from strands import tool as strands_tool
except Exception:  # pragma: no cover - local tests can run without provider extras.

    def strands_tool(func: Callable | None = None, **_kwargs):
        def decorator(inner: Callable) -> Callable:
            return inner

        return decorator(func) if func is not None else decorator


class CatalogAssistantAgentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    primary_tool: Literal[
        "search_catalog_products",
        "read_inventory_status",
        "lookup_customer_purchases",
        "catalog_assistant_agent",
    ] = "catalog_assistant_agent"
    citation_ids: list[str] = Field(default_factory=list)
    requires_followup: bool = False
    clarifying_question: str | None = None
    rationale: str = ""


CATALOG_STUDIO_ASSISTANT_PROMPT = """
You are CatalogStudioAssistantAgent for Sterling Hollis Catalog Studio.
You answer authenticated catalog-admin questions with only the provided read tools.
Use search_catalog_products for product, brand, category, lifecycle, and catalog status questions.
Use read_inventory_status for store, stock, assortment risk, markdown, low-stock, and inventory questions.
Use lookup_customer_purchases for customer, order, purchase, or bought questions.
Never invent products, stores, customers, orders, or inventory values.
Never expose phone numbers, email addresses, credentials, private prompts, or raw database rows.
If the tools return no evidence, say that no matching data was found or ask one concise clarification.
Return a concise answer and include citation_ids for the records used.
""".strip()


def _citation_model(payload: dict[str, Any]) -> CatalogVoiceCitation:
    return CatalogVoiceCitation.model_validate(payload)


def _tool_result_payload(result: CatalogAssistantToolResult) -> dict[str, Any]:
    return result.as_tool_payload()


def _coerce_agent_response(raw: Any) -> CatalogAssistantAgentResponse:
    if isinstance(raw, CatalogAssistantAgentResponse):
        return raw
    for attr in ("structured_output", "structured_response", "output", "result"):
        value = getattr(raw, attr, None)
        if isinstance(value, CatalogAssistantAgentResponse):
            return value
        if isinstance(value, dict):
            return CatalogAssistantAgentResponse.model_validate(value)
    if isinstance(raw, dict):
        return CatalogAssistantAgentResponse.model_validate(raw)
    if hasattr(raw, "model_dump"):
        return CatalogAssistantAgentResponse.model_validate(raw.model_dump())
    raise RuntimeError(
        f"CatalogStudioAssistantAgent returned unsupported result type: {type(raw).__name__}"
    )


def _question_mentions_customer(question: str) -> bool:
    normalized = question.casefold()
    return any(token in normalized for token in ("bought", "customer", "customers", "order", "orders", "purchase", "purchased"))


def _question_mentions_inventory(question: str) -> bool:
    normalized = question.casefold()
    return any(token in normalized for token in ("availability", "discount", "inventory", "low stock", "markdown", "risk", "sku", "status", "stock", "store", "stores", "unit"))


def _question_mentions_product_catalog(question: str) -> bool:
    normalized = question.casefold()
    return any(token in normalized for token in ("brand", "categories", "category", "lifecycle", "products")) and not any(
        token in normalized for token in ("discount", "inventory", "low stock", "markdown", "risk", "stock")
    )


class CatalogAssistantAgentService:
    def __init__(
        self,
        settings: Settings,
        *,
        invoker: Callable[[str, list[Callable[..., Any]]], Any] | None = None,
    ):
        self.settings = settings
        self._invoker = invoker

    def _build_tools(
        self,
        db: Session,
        captured: list[tuple[str, dict[str, Any], CatalogAssistantToolResult]],
    ) -> list[Callable[..., Any]]:
        def record(name: str, input_payload: dict[str, Any], result: CatalogAssistantToolResult) -> dict[str, Any]:
            captured.append((name, input_payload, result))
            return _tool_result_payload(result)

        @strands_tool(name="search_catalog_products")
        def search_tool(
            query: str | None = None,
            brand: str | None = None,
            category: str | None = None,
            lifecycle_status: str | None = None,
            limit: int = 6,
        ) -> dict[str, Any]:
            """Search real Catalog Studio products by title, brand, category, and lifecycle status."""
            input_payload = {
                "query": query,
                "brand": brand,
                "category": category,
                "lifecycle_status": lifecycle_status,
                "limit": limit,
            }
            return record(
                "search_catalog_products",
                input_payload,
                search_catalog_products(db, **input_payload),
            )

        @strands_tool(name="read_inventory_status")
        def inventory_tool(
            question: str | None = None,
            product_query: str | None = None,
            store_query: str | None = None,
            category: str | None = None,
            lifecycle_status: str | None = None,
            low_stock_only: bool | None = None,
            limit: int = 8,
        ) -> dict[str, Any]:
            """Read real store inventory, stock risk, and product-level inventory candidates."""
            input_payload = {
                "question": question,
                "product_query": product_query,
                "store_query": store_query,
                "category": category,
                "lifecycle_status": lifecycle_status,
                "low_stock_only": low_stock_only,
                "limit": limit,
            }
            return record(
                "read_inventory_status",
                input_payload,
                summarize_inventory(db, **input_payload),
            )

        @strands_tool(name="lookup_customer_purchases")
        def customer_purchase_tool(
            product_query: str,
            customer_query: str | None = None,
            store_query: str | None = None,
            limit: int = 8,
        ) -> dict[str, Any]:
            """Read real customer/order purchase evidence for a product query with PII-minimized citations."""
            input_payload = {
                "product_query": product_query,
                "customer_query": customer_query,
                "store_query": store_query,
                "limit": limit,
            }
            return record(
                "lookup_customer_purchases",
                input_payload,
                lookup_customer_purchases(db, **input_payload),
            )

        return [search_tool, inventory_tool, customer_purchase_tool]

    def _invoke_provider(self, prompt: str, tools: list[Callable[..., Any]]) -> CatalogAssistantAgentResponse:
        if self._invoker is not None:
            return _coerce_agent_response(self._invoker(prompt, tools))
        if not self.settings.openai_api_key:
            raise RuntimeError("OpenAI is not configured for Catalog Studio assistant orchestration.")
        try:
            from strands import Agent
            from strands.models.openai import OpenAIModel
        except Exception as exc:  # pragma: no cover - depends on runtime Strands install.
            raise RuntimeError("Strands is not available for Catalog Studio assistant orchestration.") from exc

        model = OpenAIModel(
            client_args={"api_key": self.settings.openai_api_key},
            model_id=self.settings.catalog_studio_responses_model,
        )
        agent = Agent(
            model=model,
            system_prompt=CATALOG_STUDIO_ASSISTANT_PROMPT,
            tools=tools,
            load_tools_from_directory=False,
            name="CatalogStudioAssistantAgent",
            description="Orchestrates read-only Catalog Studio data tools for admin questions.",
            structured_output_model=CatalogAssistantAgentResponse,
        )
        return _coerce_agent_response(agent(prompt))

    def _fallback(
        self,
        db: Session,
        *,
        question: str,
        query_scopes: list[Literal["catalog", "inventory"]] | None = None,
        reason: str,
    ) -> CatalogVoiceToolResult:
        scopes = query_scopes or ["catalog", "inventory"]
        if _question_mentions_customer(question):
            result = lookup_customer_purchases(db, product_query=question)
            selected_tool = "lookup_customer_purchases"
        elif _question_mentions_product_catalog(question):
            result = search_catalog_products(db, query=question)
            selected_tool = "search_catalog_products"
        elif "inventory" in scopes and _question_mentions_inventory(question):
            result = summarize_inventory(db, question=question)
            selected_tool = "read_inventory_status"
        else:
            result = search_catalog_products(db, query=question)
            selected_tool = "search_catalog_products"
        citations = [_citation_model(citation) for citation in result.citations]
        fallback_note = f"Catalog assistant fallback ({reason}): "
        return CatalogVoiceToolResult(
            message=f"{fallback_note}{result.message}",
            citations=citations,
            selected_agent="CatalogStudioAssistantFallback",
            selected_tool=selected_tool,
            agent_mode="fallback",
            fallback_reason=reason,
        )

    def answer(
        self,
        db: Session,
        *,
        question: str,
        query_scopes: list[Literal["catalog", "inventory"]] | None = None,
    ) -> CatalogVoiceToolResult:
        captured: list[tuple[str, dict[str, Any], CatalogAssistantToolResult]] = []
        tools = self._build_tools(db, captured)
        try:
            agent_response = self._invoke_provider(
                (
                    f"Question: {question}\n"
                    "Permitted catalog-admin read tools: search_catalog_products, "
                    "read_inventory_status, lookup_customer_purchases.\n"
                    "Use lookup_customer_purchases for customer, order, purchase, "
                    "or bought questions even when the legacy frontend query_scopes "
                    "only lists catalog and inventory.\n"
                    f"Frontend query_scopes: {', '.join(query_scopes or ['catalog', 'inventory'])}."
                ),
                tools,
            )
        except RuntimeError as exc:
            reason = "missing_provider_configuration" if not self.settings.openai_api_key else type(exc).__name__
            return self._fallback(
                db,
                question=question,
                query_scopes=query_scopes,
                reason=reason,
            )

        if not captured and not agent_response.requires_followup:
            return self._fallback(
                db,
                question=question,
                query_scopes=query_scopes,
                reason="agent_returned_no_tool_evidence",
            )

        all_citations = [
            citation
            for _, _, result in captured
            for citation in result.citations
        ]
        if agent_response.citation_ids:
            wanted = set(agent_response.citation_ids)
            selected_citations = [
                citation
                for citation in all_citations
                if str(citation.get("source_id")) in wanted
            ]
            if not selected_citations and all_citations:
                selected_citations = all_citations
        else:
            selected_citations = all_citations
        primary_tool = agent_response.primary_tool
        if primary_tool == "catalog_assistant_agent" and captured:
            primary_tool = captured[-1][0]  # type: ignore[assignment]
        message = agent_response.clarifying_question if agent_response.requires_followup and agent_response.clarifying_question else agent_response.message
        return CatalogVoiceToolResult(
            message=message,
            citations=[_citation_model(citation) for citation in selected_citations],
            selected_agent="CatalogStudioAssistantAgent",
            selected_tool=primary_tool,
            agent_mode="agent",
        )
