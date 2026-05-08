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


def test_network_outage_state_uses_network_correlation_payload():
    demo_observability.update_demo_observability_state(
        demo_observability.DemoObservabilityUpdateRequest(
            enabled=True,
            mode=demo_observability.DemoObservabilityMode.network_outage,
        )
    )

    state = demo_observability.get_demo_observability_state()

    assert state.enabled is True
    assert state.mode == "network_outage"
    assert state.incident_id == "demo-network-outage-2026-05-08"
    assert state.correlation_key == "sterling-hollis-network-outage"
    assert state.network_device == "DATACENTER-USER-SW11A"
    assert state.network_site == "dc01"
    assert state.outage_scope == "storefront_api"
    assert state.snmp_trap_log["ddsource"] == "snmp-traps"
    assert state.snmp_trap_log["trap_name"] == "linkDown"
    assert "correlation_key:sterling-hollis-network-outage" in state.snmp_trap_log["ddtags"]

    demo_observability.reset_demo_observability_state()
