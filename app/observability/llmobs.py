from __future__ import annotations

from typing import Any

from ddtrace.llmobs import LLMObs


def annotate_safe(span: Any | None = None, **kwargs: Any) -> None:
    try:
        if not LLMObs.enabled:
            return

        if span is not None:
            LLMObs.annotate(span=span, **kwargs)
        else:
            LLMObs.annotate(**kwargs)
    except Exception:
        pass

