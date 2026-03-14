from __future__ import annotations

import html
import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from app.config import get_settings
from app.database import SessionLocal
from app.models import UiSession

_WIDGET_TTL = timedelta(hours=2)


def _prune_widget_state(db) -> None:
    cutoff = datetime.now(timezone.utc)
    db.execute(delete(UiSession).where(UiSession.expires_at < cutoff))
    db.commit()


def register_widget_state(kind: str, payload: dict) -> str:
    token = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        _prune_widget_state(db)
        db.add(
            UiSession(
                id=token,
                kind=kind,
                state_json=payload,
                created_at=now,
                expires_at=now + _WIDGET_TTL,
            )
        )
        db.commit()
    return token


def get_widget_state(token: str) -> dict:
    with SessionLocal() as db:
        _prune_widget_state(db)
        state = db.get(UiSession, token)
        if not state:
            raise ValueError(f"Widget state {token} was not found or has expired.")
        return {
            "kind": state.kind,
            "payload": state.state_json,
            "created_at": state.created_at.isoformat(),
            "expires_at": state.expires_at.isoformat(),
        }


def _summary_text(state: dict) -> str:
    payload = state.get("payload", {})
    kind = state.get("kind")
    if kind == "associate_workspace":
        store = payload.get("store", {})
        customer = payload.get("selectedCustomer")
        if customer:
            return f"{store.get('name', 'Store')} • {customer.get('full_name', 'Customer')} • styling workspace"
        return f"{store.get('name', 'Store')} • associate styling workspace"
    if kind == "sms":
        customer = payload.get("customer", {})
        message = payload.get("message", {})
        return f"{customer.get('full_name', 'Customer')} • draft {message.get('status', 'unknown')}"
    if kind == "merch":
        store = payload.get("store", {})
        filters = payload.get("filters", {})
        question = filters.get("question")
        if question:
            return f"{store.get('name', 'Store')} • {question}"
        return f"{store.get('name', 'Store')} • merchandising board"
    return "Operator workspace"


def render_widget_html(title: str, state: dict) -> str:
    settings = get_settings()
    asset_base = settings.public_base_url.rstrip("/") + "/ui-assets"
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{html.escape(title)}</title>
    <link rel="stylesheet" href="{html.escape(asset_base)}/widget.css" />
  </head>
  <body>
    <div id="fashion-widget-root" class="fashion-widget-shell"></div>
    <script type="application/json" id="fashion-widget-state">{html.escape(json.dumps(state))}</script>
    <script>
      window.__FASHION_WIDGET__ = {{
        title: {json.dumps(title)},
        summary: {json.dumps(_summary_text(state))},
        assetBaseUrl: {json.dumps(asset_base)},
        publicBaseUrl: {json.dumps(settings.public_base_url.rstrip("/"))},
      }};
    </script>
    <script type="module" src="{html.escape(asset_base)}/widget.js"></script>
  </body>
</html>"""
