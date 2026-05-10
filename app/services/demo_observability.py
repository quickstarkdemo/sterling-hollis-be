from __future__ import annotations

from dataclasses import dataclass
import logging
from threading import Lock
import time
import traceback
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Product
from app.schemas import (
    DemoObservabilityMode,
    DemoObservabilityStateResponse,
    DemoObservabilityUpdateRequest,
)

logger = logging.getLogger(__name__)

INCIDENT_ID = "demo-atp-supplier-feed-2026-05-06"
CORRELATION_KEY = "sterling-hollis-atp-reconciliation"
NETWORK_INCIDENT_ID = "demo-network-outage-2026-05-08"
NETWORK_CORRELATION_KEY = "sterling-hollis-network-outage"
NETWORK_DEVICE = "DATACENTER-USER-SW11A"
NETWORK_DEVICE_HOSTNAME = "datacenter-user-sw11a"
NETWORK_DEVICE_IP = "10.100.1.48"
NETWORK_DOWNSTREAM_DEVICE = "STORE-FULFILLMENT-EDGE01"
NETWORK_DOWNSTREAM_DEVICE_HOSTNAME = "store-fulfillment-edge01"
NETWORK_DOWNSTREAM_DEVICE_IP = "10.100.12.21"
NETWORK_PARENT_INTERFACE = "GigabitEthernet1/0/48"
NETWORK_DOWNSTREAM_INTERFACE = "TenGigabitEthernet0/1"
NETWORK_SITE = "dc01"
NETWORK_OUTAGE_SCOPE = "storefront_api"
NETWORK_AFFECTED_SERVICE = "sterling-hollis-be"
NETWORK_EVENT_COUNT_MIN = 1
NETWORK_EVENT_COUNT_MAX = 25
APM_SPAN_NAME = "demo.inventory_reconciliation"
ERROR_MESSAGE = (
    "Supplier feed schema mismatch while reconciling available-to-promise inventory: "
    "expected availability_status, received fulfillment_state."
)


class DemoSupplierFeedSchemaError(RuntimeError):
    """Raised by the Datadog demo harness to create a realistic unhandled backend error."""


@dataclass
class DemoObservabilityState:
    enabled: bool
    mode: DemoObservabilityMode
    latency_seconds: float
    target_store_id: str | None
    incident_id: str = INCIDENT_ID
    correlation_key: str = CORRELATION_KEY
    network_device: str = NETWORK_DEVICE
    network_site: str = NETWORK_SITE
    outage_scope: str = NETWORK_OUTAGE_SCOPE
    network_event_count: int = 2

    def response(self) -> DemoObservabilityStateResponse:
        snmp_trap_logs = network_outage_snmp_trap_logs(count=self.network_event_count)
        return DemoObservabilityStateResponse(
            enabled=self.enabled,
            mode=self.mode,
            latency_seconds=self.latency_seconds,
            target_store_id=self.target_store_id,
            incident_id=self.incident_id,
            correlation_key=self.correlation_key,
            network_device=self.network_device,
            network_site=self.network_site,
            outage_scope=self.outage_scope,
            network_event_count=self.network_event_count,
            snmp_trap_log=snmp_trap_logs[0],
            snmp_trap_logs=snmp_trap_logs,
        )

    @property
    def active(self) -> bool:
        return self.enabled and self.mode != DemoObservabilityMode.off


_STATE_LOCK = Lock()
_STATE: DemoObservabilityState | None = None


def _default_state() -> DemoObservabilityState:
    settings = get_settings()
    mode = DemoObservabilityMode(settings.demo_observability_mode)
    enabled = bool(settings.demo_observability_enabled and mode != DemoObservabilityMode.off)
    return DemoObservabilityState(
        enabled=enabled,
        mode=mode if enabled else DemoObservabilityMode.off,
        latency_seconds=max(0.0, float(settings.demo_observability_latency_seconds)),
        target_store_id=settings.demo_observability_target_store_id,
        incident_id=_incident_id_for_mode(mode if enabled else DemoObservabilityMode.off),
        correlation_key=_correlation_key_for_mode(mode if enabled else DemoObservabilityMode.off),
        network_event_count=_normalize_network_event_count(settings.demo_observability_network_event_count),
    )


def get_demo_observability_state() -> DemoObservabilityStateResponse:
    return _current_state().response()


def update_demo_observability_state(req: DemoObservabilityUpdateRequest) -> DemoObservabilityStateResponse:
    with _STATE_LOCK:
        current = _state_unlocked()
        enabled = current.enabled if req.enabled is None else req.enabled
        mode = current.mode if req.mode is None else req.mode
        latency_seconds = current.latency_seconds if req.latency_seconds is None else req.latency_seconds
        network_event_count = (
            current.network_event_count if req.network_event_count is None else req.network_event_count
        )
        target_store_id = current.target_store_id
        if "target_store_id" in req.model_fields_set:
            target_store_id = req.target_store_id

        if enabled and mode == DemoObservabilityMode.off and req.mode is None:
            mode = DemoObservabilityMode.latency

        if not enabled or mode == DemoObservabilityMode.off:
            enabled = False
            mode = DemoObservabilityMode.off

        global _STATE
        _STATE = DemoObservabilityState(
            enabled=enabled,
            mode=mode,
            latency_seconds=max(0.0, float(latency_seconds)),
            target_store_id=target_store_id,
            incident_id=_incident_id_for_mode(mode),
            correlation_key=_correlation_key_for_mode(mode),
            network_event_count=_normalize_network_event_count(network_event_count),
        )
        return _STATE.response()


def reset_demo_observability_state() -> DemoObservabilityStateResponse:
    with _STATE_LOCK:
        current = _state_unlocked()
        global _STATE
        _STATE = DemoObservabilityState(
            enabled=False,
            mode=DemoObservabilityMode.off,
            latency_seconds=current.latency_seconds,
            target_store_id=current.target_store_id,
            network_event_count=current.network_event_count,
        )
        return _STATE.response()


def demo_observability_active_for_store(store_id: str | None) -> bool:
    state = _current_state()
    return bool(state.active and (not state.target_store_id or not store_id or store_id == state.target_store_id))


def demo_network_outage_active() -> bool:
    state = _current_state()
    return bool(state.enabled and state.mode == DemoObservabilityMode.network_outage)


def demo_network_outage_response_payload() -> dict[str, str]:
    state = _current_state()
    return {
        "detail": f"Demo network outage active: upstream access switch {state.network_device} is unreachable.",
        "incident_id": state.incident_id,
        "correlation_key": state.correlation_key,
        "affected_device": state.network_device,
        "network_device": state.network_device,
        "site": state.network_site,
        "outage_scope": state.outage_scope,
    }


def network_outage_tags(*, path: str | None = None, method: str | None = None) -> dict[str, str]:
    state = _current_state()
    tags = {
        "demo.incident_id": state.incident_id,
        "demo.scenario": DemoObservabilityMode.network_outage.value,
        "demo.correlation_key": state.correlation_key,
        "demo.network_device": state.network_device,
        "demo.network_site": state.network_site,
        "demo.outage_scope": state.outage_scope,
        "service": NETWORK_AFFECTED_SERVICE,
        "env": "production",
    }
    if path is not None:
        tags["http.path"] = path
    if method is not None:
        tags["http.method"] = method
    return tags


def annotate_network_outage_span(*, path: str, method: str) -> None:
    try:
        from ddtrace import tracer

        span = tracer.current_span()
        if span is not None:
            span.error = 1
            _set_span_tags(span, network_outage_tags(path=path, method=method))
            span.set_tag("http.status_code", 503)
            span.set_tag("error.type", "DemoNetworkOutage")
            span.set_tag("error.message", demo_network_outage_response_payload()["detail"])
    except Exception:
        logger.debug("Failed to annotate Datadog network outage span", exc_info=True)


def log_network_outage_block(*, path: str, method: str) -> None:
    logger.warning(
        "Demo network outage blocked API request",
        extra={
            **network_outage_tags(path=path, method=method),
            "http.status_code": 503,
            "affected_device": NETWORK_DEVICE,
        },
    )


def network_outage_snmp_trap_logs(*, count: int | None = None) -> list[dict[str, Any]]:
    event_count = _normalize_network_event_count(count if count is not None else _current_state().network_event_count)
    base_timestamp = int(time.time() * 1000)
    return [
        _network_outage_snmp_trap_log(sequence=sequence, total_events=event_count, base_timestamp=base_timestamp)
        for sequence in range(event_count)
    ]


def network_outage_snmp_trap_log() -> dict[str, Any]:
    return _network_outage_snmp_trap_log(sequence=0, total_events=1, base_timestamp=int(time.time() * 1000))


def _network_outage_snmp_trap_log(*, sequence: int, total_events: int, base_timestamp: int) -> dict[str, Any]:
    timestamp = base_timestamp + (sequence * 1500)
    history_index = (timestamp + sequence) % 100000
    uptime = 530073760 + (sequence * 137)
    facility = "IOSXE"
    message_name = "PLATFORM"
    reported_state = "down" if sequence % 4 in {0, 1} else "up"
    severity = "alert" if reported_state == "down" else "warning"
    is_downstream = sequence % 2 == 1
    if is_downstream:
        device_name = NETWORK_DOWNSTREAM_DEVICE
        device_hostname = NETWORK_DOWNSTREAM_DEVICE_HOSTNAME
        device_ip = NETWORK_DOWNSTREAM_DEVICE_IP
        device_role = "downstream_edge_switch"
        interface = NETWORK_DOWNSTREAM_INTERFACE
        topology_role = "child"
        message_text = (
            f"%LINK-2-CHANGED: Interface {interface}, changed state to {reported_state}; "
            f"upstream dependency {NETWORK_DEVICE_HOSTNAME} {NETWORK_PARENT_INTERFACE} is unstable"
        )
    else:
        device_name = NETWORK_DEVICE
        device_hostname = NETWORK_DEVICE_HOSTNAME
        device_ip = NETWORK_DEVICE_IP
        device_role = "access_switch"
        interface = NETWORK_PARENT_INTERFACE
        topology_role = "parent"
        message_text = (
            f"%LINK-2-CHANGED: Interface {interface}, changed state to {reported_state}; "
            f"downstream device {NETWORK_DOWNSTREAM_DEVICE_HOSTNAME} impacted by uplink instability"
        )

    dependency_path = f"{NETWORK_DEVICE_HOSTNAME}>{NETWORK_DOWNSTREAM_DEVICE_HOSTNAME}"
    tags = ",".join(
        [
            "env:production",
            "source:snmp-traps",
            "category:network",
            "event_type:trigger",
            f"severity:{severity}",
            "device_vendor:cisco",
            f"device_namespace:{NETWORK_SITE}",
            f"device_hostname:{device_hostname}",
            f"device_ip:{device_ip}",
            f"site:{NETWORK_SITE}",
            f"incident_id:{NETWORK_INCIDENT_ID}",
            f"correlation_key:{NETWORK_CORRELATION_KEY}",
            f"topology_role:{topology_role}",
            f"topology_parent_device:{NETWORK_DEVICE_HOSTNAME}",
            f"topology_child_device:{NETWORK_DOWNSTREAM_DEVICE_HOSTNAME}",
        ]
    )
    return {
        "message": message_text,
        "level": severity.upper(),
        "ddsource": "snmp-traps",
        "source": "snmp-traps",
        "service": "network-device-monitoring",
        "hostname": device_hostname,
        "network_device": device_ip,
        "status": severity,
        "device_name": device_name,
        "device_hostname": device_hostname,
        "device_ip": device_ip,
        "device_namespace": NETWORK_SITE,
        "device_role": device_role,
        "device_vendor": "cisco",
        "interface": interface,
        "interface_state": reported_state,
        "event_sequence": sequence + 1,
        "event_count": total_events,
        "topology_role": topology_role,
        "topology_parent_device": NETWORK_DEVICE_HOSTNAME,
        "topology_parent_ip": NETWORK_DEVICE_IP,
        "topology_parent_interface": NETWORK_PARENT_INTERFACE,
        "topology_child_device": NETWORK_DOWNSTREAM_DEVICE_HOSTNAME,
        "topology_child_ip": NETWORK_DOWNSTREAM_DEVICE_IP,
        "dependency_path": dependency_path,
        "clogHistFacility": facility,
        "clogHistMsgName": message_name,
        "clogHistMsgText": message_text,
        "clogHistSeverity": severity,
        "clogHistTimestamp": uptime - 1,
        "snmpTrapMIB": "CISCO-SYSLOG-MIB",
        "snmpTrapName": "clogMessageGenerated",
        "snmpTrapOID": "1.3.6.1.4.1.9.9.41.2.0.1",
        "uptime": uptime,
        "variables": [
            {
                "oid": f"1.3.6.1.4.1.9.9.41.1.2.3.1.2.{history_index}",
                "type": "string",
                "value": facility,
            },
            {
                "oid": f"1.3.6.1.4.1.9.9.41.1.2.3.1.3.{history_index}",
                "type": "integer",
                "value": 2,
            },
            {
                "oid": f"1.3.6.1.4.1.9.9.41.1.2.3.1.4.{history_index}",
                "type": "string",
                "value": message_name,
            },
            {
                "oid": f"1.3.6.1.4.1.9.9.41.1.2.3.1.5.{history_index}",
                "type": "string",
                "value": message_text,
            },
            {
                "oid": f"1.3.6.1.4.1.9.9.41.1.2.3.1.6.{history_index}",
                "type": "time-ticks",
                "value": uptime - 1,
            },
        ],
        "site": NETWORK_SITE,
        "namespace": NETWORK_SITE,
        "incident_id": NETWORK_INCIDENT_ID,
        "correlation_key": NETWORK_CORRELATION_KEY,
        "timestamp": timestamp,
        "tags": tags,
        "ddtags": tags,
    }


def datadog_logs_intake_url(site: str | None) -> str:
    normalized = (site or "datadoghq.com").strip() or "datadoghq.com"
    if normalized.startswith("api."):
        normalized = normalized.removeprefix("api.")
    return f"https://http-intake.logs.{normalized}/api/v2/logs"


def send_network_outage_snmp_trap_log(*, event_count: int | None = None) -> dict[str, Any]:
    settings = get_settings()
    if not settings.dd_api_key:
        raise RuntimeError("DD_API_KEY is required to send the network outage SNMP log to Datadog Logs intake.")

    payloads = network_outage_snmp_trap_logs(count=event_count)
    url = datadog_logs_intake_url(settings.dd_site)
    try:
        response = httpx.post(
            url,
            json=payloads,
            headers={
                "Content-Type": "application/json",
                "DD-API-KEY": settings.dd_api_key,
            },
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Datadog Logs intake rejected network outage SNMP log: {exc}") from exc

    datadog_response: Any
    try:
        datadog_response = response.json()
    except ValueError:
        datadog_response = {"status_code": response.status_code, "text": response.text}

    logger.info(
        "Sent demo network outage SNMP trap log to Datadog Logs intake",
        extra={
            "demo.incident_id": NETWORK_INCIDENT_ID,
            "demo.correlation_key": NETWORK_CORRELATION_KEY,
            "demo.network_device": NETWORK_DEVICE,
            "demo.network_site": NETWORK_SITE,
            "demo.network_event_count": len(payloads),
        },
    )
    return {
        "success": True,
        "intake_url": url,
        "payload": payloads[0],
        "payloads": payloads,
        "event_count": len(payloads),
        "datadog_response": datadog_response,
    }


def _incident_id_for_mode(mode: DemoObservabilityMode) -> str:
    if mode == DemoObservabilityMode.network_outage:
        return NETWORK_INCIDENT_ID
    return INCIDENT_ID


def _correlation_key_for_mode(mode: DemoObservabilityMode) -> str:
    if mode == DemoObservabilityMode.network_outage:
        return NETWORK_CORRELATION_KEY
    return CORRELATION_KEY


def _normalize_network_event_count(value: int | float | None) -> int:
    try:
        count = int(value if value is not None else 2)
    except (TypeError, ValueError):
        count = 2
    return min(NETWORK_EVENT_COUNT_MAX, max(NETWORK_EVENT_COUNT_MIN, count))


def _current_state() -> DemoObservabilityState:
    with _STATE_LOCK:
        return _state_unlocked()


def _state_unlocked() -> DemoObservabilityState:
    global _STATE
    if _STATE is None:
        _STATE = _default_state()
    return _STATE


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _set_span_tags(span: Any, tags: dict[str, Any]) -> None:
    for key, value in tags.items():
        if value is None:
            continue
        try:
            span.set_tag(key, value)
        except Exception:
            logger.debug("Failed to set Datadog demo span tag %s", key, exc_info=True)


def _set_span_exception(span: Any, exc: BaseException) -> None:
    span.error = 1
    span.set_exc_info(type(exc), exc, exc.__traceback__)
    span.set_tag("error.type", type(exc).__name__)
    span.set_tag("error.message", str(exc))
    span.set_tag("error.stack", "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip())


def _inventory_summary(db: Session, store_id: str | None) -> dict[str, int]:
    query = select(
        func.count(Product.id).label("sku_count"),
        func.coalesce(func.sum(Product.inventory_qty), 0).label("units_on_hand"),
    )
    if store_id:
        query = query.where(Product.store_id == store_id)
    row = db.execute(query).one()
    return {
        "sku_count": int(row.sku_count or 0),
        "units_on_hand": int(row.units_on_hand or 0),
    }


def run_available_to_promise_reconciliation(
    db: Session,
    *,
    conversation_id: str,
    turn_id: str | None,
    selected_tool: str | None,
    store_id: str | None,
    raise_on_error: bool = False,
) -> dict[str, Any] | None:
    state = _current_state()
    if not state.active:
        return None
    if state.target_store_id and store_id and store_id != state.target_store_id:
        return None

    tags = {
        "demo.incident_id": state.incident_id,
        "demo.scenario": state.mode.value,
        "demo.correlation_key": state.correlation_key,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "selected_tool": selected_tool,
        "store_id": store_id or state.target_store_id,
    }
    logger.info(
        "Starting demo available-to-promise reconciliation",
        extra={
            **tags,
            "demo.latency_seconds": state.latency_seconds,
        },
    )

    from ddtrace import tracer

    with tracer.trace(APM_SPAN_NAME, resource="available_to_promise_reconciliation") as span:
        _set_span_tags(span, tags)
        span.set_metric("demo.latency_seconds", float(state.latency_seconds))
        summary = _inventory_summary(db, store_id or state.target_store_id)
        span.set_metric("demo.sku_count", summary["sku_count"])
        span.set_metric("demo.units_on_hand", summary["units_on_hand"])

        if state.mode in {DemoObservabilityMode.latency, DemoObservabilityMode.latency_and_error}:
            _sleep(state.latency_seconds)

        result = {
            **tags,
            **summary,
            "latency_seconds": state.latency_seconds,
            "mode": state.mode.value,
        }

        if state.mode in {DemoObservabilityMode.error, DemoObservabilityMode.latency_and_error}:
            try:
                raise DemoSupplierFeedSchemaError(ERROR_MESSAGE)
            except DemoSupplierFeedSchemaError as exc:
                result.update(
                    {
                        "status": "degraded",
                        "error": type(exc).__name__,
                        "error_message": str(exc),
                    }
                )
                _set_span_exception(span, exc)
                logger.exception(ERROR_MESSAGE, extra=result)
                if raise_on_error:
                    raise
                return result

        logger.info("Completed demo available-to-promise reconciliation", extra=result)
        return result


def raise_unhandled_demo_supplier_feed_error() -> None:
    state = _current_state()
    tags = {
        "demo.incident_id": state.incident_id,
        "demo.scenario": "unhandled_error_trigger",
        "demo.correlation_key": state.correlation_key,
        "store_id": state.target_store_id,
    }

    from ddtrace import tracer

    with tracer.trace(APM_SPAN_NAME, resource="unhandled_supplier_feed_error") as span:
        _set_span_tags(span, tags)
        try:
            raise DemoSupplierFeedSchemaError(ERROR_MESSAGE)
        except DemoSupplierFeedSchemaError as exc:
            _set_span_exception(span, exc)
            logger.exception(ERROR_MESSAGE, extra=tags)
            raise
