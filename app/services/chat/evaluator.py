from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings
from app.services.chat.agents import CHAT_AGENT_NAMES, CHAT_TOOL_NAMES, build_chat_intake_agent
from app.services.chat.context import summarize_context
from app.services.chat.schemas import ChatContext
from app.services.chat.triage import SearchConstraints, TriageDecision, triage_chat


TargetAgent = Literal["ProductAgent", "PersonalShopperAgent", "CustomerServiceAgent", "OrderAgent"]
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


class ChatEvaluationConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    colors: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    target_categories: list[str] = Field(default_factory=list)
    exclude_categories: list[str] = Field(default_factory=list)
    order_id: str | None = None


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
    constraints: ChatEvaluationConstraints = Field(default_factory=ChatEvaluationConstraints)
    clarifying_question: str | None = None
    rationale: str


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


def _agent_for_tool(tool: str, fallback_intent: str) -> str:
    if tool == "order_status":
        return CHAT_AGENT_NAMES["order"]
    if tool in {"customer_recommendations", "customer_summary"}:
        return CHAT_AGENT_NAMES["personal_shopper"] if tool == "customer_recommendations" else CHAT_AGENT_NAMES["customer_service"]
    if tool in {"store_info", "service_answer"}:
        return CHAT_AGENT_NAMES["customer_service"]
    if fallback_intent in {"catalog_search", "complementary_products", "product_question"}:
        return CHAT_AGENT_NAMES["product"]
    return CHAT_AGENT_NAMES["customer_service"]


def _fallback_decision(message: str, context: ChatContext, *, source: str, confidence: float = 0.5) -> ChatOrchestrationDecision:
    decision = triage_chat(message, context)
    selected_tool = decision.tool if decision.tool in CHAT_TOOL_NAMES else "chat_response"
    selected_agent = _agent_for_tool(selected_tool, decision.intent)
    requires_auth = decision.requires_customer or selected_tool in {"customer_recommendations", "customer_summary", "order_status"}
    return ChatOrchestrationDecision(
        decision=decision,
        selected_agent=selected_agent,
        selected_tool=selected_tool,
        evaluator_confidence=confidence,
        evaluator_source=source,
        requires_auth=requires_auth,
    )


def _decision_from_evaluation(evaluation: ChatEvaluation, fallback: TriageDecision) -> TriageDecision:
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
        exclude_categories=constraints.exclude_categories or fallback.exclude_categories,
        constraints=SearchConstraints(
            query=constraints.query or fallback.constraints.query,
            budget_min=constraints.budget_min or fallback.constraints.budget_min,
            budget_max=constraints.budget_max or fallback.constraints.budget_max,
            colors=constraints.colors or fallback.constraints.colors,
            materials=constraints.materials or fallback.constraints.materials,
        ),
        use_current_product=fallback.use_current_product,
        tool=evaluation.tool,
    )


def evaluate_chat(message: str, context: ChatContext) -> ChatOrchestrationDecision:
    fallback = triage_chat(message, context)
    settings = get_settings()
    if not settings.openai_api_key:
        return _fallback_decision(message, context, source="deterministic_fallback_no_openai")

    prompt = (
        "Evaluate this storefront chat turn.\n"
        f"Message: {message}\n"
        f"Context: {summarize_context(context)}\n"
        "Return the best route using the provided schema."
    )
    try:
        agent = build_chat_intake_agent(settings.chat_orchestration_model)
        result = agent(prompt, structured_output_model=ChatEvaluation)
        evaluation = result.structured_output
        if not isinstance(evaluation, ChatEvaluation):
            return _fallback_decision(message, context, source="deterministic_fallback_empty_strands")
        if evaluation.confidence < settings.chat_orchestration_min_confidence:
            return _fallback_decision(
                message,
                context,
                source="deterministic_fallback_low_confidence",
                confidence=evaluation.confidence,
            )
        decision = _decision_from_evaluation(evaluation, fallback)
        return ChatOrchestrationDecision(
            decision=decision,
            selected_agent=evaluation.target_agent,
            selected_tool=evaluation.tool,
            evaluator_confidence=evaluation.confidence,
            evaluator_source="ChatIntakeAgent",
            requires_auth=evaluation.requires_auth,
            requires_followup=bool(evaluation.clarifying_question),
            clarifying_question=evaluation.clarifying_question,
        )
    except Exception:
        return _fallback_decision(message, context, source="deterministic_fallback_strands_error")
