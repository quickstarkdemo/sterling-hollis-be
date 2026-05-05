from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.catalog.schemas import CatalogProduct
from app.models import ChatSession, ChatToolCall
from app.services.auth.clerk import ChatIdentity
from app.services.chat.intent_frame import ChatIntentFrame
from app.services.chat.schemas import ChatAction, ChatRequest, ChatResponse, ChatToolTrace
from app.services.chat.strands_agent import ShoppingAgentResult, invoke_storefront_shopping_agent
from app.services.chat.strands_tools import build_storefront_tools
from app.services.chat.triage import TriageDecision


@dataclass
class CapturedToolCall:
    name: str
    input_json: dict[str, Any]
    output_json: dict[str, Any]


@dataclass
class StrandsRunResult:
    response: ChatResponse | None = None
    tool_calls: list[CapturedToolCall] = field(default_factory=list)
    error: str | None = None


def _actions_for_cards(cards: list[CatalogProduct]) -> list[ChatAction]:
    return [
        ChatAction(type="view_product", label=f"View {card.title}", href=f"/product/{card.id}", product_id=card.id)
        for card in cards
    ]


def _history_prompt(history: list[dict[str, str]]) -> str:
    lines = [
        f"- {turn.get('role', 'unknown')}: {turn.get('content', '')}"
        for turn in history[-8:]
        if turn.get("content")
    ]
    return "\n".join(lines) or "- (none)"


def _prompt(req: ChatRequest, frame: ChatIntentFrame, history: list[dict[str, str]]) -> str:
    return (
        "Handle this public storefront chat turn using the available tools when useful.\n"
        f"Recent conversation turns:\n{_history_prompt(history)}\n"
        f"Message: {req.message}\n"
        f"Current product id: {frame.current_product_id or 'none'}\n"
        f"Intent: {frame.intent}\n"
        f"Route: {frame.route}\n"
        f"Query: {frame.query or 'none'}\n"
        f"Target categories: {frame.target_categories or []}\n"
        f"Excluded categories: {frame.exclude_categories or []}\n"
        f"Target genders: {frame.target_genders or []}\n"
        f"Budget max: {frame.budget_max or 'none'}\n"
        f"Store id: {req.context.store_id or 'none'}\n"
        "Return the structured result only."
    )


def _cards_from_tool_calls(tool_calls: list[CapturedToolCall], result: ShoppingAgentResult) -> list[CatalogProduct]:
    card_map: dict[str, CatalogProduct] = {}
    ordered_cards: list[CatalogProduct] = []
    for call in tool_calls:
        output = call.output_json
        raw_cards = list(output.get("cards") or [])
        raw_card = output.get("card")
        if raw_card:
            raw_cards.append(raw_card)
        for raw in raw_cards:
            try:
                card = CatalogProduct.model_validate(raw)
            except Exception:
                continue
            if card.id not in card_map:
                card_map[card.id] = card
                ordered_cards.append(card)

    selected: list[CatalogProduct] = []
    for product_id in result.product_ids:
        card = card_map.get(product_id)
        if card and card.id not in {item.id for item in selected}:
            selected.append(card)
    if selected:
        return selected[:3]
    return ordered_cards[:3]


def run_storefront_shopping_agent(
    db: Session,
    *,
    req: ChatRequest,
    identity: ChatIdentity,
    session: ChatSession,
    decision: TriageDecision,
    frame: ChatIntentFrame,
    history: list[dict[str, str]],
) -> StrandsRunResult:
    captured: list[CapturedToolCall] = []

    def record_tool_call(name: str, input_json: dict[str, Any], output_json: dict[str, Any]) -> None:
        captured.append(CapturedToolCall(name=name, input_json=input_json, output_json=output_json))

    try:
        tools = build_storefront_tools(db, req, frame, record_tool_call)
        result = invoke_storefront_shopping_agent(_prompt(req, frame, history), tools)
        cards = _cards_from_tool_calls(captured, result)
        primary_tool = result.primary_tool or "strands_agent"
        if primary_tool == "strands_agent" and len(captured) == 1:
            primary_tool = captured[0].name
        elif len(captured) > 1:
            primary_tool = "strands_agent"
        response = ChatResponse(
            conversation_id=session.id,
            message=result.message,
            identity_status=identity.status,
            intent=result.intent or decision.intent,
            route=result.route or decision.route,
            cards=cards,
            actions=_actions_for_cards(cards),
            tool_trace=[
                ChatToolTrace(name="StrandsAgent", decision=result.rationale or "completed storefront shopping orchestration"),
                *[
                    ChatToolTrace(
                        name=call.name,
                        decision=f"product_ids={','.join(call.output_json.get('product_ids') or []) or 'none'}",
                    )
                    for call in captured
                ],
            ],
            selected_agent="StorefrontShoppingAgent",
            selected_tool=primary_tool,
            requires_followup=result.requires_followup,
            clarifying_question=result.clarifying_question,
        )
        return StrandsRunResult(response=response, tool_calls=captured)
    except Exception as exc:
        return StrandsRunResult(tool_calls=captured, error=type(exc).__name__)


def persist_strands_tool_calls(db: Session, *, session_id: str, message_id: str, tool_calls: list[CapturedToolCall], make_id) -> None:
    for call in tool_calls:
        db.add(
            ChatToolCall(
                id=make_id("tool"),
                session_id=session_id,
                message_id=message_id,
                tool_name=call.name,
                input_json=call.input_json,
                output_json=call.output_json,
            )
        )
