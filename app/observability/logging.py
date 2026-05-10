from __future__ import annotations

import logging
import os
from typing import Any


DEFAULT_SERVICE = "sterling-hollis-be"
DEFAULT_ENV = "dev"
DEFAULT_VERSION = "dev"

DATADOG_LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s "
    "[dd.service=%(dd.service)s dd.env=%(dd.env)s dd.version=%(dd.version)s "
    "dd.trace_id=%(dd.trace_id)s dd.span_id=%(dd.span_id)s]: %(message)s"
)

DATADOG_ACCESS_LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s "
    "[dd.service=%(dd.service)s dd.env=%(dd.env)s dd.version=%(dd.version)s "
    'dd.trace_id=%(dd.trace_id)s dd.span_id=%(dd.span_id)s]: %(client_addr)s - "%(request_line)s" %(status_code)s'
)


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
    access_args = record.args if isinstance(record.args, tuple) else ()
    if record.name == "uvicorn.access" and len(access_args) >= 5:
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


class DatadogLogFormatter(logging.Formatter):
    """Formatter that guarantees Datadog correlation fields exist on every record."""

    def format(self, record: logging.LogRecord) -> str:
        _set_missing_datadog_fields(record, _datadog_correlation_context())
        _set_missing_access_fields(record)
        return super().format(record)


def configure_datadog_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(DatadogLogFormatter(DATADOG_LOG_FORMAT))

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(level)
