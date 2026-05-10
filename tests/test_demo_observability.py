from __future__ import annotations

from app.config import get_settings
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
            network_event_count=4,
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
    assert state.network_event_count == 4
    assert len(state.snmp_trap_logs) == 4
    assert state.snmp_trap_log["ddsource"] == "snmp-traps"
    assert state.snmp_trap_log["status"] == "alert"
    assert state.snmp_trap_log["topology_role"] == "parent"
    assert state.snmp_trap_log["snmpTrapName"] == "clogMessageGenerated"
    assert state.snmp_trap_log["snmpTrapMIB"] == "CISCO-SYSLOG-MIB"
    assert state.snmp_trap_log["snmpTrapOID"] == "1.3.6.1.4.1.9.9.41.2.0.1"
    assert state.snmp_trap_logs[1]["topology_role"] == "child"
    assert state.snmp_trap_logs[1]["hostname"] == "store-fulfillment-edge01"
    assert state.snmp_trap_logs[1]["topology_parent_device"] == "datacenter-user-sw11a"
    assert state.snmp_trap_logs[1].keys() == state.snmp_trap_log.keys()
    assert "affected_service" not in state.snmp_trap_log
    assert "outage_scope" not in state.snmp_trap_log
    assert "service:sterling-hollis-be" not in state.snmp_trap_log["ddtags"]
    assert "correlation_key:sterling-hollis-network-outage" in state.snmp_trap_log["ddtags"]

    demo_observability.reset_demo_observability_state()


def test_network_outage_snmp_trap_log_posts_to_datadog_logs_intake(monkeypatch):
    captured = {}

    class _FakeResponse:
        status_code = 202
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {}

    def fake_post(url, *, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setenv("DD_API_KEY", "test-api-key")
    monkeypatch.setenv("DD_SITE", "api.datadoghq.com")
    get_settings.cache_clear()
    monkeypatch.setattr(demo_observability.httpx, "post", fake_post)
    demo_observability.update_demo_observability_state(
        demo_observability.DemoObservabilityUpdateRequest(
            enabled=True,
            mode=demo_observability.DemoObservabilityMode.network_outage,
            network_event_count=3,
        )
    )

    result = demo_observability.send_network_outage_snmp_trap_log()

    assert result["success"] is True
    assert result["intake_url"] == "https://http-intake.logs.datadoghq.com/api/v2/logs"
    assert result["event_count"] == 3
    assert len(result["payloads"]) == 3
    assert captured["url"] == "https://http-intake.logs.datadoghq.com/api/v2/logs"
    assert captured["headers"]["DD-API-KEY"] == "test-api-key"
    assert len(captured["json"]) == 3
    assert captured["json"][0]["ddsource"] == "snmp-traps"
    assert captured["json"][0]["source"] == "snmp-traps"
    assert captured["json"][0]["hostname"] == "datacenter-user-sw11a"
    assert captured["json"][0]["status"] == "alert"
    assert captured["json"][0]["event_count"] == 3
    assert captured["json"][0]["topology_role"] == "parent"
    assert captured["json"][1]["hostname"] == "store-fulfillment-edge01"
    assert captured["json"][1]["topology_role"] == "child"
    assert captured["json"][1]["topology_parent_device"] == "datacenter-user-sw11a"
    assert captured["json"][1].keys() == captured["json"][0].keys()
    assert captured["json"][0]["clogHistMsgName"] == "PLATFORM"
    assert captured["json"][0]["clogHistFacility"] == "IOSXE"
    assert captured["json"][0]["snmpTrapName"] == "clogMessageGenerated"
    assert captured["json"][0]["snmpTrapMIB"] == "CISCO-SYSLOG-MIB"
    assert captured["json"][0]["snmpTrapOID"] == "1.3.6.1.4.1.9.9.41.2.0.1"
    assert captured["json"][0]["variables"][3]["value"] == captured["json"][0]["clogHistMsgText"]
    assert "sterling-hollis-be" not in captured["json"][0]["message"]
    assert "affected_service" not in captured["json"][0]
    assert captured["json"][0]["tags"] == captured["json"][0]["ddtags"]

    demo_observability.reset_demo_observability_state()
    get_settings.cache_clear()
