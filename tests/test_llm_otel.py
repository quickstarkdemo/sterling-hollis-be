from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.observability import llm_otel
from app.services.chat.evaluator import ChatOrchestrationDecision
from app.services.chat.schemas import ChatContext, ChatCurrentProduct
from app.services.chat.triage import TriageDecision


class FakeSpan:
    def __init__(self) -> None:
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


def test_configure_llm_otel_environment_derives_datadog_headers():
    env = {
        "DD_API_KEY": "test-api-key",
        "DD_SITE": "datadoghq.com",
        "DD_LLMOBS_ML_APP": "sterling-hollis-be",
    }

    configured = llm_otel.configure_llm_otel_environment(env)

    assert configured["OTEL_SERVICE_NAME"] == "sterling-hollis-be"
    assert configured["OTEL_SEMCONV_STABILITY_OPT_IN"] == "gen_ai_latest_experimental"
    assert configured["OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"] == "http/protobuf"
    assert configured["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == "https://otlp.datadoghq.com/v1/traces"
    assert configured["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] == "dd-api-key=test-api-key,dd-otlp-source=llmobs"


def test_initialize_llm_otel_is_idempotent(monkeypatch):
    calls: list[str] = []

    class FakeTelemetry:
        def __init__(self) -> None:
            calls.append("init")

        def setup_otlp_exporter(self):
            calls.append("setup")
            return self

    import strands.telemetry.config as strands_config

    monkeypatch.setattr(llm_otel, "_BOOTSTRAPPED", False)
    monkeypatch.setattr(llm_otel, "_BOOTSTRAP_ERROR", None)
    monkeypatch.setattr(strands_config, "StrandsTelemetry", FakeTelemetry)
    monkeypatch.setenv("DD_LLMOBS_ENABLED", "true")
    monkeypatch.setenv("DD_API_KEY", "test-api-key")
    monkeypatch.setenv("DD_SITE", "datadoghq.com")

    assert llm_otel.initialize_llm_otel() is True
    assert llm_otel.initialize_llm_otel() is True
    assert calls == ["init", "setup"]


def test_record_chat_agent_result_sets_agent_span_attributes():
    span = FakeSpan()
    decision = ChatOrchestrationDecision(
        decision=TriageDecision(
            intent="catalog_search",
            route="semantic_catalog_search",
            reason="strands selected catalog route",
            tool="semantic_catalog_search",
        ),
        selected_agent="ProductAgent",
        selected_tool="semantic_catalog_search",
        evaluator_confidence=0.91,
        evaluator_source="ChatIntakeAgent",
        requires_auth=False,
    )

    llm_otel.record_chat_agent_result(span, decision)

    assert span.attributes["app.chat.selected_agent"] == "ProductAgent"
    assert span.attributes["app.chat.selected_tool"] == "semantic_catalog_search"
    assert span.attributes["app.chat.intent"] == "catalog_search"
    assert span.attributes["app.chat.evaluator_confidence"] == 0.91
    assert span.events[0][0] == llm_otel.GENAI_DETAILS_EVENT
    assert "gen_ai.output.messages" in span.events[0][1]


def test_trace_chat_agent_evaluation_records_request_and_exception(monkeypatch):
    span = FakeSpan()
    context = ChatContext(
        page_type="product",
        route="/product/prod_1",
        store_id="1001",
        current_product=ChatCurrentProduct(id="prod_1", category="womens_apparel"),
    )

    @contextmanager
    def fake_chat_agent_span(*, message: str, context: ChatContext, model: str):
        llm_otel._set_chat_agent_request_attributes(span, message=message, context=context, model=model)
        yield span

    def raises(message: str, context: ChatContext, *, history=None):
        raise RuntimeError("agent failed")

    monkeypatch.setattr(llm_otel, "chat_agent_span", fake_chat_agent_span)
    wrapped = llm_otel.trace_chat_agent_evaluation(raises)

    with pytest.raises(RuntimeError, match="agent failed"):
        wrapped("Find a blouse", context, history=[])

    assert span.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert span.attributes["gen_ai.provider.name"] == "openai"
    assert span.attributes["gen_ai.request.model"] == "gpt-5.4-mini"
    assert span.attributes["app.chat.current_product_id"] == "prod_1"
    assert span.attributes["error.type"] == "RuntimeError"
    assert isinstance(span.exceptions[0], RuntimeError)

