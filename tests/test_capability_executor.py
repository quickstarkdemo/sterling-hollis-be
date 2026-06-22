from __future__ import annotations

import pytest

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
