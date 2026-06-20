from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager

import pytest

from app.observability import genai_otel


class FakeSpan:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, object] = {}
        self.events: list[tuple[str, dict[str, object]]] = []
        self.status = None
        self.exceptions: list[BaseException] = []

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: dict[str, object]) -> None:
        self.events.append((name, attributes))

    def set_status(self, status) -> None:
        self.status = status

    def record_exception(self, exc: BaseException) -> None:
        self.exceptions.append(exc)


class FakeTracer:
    def __init__(self) -> None:
        self.spans: list[FakeSpan] = []

    @contextmanager
    def start_as_current_span(self, name: str, kind=None):
        span = FakeSpan(name)
        self.spans.append(span)
        yield span


def _install_fake_tracer(monkeypatch) -> FakeTracer:
    tracer = FakeTracer()
    monkeypatch.setattr(genai_otel, "_get_tracer", lambda: tracer)
    return tracer


def test_genai_tool_span_sets_datadog_tool_mapping_attributes(monkeypatch):
    tracer = _install_fake_tracer(monkeypatch)

    with genai_otel.genai_tool_span(
        "pinecone_catalog_query",
        tool_type="vector_search",
        attributes={"app.filters": {"category": "shoes"}},
    ) as span:
        genai_otel.record_tool_call(
            span,
            arguments={"top_k": 3},
            result=[{"id": "prod_1", "score": 0.92}],
        )

    recorded = tracer.spans[0]
    assert recorded.name == "pinecone_catalog_query"
    assert recorded.attributes["gen_ai.operation.name"] == "execute_tool"
    assert recorded.attributes["gen_ai.tool.name"] == "pinecone_catalog_query"
    assert recorded.attributes["gen_ai.tool.type"] == "vector_search"
    assert json.loads(recorded.attributes["app.filters"]) == {"category": "shoes"}
    event_name, event_attributes = recorded.events[0]
    assert event_name == genai_otel.GENAI_DETAILS_EVENT
    assert json.loads(event_attributes["gen_ai.tool.call.arguments"]) == {"top_k": 3}
    assert json.loads(event_attributes["gen_ai.tool.call.result"]) == [
        {"id": "prod_1", "score": 0.92}
    ]


def test_trace_genai_llm_decorator_supports_instance_model_attributes(monkeypatch):
    tracer = _install_fake_tracer(monkeypatch)

    class ImageAnalyzer:
        model = "gpt-5.5"

        @genai_otel.trace_genai_llm(
            "openai_image_analysis",
            model=lambda self: self.model,
            attributes=lambda self: {"app.schema": "consumer_image_analysis"},
        )
        def analyze(self) -> str:
            return "done"

    assert ImageAnalyzer().analyze() == "done"

    recorded = tracer.spans[0]
    assert recorded.name == "openai_image_analysis"
    assert recorded.attributes["gen_ai.operation.name"] == "generate_content"
    assert recorded.attributes["gen_ai.provider.name"] == "openai"
    assert recorded.attributes["gen_ai.request.model"] == "gpt-5.5"
    assert recorded.attributes["app.schema"] == "consumer_image_analysis"


def test_trace_genai_workflow_decorator_supports_async_functions(monkeypatch):
    tracer = _install_fake_tracer(monkeypatch)

    @genai_otel.trace_genai_workflow(
        "style_finder_image_recommendations",
        attributes={"app.workflow": "style_finder"},
    )
    async def workflow() -> str:
        return "ok"

    assert asyncio.run(workflow()) == "ok"

    recorded = tracer.spans[0]
    assert recorded.name == "style_finder_image_recommendations"
    assert recorded.attributes["gen_ai.operation.name"] == "unknown"
    assert recorded.attributes["app.workflow"] == "style_finder"


def test_suppress_genai_otel_prevents_custom_spans(monkeypatch):
    tracer = _install_fake_tracer(monkeypatch)

    @genai_otel.trace_genai_tool("pinecone_catalog_query")
    def query() -> str:
        span = genai_otel.current_genai_span()
        genai_otel.record_tool_call(span, arguments={"top_k": 3})
        return "ok"

    with genai_otel.suppress_genai_otel():
        assert query() == "ok"

    assert tracer.spans == []


def test_genai_span_records_exceptions(monkeypatch):
    tracer = _install_fake_tracer(monkeypatch)

    with pytest.raises(RuntimeError, match="failed"):
        with genai_otel.genai_embedding_span(
            "openai_text_embeddings",
            model="text-embedding-3-small",
        ):
            raise RuntimeError("failed")

    recorded = tracer.spans[0]
    assert recorded.attributes["gen_ai.operation.name"] == "embedding"
    assert recorded.attributes["error.type"] == "RuntimeError"
    assert isinstance(recorded.exceptions[0], RuntimeError)


def test_genai_span_attaches_app_trace_correlation_when_available(monkeypatch):
    tracer = _install_fake_tracer(monkeypatch)
    monkeypatch.setattr(
        genai_otel,
        "current_api_trace_correlation",
        lambda: {"app.trace_id": "trace_1", "app.span_id": "span_1"},
    )

    with genai_otel.genai_tool_span("catalog_lookup"):
        pass

    assert tracer.spans[0].attributes["app.trace_id"] == "trace_1"
    assert tracer.spans[0].attributes["app.span_id"] == "span_1"
