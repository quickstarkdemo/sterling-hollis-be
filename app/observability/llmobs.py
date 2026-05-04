from __future__ import annotations

from typing import Any

from ddtrace.llmobs import LLMObs


def annotate_safe(**kwargs: Any) -> None:
    try:
        if LLMObs.enabled:
            LLMObs.annotate(**kwargs)
    except Exception:
        pass
