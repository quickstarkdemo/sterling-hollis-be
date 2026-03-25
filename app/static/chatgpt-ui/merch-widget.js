const root = document.getElementById("fashion-widget-root");
const meta = window.__FASHION_WIDGET__ || {};

const TOOL_BY_VIEW = {
  inventory: "fashion_merch_inventory_view",
  recommendations: "fashion_merch_action_recommendations",
  mix_analysis: "fashion_merch_product_mix_recommendations",
};

const TOOL_TO_VIEW = {
  fashion_merch_inventory_view: "inventory",
  fashion_merch_action_recommendations: "recommendations",
  fashion_merch_product_mix_recommendations: "mix_analysis",
  fashion_merch_diagnostics: "diagnostics",
  fashion_merch_trend_summary: "trends",
};

const DEFAULT_PAYLOAD = {
  store: null,
  strategy_context: null,
  inventory_check: null,
  inventory_products: null,
  filters: {
    question: null,
    objective: "margin",
    category: null,
    brand: null,
    price_band: null,
    occasion: null,
    occasions: [],
    lookback_days: 90,
    inventory_scope: "combined",
    future_window_days: 120,
    compare_mode: "peer_and_prior_period",
    peer_mode: "state_and_profile",
    compare_store_id: null,
    compare_store_ids: [],
    top_k: 9,
  },
  initial_result: null,
  last_result: null,
  last_tool: "fashion_merch_inventory_view",
  initial_notice: null,
  uiHints: {
    questionPlaceholder: "Optional context (e.g., wedding occasion, protect margin, next 8 weeks)",
    emptyState: "Use Inventory filters to view/export current and potential assortment rows.",
    categoryOptions: [],
    brandOptions: [],
    occasionOptions: [],
    compareStoreOptions: [{ value: "", label: "Auto peer set" }],
    features: {
      merchStrategyContextEnabled: false,
    },
  },
};

const state = {
  payload: clone(DEFAULT_PAYLOAD),
  ui: {
    activeView: "inventory",
    question: "",
    objective: "margin",
    category: "",
    selectedBrands: [],
    priceBand: "",
    selectedOccasions: [],
    lookbackDays: "90",
    inventoryScope: "combined",
    futureWindowDays: "120",
    compareMode: "peer_and_prior_period",
    peerMode: "state_and_profile",
    compareStoreId: "",
    topK: "9",
    strategyTagIntensity: "medium",
    strategyStoreId: "",
    advancedInsightsOpen: false,
    csvText: "",
    notice: "",
    noticeTone: "info",
    isLoading: false,
    isExporting: false,
    isSavingStrategyOverride: false,
    isResettingStrategyOverride: false,
    filtersDirty: false,
  },
  data: {
    inventory: null,
    recommendations: null,
    mix_analysis: null,
    diagnostics: null,
    trends: null,
  },
  recommendationOverrides: {},
};

function isObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function clone(value) {
  if (value === null || value === undefined) {
    return value;
  }
  return JSON.parse(JSON.stringify(value));
}

function parsePositiveInt(value, fallback, min, max) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  const rounded = Math.round(parsed);
  return Math.max(min, Math.min(max, rounded));
}

function humanizeToken(value) {
  const raw = String(value || "").trim();
  if (!raw) {
    return "-";
  }
  const aliases = {
    under_250: "Under $250",
    "250_500": "$250-$500",
    "500_1000": "$500-$1000",
    "1000_plus": "$1000+",
    peer_and_prior_period: "Peer + Prior Period",
    prior_period: "Prior Period",
    state_and_profile: "State + Profile",
    profile_type: "Profile Type",
    all_profile_matches: "All Profile Matches",
    sell_through: "Sell Through",
    mix_analysis: "Mix Analysis",
    current_inventory: "Current",
    potential_offer: "Potential",
  };
  if (aliases[raw]) {
    return aliases[raw];
  }
  return raw.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatCurrency(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "-";
  }
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(numeric);
}

function formatNumber(value, digits = 2) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "-";
  }
  return numeric.toLocaleString("en-US", { maximumFractionDigits: digits });
}

function formatDate(value) {
  const token = String(value || "").trim();
  if (!token) {
    return "-";
  }
  const parsed = new Date(`${token}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) {
    return token;
  }
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" }).format(parsed);
}

function parseBrandCsv(raw) {
  const source = String(raw || "").split(",");
  const tokens = [];
  source.forEach((item) => {
    const token = String(item || "").trim();
    if (!token) {
      return;
    }
    if (!tokens.some((existing) => existing.toLowerCase() === token.toLowerCase())) {
      tokens.push(token);
    }
  });
  return tokens;
}

function normalizeOccasionToken(raw) {
  const token = String(raw || "")
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, "_")
    .replace(/[^a-z0-9_]/g, "")
    .replace(/^_+|_+$/g, "");
  return token;
}

function parseOccasionValues(rawOccasion, rawOccasions) {
  const tokens = [];
  const pushToken = (value) => {
    const token = String(value || "").trim();
    if (!token) {
      return;
    }
    tokens.push(token);
  };
  if (typeof rawOccasion === "string") {
    rawOccasion
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean)
      .forEach(pushToken);
  }
  if (Array.isArray(rawOccasions)) {
    rawOccasions.forEach(pushToken);
  }
  return tokens;
}

function normalizeOccasionSelections(values, options = []) {
  const normalizedValues = [];
  const seen = new Set();
  const canonicalByToken = new Map();
  normalizeOptions(options).forEach((option) => {
    canonicalByToken.set(normalizeOccasionToken(option.value), option.value);
  });
  values.forEach((value) => {
    const token = normalizeOccasionToken(value);
    if (!token || seen.has(token)) {
      return;
    }
    seen.add(token);
    normalizedValues.push(canonicalByToken.get(token) || token);
  });
  return normalizedValues;
}

function serializeBrands(values) {
  if (!Array.isArray(values) || !values.length) {
    return "";
  }
  return values.join(", ");
}

function normalizeOptions(raw) {
  if (!Array.isArray(raw)) {
    return [];
  }
  const options = [];
  const seen = new Set();
  raw.forEach((item) => {
    if (!isObject(item)) {
      return;
    }
    const value = String(item.value || "").trim();
    const label = String(item.label || "").trim();
    if (!value || !label || seen.has(value)) {
      return;
    }
    seen.add(value);
    options.push({ value, label });
  });
  return options;
}

function normalizeCompareStoreOptions(raw) {
  const defaults = [{ value: "", label: "Auto peer set" }];
  const options = normalizeOptions(raw);
  if (!options.some((item) => item.value === "")) {
    options.unshift(defaults[0]);
  }
  return options.length ? options : defaults;
}

function normalizeWorkspacePayload(rawInput) {
  if (!rawInput) {
    return null;
  }
  let raw = rawInput;
  if (isObject(raw.structuredContent)) {
    raw = raw.structuredContent;
  }
  if (isObject(raw.payload)) {
    raw = raw.payload;
  }
  if (!isObject(raw) || !isObject(raw.store) || !isObject(raw.filters)) {
    return null;
  }
  const filters = raw.filters;
  return {
    store: clone(raw.store),
    strategy_context: isObject(raw.strategy_context) ? clone(raw.strategy_context) : null,
    inventory_check: isObject(raw.inventory_check) ? clone(raw.inventory_check) : null,
    inventory_products: isObject(raw.inventory_products) ? clone(raw.inventory_products) : null,
    filters: {
      question: typeof filters.question === "string" ? filters.question : null,
      objective: typeof filters.objective === "string" ? filters.objective : "margin",
      category: typeof filters.category === "string" ? filters.category : null,
      brand: typeof filters.brand === "string" ? filters.brand : null,
      price_band: typeof filters.price_band === "string" ? filters.price_band : null,
      occasion: typeof filters.occasion === "string" ? filters.occasion : null,
      occasions: Array.isArray(filters.occasions) ? clone(filters.occasions) : [],
      lookback_days: parsePositiveInt(filters.lookback_days, 90, 7, 730),
      inventory_scope: typeof filters.inventory_scope === "string" ? filters.inventory_scope : "combined",
      future_window_days: parsePositiveInt(filters.future_window_days, 120, 1, 365),
      compare_mode: typeof filters.compare_mode === "string" ? filters.compare_mode : "peer_and_prior_period",
      peer_mode: typeof filters.peer_mode === "string" ? filters.peer_mode : "state_and_profile",
      compare_store_id: typeof filters.compare_store_id === "string" ? filters.compare_store_id : null,
      compare_store_ids: Array.isArray(filters.compare_store_ids) ? clone(filters.compare_store_ids) : [],
      top_k: parsePositiveInt(filters.top_k, 9, 1, 50),
    },
    initial_result: isObject(raw.initial_result) ? clone(raw.initial_result) : null,
    last_result: isObject(raw.last_result) ? clone(raw.last_result) : null,
    last_tool: typeof raw.last_tool === "string" ? raw.last_tool : "fashion_merch_inventory_view",
    initial_notice: typeof raw.initial_notice === "string" ? raw.initial_notice : null,
    uiHints: {
      questionPlaceholder:
        isObject(raw.uiHints) && typeof raw.uiHints.questionPlaceholder === "string"
          ? raw.uiHints.questionPlaceholder
          : DEFAULT_PAYLOAD.uiHints.questionPlaceholder,
      emptyState:
        isObject(raw.uiHints) && typeof raw.uiHints.emptyState === "string"
          ? raw.uiHints.emptyState
          : DEFAULT_PAYLOAD.uiHints.emptyState,
      categoryOptions: normalizeOptions(isObject(raw.uiHints) ? raw.uiHints.categoryOptions : null),
      brandOptions: normalizeOptions(isObject(raw.uiHints) ? raw.uiHints.brandOptions : null),
      occasionOptions: normalizeOptions(isObject(raw.uiHints) ? raw.uiHints.occasionOptions : null),
      compareStoreOptions: normalizeCompareStoreOptions(isObject(raw.uiHints) ? raw.uiHints.compareStoreOptions : null),
      features: {
        merchStrategyContextEnabled: Boolean(raw?.uiHints?.features?.merchStrategyContextEnabled),
      },
    },
  };
}

function parseJsonContentPayload(raw) {
  if (!raw || !Array.isArray(raw.content)) {
    return null;
  }
  const textPart = raw.content.find((entry) => entry && typeof entry.text === "string");
  if (!textPart || !textPart.text) {
    return null;
  }
  try {
    return JSON.parse(textPart.text);
  } catch {
    return null;
  }
}

function parseToolPayload(raw) {
  if (isObject(raw?.structuredContent)) {
    return parseToolPayload(raw.structuredContent);
  }
  if (isObject(raw?.payload)) {
    return raw.payload;
  }
  if (isObject(raw)) {
    return raw;
  }
  const parsed = parseJsonContentPayload(raw);
  return isObject(parsed) ? parsed : null;
}

function applyWorkspacePayload(raw) {
  const normalized = normalizeWorkspacePayload(raw);
  if (!normalized) {
    return false;
  }
  state.payload = normalized;
  const filters = normalized.filters;
  state.ui.question = filters.question || "";
  state.ui.objective = filters.objective || "margin";
  state.ui.category = filters.category || "";
  state.ui.selectedBrands = parseBrandCsv(filters.brand || "");
  state.ui.priceBand = filters.price_band || "";
  state.ui.selectedOccasions = normalizeOccasionSelections(
    parseOccasionValues(filters.occasion, filters.occasions),
    normalized.uiHints.occasionOptions,
  );
  state.ui.lookbackDays = String(parsePositiveInt(filters.lookback_days, 90, 7, 730));
  state.ui.inventoryScope = filters.inventory_scope || "combined";
  state.ui.futureWindowDays = String(parsePositiveInt(filters.future_window_days, 120, 1, 365));
  state.ui.compareMode = filters.compare_mode || "peer_and_prior_period";
  state.ui.peerMode = filters.peer_mode || "state_and_profile";
  const compareStoreIds = Array.isArray(filters.compare_store_ids) ? filters.compare_store_ids : [];
  state.ui.compareStoreId = compareStoreIds[0] || filters.compare_store_id || "";
  state.ui.topK = String(parsePositiveInt(filters.top_k, 9, 1, 50));
  state.ui.strategyStoreId = normalized.store && normalized.store.id ? normalized.store.id : "";
  state.ui.activeView = TOOL_TO_VIEW[normalized.last_tool] || "inventory";
  state.ui.filtersDirty = false;

  const initialResult = normalized.last_result || normalized.initial_result;
  const initialView = TOOL_TO_VIEW[normalized.last_tool] || "inventory";
  if (initialResult && ["inventory", "recommendations", "mix_analysis"].includes(initialView)) {
    state.data[initialView] = clone(initialResult);
  }

  if (normalized.initial_notice) {
    setNotice(normalized.initial_notice, "info");
  }
  return true;
}

function setNotice(message, tone = "info") {
  state.ui.notice = String(message || "");
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

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.entries(props).forEach(([key, value]) => {
    if (value === null || value === undefined) {
      return;
    }
    if (key === "className") {
      node.className = String(value);
      return;
    }
    if (key === "text") {
      node.textContent = String(value);
      return;
    }
    if (key === "html") {
      node.innerHTML = String(value);
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

function clear(node) {
  node.innerHTML = "";
  return node;
}

function currentStoreId() {
  return state.payload?.store?.id || "";
}

function currentViewResult() {
  return state.data[state.ui.activeView] || null;
}

function recommendationOverridesPayload() {
  return Object.values(state.recommendationOverrides)
    .filter((entry) => isObject(entry) && entry.product_id)
    .map((entry) => ({
      product_id: entry.product_id,
      final_action: entry.final_action,
      priority_tier: entry.priority_tier,
      override_note: entry.override_note || null,
    }));
}

function buildInventoryArgs() {
  const args = {
    store_id: currentStoreId(),
    lookback_days: parsePositiveInt(state.ui.lookbackDays, 90, 7, 730),
    category: state.ui.category.trim() || undefined,
    brand: serializeBrands(state.ui.selectedBrands) || undefined,
    price_band: state.ui.priceBand || undefined,
    occasion: state.ui.selectedOccasions[0] || undefined,
    occasions: state.ui.selectedOccasions.length ? state.ui.selectedOccasions : undefined,
    inventory_scope: state.ui.inventoryScope || "combined",
    future_window_days: parsePositiveInt(state.ui.futureWindowDays, 120, 1, 365),
    limit: 300,
  };
  return Object.fromEntries(Object.entries(args).filter(([, value]) => value !== undefined && value !== ""));
}

function buildRecommendationArgs() {
  const args = {
    store_id: currentStoreId(),
    question: state.ui.question.trim() || undefined,
    objective: state.ui.objective || "margin",
    lookback_days: parsePositiveInt(state.ui.lookbackDays, 90, 7, 730),
    top_k: parsePositiveInt(state.ui.topK, 9, 1, 50),
    category: state.ui.category.trim() || undefined,
    brand: serializeBrands(state.ui.selectedBrands) || undefined,
    price_band: state.ui.priceBand || undefined,
    occasion: state.ui.selectedOccasions[0] || undefined,
    occasions: state.ui.selectedOccasions.length ? state.ui.selectedOccasions : undefined,
    compare_mode: state.ui.compareMode || "peer_and_prior_period",
    peer_mode: state.ui.peerMode || "state_and_profile",
    compare_store_id: state.ui.compareStoreId || undefined,
  };
  return Object.fromEntries(Object.entries(args).filter(([, value]) => value !== undefined && value !== ""));
}

function buildMixArgs() {
  const args = {
    store_id: currentStoreId(),
    lookback_days: parsePositiveInt(state.ui.lookbackDays, 90, 7, 730),
    top_k: parsePositiveInt(state.ui.topK, 9, 1, 100),
    category: state.ui.category.trim() || undefined,
    brand: serializeBrands(state.ui.selectedBrands) || undefined,
    price_band: state.ui.priceBand || undefined,
    occasion: state.ui.selectedOccasions[0] || undefined,
    occasions: state.ui.selectedOccasions.length ? state.ui.selectedOccasions : undefined,
    inventory_scope: state.ui.inventoryScope || "combined",
    future_window_days: parsePositiveInt(state.ui.futureWindowDays, 120, 1, 365),
    recommendation_overrides: recommendationOverridesPayload(),
  };
  return Object.fromEntries(Object.entries(args).filter(([, value]) => value !== undefined && value !== ""));
}

function buildInsightArgs() {
  const args = {
    store_id: currentStoreId(),
    question: state.ui.question.trim() || undefined,
    lookback_days: parsePositiveInt(state.ui.lookbackDays, 90, 7, 730),
    category: state.ui.category.trim() || undefined,
    brand: serializeBrands(state.ui.selectedBrands) || undefined,
    price_band: state.ui.priceBand || undefined,
    occasion: state.ui.selectedOccasions[0] || undefined,
    occasions: state.ui.selectedOccasions.length ? state.ui.selectedOccasions : undefined,
    compare_mode: state.ui.compareMode || "peer_and_prior_period",
    peer_mode: state.ui.peerMode || "state_and_profile",
    compare_store_id: state.ui.compareStoreId || undefined,
  };
  return Object.fromEntries(Object.entries(args).filter(([, value]) => value !== undefined && value !== ""));
}

function buildExportArgs() {
  const activeView = state.ui.activeView;
  const base = {
    view: activeView === "recommendations" ? "actions" : activeView,
    export_mode: "view_only",
    store_id: currentStoreId(),
    question: state.ui.question.trim() || undefined,
    objective: state.ui.objective || "margin",
    lookback_days: parsePositiveInt(state.ui.lookbackDays, 90, 7, 730),
    top_k: parsePositiveInt(state.ui.topK, 9, 1, 100),
    category: state.ui.category.trim() || undefined,
    brand: serializeBrands(state.ui.selectedBrands) || undefined,
    price_band: state.ui.priceBand || undefined,
    occasion: state.ui.selectedOccasions[0] || undefined,
    occasions: state.ui.selectedOccasions.length ? state.ui.selectedOccasions : undefined,
    inventory_scope: state.ui.inventoryScope || "combined",
    future_window_days: parsePositiveInt(state.ui.futureWindowDays, 120, 1, 365),
    recommendation_overrides: recommendationOverridesPayload(),
    compare_mode: state.ui.compareMode || "peer_and_prior_period",
    peer_mode: state.ui.peerMode || "state_and_profile",
    compare_store_id: state.ui.compareStoreId || undefined,
  };
  return Object.fromEntries(Object.entries(base).filter(([, value]) => value !== undefined && value !== ""));
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
      // Ignore and fallback.
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

function normalizeRowCount(result) {
  if (!isObject(result)) {
    return 0;
  }
  if (Array.isArray(result.rows)) {
    return result.rows.length;
  }
  if (Array.isArray(result.recommendations)) {
    return result.recommendations.length;
  }
  return 0;
}

function derivePriorityTier(index, total) {
  if (total <= 0) {
    return "medium";
  }
  const percentile = (index + 1) / total;
  if (percentile <= 0.34) {
    return "high";
  }
  if (percentile <= 0.67) {
    return "medium";
  }
  return "low";
}

function applyDefaultRecommendationOverrides(rows) {
  const items = Array.isArray(rows) ? rows : [];
  const total = items.length;
  const next = {};
  items.forEach((item, index) => {
    if (!item || !item.product_id) {
      return;
    }
    const existing = state.recommendationOverrides[item.product_id];
    if (existing) {
      next[item.product_id] = existing;
      return;
    }
    next[item.product_id] = {
      product_id: item.product_id,
      final_action: item.action || "feature",
      priority_tier: derivePriorityTier(index, total),
      override_note: "",
    };
  });
  state.recommendationOverrides = next;
}

function markFiltersDirty() {
  state.ui.filtersDirty = true;
}

async function loadPrimaryView(view, options = {}) {
  if (!currentStoreId()) {
    setNotice("Resolve a store before running merchandising tools.", "error");
    render();
    return;
  }
  const toolName = TOOL_BY_VIEW[view];
  if (!toolName) {
    return;
  }

  state.ui.isLoading = true;
  state.ui.activeView = view;
  if (!options.silentNotice) {
    setNotice(`Loading ${humanizeToken(view)}...`, "info");
  }
  render();

  let args;
  if (view === "inventory") {
    args = buildInventoryArgs();
  } else if (view === "recommendations") {
    args = buildRecommendationArgs();
  } else {
    args = buildMixArgs();
  }
  const result = await callTool(toolName, args);
  state.ui.isLoading = false;
  if (result.__toolError) {
    setNotice(result.__toolError, "error");
    render();
    return;
  }

  const payload = parseToolPayload(result);
  if (!payload) {
    setNotice(`Unexpected response from ${toolName}.`, "error");
    render();
    return;
  }

  state.data[view] = clone(payload);
  state.payload.last_tool = toolName;
  state.payload.last_result = clone(payload);
  state.ui.filtersDirty = false;
  if (view === "recommendations") {
    applyDefaultRecommendationOverrides(payload.recommendations);
  }
  setNotice(`${humanizeToken(view)} loaded (${normalizeRowCount(payload)} rows).`, "info");
  render();
}

async function loadInsight(toolName) {
  if (!currentStoreId()) {
    setNotice("Resolve a store before running advanced insights.", "error");
    render();
    return;
  }
  state.ui.isLoading = true;
  setNotice(`Loading ${toolName === "fashion_merch_diagnostics" ? "Diagnostics" : "Trends"}...`, "info");
  render();

  const result = await callTool(toolName, buildInsightArgs());
  state.ui.isLoading = false;
  if (result.__toolError) {
    setNotice(result.__toolError, "error");
    render();
    return;
  }
  const payload = parseToolPayload(result);
  if (!payload) {
    setNotice("Advanced insights returned an unexpected payload.", "error");
    render();
    return;
  }
  if (toolName === "fashion_merch_diagnostics") {
    state.data.diagnostics = clone(payload);
    setNotice("Diagnostics loaded.", "info");
  } else {
    state.data.trends = clone(payload);
    setNotice("Trends loaded.", "info");
  }
  render();
}

async function exportCsv() {
  if (!currentStoreId()) {
    setNotice("Resolve a store before exporting CSV.", "error");
    render();
    return;
  }
  state.ui.isExporting = true;
  setNotice("Building CSV export...", "info");
  render();

  const result = await callTool("fashion_merch_export_csv", buildExportArgs());
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
  const copied = await copyTextToClipboard(payload.csv_text);
  if (copied) {
    setNotice(`Copied CSV (${payload.row_count || 0} rows) to clipboard.`, "info");
  } else {
    setNotice("CSV generated. Copy from the text area below.", "info");
  }
  render();
}

function currentStrategyCorePayload(context) {
  const effectiveCore = isObject(context?.effective_strategy_core) ? context.effective_strategy_core : null;
  const packetCore = isObject(context?.strategy_core) ? context.strategy_core : null;
  const source = effectiveCore || packetCore || {};
  return {
    objective: state.ui.objective || source.objective || "margin",
    lookback_days: parsePositiveInt(state.ui.lookbackDays, 90, 7, 730),
    category: state.ui.category.trim() || source.category || null,
    brands: state.ui.selectedBrands,
    discount_pct: Number.isFinite(Number(source.discount_pct)) ? Number(source.discount_pct) : 0,
    floor_space_shift_pct: Number.isFinite(Number(source.floor_space_shift_pct)) ? Number(source.floor_space_shift_pct) : 0,
    min_margin_rate: Number.isFinite(Number(source.min_margin_rate)) ? Number(source.min_margin_rate) : 0.4,
    max_discount_pct: Number.isFinite(Number(source.max_discount_pct)) ? Number(source.max_discount_pct) : 20,
  };
}

async function reloadWorkspaceFromStrategy(noticeText, options = {}) {
  const context = isObject(state.payload.strategy_context) ? state.payload.strategy_context : null;
  if (!context?.packet_id) {
    return false;
  }
  const args = {
    store_id: options.store_id || state.ui.strategyStoreId || currentStoreId(),
    question: state.ui.question.trim() || undefined,
    objective: state.ui.objective || "margin",
    lookback_days: parsePositiveInt(state.ui.lookbackDays, 90, 7, 730),
    top_k: parsePositiveInt(state.ui.topK, 9, 1, 50),
    category: state.ui.category.trim() || undefined,
    brand: serializeBrands(state.ui.selectedBrands) || undefined,
    price_band: state.ui.priceBand || undefined,
    occasion: state.ui.selectedOccasions[0] || undefined,
    occasions: state.ui.selectedOccasions.length ? state.ui.selectedOccasions : undefined,
    inventory_scope: state.ui.inventoryScope || "combined",
    future_window_days: parsePositiveInt(state.ui.futureWindowDays, 120, 1, 365),
    compare_mode: state.ui.compareMode || "peer_and_prior_period",
    peer_mode: state.ui.peerMode || "state_and_profile",
    compare_store_id: state.ui.compareStoreId || undefined,
    strategy_packet_id: context.packet_id,
    initial_notice: noticeText || undefined,
  };
  const result = await callTool("fashion_open_merch_workspace", args);
  if (result.__toolError) {
    setNotice(result.__toolError, "error");
    render();
    return false;
  }
  const payload = parseToolPayload(result);
  const applied = applyWorkspacePayload(payload);
  if (!applied) {
    setNotice("Strategy reload returned an unexpected payload.", "error");
    render();
    return false;
  }
  const activeView = options.activeView || state.ui.activeView || "inventory";
  state.ui.activeView = ["inventory", "recommendations", "mix_analysis"].includes(activeView) ? activeView : "inventory";
  render();
  await loadPrimaryView(state.ui.activeView, { silentNotice: true });
  if (noticeText) {
    setNotice(noticeText, "info");
    render();
  }
  return true;
}

async function saveStrategyOverride(usePacketDefaults) {
  const context = isObject(state.payload.strategy_context) ? state.payload.strategy_context : null;
  if (!context?.packet_id || !currentStoreId()) {
    setNotice("No active strategy packet is loaded for this store.", "error");
    render();
    return;
  }

  if (usePacketDefaults) {
    state.ui.isResettingStrategyOverride = true;
    setNotice("Applying packet defaults...", "info");
  } else {
    state.ui.isSavingStrategyOverride = true;
    setNotice("Saving strategy override...", "info");
  }
  render();

  const args = {
    packet_id: context.packet_id,
    store_id: currentStoreId(),
    use_packet_defaults: usePacketDefaults,
  };
  if (!usePacketDefaults) {
    args.strategy_core = currentStrategyCorePayload(context);
    args.tag_intensity = state.ui.strategyTagIntensity || "medium";
  }

  const result = await callTool("fashion_merch_save_strategy_override", args);
  state.ui.isSavingStrategyOverride = false;
  state.ui.isResettingStrategyOverride = false;
  if (result.__toolError) {
    setNotice(result.__toolError, "error");
    render();
    return;
  }

  await reloadWorkspaceFromStrategy(
    usePacketDefaults ? "Reverted to packet defaults for this store." : "Saved store-level strategy override.",
    { activeView: state.ui.activeView },
  );
}

function renderNotice() {
  if (!state.ui.notice) {
    return null;
  }
  return el("div", { className: `fw-notice ${state.ui.noticeTone === "error" ? "error" : ""}`, text: state.ui.notice });
}

function buildSelectControl(currentValue, options, onChange, extra = {}) {
  const select = el(
    "select",
    {
      className: "fw-input fw-select",
      disabled: extra.disabled ? "true" : null,
      onChange: (event) => {
        onChange(event.target.value);
      },
    },
    ...options.map((option) => {
      const node = el("option", { value: option.value, text: option.label });
      if (String(option.value) === String(currentValue || "")) {
        node.selected = true;
      }
      return node;
    }),
  );
  return select;
}

function normalizeSelectionValues(values) {
  if (!Array.isArray(values)) {
    return [];
  }
  const tokens = [];
  const seen = new Set();
  values.forEach((item) => {
    const token = String(item || "").trim();
    if (!token) {
      return;
    }
    const key = token.toLowerCase();
    if (seen.has(key)) {
      return;
    }
    seen.add(key);
    tokens.push(token);
  });
  return tokens;
}

function multiSelectSummary(selectedValues, optionsById, labels) {
  if (!Array.isArray(selectedValues) || !selectedValues.length) {
    return labels.all;
  }
  if (selectedValues.length === 1) {
    const label = optionsById.get(selectedValues[0]);
    return label || `1 ${labels.singular} selected`;
  }
  return `${selectedValues.length} ${labels.plural} selected`;
}

function buildCheckMultiSelect({ selectedValues, options, labels, noOptionsText, onApply }) {
  const normalizedOptions = normalizeOptions(options);
  const canonicalByLower = new Map(normalizedOptions.map((item) => [item.value.toLowerCase(), item.value]));
  const selected = new Set();
  normalizeSelectionValues(selectedValues).forEach((value) => {
    const canonical = canonicalByLower.get(value.toLowerCase()) || value;
    selected.add(canonical);
  });

  const details = el("details", { className: "fw-multi-select" });
  const optionsById = new Map(normalizedOptions.map((item) => [item.value, item.label]));
  const summary = el("summary", {
    className: "fw-input fw-multi-select-summary",
    text: multiSelectSummary(Array.from(selected), optionsById, labels),
  });
  const list = el("div", { className: "fw-multi-select-list" });

  if (!normalizedOptions.length) {
    list.appendChild(el("p", { className: "fw-empty", text: noOptionsText }));
  } else {
    normalizedOptions.forEach((option) => {
      const checkbox = el("input", {
        type: "checkbox",
        checked: selected.has(option.value) ? "true" : null,
      });
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
          selected.clear();
          list.querySelectorAll("input[type='checkbox']").forEach((node) => {
            node.checked = false;
          });
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
          const nextValues = normalizeSelectionValues(Array.from(selected));
          summary.textContent = multiSelectSummary(nextValues, optionsById, labels);
          details.open = false;
          onApply(nextValues);
        },
      },
      "Apply",
    ),
  );

  details.appendChild(summary);
  details.appendChild(el("div", { className: "fw-multi-select-panel" }, list, actions));
  return details;
}

function buildBrandsMultiSelect(selectedBrandValuesRaw, options) {
  return buildCheckMultiSelect({
    selectedValues: normalizeSelectionValues(selectedBrandValuesRaw),
    options,
    labels: { all: "All brands", singular: "brand", plural: "brands" },
    noOptionsText: "No brands available.",
    onApply: (nextValues) => {
      state.ui.selectedBrands = normalizeSelectionValues(nextValues);
      markFiltersDirty();
      render();
    },
  });
}

function buildOccasionsMultiSelect(selectedOccasionsRaw, options) {
  return buildCheckMultiSelect({
    selectedValues: normalizeOccasionSelections(selectedOccasionsRaw, options),
    options,
    labels: { all: "All occasions", singular: "occasion", plural: "occasions" },
    noOptionsText: "No occasions available.",
    onApply: (nextValues) => {
      state.ui.selectedOccasions = normalizeOccasionSelections(nextValues, options);
      markFiltersDirty();
      render();
    },
  });
}

function renderStrategyPanel() {
  const context = isObject(state.payload.strategy_context) ? state.payload.strategy_context : null;
  const strategyEnabled = Boolean(state.payload?.uiHints?.features?.merchStrategyContextEnabled);
  if (!strategyEnabled || !context) {
    return null;
  }

  const scopeOptions = Array.isArray(context.scope_store_options)
    ? context.scope_store_options.filter((item) => isObject(item) && item.value)
    : [];
  const storeSelect = scopeOptions.length
    ? buildSelectControl(
        state.ui.strategyStoreId || currentStoreId(),
        scopeOptions,
        (value) => {
          state.ui.strategyStoreId = value;
          void reloadWorkspaceFromStrategy("Pinned strategy store updated.", {
            store_id: value,
            activeView: state.ui.activeView,
          });
        },
        { disabled: state.ui.isLoading || state.ui.isSavingStrategyOverride || state.ui.isResettingStrategyOverride },
      )
    : null;

  const effectiveCore = isObject(context.effective_strategy_core) ? context.effective_strategy_core : null;
  const effectiveLine = effectiveCore
    ? `Objective ${humanizeToken(effectiveCore.objective)} · Lookback ${effectiveCore.lookback_days || "-"}d · Category ${humanizeToken(effectiveCore.category)}`
    : "Using packet defaults.";

  const tagSelect = buildSelectControl(
    state.ui.strategyTagIntensity || "medium",
    [
      { value: "low", label: "Low" },
      { value: "medium", label: "Medium" },
      { value: "high", label: "High" },
    ],
    (value) => {
      state.ui.strategyTagIntensity = value;
      render();
    },
    { disabled: state.ui.isSavingStrategyOverride || state.ui.isResettingStrategyOverride },
  );

  return el(
    "section",
    { className: "fw-panel" },
    el("div", { className: "fw-kicker", text: "Strategy Packet" }),
    el("h3", { className: "fw-panel-title", text: context.title || "Active strategy packet" }),
    el("p", { className: "fw-empty", text: context.summary || "" }),
    el("p", { className: "fw-empty", text: effectiveLine }),
    storeSelect
      ? el(
          "div",
          { className: "fw-field" },
          el("label", { className: "fw-label", text: "Strategy Store" }),
          storeSelect,
        )
      : null,
    el(
      "div",
      { className: "fw-grid", style: "grid-template-columns: minmax(0,1fr) auto auto; align-items:end;" },
      el("div", { className: "fw-field" }, el("label", { className: "fw-label", text: "Tag Intensity" }), tagSelect),
      el(
        "button",
        {
          className: "fw-button secondary",
          type: "button",
          disabled: state.ui.isSavingStrategyOverride || state.ui.isResettingStrategyOverride ? "true" : null,
          onClick: () => {
            void saveStrategyOverride(false);
          },
        },
        state.ui.isSavingStrategyOverride ? "Saving..." : "Save Override",
      ),
      el(
        "button",
        {
          className: "fw-button secondary",
          type: "button",
          disabled: state.ui.isSavingStrategyOverride || state.ui.isResettingStrategyOverride ? "true" : null,
          onClick: () => {
            void saveStrategyOverride(true);
          },
        },
        state.ui.isResettingStrategyOverride ? "Resetting..." : "Reset Defaults",
      ),
    ),
  );
}

function renderInventoryRiskPanel() {
  const payload = isObject(state.payload.inventory_check) ? state.payload.inventory_check : null;
  if (!payload) {
    return null;
  }
  const current = isObject(payload.current_store) ? payload.current_store : null;
  const totals = isObject(payload.totals) ? payload.totals : {};
  return el(
    "section",
    { className: "fw-panel" },
    el("h3", { className: "fw-panel-title", text: "Inventory Check by Store" }),
    payload.summary ? el("p", { className: "fw-empty", text: payload.summary }) : null,
    current
      ? el(
          "div",
          { className: "fw-kpi-strip" },
          kpi("Current Store Risk", `${formatNumber(current.not_in_stock_rate_pct, 2)}%`),
          kpi("Not In Stock", `${formatNumber(current.not_in_stock_skus, 0)} / ${formatNumber(current.sku_count, 0)}`),
          kpi("Preorder", formatNumber(current.preorder_skus, 0)),
          kpi("Out of Stock", formatNumber(current.out_of_stock_skus, 0)),
        )
      : null,
    el("p", {
      className: "fw-empty",
      text: `Network not-in-stock rate ${formatNumber(totals.not_in_stock_rate_pct, 2)}% across ${formatNumber(totals.sku_count, 0)} SKUs.`,
    }),
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

function renderControlsPanel() {
  const store = state.payload.store;
  const categoryOptions = [{ value: "", label: "Any" }, ...state.payload.uiHints.categoryOptions];
  const compareStoreOptions = state.payload.uiHints.compareStoreOptions;

  const brandSelect = buildBrandsMultiSelect(state.ui.selectedBrands, state.payload.uiHints.brandOptions);
  const occasionSelect = buildOccasionsMultiSelect(state.ui.selectedOccasions, state.payload.uiHints.occasionOptions);

  const controls = el(
    "section",
    { className: "fw-panel fw-controls-panel" },
    el("h2", { className: "fw-panel-title", text: store ? `Store: ${store.name}` : "Merch Workspace" }),
    renderNotice(),
    store
      ? el("p", { className: "fw-empty", text: `${store.city}, ${store.state} • profile ${humanizeToken(store.profile_type)}` })
      : el("p", { className: "fw-empty", text: "No store resolved yet." }),
    renderStrategyPanel(),
    renderInventoryRiskPanel(),
    el(
      "div",
      { className: "fw-grid merch-filters fw-merch-clean-filters" },
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Category" }),
        buildSelectControl(state.ui.category, categoryOptions, (value) => {
          state.ui.category = value;
          markFiltersDirty();
        }),
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Brand (Multi)" }),
        brandSelect,
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Occasion (Multi)" }),
        occasionSelect,
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Price Band" }),
        buildSelectControl(
          state.ui.priceBand,
          [
            { value: "", label: "Any" },
            { value: "under_250", label: "Under $250" },
            { value: "250_500", label: "$250-$500" },
            { value: "500_1000", label: "$500-$1000" },
            { value: "1000_plus", label: "$1000+" },
          ],
          (value) => {
            state.ui.priceBand = value;
            markFiltersDirty();
          },
        ),
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Lookback (Days)" }),
        el("input", {
          className: "fw-input",
          type: "number",
          min: "7",
          max: "730",
          value: state.ui.lookbackDays,
          onInput: (event) => {
            state.ui.lookbackDays = event.target.value;
            markFiltersDirty();
          },
        }),
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Inventory Scope" }),
        buildSelectControl(
          state.ui.inventoryScope,
          [
            { value: "current", label: "Current" },
            { value: "potential", label: "Potential" },
            { value: "combined", label: "Combined" },
          ],
          (value) => {
            state.ui.inventoryScope = value;
            markFiltersDirty();
          },
        ),
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Future Window Days" }),
        el("input", {
          className: "fw-input",
          type: "number",
          min: "1",
          max: "365",
          value: state.ui.futureWindowDays,
          onInput: (event) => {
            state.ui.futureWindowDays = event.target.value;
            markFiltersDirty();
          },
        }),
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Objective" }),
        buildSelectControl(
          state.ui.objective,
          [
            { value: "margin", label: "Margin" },
            { value: "sell_through", label: "Sell Through" },
            { value: "revenue", label: "Revenue" },
          ],
          (value) => {
            state.ui.objective = value;
            markFiltersDirty();
          },
        ),
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Top K" }),
        el("input", {
          className: "fw-input",
          type: "number",
          min: "1",
          max: "100",
          value: state.ui.topK,
          onInput: (event) => {
            state.ui.topK = event.target.value;
            markFiltersDirty();
          },
        }),
      ),
      el(
        "div",
        { className: "fw-field fw-span-full" },
        el("label", { className: "fw-label", text: "Context (Optional)" }),
        el("input", {
          className: "fw-input",
          type: "text",
          value: state.ui.question,
          placeholder: state.payload.uiHints.questionPlaceholder,
          onInput: (event) => {
            state.ui.question = event.target.value;
            markFiltersDirty();
          },
        }),
      ),
    ),
    el(
      "details",
      {
        className: "fw-advanced-controls",
        open: state.ui.advancedInsightsOpen ? "true" : null,
        onToggle: (event) => {
          state.ui.advancedInsightsOpen = Boolean(event.currentTarget && event.currentTarget.open);
        },
      },
      el("summary", { className: "fw-advanced-summary", text: "Advanced Insights" }),
      el(
        "div",
        { className: "fw-grid merch-filters fw-merch-clean-filters" },
        el(
          "div",
          { className: "fw-field" },
          el("label", { className: "fw-label", text: "Compare Mode" }),
          buildSelectControl(
            state.ui.compareMode,
            [
              { value: "peer_and_prior_period", label: "Peer + Prior Period" },
              { value: "peer", label: "Peer" },
              { value: "prior_period", label: "Prior Period" },
            ],
            (value) => {
              state.ui.compareMode = value;
              markFiltersDirty();
            },
          ),
        ),
        el(
          "div",
          { className: "fw-field" },
          el("label", { className: "fw-label", text: "Peer Mode" }),
          buildSelectControl(
            state.ui.peerMode,
            [
              { value: "state_and_profile", label: "State + Profile" },
              { value: "profile_type", label: "Profile Type" },
              { value: "all_profile_matches", label: "All Profile Matches" },
            ],
            (value) => {
              state.ui.peerMode = value;
              markFiltersDirty();
            },
          ),
        ),
        el(
          "div",
          { className: "fw-field" },
          el("label", { className: "fw-label", text: "Compare Store" }),
          buildSelectControl(state.ui.compareStoreId, compareStoreOptions, (value) => {
            state.ui.compareStoreId = value;
            markFiltersDirty();
          }),
        ),
      ),
      el(
        "div",
        { className: "fw-toolbar" },
        el(
          "button",
          {
            className: "fw-button secondary",
            type: "button",
            disabled: state.ui.isLoading ? "true" : null,
            onClick: () => {
              void loadInsight("fashion_merch_diagnostics");
            },
          },
          "Run Diagnostics",
        ),
        el(
          "button",
          {
            className: "fw-button secondary",
            type: "button",
            disabled: state.ui.isLoading ? "true" : null,
            onClick: () => {
              void loadInsight("fashion_merch_trend_summary");
            },
          },
          "Run Trends",
        ),
      ),
      renderAdvancedInsightsBody(),
    ),
  );

  return controls;
}

function renderAdvancedInsightsBody() {
  const diagnostics = state.data.diagnostics;
  const trends = state.data.trends;
  if (!diagnostics && !trends) {
    return el("p", { className: "fw-empty", text: "Run Diagnostics or Trends when you need deeper context." });
  }
  const children = [];
  if (diagnostics) {
    children.push(
      el(
        "section",
        { className: "fw-panel" },
        el("h3", { className: "fw-panel-title", text: "Diagnostics" }),
        diagnostics.summary ? el("p", { className: "fw-empty", text: diagnostics.summary }) : null,
        ...(Array.isArray(diagnostics.insights) ? diagnostics.insights.slice(0, 6) : []).map((item) =>
          el(
            "p",
            { className: "fw-empty" },
            `${humanizeToken(item.dimension)} · ${item.subject}: ${formatCurrency(item.current_value)} (${formatNumber(item.delta, 2)})`,
          ),
        ),
      ),
    );
  }
  if (trends) {
    children.push(
      el(
        "section",
        { className: "fw-panel" },
        el("h3", { className: "fw-panel-title", text: "Trends" }),
        trends.summary ? el("p", { className: "fw-empty", text: trends.summary }) : null,
        ...(Array.isArray(trends.highlights) ? trends.highlights.slice(0, 6) : []).map((item) =>
          el(
            "p",
            { className: "fw-empty" },
            `${item.subject}: ${formatCurrency(item.current_value)} (${formatNumber(item.pct_change, 1)}%)`,
          ),
        ),
      ),
    );
  }
  return el("div", { className: "fw-grid" }, ...children);
}

function renderPrimaryTabs() {
  const tabs = [
    { id: "inventory", label: "Inventory" },
    { id: "recommendations", label: "Recommendations" },
    { id: "mix_analysis", label: "Mix Analysis" },
  ];
  return el(
    "div",
    { className: "fw-merch-nav" },
    el(
      "div",
      { className: "fw-tabs fw-merch-tabs", role: "tablist", "aria-label": "Merch primary views" },
      ...tabs.map((tab) =>
        el(
          "button",
          {
            id: `fw-primary-tab-${tab.id}`,
            className: `fw-tab fw-merch-tab ${state.ui.activeView === tab.id ? "active" : ""}`,
            type: "button",
            role: "tab",
            "aria-selected": state.ui.activeView === tab.id ? "true" : "false",
            disabled: state.ui.isLoading ? "true" : null,
            onClick: () => {
              void loadPrimaryView(tab.id);
            },
          },
          tab.label,
        ),
      ),
    ),
    el(
      "div",
      { className: "fw-toolbar fw-toolbar-merch" },
      el(
        "button",
        {
          className: `fw-button ${state.ui.filtersDirty ? "" : "secondary"}`,
          type: "button",
          disabled: state.ui.isLoading ? "true" : null,
          onClick: () => {
            void loadPrimaryView(state.ui.activeView);
          },
        },
        state.ui.isLoading ? "Refreshing..." : state.ui.filtersDirty ? "Refresh Results" : "Refresh",
      ),
      el(
        "button",
        {
          className: "fw-button secondary",
          type: "button",
          disabled: state.ui.isLoading ? "true" : null,
          onClick: () => {
            void loadPrimaryView("mix_analysis");
          },
        },
        "Run Mix Analysis",
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
  );
}

function renderInventoryResult() {
  const result = state.data.inventory;
  if (!isObject(result) || !Array.isArray(result.rows)) {
    return el("p", { className: "fw-empty", text: state.payload.uiHints.emptyState });
  }

  const table = el(
    "table",
    { className: "fw-table" },
    el(
      "thead",
      {},
      el(
        "tr",
        {},
        el("th", { text: "Type" }),
        el("th", { text: "Title" }),
        el("th", { text: "Brand" }),
        el("th", { text: "Category" }),
        el("th", { text: "Availability" }),
        el("th", { text: "Qty" }),
        el("th", { text: "Available On" }),
        el("th", { text: "Revenue" }),
        el("th", { text: "Units" }),
        el("th", { text: "Margin" }),
      ),
    ),
    el(
      "tbody",
      {},
      ...result.rows.map((row) =>
        el(
          "tr",
          {},
          el("td", { text: humanizeToken(row.row_type) }),
          el("td", { text: row.title || "-" }),
          el("td", { text: row.brand || "-" }),
          el("td", { text: humanizeToken(row.category) }),
          el("td", { text: row.availability || row.offer_status || "-" }),
          el("td", { text: formatNumber(row.inventory_qty || 0, 0) }),
          el("td", { text: row.available_on ? formatDate(row.available_on) : "-" }),
          el("td", { text: formatCurrency(row.perf_revenue) }),
          el("td", { text: formatNumber(row.perf_units || 0, 1) }),
          el("td", { text: `${formatNumber((Number(row.perf_margin_rate || 0) * 100), 1)}%` }),
        ),
      ),
    ),
  );

  return el(
    "section",
    { className: "fw-panel" },
    el("h2", { className: "fw-panel-title", text: "Inventory" }),
    el(
      "div",
      { className: "fw-kpi-strip" },
      kpi("Rows", formatNumber(result.total_rows || result.rows.length, 0)),
      kpi("Current", formatNumber(result.current_rows || 0, 0)),
      kpi("Potential", formatNumber(result.potential_rows || 0, 0)),
      kpi("Scope", humanizeToken(result.inventory_scope || state.ui.inventoryScope)),
    ),
    result.summary ? el("p", { className: "fw-empty", text: result.summary }) : null,
    table,
  );
}

function updateRecommendationOverride(productId, patch) {
  const key = String(productId || "").trim();
  if (!key) {
    return;
  }
  const existing = state.recommendationOverrides[key] || {
    product_id: key,
    final_action: "feature",
    priority_tier: "medium",
    override_note: "",
  };
  state.recommendationOverrides[key] = { ...existing, ...patch, product_id: key };
  state.ui.filtersDirty = true;
}

function renderRecommendationsResult() {
  const result = state.data.recommendations;
  if (!isObject(result) || !Array.isArray(result.recommendations)) {
    return el("p", { className: "fw-empty", text: "Run Recommendations to evaluate and adjust actions." });
  }

  applyDefaultRecommendationOverrides(result.recommendations);
  const rows = result.recommendations;
  const table = el(
    "table",
    { className: "fw-table" },
    el(
      "thead",
      {},
      el(
        "tr",
        {},
        el("th", { text: "Product" }),
        el("th", { text: "Model Action" }),
        el("th", { text: "Metric" }),
        el("th", { text: "Final Action" }),
        el("th", { text: "Priority" }),
        el("th", { text: "Override Note" }),
      ),
    ),
    el(
      "tbody",
      {},
      ...rows.map((item, index) => {
        const override = state.recommendationOverrides[item.product_id] || {
          product_id: item.product_id,
          final_action: item.action || "feature",
          priority_tier: derivePriorityTier(index, rows.length),
          override_note: "",
        };
        const finalActionSelect = buildSelectControl(
          override.final_action,
          [
            { value: "feature", label: "feature" },
            { value: "promote", label: "promote" },
            { value: "deprioritize", label: "deprioritize" },
            { value: "drop", label: "drop" },
          ],
          (value) => {
            updateRecommendationOverride(item.product_id, { final_action: value });
          },
        );
        const prioritySelect = buildSelectControl(
          override.priority_tier,
          [
            { value: "high", label: "high" },
            { value: "medium", label: "medium" },
            { value: "low", label: "low" },
          ],
          (value) => {
            updateRecommendationOverride(item.product_id, { priority_tier: value });
          },
        );
        const noteInput = el("input", {
          className: "fw-input",
          type: "text",
          value: override.override_note || "",
          placeholder: "optional",
          onInput: (event) => {
            updateRecommendationOverride(item.product_id, { override_note: event.target.value });
          },
        });

        return el(
          "tr",
          {},
          el(
            "td",
            {},
            el("strong", { text: item.title || item.product_id }),
            el("div", { className: "fw-empty", text: `${item.brand || "-"} · ${humanizeToken(item.category)}` }),
          ),
          el("td", { text: item.action || "-" }),
          el("td", { text: formatNumber(item.metric_value, 2) }),
          el("td", {}, finalActionSelect),
          el("td", {}, prioritySelect),
          el("td", {}, noteInput),
        );
      }),
    ),
  );

  return el(
    "section",
    { className: "fw-panel" },
    el("h2", { className: "fw-panel-title", text: "Recommendations" }),
    result.summary ? el("p", { className: "fw-empty", text: result.summary || result.parsed_intent }) : null,
    el(
      "div",
      { className: "fw-toolbar" },
      el(
        "button",
        {
          className: "fw-button secondary",
          type: "button",
          onClick: () => {
            state.recommendationOverrides = {};
            applyDefaultRecommendationOverrides(rows);
            state.ui.filtersDirty = true;
            render();
          },
        },
        "Reset Row Overrides",
      ),
      el("span", {
        className: "fw-empty",
        text: `${recommendationOverridesPayload().length} row overrides in session state`,
      }),
    ),
    table,
  );
}

function renderMixResult() {
  const result = state.data.mix_analysis;
  if (!isObject(result) || !Array.isArray(result.rows)) {
    return el("p", { className: "fw-empty", text: "Run Mix Analysis to generate add/hold/reduce/swap recommendations." });
  }

  const table = el(
    "table",
    { className: "fw-table" },
    el(
      "thead",
      {},
      el(
        "tr",
        {},
        el("th", { text: "Action" }),
        el("th", { text: "Fit" }),
        el("th", { text: "Expected Impact" }),
        el("th", { text: "Current Product" }),
        el("th", { text: "Offer" }),
        el("th", { text: "Rationale" }),
      ),
    ),
    el(
      "tbody",
      {},
      ...result.rows.map((item) =>
        el(
          "tr",
          {},
          el("td", { text: humanizeToken(item.action) }),
          el("td", { text: formatNumber(item.fit_score, 2) }),
          el("td", { text: formatNumber(item.expected_mix_impact, 2) }),
          el("td", { text: item.current_title || "-" }),
          el("td", { text: item.offer_title || "-" }),
          el("td", { text: item.rationale || "-" }),
        ),
      ),
    ),
  );

  return el(
    "section",
    { className: "fw-panel" },
    el("h2", { className: "fw-panel-title", text: "Mix Analysis" }),
    result.summary ? el("p", { className: "fw-empty", text: result.summary }) : null,
    el(
      "div",
      { className: "fw-kpi-strip" },
      kpi("Rows", formatNumber(result.rows.length, 0)),
      kpi("Scope", humanizeToken(result.inventory_scope || state.ui.inventoryScope)),
      kpi("Future Window", `${formatNumber(result.future_window_days || state.ui.futureWindowDays, 0)} days`),
      kpi("Top K", formatNumber(result.top_k || state.ui.topK, 0)),
    ),
    table,
  );
}

function renderPrimaryResult() {
  if (state.ui.activeView === "recommendations") {
    return renderRecommendationsResult();
  }
  if (state.ui.activeView === "mix_analysis") {
    return renderMixResult();
  }
  return renderInventoryResult();
}

function renderCsvPanel() {
  if (!state.ui.csvText) {
    return null;
  }
  return el(
    "section",
    { className: "fw-panel" },
    el("h2", { className: "fw-panel-title", text: "CSV Export" }),
    el("p", { className: "fw-empty", text: "This CSV is generated from the active view and filters (view_only mode)." }),
    el("textarea", {
      className: "fw-textarea fw-csv-text",
      rows: "12",
      readonly: "true",
      value: state.ui.csvText,
    }),
  );
}

function render() {
  if (!root) {
    return;
  }
  const container = clear(root);
  const buildLabel =
    typeof meta.buildVersion === "string" && meta.buildVersion.trim()
      ? `build ${meta.buildVersion.trim()}`
      : null;

  const header = el(
    "header",
    { className: "fw-hero fw-workspace-header" },
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
          "Inventory-first merchandising workspace: adjust recommendations, evaluate product mix, and export exactly what is shown.",
      },
    ),
  );

  container.appendChild(
    el(
      "div",
      { className: "fw-root" },
      header,
      renderControlsPanel(),
      renderPrimaryTabs(),
      renderPrimaryResult(),
      renderCsvPanel(),
    ),
  );
}

async function boot() {
  const seeded = applyWorkspacePayload(meta.initialPayload);
  if (!seeded && !applyWorkspacePayload(window.openai && window.openai.toolOutput)) {
    state.payload = clone(DEFAULT_PAYLOAD);
    state.ui.notice = "Open this workspace from a store query to load merchandising context.";
    state.ui.noticeTone = "info";
  }
  render();
  const view = state.ui.activeView || "inventory";
  if (!state.data[view]) {
    await loadPrimaryView(view, { silentNotice: true });
  }
}

window.addEventListener(
  "openai:set_globals",
  (event) => {
    const globals = event && event.detail && event.detail.globals;
    const toolOutput = globals && globals.toolOutput;
    if (!toolOutput) {
      return;
    }
    if (state.ui.isLoading || state.ui.isExporting) {
      return;
    }
    const applied = applyWorkspacePayload(toolOutput);
    if (applied) {
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
    const payload = event.data;
    if (!isObject(payload) || payload.method !== "openai:tool_result") {
      return;
    }
    if (state.ui.isLoading || state.ui.isExporting) {
      return;
    }
    const parsed = parseToolPayload(payload.params);
    const applied = applyWorkspacePayload(parsed);
    if (applied) {
      render();
    }
  },
  { passive: true },
);

void boot();
