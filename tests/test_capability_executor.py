from __future__ import annotations

import pytest

from app.api_traces.context import TraceCaptureContext, bind_trace_capture_context
from app.api_traces.operations import api_trace_session
from app.config import Settings
from app.services.capabilities import Persona, Surface
from app.services.capability_executor import (
    CapabilityExecutionContext,
    CapabilityExecutionDenied,
    execute_capability,
)


def test_executor_denies_unsupported_surface():
    with pytest.raises(CapabilityExecutionDenied, match="not exposed on chat"):
        execute_capability(
            CapabilityExecutionContext(
                capability_id="developer_trace.read",
                personas=(Persona.DEVELOPER_TRACE,),
                surface=Surface.CHAT,
            ),
            lambda: "should-not-run",
        )


def test_executor_runs_supported_capability():
    result = execute_capability(
        CapabilityExecutionContext(
            capability_id="public.catalog.search",
            personas=(Persona.SHOPPER,),
            surface=Surface.CHAT,
            selected_tool="semantic_catalog_search",
        ),
        lambda: {"ok": True},
    )

    assert result.output == {"ok": True}
    assert result.capability.id == "public.catalog.search"
    assert result.persona == Persona.SHOPPER
    assert result.surface == Surface.CHAT


def test_executor_records_unified_capability_trace_metadata(monkeypatch):
    recorded = []

    class FakeRecorder:
        def __init__(self, *, settings):
            self.settings = settings

        def record(self, *, context, projection):
            recorded.append(projection)
            return True

    monkeypatch.setattr("app.api_traces.operations.ApiTraceRecorder", FakeRecorder)
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://postgres:postgres@localhost:5432/productdb",
        api_trace_capture_enabled=True,
    )
    context = TraceCaptureContext(
        owner_provider="clerk",
        owner_provider_user_id="owner_a",
        surface="storefront-chat",
        authorized=True,
        trace_id="1" * 32,
        span_id="2" * 16,
    )

    with bind_trace_capture_context(context), api_trace_session(settings=settings, name="Capability trace"):
        execute_capability(
            CapabilityExecutionContext(
                capability_id="public.catalog.search",
                personas=(Persona.SHOPPER,),
                surface=Surface.CHAT,
                selected_tool="semantic_catalog_search",
                selected_agent="StorefrontShoppingAgent",
                session_id="chat_session",
                attributes={"route": "catalog"},
            ),
            lambda: {"items": [{"id": "cat_1"}, {"id": "cat_2"}]},
        )

    span = next(span for span in recorded[0].spans if span.operation == "capability.execute")
    assert span.attributes["capability_id"] == "public.catalog.search"
    assert span.attributes["capability_operation"] == "catalog"
    assert span.attributes["capability_side_effect"] == "read"
    assert span.attributes["surface"] == "chat"
    assert span.attributes["persona"] == "shopper"
    assert span.attributes["selected_tool"] == "semantic_catalog_search"
    assert span.attributes["selected_agent"] == "StorefrontShoppingAgent"
    assert span.attributes["approval_required"] is False
    assert span.attributes["result_type"] == "dict"
    assert span.attributes["result_count"] == 2
