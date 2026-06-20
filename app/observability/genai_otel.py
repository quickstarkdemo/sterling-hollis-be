from __future__ import annotations

from collections.abc import Awaitable
from contextlib import contextmanager, nullcontext
import contextvars
from functools import wraps
import inspect
import json
import logging
from typing import Any, Callable, Iterator, Mapping, ParamSpec, TypeVar, cast

from app.api_traces.operations import current_api_trace_correlation

logger = logging.getLogger(__name__)

GENAI_DETAILS_EVENT = "gen_ai.client.inference.operation.details"
GENAI_EVENT = GENAI_DETAILS_EVENT

GENAI_WORKFLOW_OPERATION = "unknown"
GENAI_LLM_OPERATION = "generate_content"
GENAI_EMBEDDING_OPERATION = "embedding"
GENAI_TOOL_OPERATION = "execute_tool"
GENAI_AGENT_OPERATION = "invoke_agent"

TRACER_NAME = "sterling_hollis.llm"

P = ParamSpec("P")
R = TypeVar("R")
SpanValue = Any | Callable[..., Any]
SpanAttributes = Mapping[str, Any] | Callable[..., Mapping[str, Any] | None] | None

_SUPPRESS_GENAI_OTEL: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "suppress_genai_otel",
    default=False,
)


@contextmanager
def suppress_genai_otel() -> Iterator[None]:
    token = _SUPPRESS_GENAI_OTEL.set(True)
    try:
        yield
    finally:
        _SUPPRESS_GENAI_OTEL.reset(token)


def genai_otel_suppressed() -> bool:
    return _SUPPRESS_GENAI_OTEL.get()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)


def _attribute_value(value: Any) -> Any:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, list | tuple) and all(
        item is None or isinstance(item, str | bool | int | float) for item in value
    ):
        return [item for item in value if item is not None]
    return _json_dumps(value)


def _get_tracer() -> Any:
    from opentelemetry import trace

    return trace.get_tracer(TRACER_NAME)


def current_genai_span() -> Any:
    if genai_otel_suppressed():
        return None
    try:
        from opentelemetry import trace

        return trace.get_current_span()
    except Exception:
        logger.debug("OTel current span unavailable", exc_info=True)
        return None


def _resolve_span_value(value: SpanValue, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> Any:
    if not callable(value):
        return value
    try:
        return value(*args, **kwargs)
    except Exception:
        logger.debug("Failed to resolve GenAI span value", exc_info=True)
        return None


def _resolve_span_attributes(
    attributes: SpanAttributes,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    if attributes is None or not callable(attributes):
        return attributes
    try:
        return attributes(*args, **kwargs)
    except Exception:
        logger.debug("Failed to resolve GenAI span attributes", exc_info=True)
        return None


def set_span_attributes(span: Any, attributes: Mapping[str, Any] | None) -> None:
    if span is None or not attributes:
        return
    for key, value in attributes.items():
        if value is None:
            continue
        try:
            span.set_attribute(key, _attribute_value(value))
        except Exception:
            logger.debug("Failed to set OTel span attribute %s", key, exc_info=True)


def add_span_event(
    span: Any,
    attributes: Mapping[str, Any],
    *,
    name: str = GENAI_DETAILS_EVENT,
) -> None:
    if span is None:
        return
    try:
        span.add_event(
            name,
            {key: _attribute_value(value) for key, value in attributes.items()},
        )
    except Exception:
        logger.debug("Failed to add OTel span event %s", name, exc_info=True)


def record_genai_input(span: Any, messages: Any) -> None:
    add_span_event(
        span,
        {"gen_ai.input.messages": messages if isinstance(messages, str) else _json_dumps(messages)},
    )


def record_genai_output(span: Any, messages: Any) -> None:
    add_span_event(
        span,
        {"gen_ai.output.messages": messages if isinstance(messages, str) else _json_dumps(messages)},
    )


def record_tool_call(
    span: Any,
    *,
    arguments: Any | None = None,
    result: Any | None = None,
    description: str | None = None,
) -> None:
    attributes: dict[str, Any] = {}
    if arguments is not None:
        attributes["gen_ai.tool.call.arguments"] = (
            arguments if isinstance(arguments, str) else _json_dumps(arguments)
        )
    if result is not None:
        attributes["gen_ai.tool.call.result"] = (
            result if isinstance(result, str) else _json_dumps(result)
        )
    if description:
        attributes["gen_ai.tool.description"] = description
    add_span_event(span, attributes)


def _set_span_error(span: Any, exc: BaseException) -> None:
    if span is None:
        return
    set_span_attributes(span, {"error.type": type(exc).__name__})
    try:
        from opentelemetry.trace import Status, StatusCode

        span.set_status(Status(StatusCode.ERROR, str(exc)))
    except Exception:
        logger.debug("Failed to set OTel span error status", exc_info=True)
    try:
        span.record_exception(exc)
    except Exception:
        logger.debug("Failed to record OTel span exception", exc_info=True)


@contextmanager
def genai_span(
    name: str,
    *,
    operation: str,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Any]:
    if genai_otel_suppressed():
        yield None
        return

    try:
        from opentelemetry import trace

        span_context = _get_tracer().start_as_current_span(
            name,
            kind=trace.SpanKind.INTERNAL,
        )
    except Exception:
        logger.debug("OTel tracer unavailable for GenAI span %s", name, exc_info=True)
        span_context = nullcontext(None)

    with span_context as span:
        set_span_attributes(span, {"gen_ai.operation.name": operation})
        set_span_attributes(span, attributes)
        set_span_attributes(span, current_api_trace_correlation())
        try:
            yield span
        except Exception as exc:
            _set_span_error(span, exc)
            raise


@contextmanager
def genai_workflow_span(
    name: str,
    *,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Any]:
    with genai_span(
        name,
        operation=GENAI_WORKFLOW_OPERATION,
        attributes=attributes,
    ) as span:
        yield span


@contextmanager
def genai_llm_span(
    name: str,
    *,
    model: str | None,
    provider: str = "openai",
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Any]:
    span_attributes = {
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": model,
        **dict(attributes or {}),
    }
    with genai_span(name, operation=GENAI_LLM_OPERATION, attributes=span_attributes) as span:
        yield span


@contextmanager
def genai_embedding_span(
    name: str,
    *,
    model: str | None,
    provider: str = "openai",
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Any]:
    span_attributes = {
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": model,
        **dict(attributes or {}),
    }
    with genai_span(
        name,
        operation=GENAI_EMBEDDING_OPERATION,
        attributes=span_attributes,
    ) as span:
        yield span


@contextmanager
def genai_tool_span(
    name: str,
    *,
    tool_type: str = "function",
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Any]:
    span_attributes = {
        "gen_ai.tool.name": name,
        "gen_ai.tool.type": tool_type,
        **dict(attributes or {}),
    }
    with genai_span(name, operation=GENAI_TOOL_OPERATION, attributes=span_attributes) as span:
        yield span


@contextmanager
def genai_agent_span(
    name: str,
    *,
    agent_name: str | None = None,
    provider: str = "openai",
    model: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Any]:
    span_attributes = {
        "gen_ai.agent.name": agent_name or name,
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": model,
        **dict(attributes or {}),
    }
    with genai_span(name, operation=GENAI_AGENT_OPERATION, attributes=span_attributes) as span:
        yield span


def _decorate_with_span(
    func: Callable[P, R],
    span_factory: Callable[..., Any],
) -> Callable[P, R]:
    if inspect.iscoroutinefunction(func):

        @wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
            with span_factory(*args, **kwargs):
                return await cast(Callable[P, Awaitable[Any]], func)(*args, **kwargs)

        return cast(Callable[P, R], async_wrapper)

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with span_factory(*args, **kwargs):
            return func(*args, **kwargs)

    return wrapper


def trace_genai_workflow(
    name: str,
    *,
    attributes: SpanAttributes = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        return _decorate_with_span(
            func,
            lambda *args, **kwargs: genai_workflow_span(
                name,
                attributes=_resolve_span_attributes(attributes, args, kwargs),
            ),
        )

    return decorator


def trace_genai_llm(
    name: str,
    *,
    model: SpanValue = None,
    provider: SpanValue = "openai",
    attributes: SpanAttributes = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        return _decorate_with_span(
            func,
            lambda *args, **kwargs: genai_llm_span(
                name,
                model=_resolve_span_value(model, args, kwargs),
                provider=_resolve_span_value(provider, args, kwargs) or "custom",
                attributes=_resolve_span_attributes(attributes, args, kwargs),
            ),
        )

    return decorator


def trace_genai_embedding(
    name: str,
    *,
    model: SpanValue = None,
    provider: SpanValue = "openai",
    attributes: SpanAttributes = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        return _decorate_with_span(
            func,
            lambda *args, **kwargs: genai_embedding_span(
                name,
                model=_resolve_span_value(model, args, kwargs),
                provider=_resolve_span_value(provider, args, kwargs) or "custom",
                attributes=_resolve_span_attributes(attributes, args, kwargs),
            ),
        )

    return decorator


def trace_genai_tool(
    name: str,
    *,
    tool_type: SpanValue = "function",
    attributes: SpanAttributes = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        return _decorate_with_span(
            func,
            lambda *args, **kwargs: genai_tool_span(
                name,
                tool_type=_resolve_span_value(tool_type, args, kwargs) or "function",
                attributes=_resolve_span_attributes(attributes, args, kwargs),
            ),
        )

    return decorator


def trace_genai_agent(
    name: str,
    *,
    agent_name: SpanValue = None,
    provider: SpanValue = "openai",
    model: SpanValue = None,
    attributes: SpanAttributes = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        return _decorate_with_span(
            func,
            lambda *args, **kwargs: genai_agent_span(
                name,
                agent_name=_resolve_span_value(agent_name, args, kwargs),
                provider=_resolve_span_value(provider, args, kwargs) or "custom",
                model=_resolve_span_value(model, args, kwargs),
                attributes=_resolve_span_attributes(attributes, args, kwargs),
            ),
        )

    return decorator
