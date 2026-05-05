from __future__ import annotations

from contextlib import contextmanager, nullcontext
from functools import wraps
import json
import logging
import os
from typing import Any, Callable, Iterator, Mapping, MutableMapping, TypeVar

from app.services.chat.schemas import ChatContext


logger = logging.getLogger(__name__)

GENAI_DETAILS_EVENT = "gen_ai.client.inference.operation.details"
GENAI_AGENT_OPERATION = "invoke_agent"
GENAI_AGENT_NAME = "ChatIntakeAgent"
GENAI_PROVIDER = "openai"

_BOOTSTRAPPED = False
_BOOTSTRAP_ERROR: str | None = None

F = TypeVar("F", bound=Callable[..., Any])


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _default_otlp_endpoint(site: str | None) -> str:
    normalized_site = (site or "datadoghq.com").strip() or "datadoghq.com"
    return f"https://otlp.{normalized_site}/v1/traces"


def _merge_otlp_headers(existing: str | None, api_key: str | None) -> str:
    parts = [part.strip() for part in (existing or "").split(",") if part.strip()]
    keys = {part.split("=", 1)[0].strip().lower() for part in parts if "=" in part}
    if api_key and "dd-api-key" not in keys:
        parts.append(f"dd-api-key={api_key}")
    if "dd-otlp-source" not in keys:
        parts.append("dd-otlp-source=llmobs")
    return ",".join(parts)


def configure_llm_otel_environment(env: MutableMapping[str, str] | None = None) -> dict[str, str]:
    target_env = env if env is not None else os.environ
    target_env.setdefault(
        "OTEL_SERVICE_NAME",
        target_env.get("DD_LLMOBS_ML_APP") or target_env.get("DD_SERVICE") or "sterling-hollis-be",
    )
    target_env.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
    target_env.setdefault("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "http/protobuf")
    target_env.setdefault(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        _default_otlp_endpoint(target_env.get("DD_SITE")),
    )
    headers = _merge_otlp_headers(target_env.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS"), target_env.get("DD_API_KEY"))
    if headers:
        target_env["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] = headers
    return {
        "OTEL_SERVICE_NAME": target_env.get("OTEL_SERVICE_NAME", ""),
        "OTEL_SEMCONV_STABILITY_OPT_IN": target_env.get("OTEL_SEMCONV_STABILITY_OPT_IN", ""),
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": target_env.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", ""),
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": target_env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", ""),
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS": target_env.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS", ""),
    }


def initialize_llm_otel() -> bool:
    global _BOOTSTRAPPED, _BOOTSTRAP_ERROR

    if _BOOTSTRAPPED:
        return True
    if not _truthy(os.environ.get("DD_LLMOBS_ENABLED")):
        return False
    if not _truthy(os.environ.get("STRANDS_OTEL_ENABLED")):
        return False
    if not os.environ.get("DD_API_KEY") and "dd-api-key=" not in os.environ.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS", ""):
        _BOOTSTRAP_ERROR = "DD_API_KEY is not configured for Datadog LLM OTel export."
        logger.warning(_BOOTSTRAP_ERROR)
        return False

    configure_llm_otel_environment()
    try:
        from strands.telemetry.config import StrandsTelemetry

        StrandsTelemetry().setup_otlp_exporter()
        _BOOTSTRAPPED = True
        _BOOTSTRAP_ERROR = None
        logger.info("Datadog LLM OpenTelemetry export initialized.")
    except Exception as exc:  # pragma: no cover - defensive startup path
        _BOOTSTRAP_ERROR = f"{type(exc).__name__}: {exc}"
        logger.warning("Datadog LLM OpenTelemetry export was not initialized: %s", _BOOTSTRAP_ERROR)
        return False
    return True


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)


def _current_product_id(context: ChatContext) -> str | None:
    return context.current_product.id if context.current_product else context.product_id


def _span_set_attribute(span: Any, key: str, value: Any) -> None:
    if value is None or span is None:
        return
    try:
        span.set_attribute(key, value)
    except Exception:
        logger.debug("Failed to set OTel span attribute %s", key, exc_info=True)


def _span_add_event(span: Any, name: str, attributes: Mapping[str, Any]) -> None:
    if span is None:
        return
    try:
        span.add_event(name, dict(attributes))
    except Exception:
        logger.debug("Failed to add OTel span event %s", name, exc_info=True)


def _set_chat_agent_request_attributes(span: Any, *, message: str, context: ChatContext, model: str) -> None:
    _span_set_attribute(span, "gen_ai.operation.name", GENAI_AGENT_OPERATION)
    _span_set_attribute(span, "gen_ai.provider.name", GENAI_PROVIDER)
    _span_set_attribute(span, "gen_ai.request.model", model)
    _span_set_attribute(span, "gen_ai.agent.name", GENAI_AGENT_NAME)
    _span_set_attribute(span, "app.chat.page_type", context.page_type)
    _span_set_attribute(span, "app.chat.route", context.route)
    _span_set_attribute(span, "app.chat.store_id", context.store_id)
    _span_set_attribute(span, "app.chat.category", context.category)
    _span_set_attribute(span, "app.chat.current_product_id", _current_product_id(context))
    _span_add_event(
        span,
        GENAI_DETAILS_EVENT,
        {
            "gen_ai.input.messages": _json_dumps(
                [{"role": "user", "parts": [{"type": "text", "content": message}]}]
            )
        },
    )


def record_chat_agent_result(span: Any, decision: Any) -> None:
    _span_set_attribute(span, "app.chat.selected_agent", getattr(decision, "selected_agent", None))
    _span_set_attribute(span, "app.chat.selected_tool", getattr(decision, "selected_tool", None))
    _span_set_attribute(span, "app.chat.evaluator_source", getattr(decision, "evaluator_source", None))
    _span_set_attribute(span, "app.chat.evaluator_confidence", getattr(decision, "evaluator_confidence", None))
    _span_set_attribute(span, "app.chat.requires_auth", getattr(decision, "requires_auth", None))
    _span_set_attribute(span, "app.chat.requires_followup", getattr(decision, "requires_followup", None))
    nested_decision = getattr(decision, "decision", None)
    _span_set_attribute(span, "app.chat.intent", getattr(nested_decision, "intent", None))
    _span_set_attribute(span, "app.chat.route_kind", getattr(nested_decision, "route", None))
    evaluator_error = getattr(decision, "evaluator_error", None)
    if evaluator_error:
        _span_set_attribute(span, "error.type", evaluator_error)
        _set_span_error_status(span, evaluator_error)
    _span_add_event(
        span,
        GENAI_DETAILS_EVENT,
        {
            "gen_ai.output.messages": _json_dumps(
                [
                    {
                        "role": "assistant",
                        "parts": [
                            {
                                "type": "text",
                                "content": _json_dumps(
                                    {
                                        "selected_agent": getattr(decision, "selected_agent", None),
                                        "selected_tool": getattr(decision, "selected_tool", None),
                                        "confidence": getattr(decision, "evaluator_confidence", None),
                                        "source": getattr(decision, "evaluator_source", None),
                                        "error": evaluator_error,
                                    }
                                ),
                            }
                        ],
                    }
                ]
            )
        },
    )


def _set_span_error_status(span: Any, description: str) -> None:
    if span is None:
        return
    try:
        from opentelemetry.trace import Status, StatusCode

        span.set_status(Status(StatusCode.ERROR, description=description))
    except Exception:
        logger.debug("Failed to set OTel span error status", exc_info=True)


def _record_span_exception(span: Any, exc: BaseException) -> None:
    _span_set_attribute(span, "error.type", type(exc).__name__)
    _set_span_error_status(span, str(exc))
    if span is None:
        return
    try:
        span.record_exception(exc)
    except Exception:
        logger.debug("Failed to record OTel span exception", exc_info=True)


@contextmanager
def chat_agent_span(*, message: str, context: ChatContext, model: str) -> Iterator[Any]:
    try:
        from opentelemetry import trace

        tracer = trace.get_tracer("sterling_hollis.llm")
        span_context = tracer.start_as_current_span(GENAI_AGENT_NAME, kind=trace.SpanKind.INTERNAL)
    except Exception:
        logger.debug("OTel tracer unavailable for chat agent span", exc_info=True)
        span_context = nullcontext(None)

    with span_context as span:
        _set_chat_agent_request_attributes(span, message=message, context=context, model=model)
        yield span


def trace_chat_agent_evaluation(func: F) -> F:
    @wraps(func)
    def wrapper(message: str, context: ChatContext, *, history: list[dict[str, str]] | None = None) -> Any:
        from app.config import get_settings

        settings = get_settings()
        with chat_agent_span(message=message, context=context, model=settings.chat_orchestration_model) as span:
            try:
                decision = func(message, context, history=history)
            except Exception as exc:
                _record_span_exception(span, exc)
                raise
            record_chat_agent_result(span, decision)
            return decision

    return wrapper  # type: ignore[return-value]
