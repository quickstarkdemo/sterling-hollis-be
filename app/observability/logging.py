from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any


DEFAULT_SERVICE = "sterling-hollis-be"
DEFAULT_ENV = "dev"
DEFAULT_VERSION = "dev"

_STANDARD_LOG_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def _fallback_correlation_context() -> dict[str, str]:
    return {
        "dd.service": os.getenv("DD_SERVICE") or DEFAULT_SERVICE,
        "dd.env": os.getenv("DD_ENV") or DEFAULT_ENV,
        "dd.version": os.getenv("DD_VERSION") or os.getenv("APP_BUILD_VERSION") or DEFAULT_VERSION,
        "dd.trace_id": "0",
        "dd.span_id": "0",
    }


def _datadog_correlation_context() -> dict[str, str]:
    context = _fallback_correlation_context()
    try:
        from ddtrace import tracer

        tracer_context = tracer.get_log_correlation_context()
    except Exception:
        tracer_context = {}

    for key in ("dd.trace_id", "dd.span_id"):
        value = tracer_context.get(key)
        if value not in (None, ""):
            context[key] = str(value)

    return context


def _set_missing_datadog_fields(record: logging.LogRecord, context: dict[str, str]) -> None:
    for key, value in context.items():
        current_value: Any = record.__dict__.get(key)
        if current_value in (None, ""):
            record.__dict__[key] = value


def _set_missing_access_fields(record: logging.LogRecord) -> None:
    if record.name != "uvicorn.access":
        return

    access_args = record.args if isinstance(record.args, tuple) else ()
    if len(access_args) >= 5:
        client_addr, method, path, http_version, status_code = access_args[:5]
        request_line = f"{method} {path} HTTP/{http_version}"
    else:
        client_addr = "-"
        request_line = record.getMessage()
        status_code = "-"

    defaults = {
        "client_addr": client_addr,
        "request_line": request_line,
        "status_code": status_code,
    }
    for key, value in defaults.items():
        current_value: Any = record.__dict__.get(key)
        if current_value in (None, ""):
            record.__dict__[key] = value


class DatadogJSONFormatter(logging.Formatter):
    """JSON formatter that emits Datadog log correlation fields as attributes."""

    def format(self, record: logging.LogRecord) -> str:
        _set_missing_datadog_fields(record, _datadog_correlation_context())
        _set_missing_access_fields(record)

        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "level": record.levelname,
            "status": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "dd.service": record.__dict__.get("dd.service"),
            "dd.env": record.__dict__.get("dd.env"),
            "dd.version": record.__dict__.get("dd.version"),
            "dd.trace_id": record.__dict__.get("dd.trace_id"),
            "dd.span_id": record.__dict__.get("dd.span_id"),
            "module": record.module,
            "pathname": record.pathname,
            "lineno": record.lineno,
            "process": record.process,
            "thread": record.thread,
        }

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc_info"] = record.exc_text

        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        for key, value in record.__dict__.items():
            if key in _STANDARD_LOG_RECORD_FIELDS or key in payload:
                continue
            payload[key] = value

        return json.dumps(payload, default=str, separators=(",", ":"))


DatadogLogFormatter = DatadogJSONFormatter


def configure_datadog_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(DatadogJSONFormatter())

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(level)
