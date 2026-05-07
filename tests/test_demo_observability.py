from __future__ import annotations

from app.services import demo_observability


class _FakeSpan:
    def __init__(self) -> None:
        self.error = 0
        self.exc_info = None
        self.tags = {}

    def set_exc_info(self, exc_type, exc, traceback):
        self.exc_info = (exc_type, exc, traceback)

    def set_tag(self, key, value):
        self.tags[key] = value


def test_demo_span_exception_records_real_traceback():
    span = _FakeSpan()

    try:
        raise demo_observability.DemoSupplierFeedSchemaError(demo_observability.ERROR_MESSAGE)
    except demo_observability.DemoSupplierFeedSchemaError as exc:
        demo_observability._set_span_exception(span, exc)

    assert span.error == 1
    assert span.exc_info is not None
    exc_type, exc, tb = span.exc_info
    assert exc_type is demo_observability.DemoSupplierFeedSchemaError
    assert tb is exc.__traceback__
    assert "test_demo_span_exception_records_real_traceback" in span.tags["error.stack"]
    assert "DemoSupplierFeedSchemaError" in span.tags["error.stack"]
