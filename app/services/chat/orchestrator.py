from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from app.catalog.schemas import CatalogProduct
from app.models import ChatMessage, ChatSession, ChatToolCall
from app.services.auth.clerk import ChatIdentity
from app.services.chat.context import summarize_context
from app.services.chat.evaluator import ChatOrchestrationDecision, evaluate_chat
from app.services.chat.schemas import ChatAction, ChatCurrentProduct, ChatRequest, ChatResponse, ChatToolTrace
from app.services.chat.tools import (
    catalog_cards,
    customer_summary,
    order_status,
    product_detail,
    recommendation_cards,
    semantic_catalog_cards,
    service_answer,
    store_scoped_related_product_cards,
    store_info,
)
from app.services.chat.triage import TriageDecision, triage_chat


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:24]}"


def _actions_for_cards(cards: list[CatalogProduct]) -> list[ChatAction]:
    return [
        ChatAction(type="view_product", label=f"View {card.title}", href=f"/product/{card.id}", product_id=card.id)
        for card in cards
    ]


AUTH_REQUIRED_TOOLS = {"customer_recommendations", "customer_summary", "order_status"}


def _current_product_id(req: ChatRequest) -> str | None:
    return req.context.current_product.id if req.context.current_product else req.context.product_id


def _normalize_context(db: Session, req: ChatRequest) -> ChatRequest:
    product_id = _current_product_id(req)
    if not product_id:
        return req
    product = product_detail(db, product_id, store_id=req.context.store_id)
    if not product:
        return req
    context = req.context.model_copy(
        update={
            "product_id": product.id,
            "category": req.context.category or product.category,
            "current_product": ChatCurrentProduct(
                id=product.id,
                title=product.title,
                category=product.category,
                brand=product.brand,
                attributes=product.attributes,
            ),
        }
    )
    return req.model_copy(update={"context": context})


def _session_matches_identity(session: ChatSession, identity: ChatIdentity) -> bool:
    if session.provider_user_id:
        return bool(identity.principal and session.provider_user_id == identity.principal.provider_user_id)
    if session.customer_id:
        return session.customer_id == identity.customer_id
    return identity.status == "anonymous"


def _persist_session(db: Session, req: ChatRequest, identity: ChatIdentity) -> ChatSession:
    now = datetime.now(timezone.utc)
    session = db.get(ChatSession, req.conversation_id) if req.conversation_id else None
    requested_id_allowed = bool(req.conversation_id)
    if session and not _session_matches_identity(session, identity):
        session = None
        requested_id_allowed = False
    if not session:
        session = ChatSession(
            id=req.conversation_id if requested_id_allowed else _id("chat"),
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


def _recent_history(db: Session, session_id: str, *, limit: int = 8) -> list[dict[str, str]]:
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
        .all()
    )
    return [{"role": row.role, "content": row.content} for row in reversed(rows)]


def _record_tool(db: Session, session_id: str, message_id: str | None, name: str, input_json: dict, cards: list[CatalogProduct]) -> None:
    _record_tool_output(
        db,
        session_id,
        message_id,
        name,
        input_json,
        {"product_ids": [card.id for card in cards], "count": len(cards)},
    )


def _record_tool_output(
    db: Session,
    session_id: str,
    message_id: str | None,
    name: str,
    input_json: dict,
    output_json: dict,
) -> None:
    db.add(
        ChatToolCall(
            id=_id("tool"),
            session_id=session_id,
            message_id=message_id,
            tool_name=name,
            input_json=input_json,
            output_json=output_json,
        )
    )


def _requires_auth(decision: TriageDecision, orchestration: ChatOrchestrationDecision) -> bool:
    return bool(
        decision.requires_customer
        or orchestration.requires_auth
        or orchestration.selected_tool in AUTH_REQUIRED_TOOLS
    )


def _apply_orchestration_trace(
    response: ChatResponse,
    orchestration: ChatOrchestrationDecision,
    *,
    auth_decision: str,
) -> ChatResponse:
    response.evaluator_confidence = round(orchestration.evaluator_confidence, 4)
    response.selected_agent = orchestration.selected_agent
    response.selected_tool = orchestration.selected_tool
    response.requires_followup = orchestration.requires_followup
    response.clarifying_question = orchestration.clarifying_question
    response.tool_trace.extend(
        [
            ChatToolTrace(
                name="ChatIntakeAgent",
                decision=f"{orchestration.evaluator_source}; confidence={orchestration.evaluator_confidence:.2f}",
            ),
            ChatToolTrace(name=orchestration.selected_agent, decision=f"selected_tool={orchestration.selected_tool}"),
            ChatToolTrace(name="auth_gate", decision=auth_decision),
        ]
    )
    if orchestration.evaluator_error:
        response.tool_trace.append(ChatToolTrace(name="evaluator_error", decision=orchestration.evaluator_error))
    return response


def _blocked_response(req: ChatRequest, identity: ChatIdentity, session: ChatSession, decision: TriageDecision) -> ChatResponse:
    if identity.status == "authenticated_unlinked":
        message = (
            "You are signed in, but I cannot link this login to a Sterling Hollis customer account yet. "
            "Use the same email address as your Sterling Hollis customer profile."
        )
        actions = []
    else:
        message = "Please sign in before I look up account details or customer-specific recommendations."
        actions = [ChatAction(type="sign_in", label="Sign in", href="/sign-in")]
    return ChatResponse(
        conversation_id=session.id,
        message=message,
        identity_status=identity.status,
        route="blocked",
        intent=decision.intent,
        cards=[],
        actions=actions,
        tool_trace=[ChatToolTrace(name="triage", decision=decision.reason)],
    )


def _followup_response(req: ChatRequest, identity: ChatIdentity, session: ChatSession, decision: TriageDecision, question: str) -> ChatResponse:
    return ChatResponse(
        conversation_id=session.id,
        message=question,
        identity_status=identity.status,
        route="agentic_response",
        intent=decision.intent,
        cards=[],
        actions=[],
        tool_trace=[ChatToolTrace(name="triage", decision=decision.reason)],
        requires_followup=True,
        clarifying_question=question,
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
        intent=decision.intent,
        cards=[],
        actions=[],
        tool_trace=[
            ChatToolTrace(name="triage", decision=decision.reason),
            ChatToolTrace(name="customer_summary", decision="resolved from backend-derived customer_id"),
        ],
    )


def _store_info_response(db: Session, req: ChatRequest, identity: ChatIdentity, session: ChatSession, decision: TriageDecision) -> ChatResponse:
    info = store_info(db, store_id=req.context.store_id)
    if info.get("found"):
        phone = info.get("phone") or "the store phone number is not listed"
        message = f"{info['name']} is at {info['address']}. You can call {phone}."
    else:
        message = "I could not find store contact details."
    _record_tool_output(db, session.id, None, "store_info", summarize_context(req.context), info)
    return ChatResponse(
        conversation_id=session.id,
        message=message,
        identity_status=identity.status,
        route=decision.route,
        intent=decision.intent,
        cards=[],
        actions=[],
        tool_trace=[
            ChatToolTrace(name="triage", decision=decision.reason),
            ChatToolTrace(name="store_info", decision="resolved from backend store context"),
        ],
    )


def _service_answer_response(db: Session, req: ChatRequest, identity: ChatIdentity, session: ChatSession, decision: TriageDecision) -> ChatResponse:
    answer = service_answer(req.message)
    _record_tool_output(db, session.id, None, "service_answer", {"message": req.message}, answer)
    return ChatResponse(
        conversation_id=session.id,
        message=str(answer.get("answer") or "I can help with products, stores, returns, shipping, and signed-in order status."),
        identity_status=identity.status,
        route=decision.route,
        intent=decision.intent,
        cards=[],
        actions=[],
        tool_trace=[
            ChatToolTrace(name="triage", decision=decision.reason),
            ChatToolTrace(name="service_answer", decision=f"approved_topic={answer.get('topic', 'customer_service')}"),
        ],
    )


def _order_status_response(db: Session, req: ChatRequest, identity: ChatIdentity, session: ChatSession, decision: TriageDecision) -> ChatResponse:
    assert identity.customer_id is not None
    status = order_status(db, identity.customer_id, req.message, limit=1)
    orders = status.get("orders") or []
    if not status.get("found") or not orders:
        message = "I could not find an order for your linked account."
    else:
        order = orders[0]
        item_count = sum(int(item.get("quantity") or 0) for item in order.get("items", []))
        item_label = "item" if item_count == 1 else "items"
        returned = " It is marked returned." if order.get("returned") else ""
        message = (
            f"Your latest order {order['id']} is {order['status']} from {order['ordered_at'][:10]}, "
            f"with {item_count} {item_label} totaling ${float(order['total_amount']):,.2f}.{returned}"
        )
    _record_tool_output(db, session.id, None, "order_status", {"message": req.message}, status)
    return ChatResponse(
        conversation_id=session.id,
        message=message,
        identity_status=identity.status,
        route=decision.route,
        intent=decision.intent,
        cards=[],
        actions=[],
        tool_trace=[
            ChatToolTrace(name="triage", decision=decision.reason),
            ChatToolTrace(name="order_status", decision="resolved from backend-derived customer_id"),
        ],
    )


def _product_context_response(db: Session, req: ChatRequest, identity: ChatIdentity, session: ChatSession, decision: TriageDecision) -> ChatResponse:
    current_product_id = _current_product_id(req)
    product = product_detail(db, current_product_id or "", store_id=req.context.store_id) if current_product_id else None
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
        intent=decision.intent,
        cards=cards,
        actions=_actions_for_cards(cards),
        tool_trace=[ChatToolTrace(name="triage", decision=decision.reason), ChatToolTrace(name=tool_name, decision="direct catalog API call")],
    )


def _related_products_response(db: Session, req: ChatRequest, identity: ChatIdentity, session: ChatSession, decision: TriageDecision) -> ChatResponse:
    current_product_id = _current_product_id(req)
    cards = (
        store_scoped_related_product_cards(db, current_product_id, store_id=req.context.store_id, limit=3)
        if current_product_id
        else []
    )
    if cards:
        message = "I found related products from the catalog."
    else:
        message = "I could not find related products for that item."
    _record_tool(db, session.id, None, "related_products", summarize_context(req.context), cards)
    return ChatResponse(
        conversation_id=session.id,
        message=message,
        identity_status=identity.status,
        route=decision.route,
        intent=decision.intent,
        cards=cards,
        actions=_actions_for_cards(cards),
        tool_trace=[
            ChatToolTrace(name="triage", decision=decision.reason),
            ChatToolTrace(name="related_products", decision="direct related-products API call"),
        ],
    )


def _semantic_catalog_response(db: Session, req: ChatRequest, identity: ChatIdentity, session: ChatSession, decision: TriageDecision) -> ChatResponse:
    current_product_id = _current_product_id(req)
    cards, strategy = semantic_catalog_cards(
        db,
        query=decision.constraints.query,
        target_categories=decision.target_categories,
        exclude_categories=decision.exclude_categories,
        budget_max=decision.constraints.budget_max,
        colors=decision.constraints.colors,
        current_product_id=current_product_id,
        store_id=req.context.store_id,
        limit=3,
    )
    if cards and decision.intent == "complementary_products":
        current_label = "item"
        if req.context.current_product and req.context.current_product.title:
            current_label = req.context.current_product.title
        message = f"I found options that should pair well with {current_label}."
    elif cards:
        message = "I found a few products that fit that request."
    else:
        message = "I could not find matching catalog products for that request."

    trace_decision = (
        f"target_category={','.join(decision.target_categories) or 'any'}, "
        f"budget_max={decision.constraints.budget_max or 'none'}, "
        f"exclude_category={','.join(decision.exclude_categories) or 'none'}, "
        f"strategy={strategy}"
    )
    _record_tool(
        db,
        session.id,
        None,
        "semantic_catalog_search",
        {
            "query": decision.constraints.query,
            "target_categories": decision.target_categories,
            "exclude_categories": decision.exclude_categories,
            "budget_max": decision.constraints.budget_max,
            "strategy": strategy,
        },
        cards,
    )
    return ChatResponse(
        conversation_id=session.id,
        message=message,
        identity_status=identity.status,
        intent=decision.intent,
        route="semantic_catalog_search",
        cards=cards,
        actions=_actions_for_cards(cards),
        tool_trace=[
            ChatToolTrace(name="triage", decision=decision.reason),
            ChatToolTrace(name="semantic_catalog_search", decision=trace_decision),
        ],
    )


def _general_response(req: ChatRequest, identity: ChatIdentity, session: ChatSession, decision: TriageDecision) -> ChatResponse:
    if decision.reason == "greeting":
        message = "Hello. I can help find products, compare options, or answer questions about this item."
    else:
        message = "I can help find products, compare options, or answer questions about this item."
    return ChatResponse(
        conversation_id=session.id,
        message=message,
        identity_status=identity.status,
        route=decision.route,
        intent=decision.intent,
        cards=[],
        actions=[],
        tool_trace=[ChatToolTrace(name="triage", decision=decision.reason)],
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
    if not cards:
        cards = recommendation_cards(db, store_id=req.context.store_id, category=req.context.category, limit=3)
        tool_name = "catalog_recommendations"

    if identity.customer_id and tool_name == "customer_recommendations":
        message = "Based on your account and this shopping context, these are the best matches I would start with."
    else:
        message = "I found a few strong options from the catalog."
    _record_tool(db, session.id, None, tool_name, summarize_context(req.context), cards)
    return ChatResponse(
        conversation_id=session.id,
        message=message,
        identity_status=identity.status,
        route=decision.route,
        intent=decision.intent,
        cards=cards,
        actions=_actions_for_cards(cards),
        tool_trace=[ChatToolTrace(name="triage", decision=decision.reason), ChatToolTrace(name=tool_name, decision="backend-derived customer_id" if identity.customer_id else "anonymous catalog context")],
    )


def handle_chat(db: Session, req: ChatRequest, identity: ChatIdentity) -> ChatResponse:
    req = _normalize_context(db, req)
    session = _persist_session(db, req, identity)
    history = _recent_history(db, session.id)
    orchestration = evaluate_chat(req.message, req.context, history=history)
    decision = orchestration.decision
    _persist_message(db, session.id, "user", req.message, {"context": summarize_context(req.context)})

    auth_required = _requires_auth(decision, orchestration)
    selected_tool = orchestration.selected_tool
    if orchestration.requires_followup and orchestration.clarifying_question:
        response = _followup_response(req, identity, session, decision, orchestration.clarifying_question)
        auth_decision = "allowed; followup requested"
    elif auth_required and identity.status != "authenticated_customer":
        response = _blocked_response(req, identity, session, decision)
        auth_decision = f"blocked; tool={selected_tool}"
    elif selected_tool == "store_info":
        response = _store_info_response(db, req, identity, session, decision)
        auth_decision = "allowed; public store info"
    elif selected_tool == "service_answer":
        response = _service_answer_response(db, req, identity, session, decision)
        auth_decision = "allowed; approved service answer"
    elif selected_tool == "order_status":
        response = _order_status_response(db, req, identity, session, decision)
        auth_decision = "allowed; backend-derived customer_id"
    elif selected_tool == "related_products":
        response = _related_products_response(db, req, identity, session, decision)
        auth_decision = "allowed; public catalog"
    elif selected_tool == "customer_summary":
        response = _account_response(db, identity, session, decision)
        auth_decision = "allowed; backend-derived customer_id"
    elif selected_tool == "customer_recommendations":
        response = _recommendation_response(db, req, identity, session, decision)
        auth_decision = "allowed; backend-derived customer_id"
    elif selected_tool == "product_detail":
        response = _product_context_response(db, req, identity, session, decision)
        auth_decision = "allowed; public product context"
    elif selected_tool == "semantic_catalog_search":
        response = _semantic_catalog_response(db, req, identity, session, decision)
        auth_decision = "allowed; public catalog"
    elif selected_tool == "chat_response":
        response = _general_response(req, identity, session, decision)
        auth_decision = "allowed; general response"
    else:
        response = _general_response(req, identity, session, decision)
        auth_decision = f"allowed; unrecognized selected_tool={selected_tool}"

    response = _apply_orchestration_trace(response, orchestration, auth_decision=auth_decision)

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
