const root = document.getElementById("fashion-widget-root");
const meta = window.__FASHION_WIDGET__ || {};
const MODEL_CONTEXT_UPDATE_DEBOUNCE_MS = 600;

const DEFAULT_PAYLOAD = {
  store: null,
  filters: {
    question: null,
    objective: "margin",
    category: null,
    brand: null,
    price_band: null,
    occasion: null,
    lookback_days: 90,
    compare_mode: "peer_and_prior_period",
    peer_mode: "state_and_profile",
    compare_store_id: null,
    top_k: 9,
  },
  initial_result: null,
  last_result: null,
  last_tool: "fashion_merch_action_recommendations",
  initial_notice: null,
  uiHints: {
    questionPlaceholder: "What should this store prioritize, promote, or deprioritize?",
    emptyState: "Run Prioritize, Diagnostics, or Trends to populate this workspace.",
    categoryOptions: [],
    compareStoreOptions: [{ value: "", label: "Auto peer set" }],
    actionDefinitions: {
      feature: "High-confidence winners for full-price visibility.",
      promote: "Inventory where campaign/offer can accelerate sell-through.",
      deprioritize: "Lower-priority items to reduce exposure.",
    },
  },
};

const state = {
  payload: clone(DEFAULT_PAYLOAD),
  ui: {
    question: "",
    objective: "margin",
    category: "",
    brand: "",
    priceBand: "",
    occasion: "",
    lookbackDays: "90",
    compareMode: "peer_and_prior_period",
    peerMode: "state_and_profile",
    compareStoreId: "",
    topK: "9",
    csvText: "",
    notice: "",
    noticeTone: "info",
    isLoading: false,
    isExporting: false,
  },
  runtime: {
    toolOutputApplied: false,
    userInteracted: false,
    modelContextHash: "",
    modelContextTimer: null,
    trendMetric: "revenue",
    trendHoverIndex: null,
    diagnosticsShowAll: false,
    diagnosticsExpanded: {},
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

function normalizeCategoryOptions(raw) {
  if (!Array.isArray(raw)) {
    return [];
  }
  const options = [];
  const seen = new Set();
  raw.forEach((item) => {
    if (!isObject(item)) {
      return;
    }
    const value = typeof item.value === "string" ? item.value.trim() : "";
    const label = typeof item.label === "string" ? item.label.trim() : "";
    if (!value || !label || seen.has(value)) {
      return;
    }
    seen.add(value);
    options.push({ value, label });
  });
  return options;
}

function normalizeActionDefinitions(raw) {
  const defaults = clone(DEFAULT_PAYLOAD.uiHints.actionDefinitions);
  if (!isObject(raw)) {
    return defaults;
  }
  for (const key of ["feature", "promote", "deprioritize"]) {
    if (typeof raw[key] === "string" && raw[key].trim()) {
      defaults[key] = raw[key].trim();
    }
  }
  return defaults;
}

function normalizeCompareStoreOptions(raw) {
  const defaults = [{ value: "", label: "Auto peer set" }];
  if (!Array.isArray(raw)) {
    return defaults;
  }
  const options = [];
  const seen = new Set();
  raw.forEach((item) => {
    if (!isObject(item)) {
      return;
    }
    const value = typeof item.value === "string" ? item.value.trim() : "";
    const label = typeof item.label === "string" ? item.label.trim() : "";
    if (!label || seen.has(value)) {
      return;
    }
    seen.add(value);
    options.push({ value, label });
  });
  if (!seen.has("")) {
    options.unshift(defaults[0]);
  }
  return options.length ? options : defaults;
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

function normalizeWorkspacePayload(raw) {
  if (!raw) {
    return null;
  }
  if (isObject(raw.structuredContent)) {
    return normalizeWorkspacePayload(raw.structuredContent);
  }
  if (isObject(raw.payload) && (raw.kind === "merch_workspace" || !raw.kind)) {
    return normalizeWorkspacePayload(raw.payload);
  }
  if (!isObject(raw)) {
    return null;
  }

  const hasWorkspaceFields =
    Object.prototype.hasOwnProperty.call(raw, "filters") ||
    Object.prototype.hasOwnProperty.call(raw, "initial_result") ||
    Object.prototype.hasOwnProperty.call(raw, "initialResult");
  if (!hasWorkspaceFields || !isObject(raw.store)) {
    return null;
  }

  const rawFilters = isObject(raw.filters) ? raw.filters : {};
  const initialResult =
    isObject(raw.initial_result) ? clone(raw.initial_result) : isObject(raw.initialResult) ? clone(raw.initialResult) : null;
  const lastResult =
    isObject(raw.last_result) ? clone(raw.last_result) : isObject(raw.lastResult) ? clone(raw.lastResult) : null;

  return {
    store: clone(raw.store),
    filters: {
      question: typeof rawFilters.question === "string" ? rawFilters.question : null,
      objective: typeof rawFilters.objective === "string" ? rawFilters.objective : "margin",
      category: typeof rawFilters.category === "string" ? rawFilters.category : null,
      brand: typeof rawFilters.brand === "string" ? rawFilters.brand : null,
      price_band: typeof rawFilters.price_band === "string" ? rawFilters.price_band : null,
      occasion: typeof rawFilters.occasion === "string" ? rawFilters.occasion : null,
      lookback_days:
        Number.isFinite(Number(rawFilters.lookback_days)) && Number(rawFilters.lookback_days) > 0
          ? Number(rawFilters.lookback_days)
          : 90,
      compare_mode: typeof rawFilters.compare_mode === "string" ? rawFilters.compare_mode : "peer_and_prior_period",
      peer_mode: typeof rawFilters.peer_mode === "string" ? rawFilters.peer_mode : "state_and_profile",
      compare_store_id: typeof rawFilters.compare_store_id === "string" ? rawFilters.compare_store_id : null,
      top_k:
        Number.isFinite(Number(rawFilters.top_k)) && Number(rawFilters.top_k) > 0
          ? Number(rawFilters.top_k)
          : 9,
    },
    initial_result: initialResult,
    last_result: lastResult,
    last_tool:
      typeof raw.last_tool === "string"
        ? raw.last_tool
        : typeof raw.lastTool === "string"
          ? raw.lastTool
          : "fashion_merch_action_recommendations",
    initial_notice: typeof raw.initial_notice === "string" ? raw.initial_notice : null,
    uiHints: {
      questionPlaceholder:
        raw.uiHints && typeof raw.uiHints.questionPlaceholder === "string"
          ? raw.uiHints.questionPlaceholder
          : DEFAULT_PAYLOAD.uiHints.questionPlaceholder,
      emptyState:
        raw.uiHints && typeof raw.uiHints.emptyState === "string"
          ? raw.uiHints.emptyState
          : DEFAULT_PAYLOAD.uiHints.emptyState,
      categoryOptions: normalizeCategoryOptions(raw.uiHints && raw.uiHints.categoryOptions),
      compareStoreOptions: normalizeCompareStoreOptions(raw.uiHints && raw.uiHints.compareStoreOptions),
      actionDefinitions: normalizeActionDefinitions(raw.uiHints && raw.uiHints.actionDefinitions),
    },
  };
}

function applyWorkspacePayload(raw) {
  const payload = normalizeWorkspacePayload(raw);
  if (!payload) {
    return false;
  }
  state.payload = payload;
  state.runtime.trendMetric = "revenue";
  state.runtime.trendHoverIndex = null;
  state.runtime.diagnosticsShowAll = false;
  state.runtime.diagnosticsExpanded = {};
  hydrateUiFromFilters(payload.filters);
  if (payload.initial_notice) {
    setNotice(payload.initial_notice);
  }
  queueModelContextUpdate({ immediate: true });
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

function hydrateUiFromFilters(filters) {
  if (!isObject(filters)) {
    return;
  }
  state.ui.question = typeof filters.question === "string" ? filters.question : "";
  state.ui.objective = typeof filters.objective === "string" ? filters.objective : "margin";
  state.ui.category = typeof filters.category === "string" ? filters.category : "";
  state.ui.brand = typeof filters.brand === "string" ? filters.brand : "";
  state.ui.priceBand = typeof filters.price_band === "string" ? filters.price_band : "";
  state.ui.occasion = typeof filters.occasion === "string" ? filters.occasion : "";
  state.ui.lookbackDays = String(filters.lookback_days || 90);
  state.ui.compareMode = typeof filters.compare_mode === "string" ? filters.compare_mode : "peer_and_prior_period";
  state.ui.peerMode = typeof filters.peer_mode === "string" ? filters.peer_mode : "state_and_profile";
  state.ui.compareStoreId = typeof filters.compare_store_id === "string" ? filters.compare_store_id : "";
  state.ui.topK = String(filters.top_k || 9);
}

function applyUiWidgetState(raw) {
  if (!isObject(raw)) {
    return false;
  }
  let changed = false;
  const textFields = [
    "question",
    "objective",
    "category",
    "brand",
    "priceBand",
    "occasion",
    "lookbackDays",
    "compareMode",
    "peerMode",
    "compareStoreId",
    "topK",
    "csvText",
  ];
  for (const key of textFields) {
    if (typeof raw[key] === "string" && raw[key] !== state.ui[key]) {
      state.ui[key] = raw[key];
      changed = true;
    }
  }
  if (typeof raw.lastTool === "string" && raw.lastTool !== state.payload.last_tool) {
    state.payload.last_tool = raw.lastTool;
    changed = true;
  }
  return changed;
}

function loadWidgetState() {
  if (!window.openai || !isObject(window.openai.widgetState)) {
    return;
  }
  applyUiWidgetState(window.openai.widgetState);
}

function persistWidgetState() {
  if (!window.openai || typeof window.openai.setWidgetState !== "function") {
    queueModelContextUpdate();
    return;
  }
  try {
    window.openai.setWidgetState({
      question: state.ui.question,
      objective: state.ui.objective,
      category: state.ui.category,
      brand: state.ui.brand,
      priceBand: state.ui.priceBand,
      occasion: state.ui.occasion,
      lookbackDays: state.ui.lookbackDays,
      compareMode: state.ui.compareMode,
      peerMode: state.ui.peerMode,
      compareStoreId: state.ui.compareStoreId,
      topK: state.ui.topK,
      csvText: state.ui.csvText,
      lastTool: state.payload.last_tool,
    });
  } catch {
    // Best-effort only.
  }
  queueModelContextUpdate();
}

function compactNumber(value) {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return String(value);
  }
  return new Intl.NumberFormat("en-US", {
    maximumFractionDigits: Math.abs(numeric) >= 1000 ? 0 : 2,
  }).format(numeric);
}

function humanizeToken(value) {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  const raw = String(value).trim();
  const aliases = {
    womens_apparel: "Women's Apparel",
    mens_apparel: "Men's Apparel",
    under_250: "Under $250",
    "250_500": "$250-$500",
    "500_1000": "$500-$1000",
    "1000_plus": "$1000+",
    peer_and_prior_period: "Peer + Prior Period",
    prior_period: "Prior Period",
    state_and_profile: "State + Profile",
    profile_type: "Profile Only",
    all_profile_matches: "All Profile Matches",
    sell_through: "Sell Through",
  };
  if (aliases[raw]) {
    return aliases[raw];
  }
  return raw.replace(/[_-]+/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function money(value) {
  if (value === null || value === undefined || value === "") {
    return "Price unavailable";
  }
  const numberValue = Number(value);
  if (!Number.isFinite(numberValue)) {
    return String(value);
  }
  return `$${numberValue.toFixed(2)}`;
}

function formatCurrencyCompact(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "-";
  }
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(numeric);
}

function formatSignedCompact(value, options = {}) {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return String(value);
  }
  const maximumFractionDigits = Number.isFinite(Number(options.maximumFractionDigits))
    ? Number(options.maximumFractionDigits)
    : Math.abs(numeric) >= 1000
      ? 1
      : 2;
  const sign = numeric > 0 ? "+" : numeric < 0 ? "-" : "";
  const formatted = new Intl.NumberFormat("en-US", {
    notation: Math.abs(numeric) >= 1000 ? "compact" : "standard",
    maximumFractionDigits,
  }).format(Math.abs(numeric));
  return `${sign}${formatted}`;
}

function formatSignedPercent(value, digits = 1) {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "-";
  }
  const sign = numeric > 0 ? "+" : numeric < 0 ? "-" : "";
  return `${sign}${Math.abs(numeric).toFixed(digits)}%`;
}

function formatDateLabel(value, withYear = false) {
  if (typeof value !== "string" || !value.trim()) {
    return "-";
  }
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: withYear ? "numeric" : undefined,
  }).format(parsed);
}

function stableTextHash(raw) {
  const text = String(raw || "");
  let hash = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(16);
}

function buildModelContextPayload() {
  const active = activeResult();
  return {
    workspace: "merch_workspace",
    store_id: state.payload.store?.id || null,
    store_name: state.payload.store?.name || null,
    active_tool: state.payload.last_tool || "fashion_merch_action_recommendations",
    objective: state.ui.objective,
    question: state.ui.question.trim() || null,
    category: state.ui.category.trim() || null,
    brand: state.ui.brand.trim() || null,
    price_band: state.ui.priceBand || null,
    occasion: state.ui.occasion.trim() || null,
    lookback_days: Number(state.ui.lookbackDays || 90),
    compare_mode: state.ui.compareMode,
    peer_mode: state.ui.peerMode,
    compare_store_id: state.ui.compareStoreId || null,
    top_k: Number(state.ui.topK || 9),
    row_count: rowCountForResult(active),
    csv_hash: stableTextHash(state.ui.csvText),
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

function setNotice(message, tone = "info") {
  state.ui.notice = message || "";
  state.ui.noticeTone = tone === "error" ? "error" : "info";
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

function toolLabel(toolName) {
  if (toolName === "fashion_merch_diagnostics") {
    return "Diagnostics";
  }
  if (toolName === "fashion_merch_trend_summary") {
    return "Trends";
  }
  return "Prioritize";
}

function viewFromTool(toolName) {
  if (toolName === "fashion_merch_diagnostics") {
    return "diagnostics";
  }
  if (toolName === "fashion_merch_trend_summary") {
    return "trends";
  }
  return "actions";
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

function rowCountForResult(result) {
  if (!result || !isObject(result)) {
    return 0;
  }
  if (Array.isArray(result.recommendations)) {
    return result.recommendations.length;
  }
  if (Array.isArray(result.insights)) {
    return result.insights.length;
  }
  if (Array.isArray(result.highlights)) {
    return result.highlights.length;
  }
  return 0;
}

function parsePositiveInt(value, fallback, min, max) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  const rounded = Math.round(parsed);
  return Math.max(min, Math.min(max, rounded));
}

function currentFilters() {
  return {
    question: state.ui.question.trim() || null,
    objective: state.ui.objective || "margin",
    category: state.ui.category.trim() || null,
    brand: state.ui.brand.trim() || null,
    price_band: state.ui.priceBand || null,
    occasion: state.ui.occasion.trim() || null,
    lookback_days: parsePositiveInt(state.ui.lookbackDays, 90, 7, 730),
    compare_mode: state.ui.compareMode || "peer_and_prior_period",
    peer_mode: state.ui.peerMode || "state_and_profile",
    compare_store_id: state.ui.compareStoreId.trim() || null,
    top_k: parsePositiveInt(state.ui.topK, 9, 1, 50),
  };
}

function syncPayloadFilters() {
  state.payload.filters = currentFilters();
}

function buildToolArgs(toolName) {
  const filters = currentFilters();
  const args = {
    store_id: state.payload.store?.id || null,
    question: filters.question,
    lookback_days: filters.lookback_days,
    category: filters.category,
    brand: filters.brand,
    price_band: filters.price_band,
    occasion: filters.occasion,
    compare_mode: filters.compare_mode,
    peer_mode: filters.peer_mode,
    compare_store_id: filters.compare_store_id,
  };
  if (toolName === "fashion_merch_action_recommendations") {
    args.objective = filters.objective;
    args.top_k = filters.top_k;
  }
  return Object.fromEntries(Object.entries(args).filter(([, value]) => value !== null && value !== ""));
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

async function refreshMerch(toolName) {
  markUserInteraction();
  if (!state.payload.store?.id) {
    setNotice("Select or resolve a store before running merchandising analysis.", "error");
    render();
    return;
  }

  state.ui.isLoading = true;
  syncPayloadFilters();
  persistWidgetState();
  setNotice(`Loading ${toolLabel(toolName)}...`);
  render();

  const result = await callTool(toolName, buildToolArgs(toolName));
  state.ui.isLoading = false;
  if (result.__toolError) {
    setNotice(result.__toolError, "error");
    render();
    return;
  }

  const data = parseToolPayload(result);
  if (!data || !isObject(data.store)) {
    setNotice(`${toolLabel(toolName)} returned an unexpected payload.`, "error");
    render();
    return;
  }

  state.payload.last_result = data;
  state.payload.last_tool = toolName;
  state.ui.csvText = "";
  state.runtime.trendMetric = "revenue";
  state.runtime.trendHoverIndex = null;
  state.runtime.diagnosticsShowAll = false;
  state.runtime.diagnosticsExpanded = {};
  syncPayloadFilters();
  persistWidgetState();
  setNotice(`${toolLabel(toolName)} loaded.`);
  render();
}

async function copyTextToClipboard(text) {
  const content = String(text || "");
  if (!content) {
    return false;
  }
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    try {
      await navigator.clipboard.writeText(content);
      return true;
    } catch {
      // Fall through to legacy path.
    }
  }
  const scratch = document.createElement("textarea");
  scratch.value = content;
  scratch.setAttribute("readonly", "true");
  scratch.style.position = "fixed";
  scratch.style.left = "-9999px";
  document.body.appendChild(scratch);
  scratch.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  }
  document.body.removeChild(scratch);
  return copied;
}

async function exportCsv() {
  markUserInteraction();
  if (!state.payload.store?.id) {
    setNotice("Select or resolve a store before exporting CSV.", "error");
    render();
    return;
  }
  const toolName = state.payload.last_tool || "fashion_merch_action_recommendations";
  const view = viewFromTool(toolName);
  const args = {
    view,
    ...buildToolArgs(toolName),
  };

  state.ui.isExporting = true;
  syncPayloadFilters();
  persistWidgetState();
  setNotice("Building CSV export...");
  render();

  const result = await callTool("fashion_merch_export_csv", args);
  state.ui.isExporting = false;
  if (result.__toolError) {
    setNotice(result.__toolError, "error");
    render();
    return;
  }

  const payload = parseToolPayload(result);
  if (!payload || typeof payload.csv_text !== "string") {
    setNotice("CSV export returned an unexpected payload.", "error");
    render();
    return;
  }

  state.ui.csvText = payload.csv_text;
  persistWidgetState();
  const copied = await copyTextToClipboard(payload.csv_text);
  if (copied) {
    setNotice(`Copied CSV (${payload.row_count || 0} rows) to clipboard.`);
  } else {
    setNotice("CSV generated. Copy from the text area below.");
  }
  render();
}

function buildSelect(currentValue, options, onChange) {
  const node = el("select", {
    className: "fw-input fw-select",
    onChange: (event) => {
      markUserInteraction();
      onChange(event.target.value);
      persistWidgetState();
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

function resultHeader(result) {
  if (!result) {
    return null;
  }
  return el(
    "div",
    { className: "fw-kpi-strip" },
    kpi("Baseline", result.compare_mode === "prior_period" ? "Prior Period" : result.compare_store_name || "Peer Set"),
    kpi("Compare", humanizeToken(result.compare_mode || state.ui.compareMode)),
    kpi("Peers", String((result.peer_store_ids || []).length || 0)),
    kpi("Window", `${result.lookback_days || parsePositiveInt(state.ui.lookbackDays, 90, 7, 730)}d`),
  );
}

function kpi(label, value) {
  return el(
    "div",
    { className: "fw-kpi" },
    el("span", { className: "fw-kpi-label", text: label }),
    el("strong", { className: "fw-kpi-value", text: value }),
  );
}

function activeStoreName(result) {
  if (result && result.store && typeof result.store.name === "string" && result.store.name.trim()) {
    return result.store.name.trim();
  }
  if (state.payload.store && typeof state.payload.store.name === "string" && state.payload.store.name.trim()) {
    return state.payload.store.name.trim();
  }
  return "Current Store";
}

function peerBoxLabel(result) {
  if (result && typeof result.compare_store_name === "string" && result.compare_store_name.trim()) {
    return result.compare_store_name.trim();
  }
  return "Peer";
}

function renderActions(result) {
  const items = Array.isArray(result?.recommendations) ? result.recommendations : [];
  if (!items.length) {
    return el("p", { className: "fw-empty", text: state.payload.uiHints.emptyState });
  }
  const groups = { feature: [], promote: [], deprioritize: [] };
  items.forEach((item) => {
    const key = String(item.action || "").toLowerCase();
    if (groups[key]) {
      groups[key].push(item);
    }
  });

  const order = ["feature", "promote", "deprioritize"];
  return el(
    "div",
    { className: "fw-merch-groups" },
    ...order.map((action) =>
      el(
        "section",
        { className: "fw-panel" },
        el(
          "div",
          { className: "fw-section-head" },
          el(
            "div",
            {},
            el("div", { className: "fw-kicker", text: action }),
            el("h3", { className: "fw-panel-title", text: humanizeToken(action) }),
          ),
          el("span", { className: "fw-chip subtle", text: `${groups[action].length} products` }),
        ),
        el(
          "div",
          { className: "fw-list" },
          ...(groups[action].length
            ? groups[action].map((item) =>
                el(
                  "article",
                  { className: "fw-rec-card merch" },
                  el(
                    "div",
                    { className: "fw-rec-layout" },
                    el(
                      "div",
                      { className: "fw-rec-image-wrap" },
                      el("img", {
                        className: "fw-rec-image",
                        src: item.image_url || `${meta.assetBaseUrl}/demo/editorial-fallback.svg`,
                        alt: item.title || "Product image",
                        loading: "lazy",
                      }),
                    ),
                    el(
                      "div",
                      { className: "fw-rec-content" },
                      el("h3", { className: "fw-rec-title", text: item.title || item.product_id }),
                      el("p", { className: "fw-rec-brand", text: item.brand || "Unknown brand" }),
                      el("p", { className: "fw-rec-reason-inline", text: item.rationale || "No rationale provided" }),
                      item.link
                        ? el(
                            "a",
                            {
                              className: "fw-link",
                              href: item.link,
                              target: "_blank",
                              rel: "noreferrer",
                            },
                            "Open product",
                          )
                        : null,
                    ),
                    el(
                      "div",
                      { className: "fw-rec-side" },
                      el("p", { className: "fw-rec-price", text: money(item.price) }),
                      el(
                        "div",
                        { className: "fw-chip-row fw-chip-row-right" },
                        item.category ? el("span", { className: "fw-chip subtle", text: humanizeToken(item.category) }) : null,
                        item.price_band ? el("span", { className: "fw-chip subtle", text: humanizeToken(item.price_band) }) : null,
                        el("span", { className: "fw-chip", text: `metric ${compactNumber(item.metric_value)}` }),
                      ),
                      el(
                        "div",
                        { className: "fw-chip-row fw-chip-row-right" },
                        el("span", { className: "fw-chip subtle", text: `peer ${compactNumber(item.peer_delta)}` }),
                        item.prior_period_delta === null || item.prior_period_delta === undefined
                          ? null
                          : el("span", { className: "fw-chip subtle", text: `prior ${compactNumber(item.prior_period_delta)}` }),
                      ),
                    ),
                  ),
                ),
              )
            : [el("p", { className: "fw-empty", text: "No products in this action group for current filters." })]),
        ),
      ),
    ),
  );
}

function baselineLabel(result) {
  if (result?.compare_mode === "prior_period") {
    return "Prior Period";
  }
  return result?.compare_store_name ? result.compare_store_name : "Peer Set";
}

function diagnosticsKey(item, idx) {
  return `${String(item?.dimension || "metric")}::${String(item?.subject || "unknown")}::${idx}`;
}

function diagnosticsStatusClass(status) {
  const key = String(status || "").toLowerCase();
  if (key === "healthy_momentum") {
    return "positive";
  }
  if (key === "discount_led_growth") {
    return "caution";
  }
  if (key === "margin_risk" || key === "velocity_gap" || key === "conversion_gap") {
    return "negative";
  }
  return "neutral";
}

function valueToneClass(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric === 0) {
    return "neutral";
  }
  return numeric > 0 ? "positive" : "negative";
}

function diagnosticsDeltaChip(label, value, suffix = "") {
  return el(
    "span",
    { className: `fw-merch-micro-chip ${valueToneClass(value)}` },
    `${label} ${formatSignedCompact(value, { maximumFractionDigits: 1 })}${suffix}`,
  );
}

function diagnosticsDetailMetric(metricLabel, currentLabel, baselineLabelText, currentValue, peerValue, priorValue, formatter) {
  const baselineValue = peerValue !== null && peerValue !== undefined ? peerValue : priorValue;
  return el(
    "div",
    { className: "fw-merch-detail-card" },
    el("h4", { className: "fw-merch-detail-title", text: metricLabel }),
    el(
      "div",
      { className: "fw-merch-detail-row" },
      el("span", { className: "fw-merch-detail-label", text: currentLabel }),
      el("strong", { className: "fw-merch-detail-value", text: formatter(currentValue) }),
    ),
    el(
      "div",
      { className: "fw-merch-detail-row" },
      el("span", { className: "fw-merch-detail-label", text: baselineLabelText }),
      el("span", { className: "fw-merch-detail-value", text: formatter(baselineValue) }),
    ),
    el(
      "div",
      { className: "fw-merch-detail-row" },
      el("span", { className: "fw-merch-detail-label", text: "Prior" }),
      el("span", { className: "fw-merch-detail-value", text: formatter(priorValue) }),
    ),
  );
}

function renderDiagnostics(result) {
  const items = Array.isArray(result?.insights) ? result.insights : [];
  if (!items.length) {
    return el("p", { className: "fw-empty", text: state.payload.uiHints.emptyState });
  }

  const defaultLimit = 5;
  const visibleItems = state.runtime.diagnosticsShowAll ? items : items.slice(0, defaultLimit);
  const currentLabel = activeStoreName(result);
  const baselineLabelText = baselineLabel(result);
  const maxAbsDelta = Math.max(1, ...items.map((item) => Math.abs(Number(item?.delta) || 0)));

  return el(
    "div",
    { className: "fw-list fw-merch-diagnostics" },
    ...visibleItems.map((item, idx) => {
      const rowKey = diagnosticsKey(item, idx);
      const expanded = Boolean(state.runtime.diagnosticsExpanded[rowKey]);
      const delta = Number(item?.delta) || 0;
      const impactWidth = Math.min(50, (Math.abs(delta) / maxAbsDelta) * 50);
      const leftPct = delta >= 0 ? 50 : 50 - impactWidth;
      const priorUnits =
        item?.prior_units !== null && item?.prior_units !== undefined ? Number(item.prior_units) : null;
      const baselineUnits =
        item?.peer_units !== null && item?.peer_units !== undefined ? Number(item.peer_units) : priorUnits;
      const unitsDelta =
        Number.isFinite(Number(item?.current_units)) && Number.isFinite(baselineUnits)
          ? Number(item.current_units) - baselineUnits
          : null;
      const priorMargin =
        item?.prior_margin_pct !== null && item?.prior_margin_pct !== undefined
          ? Number(item.prior_margin_pct)
          : null;
      const baselineMargin =
        item?.peer_margin_pct !== null && item?.peer_margin_pct !== undefined
          ? Number(item.peer_margin_pct)
          : priorMargin;
      const marginDelta =
        Number.isFinite(Number(item?.current_margin_pct)) && Number.isFinite(baselineMargin)
          ? Number(item.current_margin_pct) - baselineMargin
          : null;

      return el(
        "article",
        { className: `fw-result fw-merch-impact-row ${expanded ? "expanded" : ""}` },
        el(
          "button",
          {
            className: "fw-merch-impact-head",
            type: "button",
            "aria-expanded": expanded ? "true" : "false",
            onClick: () => {
              markUserInteraction();
              state.runtime.diagnosticsExpanded[rowKey] = !expanded;
              render();
            },
          },
          el(
            "div",
            { className: "fw-merch-impact-main" },
            el(
              "div",
              { className: "fw-chip-row" },
              el("span", { className: `fw-chip fw-merch-status-chip ${diagnosticsStatusClass(item?.status)}`, text: humanizeToken(item?.status) }),
              el("span", { className: "fw-chip subtle", text: humanizeToken(item?.dimension) }),
            ),
            el("h3", { className: "fw-panel-title", text: item?.subject || "-" }),
            el("p", { className: "fw-empty", text: item?.rationale || "No rationale provided." }),
          ),
          el(
            "div",
            { className: "fw-merch-impact-values" },
            el("strong", { className: "fw-merch-impact-current", text: formatCurrencyCompact(item?.current_value) }),
            el("span", { className: `fw-merch-impact-delta ${valueToneClass(delta)}`, text: `${formatSignedCompact(delta, { maximumFractionDigits: 1 })} vs baseline` }),
          ),
        ),
        el(
          "div",
          { className: "fw-merch-impact-track" },
          el("span", { className: "fw-merch-impact-center" }),
          el("span", {
            className: `fw-merch-impact-fill ${valueToneClass(delta)}`,
            style: `left:${leftPct.toFixed(2)}%;width:${impactWidth.toFixed(2)}%;`,
          }),
        ),
        el(
          "div",
          { className: "fw-merch-micro-row" },
          diagnosticsDeltaChip("Revenue", delta),
          diagnosticsDeltaChip("Units", unitsDelta),
          diagnosticsDeltaChip("Margin", marginDelta, "pt"),
        ),
        expanded
          ? el(
              "div",
              { className: "fw-merch-impact-details" },
              diagnosticsDetailMetric(
                "Revenue",
                currentLabel,
                baselineLabelText,
                item?.current_value,
                item?.peer_value,
                item?.prior_value,
                formatCurrencyCompact,
              ),
              diagnosticsDetailMetric(
                "Units",
                currentLabel,
                baselineLabelText,
                item?.current_units,
                item?.peer_units,
                item?.prior_units,
                compactNumber,
              ),
              diagnosticsDetailMetric(
                "Margin",
                currentLabel,
                baselineLabelText,
                item?.current_margin_pct,
                item?.peer_margin_pct,
                item?.prior_margin_pct,
                (value) => {
                  if (value === null || value === undefined) {
                    return "-";
                  }
                  return `${compactNumber(value)}%`;
                },
              ),
            )
          : null,
      );
    }),
    items.length > defaultLimit
      ? el(
          "button",
          {
            className: "fw-text-button fw-merch-show-more",
            type: "button",
            onClick: () => {
              markUserInteraction();
              state.runtime.diagnosticsShowAll = !state.runtime.diagnosticsShowAll;
              render();
            },
          },
          state.runtime.diagnosticsShowAll ? `Show top ${defaultLimit}` : `Show ${items.length - defaultLimit} more`,
        )
      : null,
  );
}

function trendMetricConfig(metric) {
  if (metric === "units") {
    return {
      key: "units",
      title: "Weekly Units Trend",
      latestLabel: "Latest Units",
      currentField: "current_units",
      baselineField: "baseline_units",
      formatValue: (value) => compactNumber(value),
      ariaMetricLabel: "units",
    };
  }
  return {
    key: "revenue",
    title: "Weekly Revenue Trend",
    latestLabel: "Latest Revenue",
    currentField: "current_revenue",
    baselineField: "baseline_revenue",
    formatValue: (value) => formatCurrencyCompact(value),
    ariaMetricLabel: "revenue",
  };
}

function trendMomentumPct(series, currentField) {
  const valid = series.filter((point) => point[currentField] !== null);
  const recent = valid.slice(-4);
  if (recent.length < 2) {
    return null;
  }
  const start = Number(recent[0][currentField]);
  const end = Number(recent[recent.length - 1][currentField]);
  if (!Number.isFinite(start) || !Number.isFinite(end) || start === 0) {
    return null;
  }
  return ((end - start) / Math.abs(start)) * 100;
}

function setTrendHoverIndex(nextIndex) {
  const normalized = Number.isInteger(nextIndex) ? nextIndex : null;
  if (state.runtime.trendHoverIndex === normalized) {
    return;
  }
  state.runtime.trendHoverIndex = normalized;
  render();
}

function renderTrendChart(result) {
  const points = Array.isArray(result?.time_series) ? result.time_series : [];
  if (!points.length) {
    return null;
  }
  const metric = state.runtime.trendMetric === "units" ? "units" : "revenue";
  const metricConfig = trendMetricConfig(metric);
  const series = points.map((point) => {
    const currentRevenue = Number(point?.current_revenue);
    const baselineRevenue =
      point?.baseline_revenue === null || point?.baseline_revenue === undefined ? null : Number(point?.baseline_revenue);
    const currentUnits = Number(point?.current_units);
    const baselineUnits = point?.baseline_units === null || point?.baseline_units === undefined ? null : Number(point?.baseline_units);
    return {
      period_start: typeof point?.period_start === "string" ? point.period_start : "",
      current_revenue: Number.isFinite(currentRevenue) ? currentRevenue : null,
      baseline_revenue: baselineRevenue !== null && Number.isFinite(baselineRevenue) ? baselineRevenue : null,
      current_units: Number.isFinite(currentUnits) ? currentUnits : null,
      baseline_units: baselineUnits !== null && Number.isFinite(baselineUnits) ? baselineUnits : null,
    };
  });

  const finiteValues = series
    .flatMap((point) => [point[metricConfig.currentField], point[metricConfig.baselineField]])
    .filter((value) => value !== null && Number.isFinite(value));
  if (!finiteValues.length) {
    return null;
  }

  const width = 760;
  const height = 220;
  const chartLeft = 52;
  const chartRight = 14;
  const chartTop = 22;
  const chartBottom = 34;
  const plotWidth = width - chartLeft - chartRight;
  const plotHeight = height - chartTop - chartBottom;
  let minValue = Math.min(...finiteValues);
  let maxValue = Math.max(...finiteValues);
  if (maxValue === minValue) {
    const padding = Math.max(Math.abs(maxValue) * 0.08, 1);
    minValue -= padding;
    maxValue += padding;
  }
  const span = Math.max(maxValue - minValue, 1);
  const yTicks = Array.from({ length: 4 }, (_, idx) => maxValue - (span * idx) / 3);
  const xLabelIndices = Array.from(new Set([0, Math.floor((series.length - 1) / 2), series.length - 1])).filter(
    (idx) => idx >= 0 && idx < series.length,
  );

  const xFor = (idx) => {
    if (series.length <= 1) {
      return chartLeft + plotWidth / 2;
    }
    return chartLeft + (idx / (series.length - 1)) * plotWidth;
  };
  const yFor = (value) => {
    const normalized = (Number(value) - minValue) / span;
    return chartTop + (1 - normalized) * plotHeight;
  };

  const buildPath = (field) => {
    const segments = [];
    let started = false;
    series.forEach((point, idx) => {
      const value = point[field];
      if (value === null || value === undefined || !Number.isFinite(Number(value))) {
        started = false;
        return;
      }
      segments.push(`${started ? "L" : "M"} ${xFor(idx).toFixed(2)} ${yFor(value).toFixed(2)}`);
      started = true;
    });
    return segments.join(" ");
  };

  const currentPath = buildPath(metricConfig.currentField);
  const baselinePath = buildPath(metricConfig.baselineField);
  const currentLabel = activeStoreName(result);
  const baselineLabelText = baselineLabel(result);
  const latestPoint = series[series.length - 1];
  const latestCurrent = latestPoint ? latestPoint[metricConfig.currentField] : null;
  const latestBaseline = latestPoint ? latestPoint[metricConfig.baselineField] : null;
  const hasBaselineLatest = latestPoint && latestBaseline !== null && latestBaseline !== undefined;
  const latestDeltaPct =
    hasBaselineLatest && Number(latestBaseline) !== 0
      ? ((Number(latestCurrent) - Number(latestBaseline)) / Number(latestBaseline)) * 100
      : null;
  const momentumPct = trendMomentumPct(series, metricConfig.currentField);

  const fallbackIndex = series.length - 1;
  const activeIndex =
    Number.isInteger(state.runtime.trendHoverIndex) && state.runtime.trendHoverIndex >= 0 && state.runtime.trendHoverIndex < series.length
      ? state.runtime.trendHoverIndex
      : fallbackIndex;
  const activePoint = series[activeIndex];
  const activeCurrent = activePoint?.[metricConfig.currentField];
  const activeBaseline = activePoint?.[metricConfig.baselineField];
  const activeHasBaseline = activeBaseline !== null && activeBaseline !== undefined;
  const activeDeltaPct =
    activeHasBaseline && Number(activeBaseline) !== 0
      ? ((Number(activeCurrent) - Number(activeBaseline)) / Number(activeBaseline)) * 100
      : null;
  const activeX = xFor(activeIndex);
  const activeY = Number.isFinite(Number(activeCurrent)) ? yFor(activeCurrent) : chartTop + plotHeight / 2;

  const setHoverFromPointer = (event) => {
    const rect = event.currentTarget.getBoundingClientRect();
    if (!rect.width) {
      return;
    }
    const ratio = (event.clientX - rect.left) / rect.width;
    const viewX = Math.max(chartLeft, Math.min(width - chartRight, ratio * width));
    let nearest = 0;
    let nearestDistance = Number.POSITIVE_INFINITY;
    for (let idx = 0; idx < series.length; idx += 1) {
      const distance = Math.abs(viewX - xFor(idx));
      if (distance < nearestDistance) {
        nearest = idx;
        nearestDistance = distance;
      }
    }
    setTrendHoverIndex(nearest);
  };

  const activeTooltipTop = Math.max(12, activeY - 12);
  return el(
    "section",
    { className: "fw-panel fw-trend-chart-panel fw-merch-trend-panel" },
    el(
      "div",
      { className: "fw-merch-trend-head" },
      el("h3", { className: "fw-panel-title", text: metricConfig.title }),
      el(
        "div",
        { className: "fw-merch-segmented", role: "tablist", "aria-label": "Trend metric selector" },
        ...["revenue", "units"].map((metricOption) =>
          el(
            "button",
            {
              className: `fw-merch-segmented-btn ${metricOption === metric ? "active" : ""}`,
              type: "button",
              role: "tab",
              "aria-selected": metricOption === metric ? "true" : "false",
              onClick: () => {
                if (state.runtime.trendMetric === metricOption) {
                  return;
                }
                markUserInteraction();
                state.runtime.trendMetric = metricOption;
                state.runtime.trendHoverIndex = null;
                render();
              },
            },
            metricOption === "revenue" ? "Revenue" : "Units",
          ),
        ),
      ),
    ),
    el(
      "div",
      { className: "fw-merch-trend-kpis" },
      el(
        "div",
        { className: "fw-merch-trend-kpi" },
        el("span", { className: "fw-merch-trend-kpi-label", text: metricConfig.latestLabel }),
        el("strong", { className: "fw-merch-trend-kpi-value", text: metricConfig.formatValue(latestCurrent) }),
      ),
      el(
        "div",
        { className: "fw-merch-trend-kpi" },
        el("span", { className: "fw-merch-trend-kpi-label", text: `Vs ${baselineLabelText}` }),
        el(
          "strong",
          { className: `fw-merch-trend-kpi-value ${valueToneClass(latestDeltaPct)}` },
          hasBaselineLatest ? formatSignedPercent(latestDeltaPct, 1) : "No baseline",
        ),
      ),
      el(
        "div",
        { className: "fw-merch-trend-kpi" },
        el("span", { className: "fw-merch-trend-kpi-label", text: "4-Week Momentum" }),
        el(
          "strong",
          { className: `fw-merch-trend-kpi-value ${valueToneClass(momentumPct)}` },
          momentumPct === null ? "-" : formatSignedPercent(momentumPct, 1),
        ),
      ),
    ),
    el(
      "div",
      { className: "fw-trend-legend fw-merch-trend-legend" },
      el("span", { className: "fw-chip", text: currentLabel }),
      baselinePath ? el("span", { className: "fw-chip subtle", text: baselineLabelText }) : null,
    ),
    el(
      "div",
      { className: "fw-merch-trend-canvas" },
      el(
        "svg",
        {
          className: "fw-trend-chart fw-merch-trend-chart",
          viewBox: `0 0 ${width} ${height}`,
          role: "img",
          tabindex: "0",
          "aria-label": `Weekly ${metricConfig.ariaMetricLabel} trend chart with baseline comparison`,
          onMousemove: setHoverFromPointer,
          onKeydown: (event) => {
            if (event.key === "ArrowLeft") {
              event.preventDefault();
              setTrendHoverIndex(Math.max(0, activeIndex - 1));
            } else if (event.key === "ArrowRight") {
              event.preventDefault();
              setTrendHoverIndex(Math.min(series.length - 1, activeIndex + 1));
            }
          },
        },
        el("defs", {}, el("linearGradient", { id: "fw-merch-grid-fade", x1: "0", y1: "0", x2: "0", y2: "1" }, el("stop", { offset: "0%", "stop-color": "#f3f8fd" }), el("stop", { offset: "100%", "stop-color": "#ffffff" }))),
        el("rect", { x: "0", y: "0", width: String(width), height: String(height), fill: "url(#fw-merch-grid-fade)" }),
        ...yTicks.map((value) => {
          const y = yFor(value).toFixed(2);
          return el("line", {
            x1: chartLeft.toFixed(2),
            y1: y,
            x2: (width - chartRight).toFixed(2),
            y2: y,
            stroke: "#e4edf6",
            "stroke-width": "1",
          });
        }),
        el("line", {
          x1: activeX.toFixed(2),
          y1: chartTop.toFixed(2),
          x2: activeX.toFixed(2),
          y2: (height - chartBottom).toFixed(2),
          stroke: "#b3c9dc",
          "stroke-width": "1.1",
          "stroke-dasharray": "3 3",
        }),
        baselinePath
          ? el("path", {
              d: baselinePath,
              fill: "none",
              stroke: "#7d8e9f",
              "stroke-width": "2.1",
              "stroke-linecap": "round",
            })
          : null,
        el("path", {
          d: currentPath,
          fill: "none",
          stroke: "#1f5d8f",
          "stroke-width": "2.7",
          "stroke-linecap": "round",
        }),
        ...series.map((point, idx) => {
          const baselineValue = point[metricConfig.baselineField];
          if (baselineValue === null || baselineValue === undefined) {
            return null;
          }
          const isActive = idx === activeIndex;
          return el("circle", {
            cx: xFor(idx).toFixed(2),
            cy: yFor(baselineValue).toFixed(2),
            r: isActive ? "3.2" : "2.1",
            fill: "#7d8e9f",
            opacity: isActive ? "1" : "0.85",
          });
        }),
        ...series.map((point, idx) => {
          const currentValue = point[metricConfig.currentField];
          if (currentValue === null || currentValue === undefined) {
            return null;
          }
          const isActive = idx === activeIndex;
          return el("circle", {
            cx: xFor(idx).toFixed(2),
            cy: yFor(currentValue).toFixed(2),
            r: isActive ? "4.2" : "2.6",
            fill: "#1f5d8f",
            stroke: "#fff",
            "stroke-width": isActive ? "1.5" : "0.8",
            "aria-label": `${formatDateLabel(point.period_start, true)} ${metricConfig.ariaMetricLabel} ${metricConfig.formatValue(currentValue)}`,
          });
        }),
        ...yTicks.map((value) =>
          el("text", {
            x: String(chartLeft - 7),
            y: yFor(value).toFixed(2),
            "text-anchor": "end",
            "dominant-baseline": "middle",
            fill: "#617386",
            "font-size": "10.5",
            text: metricConfig.formatValue(value),
          }),
        ),
        ...xLabelIndices.map((idx, idxPosition) => {
          const anchor = idxPosition === 0 ? "start" : idxPosition === xLabelIndices.length - 1 ? "end" : "middle";
          return el("text", {
            x: xFor(idx).toFixed(2),
            y: String(height - 11),
            "text-anchor": anchor,
            fill: "#617386",
            "font-size": "10.5",
            text: formatDateLabel(series[idx].period_start, false),
          });
        }),
      ),
      el(
        "div",
        {
          className: "fw-merch-trend-tooltip",
          style: `left:${((activeX / width) * 100).toFixed(2)}%;top:${activeTooltipTop.toFixed(2)}px;`,
        },
        el("strong", { text: formatDateLabel(activePoint?.period_start, true) }),
        el("span", { text: `${currentLabel}: ${metricConfig.formatValue(activeCurrent)}` }),
        activeHasBaseline ? el("span", { text: `${baselineLabelText}: ${metricConfig.formatValue(activeBaseline)}` }) : null,
        activeHasBaseline ? el("span", { className: valueToneClass(activeDeltaPct), text: `Delta ${formatSignedPercent(activeDeltaPct, 1)}` }) : null,
      ),
    ),
    el("p", { className: "fw-empty", text: "Hover or use left/right arrows for week-level detail." }),
  );
}

function renderTrends(result) {
  const items = Array.isArray(result?.highlights) ? result.highlights : [];
  const trendChart = renderTrendChart(result);
  if (!items.length && !trendChart) {
    return el("p", { className: "fw-empty", text: state.payload.uiHints.emptyState });
  }
  return el(
    "div",
    { className: "fw-list" },
    trendChart,
    ...items.map((item) =>
      el(
        "article",
        { className: "fw-result" },
        el(
          "div",
          { className: "fw-chip-row" },
          el("span", { className: `fw-chip ${valueToneClass(item?.pct_change) === "negative" ? "subtle" : ""}`, text: `Delta ${formatSignedPercent(item?.pct_change, 1)}` }),
        ),
        el("h3", { className: "fw-panel-title", text: item.subject || "-" }),
        el("p", { className: "fw-empty", text: item.rationale || "No rationale provided." }),
        el(
          "div",
          { className: "fw-kpi-strip" },
          kpi(activeStoreName(result), formatCurrencyCompact(item.current_value)),
          kpi(peerBoxLabel(result), formatCurrencyCompact(item.peer_value)),
          kpi("Prior", formatCurrencyCompact(item.prior_value)),
        ),
      ),
    ),
  );
}

function renderResults() {
  const activeTool = state.payload.last_tool || "fashion_merch_action_recommendations";
  const result = activeResult();
  if (!result) {
    return el("section", { className: "fw-panel" }, el("p", { className: "fw-empty", text: state.payload.uiHints.emptyState }));
  }
  if (activeTool === "fashion_merch_diagnostics") {
    return renderDiagnostics(result);
  }
  if (activeTool === "fashion_merch_trend_summary") {
    return renderTrends(result);
  }
  return renderActions(result);
}

function render() {
  const container = clear(root);
  syncPayloadFilters();

  const notice = state.ui.notice
    ? el("div", { className: `fw-notice ${state.ui.noticeTone === "error" ? "error" : ""}`, text: state.ui.notice })
    : null;

  const buildLabel =
    typeof meta.buildVersion === "string" && meta.buildVersion.trim()
      ? `build ${meta.buildVersion.trim()}`
      : null;

  const activeTool = state.payload.last_tool || "fashion_merch_action_recommendations";
  const result = activeResult();
  const store = state.payload.store;

  const header = el(
    "header",
    { className: "fw-hero" },
    el(
      "div",
      { className: "fw-title-row" },
      el("h1", { className: "fw-title", text: meta.title || "Merchandising Workspace" }),
      buildLabel ? el("span", { className: "fw-version", text: buildLabel }) : null,
    ),
    el(
      "p",
      {
        className: "fw-subtitle",
        text:
          meta.summary ||
          "Evaluate store performance, compare peers, and generate CSV-ready merchandising actions.",
      },
    ),
  );

  const questionInput = el("input", {
    className: "fw-input",
    type: "text",
    value: state.ui.question,
    placeholder: state.payload.uiHints.questionPlaceholder,
    onInput: (event) => {
      markUserInteraction();
      state.ui.question = event.target.value;
      persistWidgetState();
    },
  });

  const categoryOptions = [
    { label: "Any", value: "" },
    ...(Array.isArray(state.payload.uiHints.categoryOptions) ? state.payload.uiHints.categoryOptions : []),
  ];
  const categorySelect = buildSelect(
    state.ui.category,
    categoryOptions,
    (value) => {
      state.ui.category = value;
    },
  );

  const objectiveSelect = buildSelect(
    state.ui.objective,
    [
      { label: "Margin", value: "margin" },
      { label: "Sell Through", value: "sell_through" },
      { label: "Revenue", value: "revenue" },
    ],
    (value) => {
      state.ui.objective = value;
    },
  );

  const priceBandSelect = buildSelect(
    state.ui.priceBand,
    [
      { label: "Any", value: "" },
      { label: "Under $250", value: "under_250" },
      { label: "$250-$500", value: "250_500" },
      { label: "$500-$1000", value: "500_1000" },
      { label: "$1000+", value: "1000_plus" },
    ],
    (value) => {
      state.ui.priceBand = value;
    },
  );

  const compareModeSelect = buildSelect(
    state.ui.compareMode,
    [
      { label: "Peer + Prior Period", value: "peer_and_prior_period" },
      { label: "Peer", value: "peer" },
      { label: "Prior Period", value: "prior_period" },
    ],
    (value) => {
      state.ui.compareMode = value;
    },
  );

  const peerModeSelect = buildSelect(
    state.ui.peerMode,
    [
      { label: "State + Profile", value: "state_and_profile" },
      { label: "Profile Type", value: "profile_type" },
      { label: "All Profile Matches", value: "all_profile_matches" },
    ],
    (value) => {
      state.ui.peerMode = value;
    },
  );

  const compareStoreSelect = buildSelect(
    state.ui.compareStoreId,
    Array.isArray(state.payload.uiHints.compareStoreOptions) && state.payload.uiHints.compareStoreOptions.length
      ? state.payload.uiHints.compareStoreOptions
      : [{ label: "Auto peer set", value: "" }],
    (value) => {
      state.ui.compareStoreId = value;
    },
  );

  const controlsPanel = el(
    "section",
    { className: "fw-panel" },
    el("h2", { className: "fw-panel-title", text: store ? `Store: ${store.name}` : "Merch Workspace" }),
    notice,
    store
      ? el("p", { className: "fw-empty", text: `${store.city}, ${store.state} • profile ${humanizeToken(store.profile_type)}` })
      : el("p", { className: "fw-empty", text: "No store resolved yet. Open from a store query in chat." }),
    el(
      "div",
      { className: "fw-grid merch-filters" },
      el("div", { className: "fw-field fw-span-full" }, el("label", { className: "fw-label", text: "Question" }), questionInput),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Objective" }),
        objectiveSelect,
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Category" }),
        categorySelect,
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Brand" }),
        el("input", {
          className: "fw-input",
          type: "text",
          value: state.ui.brand,
          placeholder: "Valentino",
          onInput: (event) => {
            markUserInteraction();
            state.ui.brand = event.target.value;
            persistWidgetState();
          },
        }),
      ),
      el("div", { className: "fw-field" }, el("label", { className: "fw-label", text: "Price Band" }), priceBandSelect),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Occasion" }),
        el("input", {
          className: "fw-input",
          type: "text",
          value: state.ui.occasion,
          placeholder: "wedding",
          onInput: (event) => {
            markUserInteraction();
            state.ui.occasion = event.target.value;
            persistWidgetState();
          },
        }),
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Lookback Days" }),
        el("input", {
          className: "fw-input",
          type: "number",
          min: "7",
          max: "730",
          value: state.ui.lookbackDays,
          onInput: (event) => {
            markUserInteraction();
            state.ui.lookbackDays = event.target.value;
            persistWidgetState();
          },
        }),
      ),
      el("div", { className: "fw-field" }, el("label", { className: "fw-label", text: "Compare Mode" }), compareModeSelect),
      el("div", { className: "fw-field" }, el("label", { className: "fw-label", text: "Peer Mode" }), peerModeSelect),
      el("div", { className: "fw-field" }, el("label", { className: "fw-label", text: "Compare To Store" }), compareStoreSelect),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Top K" }),
        el("input", {
          className: "fw-input",
          type: "number",
          min: "1",
          max: "50",
          value: state.ui.topK,
          onInput: (event) => {
            markUserInteraction();
            state.ui.topK = event.target.value;
            persistWidgetState();
          },
        }),
      ),
    ),
    el(
      "div",
      { className: "fw-tabs" },
      el(
        "button",
        {
          className: `fw-tab ${activeTool === "fashion_merch_action_recommendations" ? "active" : ""}`,
          type: "button",
          disabled: state.ui.isLoading ? "true" : null,
          onClick: () => {
            void refreshMerch("fashion_merch_action_recommendations");
          },
        },
        state.ui.isLoading && activeTool === "fashion_merch_action_recommendations" ? "Loading..." : "Prioritize",
      ),
      el(
        "button",
        {
          className: `fw-tab ${activeTool === "fashion_merch_diagnostics" ? "active" : ""}`,
          type: "button",
          disabled: state.ui.isLoading ? "true" : null,
          onClick: () => {
            void refreshMerch("fashion_merch_diagnostics");
          },
        },
        state.ui.isLoading && activeTool === "fashion_merch_diagnostics" ? "Loading..." : "Diagnostics",
      ),
      el(
        "button",
        {
          className: `fw-tab ${activeTool === "fashion_merch_trend_summary" ? "active" : ""}`,
          type: "button",
          disabled: state.ui.isLoading ? "true" : null,
          onClick: () => {
            void refreshMerch("fashion_merch_trend_summary");
          },
        },
        state.ui.isLoading && activeTool === "fashion_merch_trend_summary" ? "Loading..." : "Trends",
      ),
      el(
        "button",
        {
          className: "fw-button secondary",
          type: "button",
          disabled: state.ui.isExporting ? "true" : null,
          onClick: () => {
            void exportCsv();
          },
        },
        state.ui.isExporting ? "Exporting..." : "Copy CSV",
      ),
    ),
    activeTool === "fashion_merch_action_recommendations"
      ? el(
          "p",
          {
            className: "fw-empty",
            text: `Feature: ${state.payload.uiHints.actionDefinitions.feature} Promote: ${state.payload.uiHints.actionDefinitions.promote} Deprioritize: ${state.payload.uiHints.actionDefinitions.deprioritize}`,
          },
        )
      : null,
  );

  const contextPanel = el(
    "section",
    { className: "fw-panel" },
    el("h2", { className: "fw-panel-title", text: toolLabel(activeTool) }),
    result
      ? el("p", { className: "fw-empty", text: result.summary || result.parsed_intent || "Current merchandising frame" })
      : el("p", { className: "fw-empty", text: state.payload.uiHints.emptyState }),
    result ? resultHeader(result) : null,
  );

  const csvPanel = state.ui.csvText
    ? el(
        "section",
        { className: "fw-panel" },
        el("h2", { className: "fw-panel-title", text: "CSV Export" }),
        el("p", { className: "fw-empty", text: "Copy this CSV into Sheets or Excel." }),
        el("textarea", {
          className: "fw-textarea fw-csv-text",
          rows: "10",
          readonly: "true",
          value: state.ui.csvText,
        }),
      )
    : null;

  container.appendChild(
    el(
      "div",
      { className: "fw-root" },
      header,
      controlsPanel,
      contextPanel,
      el("section", { className: "fw-panel" }, renderResults()),
      csvPanel,
    ),
  );
}

function boot() {
  applyWorkspacePayload(meta.initialPayload);
  applyInitialToolOutput(window.openai && window.openai.toolOutput, { force: true });
  loadWidgetState();
  queueModelContextUpdate({ force: true, immediate: true });
  render();
}

window.addEventListener(
  "openai:set_globals",
  (event) => {
    const globals = (event && event.detail && event.detail.globals) || {};
    const payloadChanged = applyInitialToolOutput(globals.toolOutput);
    const uiChanged = applyUiWidgetState(globals.widgetState);
    if (uiChanged) {
      queueModelContextUpdate();
    }
    if (payloadChanged || uiChanged) {
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
    if (!data || typeof data !== "object") {
      return;
    }
    if (data.jsonrpc !== "2.0") {
      return;
    }
    if (data.method !== "ui/notifications/tool-result") {
      return;
    }
    const payloadChanged = applyWorkspacePayload(data.params);
    if (payloadChanged) {
      render();
    }
  },
  { passive: true },
);

boot();
