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
    stored_payload = {**payload, "widgetSessionId": token}
    with SessionLocal() as db:
        _prune_widget_state(db)
        db.add(
            UiSession(
                id=token,
                kind=kind,
                state_json=stored_payload,
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


def render_widget_html(title: str, kind: str, summary: str | None = None) -> str:
    settings = get_settings()
    asset_base = settings.public_base_url.rstrip("/") + "/ui-assets"
    widget_summary = summary or {
        "associate_workspace": "Interactive associate workspace for customer search, recommendations, and SMS drafting.",
        "sms": "Editable SMS review and test-send board.",
        "merch": "Interactive merchandising board with actions, diagnostics, and trends.",
    }.get(kind, "Operator workspace")
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
    <script>
      window.__FASHION_WIDGET__ = {{
        title: {json.dumps(title)},
        kind: {json.dumps(kind)},
        summary: {json.dumps(widget_summary)},
        assetBaseUrl: {json.dumps(asset_base)},
        publicBaseUrl: {json.dumps(settings.public_base_url.rstrip("/"))},
        sessionEndpointBase: {json.dumps(settings.public_base_url.rstrip("/") + "/ui-assets/session")},
      }};
    </script>
    <script type="module" src="{html.escape(asset_base)}/widget.js"></script>
  </body>
</html>"""
