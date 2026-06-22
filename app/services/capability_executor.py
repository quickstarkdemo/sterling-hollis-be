from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TypeVar

from app.api_traces.operations import api_trace_operation
from app.services.auth.clerk import AuthenticatedPrincipal, ChatIdentity
from app.services.capabilities import (
    Capability,
    Persona,
    Surface,
    capability_allowed_for_personas,
    get_capability,
)
from app.services.capability_tracing import (
    annotate_capability_span,
    capability_trace_attributes,
)


T = TypeVar("T")


CHAT_TOOL_CAPABILITIES = {
    "semantic_catalog_search": "public.catalog.search",
    "search_catalog": "public.catalog.search",
    "product_detail": "public.catalog.product_detail",
    "get_current_product": "public.catalog.product_detail",
    "get_product_detail": "public.catalog.product_detail",
    "related_products": "public.catalog.recommendations",
    "find_related_products": "public.catalog.recommendations",
    "customer_recommendations": "shopper.account.recommendations",
    "order_status": "shopper.account.order_status",
    "customer_summary": "shopper.account.order_status",
    "store_info": "shopper.chat.turn",
    "service_answer": "shopper.chat.turn",
    "chat_response": "shopper.chat.turn",
    "strands_agent": "shopper.chat.turn",
}


@dataclass(frozen=True)
class CapabilityExecutionContext:
    capability_id: str
    personas: tuple[Persona, ...]
    surface: Surface
    selected_tool: str | None = None
    selected_agent: str | None = None
    actor_id: str | None = None
    session_id: str | None = None
    approval_state: Mapping[str, object] = field(default_factory=dict)
    attributes: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CapabilityExecutionResult:
    output: object
    capability: Capability
    persona: Persona
    surface: Surface
    selected_tool: str | None
    selected_agent: str | None
    status: str


class CapabilityExecutionDenied(PermissionError):
    pass


def chat_capability_id_for_tool(selected_tool: str | None) -> str:
    return CHAT_TOOL_CAPABILITIES.get(selected_tool or "", "shopper.chat.turn")


def personas_for_chat_identity(identity: ChatIdentity) -> tuple[Persona, ...]:
    if identity.status == "authenticated_customer":
        return (Persona.AUTHENTICATED_SHOPPER,)
    return (Persona.SHOPPER,)


def personas_for_catalog_admin(_: AuthenticatedPrincipal) -> tuple[Persona, ...]:
    return (Persona.CATALOG_ADMIN,)


def execute_capability(
    context: CapabilityExecutionContext,
    handler: Callable[[], T],
) -> CapabilityExecutionResult:
    capability = get_capability(context.capability_id)
    if context.surface not in capability.surfaces:
        raise CapabilityExecutionDenied(
            f"{capability.id} is not exposed on {context.surface.value}"
        )
    if not capability_allowed_for_personas(capability, context.personas):
        raise CapabilityExecutionDenied(
            f"{context.personas[0].value if context.personas else 'unknown'} cannot execute {capability.id}"
        )
    if capability.requires_approval:
        approved = bool(context.approval_state.get(capability.approval_field or ""))
        if not approved:
            raise CapabilityExecutionDenied(f"{capability.id} requires explicit approval")

    persona = context.personas[0]
    status = "succeeded"
    output: T
    try:
        with api_trace_operation(
            "Execute capability",
            "capability.execute",
            attributes=capability_trace_attributes(
                capability=capability,
                persona=persona,
                surface=context.surface,
                selected_tool=context.selected_tool,
                selected_agent=context.selected_agent,
                actor_id=context.actor_id,
                session_id=context.session_id,
                approval_state=context.approval_state,
                attributes=context.attributes,
            ),
        ) as trace_span:
            output = handler()
            annotate_capability_span(trace_span, output=output, status=status)
    except Exception:
        status = "failed"
        raise

    return CapabilityExecutionResult(
        output=output,
        capability=capability,
        persona=persona,
        surface=context.surface,
        selected_tool=context.selected_tool,
        selected_agent=context.selected_agent,
        status=status,
    )
