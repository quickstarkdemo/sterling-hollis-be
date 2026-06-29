from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.api_traces.operations import ApiTraceSpanHandle
from app.services.capabilities import Capability, Persona, Surface


def _compact(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, {}, [])}


def _count_sequence(value: object) -> int | None:
    if isinstance(value, (str, bytes, bytearray)):
        return None
    if isinstance(value, Mapping):
        return None
    try:
        return len(value)  # type: ignore[arg-type]
    except TypeError:
        return None


def capability_result_count(output: object) -> int | None:
    """Best-effort cardinality without copying payloads into trace attributes."""

    if output is None:
        return 0

    structured = getattr(output, "structuredContent", None)
    if isinstance(structured, Mapping):
        return capability_result_count(structured)

    if isinstance(output, Mapping):
        for key in ("count", "total", "result_count"):
            value = output.get(key)
            if isinstance(value, int):
                return value
        payload = output.get("payload")
        if isinstance(payload, Mapping):
            nested = capability_result_count(payload)
            if nested is not None:
                return nested
        for key in ("items", "products", "cards", "recommendations", "rows"):
            count = _count_sequence(output.get(key))
            if count is not None:
                return count
        return None

    if hasattr(output, "model_dump"):
        try:
            return capability_result_count(output.model_dump(mode="json"))
        except Exception:
            return None

    return _count_sequence(output)


def capability_trace_attributes(
    *,
    capability: Capability,
    persona: Persona,
    surface: Surface,
    selected_tool: str | None = None,
    selected_agent: str | None = None,
    actor_id: str | None = None,
    session_id: str | None = None,
    approval_state: Mapping[str, object] | None = None,
    attributes: Mapping[str, object] | None = None,
) -> dict[str, object]:
    approval_field = capability.approval_field
    approval_granted = (
        bool((approval_state or {}).get(approval_field))
        if approval_field
        else None
    )
    return _compact(
        {
            "capability_id": capability.id,
            "capability_name": capability.name,
            "capability_operation": capability.operation.value,
            "capability_side_effect": capability.side_effect.value,
            "persona": persona.value,
            "surface": surface.value,
            "selected_tool": selected_tool,
            "selected_agent": selected_agent,
            "actor_id": actor_id,
            "session_id": session_id,
            "approval_required": capability.requires_approval,
            "approval_field": approval_field,
            "approval_granted": approval_granted,
            **dict(attributes or {}),
        }
    )


def capability_result_trace_attributes(output: object, *, status: str) -> dict[str, object]:
    selected_agent = getattr(output, "selected_agent", None)
    selected_tool = getattr(output, "selected_tool", None)
    agent_mode = getattr(output, "agent_mode", None)
    fallback_reason = getattr(output, "fallback_reason", None)
    if isinstance(output, Mapping):
        selected_agent = selected_agent or output.get("selected_agent")
        selected_tool = selected_tool or output.get("selected_tool")
        agent_mode = agent_mode or output.get("agent_mode")
        fallback_reason = fallback_reason or output.get("fallback_reason")
    return _compact(
        {
            "status": status,
            "result_type": type(output).__name__ if output is not None else None,
            "result_count": capability_result_count(output),
            "selected_agent": selected_agent,
            "selected_tool": selected_tool,
            "agent_mode": agent_mode,
            "fallback_reason": fallback_reason,
        }
    )


def annotate_capability_span(
    span: ApiTraceSpanHandle | None,
    *,
    output: object,
    status: str,
) -> None:
    if span is None:
        return
    span.annotate(capability_result_trace_attributes(output, status=status))
