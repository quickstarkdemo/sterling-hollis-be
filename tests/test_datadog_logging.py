from __future__ import annotations

import logging

from app.observability.logging import DATADOG_LOG_FORMAT, DatadogLogFormatter


def _format_record(message: str = "hello") -> str:
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    formatter = DatadogLogFormatter(DATADOG_LOG_FORMAT)
    return formatter.format(record)


def test_datadog_formatter_adds_default_correlation_fields(monkeypatch):
    monkeypatch.delenv("DD_SERVICE", raising=False)
    monkeypatch.delenv("DD_ENV", raising=False)
    monkeypatch.delenv("DD_VERSION", raising=False)
    monkeypatch.delenv("APP_BUILD_VERSION", raising=False)

    rendered = _format_record()

    assert "dd.service=sterling-hollis-be" in rendered
    assert "dd.env=dev" in rendered
    assert "dd.version=dev" in rendered
    assert "dd.trace_id=0" in rendered
    assert "dd.span_id=0" in rendered


def test_datadog_formatter_uses_runtime_datadog_tags(monkeypatch):
    monkeypatch.setenv("DD_SERVICE", "fastapi-app")
    monkeypatch.setenv("DD_ENV", "prod")
    monkeypatch.setenv("DD_VERSION", "1.2.3")

    rendered = _format_record()

    assert "dd.service=fastapi-app" in rendered
    assert "dd.env=prod" in rendered
    assert "dd.version=1.2.3" in rendered


def test_datadog_formatter_preserves_injected_trace_ids():
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.__dict__["dd.trace_id"] = "123"
    record.__dict__["dd.span_id"] = "456"

    rendered = DatadogLogFormatter(DATADOG_LOG_FORMAT).format(record)

    assert "dd.trace_id=123" in rendered
    assert "dd.span_id=456" in rendered
