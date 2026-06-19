from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import re
import secrets
from typing import Iterator


_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<parent_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)
_ZERO_TRACE_ID = "0" * 32
_ZERO_SPAN_ID = "0" * 16


@dataclass(frozen=True, slots=True)
class ProvisionalTraceContext:
    """Validated request context. It is never authorization to persist a trace."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    trace_flags: str = "01"
    tracestate: str | None = None
    remote_parent_valid: bool = False

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"


def _safe_tracestate(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value or len(value) > 512:
        return None
    if any(ord(char) < 32 or ord(char) > 126 for char in value):
        return None
    return value


def provisional_trace_context(
    traceparent: str | None,
    tracestate: str | None = None,
) -> ProvisionalTraceContext:
    normalized = (traceparent or "").strip().lower()
    match = _TRACEPARENT_RE.fullmatch(normalized)
    valid = bool(
        match
        and match.group("version") != "ff"
        and match.group("trace_id") != _ZERO_TRACE_ID
        and match.group("parent_id") != _ZERO_SPAN_ID
    )
    if not valid:
        return ProvisionalTraceContext(
            trace_id=secrets.token_hex(16),
            span_id=secrets.token_hex(8),
            parent_span_id=None,
        )
    return ProvisionalTraceContext(
        trace_id=match.group("trace_id"),
        span_id=secrets.token_hex(8),
        parent_span_id=match.group("parent_id"),
        trace_flags=match.group("flags"),
        tracestate=_safe_tracestate(tracestate),
        remote_parent_valid=True,
    )


@dataclass(frozen=True, slots=True)
class TraceCaptureContext:
    owner_provider: str
    owner_provider_user_id: str
    surface: str
    authorized: bool = False
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    trace_flags: str = "01"
    tracestate: str | None = None

    @classmethod
    def authorized_for(
        cls,
        *,
        owner_provider: str,
        owner_provider_user_id: str,
        surface: str,
        provisional: ProvisionalTraceContext | None = None,
    ) -> TraceCaptureContext:
        return cls(
            owner_provider=owner_provider,
            owner_provider_user_id=owner_provider_user_id,
            surface=surface,
            authorized=True,
            trace_id=provisional.trace_id if provisional else None,
            span_id=provisional.span_id if provisional else None,
            parent_span_id=provisional.parent_span_id if provisional else None,
            trace_flags=provisional.trace_flags if provisional else "01",
            tracestate=provisional.tracestate if provisional else None,
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


_CURRENT_PROVISIONAL_TRACE_CONTEXT: ContextVar[ProvisionalTraceContext | None] = ContextVar(
    "current_provisional_trace_context",
    default=None,
)


def current_provisional_trace_context() -> ProvisionalTraceContext | None:
    return _CURRENT_PROVISIONAL_TRACE_CONTEXT.get()


@contextmanager
def bind_provisional_trace_context(context: ProvisionalTraceContext) -> Iterator[None]:
    token = _CURRENT_PROVISIONAL_TRACE_CONTEXT.set(context)
    try:
        yield
    finally:
        _CURRENT_PROVISIONAL_TRACE_CONTEXT.reset(token)
