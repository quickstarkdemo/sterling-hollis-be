from __future__ import annotations

import html
import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

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
        --ok: #1f6b3b;
        --warn: #8a4b00;
        --bad: #9c1f1f;
      }}
      body {{ margin: 0; padding: 16px; background: linear-gradient(180deg, #f6f1e7 0%, #efe6d9 100%); color: var(--ink); font-family: Georgia, 'Times New Roman', serif; }}
      .shell {{ display: grid; gap: 14px; }}
      .hero, .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 18px; padding: 16px; box-shadow: 0 10px 24px rgba(22,34,45,0.08); }}
      .hero h1, .card h2 {{ margin: 0 0 8px 0; }}
      .hero p, .meta, .subtle, label {{ margin: 0; color: var(--muted); font-family: ui-sans-serif, system-ui, sans-serif; font-size: 13px; }}
      .grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
      .stack {{ display: grid; gap: 10px; }}
      .row {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
      .split {{ display: grid; gap: 12px; grid-template-columns: minmax(280px, 1fr) minmax(320px, 2fr); }}
      @media (max-width: 920px) {{ .split {{ grid-template-columns: 1fr; }} }}
      input, textarea, select {{ width: 100%; border: 1px solid var(--line); border-radius: 12px; padding: 10px 12px; font: inherit; background: white; color: var(--ink); box-sizing: border-box; }}
      textarea {{ min-height: 180px; resize: vertical; font-family: ui-monospace, monospace; }}
      button {{ background: var(--ink); color: white; border: none; border-radius: 999px; padding: 10px 14px; font-weight: 600; cursor: pointer; }}
      button.secondary {{ background: #e9e2d6; color: var(--ink); }}
      button.ghost {{ background: transparent; color: var(--accent); border: 1px solid var(--line); }}
      .pill {{ display: inline-block; padding: 4px 8px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-family: ui-sans-serif, system-ui, sans-serif; font-size: 12px; }}
      .status-ok {{ color: var(--ok); }}
      .status-warn {{ color: var(--warn); }}
      .status-bad {{ color: var(--bad); }}
      .result {{ border: 1px solid var(--line); border-radius: 14px; padding: 12px; background: white; }}
      .eyebrow {{ font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); font-family: ui-sans-serif, system-ui, sans-serif; }}
      .title {{ font-size: 18px; margin: 6px 0; }}
      ul {{ margin: 8px 0 0 18px; padding: 0; }}
      li {{ margin: 4px 0; }}
      .hidden {{ display: none; }}
      .toolbar {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    </style>
  </head>
  <body>
    <div class=\"shell\">
      <div class=\"hero\">
        <h1>{title}</h1>
        <p>{summary}</p>
      </div>
      <div id=\"app\"></div>
    </div>
    <script>
      const persisted = {state_json};
      let state = JSON.parse(JSON.stringify(persisted));
      const app = document.getElementById('app');

      function payload(result) {{
        if (!result) return null;
        if (result.structuredContent) return result.structuredContent;
        try {{
          const text = result.content && result.content[0] && result.content[0].text;
          return text ? JSON.parse(text) : null;
        }} catch (_) {{
          return null;
        }}
      }}

      async function callTool(name, args) {{
        if (!window.openai || !window.openai.callTool) {{
          alert('Tool bridge is not available in this environment.');
          return null;
        }}
        return await window.openai.callTool(name, args || {{}});
      }}

      function clear(el) {{ el.innerHTML = ''; return el; }}
      function el(tag, props = {{}}, ...children) {{
        const node = document.createElement(tag);
        for (const [key, value] of Object.entries(props)) {{
          if (key === 'className') node.className = value;
          else if (key === 'text') node.textContent = value;
          else if (key === 'html') node.innerHTML = value;
          else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
          else if (value !== undefined && value !== null) node.setAttribute(key, value);
        }}
        for (const child of children) {{
          if (child === null || child === undefined) continue;
          node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
        }}
        return node;
      }}

      function maskPhone(phone) {{
        const digits = (phone || '').replace(/\\D/g, '');
        if (digits.length < 4) return phone || '';
        return '(***) ***-' + digits.slice(-4);
      }}

      async function renderAssociateWorkspace() {{
        const root = clear(app);
        const payloadState = state.payload;
        const filters = payloadState.filters || {{ occasion: '', budget_min: '', budget_max: '', top_k: 5 }};
        const left = el('div', {{ className: 'stack' }});
        const right = el('div', {{ className: 'stack' }});
        const searchInput = el('input', {{ value: payloadState.customerQuery || '', placeholder: 'Search name, email, or phone' }});
        const occasionInput = el('input', {{ value: filters.occasion || '', placeholder: 'Occasion (optional)' }});
        const budgetMinInput = el('input', {{ value: filters.budget_min ?? '', placeholder: 'Budget min', type: 'number', step: '0.01' }});
        const budgetMaxInput = el('input', {{ value: filters.budget_max ?? '', placeholder: 'Budget max', type: 'number', step: '0.01' }});
        const topKInput = el('input', {{ value: filters.top_k ?? 5, placeholder: 'Top K', type: 'number', min: '1', max: '12' }});
        const resultBox = el('div', {{ className: 'stack' }});
        const recBox = el('div', {{ className: 'stack' }});

        async function searchCustomers() {{
          const query = searchInput.value.trim();
          if (!query) return;
          const result = await callTool('fashion_find_customers', {{ query, limit: 10 }});
          payloadState.customerQuery = query;
          payloadState.customerResults = (payload(result) || {{ results: [] }}).results || [];
          payloadState.selectedCustomer = null;
          payloadState.recommendation = null;
          await renderAssociateWorkspace();
        }}

        async function loadRecommendations(customer) {{
          payloadState.selectedCustomer = customer;
          payloadState.filters = {{
            occasion: occasionInput.value.trim() || null,
            budget_min: budgetMinInput.value ? Number(budgetMinInput.value) : null,
            budget_max: budgetMaxInput.value ? Number(budgetMaxInput.value) : null,
            top_k: topKInput.value ? Number(topKInput.value) : 5,
          }};
          const result = await callTool('fashion_store_associate_recommend', {{
            store_id: payloadState.store.id,
            customer_id: customer.id,
            occasion: payloadState.filters.occasion,
            budget_min: payloadState.filters.budget_min,
            budget_max: payloadState.filters.budget_max,
            top_k: payloadState.filters.top_k,
          }});
          payloadState.recommendation = payload(result);
          await renderAssociateWorkspace();
        }}

        async function draftSms() {{
          if (!payloadState.selectedCustomer) return;
          const result = await callTool('fashion_prepare_customer_sms', {{
            store_id: payloadState.store.id,
            customer_id: payloadState.selectedCustomer.id,
            occasion: payloadState.filters.occasion,
            budget_min: payloadState.filters.budget_min,
            budget_max: payloadState.filters.budget_max,
            top_k: payloadState.filters.top_k,
          }});
          const draft = payload(result);
          if (draft && draft.message) payloadState.lastDraft = draft;
          await renderAssociateWorkspace();
        }}

        async function openDraftReview() {{
          if (!payloadState.lastDraft || !payloadState.lastDraft.message) return;
          await callTool('fashion_render_sms_review', {{ message_id: payloadState.lastDraft.message.id }});
        }}

        left.appendChild(el('div', {{ className: 'card stack' }},
          el('h2', {{ text: 'Customer Search' }}),
          searchInput,
          el('div', {{ className: 'toolbar' }},
            el('button', {{ onclick: searchCustomers, text: 'Search Customers' }}),
          ),
          resultBox,
        ));

        left.appendChild(el('div', {{ className: 'card stack' }},
          el('h2', {{ text: 'Associate Filters' }}),
          el('label', {{ text: 'Occasion' }}), occasionInput,
          el('label', {{ text: 'Budget Min' }}), budgetMinInput,
          el('label', {{ text: 'Budget Max' }}), budgetMaxInput,
          el('label', {{ text: 'Top K' }}), topKInput,
        ));

        if ((payloadState.customerResults || []).length) {{
          for (const customer of payloadState.customerResults) {{
            resultBox.appendChild(el('div', {{ className: 'result stack' }},
              el('div', {{ className: 'eyebrow', text: customer.match_reason + ' • score ' + customer.match_score.toFixed(2) }}),
              el('div', {{ className: 'title', text: customer.full_name }}),
              el('div', {{ className: 'meta', text: customer.email + ' • ' + customer.masked_phone }}),
              el('div', {{ className: 'meta', text: customer.home_store_name + ' • ' + customer.loyalty_tier }}),
              el('div', {{ className: 'toolbar' }},
                el('button', {{ onclick: () => loadRecommendations(customer), text: 'Select Customer' }}),
              )
            ));
          }}
        }} else {{
          resultBox.appendChild(el('div', {{ className: 'meta', text: 'Search results will appear here.' }}));
        }}

        const selected = payloadState.selectedCustomer;
        right.appendChild(el('div', {{ className: 'card stack' }},
          el('h2', {{ text: 'Selected Customer' }}),
          selected ? el('div', {{ className: 'stack' }},
            el('div', {{ className: 'title', text: selected.full_name || (selected.first_name + ' ' + selected.last_name) }}),
            el('div', {{ className: 'meta', text: selected.email + ' • ' + maskPhone(selected.phone_e164) }}),
            el('div', {{ className: 'meta', text: selected.home_store_name + ' • ' + selected.loyalty_tier }}),
            el('div', {{ className: 'toolbar' }},
              el('button', {{ onclick: draftSms, text: 'Draft SMS' }}),
              payloadState.lastDraft ? el('button', {{ className: 'secondary', onclick: openDraftReview, text: 'Open Draft Review' }}) : null,
            )
          ) : el('div', {{ className: 'meta', text: 'No customer selected yet.' }}),
          payloadState.lastDraft ? el('div', {{ className: 'pill', text: 'Latest draft ' + payloadState.lastDraft.message.id }}) : null,
        ));

        right.appendChild(el('div', {{ className: 'card stack' }},
          el('h2', {{ text: 'Recommendations' }}),
          recBox,
        ));

        const reco = payloadState.recommendation;
        if (reco && reco.recommendation && reco.recommendation.recommendations) {{
          for (const item of reco.recommendation.recommendations) {{
            const reasons = el('ul');
            for (const reason of item.reasons) reasons.appendChild(el('li', {{ text: reason }}));
            recBox.appendChild(el('div', {{ className: 'result stack' }},
              el('div', {{ className: 'eyebrow', text: item.category.replaceAll('_', ' ') }}),
              el('div', {{ className: 'title', text: item.title }}),
              el('div', {{ className: 'meta', text: item.brand + ' • $' + Number(item.price).toFixed(2) + ' • ' + item.availability }}),
              reasons,
              el('div', {{ className: 'pill', text: 'Score ' + Number(item.score).toFixed(2) }})
            ));
          }}
        }} else {{
          recBox.appendChild(el('div', {{ className: 'meta', text: 'Recommendations appear after selecting a customer.' }}));
        }}

        root.appendChild(el('div', {{ className: 'split' }}, left, right));
      }}

      async function renderSmsReview() {{
        const root = clear(app);
        const payloadState = state.payload;
        const message = payloadState.message;
        const textarea = el('textarea', {{ text: message.body_text }});
        textarea.value = message.body_text || '';
        const historyBox = el('div', {{ className: 'stack' }});
        const statusClass = message.status === 'sent' ? 'status-ok' : message.status === 'failed' ? 'status-bad' : 'status-warn';

        async function saveDraft() {{
          const result = await callTool('fashion_update_customer_sms_draft', {{
            message_id: message.id,
            body_text: textarea.value,
            selected_product_ids: message.product_ids,
          }});
          const updated = payload(result);
          if (updated && updated.message) {{
            payloadState.message = updated.message;
            await renderSmsReview();
          }}
        }}

        async function sendDraft() {{
          const result = await callTool('fashion_send_customer_sms', {{ message_id: message.id }});
          const updated = payload(result);
          if (updated) {{
            payloadState.message = updated;
            await renderSmsReview();
          }}
        }}

        async function loadHistory() {{
          const result = await callTool('fashion_customer_message_history', {{ customer_id: payloadState.customer.id, limit: 10 }});
          const history = payload(result);
          payloadState.history = history ? history.messages : [];
          await renderSmsReview();
        }}

        root.appendChild(el('div', {{ className: 'split' }},
          el('div', {{ className: 'card stack' }},
            el('h2', {{ text: 'SMS Draft' }}),
            el('div', {{ className: 'meta', text: payloadState.customer.full_name + ' • ' + payloadState.customer.email + ' • ' + maskPhone(payloadState.customer.phone_e164) }}),
            el('div', {{ className: 'meta', text: 'Store ' + payloadState.store.name + ' • Test send target ' + message.destination_e164 }}),
            el('div', {{ className: statusClass + ' meta', text: 'Current status: ' + message.status }}),
            textarea,
            el('div', {{ className: 'toolbar' }},
              el('button', {{ onclick: saveDraft, text: 'Save Draft' }}),
              el('button', {{ onclick: sendDraft, text: 'Send Test SMS' }}),
              el('button', {{ className: 'secondary', onclick: loadHistory, text: 'Load History' }}),
            ),
          ),
          el('div', {{ className: 'card stack' }},
            el('h2', {{ text: 'Selected Products' }}),
            ...((message.product_ids || []).map((id) => el('div', {{ className: 'pill', text: id }}))),
            el('h2', {{ text: 'Recent History' }}),
            historyBox,
          )
        ));

        const history = payloadState.history || [];
        if (!history.length) historyBox.appendChild(el('div', {{ className: 'meta', text: 'No history loaded yet.' }}));
        for (const row of history) {{
          historyBox.appendChild(el('div', {{ className: 'result stack' }},
            el('div', {{ className: 'eyebrow', text: row.status + ' • ' + row.id }}),
            el('div', {{ className: 'meta', text: row.destination_e164 }}),
            el('div', {{ className: 'meta', text: row.body_text.slice(0, 180) }}),
          ));
        }}
      }}

      async function renderMerchBoard() {{
        const root = clear(app);
        const payloadState = state.payload;
        const filters = payloadState.filters || {{ question: '', category: '', brand: '', price_band: '', occasion: '', lookback_days: 90, compare_mode: 'peer_and_prior_period', peer_mode: 'state_and_profile', top_k: 9 }};
        const questionInput = el('input', {{ value: filters.question || '', placeholder: 'Ask a merchandising question' }});
        const categoryInput = el('input', {{ value: filters.category || '', placeholder: 'Category' }});
        const brandInput = el('input', {{ value: filters.brand || '', placeholder: 'Brand' }});
        const priceBandSelect = el('select');
        for (const [label, value] of [['Any', ''], ['Under $250', 'under_250'], ['$250-$500', '250_500'], ['$500-$1000', '500_1000'], ['$1000+', '1000_plus']]) {{
          const option = el('option', {{ value, text: label }});
          if ((filters.price_band || '') === value) option.selected = true;
          priceBandSelect.appendChild(option);
        }}
        const occasionInput = el('input', {{ value: filters.occasion || '', placeholder: 'Occasion' }});
        const lookbackInput = el('input', {{ value: filters.lookback_days || 90, type: 'number', min: '7', max: '730' }});
        const compareSelect = el('select');
        for (const value of ['peer', 'prior_period', 'peer_and_prior_period']) {{
          const option = el('option', {{ value, text: value }});
          if ((filters.compare_mode || 'peer_and_prior_period') === value) option.selected = true;
          compareSelect.appendChild(option);
        }}
        const peerSelect = el('select');
        for (const value of ['state_and_profile', 'profile_type', 'all_profile_matches']) {{
          const option = el('option', {{ value, text: value }});
          if ((filters.peer_mode || 'state_and_profile') === value) option.selected = true;
          peerSelect.appendChild(option);
        }}
        const output = el('div', {{ className: 'stack' }});

        async function refresh(toolName) {{
          payloadState.filters = {{
            question: questionInput.value.trim() || null,
            category: categoryInput.value.trim() || null,
            brand: brandInput.value.trim() || null,
            price_band: priceBandSelect.value || null,
            occasion: occasionInput.value.trim() || null,
            lookback_days: Number(lookbackInput.value || 90),
            compare_mode: compareSelect.value,
            peer_mode: peerSelect.value,
            top_k: Number(filters.top_k || 9),
          }};
          const result = await callTool(toolName, {{
            store_id: payloadState.store.id,
            ...payloadState.filters,
          }});
          payloadState.lastTool = toolName;
          payloadState.lastResult = payload(result);
          await renderMerchBoard();
        }}

        root.appendChild(el('div', {{ className: 'card stack' }},
          el('h2', {{ text: 'Merchandising Filters' }}),
          questionInput,
          el('div', {{ className: 'grid' }},
            el('div', {{ className: 'stack' }}, el('label', {{ text: 'Category' }}), categoryInput),
            el('div', {{ className: 'stack' }}, el('label', {{ text: 'Brand' }}), brandInput),
            el('div', {{ className: 'stack' }}, el('label', {{ text: 'Price Band' }}), priceBandSelect),
            el('div', {{ className: 'stack' }}, el('label', {{ text: 'Occasion' }}), occasionInput),
            el('div', {{ className: 'stack' }}, el('label', {{ text: 'Lookback Days' }}), lookbackInput),
            el('div', {{ className: 'stack' }}, el('label', {{ text: 'Compare Mode' }}), compareSelect),
            el('div', {{ className: 'stack' }}, el('label', {{ text: 'Peer Mode' }}), peerSelect),
          ),
          el('div', {{ className: 'toolbar' }},
            el('button', {{ onclick: () => refresh('fashion_merch_action_recommendations'), text: 'Actions' }}),
            el('button', {{ className: 'secondary', onclick: () => refresh('fashion_merch_diagnostics'), text: 'Diagnostics' }}),
            el('button', {{ className: 'ghost', onclick: () => refresh('fashion_merch_trend_summary'), text: 'Trends' }}),
          ),
        ));

        root.appendChild(el('div', {{ className: 'card stack' }},
          el('h2', {{ text: 'Merchandising Output' }}),
          output,
        ));

        const result = payloadState.lastResult || payloadState.initialResult;
        const toolName = payloadState.lastTool || 'fashion_merch_action_recommendations';
        if (!result) {{
          output.appendChild(el('div', {{ className: 'meta', text: 'Run Actions, Diagnostics, or Trends to populate this board.' }}));
          return;
        }}
        if (toolName === 'fashion_merch_action_recommendations') {{
          const groups = {{ feature: [], deprioritize: [], promote: [] }};
          for (const row of result.recommendations || []) groups[row.action].push(row);
          const grid = el('div', {{ className: 'grid' }});
          for (const action of ['feature', 'deprioritize', 'promote']) {{
            const section = el('div', {{ className: 'result stack' }}, el('div', {{ className: 'eyebrow', text: action }}));
            for (const row of groups[action]) {{
              section.appendChild(el('div', {{ className: 'result stack' }},
                el('div', {{ className: 'title', text: row.title }}),
                el('div', {{ className: 'meta', text: row.brand + ' • ' + row.category + ' • ' + (row.price_band || 'n/a') }}),
                el('div', {{ className: 'meta', text: row.rationale }}),
                el('div', {{ className: 'pill', text: 'Metric ' + row.metric_value + ' • Peer Δ ' + row.peer_delta + ' • Prior Δ ' + (row.prior_period_delta ?? 0) }})
              ));
            }}
            grid.appendChild(section);
          }}
          output.appendChild(grid);
          return;
        }}
        if (toolName === 'fashion_merch_diagnostics') {{
          for (const row of result.insights || []) {{
            output.appendChild(el('div', {{ className: 'result stack' }},
              el('div', {{ className: 'title', text: row.subject }}),
              el('div', {{ className: 'meta', text: row.dimension + ' • ' + row.status }}),
              el('div', {{ className: 'meta', text: row.rationale }}),
              el('div', {{ className: 'pill', text: 'Current ' + row.current_value + ' • Peer ' + (row.peer_value ?? 'n/a') + ' • Prior ' + (row.prior_value ?? 'n/a') }})
            ));
          }}
          return;
        }}
        for (const row of result.highlights || []) {{
          output.appendChild(el('div', {{ className: 'result stack' }},
            el('div', {{ className: 'title', text: row.subject }}),
            el('div', {{ className: 'meta', text: row.rationale }}),
            el('div', {{ className: 'pill', text: 'Current ' + row.current_value + ' • Peer ' + (row.peer_value ?? 'n/a') + ' • Prior ' + (row.prior_value ?? 'n/a') + ' • Δ ' + row.pct_change + '%' }})
          ));
        }}
      }}

      async function boot() {{
        if (state.kind === 'associate_workspace') await renderAssociateWorkspace();
        else if (state.kind === 'sms') await renderSmsReview();
        else if (state.kind === 'merch') await renderMerchBoard();
      }}

      boot();
    </script>
  </body>
</html>"""


def _summary_text(state: dict) -> str:
    payload = state.get("payload", {})
    kind = state.get("kind")
    if kind == "associate_workspace":
        store = payload.get("store", {})
        return f"Associate workspace • {store.get('name', 'Store context unavailable')}"
    if kind == "sms":
        customer = payload.get("customer", {})
        message = payload.get("message", {})
        return f"{customer.get('full_name', 'Customer')} • draft status: {message.get('status', 'unknown')}"
    if kind == "merch":
        store = payload.get("store", {})
        return f"{store.get('name', 'Store context unavailable')} • merchandising workspace"
    return "Operator workspace"


def render_widget_html(title: str, state: dict) -> str:
    return _WIDGET_SHELL.format(
        title=html.escape(title),
        summary=html.escape(_summary_text(state)),
        state_json=json.dumps(state),
    )
