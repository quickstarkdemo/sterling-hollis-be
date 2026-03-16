from __future__ import annotations

import html
import json
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from app.config import get_settings
from app.database import SessionLocal
from app.models import UiSession

_WIDGET_TTL = timedelta(hours=2)
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static" / "chatgpt-ui"


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


def _widget_css() -> str:
    return (_STATIC_DIR / "widget.css").read_text(encoding="utf-8")


def _widget_js(kind: str) -> str:
    script_name = "merch-widget.js" if kind == "merch_workspace" else "widget.js"
    return (_STATIC_DIR / script_name).read_text(encoding="utf-8")


def render_widget_html(
    title: str,
    kind: str,
    summary: str | None = None,
    widget_session_id: str | None = None,
    initial_payload: dict | None = None,
) -> str:
    settings = get_settings()
    asset_base = settings.public_base_url.rstrip("/") + "/ui-assets"
    build_version = settings.app_build_version or "dev"
    widget_summary = summary or {
        "customer_search_workspace": "Search customers by name, email, or phone and select a profile for follow-up actions.",
        "merch_workspace": "Evaluate store performance, compare peers, and export merchandising decisions to CSV.",
    }.get(kind, "Operator workspace")
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{html.escape(title)}</title>
    <style>{_widget_css()}</style>
  </head>
  <body>
    <div id="fashion-widget-root" class="fashion-widget-shell">
      <div style="padding:24px;font:15px/1.5 system-ui,sans-serif;color:#5c4a1e;">
        Loading {html.escape(title)}…
      </div>
    </div>
    <script>
      window.__FASHION_WIDGET__ = {{
        title: {json.dumps(title)},
        kind: {json.dumps(kind)},
        buildVersion: {json.dumps(build_version)},
        summary: {json.dumps(widget_summary)},
        widgetSessionId: {json.dumps(widget_session_id)},
        initialPayload: {json.dumps(initial_payload)},
        assetBaseUrl: {json.dumps(asset_base)},
        publicBaseUrl: {json.dumps(settings.public_base_url.rstrip("/"))},
      }};
    </script>
    <script type="module">
{_widget_js(kind)}
    </script>
  </body>
</html>"""
