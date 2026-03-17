const root = document.getElementById("fashion-widget-root");
const meta = window.__FASHION_WIDGET__ || {};
const MODEL_CONTEXT_UPDATE_DEBOUNCE_MS = 600;
const DEFAULT_EXEC_TO_EMAIL = "djn12313@gmail.com";

const LOOKBACK_PRESET_OPTIONS = [
  { label: "Last 2 Weeks", value: "14" },
  { label: "Last 4 Weeks", value: "28" },
  { label: "Last 8 Weeks", value: "56" },
  { label: "Last Quarter (13 Weeks)", value: "90" },
  { label: "Last 6 Months", value: "180" },
  { label: "Last 12 Months", value: "365" },
  { label: "Custom Days", value: "custom" },
];

const DEFAULT_PAYLOAD = {
  filters: {
    lookback_days: 90,
    objective: "revenue",
    top_k_stores: 12,
    events: ["wedding", "holiday_party", "workwear"],
    store_id: null,
    store_ids: [],
    discount_pct: 10,
    floor_space_shift_pct: 5,
    from_category: "womens_apparel",
    to_category: "shoes",
    to_email: DEFAULT_EXEC_TO_EMAIL,
    autopilot_top_k: 6,
  },
  initial_result: null,
  last_result: null,
  last_tool: "fashion_exec_overview",
  initial_notice: null,
  uiHints: {
    emptyState: "Run one of the executive tabs to populate this workspace.",
    categoryOptions: [],
    events: ["wedding", "holiday_party", "workwear"],
    storeOptions: [],
  },
};

const state = {
  payload: clone(DEFAULT_PAYLOAD),
  ui: {
    lookbackPreset: "90",
    lookbackDays: "90",
    objective: "revenue",
    storeIds: [],
    eventsCsv: "wedding, holiday_party, workwear",
    discountPct: "10",
    floorSpaceShiftPct: "5",
    fromCategory: "womens_apparel",
    toCategory: "shoes",
    toEmail: DEFAULT_EXEC_TO_EMAIL,
    autopilotTopK: "6",
    notice: "",
    noticeTone: "info",
    isLoading: false,
    isSending: false,
  },
  runtime: {
    toolOutputApplied: false,
    userInteracted: false,
    modelContextHash: "",
    modelContextTimer: null,
    chartCleanupFns: [],
    autopilotDraftId: null,
  },
};

function isObject(value) {
  return value && typeof value === "object" && !Array.isArray(value);
}

function clone(value) {
  if (value === null || value === undefined) {
    return value;
  }
  return JSON.parse(JSON.stringify(value));
}

function parseJsonContentPayload(raw) {
  if (!raw || !Array.isArray(raw.content)) {
    return null;
  }
  const firstText = raw.content.find((part) => part && typeof part.text === "string");
  if (!firstText || !firstText.text) {
    return null;
  }
  try {
    return JSON.parse(firstText.text);
  } catch {
    return null;
  }
}

function parseToolPayload(raw) {
  if (isObject(raw?.structuredContent)) {
    return parseToolPayload(raw.structuredContent);
  }
  if (isObject(raw)) {
    return raw;
  }
  const parsed = parseJsonContentPayload(raw);
  return parsed && isObject(parsed) ? parsed : null;
}

function normalizeWorkspacePayload(raw) {
  if (!raw) {
    return null;
  }
  if (isObject(raw.structuredContent)) {
    return normalizeWorkspacePayload(raw.structuredContent);
  }
  if (isObject(raw.payload) && (raw.kind === "exec_workspace" || !raw.kind)) {
    return normalizeWorkspacePayload(raw.payload);
  }
  if (!isObject(raw)) {
    return null;
  }
  const hasWorkspaceFields =
    Object.prototype.hasOwnProperty.call(raw, "filters") ||
    Object.prototype.hasOwnProperty.call(raw, "initial_result") ||
    Object.prototype.hasOwnProperty.call(raw, "initialResult");
  if (!hasWorkspaceFields) {
    return null;
  }
  const rawFilters = isObject(raw.filters) ? raw.filters : {};
  const explicitStoreIds = Array.isArray(rawFilters.store_ids)
    ? rawFilters.store_ids.map((value) => String(value).trim()).filter(Boolean)
    : [];
  const legacyStoreId =
    typeof rawFilters.store_id === "string" && rawFilters.store_id.trim() ? rawFilters.store_id.trim() : null;
  const normalizedStoreIds = explicitStoreIds.length
    ? Array.from(new Set(explicitStoreIds))
    : legacyStoreId
      ? [legacyStoreId]
      : [];

  return {
    filters: {
      lookback_days: Number(rawFilters.lookback_days) > 0 ? Number(rawFilters.lookback_days) : 90,
      objective: typeof rawFilters.objective === "string" ? rawFilters.objective : "revenue",
      top_k_stores: Number(rawFilters.top_k_stores) > 0 ? Number(rawFilters.top_k_stores) : 12,
      events: Array.isArray(rawFilters.events) ? clone(rawFilters.events) : ["wedding", "holiday_party", "workwear"],
      store_id: legacyStoreId,
      store_ids: normalizedStoreIds,
      discount_pct: Number.isFinite(Number(rawFilters.discount_pct)) ? Number(rawFilters.discount_pct) : 10,
      floor_space_shift_pct:
        Number.isFinite(Number(rawFilters.floor_space_shift_pct)) ? Number(rawFilters.floor_space_shift_pct) : 5,
      from_category: typeof rawFilters.from_category === "string" ? rawFilters.from_category : "womens_apparel",
      to_category: typeof rawFilters.to_category === "string" ? rawFilters.to_category : "shoes",
      to_email:
        typeof rawFilters.to_email === "string" && rawFilters.to_email.trim()
          ? rawFilters.to_email.trim()
          : DEFAULT_EXEC_TO_EMAIL,
      autopilot_top_k: Number(rawFilters.autopilot_top_k) > 0 ? Number(rawFilters.autopilot_top_k) : 6,
    },
    initial_result: isObject(raw.initial_result) ? clone(raw.initial_result) : null,
    last_result: isObject(raw.last_result) ? clone(raw.last_result) : null,
    last_tool: typeof raw.last_tool === "string" ? raw.last_tool : "fashion_exec_overview",
    initial_notice: typeof raw.initial_notice === "string" ? raw.initial_notice : null,
    uiHints: isObject(raw.uiHints) ? clone(raw.uiHints) : clone(DEFAULT_PAYLOAD.uiHints),
  };
}

function applyWorkspacePayload(raw) {
  const payload = normalizeWorkspacePayload(raw);
  if (!payload) {
    return false;
  }
  state.payload = payload;
  state.ui.lookbackDays = String(payload.filters.lookback_days || 90);
  state.ui.lookbackPreset = resolveLookbackPreset(state.ui.lookbackDays);
  state.ui.objective = payload.filters.objective || "revenue";
  state.ui.storeIds = clone(payload.filters.store_ids || []);
  state.ui.eventsCsv = Array.isArray(payload.filters.events) ? payload.filters.events.join(", ") : "wedding, holiday_party, workwear";
  state.ui.discountPct = String(payload.filters.discount_pct ?? 10);
  state.ui.floorSpaceShiftPct = String(payload.filters.floor_space_shift_pct ?? 5);
  state.ui.fromCategory = payload.filters.from_category || "";
  state.ui.toCategory = payload.filters.to_category || "";
  state.ui.toEmail = (payload.filters.to_email || DEFAULT_EXEC_TO_EMAIL).trim() || DEFAULT_EXEC_TO_EMAIL;
  state.ui.autopilotTopK = String(payload.filters.autopilot_top_k ?? 6);
  if (payload.initial_notice) {
    setNotice(payload.initial_notice);
  }
  return true;
}

function applyInitialToolOutput(raw, options = {}) {
  const force = options.force === true;
  if (!force && (state.runtime.toolOutputApplied || state.runtime.userInteracted)) {
    return false;
  }
  const applied = applyWorkspacePayload(raw);
  if (applied) {
    state.runtime.toolOutputApplied = true;
  }
  return applied;
}

function markUserInteraction() {
  state.runtime.userInteracted = true;
}

function callTool(name, args) {
  if (!window.openai || typeof window.openai.callTool !== "function") {
    return Promise.resolve({ __toolError: "Tool bridge is unavailable in this environment." });
  }
  return window.openai
    .callTool(name, args)
    .catch((error) => ({ __toolError: error instanceof Error ? error.message : "Tool invocation failed." }));
}

function clear(node) {
  node.innerHTML = "";
  return node;
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.entries(props).forEach(([key, value]) => {
    if (value === null || value === undefined) {
      return;
    }
    if (key === "className") {
      node.className = value;
      return;
    }
    if (key === "text") {
      node.textContent = String(value);
      return;
    }
    if (key === "value") {
      node.value = String(value);
      return;
    }
    if (key === "checked") {
      node.checked = Boolean(value);
      return;
    }
    if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
      return;
    }
    node.setAttribute(key, String(value));
  });
  children.forEach((child) => {
    if (child === null || child === undefined) {
      return;
    }
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  });
  return node;
}

function compactNumber(value, digits = 1) {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "-";
  }
  return new Intl.NumberFormat("en-US", {
    notation: Math.abs(numeric) >= 1000 ? "compact" : "standard",
    maximumFractionDigits: digits,
  }).format(numeric);
}

function formatCurrency(value) {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "-";
  }
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(numeric);
}

function formatPct(value, digits = 1) {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "-";
  }
  const sign = numeric > 0 ? "+" : "";
  return `${sign}${numeric.toFixed(digits)}%`;
}

function setNotice(message, tone = "info") {
  state.ui.notice = message || "";
  state.ui.noticeTone = tone === "error" ? "error" : "info";
}

function activeResult() {
  if (isObject(state.payload.last_result)) {
    return state.payload.last_result;
  }
  if (isObject(state.payload.initial_result)) {
    return state.payload.initial_result;
  }
  return null;
}

function parseEventsCsv(raw) {
  if (typeof raw !== "string" || !raw.trim()) {
    return ["wedding", "holiday_party", "workwear"];
  }
  const values = [];
  raw.split(",").forEach((item) => {
    const token = item.trim().toLowerCase().replace(/\s+/g, "_");
    if (!token || values.includes(token)) {
      return;
    }
    values.push(token);
  });
  return values.length ? values : ["wedding", "holiday_party", "workwear"];
}

function normalizeStoreIds(raw) {
  if (!Array.isArray(raw)) {
    return [];
  }
  const unique = [];
  raw.forEach((item) => {
    const value = String(item || "").trim();
    if (!value || unique.includes(value)) {
      return;
    }
    unique.push(value);
  });
  return unique;
}

function parsePositiveInt(value, fallback, min, max) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.max(min, Math.min(max, Math.round(parsed)));
}

function resolveLookbackPreset(daysValue) {
  const normalized = String(parsePositiveInt(daysValue, 90, 7, 730));
  const supported = new Set(["14", "28", "56", "90", "180", "365"]);
  return supported.has(normalized) ? normalized : "custom";
}

function normalizeLookbackDays(value, preset) {
  if (preset && preset !== "custom") {
    return parsePositiveInt(preset, 90, 7, 730);
  }
  return parsePositiveInt(value, 90, 7, 730);
}

function buildModelContextPayload() {
  return {
    workspace: "exec_workspace",
    active_tool: state.payload.last_tool || "fashion_exec_overview",
    lookback_days: normalizeLookbackDays(state.ui.lookbackDays, state.ui.lookbackPreset),
    objective: state.ui.objective,
    store_ids: normalizeStoreIds(state.ui.storeIds),
    events: parseEventsCsv(state.ui.eventsCsv),
    to_email: state.ui.toEmail || DEFAULT_EXEC_TO_EMAIL,
    autopilot_draft_id: state.runtime.autopilotDraftId || null,
  };
}

function queueModelContextUpdate(options = {}) {
  if (!window.parent || typeof window.parent.postMessage !== "function") {
    return;
  }
  const payload = buildModelContextPayload();
  const serialized = JSON.stringify(payload);
  if (!options.force && serialized === state.runtime.modelContextHash) {
    return;
  }
  state.runtime.modelContextHash = serialized;
  const send = () => {
    state.runtime.modelContextTimer = null;
    window.parent.postMessage(
      {
        jsonrpc: "2.0",
        id: `ctx_${Date.now()}`,
        method: "ui/update-model-context",
        params: {
          content: [{ type: "text", text: `Workspace context:\n${serialized}` }],
        },
      },
      "*",
    );
  };
  if (state.runtime.modelContextTimer) {
    window.clearTimeout(state.runtime.modelContextTimer);
    state.runtime.modelContextTimer = null;
  }
  if (options.immediate) {
    send();
    return;
  }
  state.runtime.modelContextTimer = window.setTimeout(send, MODEL_CONTEXT_UPDATE_DEBOUNCE_MS);
}

function buildCommonArgs() {
  const args = {
    lookback_days: normalizeLookbackDays(state.ui.lookbackDays, state.ui.lookbackPreset),
  };
  const selectedStoreIds = normalizeStoreIds(state.ui.storeIds);
  if (selectedStoreIds.length) {
    args.store_ids = selectedStoreIds;
  }
  return args;
}

function buildToolArgs(toolName) {
  const common = buildCommonArgs();
  if (toolName === "fashion_exec_overview") {
    const selectedStoreIds = normalizeStoreIds(state.ui.storeIds);
    return {
      ...common,
      objective: state.ui.objective || "revenue",
      top_k_stores: selectedStoreIds.length ? Math.max(5, selectedStoreIds.length) : 12,
    };
  }
  if (toolName === "fashion_exec_event_readiness_radar") {
    return {
      ...common,
      events: parseEventsCsv(state.ui.eventsCsv),
    };
  }
  if (toolName === "fashion_exec_what_if_simulator") {
    return {
      ...common,
      discount_pct: Number(state.ui.discountPct || 0),
      floor_space_shift_pct: Number(state.ui.floorSpaceShiftPct || 0),
      from_category: state.ui.fromCategory || undefined,
      to_category: state.ui.toCategory || undefined,
    };
  }
  if (toolName === "fashion_exec_campaign_autopilot_prepare") {
    return {
      ...common,
      to_email: (state.ui.toEmail || DEFAULT_EXEC_TO_EMAIL).trim() || DEFAULT_EXEC_TO_EMAIL,
      lookback_days: normalizeLookbackDays(state.ui.lookbackDays, state.ui.lookbackPreset),
      top_k: parsePositiveInt(state.ui.autopilotTopK, 6, 1, 20),
      events: parseEventsCsv(state.ui.eventsCsv),
    };
  }
  return common;
}

function toolLabel(toolName) {
  if (toolName === "fashion_exec_event_readiness_radar") {
    return "Event Readiness Radar";
  }
  if (toolName === "fashion_exec_what_if_simulator") {
    return "What-if Simulator";
  }
  if (toolName === "fashion_exec_campaign_autopilot_prepare" || toolName === "fashion_exec_campaign_autopilot_send") {
    return "Campaign Autopilot";
  }
  return "Executive Overview";
}

function tabProblemStatement(toolName) {
  if (toolName === "fashion_exec_event_readiness_radar") {
    return "Problem: Which store-event demand spikes are at risk so we can intervene early with transfers or bounded promotions?";
  }
  if (toolName === "fashion_exec_what_if_simulator") {
    return "Problem: Before execution, what revenue and margin impact should we expect from category reallocation plus pricing/space levers?";
  }
  if (toolName === "fashion_exec_campaign_autopilot_prepare" || toolName === "fashion_exec_campaign_autopilot_send") {
    return "Problem: Which campaign opportunities clear guardrails and are ready for manager approval and send?";
  }
  return "Problem: Are we on company plan, and which stores are driving or dragging performance?";
}

async function refreshExec(toolName) {
  markUserInteraction();
  state.ui.isLoading = true;
  setNotice(`Loading ${toolLabel(toolName)}...`);
  render();
  const result = await callTool(toolName, buildToolArgs(toolName));
  state.ui.isLoading = false;
  if (result.__toolError) {
    setNotice(result.__toolError, "error");
    render();
    return;
  }
  const payload = parseToolPayload(result);
  if (!payload || !isObject(payload)) {
    setNotice(`${toolLabel(toolName)} returned an unexpected payload.`, "error");
    render();
    return;
  }
  state.payload.last_result = payload;
  state.payload.last_tool = toolName;
  if (toolName === "fashion_exec_campaign_autopilot_prepare" && typeof payload.draft_id === "string") {
    state.runtime.autopilotDraftId = payload.draft_id;
  }
  setNotice(`${toolLabel(toolName)} loaded.`);
  queueModelContextUpdate();
  render();
}

async function sendAutopilotDraft() {
  markUserInteraction();
  const active = activeResult();
  const draftId = (active && typeof active.draft_id === "string" && active.draft_id) || state.runtime.autopilotDraftId;
  if (!draftId) {
    setNotice("Prepare an autopilot draft before sending.", "error");
    render();
    return;
  }
  state.ui.isSending = true;
  setNotice("Sending campaign package...");
  render();
  const result = await callTool("fashion_exec_campaign_autopilot_send", { draft_id: draftId, approved: true });
  state.ui.isSending = false;
  if (result.__toolError) {
    setNotice(result.__toolError, "error");
    render();
    return;
  }
  const payload = parseToolPayload(result);
  if (!payload || !isObject(payload)) {
    setNotice("Autopilot send returned an unexpected payload.", "error");
    render();
    return;
  }
  state.payload.last_result = payload;
  state.payload.last_tool = "fashion_exec_campaign_autopilot_send";
  const status = typeof payload.status === "string" ? payload.status : "";
  if (status === "sent") {
    setNotice(`Campaign package sent to ${payload.to_email || state.ui.toEmail}.`);
  } else {
    setNotice(payload.error_message || "Campaign send failed.", "error");
  }
  queueModelContextUpdate();
  render();
}

function registerChartCleanup(cleanup) {
  if (typeof cleanup !== "function") {
    return;
  }
  state.runtime.chartCleanupFns.push(cleanup);
}

function teardownCharts() {
  const cleanups = Array.isArray(state.runtime.chartCleanupFns) ? [...state.runtime.chartCleanupFns] : [];
  state.runtime.chartCleanupFns = [];
  cleanups.forEach((cleanup) => {
    try {
      cleanup();
    } catch {
      // Best effort.
    }
  });
}

function kpi(label, value) {
  return el(
    "div",
    { className: "fw-kpi" },
    el("span", { className: "fw-kpi-label", text: label }),
    el("strong", { className: "fw-kpi-value", text: value }),
  );
}

function renderOverview(result) {
  const stores = Array.isArray(result?.stores) ? result.stores : [];
  const trend = Array.isArray(result?.trend) ? result.trend : [];

  const panel = el(
    "div",
    { className: "fw-list" },
    el(
      "section",
      { className: "fw-panel" },
      el(
        "div",
        { className: "fw-kpi-strip" },
        kpi("Revenue", formatCurrency(result?.total_revenue)),
        kpi("Units", compactNumber(result?.total_units, 0)),
        kpi("Margin", formatPct(Number(result?.margin_rate || 0) * 100, 1)),
        kpi("Vs Prior", formatPct(result?.revenue_delta_pct, 1)),
      ),
    ),
    trend.length
      ? el(
          "section",
          { className: "fw-panel" },
          el("h3", { className: "fw-panel-title", text: "Revenue Trend" }),
          el(
            "div",
            { className: "fw-merch-chart-wrap" },
            el("canvas", { className: "fw-merch-chart-canvas", "data-role": "exec-overview-chart" }),
          ),
        )
      : null,
    el(
      "section",
      { className: "fw-panel" },
      el("h3", { className: "fw-panel-title", text: "Store Contributions" }),
      stores.length
        ? el(
            "div",
            { className: "fw-list" },
            ...stores.map((row) =>
              el(
                "article",
                { className: "fw-result" },
                el("h4", { className: "fw-panel-title", text: `${row.rank}. ${row.store_name}` }),
                el("p", { className: "fw-empty", text: `${row.city}, ${row.state}` }),
                el(
                  "div",
                  { className: "fw-chip-row" },
                  el("span", { className: "fw-chip", text: `Revenue ${formatCurrency(row.revenue)}` }),
                  el("span", { className: "fw-chip subtle", text: `Share ${formatPct(row.revenue_share_pct, 1)}` }),
                  el("span", { className: "fw-chip subtle", text: `Delta ${formatPct(row.revenue_delta_pct, 1)}` }),
                ),
              ),
            ),
          )
        : el("p", { className: "fw-empty", text: "No store rows available for selected scope." }),
    ),
  );

  const canvas = panel.querySelector("[data-role='exec-overview-chart']");
  if (canvas && typeof window?.Chart === "function") {
    const labels = trend.map((row) => row.period_start);
    const values = trend.map((row) => Number(row.revenue || 0));
    const chart = new window.Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Revenue",
            data: values,
            borderColor: "#1f5d8f",
            backgroundColor: "rgba(31,93,143,0.14)",
            fill: true,
            tension: 0.25,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: { legend: { display: false } },
        scales: {
          y: { ticks: { callback: (value) => formatCurrency(value) } },
        },
      },
    });
    registerChartCleanup(() => chart.destroy());
  }
  return panel;
}

function radarRiskTone(level) {
  const key = String(level || "").toLowerCase();
  if (key === "critical" || key === "high") {
    return "negative";
  }
  if (key === "medium") {
    return "caution";
  }
  return "positive";
}

function radarCoverageTone(weeks) {
  const numeric = Number(weeks || 0);
  if (!Number.isFinite(numeric) || numeric < 3) {
    return "negative";
  }
  if (numeric < 6) {
    return "caution";
  }
  return "positive";
}

function radarActionTone(action) {
  const key = String(action || "").toLowerCase();
  if (key === "transfer") {
    return "negative";
  }
  if (key === "promotion") {
    return "positive";
  }
  return "neutral";
}

function radarActionLabel(row) {
  const action = String(row?.recommendation?.action || "monitor").toLowerCase();
  if (action === "promotion") {
    const discount = Number(row?.recommendation?.suggested_discount_pct || 0);
    return discount > 0 ? `Promotion (${compactNumber(discount)}% off)` : "Promotion";
  }
  if (action === "transfer") {
    const sourceName = typeof row?.recommendation?.source_store_name === "string" ? row.recommendation.source_store_name : "";
    return sourceName ? `Transfer from ${sourceName}` : "Transfer replenishment";
  }
  return "Monitor";
}

function radarMeter(label, valueText, pct, tone) {
  return el(
    "div",
    { className: "fw-exec-radar-meter-row" },
    el(
      "div",
      { className: "fw-exec-radar-meter-head" },
      el("span", { text: label }),
      el("span", { text: valueText }),
    ),
    el(
      "div",
      { className: "fw-exec-radar-meter-track" },
      el("span", {
        className: `fw-exec-radar-meter-fill ${tone}`,
        style: `width:${clampNumber(pct, 0, 0, 100).toFixed(2)}%;`,
      }),
    ),
  );
}

function radarStoreGroups(rows) {
  const groups = new Map();
  rows.forEach((row) => {
    const key = String(row?.store_id || "").trim();
    if (!key) {
      return;
    }
    if (!groups.has(key)) {
      groups.set(key, {
        store_id: key,
        store_name: row.store_name,
        city: row.city,
        state: row.state,
        signals: [],
      });
    }
    groups.get(key).signals.push(row);
  });
  const stores = Array.from(groups.values());
  stores.forEach((store) => {
    store.signals.sort((a, b) => Number(b?.risk_score || 0) - Number(a?.risk_score || 0));
  });
  stores.sort((a, b) => {
    const maxA = Math.max(...a.signals.map((item) => Number(item?.risk_score || 0)), 0);
    const maxB = Math.max(...b.signals.map((item) => Number(item?.risk_score || 0)), 0);
    return maxB - maxA;
  });
  return stores;
}

function renderRadar(result) {
  const rows = Array.isArray(result?.rows) ? result.rows.slice(0, 30) : [];
  if (!rows.length) {
    return el("p", { className: "fw-empty", text: state.payload.uiHints.emptyState });
  }
  const stores = radarStoreGroups(rows);
  const criticalHighCount = rows.filter((row) => {
    const level = String(row?.risk_level || "").toLowerCase();
    return level === "critical" || level === "high";
  }).length;
  const transferCount = rows.filter((row) => String(row?.recommendation?.action || "").toLowerCase() === "transfer").length;
  const avgRisk = rows.reduce((acc, row) => acc + Number(row?.risk_score || 0), 0) / rows.length;
  const avgCover = rows.reduce((acc, row) => acc + Number(row?.coverage_weeks || 0), 0) / rows.length;

  return el(
    "div",
    { className: "fw-list" },
    el(
      "section",
      { className: "fw-panel" },
      el(
        "div",
        { className: "fw-kpi-strip" },
        kpi("Stores", compactNumber(stores.length, 0)),
        kpi("Signals", compactNumber(rows.length, 0)),
        kpi("High/Critical", compactNumber(criticalHighCount, 0)),
        kpi("Transfer Flags", compactNumber(transferCount, 0)),
        kpi("Avg Risk", compactNumber(avgRisk, 1)),
        kpi("Avg Cover", `${compactNumber(avgCover, 1)}w`),
      ),
      el(
        "div",
        { className: "fw-empty fw-exec-radar-note" },
        "This is store-event readiness, not product-level data. Each store card shows one signal per selected event.",
      ),
    ),
    el(
      "div",
      { className: "fw-list fw-exec-radar-list" },
      ...stores.map((store) => {
        const maxRisk = Math.max(...store.signals.map((item) => Number(item?.risk_score || 0)), 0);
        return el(
          "article",
          { className: "fw-result fw-exec-radar-card" },
          el(
            "div",
            { className: "fw-exec-radar-head" },
            el(
              "div",
              { className: "fw-chip-row" },
              el("span", { className: "fw-chip subtle", text: `${store.signals.length} events` }),
              el("span", {
                className: `fw-chip fw-merch-status-chip ${radarRiskTone(maxRisk >= 55 ? "high" : "low")}`,
                text: `Max Risk ${compactNumber(maxRisk, 1)}`,
              }),
            ),
            el("span", { className: "fw-chip fw-exec-radar-action subtle", text: "Store View" }),
          ),
          el("h4", { className: "fw-panel-title", text: store.store_name }),
          el("div", { className: "fw-empty fw-exec-radar-meta", text: `${store.city}, ${store.state}` }),
          el(
            "div",
            { className: "fw-exec-radar-signals" },
            ...store.signals.map((row) =>
              el(
                "div",
                { className: "fw-exec-radar-signal" },
                el(
                  "div",
                  { className: "fw-exec-radar-signal-head" },
                  el("span", { className: "fw-chip", text: row.event }),
                  el("span", {
                    className: `fw-chip fw-merch-status-chip ${radarActionTone(row?.recommendation?.action)}`,
                    text: radarActionLabel(row),
                  }),
                  el("span", {
                    className: `fw-chip fw-merch-status-chip ${radarRiskTone(row.risk_level)}`,
                    text: `Risk ${compactNumber(row.risk_score, 1)}`,
                  }),
                ),
                el(
                  "div",
                  { className: "fw-exec-radar-meters" },
                  radarMeter("Risk Score", `${compactNumber(row.risk_score, 1)} / 100`, Number(row.risk_score || 0), radarRiskTone(row.risk_level)),
                  radarMeter(
                    "Coverage Weeks",
                    `${compactNumber(row.coverage_weeks, 1)}w`,
                    (Number(row.coverage_weeks || 0) / 12) * 100,
                    radarCoverageTone(row.coverage_weeks),
                  ),
                ),
                el("div", { className: "fw-empty fw-exec-radar-rationale", text: row.recommendation?.rationale || "" }),
              ),
            ),
          ),
        );
      }),
    ),
  );
}

function renderSimulator(result) {
  return el(
    "div",
    { className: "fw-list" },
    el(
      "section",
      { className: "fw-panel" },
      el(
        "div",
        { className: "fw-kpi-strip" },
        kpi("Baseline Revenue", formatCurrency(result?.baseline_revenue)),
        kpi("Expected Revenue", formatCurrency(result?.expected_revenue)),
        kpi("Revenue Delta", formatCurrency(result?.revenue_delta)),
        kpi("Margin Delta", formatPct(Number(result?.margin_rate_delta || 0) * 100, 2)),
      ),
      el(
        "p",
        {
          className: "fw-empty",
          text: `Confidence band: ${formatCurrency(result?.confidence_interval_low)} to ${formatCurrency(result?.confidence_interval_high)}`,
        },
      ),
    ),
    el(
      "section",
      { className: "fw-panel" },
      el("h3", { className: "fw-panel-title", text: "Model Components" }),
      ...(Array.isArray(result?.components) && result.components.length
        ? result.components.map((component) =>
            el(
              "article",
              { className: "fw-result" },
              el("h4", { className: "fw-panel-title", text: component.name }),
              el("p", { className: "fw-empty", text: component.rationale || "" }),
              el(
                "div",
                { className: "fw-chip-row" },
                el("span", { className: "fw-chip", text: `Revenue ${formatCurrency(component.revenue_delta)}` }),
                el("span", { className: "fw-chip subtle", text: `Margin ${formatPct(Number(component.margin_rate_delta || 0) * 100, 2)}` }),
              ),
            ),
          )
        : [el("p", { className: "fw-empty", text: "No simulation components available." })]),
    ),
  );
}

function renderAutopilot(result) {
  const candidates = Array.isArray(result?.candidates) ? result.candidates : [];
  const status = typeof result?.status === "string" ? result.status : "";
  const canSend = status === "draft" && (typeof result?.draft_id === "string" || state.runtime.autopilotDraftId);
  return el(
    "div",
    { className: "fw-list" },
    el(
      "section",
      { className: "fw-panel" },
      el("h3", { className: "fw-panel-title", text: "Campaign Package" }),
      result?.draft_id ? el("p", { className: "fw-empty", text: `Draft ID: ${result.draft_id}` }) : null,
      result?.to_email ? el("p", { className: "fw-empty", text: `Recipient: ${result.to_email}` }) : null,
      result?.subject ? el("p", { className: "fw-empty", text: `Subject: ${result.subject}` }) : null,
      result?.body_text
        ? el("textarea", { className: "fw-textarea", rows: "10", readonly: "true", value: result.body_text })
        : null,
      canSend
        ? el(
            "button",
            {
              className: "fw-button",
              type: "button",
              disabled: state.ui.isSending ? "true" : null,
              onClick: () => {
                void sendAutopilotDraft();
              },
            },
            state.ui.isSending ? "Sending..." : "Approve + Send",
          )
        : null,
    ),
    el(
      "section",
      { className: "fw-panel" },
      el("h3", { className: "fw-panel-title", text: "Shortlist" }),
      candidates.length
        ? el(
            "div",
            { className: "fw-list" },
            ...candidates.map((item) =>
              el(
                "article",
                { className: "fw-result" },
                el("h4", { className: "fw-panel-title", text: `${item.store_name} - ${item.event}` }),
                el("p", { className: "fw-empty", text: `${item.city}, ${item.state}` }),
                el(
                  "div",
                  { className: "fw-chip-row" },
                  el("span", { className: "fw-chip", text: `Action ${item.action}` }),
                  el("span", { className: "fw-chip subtle", text: `Risk ${compactNumber(item.risk_score)} (${item.risk_level})` }),
                  item.suggested_discount_pct
                    ? el("span", { className: "fw-chip subtle", text: `${item.suggested_discount_pct}%` })
                    : null,
                ),
                el("p", { className: "fw-empty", text: item.rationale || "" }),
              ),
            ),
          )
        : el("p", { className: "fw-empty", text: "No candidates currently meet guardrails." }),
    ),
  );
}

function renderResults() {
  const result = activeResult();
  if (!result) {
    return el("p", { className: "fw-empty", text: state.payload.uiHints.emptyState });
  }
  const tool = state.payload.last_tool || "fashion_exec_overview";
  if (tool === "fashion_exec_event_readiness_radar") {
    return renderRadar(result);
  }
  if (tool === "fashion_exec_what_if_simulator") {
    return renderSimulator(result);
  }
  if (tool === "fashion_exec_campaign_autopilot_prepare" || tool === "fashion_exec_campaign_autopilot_send") {
    return renderAutopilot(result);
  }
  return renderOverview(result);
}

function buildSelect(currentValue, options, onChange) {
  const node = el("select", {
    className: "fw-input fw-select",
    onChange: (event) => {
      markUserInteraction();
      onChange(event.target.value);
      queueModelContextUpdate();
      render();
    },
  });
  options.forEach((option) => {
    const optionNode = el("option", { value: option.value, text: option.label });
    if (option.value === currentValue) {
      optionNode.selected = true;
    }
    node.appendChild(optionNode);
  });
  return node;
}

function clampNumber(value, fallback, min, max) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.max(min, Math.min(max, parsed));
}

function buildPercentLeverField({
  label,
  value,
  min,
  max,
  step,
  onNumberInput,
  onSliderInput,
}) {
  const sliderValue = String(clampNumber(value, 0, min, max));
  return el(
    "div",
    { className: "fw-field" },
    el("label", { className: "fw-label", text: label }),
    el("input", {
      className: "fw-input",
      type: "number",
      min: String(min),
      max: String(max),
      step: String(step),
      value: value,
      onInput: (event) => {
        markUserInteraction();
        onNumberInput(event.target.value);
        queueModelContextUpdate();
      },
    }),
    el("input", {
      className: "fw-range",
      type: "range",
      min: String(min),
      max: String(max),
      step: String(step),
      value: sliderValue,
      onInput: (event) => {
        markUserInteraction();
        onSliderInput(event.target.value);
        queueModelContextUpdate();
      },
    }),
  );
}

function storeSelectionSummary(selectedIds, optionsById) {
  if (!Array.isArray(selectedIds) || !selectedIds.length) {
    return "All stores";
  }
  if (selectedIds.length === 1) {
    const label = optionsById.get(selectedIds[0]);
    return label || "1 store selected";
  }
  return `${selectedIds.length} stores selected`;
}

function buildStoreMultiSelect(selectedStoreIds, options) {
  const normalizedSelected = normalizeStoreIds(selectedStoreIds);
  const selected = new Set(normalizedSelected);
  const normalizedOptions = [];
  (Array.isArray(options) ? options : []).forEach((option) => {
    const value = String(option.value || "").trim();
    const label = String(option.label || value).trim();
    if (!value || !label) {
      return;
    }
    normalizedOptions.push({ value, label });
  });
  const optionsById = new Map(normalizedOptions.map((item) => [item.value, item.label]));
  const details = el("details", { className: "fw-multi-select" });
  const summary = el("summary", {
    className: "fw-input fw-multi-select-summary",
    text: storeSelectionSummary(normalizedSelected, optionsById),
  });
  const list = el("div", { className: "fw-multi-select-list" });

  if (!normalizedOptions.length) {
    list.appendChild(el("p", { className: "fw-empty", text: "No stores available." }));
  } else {
    normalizedOptions.forEach((option) => {
      const checkbox = el("input", { type: "checkbox", checked: selected.has(option.value) ? "true" : null });
      const label = el(
        "label",
        { className: "fw-multi-select-option" },
        checkbox,
        el("span", { text: option.label }),
      );
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) {
          selected.add(option.value);
        } else {
          selected.delete(option.value);
        }
      });
      list.appendChild(label);
    });
  }

  const actions = el(
    "div",
    { className: "fw-multi-select-actions" },
    el(
      "button",
      {
        className: "fw-text-button",
        type: "button",
        onClick: () => {
          list.querySelectorAll("input[type='checkbox']").forEach((node) => {
            node.checked = false;
          });
          selected.clear();
        },
      },
      "Clear",
    ),
    el(
      "button",
      {
        className: "fw-button secondary",
        type: "button",
        onClick: () => {
          state.ui.storeIds = normalizeStoreIds(Array.from(selected));
          summary.textContent = storeSelectionSummary(state.ui.storeIds, optionsById);
          details.open = false;
          markUserInteraction();
          queueModelContextUpdate();
          render();
        },
      },
      "Apply",
    ),
  );

  details.appendChild(summary);
  details.appendChild(el("div", { className: "fw-multi-select-panel" }, list, actions));
  return details;
}

function render() {
  teardownCharts();
  const container = clear(root);
  const notice = state.ui.notice
    ? el("div", { className: `fw-notice ${state.ui.noticeTone === "error" ? "error" : ""}`, text: state.ui.notice })
    : null;
  const buildLabel =
    typeof meta.buildVersion === "string" && meta.buildVersion.trim()
      ? `build ${meta.buildVersion.trim()}`
      : null;

  const header = el(
    "header",
    { className: "fw-hero" },
    el(
      "div",
      { className: "fw-title-row" },
      el("h1", { className: "fw-title", text: meta.title || "Executive Overview Workspace" }),
      buildLabel ? el("span", { className: "fw-version", text: buildLabel }) : null,
    ),
    el(
      "p",
      {
        className: "fw-subtitle",
        text:
          meta.summary ||
          "Company-wide performance, event readiness risk detection, scenario simulation, and campaign package approvals.",
      },
    ),
  );

  const tool = state.payload.last_tool || "fashion_exec_overview";
  const tabs = [
    { label: "Overview", tool: "fashion_exec_overview" },
    { label: "Readiness Radar", tool: "fashion_exec_event_readiness_radar" },
    { label: "What-if Simulator", tool: "fashion_exec_what_if_simulator" },
    { label: "Campaign Autopilot", tool: "fashion_exec_campaign_autopilot_prepare" },
  ];

  const categoryOptions = [
    { label: "Any", value: "" },
    ...((Array.isArray(state.payload.uiHints?.categoryOptions) ? state.payload.uiHints.categoryOptions : []).map((item) => ({
      label: item.label || item.value,
      value: item.value,
    }))),
  ];
  const storeOptions = Array.isArray(state.payload.uiHints?.storeOptions) ? state.payload.uiHints.storeOptions : [];
  const selectedStoreCount = normalizeStoreIds(state.ui.storeIds).length;
  const normalizedLookbackDays = String(normalizeLookbackDays(state.ui.lookbackDays, state.ui.lookbackPreset));

  const lookbackPresetSelect = buildSelect(state.ui.lookbackPreset, LOOKBACK_PRESET_OPTIONS, (value) => {
    state.ui.lookbackPreset = value;
    if (value !== "custom") {
      state.ui.lookbackDays = value;
    }
  });

  const controlsPanel = el(
    "section",
    { className: "fw-panel" },
    notice,
    el(
      "div",
      { className: "fw-grid merch-filters" },
      el(
        "div",
        { className: "fw-field fw-span-full" },
        el("label", { className: "fw-label", text: "Store Scope (multi-select)" }),
        buildStoreMultiSelect(state.ui.storeIds, storeOptions),
        el(
          "p",
          {
            className: "fw-empty",
            text: selectedStoreCount ? `${selectedStoreCount} store(s) selected.` : "No stores selected: defaults to company-wide network.",
          },
        ),
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Lookback Window" }),
        lookbackPresetSelect,
      ),
      state.ui.lookbackPreset === "custom"
        ? el(
            "div",
            { className: "fw-field" },
            el("label", { className: "fw-label", text: "Custom Days" }),
            el("input", {
              className: "fw-input",
              type: "number",
              min: "7",
              max: "730",
              value: normalizedLookbackDays,
              onInput: (event) => {
                markUserInteraction();
                state.ui.lookbackDays = event.target.value;
                state.ui.lookbackPreset = "custom";
                queueModelContextUpdate();
              },
            }),
          )
        : null,
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Overview Objective" }),
        buildSelect(
          state.ui.objective,
          [
            { label: "Revenue", value: "revenue" },
            { label: "Margin", value: "margin" },
            { label: "Sell Through", value: "sell_through" },
          ],
          (value) => {
            state.ui.objective = value;
          },
        ),
      ),
      el(
        "div",
        { className: "fw-field fw-span-full" },
        el("h3", { className: "fw-panel-title", text: "Event + Autopilot Inputs" }),
        el("p", { className: "fw-empty", text: "Store scope + lookback + events apply to all tabs. Email and shortlist size apply to Campaign Autopilot." }),
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Events (comma separated)" }),
        el("input", {
          className: "fw-input",
          type: "text",
          value: state.ui.eventsCsv,
          onInput: (event) => {
            markUserInteraction();
            state.ui.eventsCsv = event.target.value;
            queueModelContextUpdate();
          },
        }),
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Manager Email" }),
        el("input", {
          className: "fw-input",
          type: "email",
          value: state.ui.toEmail,
          onInput: (event) => {
            markUserInteraction();
            state.ui.toEmail = event.target.value;
            queueModelContextUpdate();
          },
        }),
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Weekly Shortlist Size" }),
        el("input", {
          className: "fw-input",
          type: "number",
          min: "1",
          max: "20",
          value: state.ui.autopilotTopK,
          onInput: (event) => {
            markUserInteraction();
            state.ui.autopilotTopK = event.target.value;
            queueModelContextUpdate();
          },
        }),
      ),
      el(
        "div",
        { className: "fw-field fw-span-full" },
        el("h3", { className: "fw-panel-title", text: "What-if Scenario Inputs" }),
        el("p", { className: "fw-empty", text: "Configure category reallocation, then apply pricing/space levers on the Reallocate To category." }),
      ),
      el(
        "div",
        { className: "fw-field fw-span-full" },
        el("h4", { className: "fw-panel-title", text: "Category Reallocation Pair" }),
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Reallocate From Category" }),
        buildSelect(state.ui.fromCategory, categoryOptions, (value) => {
          state.ui.fromCategory = value;
        }),
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Reallocate To Category" }),
        buildSelect(state.ui.toCategory, categoryOptions, (value) => {
          state.ui.toCategory = value;
        }),
      ),
      el(
        "div",
        { className: "fw-field fw-span-full" },
        el("h4", { className: "fw-panel-title", text: "Reallocate-To Levers" }),
      ),
      buildPercentLeverField({
        label: "Discount (%)",
        value: state.ui.discountPct,
        min: 0,
        max: 60,
        step: 1,
        onNumberInput: (value) => {
          state.ui.discountPct = value;
        },
        onSliderInput: (value) => {
          state.ui.discountPct = value;
        },
      }),
      buildPercentLeverField({
        label: "Space Shift (%)",
        value: state.ui.floorSpaceShiftPct,
        min: -40,
        max: 40,
        step: 1,
        onNumberInput: (value) => {
          state.ui.floorSpaceShiftPct = value;
        },
        onSliderInput: (value) => {
          state.ui.floorSpaceShiftPct = value;
        },
      }),
    ),
    el(
      "div",
      { className: "fw-merch-nav" },
      el(
        "div",
        { className: "fw-tabs fw-merch-tabs" },
        ...tabs.map((tab) =>
          el(
            "button",
            {
              className: `fw-tab fw-merch-tab ${tab.tool === tool ? "active" : ""}`,
              type: "button",
              disabled: state.ui.isLoading ? "true" : null,
              onClick: () => {
                void refreshExec(tab.tool);
              },
            },
            state.ui.isLoading && tab.tool === tool ? "Loading..." : tab.label,
          ),
        ),
      ),
      el(
        "button",
        {
          className: "fw-button",
          type: "button",
          disabled: state.ui.isLoading ? "true" : null,
          onClick: () => {
            void refreshExec(tool);
          },
        },
        state.ui.isLoading ? "Refreshing..." : "Refresh",
      ),
    ),
  );

  const contextPanel = el(
    "section",
    { className: "fw-panel" },
    el("h2", { className: "fw-panel-title", text: toolLabel(tool) }),
    el("p", { className: "fw-empty fw-exec-problem", text: tabProblemStatement(tool) }),
    el("p", { className: "fw-empty", text: activeResult()?.summary || state.payload.uiHints.emptyState }),
  );

  container.appendChild(
    el(
      "div",
      { className: "fw-root" },
      header,
      controlsPanel,
      contextPanel,
      el("section", { className: "fw-panel" }, renderResults()),
    ),
  );
}

function boot() {
  applyWorkspacePayload(meta.initialPayload);
  applyInitialToolOutput(window.openai && window.openai.toolOutput, { force: true });
  queueModelContextUpdate({ force: true, immediate: true });
  render();
}

window.addEventListener(
  "openai:set_globals",
  (event) => {
    const globals = (event && event.detail && event.detail.globals) || {};
    const payloadChanged = applyInitialToolOutput(globals.toolOutput);
    if (payloadChanged) {
      queueModelContextUpdate();
      render();
    }
  },
  { passive: true },
);

window.addEventListener(
  "message",
  (event) => {
    if (event.source !== window.parent) {
      return;
    }
    const data = event.data;
    if (!isObject(data) || data.method !== "openai:tool_result") {
      return;
    }
    const payload = parseToolPayload(data.params);
    if (!payload) {
      return;
    }
    const changed = applyWorkspacePayload(payload);
    if (changed) {
      queueModelContextUpdate();
      render();
    }
  },
  { passive: true },
);

boot();
