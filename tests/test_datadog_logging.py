from __future__ import annotations

import json
import logging
import sys
from http import HTTPStatus
from types import SimpleNamespace

from app.observability.logging import DatadogJSONFormatter


def _record(message: str = "hello", *, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=exc_info,
    )


def _format_record(record: logging.LogRecord | None = None) -> dict[str, object]:
    rendered = DatadogJSONFormatter().format(record or _record())
    return json.loads(rendered)


def test_datadog_json_formatter_adds_default_correlation_fields(monkeypatch):
    monkeypatch.delenv("DD_SERVICE", raising=False)
    monkeypatch.delenv("DD_ENV", raising=False)
    monkeypatch.delenv("DD_VERSION", raising=False)
    monkeypatch.delenv("APP_BUILD_VERSION", raising=False)

    payload = _format_record()

    assert payload["dd.service"] == "sterling-hollis-be"
    assert payload["dd.env"] == "dev"
    assert payload["dd.version"] == "dev"
    assert payload["dd.trace_id"] == "0"
    assert payload["dd.span_id"] == "0"
    assert payload["status"] == "info"
    assert payload["message"] == "hello"


def test_datadog_json_formatter_uses_runtime_datadog_tags(monkeypatch):
    monkeypatch.setenv("DD_SERVICE", "fastapi-app")
    monkeypatch.setenv("DD_ENV", "prod")
    monkeypatch.setenv("DD_VERSION", "1.2.3")

    payload = _format_record()

    assert payload["dd.service"] == "fastapi-app"
    assert payload["dd.env"] == "prod"
    assert payload["dd.version"] == "1.2.3"


def test_datadog_json_formatter_uses_tracer_correlation_context(monkeypatch):
    fake_tracer = SimpleNamespace(
        get_log_correlation_context=lambda: {
            "dd.trace_id": "123",
            "dd.span_id": "456",
        }
    )
    monkeypatch.setitem(sys.modules, "ddtrace", SimpleNamespace(tracer=fake_tracer))

    payload = _format_record()

    assert payload["dd.trace_id"] == "123"
    assert payload["dd.span_id"] == "456"


def test_datadog_json_formatter_preserves_injected_trace_ids():
    record = _record()
    record.__dict__["dd.trace_id"] = "789"
    record.__dict__["dd.span_id"] = "101112"

    payload = _format_record(record)

    assert payload["dd.trace_id"] == "789"
    assert payload["dd.span_id"] == "101112"


def test_datadog_access_formatter_unpacks_uvicorn_access_args():
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:53808", "GET", "/health", "1.1", HTTPStatus.OK),
        exc_info=None,
    )

    payload = _format_record(record)

    assert payload["client_addr"] == "127.0.0.1:53808"
    assert payload["request_line"] == "GET /health HTTP/1.1"
    assert payload["status_code"] == 200
    assert payload["message"] == '127.0.0.1:53808 - "GET /health HTTP/1.1" 200'
    assert payload["dd.service"] == "sterling-hollis-be"


def test_datadog_access_formatter_handles_missing_access_args():
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="plain message",
        args=(),
        exc_info=None,
    )

    payload = _format_record(record)

    assert payload["client_addr"] == "-"
    assert payload["request_line"] == "plain message"
    assert payload["status_code"] == "-"


def test_datadog_json_formatter_does_not_add_access_fields_to_app_logs():
    payload = _format_record(_record("plain message"))

    assert "client_addr" not in payload
    assert "request_line" not in payload
    assert "status_code" not in payload


def test_datadog_json_formatter_includes_exception_text():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        payload = _format_record(_record("failed", exc_info=sys.exc_info()))

    assert payload["message"] == "failed"
    assert "RuntimeError: boom" in str(payload["exc_info"])
