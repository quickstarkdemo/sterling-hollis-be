from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True, slots=True)
class TraceCaptureContext:
    owner_provider: str
    owner_provider_user_id: str
    surface: str
    authorized: bool = False

    @classmethod
    def authorized_for(
        cls,
        *,
        owner_provider: str,
        owner_provider_user_id: str,
        surface: str,
    ) -> TraceCaptureContext:
        return cls(
            owner_provider=owner_provider,
            owner_provider_user_id=owner_provider_user_id,
            surface=surface,
            authorized=True,
        )


_CURRENT_TRACE_CAPTURE_CONTEXT: ContextVar[TraceCaptureContext | None] = ContextVar(
    "current_trace_capture_context",
    default=None,
)


def current_trace_capture_context() -> TraceCaptureContext | None:
    return _CURRENT_TRACE_CAPTURE_CONTEXT.get()


@contextmanager
def bind_trace_capture_context(context: TraceCaptureContext) -> Iterator[None]:
    token = _CURRENT_TRACE_CAPTURE_CONTEXT.set(context)
    try:
        yield
    finally:
        _CURRENT_TRACE_CAPTURE_CONTEXT.reset(token)
