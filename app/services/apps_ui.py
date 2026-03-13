from __future__ import annotations

import html
import json
import uuid
from datetime import datetime, timedelta, timezone

_WIDGET_TTL = timedelta(hours=2)
_WIDGET_STATE: dict[str, dict] = {}


def register_widget_state(kind: str, payload: dict) -> str:
    _prune_widget_state()
    token = uuid.uuid4().hex
    _WIDGET_STATE[token] = {
        "kind": kind,
        "payload": payload,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return token


def get_widget_state(token: str) -> dict:
    state = _WIDGET_STATE.get(token)
    if not state:
        raise ValueError(f"Widget state {token} was not found or has expired.")
    return state


def _prune_widget_state() -> None:
    cutoff = datetime.now(timezone.utc) - _WIDGET_TTL
    expired = [token for token, payload in _WIDGET_STATE.items() if datetime.fromisoformat(payload["created_at"]) < cutoff]
    for token in expired:
        _WIDGET_STATE.pop(token, None)


_WIDGET_SHELL = """<!doctype html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>{title}</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #f6f1e7;
        --card: #fffdf8;
        --ink: #16222d;
        --muted: #566370;
        --line: #d9d0c0;
        --accent: #9d3d12;
        --accent-soft: #f3dfd4;
      }}
      body {{ margin: 0; padding: 16px; background: linear-gradient(180deg, #f6f1e7 0%, #efe6d9 100%); color: var(--ink); font-family: Georgia, 'Times New Roman', serif; }}
      .wrap {{ display: grid; gap: 12px; }}
      .hero {{ background: var(--card); border: 1px solid var(--line); border-radius: 18px; padding: 16px; box-shadow: 0 10px 24px rgba(22,34,45,0.08); }}
      .hero h1 {{ margin: 0 0 6px 0; font-size: 22px; }}
      .hero p {{ margin: 0; color: var(--muted); font-family: ui-sans-serif, system-ui, sans-serif; }}
      .grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
      .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 14px; }}
      .eyebrow {{ font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); font-family: ui-sans-serif, system-ui, sans-serif; }}
      .title {{ font-size: 18px; margin: 6px 0; }}
      .meta {{ font-family: ui-sans-serif, system-ui, sans-serif; color: var(--muted); font-size: 13px; }}
      .score {{ display: inline-block; margin-top: 8px; padding: 4px 8px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-family: ui-sans-serif, system-ui, sans-serif; font-size: 12px; }}
      .actions {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }}
      button {{ background: var(--ink); color: white; border: none; border-radius: 999px; padding: 10px 14px; font-weight: 600; cursor: pointer; }}
      button.secondary {{ background: #e9e2d6; color: var(--ink); }}
      pre {{ white-space: pre-wrap; font-family: ui-monospace, monospace; font-size: 13px; }}
      ul {{ margin: 8px 0 0 18px; padding: 0; }}
      li {{ margin: 4px 0; }}
      .group {{ display: grid; gap: 10px; }}
    </style>
  </head>
  <body>
    <div class=\"hero\">
      <h1>{title}</h1>
      <p>{summary}</p>
    </div>
    <div class=\"wrap\" id=\"app\"></div>
    <script>
      const state = {state_json};
      const app = document.getElementById('app');

      function text(el, value) {{ el.textContent = value; return el; }}
      function div(cls, textValue) {{ const el = document.createElement('div'); if (cls) el.className = cls; if (textValue !== undefined) el.textContent = textValue; return el; }}
      function button(label, onClick, secondary=false) {{ const el = document.createElement('button'); el.textContent = label; if (secondary) el.classList.add('secondary'); el.onclick = onClick; return el; }}

      async function callTool(name, args) {{
        if (!window.openai || !window.openai.callTool) {{
          alert('Tool bridge is not available in this environment.');
          return null;
        }}
        return await window.openai.callTool(name, args || {{}});
      }}

      function renderAssociate() {{
        const payload = state.payload;
        const hero = div('hero');
        hero.appendChild(text(document.createElement('h1'), payload.customer.first_name + ' ' + payload.customer.last_name));
        hero.appendChild(text(document.createElement('p'), 'Store: ' + payload.store.name + ' • Strategy: ' + payload.recommendation.strategy));
        const actions = div('actions');
        actions.appendChild(button('Draft SMS', async () => {{
          const result = await callTool('fashion_prepare_customer_sms', payload.draftArgs);
          console.log(result);
        }}));
        hero.appendChild(actions);
        app.appendChild(hero);

        const grid = div('grid');
        for (const item of payload.recommendation.recommendations) {{
          const card = div('card');
          card.appendChild(div('eyebrow', item.category.replaceAll('_', ' ')));
          card.appendChild(div('title', item.title));
          card.appendChild(div('meta', item.brand + ' • $' + Number(item.price).toFixed(2) + ' • ' + item.availability));
          const reasons = document.createElement('ul');
          for (const reason of item.reasons) {{ const li = document.createElement('li'); li.textContent = reason; reasons.appendChild(li); }}
          card.appendChild(reasons);
          card.appendChild(div('score', 'Score ' + Number(item.score).toFixed(2)));
          grid.appendChild(card);
        }}
        app.appendChild(grid);
      }}

      function renderSms() {{
        const payload = state.payload;
        const hero = div('hero');
        hero.appendChild(text(document.createElement('h1'), 'SMS Draft')); 
        hero.appendChild(text(document.createElement('p'), payload.customer.first_name + ' • ' + payload.store.name + ' • ' + payload.message.status));
        const pre = document.createElement('pre');
        pre.textContent = payload.message.body_text;
        hero.appendChild(pre);
        const actions = div('actions');
        actions.appendChild(button('Send SMS', async () => {{
          const result = await callTool('fashion_send_customer_sms', {{ message_id: payload.message.id }});
          console.log(result);
        }}));
        actions.appendChild(button('View History', async () => {{
          const result = await callTool('fashion_customer_message_history', {{ customer_id: payload.customer.id }});
          console.log(result);
        }}, true));
        hero.appendChild(actions);
        app.appendChild(hero);
      }}

      function renderMerch() {{
        const payload = state.payload;
        const hero = div('hero');
        hero.appendChild(text(document.createElement('h1'), payload.store.name));
        hero.appendChild(text(document.createElement('p'), payload.parsed_intent + ' • Peers: ' + payload.peer_store_ids.join(', ')));
        app.appendChild(hero);

        const groups = {{ feature: [], deprioritize: [], promote: [] }};
        for (const row of payload.recommendations) groups[row.action].push(row);
        const grid = div('grid');
        for (const [action, rows] of Object.entries(groups)) {{
          const section = div('card group');
          section.appendChild(div('eyebrow', action));
          for (const row of rows) {{
            const item = div('card');
            item.appendChild(div('title', row.title));
            item.appendChild(div('meta', row.brand + ' • ' + row.category.replaceAll('_', ' ')));
            item.appendChild(div('meta', row.rationale));
            item.appendChild(div('score', 'Metric ' + Number(row.metric_value).toFixed(2) + ' • Peer Δ ' + Number(row.peer_delta).toFixed(2)));
            section.appendChild(item);
          }}
          grid.appendChild(section);
        }}
        app.appendChild(grid);
      }}

      if (state.kind === 'associate') renderAssociate();
      else if (state.kind === 'sms') renderSms();
      else if (state.kind === 'merch') renderMerch();
    </script>
  </body>
</html>"""


def _summary_text(state: dict) -> str:
    payload = state.get("payload", {})
    kind = state.get("kind")
    if kind == "associate":
        customer = payload.get("customer", {})
        store = payload.get("store", {})
        name = " ".join(part for part in [customer.get("first_name"), customer.get("last_name")] if part)
        return f"{name} • {store.get('name', 'Store context unavailable')}".strip()
    if kind == "sms":
        customer = payload.get("customer", {})
        message = payload.get("message", {})
        name = " ".join(part for part in [customer.get("first_name"), customer.get("last_name")] if part)
        return f"{name} • draft status: {message.get('status', 'unknown')}".strip()
    if kind == "merch":
        store = payload.get("store", {})
        return f"{store.get('name', 'Store context unavailable')} • merchandising actions"
    return "Operator workspace"


def render_widget_html(title: str, state: dict) -> str:
    return _WIDGET_SHELL.format(
        title=html.escape(title),
        summary=html.escape(_summary_text(state)),
        state_json=json.dumps(state),
    )
