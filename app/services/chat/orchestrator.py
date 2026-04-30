from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from app.catalog.schemas import CatalogProduct
from app.models import ChatMessage, ChatSession, ChatToolCall
from app.services.auth.clerk import ChatIdentity
from app.services.chat.context import summarize_context
from app.services.chat.schemas import ChatAction, ChatRequest, ChatResponse, ChatToolTrace
from app.services.chat.tools import catalog_cards, customer_summary, product_detail, recommendation_cards, related_product_cards
from app.services.chat.triage import TriageDecision, triage_chat


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:24]}"


def _actions_for_cards(cards: list[CatalogProduct]) -> list[ChatAction]:
    return [
        ChatAction(type="view_product", label=f"View {card.title}", href=f"/product/{card.id}", product_id=card.id)
        for card in cards
    ]


def _persist_session(db: Session, req: ChatRequest, identity: ChatIdentity) -> ChatSession:
    now = datetime.now(timezone.utc)
    session = db.get(ChatSession, req.conversation_id) if req.conversation_id else None
    if not session:
        session = ChatSession(
            id=req.conversation_id or _id("chat"),
            customer_id=identity.customer_id,
            provider=identity.principal.provider if identity.principal else None,
            provider_user_id=identity.principal.provider_user_id if identity.principal else None,
            context_json=summarize_context(req.context),
            created_at=now,
            updated_at=now,
        )
        db.add(session)
        db.flush()
    else:
        session.customer_id = identity.customer_id
        session.provider = identity.principal.provider if identity.principal else session.provider
        session.provider_user_id = identity.principal.provider_user_id if identity.principal else session.provider_user_id
        session.context_json = summarize_context(req.context)
        session.updated_at = now
    return session


def _persist_message(db: Session, session_id: str, role: str, content: str, payload: dict | None = None) -> ChatMessage:
    message = ChatMessage(
        id=_id("msg"),
        session_id=session_id,
        role=role,
        content=content,
        payload_json=payload or {},
    )
    db.add(message)
    db.flush()
    return message


def _record_tool(db: Session, session_id: str, message_id: str | None, name: str, input_json: dict, cards: list[CatalogProduct]) -> None:
    db.add(
        ChatToolCall(
            id=_id("tool"),
            session_id=session_id,
            message_id=message_id,
            tool_name=name,
            input_json=input_json,
            output_json={"product_ids": [card.id for card in cards], "count": len(cards)},
        )
    )


def _blocked_response(req: ChatRequest, identity: ChatIdentity, session: ChatSession, decision: TriageDecision) -> ChatResponse:
    if identity.status == "authenticated_unlinked":
        message = "You are signed in, but I cannot link this login to a Sterling Hollis customer account yet."
        action = ChatAction(type="link_account", label="Link account", href="/account/link")
    else:
        message = "Please sign in before I look up account details or customer-specific recommendations."
        action = ChatAction(type="sign_in", label="Sign in", href="/sign-in")
    return ChatResponse(
        conversation_id=session.id,
        message=message,
        identity_status=identity.status,
        route="blocked",
        cards=[],
        actions=[action],
        tool_trace=[ChatToolTrace(name="triage", decision=decision.reason)],
    )


def _account_response(db: Session, identity: ChatIdentity, session: ChatSession, decision: TriageDecision) -> ChatResponse:
    assert identity.customer_id is not None
    summary = customer_summary(db, identity.customer_id)
    name = identity.customer.first_name if identity.customer else summary.get("first_name", "there")
    tier = summary.get("loyalty_tier", "standard")
    preferred = ", ".join(identity.customer.preferred_categories) if identity.customer else ""
    message = f"{name}, your account is linked. Your loyalty tier is {tier}."
    if preferred:
        message += f" Your strongest style signals are {preferred}."
    return ChatResponse(
        conversation_id=session.id,
        message=message,
        identity_status=identity.status,
        route=decision.route,
        cards=[],
        actions=[],
        tool_trace=[
            ChatToolTrace(name="triage", decision=decision.reason),
            ChatToolTrace(name="customer_summary", decision="resolved from backend-derived customer_id"),
        ],
    )


def _product_context_response(db: Session, req: ChatRequest, identity: ChatIdentity, session: ChatSession, decision: TriageDecision) -> ChatResponse:
    product = product_detail(db, req.context.product_id or "", store_id=req.context.store_id) if req.context.product_id else None
    if not product:
        cards = catalog_cards(db, category=req.context.category, store_id=req.context.store_id, query=req.message, limit=3)
        message = "I found a few products that fit that request."
        tool_name = "catalog_search"
    else:
        cards = [product]
        message = f"{product.title} is a {product.category_label.lower()} from {product.brand}, priced around ${product.price:,.2f}."
        if product.inventory_summary.availability == "in_stock":
            message += " It is currently in stock."
        tool_name = "product_detail"
    _record_tool(db, session.id, None, tool_name, summarize_context(req.context), cards)
    return ChatResponse(
        conversation_id=session.id,
        message=message,
        identity_status=identity.status,
        route=decision.route,
        cards=cards,
        actions=_actions_for_cards(cards),
        tool_trace=[ChatToolTrace(name="triage", decision=decision.reason), ChatToolTrace(name=tool_name, decision="direct catalog API call")],
    )


def _recommendation_response(db: Session, req: ChatRequest, identity: ChatIdentity, session: ChatSession, decision: TriageDecision) -> ChatResponse:
    cards: list[CatalogProduct] = []
    tool_name = "related_products"
    if identity.customer_id and req.context.store_id:
        try:
            cards = recommendation_cards(
                db,
                customer_id=identity.customer_id,
                store_id=req.context.store_id,
                category=req.context.category,
                limit=3,
            )
            tool_name = "customer_recommendations"
        except Exception:
            cards = []
    if not cards and req.context.product_id:
        cards = related_product_cards(db, req.context.product_id, limit=3)
        tool_name = "related_products"
    if not cards:
        cards = recommendation_cards(db, store_id=req.context.store_id, category=req.context.category, limit=3)
        tool_name = "catalog_recommendations"

    if identity.customer_id and tool_name == "customer_recommendations":
        message = "Based on your account and this shopping context, these are the best matches I would start with."
    elif req.context.product_id:
        message = "These pieces should pair well with the product you are viewing."
    else:
        message = "I found a few strong options from the catalog."
    _record_tool(db, session.id, None, tool_name, summarize_context(req.context), cards)
    return ChatResponse(
        conversation_id=session.id,
        message=message,
        identity_status=identity.status,
        route=decision.route,
        cards=cards,
        actions=_actions_for_cards(cards),
        tool_trace=[ChatToolTrace(name="triage", decision=decision.reason), ChatToolTrace(name=tool_name, decision="backend-derived customer_id" if identity.customer_id else "anonymous catalog context")],
    )


def handle_chat(db: Session, req: ChatRequest, identity: ChatIdentity) -> ChatResponse:
    decision = triage_chat(req.message, req.context)
    session = _persist_session(db, req, identity)
    _persist_message(db, session.id, "user", req.message, {"context": summarize_context(req.context)})

    if decision.intent == "account" and identity.status != "authenticated_customer":
        response = _blocked_response(req, identity, session, decision)
    elif decision.intent == "account":
        response = _account_response(db, identity, session, decision)
    elif decision.intent == "product_context":
        response = _product_context_response(db, req, identity, session, decision)
    else:
        if decision.intent == "customer_recommendation" and "my " in req.message.lower() and identity.status != "authenticated_customer":
            response = _blocked_response(req, identity, session, decision)
        else:
            response = _recommendation_response(db, req, identity, session, decision)

    assistant = _persist_message(
        db,
        session.id,
        "assistant",
        response.message,
        response.model_dump(mode="json"),
    )
    for call in db.query(ChatToolCall).filter(ChatToolCall.session_id == session.id, ChatToolCall.message_id.is_(None)).all():
        call.message_id = assistant.id
    db.commit()
    return response
