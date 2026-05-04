from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import get_settings
from app.services.chat.agents import (
    CHAT_AGENT_NAMES,
    CHAT_TOOL_NAMES,
    INTAKE_SYSTEM_PROMPT,
)
from app.services.chat.context import summarize_context
from app.services.chat.schemas import ChatContext
from app.services.chat.triage import SearchConstraints, TriageDecision, triage_chat

from ddtrace.llmobs import LLMObs


logger = logging.getLogger(__name__)

TargetAgent = Literal[
    "ProductAgent", "PersonalShopperAgent", "CustomerServiceAgent", "OrderAgent"
]
TargetTool = Literal[
    "product_detail",
    "semantic_catalog_search",
    "related_products",
    "customer_recommendations",
    "customer_summary",
    "store_info",
    "service_answer",
    "order_status",
    "chat_response",
]


def _llmobs_annotate_safe(**kwargs) -> None:
    try:
        if not LLMObs.enabled:
            return
        LLMObs.annotate(**kwargs)
    except Exception:
        logger.debug("Failed to annotate Datadog LLMObs span", exc_info=True)


def _constraints_summary(decision: TriageDecision) -> dict:
    constraints = decision.constraints
    return {
        "query": constraints.query,
        "budget_min": constraints.budget_min,
        "budget_max": constraints.budget_max,
        "colors": constraints.colors,
        "materials": constraints.materials,
        "target_genders": constraints.target_genders,
        "target_categories": decision.target_categories,
        "exclude_categories": decision.exclude_categories,
        "use_current_product": decision.use_current_product,
    }


def _record_fallback_task(
    message: str,
    context: ChatContext,
    *,
    source: str,
    confidence: float = 0.5,
    error: str | None = None,
    session_id: str | None = None,
) -> ChatOrchestrationDecision:
    with LLMObs.task(name="deterministic_triage_fallback", session_id=session_id) as fallback_span:
        _llmobs_annotate_safe(
            span=fallback_span,
            input_data={
                "message": message,
                "context": summarize_context(context),
                "source": source,
                "confidence": confidence,
                "error": error,
            },
            tags={
                "workflow": "chat",
                "agent": "ChatIntakeAgent",
                "fallback_source": source,
            },
        )
        decision_result = _fallback_decision(
            message,
            context,
            source=source,
            confidence=confidence,
            error=error,
        )
        _llmobs_annotate_safe(
            span=fallback_span,
            output_data={
                "selected_agent": decision_result.selected_agent,
                "selected_tool": decision_result.selected_tool,
                "confidence": decision_result.evaluator_confidence,
                "requires_auth": decision_result.requires_auth,
                "requires_followup": decision_result.requires_followup,
                "intent": decision_result.decision.intent,
                "route": decision_result.decision.route,
                "constraints": _constraints_summary(decision_result.decision),
            },
        )
        return decision_result


class ChatEvaluationConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    colors: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    target_genders: list[str] = Field(default_factory=list)
    target_categories: list[str] = Field(default_factory=list)
    exclude_categories: list[str] = Field(default_factory=list)
    order_id: str | None = None

    @field_validator(
        "colors",
        "materials",
        "target_categories",
        "exclude_categories",
        mode="before",
    )
    @classmethod
    def _empty_list_for_null(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            clean = value.strip()
            return [clean] if clean else []
        return value


class ChatEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Literal[
        "catalog_search",
        "complementary_products",
        "product_question",
        "account_question",
        "customer_recommendation",
        "general_style",
    ]
    target_agent: TargetAgent
    tool: TargetTool
    confidence: float = Field(ge=0.0, le=1.0)
    requires_auth: bool
    constraints: ChatEvaluationConstraints = Field(
        default_factory=ChatEvaluationConstraints
    )
    clarifying_question: str | None = None
    rationale: str


def _run_chat_intake_llm(prompt: str, *, model: str) -> ChatEvaluation:
    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover - dependency import guard
        raise RuntimeError("OpenAI SDK is not available for chat orchestration.") from exc

    settings = get_settings()
    client = OpenAI(api_key=settings.openai_api_key)
    completion = client.beta.chat.completions.parse(
        model=model,
        messages=[
            {"role": "system", "content": INTAKE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format=ChatEvaluation,
    )
    parsed = completion.choices[0].message.parsed
    if not isinstance(parsed, ChatEvaluation):
        raise RuntimeError("OpenAI chat intake returned no parsed ChatEvaluation.")
    return parsed


@dataclass(frozen=True)
class ChatOrchestrationDecision:
    decision: TriageDecision
    selected_agent: str
    selected_tool: str
    evaluator_confidence: float
    evaluator_source: str
    requires_auth: bool
    requires_followup: bool = False
    clarifying_question: str | None = None
    evaluator_error: str | None = None


def _agent_for_tool(tool: str, fallback_intent: str) -> str:
    if tool == "order_status":
        return CHAT_AGENT_NAMES["order"]
    if tool in {"customer_recommendations", "customer_summary"}:
        return (
            CHAT_AGENT_NAMES["personal_shopper"]
            if tool == "customer_recommendations"
            else CHAT_AGENT_NAMES["customer_service"]
        )
    if tool in {"store_info", "service_answer"}:
        return CHAT_AGENT_NAMES["customer_service"]
    if fallback_intent in {
        "catalog_search",
        "complementary_products",
        "product_question",
    }:
        return CHAT_AGENT_NAMES["product"]
    return CHAT_AGENT_NAMES["customer_service"]


def _fallback_followup_question(decision: TriageDecision) -> str | None:
    if decision.reason == "ambiguous pairing request":
        return "Which item would you like styling suggestions for?"
    if decision.reason == "ambiguous product question":
        return "Which product would you like me to check?"
    return None


def _fallback_decision(
    message: str,
    context: ChatContext,
    *,
    source: str,
    confidence: float = 0.5,
    error: str | None = None,
) -> ChatOrchestrationDecision:
    decision = triage_chat(message, context)
    selected_tool = (
        decision.tool if decision.tool in CHAT_TOOL_NAMES else "chat_response"
    )
    selected_agent = _agent_for_tool(selected_tool, decision.intent)
    requires_auth = decision.requires_customer or selected_tool in {
        "customer_recommendations",
        "customer_summary",
        "order_status",
    }
    clarifying_question = _fallback_followup_question(decision)
    return ChatOrchestrationDecision(
        decision=decision,
        selected_agent=selected_agent,
        selected_tool=selected_tool,
        evaluator_confidence=confidence,
        evaluator_source=source,
        requires_auth=requires_auth,
        requires_followup=bool(clarifying_question),
        clarifying_question=clarifying_question,
        evaluator_error=error,
    )


def _decision_from_evaluation(
    evaluation: ChatEvaluation, fallback: TriageDecision
) -> TriageDecision:
    constraints = evaluation.constraints
    route = fallback.route
    if evaluation.tool == "semantic_catalog_search":
        route = "semantic_catalog_search"
    elif evaluation.tool == "chat_response":
        route = "agentic_response"
    else:
        route = "simple_tool"

    return TriageDecision(
        intent=evaluation.intent,
        route=route,
        reason=f"strands:{evaluation.rationale}",
        requires_customer=evaluation.requires_auth,
        target_categories=constraints.target_categories or fallback.target_categories,
        exclude_categories=constraints.exclude_categories
        or fallback.exclude_categories,
        constraints=SearchConstraints(
            query=constraints.query or fallback.constraints.query,
            budget_min=constraints.budget_min or fallback.constraints.budget_min,
            budget_max=constraints.budget_max or fallback.constraints.budget_max,
            colors=constraints.colors or fallback.constraints.colors,
            materials=constraints.materials or fallback.constraints.materials,
            target_genders=constraints.target_genders or fallback.constraints.target_genders,
        ),
        use_current_product=fallback.use_current_product,
        tool=evaluation.tool,
    )


def evaluate_chat(
    message: str,
    context: ChatContext,
    *,
    history: list[dict[str, str]] | None = None,
    session_id: str | None = None,
) -> ChatOrchestrationDecision:
    fallback = triage_chat(message, context)
    settings = get_settings()

    history_lines = "\n".join(
        f"- {turn.get('role', 'unknown')}: {turn.get('content', '')}"
        for turn in (history or [])[-8:]
        if turn.get("content")
    )
    prompt = (
        "Evaluate this storefront chat turn.\n"
        f"Recent conversation turns:\n{history_lines or '- (none)'}\n"
        f"Message: {message}\n"
        f"Context: {summarize_context(context)}\n"
        "Return the best route using the provided schema."
    )

    with LLMObs.workflow(name="chat_intake", session_id=session_id) as intake_span:
        _llmobs_annotate_safe(
            span=intake_span,
            input_data={
                "message": message,
                "history_count": len(history or []),
                "context": summarize_context(context),
            },
            tags={
                "workflow": "chat",
                "agent": "ChatIntakeAgent",
            },
        )

        if not settings.openai_api_key:
            decision_result = _record_fallback_task(
                message,
                context,
                source="deterministic_fallback_no_openai",
                session_id=session_id,
            )
        else:
            try:
                with LLMObs.llm(
                    name="chat_intake_llm_call",
                    model_name=settings.chat_orchestration_model,
                    model_provider="openai",
                    session_id=session_id,
                ) as llm_span:
                    _llmobs_annotate_safe(
                        span=llm_span,
                        input_data=[
                            {
                                "role": "user",
                                "content": prompt,
                            }
                        ],
                        metadata={
                            "structured_output_model": "ChatEvaluation",
                            "history_count": len(history or []),
                            "fallback_intent": fallback.intent,
                            "fallback_route": fallback.route,
                            "fallback_tool": fallback.tool,
                        },
                        tags={
                            "workflow": "chat",
                            "agent": "ChatIntakeAgent",
                        },
                    )

                    evaluation = _run_chat_intake_llm(
                        prompt,
                        model=settings.chat_orchestration_model,
                    )

                    if isinstance(evaluation, ChatEvaluation):
                        _llmobs_annotate_safe(
                            span=llm_span,
                            output_data={
                                "intent": evaluation.intent,
                                "target_agent": evaluation.target_agent,
                                "tool": evaluation.tool,
                                "confidence": evaluation.confidence,
                                "requires_auth": evaluation.requires_auth,
                                "constraints": evaluation.constraints.model_dump(mode="json"),
                                "clarifying_question": evaluation.clarifying_question,
                                "rationale": evaluation.rationale,
                            },
                        )
                    else:
                        _llmobs_annotate_safe(
                            span=llm_span,
                            output_data={
                                "error": "structured_output_missing_or_invalid",
                                "output_type": type(evaluation).__name__,
                            },
                        )

                if not isinstance(evaluation, ChatEvaluation):
                    decision_result = _record_fallback_task(
                        message,
                        context,
                        source="deterministic_fallback_empty_strands",
                        session_id=session_id,
                    )
                elif evaluation.confidence < settings.chat_orchestration_min_confidence:
                    decision_result = _record_fallback_task(
                        message,
                        context,
                        source="deterministic_fallback_low_confidence",
                        confidence=evaluation.confidence,
                        session_id=session_id,
                    )
                else:
                    fallback_question = _fallback_followup_question(fallback)

                    if fallback_question:
                        decision_result = ChatOrchestrationDecision(
                            decision=fallback,
                            selected_agent=CHAT_AGENT_NAMES["customer_service"],
                            selected_tool="chat_response",
                            evaluator_confidence=evaluation.confidence,
                            evaluator_source="ChatIntakeAgent",
                            requires_auth=False,
                            requires_followup=True,
                            clarifying_question=(
                                evaluation.clarifying_question or fallback_question
                            ),
                        )
                    else:
                        decision = _decision_from_evaluation(evaluation, fallback)
                        decision_result = ChatOrchestrationDecision(
                            decision=decision,
                            selected_agent=evaluation.target_agent,
                            selected_tool=evaluation.tool,
                            evaluator_confidence=evaluation.confidence,
                            evaluator_source="ChatIntakeAgent",
                            requires_auth=evaluation.requires_auth,
                            requires_followup=bool(evaluation.clarifying_question),
                            clarifying_question=evaluation.clarifying_question,
                        )

            except Exception as exc:
                error = type(exc).__name__
                logger.exception("Chat evaluator failed; falling back to deterministic triage")
                decision_result = _record_fallback_task(
                    message,
                    context,
                    source=f"deterministic_fallback_strands_error:{error}",
                    error=error,
                    session_id=session_id,
                )

        if "llm_span" in locals():
            _llmobs_annotate_safe(
                span=llm_span,
                metadata={
                    "selected_agent": decision_result.selected_agent,
                    "selected_tool": decision_result.selected_tool,
                    "confidence": decision_result.evaluator_confidence,
                    "source": decision_result.evaluator_source,
                    "route": decision_result.decision.route,
                    "intent": decision_result.decision.intent,
                    "requires_auth": decision_result.requires_auth,
                    "requires_followup": decision_result.requires_followup,
                    "constraints": _constraints_summary(decision_result.decision),
                },
                tags={
                    "selected_agent": decision_result.selected_agent,
                    "selected_tool": decision_result.selected_tool,
                    "route": decision_result.decision.route,
                    "intent": decision_result.decision.intent,
                },
            )

        _llmobs_annotate_safe(
            span=intake_span,
            output_data={
                "selected_agent": decision_result.selected_agent,
                "selected_tool": decision_result.selected_tool,
                "confidence": decision_result.evaluator_confidence,
                "source": decision_result.evaluator_source,
                "requires_auth": decision_result.requires_auth,
                "requires_followup": decision_result.requires_followup,
                "clarifying_question": decision_result.clarifying_question,
                "error": decision_result.evaluator_error,
                "intent": decision_result.decision.intent,
                "route": decision_result.decision.route,
                "constraints": _constraints_summary(decision_result.decision),
            },
            metadata={
                "fallback_reason": fallback.reason,
                "agent": "ChatIntakeAgent",
            },
        )

        return decision_result
