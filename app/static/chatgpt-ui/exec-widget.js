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
    brands: [],
    store_id: null,
    store_ids: [],
    discount_pct: 10,
    floor_space_shift_pct: 5,
    from_category: "womens_apparel",
    to_category: "shoes",
    to_email: DEFAULT_EXEC_TO_EMAIL,
    autopilot_top_k: 6,
    optimize_discount_min_pct: 0,
    optimize_discount_max_pct: 20,
    optimize_discount_step_pct: 5,
    optimize_shift_min_pct: 0,
    optimize_shift_max_pct: 20,
    optimize_shift_step_pct: 5,
    optimize_top_k_scenarios: 3,
    min_margin_rate: 0.4,
    max_discount_pct: 20,
    strategy_packet_id: null,
  },
  initial_result: null,
  last_result: null,
  last_tool: "fashion_exec_overview",
  initial_notice: null,
  uiHints: {
    emptyState: "Run one of the executive tabs to populate this workspace.",
    categoryOptions: [],
    events: ["wedding", "holiday_party", "workwear"],
    brandOptions: [],
    storeOptions: [],
    features: {
      execAutoOptimizeEnabled: false,
      strategyPacketEnabled: false,
    },
  },
};

const state = {
  payload: clone(DEFAULT_PAYLOAD),
  ui: {
    lookbackPreset: "90",
    lookbackDays: "90",
    objective: "revenue",
    storeIds: [],
    selectedEvents: ["wedding", "holiday_party", "workwear"],
    selectedBrands: [],
    discountPct: "10",
    floorSpaceShiftPct: "5",
    fromCategory: "womens_apparel",
    toCategory: "shoes",
    toEmail: DEFAULT_EXEC_TO_EMAIL,
    autopilotTopK: "6",
    optimizeDiscountMinPct: "0",
    optimizeDiscountMaxPct: "20",
    optimizeDiscountStepPct: "5",
    optimizeShiftMinPct: "0",
    optimizeShiftMaxPct: "20",
    optimizeShiftStepPct: "5",
    optimizeTopKScenarios: "3",
    minMarginRatePct: "40",
    maxDiscountPct: "20",
    strategyPacketId: "",
    notice: "",
    noticeTone: "info",
    isLoading: false,
    isSending: false,
    isPublishingStrategy: false,
    isPreparingStrategyEmail: false,
    isSendingStrategyEmail: false,
  },
  runtime: {
    toolOutputApplied: false,
    userInteracted: false,
    modelContextHash: "",
    modelContextTimer: null,
    chartCleanupFns: [],
    autopilotDraftId: null,
    strategyPacket: null,
    strategyEmailDraft: null,
    strategyEmailSend: null,
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
      events: normalizeTokenSelections(rawFilters.events, { lowerCase: true, normalizeSpaces: true }),
      brands: normalizeTokenSelections(rawFilters.brands, { lowerCase: false }),
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
      optimize_discount_min_pct:
        Number.isFinite(Number(rawFilters.optimize_discount_min_pct)) ? Number(rawFilters.optimize_discount_min_pct) : 0,
      optimize_discount_max_pct:
        Number.isFinite(Number(rawFilters.optimize_discount_max_pct)) ? Number(rawFilters.optimize_discount_max_pct) : 20,
      optimize_discount_step_pct:
        Number.isFinite(Number(rawFilters.optimize_discount_step_pct)) ? Number(rawFilters.optimize_discount_step_pct) : 5,
      optimize_shift_min_pct:
        Number.isFinite(Number(rawFilters.optimize_shift_min_pct)) ? Number(rawFilters.optimize_shift_min_pct) : 0,
      optimize_shift_max_pct:
        Number.isFinite(Number(rawFilters.optimize_shift_max_pct)) ? Number(rawFilters.optimize_shift_max_pct) : 20,
      optimize_shift_step_pct:
        Number.isFinite(Number(rawFilters.optimize_shift_step_pct)) ? Number(rawFilters.optimize_shift_step_pct) : 5,
      optimize_top_k_scenarios:
        Number(rawFilters.optimize_top_k_scenarios) > 0 ? Number(rawFilters.optimize_top_k_scenarios) : 3,
      min_margin_rate: Number.isFinite(Number(rawFilters.min_margin_rate)) ? Number(rawFilters.min_margin_rate) : 0.4,
      max_discount_pct: Number.isFinite(Number(rawFilters.max_discount_pct)) ? Number(rawFilters.max_discount_pct) : 20,
      strategy_packet_id:
        typeof rawFilters.strategy_packet_id === "string" && rawFilters.strategy_packet_id.trim()
          ? rawFilters.strategy_packet_id.trim()
          : null,
    },
    initial_result: isObject(raw.initial_result) ? clone(raw.initial_result) : null,
    last_result: isObject(raw.last_result) ? clone(raw.last_result) : null,
    last_tool: typeof raw.last_tool === "string" ? raw.last_tool : "fashion_exec_overview",
    initial_notice: typeof raw.initial_notice === "string" ? raw.initial_notice : null,
    uiHints: {
      ...(isObject(raw.uiHints) ? clone(raw.uiHints) : clone(DEFAULT_PAYLOAD.uiHints)),
      features: {
        execAutoOptimizeEnabled: Boolean(raw?.uiHints?.features?.execAutoOptimizeEnabled),
        strategyPacketEnabled: Boolean(raw?.uiHints?.features?.strategyPacketEnabled),
      },
    },
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
  state.ui.selectedEvents = normalizeTokenSelections(payload.filters.events, { lowerCase: true, normalizeSpaces: true });
  state.ui.selectedBrands = normalizeTokenSelections(payload.filters.brands, { lowerCase: false });
  state.ui.discountPct = String(payload.filters.discount_pct ?? 10);
  state.ui.floorSpaceShiftPct = String(payload.filters.floor_space_shift_pct ?? 5);
  state.ui.fromCategory = payload.filters.from_category || "";
  state.ui.toCategory = payload.filters.to_category || "";
  state.ui.toEmail = (payload.filters.to_email || DEFAULT_EXEC_TO_EMAIL).trim() || DEFAULT_EXEC_TO_EMAIL;
  state.ui.autopilotTopK = String(payload.filters.autopilot_top_k ?? 6);
  state.ui.optimizeDiscountMinPct = String(payload.filters.optimize_discount_min_pct ?? 0);
  state.ui.optimizeDiscountMaxPct = String(payload.filters.optimize_discount_max_pct ?? 20);
  state.ui.optimizeDiscountStepPct = String(payload.filters.optimize_discount_step_pct ?? 5);
  state.ui.optimizeShiftMinPct = String(payload.filters.optimize_shift_min_pct ?? 0);
  state.ui.optimizeShiftMaxPct = String(payload.filters.optimize_shift_max_pct ?? 20);
  state.ui.optimizeShiftStepPct = String(payload.filters.optimize_shift_step_pct ?? 5);
  state.ui.optimizeTopKScenarios = String(payload.filters.optimize_top_k_scenarios ?? 3);
  state.ui.minMarginRatePct = String(Number(payload.filters.min_margin_rate ?? 0.4) * 100);
  state.ui.maxDiscountPct = String(payload.filters.max_discount_pct ?? 20);
  state.ui.strategyPacketId = payload.filters.strategy_packet_id || "";
  state.runtime.strategyPacket = null;
  state.runtime.strategyEmailDraft = null;
  state.runtime.strategyEmailSend = null;
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

function formatPlainPct(value, digits = 1) {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "-";
  }
  return `${numeric.toFixed(digits)}%`;
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

function normalizeTokenSelections(raw, options = {}) {
  const lowerCase = options.lowerCase !== false;
  const normalizeSpaces = options.normalizeSpaces === true;
  const source = Array.isArray(raw) ? raw : typeof raw === "string" ? raw.split(",") : [];
  const values = [];
  source.forEach((item) => {
    let token = String(item || "").trim();
    if (!token) {
      return;
    }
    if (normalizeSpaces) {
      token = token.replace(/\s+/g, "_");
    }
    if (lowerCase) {
      token = token.toLowerCase();
    }
    if (!token || values.includes(token)) {
      return;
    }
    values.push(token);
  });
  return values;
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

function humanizeToken(value) {
  return String(value || "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (match) => match.toUpperCase());
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

function normalizeOptions(raw, options = {}) {
  const useHumanizedLabel = options.useHumanizedLabel === true;
  const list = Array.isArray(raw) ? raw : [];
  return list
    .map((item) => {
      if (isObject(item)) {
        const value = String(item.value || "").trim();
        const label = String(item.label || value).trim();
        if (!value || !label) {
          return null;
        }
        return { value, label };
      }
      const value = String(item || "").trim();
      if (!value) {
        return null;
      }
      return { value, label: useHumanizedLabel ? humanizeToken(value) : value };
    })
    .filter(Boolean);
}

function selectedEventValues() {
  const selected = normalizeTokenSelections(state.ui.selectedEvents, { lowerCase: true, normalizeSpaces: true });
  if (selected.length) {
    return selected;
  }
  const defaults = normalizeOptions(state.payload.uiHints?.events || DEFAULT_PAYLOAD.uiHints.events).map((item) =>
    String(item.value).toLowerCase(),
  );
  return defaults.length ? defaults : ["wedding", "holiday_party", "workwear"];
}

function selectedBrandValues() {
  return normalizeTokenSelections(state.ui.selectedBrands, { lowerCase: false });
}

function buildModelContextPayload() {
  return {
    workspace: "exec_workspace",
    active_tool: state.payload.last_tool || "fashion_exec_overview",
    lookback_days: normalizeLookbackDays(state.ui.lookbackDays, state.ui.lookbackPreset),
    objective: state.ui.objective,
    store_ids: normalizeStoreIds(state.ui.storeIds),
    events: selectedEventValues(),
    brands: selectedBrandValues(),
    to_email: state.ui.toEmail || DEFAULT_EXEC_TO_EMAIL,
    optimize_discount_min_pct: clampNumber(state.ui.optimizeDiscountMinPct, 0, 0, 60),
    optimize_discount_max_pct: clampNumber(state.ui.optimizeDiscountMaxPct, 20, 0, 60),
    optimize_discount_step_pct: clampNumber(state.ui.optimizeDiscountStepPct, 5, 1, 20),
    optimize_shift_min_pct: clampNumber(state.ui.optimizeShiftMinPct, 0, -40, 40),
    optimize_shift_max_pct: clampNumber(state.ui.optimizeShiftMaxPct, 20, -40, 40),
    optimize_shift_step_pct: clampNumber(state.ui.optimizeShiftStepPct, 5, 1, 20),
    optimize_top_k_scenarios: parsePositiveInt(state.ui.optimizeTopKScenarios, 3, 1, 10),
    min_margin_rate: clampNumber(Number(state.ui.minMarginRatePct) / 100, 0.4, 0, 1),
    max_discount_pct: clampNumber(state.ui.maxDiscountPct, 20, 0, 60),
    strategy_packet_id: state.ui.strategyPacketId || null,
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
    const brands = selectedBrandValues();
    const args = {
      ...common,
      events: selectedEventValues(),
    };
    if (brands.length) {
      args.brands = brands;
    }
    return args;
  }
  if (toolName === "fashion_exec_what_if_simulator") {
    const brands = selectedBrandValues();
    const args = {
      ...common,
      discount_pct: Number(state.ui.discountPct || 0),
      floor_space_shift_pct: Number(state.ui.floorSpaceShiftPct || 0),
      from_category: state.ui.fromCategory || undefined,
      to_category: state.ui.toCategory || undefined,
    };
    if (brands.length) {
      args.brands = brands;
    }
    return args;
  }
  if (toolName === "fashion_exec_campaign_autopilot_prepare") {
    const brands = selectedBrandValues();
    const args = {
      ...common,
      to_email: (state.ui.toEmail || DEFAULT_EXEC_TO_EMAIL).trim() || DEFAULT_EXEC_TO_EMAIL,
      lookback_days: normalizeLookbackDays(state.ui.lookbackDays, state.ui.lookbackPreset),
      top_k: parsePositiveInt(state.ui.autopilotTopK, 6, 1, 20),
      events: selectedEventValues(),
    };
    if (brands.length) {
      args.brands = brands;
    }
    return args;
  }
  if (toolName === "fashion_exec_auto_optimize_strategy") {
    const brands = selectedBrandValues();
    const args = {
      ...common,
      objective: state.ui.objective || "revenue",
      from_category: state.ui.fromCategory || undefined,
      to_category: state.ui.toCategory || undefined,
      discount_min_pct: clampNumber(state.ui.optimizeDiscountMinPct, 0, 0, 60),
      discount_max_pct: clampNumber(state.ui.optimizeDiscountMaxPct, 20, 0, 60),
      discount_step_pct: clampNumber(state.ui.optimizeDiscountStepPct, 5, 1, 20),
      shift_min_pct: clampNumber(state.ui.optimizeShiftMinPct, 0, -40, 40),
      shift_max_pct: clampNumber(state.ui.optimizeShiftMaxPct, 20, -40, 40),
      shift_step_pct: clampNumber(state.ui.optimizeShiftStepPct, 5, 1, 20),
      top_k_scenarios: parsePositiveInt(state.ui.optimizeTopKScenarios, 3, 1, 10),
      min_margin_rate: clampNumber(Number(state.ui.minMarginRatePct) / 100, 0.4, 0, 1),
      max_discount_pct: clampNumber(state.ui.maxDiscountPct, 20, 0, 60),
    };
    if (brands.length) {
      args.brands = brands;
    }
    return args;
  }
  return common;
}

function toolLabel(toolName) {
  if (
    toolName === "fashion_exec_auto_optimize_strategy" ||
    toolName === "fashion_exec_publish_strategy_packet" ||
    toolName === "fashion_exec_get_strategy_packet" ||
    toolName === "fashion_exec_prepare_strategy_packet_email" ||
    toolName === "fashion_exec_send_strategy_packet_email"
  ) {
    return "Auto-Optimize Strategy";
  }
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
  if (
    toolName === "fashion_exec_auto_optimize_strategy" ||
    toolName === "fashion_exec_publish_strategy_packet" ||
    toolName === "fashion_exec_get_strategy_packet" ||
    toolName === "fashion_exec_prepare_strategy_packet_email" ||
    toolName === "fashion_exec_send_strategy_packet_email"
  ) {
    return "Problem: Which strategy scenario should we publish and hand off so merch and associates execute the same plan?";
  }
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

function tabControlHint(toolName) {
  if (
    toolName === "fashion_exec_auto_optimize_strategy" ||
    toolName === "fashion_exec_publish_strategy_packet" ||
    toolName === "fashion_exec_get_strategy_packet" ||
    toolName === "fashion_exec_prepare_strategy_packet_email" ||
    toolName === "fashion_exec_send_strategy_packet_email"
  ) {
    return "Controls: set scenario ranges and guardrails, then publish one scenario and send an approval-gated strategy email.";
  }
  if (toolName === "fashion_exec_event_readiness_radar") {
    return "Controls: scope + lookback + events + brands determine readiness signals.";
  }
  if (toolName === "fashion_exec_what_if_simulator") {
    return "Controls: choose a category move and apply discount/space levers to the Reallocate-To category.";
  }
  if (toolName === "fashion_exec_campaign_autopilot_prepare" || toolName === "fashion_exec_campaign_autopilot_send") {
    return "Controls: scope/events/brands filter candidates; email and shortlist size define the draft package.";
  }
  return "Controls: scope + lookback apply globally; objective controls the overview lens.";
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

function activeExecFeatureFlags() {
  const features = isObject(state.payload.uiHints?.features) ? state.payload.uiHints.features : {};
  return {
    execAutoOptimizeEnabled: features.execAutoOptimizeEnabled === true,
    strategyPacketEnabled: features.strategyPacketEnabled === true,
  };
}

function canonicalTabTool(toolName) {
  const key = String(toolName || "");
  if (key === "fashion_exec_campaign_autopilot_send") {
    return "fashion_exec_campaign_autopilot_prepare";
  }
  if (
    key === "fashion_exec_publish_strategy_packet" ||
    key === "fashion_exec_get_strategy_packet" ||
    key === "fashion_exec_prepare_strategy_packet_email" ||
    key === "fashion_exec_send_strategy_packet_email"
  ) {
    return "fashion_exec_auto_optimize_strategy";
  }
  return key || "fashion_exec_overview";
}

async function publishStrategyPacket(scenario) {
  const features = activeExecFeatureFlags();
  if (!features.strategyPacketEnabled) {
    setNotice("Strategy packet feature is disabled.", "error");
    render();
    return;
  }
  if (!scenario || !isObject(scenario)) {
    setNotice("Choose a scenario before publishing.", "error");
    render();
    return;
  }
  markUserInteraction();
  state.ui.isPublishingStrategy = true;
  setNotice("Publishing strategy packet...");
  render();
  const args = {
    scenario,
    objective: state.ui.objective || "revenue",
    lookback_days: normalizeLookbackDays(state.ui.lookbackDays, state.ui.lookbackPreset),
    store_ids: normalizeStoreIds(state.ui.storeIds),
    brands: selectedBrandValues(),
    from_category: state.ui.fromCategory || undefined,
    to_category: state.ui.toCategory || undefined,
    min_margin_rate: clampNumber(Number(state.ui.minMarginRatePct) / 100, 0.4, 0, 1),
    max_discount_pct: clampNumber(state.ui.maxDiscountPct, 20, 0, 60),
    title: `Strategy Packet - ${new Date().toISOString().slice(0, 10)}`,
  };
  const result = await callTool("fashion_exec_publish_strategy_packet", args);
  state.ui.isPublishingStrategy = false;
  if (result.__toolError) {
    setNotice(result.__toolError, "error");
    render();
    return;
  }
  const payload = parseToolPayload(result);
  if (!payload || !isObject(payload) || typeof payload.packet_id !== "string") {
    setNotice("Publish strategy packet returned an unexpected payload.", "error");
    render();
    return;
  }
  state.runtime.strategyPacket = payload;
  state.runtime.strategyEmailDraft = null;
  state.runtime.strategyEmailSend = null;
  state.ui.strategyPacketId = payload.packet_id;
  if (isObject(state.payload.filters)) {
    state.payload.filters.strategy_packet_id = payload.packet_id;
  }
  setNotice(`Published strategy packet ${payload.packet_id}.`);
  queueModelContextUpdate();
  render();
}

async function prepareStrategyPacketEmail() {
  const features = activeExecFeatureFlags();
  if (!features.strategyPacketEnabled) {
    setNotice("Strategy packet feature is disabled.", "error");
    render();
    return;
  }
  const packetId = (state.ui.strategyPacketId || "").trim();
  if (!packetId) {
    setNotice("Publish a strategy packet before preparing email.", "error");
    render();
    return;
  }
  markUserInteraction();
  state.ui.isPreparingStrategyEmail = true;
  setNotice("Preparing strategy packet email...");
  render();
  const result = await callTool("fashion_exec_prepare_strategy_packet_email", {
    packet_id: packetId,
    to_email: (state.ui.toEmail || DEFAULT_EXEC_TO_EMAIL).trim() || DEFAULT_EXEC_TO_EMAIL,
  });
  state.ui.isPreparingStrategyEmail = false;
  if (result.__toolError) {
    setNotice(result.__toolError, "error");
    render();
    return;
  }
  const payload = parseToolPayload(result);
  if (!payload || !isObject(payload) || typeof payload.packet_id !== "string") {
    setNotice("Prepare strategy email returned an unexpected payload.", "error");
    render();
    return;
  }
  state.runtime.strategyEmailDraft = payload;
  setNotice(`Prepared strategy email draft for ${payload.to_email || state.ui.toEmail}.`);
  queueModelContextUpdate();
  render();
}

async function sendStrategyPacketEmail() {
  const features = activeExecFeatureFlags();
  if (!features.strategyPacketEnabled) {
    setNotice("Strategy packet feature is disabled.", "error");
    render();
    return;
  }
  const packetId = (state.ui.strategyPacketId || "").trim();
  if (!packetId) {
    setNotice("Publish a strategy packet before sending email.", "error");
    render();
    return;
  }
  markUserInteraction();
  state.ui.isSendingStrategyEmail = true;
  setNotice("Sending strategy packet email...");
  render();
  const result = await callTool("fashion_exec_send_strategy_packet_email", {
    packet_id: packetId,
    approved: true,
  });
  state.ui.isSendingStrategyEmail = false;
  if (result.__toolError) {
    setNotice(result.__toolError, "error");
    render();
    return;
  }
  const payload = parseToolPayload(result);
  if (!payload || !isObject(payload) || typeof payload.packet_id !== "string") {
    setNotice("Send strategy email returned an unexpected payload.", "error");
    render();
    return;
  }
  state.runtime.strategyEmailSend = payload;
  const status = String(payload.email_status || "");
  if (status === "sent") {
    setNotice(`Strategy packet email sent to ${payload.to_email || state.ui.toEmail}.`);
  } else {
    setNotice(payload.error_message || "Strategy packet email failed.", "error");
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

function allocationTone(beforePct, afterPct) {
  const delta = Number(afterPct || 0) - Number(beforePct || 0);
  if (delta > 0.25) {
    return "positive";
  }
  if (delta < -0.25) {
    return "negative";
  }
  return "neutral";
}

function allocationTrackRow(label, value, tone) {
  return el(
    "div",
    { className: "fw-whatif-alloc-track-row" },
    el("span", { className: "fw-whatif-alloc-track-label", text: label }),
    el(
      "div",
      { className: "fw-whatif-alloc-track" },
      el("span", {
        className: `fw-whatif-alloc-fill ${tone}`,
        style: `width:${clampNumber(value, 0, 0, 100).toFixed(2)}%;`,
      }),
    ),
    el("span", { className: "fw-whatif-alloc-track-value", text: formatPlainPct(value, 1) }),
  );
}

function renderAllocationList(title, rows, subtitle) {
  if (!Array.isArray(rows) || !rows.length) {
    return el(
      "section",
      { className: "fw-panel" },
      el("h3", { className: "fw-panel-title", text: title }),
      subtitle ? el("p", { className: "fw-empty", text: subtitle }) : null,
      el("p", { className: "fw-empty", text: "No allocation rows available for this scenario." }),
    );
  }
  return el(
    "section",
    { className: "fw-panel" },
    el("h3", { className: "fw-panel-title", text: title }),
    subtitle ? el("p", { className: "fw-empty", text: subtitle }) : null,
    el(
      "div",
      { className: "fw-list" },
      ...rows.slice(0, 10).map((row) => {
        const spaceTone = allocationTone(row.baseline_space_share_pct, row.projected_space_share_pct);
        const revenueTone = allocationTone(row.baseline_revenue_share_pct, row.projected_revenue_share_pct);
        return el(
          "article",
          { className: "fw-result fw-whatif-alloc-row" },
          el(
            "div",
            { className: "fw-whatif-alloc-head" },
            el("h4", { className: "fw-panel-title", text: humanizeToken(row.category) }),
            el(
              "div",
              { className: "fw-chip-row" },
              Number(row.applied_discount_pct || 0) > 0
                ? el("span", { className: "fw-chip", text: `Discount ${compactNumber(row.applied_discount_pct, 1)}%` })
                : el("span", { className: "fw-chip subtle", text: "No discount change" }),
              el("span", {
                className: `fw-chip fw-merch-status-chip ${revenueTone}`,
                text: `Revenue ${formatCurrency(row.baseline_revenue)} -> ${formatCurrency(row.projected_revenue)}`,
              }),
            ),
          ),
          el(
            "div",
            { className: "fw-whatif-alloc-metrics" },
            allocationTrackRow("Space Before", row.baseline_space_share_pct, "neutral"),
            allocationTrackRow("Space After", row.projected_space_share_pct, spaceTone),
            allocationTrackRow("Revenue Mix Before", row.baseline_revenue_share_pct, "neutral"),
            allocationTrackRow("Revenue Mix After", row.projected_revenue_share_pct, revenueTone),
          ),
        );
      }),
    ),
  );
}

function renderSimulator(result) {
  const networkAllocations = Array.isArray(result?.category_allocations) ? result.category_allocations : [];
  const storeAllocations = Array.isArray(result?.store_allocations) ? result.store_allocations : [];
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
      el(
        "p",
        {
          className: "fw-empty",
          text: "Allocation visuals show floor-space proxy and revenue mix before vs after the scenario.",
        },
      ),
    ),
    renderAllocationList("Network Allocation Before vs After", networkAllocations),
    ...storeAllocations.map((store) =>
      renderAllocationList(
        `${store.store_name} Allocation Before vs After`,
        Array.isArray(store.categories) ? store.categories : [],
        `${store.city}, ${store.state}`,
      ),
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

function renderAutoOptimize(result) {
  const scenarios = Array.isArray(result?.scenarios) ? result.scenarios : [];
  const packet = isObject(state.runtime.strategyPacket) ? state.runtime.strategyPacket : null;
  const emailDraft = isObject(state.runtime.strategyEmailDraft) ? state.runtime.strategyEmailDraft : null;
  const emailSend = isObject(state.runtime.strategyEmailSend) ? state.runtime.strategyEmailSend : null;
  const features = activeExecFeatureFlags();
  const strategyPacketId = packet?.packet_id || state.ui.strategyPacketId || "";

  return el(
    "div",
    { className: "fw-list" },
    el(
      "section",
      { className: "fw-panel" },
      el(
        "div",
        { className: "fw-kpi-strip" },
        kpi("Scope", result?.scope_label || "Network"),
        kpi("Lookback", `${compactNumber(result?.lookback_days, 0)}d`),
        kpi("Baseline Revenue", formatCurrency(result?.baseline_revenue)),
        kpi("Baseline Margin", formatPct(Number(result?.baseline_margin_rate || 0) * 100, 1)),
        kpi("Scenarios", compactNumber(scenarios.length, 0)),
      ),
    ),
    el(
      "section",
      { className: "fw-panel" },
      el("h3", { className: "fw-panel-title", text: "Recommended Scenarios" }),
      scenarios.length
        ? el(
            "div",
            { className: "fw-list" },
            ...scenarios.map((scenario) =>
              el(
                "article",
                { className: "fw-result" },
                el(
                  "div",
                  { className: "fw-section-head" },
                  el(
                    "div",
                    {},
                    el("h4", {
                      className: "fw-panel-title",
                      text: `${scenario.scenario_id}: ${compactNumber(scenario.discount_pct, 1)}% discount, ${compactNumber(scenario.floor_space_shift_pct, 1)}% shift`,
                    }),
                    el("p", { className: "fw-empty", text: scenario.rationale || "" }),
                  ),
                  el(
                    "div",
                    { className: "fw-chip-row" },
                    el("span", {
                      className: `fw-chip fw-merch-status-chip ${scenario.guardrail_passed ? "positive" : "negative"}`,
                      text: scenario.guardrail_passed ? "Guardrails Pass" : "Guardrails Fail",
                    }),
                    el("span", { className: "fw-chip subtle", text: `Score ${compactNumber(scenario.objective_score, 1)}` }),
                    el("span", { className: "fw-chip subtle", text: `Revenue ${formatSignedCurrency(scenario.revenue_delta)}` }),
                    el("span", {
                      className: "fw-chip subtle",
                      text: `Margin ${formatPct(Number(scenario.margin_rate_delta || 0) * 100, 2)}`,
                    }),
                  ),
                ),
                el(
                  "p",
                  {
                    className: "fw-empty",
                    text: `Confidence band: ${formatCurrency(scenario.confidence_interval_low)} to ${formatCurrency(scenario.confidence_interval_high)}`,
                  },
                ),
                Array.isArray(scenario.guardrail_reasons) && scenario.guardrail_reasons.length
                  ? el(
                      "ul",
                      { className: "fw-empty", style: "margin:0;padding-left:1rem;" },
                      ...scenario.guardrail_reasons.map((reason) => el("li", { text: reason })),
                    )
                  : null,
                features.strategyPacketEnabled
                  ? el(
                      "div",
                      { className: "fw-toolbar" },
                      el(
                        "button",
                        {
                          className: "fw-button",
                          type: "button",
                          disabled:
                            state.ui.isPublishingStrategy || !scenario.guardrail_passed || features.strategyPacketEnabled !== true
                              ? "true"
                              : null,
                          onClick: () => {
                            void publishStrategyPacket(scenario);
                          },
                        },
                        state.ui.isPublishingStrategy ? "Publishing..." : "Publish Strategy",
                      ),
                    )
                  : null,
              ),
            ),
          )
        : el("p", { className: "fw-empty", text: "No scenarios generated for this scope." }),
    ),
    features.strategyPacketEnabled
      ? el(
          "section",
          { className: "fw-panel" },
          el("h3", { className: "fw-panel-title", text: "Strategy Packet Handoff" }),
          strategyPacketId ? el("p", { className: "fw-empty", text: `Packet ID: ${strategyPacketId}` }) : null,
          packet
            ? el(
                "div",
                { className: "fw-chip-row" },
                packet.title ? el("span", { className: "fw-chip", text: packet.title }) : null,
                packet.scope_label ? el("span", { className: "fw-chip subtle", text: packet.scope_label }) : null,
                packet.to_category ? el("span", { className: "fw-chip subtle", text: `To ${humanizeToken(packet.to_category)}` }) : null,
              )
            : null,
          el(
            "div",
            { className: "fw-field" },
            el("label", { className: "fw-label", text: "Merch Manager Email" }),
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
            { className: "fw-toolbar" },
            el(
              "button",
              {
                className: "fw-button secondary",
                type: "button",
                disabled: state.ui.isPreparingStrategyEmail || !strategyPacketId ? "true" : null,
                onClick: () => {
                  void prepareStrategyPacketEmail();
                },
              },
              state.ui.isPreparingStrategyEmail ? "Preparing..." : "Prepare Email Draft",
            ),
            el(
              "button",
              {
                className: "fw-button",
                type: "button",
                disabled: state.ui.isSendingStrategyEmail || !strategyPacketId ? "true" : null,
                onClick: () => {
                  void sendStrategyPacketEmail();
                },
              },
              state.ui.isSendingStrategyEmail ? "Sending..." : "Approve + Send",
            ),
          ),
          emailDraft
            ? el(
                "details",
                { className: "fw-panel" },
                el("summary", { className: "fw-empty", text: `Draft ready for ${emailDraft.to_email}` }),
                el("p", { className: "fw-empty", text: emailDraft.subject || "" }),
                emailDraft.body_text ? el("textarea", { className: "fw-textarea", rows: "8", readonly: "true", value: emailDraft.body_text }) : null,
              )
            : null,
          emailSend
            ? el(
                "p",
                {
                  className: "fw-empty",
                  text:
                    emailSend.email_status === "sent"
                      ? `Sent to ${emailSend.to_email}${emailSend.provider_message_id ? ` (id: ${emailSend.provider_message_id})` : ""}.`
                      : emailSend.error_message || "Send failed.",
                },
              )
            : null,
        )
      : null,
  );
}

function formatSignedCurrency(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "-";
  }
  const sign = numeric > 0 ? "+" : numeric < 0 ? "-" : "";
  return `${sign}${formatCurrency(Math.abs(numeric))}`;
}

function renderResults() {
  const result = activeResult();
  if (!result) {
    return el("p", { className: "fw-empty", text: state.payload.uiHints.emptyState });
  }
  const tool = state.payload.last_tool || "fashion_exec_overview";
  if (
    tool === "fashion_exec_auto_optimize_strategy" ||
    tool === "fashion_exec_publish_strategy_packet" ||
    tool === "fashion_exec_get_strategy_packet" ||
    tool === "fashion_exec_prepare_strategy_packet_email" ||
    tool === "fashion_exec_send_strategy_packet_email"
  ) {
    return renderAutoOptimize(result);
  }
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
  onChange,
}) {
  const normalizedValue = clampNumber(value, 0, min, max);
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
      value: String(normalizedValue),
      onInput: (event) => {
        const next = clampNumber(event.target.value, normalizedValue, min, max);
        markUserInteraction();
        onChange(String(next));
        event.target.value = String(next);
        queueModelContextUpdate();
      },
    }),
  );
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

function buildCheckMultiSelect({
  selectedValues,
  options,
  labels,
  noOptionsText,
  onApply,
}) {
  const selected = new Set(selectedValues || []);
  const normalizedOptions = normalizeOptions(options);
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
          const nextValues = Array.from(selected);
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

function buildStoreMultiSelect(selectedStoreIds, options) {
  return buildCheckMultiSelect({
    selectedValues: normalizeStoreIds(selectedStoreIds),
    options,
    labels: { all: "All stores", singular: "store", plural: "stores" },
    noOptionsText: "No stores available.",
    onApply: (nextValues) => {
      state.ui.storeIds = normalizeStoreIds(nextValues);
      markUserInteraction();
      queueModelContextUpdate();
      render();
    },
  });
}

function buildEventsMultiSelect(selectedEventValuesRaw, options) {
  return buildCheckMultiSelect({
    selectedValues: normalizeTokenSelections(selectedEventValuesRaw, { lowerCase: true, normalizeSpaces: true }),
    options: normalizeOptions(options, { useHumanizedLabel: true }),
    labels: { all: "All events", singular: "event", plural: "events" },
    noOptionsText: "No events configured.",
    onApply: (nextValues) => {
      state.ui.selectedEvents = normalizeTokenSelections(nextValues, { lowerCase: true, normalizeSpaces: true });
      markUserInteraction();
      queueModelContextUpdate();
      render();
    },
  });
}

function buildBrandsMultiSelect(selectedBrandValuesRaw, options) {
  return buildCheckMultiSelect({
    selectedValues: normalizeTokenSelections(selectedBrandValuesRaw, { lowerCase: false }),
    options,
    labels: { all: "All brands", singular: "brand", plural: "brands" },
    noOptionsText: "No brands available.",
    onApply: (nextValues) => {
      state.ui.selectedBrands = normalizeTokenSelections(nextValues, { lowerCase: false });
      markUserInteraction();
      queueModelContextUpdate();
      render();
    },
  });
}

function selectionCountText(count, singular, plural, emptyText) {
  if (!count) {
    return emptyText;
  }
  return `${count} ${count === 1 ? singular : plural} selected.`;
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
    { className: "fw-hero fw-workspace-header" },
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
  const features = activeExecFeatureFlags();
  const tabs = [
    { label: "Overview", tool: "fashion_exec_overview" },
    { label: "Readiness Radar", tool: "fashion_exec_event_readiness_radar" },
    { label: "What-if Simulator", tool: "fashion_exec_what_if_simulator" },
    {
      label: "Auto-Optimize",
      tool: "fashion_exec_auto_optimize_strategy",
      featureFlag: "execAutoOptimizeEnabled",
    },
    { label: "Campaign Autopilot", tool: "fashion_exec_campaign_autopilot_prepare" },
  ];
  const isTabFeatureEnabled = (tab) => {
    if (!tab.featureFlag) {
      return true;
    }
    return features[tab.featureFlag] === true;
  };
  const normalizedTool = canonicalTabTool(tool);
  const requestedTab = tabs.find((tab) => tab.tool === normalizedTool);
  const activeTabTool = requestedTab && isTabFeatureEnabled(requestedTab) ? requestedTab.tool : tabs[0].tool;
  const activeTabIndex = Math.max(
    0,
    tabs.findIndex((tab) => tab.tool === activeTabTool),
  );
  const handleTabKeyDown = (event, index) => {
    if (state.ui.isLoading) {
      return;
    }
    let nextIndex = null;
    if (event.key === "ArrowRight") {
      nextIndex = (index + 1) % tabs.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex = (index - 1 + tabs.length) % tabs.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = tabs.length - 1;
    }
    if (nextIndex === null) {
      return;
    }
    const nextTab = tabs[nextIndex];
    if (!isTabFeatureEnabled(nextTab)) {
      event.preventDefault();
      setNotice("Auto-Optimize is disabled. Enable EXEC_AUTO_OPTIMIZE_ENABLED to use this tab.", "error");
      render();
      return;
    }
    event.preventDefault();
    void refreshExec(tabs[nextIndex].tool);
  };
  const activeTabId = tabs[activeTabIndex] ? `fw-exec-tab-${activeTabIndex}` : "fw-exec-tab-0";

  const categoryOptions = [
    { label: "Any", value: "" },
    ...normalizeOptions(state.payload.uiHints?.categoryOptions),
  ];
  const storeOptions = normalizeOptions(state.payload.uiHints?.storeOptions);
  const eventOptions = normalizeOptions(state.payload.uiHints?.events || DEFAULT_PAYLOAD.uiHints.events, {
    useHumanizedLabel: true,
  }).map((option) => ({
    value: String(option.value).toLowerCase().replace(/\s+/g, "_"),
    label: option.label,
  }));
  const brandOptions = normalizeOptions(state.payload.uiHints?.brandOptions);
  const selectedStoreCount = normalizeStoreIds(state.ui.storeIds).length;
  const selectedEventCount = selectedEventValues().length;
  const selectedBrandCount = selectedBrandValues().length;
  const normalizedLookbackDays = String(normalizeLookbackDays(state.ui.lookbackDays, state.ui.lookbackPreset));

  const lookbackPresetSelect = buildSelect(state.ui.lookbackPreset, LOOKBACK_PRESET_OPTIONS, (value) => {
    state.ui.lookbackPreset = value;
    if (value !== "custom") {
      state.ui.lookbackDays = value;
    }
  });

  const tabScopedControls = [];
  if (activeTabTool === "fashion_exec_overview" || activeTabTool === "fashion_exec_auto_optimize_strategy") {
    tabScopedControls.push(
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
    );
  }
  if (activeTabTool === "fashion_exec_event_readiness_radar") {
    tabScopedControls.push(
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Events" }),
        buildEventsMultiSelect(state.ui.selectedEvents, eventOptions),
        el("p", {
          className: "fw-empty fw-inline-meta",
          text: selectionCountText(selectedEventCount, "event", "events", "No event selected: defaults to all configured events."),
        }),
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Brand Scope" }),
        buildBrandsMultiSelect(state.ui.selectedBrands, brandOptions),
        el("p", {
          className: "fw-empty fw-inline-meta",
          text: selectionCountText(selectedBrandCount, "brand", "brands", "No brand selected: includes all brands."),
        }),
      ),
    );
  }
  if (activeTabTool === "fashion_exec_what_if_simulator") {
    tabScopedControls.push(
      el(
        "div",
        { className: "fw-field fw-span-full fw-exec-whatif-controls" },
        el(
          "div",
          { className: "fw-exec-whatif-grid" },
          el(
            "div",
            { className: "fw-field" },
            el("label", { className: "fw-label", text: "Brand Scope" }),
            buildBrandsMultiSelect(state.ui.selectedBrands, brandOptions),
            el("p", {
              className: "fw-empty fw-inline-meta",
              text: selectionCountText(selectedBrandCount, "brand", "brands", "No brand selected: includes all brands."),
            }),
          ),
          el(
            "div",
            { className: "fw-exec-whatif-pair" },
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
          ),
          el(
            "div",
            { className: "fw-exec-whatif-pair" },
            buildPercentLeverField({
              label: "Discount on Reallocate-To (%)",
              value: state.ui.discountPct,
              min: 0,
              max: 60,
              step: 1,
              onChange: (value) => {
                state.ui.discountPct = value;
              },
            }),
            buildPercentLeverField({
              label: "Floor Space Shift to Reallocate-To (%)",
              value: state.ui.floorSpaceShiftPct,
              min: -40,
              max: 40,
              step: 1,
              onChange: (value) => {
                state.ui.floorSpaceShiftPct = value;
              },
            }),
          ),
        ),
      ),
    );
  }
  if (activeTabTool === "fashion_exec_auto_optimize_strategy") {
    tabScopedControls.push(
      el(
        "div",
        { className: "fw-field fw-span-full fw-exec-whatif-controls" },
        el(
          "div",
          { className: "fw-exec-whatif-grid" },
          el(
            "div",
            { className: "fw-field" },
            el("label", { className: "fw-label", text: "Brand Scope" }),
            buildBrandsMultiSelect(state.ui.selectedBrands, brandOptions),
            el("p", {
              className: "fw-empty fw-inline-meta",
              text: selectionCountText(selectedBrandCount, "brand", "brands", "No brand selected: includes all brands."),
            }),
          ),
          el(
            "div",
            { className: "fw-exec-whatif-pair" },
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
          ),
          el(
            "div",
            { className: "fw-exec-whatif-pair" },
            buildPercentLeverField({
              label: "Discount Min (%)",
              value: state.ui.optimizeDiscountMinPct,
              min: 0,
              max: 60,
              step: 1,
              onChange: (value) => {
                state.ui.optimizeDiscountMinPct = value;
              },
            }),
            buildPercentLeverField({
              label: "Discount Max (%)",
              value: state.ui.optimizeDiscountMaxPct,
              min: 0,
              max: 60,
              step: 1,
              onChange: (value) => {
                state.ui.optimizeDiscountMaxPct = value;
              },
            }),
          ),
          el(
            "div",
            { className: "fw-exec-whatif-pair" },
            buildPercentLeverField({
              label: "Shift Min (%)",
              value: state.ui.optimizeShiftMinPct,
              min: -40,
              max: 40,
              step: 1,
              onChange: (value) => {
                state.ui.optimizeShiftMinPct = value;
              },
            }),
            buildPercentLeverField({
              label: "Shift Max (%)",
              value: state.ui.optimizeShiftMaxPct,
              min: -40,
              max: 40,
              step: 1,
              onChange: (value) => {
                state.ui.optimizeShiftMaxPct = value;
              },
            }),
          ),
          el(
            "div",
            { className: "fw-exec-whatif-pair" },
            buildPercentLeverField({
              label: "Discount Step (%)",
              value: state.ui.optimizeDiscountStepPct,
              min: 1,
              max: 20,
              step: 1,
              onChange: (value) => {
                state.ui.optimizeDiscountStepPct = value;
              },
            }),
            buildPercentLeverField({
              label: "Shift Step (%)",
              value: state.ui.optimizeShiftStepPct,
              min: 1,
              max: 20,
              step: 1,
              onChange: (value) => {
                state.ui.optimizeShiftStepPct = value;
              },
            }),
          ),
          el(
            "div",
            { className: "fw-exec-whatif-pair" },
            buildPercentLeverField({
              label: "Min Margin Guardrail (%)",
              value: state.ui.minMarginRatePct,
              min: 0,
              max: 100,
              step: 1,
              onChange: (value) => {
                state.ui.minMarginRatePct = value;
              },
            }),
            buildPercentLeverField({
              label: "Max Discount Guardrail (%)",
              value: state.ui.maxDiscountPct,
              min: 0,
              max: 60,
              step: 1,
              onChange: (value) => {
                state.ui.maxDiscountPct = value;
              },
            }),
          ),
          el(
            "div",
            { className: "fw-field" },
            el("label", { className: "fw-label", text: "Top Scenarios" }),
            el("input", {
              className: "fw-input",
              type: "number",
              min: "1",
              max: "10",
              value: state.ui.optimizeTopKScenarios,
              onInput: (event) => {
                markUserInteraction();
                state.ui.optimizeTopKScenarios = event.target.value;
                queueModelContextUpdate();
              },
            }),
          ),
        ),
      ),
    );
  }
  if (activeTabTool === "fashion_exec_campaign_autopilot_prepare") {
    tabScopedControls.push(
      el(
        "div",
        { className: "fw-field fw-span-full fw-exec-autopilot-controls" },
        el(
          "div",
          { className: "fw-exec-autopilot-row" },
          el(
            "div",
            { className: "fw-field" },
            el("label", { className: "fw-label", text: "Events" }),
            buildEventsMultiSelect(state.ui.selectedEvents, eventOptions),
          ),
          el(
            "div",
            { className: "fw-field" },
            el("label", { className: "fw-label", text: "Brand Scope" }),
            buildBrandsMultiSelect(state.ui.selectedBrands, brandOptions),
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
        ),
      ),
    );
  }

  const controlsPanel = el(
    "section",
    { className: "fw-panel fw-controls-panel fw-exec-controls-panel" },
    notice,
    el(
      "div",
      { className: "fw-chip-row fw-exec-feature-row" },
      el(
        "span",
        {
          className: `fw-chip fw-merch-status-chip ${features.execAutoOptimizeEnabled ? "positive" : "neutral"}`,
          text: `Auto-Optimize ${features.execAutoOptimizeEnabled ? "On" : "Off"}`,
        },
      ),
      el(
        "span",
        {
          className: `fw-chip fw-merch-status-chip ${features.strategyPacketEnabled ? "positive" : "neutral"}`,
          text: `Strategy Packet ${features.strategyPacketEnabled ? "On" : "Off"}`,
        },
      ),
      !features.execAutoOptimizeEnabled || !features.strategyPacketEnabled
        ? el(
            "span",
            {
              className: "fw-chip subtle",
              text: "Enable EXEC_AUTO_OPTIMIZE_ENABLED and STRATEGY_PACKET_ENABLED to surface full flow.",
            },
          )
        : null,
    ),
    el(
      "div",
      { className: "fw-exec-global-row" },
      el(
        "div",
        { className: "fw-field fw-exec-store-field" },
        el("label", { className: "fw-label", text: "Store Scope" }),
        buildStoreMultiSelect(state.ui.storeIds, storeOptions),
        el("p", {
          className: "fw-empty fw-inline-meta",
          text: selectionCountText(selectedStoreCount, "store", "stores", "No stores selected: defaults to company-wide network."),
        }),
      ),
      el(
        "div",
        { className: "fw-exec-global-side" },
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
      ),
    ),
    el(
      "div",
      { className: "fw-merch-nav fw-exec-nav" },
      el(
        "div",
        {
          className: "fw-tabs fw-merch-tabs",
          role: "tablist",
          "aria-label": "Executive views",
          "aria-orientation": "horizontal",
        },
        ...tabs.map((tab, index) => {
          const isActive = index === activeTabIndex;
          const featureEnabled = isTabFeatureEnabled(tab);
          return el(
            "button",
            {
              id: `fw-exec-tab-${index}`,
              className: `fw-tab fw-merch-tab ${isActive ? "active" : ""}`,
              type: "button",
              role: "tab",
              "aria-selected": isActive ? "true" : "false",
              "aria-controls": "fw-exec-view-panel",
              tabindex: isActive ? "0" : "-1",
              disabled: state.ui.isLoading || !featureEnabled ? "true" : null,
              onKeydown: (event) => {
                handleTabKeyDown(event, index);
              },
              onClick: () => {
                if (!featureEnabled) {
                  setNotice("Auto-Optimize is disabled. Enable EXEC_AUTO_OPTIMIZE_ENABLED to use this tab.", "error");
                  render();
                  return;
                }
                void refreshExec(tab.tool);
              },
            },
            state.ui.isLoading && tab.tool === activeTabTool ? "Loading..." : featureEnabled ? tab.label : `${tab.label} (Off)`,
          );
        }),
      ),
      el(
        "button",
        {
          className: "fw-button",
          type: "button",
          disabled: state.ui.isLoading ? "true" : null,
          onClick: () => {
            void refreshExec(activeTabTool);
          },
        },
        state.ui.isLoading ? "Refreshing..." : "Refresh",
      ),
    ),
    el("p", { className: "fw-empty fw-exec-control-hint", text: tabControlHint(activeTabTool) }),
    tabScopedControls.length ? el("div", { className: "fw-grid merch-filters fw-exec-tab-grid" }, ...tabScopedControls) : null,
  );

  const contextTool = canonicalTabTool(tool);
  const contextPanel = el(
    "section",
    { className: "fw-panel", id: "fw-exec-view-panel", role: "tabpanel", "aria-labelledby": activeTabId },
    el("h2", { className: "fw-panel-title", text: toolLabel(contextTool) }),
    el("p", { className: "fw-empty fw-exec-problem", text: tabProblemStatement(contextTool) }),
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
