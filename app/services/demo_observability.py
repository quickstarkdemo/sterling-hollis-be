from __future__ import annotations

from dataclasses import dataclass
import logging
from threading import Lock
import time
from typing import Any

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
APM_SPAN_NAME = "demo.inventory_reconciliation"


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

    def response(self) -> DemoObservabilityStateResponse:
        return DemoObservabilityStateResponse(
            enabled=self.enabled,
            mode=self.mode,
            latency_seconds=self.latency_seconds,
            target_store_id=self.target_store_id,
            incident_id=self.incident_id,
            correlation_key=self.correlation_key,
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
    )


def get_demo_observability_state() -> DemoObservabilityStateResponse:
    return _current_state().response()


def update_demo_observability_state(req: DemoObservabilityUpdateRequest) -> DemoObservabilityStateResponse:
    with _STATE_LOCK:
        current = _state_unlocked()
        enabled = current.enabled if req.enabled is None else req.enabled
        mode = current.mode if req.mode is None else req.mode
        latency_seconds = current.latency_seconds if req.latency_seconds is None else req.latency_seconds
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
        )
        return _STATE.response()


def demo_observability_active_for_store(store_id: str | None) -> bool:
    state = _current_state()
    return bool(state.active and (not state.target_store_id or not store_id or store_id == state.target_store_id))


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
            message = (
                "Supplier feed schema mismatch while reconciling available-to-promise inventory: "
                "expected availability_status, received fulfillment_state."
            )
            logger.error(message, extra=result)
            raise DemoSupplierFeedSchemaError(message)

        logger.info("Completed demo available-to-promise reconciliation", extra=result)
        return result
