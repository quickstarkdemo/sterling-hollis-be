from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import re
from uuid import uuid4

from sqlalchemy.orm import Session

from app.api_traces.operations import (
    api_trace_database_operation,
    api_trace_operation,
    api_trace_session,
    correlated_observability_kwargs,
    current_api_trace_session,
    link_api_trace_replay,
)
from app.catalog.schemas import CatalogProduct
from app.config import get_settings
from app.models import ChatMessage, ChatSession, ChatToolCall, ChatTurn
from app.observability.genai_otel import suppress_genai_otel
from app.services.auth.clerk import ChatIdentity
from app.services.chat.context import summarize_context
from app.services.chat.evaluator import ChatOrchestrationDecision, evaluate_chat
from app.services.chat.intent_frame import ChatIntentFrame, build_chat_intent_frame
from app.services.chat.safety import ChatSafetyDecision, evaluate_chat_safety
from app.services.chat.schemas import ChatAction, ChatCurrentProduct, ChatRequest, ChatResponse, ChatToolTrace
from app.services.chat.strands_orchestrator import persist_strands_tool_calls, run_storefront_shopping_agent
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
from app.services.chat.triage import OUTFIT_TERMS, PAIRING_TERMS, TriageDecision, triage_chat
from app.services.demo_observability import (
    CORRELATION_KEY,
    INCIDENT_ID,
    demo_observability_active_for_store,
    run_available_to_promise_reconciliation,
)
from ddtrace.llmobs import LLMObs


logger = logging.getLogger(__name__)


def _llmobs_annotate_safe(**kwargs) -> None:
    try:
        if not LLMObs.enabled:
            return
        LLMObs.annotate(**correlated_observability_kwargs(kwargs))
    except Exception:
        logger.debug("Failed to annotate Datadog LLMObs span", exc_info=True)


def _llmobs_export_span_safe(span):
    try:
        if not LLMObs.enabled:
            return None
        return LLMObs.export_span(span)
    except Exception:
        logger.debug("Failed to export Datadog LLMObs span", exc_info=True)
        return None


def _llmobs_submit_evaluation_safe(span, *, label: str, value: float, metadata: dict | None = None) -> None:
    try:
        if not LLMObs.enabled or span is None:
            return
        LLMObs.submit_evaluation(
            span=span,
            label=label,
            value=float(value),
            metric_type="score",
            metadata=metadata,
        )
    except Exception:
        logger.debug("Failed to submit Datadog LLMObs evaluation %s", label, exc_info=True)


def _run_demo_available_to_promise_reconciliation(
    db: Session,
    *,
    session_id: str,
    turn_id: str | None,
    selected_tool: str | None,
    store_id: str | None,
    turn_metadata: dict,
) -> dict | None:
    if not demo_observability_active_for_store(store_id):
        return None

    with LLMObs.tool(name="available_to_promise_reconciliation", session_id=session_id) as demo_span:
        tags = {
            "workflow": "chat",
            "tool": "available_to_promise_reconciliation",
            "selected_tool": selected_tool,
            "conversation_id": session_id,
            "turn_id": turn_id,
            "store_id": store_id,
            **turn_metadata,
        }
        _llmobs_annotate_safe(
            span=demo_span,
            input_data={
                "conversation_id": session_id,
                "turn_id": turn_id,
                "selected_tool": selected_tool,
                "store_id": store_id,
            },
            tags=tags,
        )
        try:
            result = run_available_to_promise_reconciliation(
                db,
                conversation_id=session_id,
                turn_id=turn_id,
                selected_tool=selected_tool,
                store_id=store_id,
            )
        except Exception as exc:
            _llmobs_annotate_safe(
                span=demo_span,
                output_data={
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "conversation_id": session_id,
                    "turn_id": turn_id,
                    "selected_tool": selected_tool,
                    "store_id": store_id,
                },
                tags={**tags, "error.type": type(exc).__name__},
            )
            logger.exception("Demo available-to-promise reconciliation failed unexpectedly; continuing chat")
            return {
                "demo.incident_id": INCIDENT_ID,
                "demo.correlation_key": CORRELATION_KEY,
                "mode": "unexpected_error",
                "status": "degraded",
                "error": type(exc).__name__,
                "error_message": str(exc),
            }

        if result is not None:
            _llmobs_annotate_safe(
                span=demo_span,
                output_data=result,
                tags={
                    **tags,
                    "demo.incident_id": result.get("demo.incident_id"),
                    "demo.scenario": result.get("demo.scenario"),
                    "demo.correlation_key": result.get("demo.correlation_key"),
                },
            )
        return result


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:24]}"


def _actions_for_cards(cards: list[CatalogProduct]) -> list[ChatAction]:
    return [
        ChatAction(type="view_product", label=f"View {card.title}", href=f"/product/{card.id}", product_id=card.id)
        for card in cards
    ]


AUTH_REQUIRED_TOOLS = {"customer_recommendations", "customer_summary", "order_status"}
STRANDS_PUBLIC_TOOLS = {"semantic_catalog_search", "related_products", "product_detail", "store_info"}
SIMILAR_PRODUCT_TERMS = {
    "similar",
    "more like this",
    "another",
    "alternatives",
    "alternative",
    "same style",
    "same shoe",
    "other colors",
    "other color",
    "more colors",
}


def _current_product_id(req: ChatRequest) -> str | None:
    return req.context.current_product.id if req.context.current_product else req.context.product_id


def _normalized_message(message: str) -> str:
    return " ".join(re.sub(r"[^\w\s$.]", " ", message.lower()).split())


def _contains_any(message: str, terms: set[str]) -> bool:
    return any(term in message for term in terms)


def _should_run_pairing_route_policy(req: ChatRequest) -> bool:
    normalized = _normalized_message(req.message)
    return bool(
        _current_product_id(req)
        and (_contains_any(normalized, PAIRING_TERMS) or _contains_any(normalized, OUTFIT_TERMS))
        and not _contains_any(normalized, SIMILAR_PRODUCT_TERMS)
    )


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


def _normalize_fingerprint_message(message: str) -> str:
    return " ".join(message.casefold().split())


def _request_fingerprint(req: ChatRequest) -> str:
    context = summarize_context(req.context)
    material_context = {
        "category": context.get("category"),
        "current_product": context.get("current_product"),
        "page_type": context.get("page_type"),
        "product_id": context.get("product_id"),
        "route": context.get("route"),
        "store_id": context.get("store_id"),
    }
    payload = {
        "message": _normalize_fingerprint_message(req.message),
        "context": material_context,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _trace_client_request_id(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _trace_route(value: str | None) -> str | None:
    if not value:
        return None
    return value.split("?", 1)[0].split("#", 1)[0][:128]


def _turn_metadata(
    *,
    turn_id: str,
    req: ChatRequest,
    fingerprint: str,
    possible_duplicate: bool = False,
    duplicate_replay: bool = False,
) -> dict:
    return {
        "turn_id": turn_id,
        "client_request_id": req.client_request_id or "",
        "trigger_type": req.trigger_type,
        "parent_turn_id": req.parent_turn_id or "",
        "request_fingerprint": fingerprint,
        "possible_duplicate": possible_duplicate,
        "duplicate_replay": duplicate_replay,
    }


def _turn_message_payload(req: ChatRequest, metadata: dict, *, context: dict | None = None) -> dict:
    return {
        "context": context if context is not None else summarize_context(req.context),
        "turn": metadata,
    }


def _assistant_message_payload(response: ChatResponse, metadata: dict) -> dict:
    payload = response.model_dump(mode="json")
    payload["turn"] = metadata
    return payload


def _find_completed_turn_by_client_request(
    db: Session,
    *,
    session_id: str,
    client_request_id: str | None,
) -> ChatTurn | None:
    if not client_request_id:
        return None
    return (
        db.query(ChatTurn)
        .filter(
            ChatTurn.session_id == session_id,
            ChatTurn.client_request_id == client_request_id,
            ChatTurn.status == "completed",
        )
        .order_by(ChatTurn.created_at.desc())
        .first()
    )


def _has_recent_matching_turn(
    db: Session,
    *,
    session_id: str,
    fingerprint: str,
    now: datetime,
) -> bool:
    cutoff = now - timedelta(seconds=10)
    return (
        db.query(ChatTurn.id)
        .filter(
            ChatTurn.session_id == session_id,
            ChatTurn.request_fingerprint == fingerprint,
            ChatTurn.created_at >= cutoff,
            ChatTurn.status == "completed",
        )
        .first()
        is not None
    )


def _create_chat_turn(db: Session, *, session: ChatSession, req: ChatRequest, fingerprint: str, now: datetime) -> ChatTurn:
    turn = ChatTurn(
        id=_id("turn"),
        session_id=session.id,
        client_request_id=req.client_request_id,
        trigger_type=req.trigger_type,
        parent_turn_id=req.parent_turn_id,
        request_fingerprint=fingerprint,
        message=req.message,
        context_json=summarize_context(req.context),
        response_json={},
        status="pending",
        created_at=now,
        updated_at=now,
    )
    db.add(turn)
    db.flush()
    return turn


def _apply_turn_metadata(response: ChatResponse, metadata: dict) -> ChatResponse:
    response.turn_id = metadata["turn_id"]
    response.client_request_id = metadata["client_request_id"] or None
    response.trigger_type = metadata["trigger_type"]
    response.duplicate_replay = bool(metadata["duplicate_replay"])
    return response


def _complete_chat_turn(
    turn: ChatTurn,
    *,
    response: ChatResponse,
    user_message: ChatMessage | None,
    assistant_message: ChatMessage | None,
) -> None:
    now = datetime.now(timezone.utc)
    turn.user_message_id = user_message.id if user_message else turn.user_message_id
    turn.assistant_message_id = assistant_message.id if assistant_message else turn.assistant_message_id
    response_payload = response.model_dump(mode="json")
    trace = current_api_trace_session()
    if trace is not None:
        response_payload["_api_trace"] = {
            "trace_id": trace.trace_id,
            "span_id": trace.server_span_id,
        }
    turn.response_json = response_payload
    turn.status = "completed"
    turn.updated_at = now


def _response_from_completed_turn(turn: ChatTurn, *, req: ChatRequest) -> ChatResponse:
    response = ChatResponse.model_validate(turn.response_json)
    response.duplicate_replay = True
    response.turn_id = response.turn_id or turn.id
    response.client_request_id = response.client_request_id or req.client_request_id
    return response


def _annotate_duplicate_replay(
    *,
    root_span,
    req: ChatRequest,
    session: ChatSession,
    identity: ChatIdentity,
    response: ChatResponse,
    metadata: dict,
) -> None:
    _llmobs_annotate_safe(
        span=root_span,
        input_data={
            "message": req.message,
            "conversation_id": session.id,
            "context": summarize_context(req.context),
            **metadata,
        },
        output_data={
            "conversation_id": response.conversation_id,
            "message": response.message,
            "route": response.route,
            "intent": response.intent,
            "selected_agent": response.selected_agent,
            "selected_tool": response.selected_tool,
            "duplicate_replay": True,
        },
        metadata={
            "identity_status": identity.status,
            "conversation_id": session.id,
            **metadata,
        },
        tags={
            "workflow": "chat",
            "identity_status": identity.status,
            "conversation_id": session.id,
            **metadata,
        },
    )


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
    with api_trace_database_operation(
        "Stage chat tool result",
        attributes={"tool_name": name, "status": "pending"},
    ) as trace_span:
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
        if trace_span is not None:
            trace_span.annotate(status="staged")


def _requires_auth(decision: TriageDecision, orchestration: ChatOrchestrationDecision) -> bool:
    return bool(
        decision.requires_customer
        or orchestration.requires_auth
        or orchestration.selected_tool in AUTH_REQUIRED_TOOLS
    )


def _settings_enable_strands_product() -> bool:
    from app.config import get_settings

    return get_settings().chat_orchestration_mode == "strands_product"


def _strands_candidate_decision(req: ChatRequest, identity: ChatIdentity) -> ChatOrchestrationDecision | None:
    if not _settings_enable_strands_product():
        return None
    decision = triage_chat(req.message, req.context)
    selected_tool = decision.tool if decision.tool in STRANDS_PUBLIC_TOOLS else "chat_response"
    if selected_tool not in STRANDS_PUBLIC_TOOLS:
        return None
    if decision.requires_customer or selected_tool in AUTH_REQUIRED_TOOLS:
        return None
    if identity.status not in {"anonymous", "authenticated_customer", "authenticated_unlinked"}:
        return None
    return ChatOrchestrationDecision(
        decision=decision,
        selected_agent="StorefrontShoppingAgent",
        selected_tool="strands_agent",
        evaluator_confidence=1.0,
        evaluator_source="deterministic_strands_product_eligibility",
        requires_auth=False,
        requires_followup=False,
        clarifying_question=None,
    )


def _apply_pairing_route_policy(req: ChatRequest, orchestration: ChatOrchestrationDecision) -> ChatOrchestrationDecision:
    normalized = _normalized_message(req.message)
    if not _current_product_id(req):
        return orchestration
    if not (_contains_any(normalized, PAIRING_TERMS) or _contains_any(normalized, OUTFIT_TERMS)):
        return orchestration
    if _contains_any(normalized, SIMILAR_PRODUCT_TERMS):
        return orchestration

    policy_decision = triage_chat(req.message, req.context)
    if policy_decision.route != "semantic_catalog_search":
        return orchestration
    if orchestration.selected_tool == "semantic_catalog_search" and orchestration.decision == policy_decision:
        return orchestration

    return replace(
        orchestration,
        decision=policy_decision,
        selected_agent="ProductAgent",
        selected_tool="semantic_catalog_search",
        requires_auth=policy_decision.requires_customer,
        requires_followup=False,
        clarifying_question=None,
        evaluator_source=f"{orchestration.evaluator_source}; policy_override=pairing_semantic",
    )


def _semantic_search_used(response: ChatResponse, orchestration: ChatOrchestrationDecision | None) -> bool:
    if orchestration is None:
        return False
    if orchestration.selected_tool == "semantic_catalog_search" or response.selected_tool == "semantic_catalog_search":
        return True
    return any(
        trace.name == "semantic_catalog_search" or "strategy=semantic_catalog_search" in trace.decision
        for trace in response.tool_trace
    )


def _safety_decision_summary(decision: ChatSafetyDecision | None) -> dict:
    if decision is None:
        return {
            "safety_intercepted": False,
            "safety_source": "not_evaluated",
            "safety_action": "not_evaluated",
            "safety_category": "none",
        }
    return {
        "safety_intercepted": decision.intercepted,
        "safety_source": decision.source,
        "safety_action": decision.action,
        "safety_category": decision.category or "none",
    }


def _safety_prompt_injection_detected(decision: ChatSafetyDecision | None) -> bool:
    if not decision or not decision.intercepted:
        return False
    tags = " ".join(decision.tags).lower()
    return bool(
        decision.category == "prompt_injection"
        or "prompt" in tags
        or "injection" in tags
        or "jailbreak" in tags
        or "system prompt" in tags
        or "role" in tags
    )


def _safety_data_exfiltration_detected(decision: ChatSafetyDecision | None) -> bool:
    if not decision or not decision.intercepted:
        return False
    tags = " ".join(decision.tags).lower()
    return bool(
        decision.category == "data_exfiltration"
        or "exfil" in tags
        or "personal" in tags
        or "customer" in tags
        or "account" in tags
        or "password" in tags
        or "api key" in tags
        or "token" in tags
        or "secret" in tags
    )


def _submit_chat_evaluations(
    exported_span,
    *,
    response: ChatResponse,
    orchestration: ChatOrchestrationDecision | None,
    auth_decision: str,
    safety_decision: ChatSafetyDecision | None = None,
) -> None:
    safety = _safety_decision_summary(safety_decision)
    metadata = {
        "conversation_id": response.conversation_id,
        "identity_status": response.identity_status,
        "selected_agent": response.selected_agent,
        "selected_tool": response.selected_tool,
        "intent": response.intent,
        "route": response.route,
        "auth_decision": auth_decision,
        "card_count": len(response.cards),
        **safety,
    }
    safety_blocked = bool(safety_decision and safety_decision.intercepted)
    auth_blocked = (not safety_blocked) and (response.route == "blocked" or auth_decision.startswith("blocked"))
    evaluations = {
        "chat_route_confidence": orchestration.evaluator_confidence if orchestration else 0.0,
        "chat_auth_blocked": 1.0 if auth_blocked else 0.0,
        "chat_followup_required": 1.0 if response.requires_followup else 0.0,
        "chat_result_card_count": float(len(response.cards)),
        "chat_semantic_search_used": 1.0 if _semantic_search_used(response, orchestration) else 0.0,
        "chat_fallback_used": (
            1.0
            if orchestration and orchestration.evaluator_source.startswith("deterministic_fallback")
            else 0.0
        ),
        "chat_safety_blocked": 1.0 if safety_blocked else 0.0,
        "chat_prompt_injection_detected": 1.0 if _safety_prompt_injection_detected(safety_decision) else 0.0,
        "chat_data_exfiltration_request": 1.0 if _safety_data_exfiltration_detected(safety_decision) else 0.0,
    }
    for label, value in evaluations.items():
        _llmobs_submit_evaluation_safe(exported_span, label=label, value=value, metadata=metadata)


def _apply_orchestration_trace(
    response: ChatResponse,
    orchestration: ChatOrchestrationDecision,
    frame: ChatIntentFrame,
    *,
    auth_decision: str,
) -> ChatResponse:
    response.evaluator_confidence = round(orchestration.evaluator_confidence, 4)
    response.selected_agent = orchestration.selected_agent
    response.selected_tool = orchestration.selected_tool
    response.requires_followup = response.requires_followup or orchestration.requires_followup
    response.clarifying_question = response.clarifying_question or orchestration.clarifying_question
    response.tool_trace.extend(
        [
            ChatToolTrace(
                name="ChatIntakeAgent",
                decision=f"{orchestration.evaluator_source}; confidence={orchestration.evaluator_confidence:.2f}",
            ),
            ChatToolTrace(name="intent_frame", decision=frame.trace_decision()),
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


def _safety_response(session: ChatSession, identity: ChatIdentity, decision: ChatSafetyDecision) -> ChatResponse:
    trace_decision = f"{decision.source}; action={decision.action}; category={decision.category or 'none'}"
    if decision.reason:
        trace_decision = f"{trace_decision}; reason={decision.reason}"
    return ChatResponse(
        conversation_id=session.id,
        message=decision.content,
        identity_status=identity.status,
        route="blocked",
        intent="account_question",
        cards=[],
        actions=[],
        selected_agent="SafetyGuard",
        selected_tool="safety_refusal",
        tool_trace=[
            ChatToolTrace(name="chat_safety_guard", decision=trace_decision),
            ChatToolTrace(name="safety_refusal", decision="returned safety refusal"),
        ],
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


def _related_products_response(
    db: Session,
    req: ChatRequest,
    identity: ChatIdentity,
    session: ChatSession,
    decision: TriageDecision,
    frame: ChatIntentFrame,
) -> ChatResponse:
    current_product_id = frame.current_product_id
    cards = (
        store_scoped_related_product_cards(
            db,
            current_product_id,
            store_id=req.context.store_id,
            target_genders=frame.target_genders,
            strict_gender=bool(frame.target_genders),
            limit=3,
        )
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


def _semantic_catalog_response(
    db: Session,
    req: ChatRequest,
    identity: ChatIdentity,
    session: ChatSession,
    decision: TriageDecision,
    frame: ChatIntentFrame,
) -> ChatResponse:
    strict_gender = bool(frame.target_genders)
    cards, strategy = semantic_catalog_cards(
        db,
        query=frame.query,
        target_categories=frame.target_categories,
        exclude_categories=frame.exclude_categories,
        target_genders=frame.target_genders,
        budget_max=frame.budget_max,
        colors=frame.colors,
        current_product_id=frame.current_product_id,
        store_id=req.context.store_id,
        strict_gender=strict_gender,
        limit=3,
    )
    if cards and frame.intent == "complementary_products":
        current_label = "item"
        if req.context.current_product and req.context.current_product.title:
            current_label = req.context.current_product.title
        message = f"I found options that should pair well with {current_label}."
    elif cards:
        message = "I found a few products that fit that request."
    else:
        message = "I could not find matching catalog products for that request."

    needs_strict_followup = frame.intent == "complementary_products" and strict_gender and len(cards) < 3
    clarifying_question = None
    if needs_strict_followup:
        current_label = "that item"
        if req.context.current_product and req.context.current_product.title:
            current_label = req.context.current_product.title
        clarifying_question = (
            f"I found fewer than three strict matches for {current_label}. "
            "Would you like me to broaden to unisex accessories or looser styling matches?"
        )

    trace_decision = (
        f"target_category={','.join(frame.target_categories) or 'any'}, "
        f"target_gender={','.join(frame.target_genders) or 'any'}, "
        f"budget_max={frame.budget_max or 'none'}, "
        f"exclude_category={','.join(frame.exclude_categories) or 'none'}, "
        f"strategy={strategy}"
    )
    _record_tool(
        db,
        session.id,
        None,
        "semantic_catalog_search",
        {
            "query": frame.query,
            "target_categories": frame.target_categories,
            "exclude_categories": frame.exclude_categories,
            "target_genders": frame.target_genders,
            "budget_max": frame.budget_max,
            "strategy": strategy,
            "strict_gender": strict_gender,
        },
        cards,
    )
    return ChatResponse(
        conversation_id=session.id,
        message=message,
        identity_status=identity.status,
        intent=frame.intent,
        route="semantic_catalog_search",
        cards=cards,
        actions=_actions_for_cards(cards),
        tool_trace=[
            ChatToolTrace(name="triage", decision=decision.reason),
            ChatToolTrace(name="semantic_catalog_search", decision=trace_decision),
        ],
        requires_followup=needs_strict_followup,
        clarifying_question=clarifying_question,
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


def _execute_selected_tool_response(
    db: Session,
    req: ChatRequest,
    identity: ChatIdentity,
    session: ChatSession,
    decision: TriageDecision,
    orchestration: ChatOrchestrationDecision,
    frame: ChatIntentFrame,
    *,
    auth_required: bool,
    selected_tool: str,
) -> tuple[ChatResponse, str]:
    if orchestration.requires_followup and orchestration.clarifying_question:
        return (
            _followup_response(
                req,
                identity,
                session,
                decision,
                orchestration.clarifying_question,
            ),
            "allowed; followup requested",
        )
    if auth_required and identity.status != "authenticated_customer":
        return _blocked_response(req, identity, session, decision), f"blocked; tool={selected_tool}"
    if selected_tool == "store_info":
        return _store_info_response(db, req, identity, session, decision), "allowed; public store info"
    if selected_tool == "service_answer":
        return _service_answer_response(db, req, identity, session, decision), "allowed; approved service answer"
    if selected_tool == "order_status":
        return _order_status_response(db, req, identity, session, decision), "allowed; backend-derived customer_id"
    if selected_tool == "related_products":
        return _related_products_response(db, req, identity, session, decision, frame), "allowed; public catalog"
    if selected_tool == "customer_summary":
        return _account_response(db, identity, session, decision), "allowed; backend-derived customer_id"
    if selected_tool == "customer_recommendations":
        return _recommendation_response(db, req, identity, session, decision), "allowed; backend-derived customer_id"
    if selected_tool == "product_detail":
        return _product_context_response(db, req, identity, session, decision), "allowed; public product context"
    if selected_tool == "semantic_catalog_search":
        return _semantic_catalog_response(db, req, identity, session, decision, frame), "allowed; public catalog"
    if selected_tool == "chat_response":
        return _general_response(req, identity, session, decision), "allowed; general response"
    return _general_response(req, identity, session, decision), f"allowed; unrecognized selected_tool={selected_tool}"


def _handle_chat(db: Session, req: ChatRequest, identity: ChatIdentity) -> ChatResponse:
    with api_trace_database_operation(
        "Resolve storefront chat context",
        attributes={
            "identity_status": identity.status,
            "route": _trace_route(req.context.route),
            "store_id": req.context.store_id,
        },
    ) as context_span:
        original_context = summarize_context(req.context)
        req = _normalize_context(db, req)
        session = _persist_session(db, req, identity)
        if context_span is not None:
            context_span.annotate(
                session_id=session.id,
                context=summarize_context(req.context),
            )
    now = datetime.now(timezone.utc)
    request_fingerprint = _request_fingerprint(req)
    completed_turn = _find_completed_turn_by_client_request(
        db,
        session_id=session.id,
        client_request_id=req.client_request_id,
    )
    if completed_turn is not None:
        with api_trace_operation(
            "Replay completed chat turn",
            "chat.replay",
            attributes={
                "client_request_id": _trace_client_request_id(
                    req.client_request_id
                ),
                "turn_id": completed_turn.id,
                "duplicate_replay": True,
            },
        ):
            response = _response_from_completed_turn(completed_turn, req=req)
            prior_trace = dict(completed_turn.response_json or {}).get("_api_trace")
            if isinstance(prior_trace, dict):
                link_api_trace_replay(
                    linked_trace_id=prior_trace.get("trace_id"),
                    linked_span_id=prior_trace.get("span_id"),
                )
            replay_metadata = _turn_metadata(
                turn_id=completed_turn.id,
                req=req,
                fingerprint=completed_turn.request_fingerprint,
                duplicate_replay=True,
            )
            with suppress_genai_otel(), LLMObs.agent(name="sterling_hollis_chat", session_id=session.id) as root_span:
                _annotate_duplicate_replay(
                    root_span=root_span,
                    req=req,
                    session=session,
                    identity=identity,
                    response=response,
                    metadata=replay_metadata,
                )
                _llmobs_export_span_safe(root_span)
        return response

    possible_duplicate = False
    if not req.client_request_id:
        possible_duplicate = _has_recent_matching_turn(
            db,
            session_id=session.id,
            fingerprint=request_fingerprint,
            now=now,
        )
    turn = _create_chat_turn(
        db,
        session=session,
        req=req,
        fingerprint=request_fingerprint,
        now=now,
    )
    turn_metadata = _turn_metadata(
        turn_id=turn.id,
        req=req,
        fingerprint=request_fingerprint,
        possible_duplicate=possible_duplicate,
    )
    exported_span = None
    response: ChatResponse | None = None
    orchestration: ChatOrchestrationDecision | None = None
    safety_decision: ChatSafetyDecision | None = None
    auth_decision = "not_evaluated"

    with suppress_genai_otel(), LLMObs.agent(name="sterling_hollis_chat", session_id=session.id) as root_span:
        _llmobs_annotate_safe(
            span=root_span,
            input_data={
                "message": req.message,
                "conversation_id": session.id,
                "context": summarize_context(req.context),
                **turn_metadata,
            },
            tags={
                "workflow": "chat",
                "identity_status": identity.status,
                "conversation_id": session.id,
                **turn_metadata,
            },
        )

        with LLMObs.workflow(name="chat_turn", session_id=session.id) as workflow_span:
            _llmobs_annotate_safe(
                span=workflow_span,
                input_data={
                    "message": req.message,
                    "conversation_id": session.id,
                    "context": summarize_context(req.context),
                    **turn_metadata,
                },
                tags={
                    "workflow": "chat",
                    "identity_status": identity.status,
                    **turn_metadata,
                },
            )

            with LLMObs.tool(name="normalize_context", session_id=session.id) as normalize_span:
                _llmobs_annotate_safe(
                    span=normalize_span,
                    input_data={"context": original_context},
                    output_data={"context": summarize_context(req.context)},
                    tags={"workflow": "chat", **turn_metadata},
                )

            with LLMObs.workflow(name="chat_history", session_id=session.id) as history_span:
                with api_trace_database_operation(
                    "Load recent chat history",
                    attributes={"session_id": session.id},
                ) as history_trace_span:
                    history = _recent_history(db, session.id)
                    if history_trace_span is not None:
                        history_trace_span.annotate(
                            history_count=len(history),
                            status="loaded",
                        )
                _llmobs_annotate_safe(
                    span=history_span,
                    output_data={
                        "conversation_id": session.id,
                        "history_count": len(history),
                        "roles": [turn["role"] for turn in history],
                    },
                    tags={"workflow": "chat", **turn_metadata},
                )

            with LLMObs.tool(name="chat_safety_guard", session_id=session.id) as safety_span:
                with api_trace_operation(
                    "Evaluate chat safety",
                    "chat.safety",
                    attributes={"history_count": len(history)},
                ) as safety_trace_span:
                    safety_decision = evaluate_chat_safety(req.message, history=history)
                    if safety_trace_span is not None:
                        safety_trace_span.status = (
                            "blocked" if safety_decision.intercepted else "succeeded"
                        )
                        safety_trace_span.annotate(
                            blocked=safety_decision.intercepted,
                            source=safety_decision.source,
                            decision=safety_decision.action,
                            category=safety_decision.category,
                        )
                _llmobs_annotate_safe(
                    span=safety_span,
                    input_data={
                        "message": req.message,
                        "conversation_id": session.id,
                        "history_count": len(history),
                        **turn_metadata,
                    },
                    output_data={
                        "intercepted": safety_decision.intercepted,
                        "source": safety_decision.source,
                        "action": safety_decision.action,
                        "category": safety_decision.category,
                        "reason": safety_decision.reason,
                        "tags": list(safety_decision.tags),
                    },
                    tags={
                        "workflow": "chat",
                        **turn_metadata,
                        **_safety_decision_summary(safety_decision),
                    },
                )

            if safety_decision.intercepted:
                auth_decision = f"safety_blocked; source={safety_decision.source}; action={safety_decision.action}"

                with LLMObs.tool(name="persist_user_message", session_id=session.id) as persist_user_span:
                    user_message = _persist_message(
                        db,
                        session.id,
                        "user",
                        req.message,
                        _turn_message_payload(req, turn_metadata),
                    )
                    _llmobs_annotate_safe(
                        span=persist_user_span,
                        input_data={"conversation_id": session.id, "role": "user"},
                        output_data={"message_id": user_message.id},
                        tags={"workflow": "chat", **turn_metadata},
                    )

                with LLMObs.tool(name="safety_refusal", session_id=session.id) as refusal_span:
                    response = _safety_response(session, identity, safety_decision)
                    response = _apply_turn_metadata(response, turn_metadata)
                    _llmobs_annotate_safe(
                        span=refusal_span,
                        input_data={
                            "source": safety_decision.source,
                            "action": safety_decision.action,
                            "category": safety_decision.category,
                            "tags": list(safety_decision.tags),
                        },
                        output_data={
                            "route": response.route,
                            "intent": response.intent,
                            "message": response.message,
                            "auth_decision": auth_decision,
                            **turn_metadata,
                        },
                        tags={
                            "workflow": "chat",
                            "tool": "safety_refusal",
                            "agent": "SafetyGuard",
                            **turn_metadata,
                            **_safety_decision_summary(safety_decision),
                        },
                    )

                with LLMObs.task(name="apply_orchestration_trace", session_id=session.id) as trace_span:
                    _llmobs_annotate_safe(
                        span=trace_span,
                        output_data={
                            "trace_count": len(response.tool_trace),
                            "selected_agent": response.selected_agent,
                            "selected_tool": response.selected_tool,
                        },
                        tags={"workflow": "chat", **turn_metadata},
                    )

                with LLMObs.tool(name="persist_assistant_message", session_id=session.id) as persist_assistant_span:
                    with api_trace_database_operation(
                        "Commit safety-blocked chat turn",
                        attributes={
                            "turn_id": turn.id,
                            "blocked": True,
                            "status": "pending",
                        },
                    ) as persist_trace_span:
                        assistant = _persist_message(
                            db,
                            session.id,
                            "assistant",
                            response.message,
                            _assistant_message_payload(response, turn_metadata),
                        )
                        _complete_chat_turn(
                            turn,
                            response=response,
                            user_message=user_message,
                            assistant_message=assistant,
                        )
                        db.commit()
                        if persist_trace_span is not None:
                            persist_trace_span.annotate(status="committed")
                    _llmobs_annotate_safe(
                        span=persist_assistant_span,
                        input_data={"conversation_id": session.id, "role": "assistant"},
                        output_data={"message_id": assistant.id},
                        tags={"workflow": "chat", **turn_metadata},
                    )
            else:
                strands_tool_calls = []
                using_strands = False
                strands_selected_tool: str | None = None
                with api_trace_operation(
                    "Route storefront chat turn",
                    "chat.routing",
                    attributes={"history_count": len(history)},
                ) as routing_trace_span:
                    orchestration = _strands_candidate_decision(req, identity)
                    if orchestration is None:
                        orchestration = evaluate_chat(
                            req.message,
                            req.context,
                            history=history,
                            session_id=session.id,
                        )
                    else:
                        using_strands = True
                    if routing_trace_span is not None:
                        routing_trace_span.annotate(
                            intent=orchestration.decision.intent,
                            route=orchestration.decision.route,
                            selected_agent=orchestration.selected_agent,
                            selected_tool=orchestration.selected_tool,
                            source=orchestration.evaluator_source,
                        )
                if not using_strands and _should_run_pairing_route_policy(req):
                    with LLMObs.task(name="pairing_route_policy", session_id=session.id) as policy_span:
                        before_tool = orchestration.selected_tool
                        orchestration = _apply_pairing_route_policy(req, orchestration)
                        _llmobs_annotate_safe(
                            span=policy_span,
                            input_data={
                                "message": req.message,
                                "selected_tool": before_tool,
                                "current_product_id": _current_product_id(req),
                            },
                            output_data={
                                "selected_tool": orchestration.selected_tool,
                                "selected_agent": orchestration.selected_agent,
                                "policy_applied": before_tool != orchestration.selected_tool,
                                "source": orchestration.evaluator_source,
                            },
                            tags={"workflow": "chat", **turn_metadata},
                        )
                decision = orchestration.decision

                with LLMObs.task(name="build_intent_frame", session_id=session.id) as frame_span:
                    frame = build_chat_intent_frame(req, orchestration)
                    _llmobs_annotate_safe(
                        span=frame_span,
                        input_data={
                            "intent": decision.intent,
                            "route": decision.route,
                            "selected_tool": orchestration.selected_tool,
                        },
                        output_data={
                            "intent": frame.intent,
                            "query": frame.query,
                            "target_categories": frame.target_categories,
                            "exclude_categories": frame.exclude_categories,
                            "target_genders": frame.target_genders,
                            "budget_max": frame.budget_max,
                            "current_product_id": frame.current_product_id,
                        },
                        tags={"workflow": "chat", **turn_metadata},
                    )

                with LLMObs.tool(name="persist_user_message", session_id=session.id) as persist_user_span:
                    user_message = _persist_message(
                        db,
                        session.id,
                        "user",
                        req.message,
                        _turn_message_payload(req, turn_metadata),
                    )
                    _llmobs_annotate_safe(
                        span=persist_user_span,
                        input_data={"conversation_id": session.id, "role": "user"},
                        output_data={"message_id": user_message.id},
                        tags={"workflow": "chat", **turn_metadata},
                    )

                auth_required = _requires_auth(decision, orchestration)
                selected_tool = orchestration.selected_tool

                with LLMObs.tool(name="auth_gate", session_id=session.id) as auth_span:
                    with api_trace_operation(
                        "Authorize selected chat tool",
                        "chat.authorization",
                        attributes={
                            "selected_tool": selected_tool,
                            "auth_required": auth_required,
                            "identity_status": identity.status,
                        },
                    ) as auth_trace_span:
                        will_block = bool(
                            auth_required
                            and identity.status != "authenticated_customer"
                        )
                        if auth_trace_span is not None:
                            auth_trace_span.status = (
                                "blocked" if will_block else "succeeded"
                            )
                            auth_trace_span.annotate(blocked=will_block)
                    _llmobs_annotate_safe(
                        span=auth_span,
                        input_data={
                            "selected_tool": selected_tool,
                            "auth_required": auth_required,
                            "identity_status": identity.status,
                        },
                        output_data={
                            "blocked": will_block,
                            "requires_followup": orchestration.requires_followup,
                        },
                        tags={
                            "workflow": "chat",
                            "tool": selected_tool,
                            "identity_status": identity.status,
                            **turn_metadata,
                        },
                    )

                with LLMObs.tool(name="execute_selected_tool", session_id=session.id) as execute_span:
                    _llmobs_annotate_safe(
                        span=execute_span,
                        input_data={
                            "selected_tool": selected_tool,
                            "selected_agent": orchestration.selected_agent,
                            "message": req.message,
                            "auth_required": auth_required,
                            "identity_status": identity.status,
                            "context": summarize_context(req.context),
                            **turn_metadata,
                        },
                        tags={
                            "workflow": "chat",
                            "tool": selected_tool,
                            "agent": orchestration.selected_agent,
                            **turn_metadata,
                        },
                    )

                    with LLMObs.tool(name=selected_tool, session_id=session.id) as selected_tool_span:
                        _llmobs_annotate_safe(
                            span=selected_tool_span,
                            input_data={
                                "selected_tool": selected_tool,
                                "selected_agent": orchestration.selected_agent,
                                "message": req.message,
                                "auth_required": auth_required,
                                "identity_status": identity.status,
                                "context": summarize_context(req.context),
                                **turn_metadata,
                            },
                            tags={
                                "workflow": "chat",
                                "tool": selected_tool,
                                "agent": orchestration.selected_agent,
                                **turn_metadata,
                            },
                        )

                        if using_strands:
                            with api_trace_operation(
                                "Run storefront shopping agent",
                                "chat.agent",
                                attributes={
                                    "selected_agent": "StorefrontShoppingAgent",
                                    "selected_tool": selected_tool,
                                    "intent": decision.intent,
                                },
                            ) as agent_trace_span:
                                strands_result = run_storefront_shopping_agent(
                                    db,
                                    req=req,
                                    identity=identity,
                                    session=session,
                                    decision=decision,
                                    frame=frame,
                                    history=history,
                                )
                                if agent_trace_span is not None:
                                    agent_trace_span.status = (
                                        "failed"
                                        if strands_result.error
                                        else "succeeded"
                                    )
                                    agent_trace_span.annotate(
                                        error_code=strands_result.error,
                                        tool_count=len(strands_result.tool_calls),
                                    )
                            strands_tool_calls = strands_result.tool_calls
                            if strands_result.response is not None:
                                response = strands_result.response
                                strands_selected_tool = response.selected_tool
                                auth_decision = "allowed; strands public storefront orchestration"
                            else:
                                fallback_tool = decision.tool if decision.tool in STRANDS_PUBLIC_TOOLS else "chat_response"
                                with api_trace_operation(
                                    "Run deterministic chat fallback",
                                    "chat.fallback",
                                    attributes={
                                        "selected_tool": fallback_tool,
                                        "error_code": strands_result.error,
                                        "fallback": True,
                                    },
                                ) as fallback_trace_span:
                                    response, auth_decision = _execute_selected_tool_response(
                                        db,
                                        req,
                                        identity,
                                        session,
                                        decision,
                                        replace(orchestration, selected_tool=fallback_tool),
                                        frame,
                                        auth_required=False,
                                        selected_tool=fallback_tool,
                                    )
                                    if fallback_trace_span is not None:
                                        fallback_trace_span.annotate(
                                            route=response.route,
                                            card_count=len(response.cards),
                                        )
                                response.tool_trace.append(
                                    ChatToolTrace(
                                        name="StrandsAgent",
                                        decision=f"fallback_to_deterministic; error={strands_result.error or 'unknown'}",
                                    )
                                )
                                strands_selected_tool = fallback_tool
                        else:
                            with api_trace_operation(
                                f"Execute {selected_tool}",
                                "chat.tool",
                                attributes={
                                    "selected_agent": orchestration.selected_agent,
                                    "selected_tool": selected_tool,
                                    "auth_required": auth_required,
                                },
                            ) as tool_trace_span:
                                response, auth_decision = _execute_selected_tool_response(
                                    db,
                                    req,
                                    identity,
                                    session,
                                    decision,
                                    orchestration,
                                    frame,
                                    auth_required=auth_required,
                                    selected_tool=selected_tool,
                                )
                                if tool_trace_span is not None:
                                    tool_trace_span.status = (
                                        "blocked"
                                        if response.route == "blocked"
                                        else "succeeded"
                                    )
                                    tool_trace_span.annotate(
                                        route=response.route,
                                        intent=response.intent,
                                        card_count=len(response.cards),
                                        decision=auth_decision,
                                    )

                        demo_reconciliation = _run_demo_available_to_promise_reconciliation(
                            db,
                            session_id=session.id,
                            turn_id=turn_metadata.get("turn_id"),
                            selected_tool=selected_tool,
                            store_id=req.context.store_id,
                            turn_metadata=turn_metadata,
                        )
                        if demo_reconciliation is not None:
                            response.tool_trace.append(
                                ChatToolTrace(
                                    name="available_to_promise_reconciliation",
                                    decision=(
                                        "demo_observability="
                                        f"{demo_reconciliation.get('mode')}; "
                                        f"status={demo_reconciliation.get('status', 'completed')}; "
                                        f"incident_id={demo_reconciliation.get('demo.incident_id')}; "
                                        f"correlation_key={demo_reconciliation.get('demo.correlation_key')}"
                                    ),
                                )
                            )

                        _llmobs_annotate_safe(
                            span=selected_tool_span,
                            output_data={
                                "route": response.route,
                                "intent": response.intent,
                                "message": response.message,
                                "auth_decision": auth_decision,
                                "card_count": len(response.cards),
                                "product_ids": [card.id for card in response.cards],
                                **turn_metadata,
                            },
                            metadata={
                                "requires_followup": response.requires_followup,
                                "selected_agent": response.selected_agent,
                                "selected_tool": response.selected_tool,
                                **turn_metadata,
                            },
                        )

                    _llmobs_annotate_safe(
                        span=execute_span,
                        output_data={
                            "route": response.route,
                            "intent": response.intent,
                            "message": response.message,
                            "auth_decision": auth_decision,
                            "card_count": len(response.cards),
                            "product_ids": [card.id for card in response.cards],
                            **turn_metadata,
                        },
                        metadata={
                            "requires_followup": response.requires_followup,
                            "selected_agent": response.selected_agent,
                            "selected_tool": response.selected_tool,
                            **turn_metadata,
                        },
                    )

                with LLMObs.task(name="apply_orchestration_trace", session_id=session.id) as trace_span:
                    response = _apply_orchestration_trace(
                        response,
                        orchestration,
                        frame,
                        auth_decision=auth_decision,
                    )
                    if using_strands:
                        response.selected_agent = "StorefrontShoppingAgent"
                        if strands_selected_tool:
                            response.selected_tool = strands_selected_tool
                    response = _apply_turn_metadata(response, turn_metadata)
                    _llmobs_annotate_safe(
                        span=trace_span,
                        output_data={
                            "trace_count": len(response.tool_trace),
                            "selected_agent": response.selected_agent,
                            "selected_tool": response.selected_tool,
                            **turn_metadata,
                        },
                        tags={"workflow": "chat", **turn_metadata},
                    )

                with LLMObs.tool(name="persist_assistant_message", session_id=session.id) as persist_assistant_span:
                    with api_trace_database_operation(
                        "Commit completed chat turn",
                        attributes={
                            "turn_id": turn.id,
                            "selected_tool": response.selected_tool,
                            "status": "pending",
                        },
                    ) as persist_trace_span:
                        assistant = _persist_message(
                            db,
                            session.id,
                            "assistant",
                            response.message,
                            _assistant_message_payload(response, turn_metadata),
                        )

                        for call in (
                            db.query(ChatToolCall)
                            .filter(
                                ChatToolCall.session_id == session.id,
                                ChatToolCall.message_id.is_(None),
                            )
                            .all()
                        ):
                            call.message_id = assistant.id
                        persist_strands_tool_calls(
                            db,
                            session_id=session.id,
                            message_id=assistant.id,
                            tool_calls=strands_tool_calls,
                            make_id=_id,
                        )

                        _complete_chat_turn(
                            turn,
                            response=response,
                            user_message=user_message,
                            assistant_message=assistant,
                        )
                        db.commit()
                        if persist_trace_span is not None:
                            persist_trace_span.annotate(
                                status="committed",
                                tool_count=len(strands_tool_calls),
                            )
                    _llmobs_annotate_safe(
                        span=persist_assistant_span,
                        input_data={"conversation_id": session.id, "role": "assistant"},
                        output_data={"message_id": assistant.id},
                        tags={"workflow": "chat", **turn_metadata},
                    )

            _llmobs_annotate_safe(
                span=workflow_span,
                output_data={
                    "conversation_id": response.conversation_id,
                    "message": response.message,
                    "route": response.route,
                    "intent": response.intent,
                    "selected_agent": response.selected_agent,
                    "selected_tool": response.selected_tool,
                    "requires_followup": response.requires_followup,
                    "card_count": len(response.cards),
                    "product_ids": [card.id for card in response.cards],
                    **turn_metadata,
                },
                metadata={
                    "auth_decision": auth_decision,
                    **turn_metadata,
                    **_safety_decision_summary(safety_decision),
                },
                tags={
                    "selected_agent": response.selected_agent,
                    "selected_tool": response.selected_tool,
                    "intent": response.intent,
                    "route": response.route,
                    **turn_metadata,
                    **_safety_decision_summary(safety_decision),
                },
            )

        _llmobs_annotate_safe(
            span=root_span,
            output_data={
                "conversation_id": response.conversation_id,
                "message": response.message,
                "route": response.route,
                "intent": response.intent,
                "selected_agent": response.selected_agent,
                "selected_tool": response.selected_tool,
                "requires_followup": response.requires_followup,
                "card_count": len(response.cards),
                "product_ids": [card.id for card in response.cards],
                **turn_metadata,
            },
            metadata={
                "identity_status": response.identity_status,
                "selected_agent": response.selected_agent,
                "selected_tool": response.selected_tool,
                "intent": response.intent,
                "route": response.route,
                "auth_decision": auth_decision,
                "conversation_id": response.conversation_id,
                "card_count": len(response.cards),
                **turn_metadata,
                **_safety_decision_summary(safety_decision),
            },
            tags={
                "identity_status": response.identity_status,
                "selected_agent": response.selected_agent,
                "selected_tool": response.selected_tool,
                "intent": response.intent,
                "route": response.route,
                "conversation_id": response.conversation_id,
                **turn_metadata,
                **_safety_decision_summary(safety_decision),
            },
        )
        exported_span = _llmobs_export_span_safe(root_span)

    assert response is not None
    _submit_chat_evaluations(
        exported_span,
        response=response,
        orchestration=orchestration,
        auth_decision=auth_decision,
        safety_decision=safety_decision,
    )
    return response


def handle_chat(db: Session, req: ChatRequest, identity: ChatIdentity) -> ChatResponse:
    settings = get_settings()
    with api_trace_session(
        settings=settings,
        name="Storefront chat turn",
        attributes={
            "identity_status": identity.status,
            "trigger_type": req.trigger_type,
            "route": _trace_route(req.context.route),
            "store_id": req.context.store_id,
            "client_request_id": _trace_client_request_id(req.client_request_id),
        },
    ) as trace:
        response = _handle_chat(db, req, identity)
        with api_trace_operation(
            "Prepare storefront chat response",
            "ui.response",
            attributes={
                "route": response.route,
                "intent": response.intent,
                "selected_agent": response.selected_agent,
                "selected_tool": response.selected_tool,
                "card_count": len(response.cards),
                "duplicate_replay": response.duplicate_replay,
            },
        ):
            pass
        if trace is not None:
            trace.status = "blocked" if response.route == "blocked" else "succeeded"
            trace.annotate(
                route=response.route,
                intent=response.intent,
                selected_agent=response.selected_agent,
                selected_tool=response.selected_tool,
                card_count=len(response.cards),
                duplicate_replay=response.duplicate_replay,
                turn_id=response.turn_id,
            )
        return response
