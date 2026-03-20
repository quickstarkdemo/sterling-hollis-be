const root = document.getElementById("fashion-widget-root");
const meta = window.__FASHION_WIDGET__ || {};
const MODEL_CONTEXT_UPDATE_DEBOUNCE_MS = 600;
const TREND_DEBUG_ENABLED = meta?.trendDebug === true;

const DEFAULT_PAYLOAD = {
  store: null,
  strategy_context: null,
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
    compare_store_ids: [],
    top_k: 9,
  },
  initial_result: null,
  last_result: null,
  last_tool: "fashion_merch_action_recommendations",
  initial_notice: null,
  uiHints: {
    questionPlaceholder: "Optional context (e.g., wedding occasion, protect margin, next 8 weeks)",
    emptyState: "Run Prioritize, Diagnostics, or Trends to populate this workspace.",
    categoryOptions: [],
    brandOptions: [],
    compareStoreOptions: [{ value: "", label: "Auto peer set" }],
    actionDefinitions: {
      feature: "Strongest demand momentum versus baseline with healthy margin/inventory for full-price placement.",
      promote: "Featured Campaign candidates: margin >= 42%, inventory >= 6 units, and softer demand that can respond to campaign support.",
      deprioritize: "Inventory pressure plus below-baseline demand; reduce exposure and floor priority.",
    },
    features: {
      merchStrategyContextEnabled: false,
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
    lookbackPreset: "90",
    lookbackDays: "90",
    compareMode: "peer_and_prior_period",
    peerMode: "state_and_profile",
    compareStoreId: "",
    compareStoreIds: [],
    topK: "9",
    strategyTagIntensity: "medium",
    strategyContextExpanded: false,
    merchAdvancedOpen: true,
    strategyStoreId: "",
    csvText: "",
    notice: "",
    noticeTone: "info",
    isLoading: false,
    isExporting: false,
    isSavingStrategyOverride: false,
    isResettingStrategyOverride: false,
  },
  runtime: {
    toolOutputApplied: false,
    userInteracted: false,
    modelContextHash: "",
    modelContextTimer: null,
    trendChartEngine: typeof meta?.trendChartEngine === "string" ? meta.trendChartEngine.toLowerCase() : "chartjs",
    trendMetric: "revenue",
    trendHoverIndex: null,
    trendInteractionCleanup: null,
    trendInteractionUpdateCount: 0,
    chartCleanupFns: [],
    filtersDirty: false,
    diagnosticsShowAll: false,
    diagnosticsExpanded: {},
  },
};

const LOOKBACK_PRESET_OPTIONS = [
  { label: "Last 2 Weeks", value: "14" },
  { label: "Last 4 Weeks", value: "28" },
  { label: "Last 8 Weeks", value: "56" },
  { label: "Last Quarter (13 Weeks)", value: "90" },
  { label: "Last 6 Months", value: "180" },
  { label: "Last 12 Months", value: "365" },
  { label: "Custom Days", value: "custom" },
];

function isObject(value) {
  return value && typeof value === "object" && !Array.isArray(value);
}

function clone(value) {
  if (value === null || value === undefined) {
    return value;
  }
  return JSON.parse(JSON.stringify(value));
}

function teardownTrendInteraction() {
  if (typeof state.runtime.trendInteractionCleanup === "function") {
    try {
      state.runtime.trendInteractionCleanup();
    } catch {
      // Best-effort cleanup.
    }
  }
  state.runtime.trendInteractionCleanup = null;
}

function registerChartCleanup(cleanup) {
  if (typeof cleanup !== "function") {
    return;
  }
  state.runtime.chartCleanupFns.push(cleanup);
}

function teardownChartControllers() {
  const cleanups = Array.isArray(state.runtime.chartCleanupFns) ? [...state.runtime.chartCleanupFns] : [];
  state.runtime.chartCleanupFns = [];
  cleanups.forEach((cleanup) => {
    try {
      cleanup();
    } catch {
      // Best-effort cleanup.
    }
  });
}

function teardownMerchVisualControllers() {
  teardownTrendInteraction();
  teardownChartControllers();
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

function normalizeBrandOptions(raw) {
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
    if (!value || !label) {
      return;
    }
    const key = value.toLowerCase();
    if (seen.has(key)) {
      return;
    }
    seen.add(key);
    options.push({ value, label });
  });
  options.sort((left, right) => left.label.localeCompare(right.label, "en", { sensitivity: "base" }));
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

function normalizeStrategyContext(raw) {
  if (!isObject(raw)) {
    return null;
  }
  const packetId = typeof raw.packet_id === "string" ? raw.packet_id.trim() : "";
  if (!packetId) {
    return null;
  }
  const scenario = isObject(raw.scenario) ? clone(raw.scenario) : null;
  const normalizeStrategyCore = (candidate) => {
    if (!isObject(candidate)) {
      return null;
    }
    const objective = typeof candidate.objective === "string" ? candidate.objective : null;
    const lookbackDays = Number.isFinite(Number(candidate.lookback_days)) ? Number(candidate.lookback_days) : null;
    const category = typeof candidate.category === "string" ? candidate.category : null;
    const brands = normalizeSelectionList(candidate.brands);
    const discountPct = Number.isFinite(Number(candidate.discount_pct)) ? Number(candidate.discount_pct) : null;
    const shiftPct = Number.isFinite(Number(candidate.floor_space_shift_pct))
      ? Number(candidate.floor_space_shift_pct)
      : null;
    const minMarginRate = Number.isFinite(Number(candidate.min_margin_rate)) ? Number(candidate.min_margin_rate) : null;
    const maxDiscountPct = Number.isFinite(Number(candidate.max_discount_pct)) ? Number(candidate.max_discount_pct) : null;
    return {
      objective,
      lookback_days: lookbackDays,
      category,
      brands,
      discount_pct: discountPct,
      floor_space_shift_pct: shiftPct,
      min_margin_rate: minMarginRate,
      max_discount_pct: maxDiscountPct,
    };
  };
  const packetStrategyCore = normalizeStrategyCore(raw.strategy_core);
  const effectiveStrategyCore = normalizeStrategyCore(raw.effective_strategy_core) || packetStrategyCore;
  const normalizeScopeStoreOptions = (candidate, fallbackIds) => {
    const ids = normalizeSelectionList(fallbackIds);
    const seen = new Set();
    const options = [];
    if (Array.isArray(candidate)) {
      candidate.forEach((item) => {
        if (!isObject(item)) {
          return;
        }
        const value = typeof item.value === "string" ? item.value.trim() : "";
        if (!value || seen.has(value)) {
          return;
        }
        seen.add(value);
        const label = typeof item.label === "string" && item.label.trim() ? item.label.trim() : value;
        options.push({ value, label });
      });
    }
    ids.forEach((storeId) => {
      if (!seen.has(storeId)) {
        seen.add(storeId);
        options.push({ value: storeId, label: storeId });
      }
    });
    return options;
  };
  const scopeStoreIds = normalizeSelectionList(raw.scope_store_ids);
  const scopeStoreOptions = normalizeScopeStoreOptions(raw.scope_store_options, scopeStoreIds);
  const handoffStoreId = typeof raw.handoff_store_id === "string" ? raw.handoff_store_id.trim() : "";
  const currentStoreId = typeof raw.current_store_id === "string" ? raw.current_store_id.trim() : "";
  return {
    packet_id: packetId,
    title: typeof raw.title === "string" ? raw.title : "",
    summary: typeof raw.summary === "string" ? raw.summary : "",
    objective: typeof raw.objective === "string" ? raw.objective : "",
    lookback_days: Number.isFinite(Number(raw.lookback_days)) ? Number(raw.lookback_days) : null,
    scope_label: typeof raw.scope_label === "string" ? raw.scope_label : "",
    scope_store_ids: scopeStoreIds,
    scope_store_options: scopeStoreOptions,
    handoff_store_id: handoffStoreId || null,
    current_store_id: currentStoreId || null,
    brands: normalizeSelectionList(raw.brands),
    from_category: typeof raw.from_category === "string" ? raw.from_category : null,
    to_category: typeof raw.to_category === "string" ? raw.to_category : null,
    min_margin_rate: Number.isFinite(Number(raw.min_margin_rate)) ? Number(raw.min_margin_rate) : null,
    max_discount_pct: Number.isFinite(Number(raw.max_discount_pct)) ? Number(raw.max_discount_pct) : null,
    strategy_core: packetStrategyCore,
    effective_strategy_core: effectiveStrategyCore,
    tag_intensity: typeof raw.tag_intensity === "string" ? raw.tag_intensity : "medium",
    effective_tag_intensity: typeof raw.effective_tag_intensity === "string" ? raw.effective_tag_intensity : "medium",
    override_active: raw.override_active === true,
    effective_source: typeof raw.effective_source === "string" ? raw.effective_source : "packet",
    override_updated_at: typeof raw.override_updated_at === "string" ? raw.override_updated_at : null,
    scenario,
    updated_at: typeof raw.updated_at === "string" ? raw.updated_at : null,
  };
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

function normalizeSelectionList(raw) {
  const source = Array.isArray(raw) ? raw : typeof raw === "string" ? raw.split(",") : [];
  const values = [];
  source.forEach((item) => {
    const token = String(item || "").trim();
    if (!token || values.includes(token)) {
      return;
    }
    values.push(token);
  });
  return values;
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
  const explicitCompareStoreIds = normalizeSelectionList(rawFilters.compare_store_ids);
  const legacyCompareStoreId =
    typeof rawFilters.compare_store_id === "string" && rawFilters.compare_store_id.trim()
      ? rawFilters.compare_store_id.trim()
      : null;
  const normalizedCompareStoreIds = explicitCompareStoreIds.length
    ? explicitCompareStoreIds
    : legacyCompareStoreId
      ? normalizeSelectionList(legacyCompareStoreId)
      : [];
  const initialResult =
    isObject(raw.initial_result) ? clone(raw.initial_result) : isObject(raw.initialResult) ? clone(raw.initialResult) : null;
  const lastResult =
    isObject(raw.last_result) ? clone(raw.last_result) : isObject(raw.lastResult) ? clone(raw.lastResult) : null;

  return {
    store: clone(raw.store),
    strategy_context: normalizeStrategyContext(raw.strategy_context),
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
      compare_store_id: normalizedCompareStoreIds[0] || null,
      compare_store_ids: normalizedCompareStoreIds,
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
      brandOptions: normalizeBrandOptions(raw.uiHints && raw.uiHints.brandOptions),
      compareStoreOptions: normalizeCompareStoreOptions(raw.uiHints && raw.uiHints.compareStoreOptions),
      actionDefinitions: normalizeActionDefinitions(raw.uiHints && raw.uiHints.actionDefinitions),
      features: {
        merchStrategyContextEnabled: Boolean(raw?.uiHints?.features?.merchStrategyContextEnabled),
      },
    },
  };
}

function applyWorkspacePayload(raw) {
  const payload = normalizeWorkspacePayload(raw);
  if (!payload) {
    return false;
  }
  teardownMerchVisualControllers();
  state.payload = payload;
  state.runtime.trendMetric = "revenue";
  state.runtime.trendHoverIndex = null;
  state.runtime.trendInteractionUpdateCount = 0;
  state.runtime.filtersDirty = false;
  state.runtime.diagnosticsShowAll = false;
  state.runtime.diagnosticsExpanded = {};
  hydrateUiFromFilters(payload.filters);
  const context = normalizeStrategyContext(payload.strategy_context);
  state.ui.strategyTagIntensity =
    (context && typeof context.effective_tag_intensity === "string" && context.effective_tag_intensity) || "medium";
  state.ui.strategyStoreId = payload.store && typeof payload.store.id === "string" ? payload.store.id : "";
  state.ui.strategyContextExpanded = false;
  state.ui.merchAdvancedOpen = context ? false : true;
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

function markFilterChange() {
  markUserInteraction();
  state.runtime.filtersDirty = true;
}

function normalizeLookbackDays(value) {
  return String(parsePositiveInt(value, 90, 7, 730));
}

function presetFromLookbackDays(value) {
  const normalized = normalizeLookbackDays(value);
  const match = LOOKBACK_PRESET_OPTIONS.find((option) => option.value === normalized);
  return match ? match.value : "custom";
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
  state.ui.lookbackDays = normalizeLookbackDays(filters.lookback_days);
  state.ui.lookbackPreset = presetFromLookbackDays(filters.lookback_days);
  state.ui.compareMode = typeof filters.compare_mode === "string" ? filters.compare_mode : "peer_and_prior_period";
  state.ui.peerMode = typeof filters.peer_mode === "string" ? filters.peer_mode : "state_and_profile";
  const compareStoreIds = normalizeSelectionList(filters.compare_store_ids);
  if (!compareStoreIds.length && typeof filters.compare_store_id === "string") {
    compareStoreIds.push(...normalizeSelectionList(filters.compare_store_id));
  }
  state.ui.compareStoreIds = compareStoreIds;
  state.ui.compareStoreId = compareStoreIds[0] || "";
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
    "lookbackPreset",
    "lookbackDays",
    "compareMode",
    "peerMode",
    "topK",
    "strategyTagIntensity",
    "strategyStoreId",
    "csvText",
  ];
  for (const key of textFields) {
    if (typeof raw[key] === "string" && raw[key] !== state.ui[key]) {
      state.ui[key] = raw[key];
      changed = true;
    }
  }
  if (typeof raw.lookbackDays === "string" && typeof raw.lookbackPreset !== "string") {
    const derivedPreset = presetFromLookbackDays(raw.lookbackDays);
    if (derivedPreset !== state.ui.lookbackPreset) {
      state.ui.lookbackPreset = derivedPreset;
      changed = true;
    }
  }
  if (typeof raw.strategyContextExpanded === "boolean" && raw.strategyContextExpanded !== state.ui.strategyContextExpanded) {
    state.ui.strategyContextExpanded = raw.strategyContextExpanded;
    changed = true;
  }
  if (typeof raw.merchAdvancedOpen === "boolean" && raw.merchAdvancedOpen !== state.ui.merchAdvancedOpen) {
    state.ui.merchAdvancedOpen = raw.merchAdvancedOpen;
    changed = true;
  }
  const compareStoreIdsInput =
    Array.isArray(raw.compareStoreIds) || typeof raw.compareStoreIds === "string"
      ? raw.compareStoreIds
      : typeof raw.compareStoreId === "string"
        ? raw.compareStoreId
        : null;
  if (compareStoreIdsInput !== null) {
    const normalized = normalizeSelectionList(compareStoreIdsInput);
    const normalizedSerialized = normalized.join("|");
    const existingSerialized = normalizeSelectionList(state.ui.compareStoreIds).join("|");
    if (normalizedSerialized !== existingSerialized) {
      state.ui.compareStoreIds = normalized;
      state.ui.compareStoreId = normalized[0] || "";
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
      lookbackPreset: state.ui.lookbackPreset,
      lookbackDays: state.ui.lookbackDays,
      compareMode: state.ui.compareMode,
      peerMode: state.ui.peerMode,
      compareStoreId: state.ui.compareStoreId,
      compareStoreIds: clone(state.ui.compareStoreIds),
      topK: state.ui.topK,
      strategyTagIntensity: state.ui.strategyTagIntensity,
      strategyContextExpanded: Boolean(state.ui.strategyContextExpanded),
      merchAdvancedOpen: Boolean(state.ui.merchAdvancedOpen),
      strategyStoreId: state.ui.strategyStoreId,
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
  const compareStoreIds = normalizeSelectionList(state.ui.compareStoreIds);
  const strategyContext = normalizeStrategyContext(state.payload.strategy_context);
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
    lookback_days: Number(normalizeLookbackDays(state.ui.lookbackDays)),
    compare_mode: state.ui.compareMode,
    peer_mode: state.ui.peerMode,
    compare_store_id: compareStoreIds[0] || null,
    compare_store_ids: compareStoreIds,
    top_k: Number(state.ui.topK || 9),
    strategy_packet_id: strategyContext?.packet_id || null,
    strategy_objective: strategyContext?.objective || null,
    strategy_to_category: strategyContext?.to_category || null,
    strategy_tag_intensity: state.ui.strategyTagIntensity || "medium",
    strategy_store_id: state.ui.strategyStoreId || state.payload.store?.id || null,
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
  const compareStoreIds = normalizeSelectionList(state.ui.compareStoreIds);
  return {
    question: state.ui.question.trim() || null,
    objective: state.ui.objective || "margin",
    category: state.ui.category.trim() || null,
    brand: state.ui.brand.trim() || null,
    price_band: state.ui.priceBand || null,
    occasion: state.ui.occasion.trim() || null,
    lookback_days: Number(normalizeLookbackDays(state.ui.lookbackDays)),
    compare_mode: state.ui.compareMode || "peer_and_prior_period",
    peer_mode: state.ui.peerMode || "state_and_profile",
    compare_store_id: compareStoreIds.length ? compareStoreIds.join(",") : null,
    compare_store_ids: compareStoreIds,
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

  teardownMerchVisualControllers();
  state.payload.last_result = data;
  state.payload.last_tool = toolName;
  state.ui.csvText = "";
  state.runtime.trendMetric = "revenue";
  state.runtime.trendHoverIndex = null;
  state.runtime.trendInteractionUpdateCount = 0;
  state.runtime.filtersDirty = false;
  state.runtime.diagnosticsShowAll = false;
  state.runtime.diagnosticsExpanded = {};
  syncPayloadFilters();
  persistWidgetState();
  setNotice(`${toolLabel(toolName)} loaded.`);
  render();
}

async function refreshActiveView() {
  const toolName = state.payload.last_tool || "fashion_merch_action_recommendations";
  await refreshMerch(toolName);
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

function parseBrandSelections(value) {
  if (!value || typeof value !== "string") {
    return [];
  }
  const unique = [];
  value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
    .forEach((item) => {
      if (!unique.some((existing) => existing.toLowerCase() === item.toLowerCase())) {
        unique.push(item);
      }
    });
  return unique;
}

function serializeBrandSelections(values) {
  if (!Array.isArray(values) || !values.length) {
    return "";
  }
  return values.join(", ");
}

function brandSelectionSummary(values) {
  if (!Array.isArray(values) || !values.length) {
    return "All brands";
  }
  if (values.length === 1) {
    return values[0];
  }
  return `${values.length} brands selected`;
}

function currentStrategyCorePayload(context) {
  const effectiveCore = isObject(context?.effective_strategy_core) ? context.effective_strategy_core : null;
  const packetCore = isObject(context?.strategy_core) ? context.strategy_core : null;
  const referenceCore = effectiveCore || packetCore || {};
  return {
    objective: state.ui.objective || referenceCore.objective || "margin",
    lookback_days: Number(normalizeLookbackDays(state.ui.lookbackDays)),
    category: state.ui.category.trim() || referenceCore.category || null,
    brands: parseBrandSelections(state.ui.brand),
    discount_pct: Number.isFinite(Number(referenceCore.discount_pct)) ? Number(referenceCore.discount_pct) : 0,
    floor_space_shift_pct: Number.isFinite(Number(referenceCore.floor_space_shift_pct))
      ? Number(referenceCore.floor_space_shift_pct)
      : 0,
    min_margin_rate: Number.isFinite(Number(referenceCore.min_margin_rate)) ? Number(referenceCore.min_margin_rate) : 0.4,
    max_discount_pct: Number.isFinite(Number(referenceCore.max_discount_pct)) ? Number(referenceCore.max_discount_pct) : 20,
  };
}

async function reloadMerchWorkspaceFromStrategy(context, noticeText, options = {}) {
  if (!context?.packet_id) {
    return false;
  }
  const nextStoreId =
    typeof options.storeId === "string" && options.storeId.trim()
      ? options.storeId.trim()
      : state.payload.store?.id || context.handoff_store_id || "";
  if (!nextStoreId) {
    return false;
  }
  const compareStoreIds = normalizeSelectionList(state.ui.compareStoreIds);
  const activeToolBeforeReload = options.activeTool || state.payload.last_tool || "fashion_merch_action_recommendations";
  const args = {
    store_id: nextStoreId,
    question: state.ui.question.trim() || undefined,
    objective: options.resetStrategyCore === true ? "margin" : state.ui.objective || "margin",
    lookback_days:
      options.resetStrategyCore === true ? 90 : Number(normalizeLookbackDays(state.ui.lookbackDays || "90")),
    top_k: parsePositiveInt(state.ui.topK, 9, 1, 50),
    category: options.resetStrategyCore === true ? undefined : state.ui.category.trim() || undefined,
    brand: options.resetStrategyCore === true ? undefined : state.ui.brand.trim() || undefined,
    price_band: state.ui.priceBand || undefined,
    occasion: state.ui.occasion.trim() || undefined,
    compare_mode: state.ui.compareMode || "peer_and_prior_period",
    peer_mode: state.ui.peerMode || "state_and_profile",
    compare_store_id: compareStoreIds[0] || undefined,
    strategy_packet_id: context.packet_id,
    initial_notice: noticeText || undefined,
  };
  const result = await callTool("fashion_open_merch_workspace", args);
  if (result.__toolError) {
    setNotice(result.__toolError, "error");
    render();
    return false;
  }
  const changed = applyWorkspacePayload(result);
  if (!changed) {
    setNotice("Strategy refresh returned an unexpected payload.", "error");
    render();
    return false;
  }
  state.ui.strategyStoreId = nextStoreId;
  persistWidgetState();
  if (activeToolBeforeReload !== "fashion_merch_action_recommendations") {
    await refreshMerch(activeToolBeforeReload);
    return true;
  }
  if (noticeText) {
    setNotice(noticeText);
  }
  render();
  return true;
}

async function saveStrategyOverride(options = {}) {
  const usePacketDefaults = options.usePacketDefaults === true;
  const context = normalizeStrategyContext(state.payload.strategy_context);
  if (!context?.packet_id || !state.payload.store?.id) {
    setNotice("No active strategy packet is loaded for this store.", "error");
    render();
    return;
  }
  markUserInteraction();
  if (usePacketDefaults) {
    state.ui.isResettingStrategyOverride = true;
    setNotice("Applying packet defaults...");
  } else {
    state.ui.isSavingStrategyOverride = true;
    setNotice("Saving store override...");
  }
  render();
  const args = {
    packet_id: context.packet_id,
    store_id: state.payload.store.id,
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
  const payload = parseToolPayload(result);
  if (!payload || !isObject(payload)) {
    setNotice("Strategy override save returned an unexpected payload.", "error");
    render();
    return;
  }
  await reloadMerchWorkspaceFromStrategy(
    context,
    usePacketDefaults ? "Reverted to packet defaults for this store." : "Saved store-level strategy override.",
    {
      resetStrategyCore: true,
    },
  );
}

async function switchStrategyStore(context, nextStoreId) {
  const normalizedStoreId = String(nextStoreId || "").trim();
  if (!context?.packet_id || !normalizedStoreId) {
    return;
  }
  if (state.ui.isLoading || state.ui.isSavingStrategyOverride || state.ui.isResettingStrategyOverride) {
    return;
  }
  if (normalizedStoreId === (state.payload.store?.id || "")) {
    state.ui.strategyStoreId = normalizedStoreId;
    persistWidgetState();
    return;
  }
  markUserInteraction();
  state.ui.isLoading = true;
  state.ui.strategyStoreId = normalizedStoreId;
  setNotice("Switching strategy store...");
  persistWidgetState();
  render();
  const switched = await reloadMerchWorkspaceFromStrategy(
    context,
    "Pinned strategy store updated.",
    {
      storeId: normalizedStoreId,
      resetStrategyCore: false,
      activeTool: state.payload.last_tool || "fashion_merch_action_recommendations",
    },
  );
  state.ui.isLoading = false;
  if (!switched) {
    setNotice("Unable to switch strategy store.", "error");
    render();
    return;
  }
  render();
}

function buildBrandMultiSelect(selectedCsv, options) {
  const currentValues = parseBrandSelections(selectedCsv);
  const selected = new Set(currentValues.map((value) => value.toLowerCase()));
  const byKey = new Map(
    (Array.isArray(options) ? options : []).map((option) => [String(option.value).toLowerCase(), option]),
  );
  const details = el("details", { className: "fw-multi-select" });
  const summary = el("summary", { className: "fw-input fw-multi-select-summary", text: brandSelectionSummary(currentValues) });
  const list = el("div", { className: "fw-multi-select-list" });

  if (!Array.isArray(options) || !options.length) {
    list.appendChild(el("p", { className: "fw-empty", text: "No brand options available for this store." }));
  } else {
    options.forEach((option) => {
      const key = String(option.value).toLowerCase();
      const checked = selected.has(key);
      const checkbox = el("input", { type: "checkbox", checked: checked ? "true" : null });
      const label = el(
        "label",
        { className: "fw-multi-select-option" },
        checkbox,
        el("span", { text: option.label }),
      );
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) {
          selected.add(key);
        } else {
          selected.delete(key);
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
          const selectedValues = Array.from(selected)
            .map((key) => byKey.get(key))
            .filter(Boolean)
            .map((option) => option.value);
          const nextValue = serializeBrandSelections(selectedValues);
          state.ui.brand = nextValue;
          summary.textContent = brandSelectionSummary(selectedValues);
          details.open = false;
          markFilterChange();
          persistWidgetState();
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

function compareStoreSelectionSummary(values, labelsByValue, autoLabel) {
  if (!Array.isArray(values) || !values.length) {
    return autoLabel || "Auto peer set";
  }
  if (values.length === 1) {
    return labelsByValue.get(values[0]) || "1 store selected";
  }
  return `${values.length} stores selected`;
}

function selectionSignature(values) {
  return normalizeSelectionList(values).slice().sort().join("|");
}

function buildCompareStoreMultiSelect(selectedValuesRaw, options, autoLabel, autoSelectedValuesRaw) {
  const explicitValues = normalizeSelectionList(selectedValuesRaw);
  const autoSelectedValues = normalizeSelectionList(autoSelectedValuesRaw);
  const hasExplicitSelection = explicitValues.length > 0;
  const initialValues = hasExplicitSelection ? explicitValues : autoSelectedValues;
  const selected = new Set(initialValues);
  const normalizedOptions = (Array.isArray(options) ? options : [])
    .map((option) => {
      if (!isObject(option)) {
        return null;
      }
      const value = String(option.value || "").trim();
      const label = String(option.label || value).trim();
      if (!value || !label) {
        return null;
      }
      return { value, label };
    })
    .filter(Boolean);
  const byValue = new Map(normalizedOptions.map((option) => [option.value, option]));
  const labelsByValue = new Map(normalizedOptions.map((option) => [option.value, option.label]));
  const details = el("details", { className: "fw-multi-select" });
  const summary = el("summary", {
    className: "fw-input fw-multi-select-summary",
    text: compareStoreSelectionSummary(hasExplicitSelection ? explicitValues : [], labelsByValue, autoLabel),
  });
  const list = el("div", { className: "fw-multi-select-list" });

  if (!normalizedOptions.length) {
    list.appendChild(el("p", { className: "fw-empty", text: "No peer stores available for explicit compare." }));
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
          const selectedValues = Array.from(selected)
            .map((value) => byValue.get(value))
            .filter(Boolean)
            .map((option) => option.value);
          const canStayAuto =
            !hasExplicitSelection &&
            autoSelectedValues.length > 0 &&
            selectionSignature(selectedValues) === selectionSignature(autoSelectedValues);
          const nextExplicitValues = canStayAuto ? [] : selectedValues;
          state.ui.compareStoreIds = nextExplicitValues;
          state.ui.compareStoreId = nextExplicitValues[0] || "";
          summary.textContent = compareStoreSelectionSummary(nextExplicitValues, labelsByValue, autoLabel);
          details.open = false;
          markFilterChange();
          persistWidgetState();
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

function buildSelect(currentValue, options, onChange, config = {}) {
  const markDirty = config.markDirty !== false;
  const shouldPersist = config.persist !== false;
  const shouldRender = config.rerender !== false;
  const node = el("select", {
    className: "fw-input fw-select",
    disabled: config.disabled ? "true" : null,
    onChange: (event) => {
      if (markDirty) {
        markFilterChange();
      } else {
        markUserInteraction();
      }
      onChange(event.target.value);
      if (shouldPersist) {
        persistWidgetState();
      }
      if (shouldRender) {
        render();
      }
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

function lookbackWindowLabel(daysValue) {
  const days = parsePositiveInt(daysValue, 90, 7, 730);
  const mapping = {
    14: "2w",
    28: "4w",
    56: "8w",
    90: "13w",
    180: "26w",
    365: "52w",
  };
  return mapping[days] || `${days}d`;
}

function resultHeader(result) {
  if (!result) {
    return null;
  }
  const peerCount = Array.isArray(result.peer_store_ids) ? result.peer_store_ids.length : 0;
  return el(
    "div",
    { className: "fw-kpi-strip" },
    kpi("Baseline", baselineLabel(result)),
    kpi("Compare", humanizeToken(result.compare_mode || state.ui.compareMode)),
    kpi("Peers", String(peerCount || 0)),
    kpi("Window", lookbackWindowLabel(result.lookback_days || state.ui.lookbackDays)),
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

function peerCountForResult(result) {
  return Array.isArray(result?.peer_store_ids) ? result.peer_store_ids.length : 0;
}

function autoPeerSetLabel(result) {
  const count = peerCountForResult(result);
  const mode = humanizeToken(result?.peer_mode || state.ui.peerMode || state.payload?.filters?.peer_mode || "state_and_profile");
  if (count > 0) {
    return `Auto peer set (${count} ${count === 1 ? "store" : "stores"} · ${mode})`;
  }
  return `Auto peer set (${mode})`;
}

function peerBoxLabel(result) {
  if (result?.compare_mode === "prior_period") {
    return "Prior Period";
  }
  if (result && typeof result.compare_store_name === "string" && result.compare_store_name.trim()) {
    return result.compare_store_name.trim();
  }
  return autoPeerSetLabel(result);
}

function renderStrategyContextCard() {
  const context = normalizeStrategyContext(state.payload.strategy_context);
  const strategyEnabled = Boolean(state.payload?.uiHints?.features?.merchStrategyContextEnabled);
  if (!strategyEnabled || !context) {
    return null;
  }
  const scenario = isObject(context.scenario) ? context.scenario : null;
  const packetCore = isObject(context.strategy_core) ? context.strategy_core : null;
  const effectiveCore = isObject(context.effective_strategy_core) ? context.effective_strategy_core : packetCore;
  const scopeOptions = Array.isArray(context.scope_store_options) ? context.scope_store_options : [];
  const scopeCount = Array.isArray(context.scope_store_ids) ? context.scope_store_ids.length : scopeOptions.length;
  const storeName = state.payload.store?.name || "Current store";
  const effectiveSummary = [];
  if (effectiveCore?.objective) {
    effectiveSummary.push(`Objective ${humanizeToken(effectiveCore.objective)}`);
  }
  if (Number.isFinite(Number(effectiveCore?.lookback_days))) {
    effectiveSummary.push(`Lookback ${effectiveCore.lookback_days}d`);
  }
  if (effectiveCore?.category) {
    effectiveSummary.push(`Category ${humanizeToken(effectiveCore.category)}`);
  }
  if (Array.isArray(effectiveCore?.brands) && effectiveCore.brands.length) {
    effectiveSummary.push(`${effectiveCore.brands.length} brands`);
  }
  if (Number.isFinite(Number(effectiveCore?.discount_pct))) {
    effectiveSummary.push(`Discount ${compactNumber(effectiveCore.discount_pct)}%`);
  }
  if (Number.isFinite(Number(effectiveCore?.floor_space_shift_pct))) {
    effectiveSummary.push(`Shift ${compactNumber(effectiveCore.floor_space_shift_pct)}%`);
  }
  if (Number.isFinite(Number(effectiveCore?.min_margin_rate))) {
    effectiveSummary.push(`Min margin ${(Number(effectiveCore.min_margin_rate) * 100).toFixed(1)}%`);
  }
  if (Number.isFinite(Number(effectiveCore?.max_discount_pct))) {
    effectiveSummary.push(`Max discount ${compactNumber(effectiveCore.max_discount_pct)}%`);
  }
  const effectiveSummaryText = effectiveSummary.length ? effectiveSummary.join(" · ") : "Using packet defaults.";
  const renderCoreChips = (core, prefix) => {
    if (!core) {
      return [];
    }
    const chips = [];
    if (core.objective) {
      chips.push(el("span", { className: "fw-chip subtle", text: `${prefix} Objective ${humanizeToken(core.objective)}` }));
    }
    if (Number.isFinite(Number(core.lookback_days))) {
      chips.push(el("span", { className: "fw-chip subtle", text: `${prefix} Lookback ${core.lookback_days}d` }));
    }
    if (core.category) {
      chips.push(el("span", { className: "fw-chip subtle", text: `${prefix} Category ${humanizeToken(core.category)}` }));
    }
    if (Array.isArray(core.brands) && core.brands.length) {
      chips.push(el("span", { className: "fw-chip subtle", text: `${prefix} ${core.brands.length} brands` }));
    }
    if (Number.isFinite(Number(core.discount_pct))) {
      chips.push(el("span", { className: "fw-chip subtle", text: `${prefix} Discount ${compactNumber(core.discount_pct)}%` }));
    }
    if (Number.isFinite(Number(core.floor_space_shift_pct))) {
      chips.push(el("span", { className: "fw-chip subtle", text: `${prefix} Shift ${compactNumber(core.floor_space_shift_pct)}%` }));
    }
    if (Number.isFinite(Number(core.min_margin_rate))) {
      chips.push(
        el("span", {
          className: "fw-chip subtle",
          text: `${prefix} Min margin ${(Number(core.min_margin_rate) * 100).toFixed(1)}%`,
        }),
      );
    }
    if (Number.isFinite(Number(core.max_discount_pct))) {
      chips.push(el("span", { className: "fw-chip subtle", text: `${prefix} Max discount ${compactNumber(core.max_discount_pct)}%` }));
    }
    return chips;
  };
  return el(
    "section",
    { className: "fw-panel fw-strategy-context-card" },
    el(
      "div",
      { className: "fw-section-head" },
      el(
        "div",
        { className: "fw-strategy-title-wrap" },
        el("div", { className: "fw-kicker", text: "Strategy Context" }),
        el("h3", { className: "fw-panel-title", text: context.title || "Active strategy packet" }),
        el("p", { className: "fw-empty fw-strategy-summary-line", text: effectiveSummaryText }),
      ),
      el("span", { className: "fw-chip subtle", text: context.packet_id }),
    ),
    el(
      "div",
      { className: "fw-chip-row" },
      context.objective ? el("span", { className: "fw-chip", text: `Objective ${humanizeToken(context.objective)}` }) : null,
      scopeCount ? el("span", { className: "fw-chip subtle", text: `${scopeCount} scoped stores` }) : null,
      el("span", { className: "fw-chip subtle", text: `Pinned ${storeName}` }),
      context.override_active
        ? el("span", { className: "fw-chip fw-merch-status-chip positive", text: "Store Override Active" })
        : el("span", { className: "fw-chip subtle", text: "Using Packet Defaults" }),
      el("span", { className: "fw-chip subtle", text: `Tag Intensity ${humanizeToken(context.effective_tag_intensity || "medium")}` }),
    ),
    el(
      "div",
      { className: "fw-grid merch-filters fw-strategy-actions-row" },
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Tag Intensity" }),
        buildSelect(
          state.ui.strategyTagIntensity || "medium",
          [
            { value: "low", label: "Low" },
            { value: "medium", label: "Medium" },
            { value: "high", label: "High" },
          ],
          (value) => {
            state.ui.strategyTagIntensity = value;
          },
          { markDirty: false, persist: true, rerender: false },
        ),
      ),
      el(
        "div",
        { className: "fw-field actions" },
        el("label", { className: "fw-label", text: "Strategy Actions" }),
        el(
          "div",
          { className: "fw-toolbar" },
          el(
            "button",
            {
              className: "fw-button",
              type: "button",
              disabled: state.ui.isSavingStrategyOverride || state.ui.isResettingStrategyOverride ? "true" : null,
              onClick: () => {
                void saveStrategyOverride({ usePacketDefaults: false });
              },
            },
            state.ui.isSavingStrategyOverride ? "Saving..." : "Save Store Override",
          ),
          el(
            "button",
            {
              className: "fw-button secondary",
              type: "button",
              disabled: state.ui.isSavingStrategyOverride || state.ui.isResettingStrategyOverride ? "true" : null,
              onClick: () => {
                void saveStrategyOverride({ usePacketDefaults: true });
              },
            },
            state.ui.isResettingStrategyOverride ? "Reverting..." : "Use Packet Defaults",
          ),
        ),
      ),
    ),
    context.summary ? el("p", { className: "fw-empty fw-inline-meta", text: context.summary }) : null,
    el(
      "button",
      {
        className: "fw-text-button",
        type: "button",
        onClick: () => {
          state.ui.strategyContextExpanded = !state.ui.strategyContextExpanded;
          persistWidgetState();
          render();
        },
      },
      state.ui.strategyContextExpanded ? "Hide details" : "Show details",
    ),
    state.ui.strategyContextExpanded
      ? el(
          "div",
          { className: "fw-strategy-details" },
          packetCore
            ? el(
                "p",
                { className: "fw-empty fw-inline-meta" },
                "Packet defaults are shown below. Save Store Override to persist tuned store-specific strategy values.",
              )
            : null,
          el("div", { className: "fw-chip-row" }, ...renderCoreChips(packetCore, "Packet")),
          el("div", { className: "fw-chip-row" }, ...renderCoreChips(effectiveCore, "Effective")),
          scenario
            ? el(
                "p",
                { className: "fw-empty fw-inline-meta" },
                `Scenario ${scenario.scenario_id}: ${compactNumber(scenario.discount_pct)}% discount, ${compactNumber(scenario.floor_space_shift_pct)}% shift.`,
              )
            : null,
        )
      : null,
  );
}

function renderActions(result) {
  const items = Array.isArray(result?.recommendations) ? result.recommendations : [];
  if (!items.length) {
    return el("p", { className: "fw-empty", text: state.payload.uiHints.emptyState });
  }
  const actionMeta = {
    feature: { kicker: "Full-Price Priority", title: "Feature" },
    promote: { kicker: "Campaign Candidate", title: "Featured Campaign" },
    deprioritize: { kicker: "Floor Space Recovery", title: "Deprioritize" },
  };
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
            el("div", { className: "fw-kicker", text: actionMeta[action].kicker }),
            el("h3", { className: "fw-panel-title", text: actionMeta[action].title }),
            el("p", { className: "fw-empty", text: `Criteria: ${state.payload.uiHints.actionDefinitions[action]}` }),
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
  return result?.compare_store_name ? result.compare_store_name : autoPeerSetLabel(result);
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
  const impactChart = renderDiagnosticsImpactChart(items);

  return el(
    "div",
    { className: "fw-list fw-merch-diagnostics" },
    impactChart,
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
    return false;
  }
  state.runtime.trendHoverIndex = normalized;
  return true;
}

function resolveTrendChartEngine() {
  const requested = state.runtime.trendChartEngine === "native" ? "native" : "chartjs";
  const hasChartJs = typeof window?.Chart === "function";
  if (requested === "chartjs" && hasChartJs) {
    return "chartjs";
  }
  return "native";
}

function chartJsStatusColor(status) {
  const key = String(status || "").toLowerCase();
  if (key === "healthy_momentum") {
    return "#1f8f63";
  }
  if (key === "discount_led_growth") {
    return "#c07c1d";
  }
  if (key === "margin_risk" || key === "velocity_gap" || key === "conversion_gap") {
    return "#c44d57";
  }
  return "#7e8fa1";
}

function formatChartTick(value, metricConfig) {
  if (metricConfig.key === "units") {
    return compactNumber(value);
  }
  return formatCurrencyCompact(value);
}

function renderTrendChartChartJs(config) {
  const { series, metricConfig, currentLabel, baselineLabelText, latestCurrent, latestBaseline, latestDeltaPct, momentumPct } = config;
  const hasBaselineLatest = latestBaseline !== null && latestBaseline !== undefined;
  const panel = el(
    "section",
    { className: "fw-panel fw-trend-chart-panel fw-merch-trend-panel" },
    el(
      "div",
      { className: "fw-merch-trend-head" },
      el("h3", { className: "fw-panel-title", text: metricConfig.title }),
      el(
        "div",
        {},
        el(
          "div",
          { className: "fw-merch-segmented", role: "tablist", "aria-label": "Trend metric selector" },
          ...["revenue", "units"].map((metricOption) =>
            el(
              "button",
              {
                className: `fw-merch-segmented-btn ${metricOption === metricConfig.key ? "active" : ""}`,
                type: "button",
                role: "tab",
                "aria-selected": metricOption === metricConfig.key ? "true" : "false",
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
      el("span", { className: "fw-chip subtle", text: baselineLabelText }),
    ),
    el(
      "div",
      { className: "fw-merch-chart-wrap fw-merch-trend-canvas" },
      el("canvas", {
        className: "fw-merch-chart-canvas fw-merch-trend-chartjs-canvas",
        "data-role": "trend-chartjs-canvas",
        "aria-label": `Weekly ${metricConfig.ariaMetricLabel} trend chart with baseline comparison`,
      }),
    ),
    el("p", { className: "fw-empty", text: "Hover chart points for week-level details." }),
  );

  const canvas = panel.querySelector("[data-role='trend-chartjs-canvas']");
  const ChartCtor = window?.Chart;
  if (canvas && typeof ChartCtor === "function") {
    const ctx = canvas.getContext("2d");
    if (ctx) {
      const labels = series.map((point) => formatDateLabel(point.period_start, false));
      const fullDateLabels = series.map((point) => formatDateLabel(point.period_start, true));
      const datasetCurrent = series.map((point) => point[metricConfig.currentField]);
      const datasetBaseline = series.map((point) => point[metricConfig.baselineField]);
      const chart = new ChartCtor(ctx, {
        type: "line",
        data: {
          labels,
          datasets: [
            {
              label: currentLabel,
              data: datasetCurrent,
              borderColor: "#1f5d8f",
              backgroundColor: "rgba(31, 93, 143, 0.1)",
              pointRadius: 2.6,
              pointHoverRadius: 4.4,
              tension: 0.3,
              borderWidth: 2.4,
              fill: true,
              spanGaps: true,
            },
            {
              label: baselineLabelText,
              data: datasetBaseline,
              borderColor: "#7d8e9f",
              pointRadius: 2.1,
              pointHoverRadius: 3.6,
              tension: 0.28,
              borderWidth: 2,
              fill: false,
              spanGaps: true,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: "rgba(255,255,255,0.96)",
              borderColor: "#c6d8e8",
              borderWidth: 1,
              titleColor: "#1e3449",
              bodyColor: "#1e3449",
              displayColors: false,
              padding: 10,
              callbacks: {
                title: (items) => {
                  if (!items?.length) {
                    return "";
                  }
                  return fullDateLabels[items[0].dataIndex] || "";
                },
                label: (item) => `${item.dataset.label}: ${metricConfig.formatValue(item.parsed.y)}`,
                afterBody: (items) => {
                  const currentItem = items.find((item) => item.datasetIndex === 0);
                  const baselineItem = items.find((item) => item.datasetIndex === 1);
                  const currentValue = currentItem ? Number(currentItem.parsed.y) : null;
                  const baselineValue = baselineItem ? Number(baselineItem.parsed.y) : null;
                  if (!Number.isFinite(currentValue) || !Number.isFinite(baselineValue) || baselineValue === 0) {
                    return "";
                  }
                  const deltaPct = ((currentValue - baselineValue) / baselineValue) * 100;
                  return `Delta ${formatSignedPercent(deltaPct, 1)}`;
                },
              },
            },
          },
          scales: {
            x: {
              grid: { display: false },
              ticks: { color: "#617386", maxTicksLimit: 3, autoSkip: true },
            },
            y: {
              grid: { color: "#e4edf6" },
              ticks: {
                color: "#617386",
                callback: (value) => formatChartTick(value, metricConfig),
              },
            },
          },
        },
      });
      registerChartCleanup(() => {
        chart.destroy();
      });
    }
  }
  return panel;
}

function renderDiagnosticsImpactChart(items) {
  const rows = Array.isArray(items) ? items.slice(0, 5) : [];
  if (!rows.length) {
    return null;
  }
  if (resolveTrendChartEngine() !== "chartjs") {
    return null;
  }
  const ChartCtor = window?.Chart;
  if (typeof ChartCtor !== "function") {
    return null;
  }
  const panel = el(
    "section",
    { className: "fw-panel fw-merch-diag-chart-panel" },
    el("h3", { className: "fw-panel-title", text: "Diagnostics Impact Overview" }),
    el(
      "div",
      { className: "fw-merch-chart-wrap fw-merch-diag-chart-wrap" },
      el("canvas", { className: "fw-merch-chart-canvas fw-merch-diag-chart-canvas", "data-role": "diag-chartjs-canvas" }),
    ),
  );
  const canvas = panel.querySelector("[data-role='diag-chartjs-canvas']");
  if (!canvas) {
    return panel;
  }
  const ctx = canvas.getContext("2d");
  if (!ctx) {
    return panel;
  }

  const labels = rows.map((item) => String(item.subject || "-"));
  const values = rows.map((item) => Number(item.delta) || 0);
  const maxAbs = Math.max(1, ...values.map((value) => Math.abs(value)));
  const chart = new ChartCtor(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          data: values,
          borderRadius: 5,
          borderSkipped: false,
          backgroundColor: rows.map((item) => chartJsStatusColor(item.status)),
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      indexAxis: "y",
      plugins: {
        legend: { display: false },
        tooltip: {
          displayColors: false,
          callbacks: {
            title: (items) => (items?.length ? String(items[0].label || "") : ""),
            label: (item) => `Revenue delta ${formatSignedCompact(item.parsed.x, { maximumFractionDigits: 1 })}`,
          },
        },
      },
      scales: {
        y: {
          grid: { display: false },
          ticks: { color: "#526578", font: { size: 11 } },
        },
        x: {
          min: -maxAbs,
          max: maxAbs,
          grid: { color: "#e4edf6" },
          ticks: {
            color: "#617386",
            callback: (value) => formatCurrencyCompact(value),
          },
        },
      },
    },
  });
  registerChartCleanup(() => {
    chart.destroy();
  });
  return panel;
}

function mountNativeTrendInteraction(panel, config) {
  const {
    series,
    metricConfig,
    currentLabel,
    baselineLabelText,
    width,
    chartLeft,
    chartRight,
    chartTop,
    chartBottom,
    plotHeight,
    xFor,
    yFor,
    fallbackIndex,
  } = config;
  const svg = panel.querySelector("[data-role='trend-svg']");
  const crosshair = panel.querySelector("[data-role='trend-crosshair']");
  const tooltip = panel.querySelector("[data-role='trend-tooltip']");
  const tooltipDate = panel.querySelector("[data-role='trend-tooltip-date']");
  const tooltipCurrent = panel.querySelector("[data-role='trend-tooltip-current']");
  const tooltipBaseline = panel.querySelector("[data-role='trend-tooltip-baseline']");
  const tooltipDelta = panel.querySelector("[data-role='trend-tooltip-delta']");
  const currentPoints = Array.from(panel.querySelectorAll(".fw-merch-point.current"));
  const baselinePoints = Array.from(panel.querySelectorAll(".fw-merch-point.baseline"));
  if (!svg || !crosshair || !tooltip || !tooltipDate || !tooltipCurrent || !tooltipBaseline || !tooltipDelta) {
    return () => {};
  }

  let activeIndex =
    Number.isInteger(state.runtime.trendHoverIndex) && state.runtime.trendHoverIndex >= 0 && state.runtime.trendHoverIndex < series.length
      ? state.runtime.trendHoverIndex
      : fallbackIndex;
  let pointerRaf = null;
  let resizeRaf = null;
  let pendingClientX = null;
  let chartRect = null;

  const measureChart = () => {
    chartRect = svg.getBoundingClientRect();
  };

  const updatePointStyles = () => {
    currentPoints.forEach((node) => {
      const idx = Number(node.getAttribute("data-index"));
      const isActive = idx === activeIndex;
      node.setAttribute("r", isActive ? "4.2" : "2.6");
      node.setAttribute("stroke-width", isActive ? "1.5" : "0.8");
      node.setAttribute("opacity", isActive ? "1" : "0.92");
    });
    baselinePoints.forEach((node) => {
      const idx = Number(node.getAttribute("data-index"));
      const isActive = idx === activeIndex;
      node.setAttribute("r", isActive ? "3.2" : "2.1");
      node.setAttribute("opacity", isActive ? "1" : "0.85");
    });
  };

  const updateTooltip = () => {
    const activePoint = series[activeIndex];
    const currentValue = activePoint?.[metricConfig.currentField];
    const baselineValue = activePoint?.[metricConfig.baselineField];
    const hasBaseline = baselineValue !== null && baselineValue !== undefined;
    const deltaPct =
      hasBaseline && Number(baselineValue) !== 0
        ? ((Number(currentValue) - Number(baselineValue)) / Number(baselineValue)) * 100
        : null;
    const activeX = xFor(activeIndex);
    const activeY = Number.isFinite(Number(currentValue)) ? yFor(currentValue) : chartTop + plotHeight / 2;
    crosshair.setAttribute("x1", activeX.toFixed(2));
    crosshair.setAttribute("x2", activeX.toFixed(2));
    tooltip.style.left = `${((activeX / width) * 100).toFixed(2)}%`;
    tooltip.style.top = `${Math.max(12, activeY - 12).toFixed(2)}px`;
    tooltipDate.textContent = formatDateLabel(activePoint?.period_start, true);
    tooltipCurrent.textContent = `${currentLabel}: ${metricConfig.formatValue(currentValue)}`;
    if (hasBaseline) {
      tooltipBaseline.textContent = `${baselineLabelText}: ${metricConfig.formatValue(baselineValue)}`;
      tooltipBaseline.classList.remove("is-hidden");
      tooltipDelta.textContent = `Delta ${formatSignedPercent(deltaPct, 1)}`;
      tooltipDelta.className = `fw-merch-trend-tooltip-delta ${valueToneClass(deltaPct)}`;
      tooltipDelta.classList.remove("is-hidden");
    } else {
      tooltipBaseline.classList.add("is-hidden");
      tooltipDelta.classList.add("is-hidden");
      tooltipDelta.className = "fw-merch-trend-tooltip-delta is-hidden";
    }
  };

  const setActiveIndex = (nextIndex) => {
    const normalized = Number.isInteger(nextIndex) ? Math.max(0, Math.min(series.length - 1, nextIndex)) : fallbackIndex;
    if (normalized === activeIndex) {
      return;
    }
    activeIndex = normalized;
    setTrendHoverIndex(normalized);
    updatePointStyles();
    updateTooltip();
    state.runtime.trendInteractionUpdateCount += 1;
    if (TREND_DEBUG_ENABLED && state.runtime.trendInteractionUpdateCount % 25 === 0) {
      console.debug("[merch-trend] local updates", state.runtime.trendInteractionUpdateCount);
    }
  };

  const nearestIndexFromClientX = (clientX) => {
    if (!chartRect || !chartRect.width) {
      return activeIndex;
    }
    const ratio = (clientX - chartRect.left) / chartRect.width;
    const viewX = Math.max(chartLeft, Math.min(width - chartRight, ratio * width));
    let nearest = 0;
    let nearestDistance = Number.POSITIVE_INFINITY;
    for (let idx = 0; idx < series.length; idx += 1) {
      const distance = Math.abs(viewX - xFor(idx));
      if (distance < nearestDistance) {
        nearestDistance = distance;
        nearest = idx;
      }
    }
    return nearest;
  };

  const onMouseMove = (event) => {
    pendingClientX = event.clientX;
    if (pointerRaf !== null) {
      return;
    }
    pointerRaf = window.requestAnimationFrame(() => {
      pointerRaf = null;
      if (!Number.isFinite(pendingClientX)) {
        return;
      }
      setActiveIndex(nearestIndexFromClientX(pendingClientX));
    });
  };

  const onKeyDown = (event) => {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      setActiveIndex(activeIndex - 1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      setActiveIndex(activeIndex + 1);
    }
  };

  const onResize = () => {
    if (resizeRaf !== null) {
      return;
    }
    resizeRaf = window.requestAnimationFrame(() => {
      resizeRaf = null;
      measureChart();
    });
  };

  measureChart();
  setTrendHoverIndex(activeIndex);
  updatePointStyles();
  updateTooltip();
  svg.addEventListener("mousemove", onMouseMove);
  svg.addEventListener("keydown", onKeyDown);
  window.addEventListener("resize", onResize);

  return () => {
    svg.removeEventListener("mousemove", onMouseMove);
    svg.removeEventListener("keydown", onKeyDown);
    window.removeEventListener("resize", onResize);
    if (pointerRaf !== null) {
      window.cancelAnimationFrame(pointerRaf);
      pointerRaf = null;
    }
    if (resizeRaf !== null) {
      window.cancelAnimationFrame(resizeRaf);
      resizeRaf = null;
    }
  };
}

function renderTrendChart(result) {
  const points = Array.isArray(result?.time_series) ? result.time_series : [];
  if (!points.length) {
    return null;
  }
  const resolvedEngine = resolveTrendChartEngine();
  const usingNativeFallback = state.runtime.trendChartEngine !== "native" && resolvedEngine === "native";
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

  if (resolvedEngine === "chartjs") {
    return renderTrendChartChartJs({
      series,
      metricConfig,
      currentLabel,
      baselineLabelText,
      latestCurrent,
      latestBaseline,
      latestDeltaPct,
      momentumPct,
    });
  }

  const fallbackIndex = series.length - 1;
  const panel = el(
    "section",
    { className: "fw-panel fw-trend-chart-panel fw-merch-trend-panel" },
    el(
      "div",
      { className: "fw-merch-trend-head" },
      el("h3", { className: "fw-panel-title", text: metricConfig.title }),
      el(
        "div",
        {},
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
        usingNativeFallback ? el("p", { className: "fw-merch-engine-note", text: "Chart.js unavailable, native fallback active." }) : null,
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
          "data-role": "trend-svg",
          viewBox: `0 0 ${width} ${height}`,
          role: "img",
          tabindex: "0",
          "aria-label": `Weekly ${metricConfig.ariaMetricLabel} trend chart with baseline comparison`,
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
          "data-role": "trend-crosshair",
          x1: xFor(fallbackIndex).toFixed(2),
          y1: chartTop.toFixed(2),
          x2: xFor(fallbackIndex).toFixed(2),
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
          return el("circle", {
            className: "fw-merch-point baseline",
            "data-index": String(idx),
            cx: xFor(idx).toFixed(2),
            cy: yFor(baselineValue).toFixed(2),
            r: "2.1",
            fill: "#7d8e9f",
            opacity: "0.85",
          });
        }),
        ...series.map((point, idx) => {
          const currentValue = point[metricConfig.currentField];
          if (currentValue === null || currentValue === undefined) {
            return null;
          }
          return el("circle", {
            className: "fw-merch-point current",
            "data-index": String(idx),
            cx: xFor(idx).toFixed(2),
            cy: yFor(currentValue).toFixed(2),
            r: "2.6",
            fill: "#1f5d8f",
            stroke: "#fff",
            "stroke-width": "0.8",
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
          "data-role": "trend-tooltip",
          style: `left:${((xFor(fallbackIndex) / width) * 100).toFixed(2)}%;top:24px;`,
        },
        el("strong", { "data-role": "trend-tooltip-date", text: "" }),
        el("span", { "data-role": "trend-tooltip-current", text: "" }),
        el("span", { "data-role": "trend-tooltip-baseline", text: "" }),
        el("span", { className: "fw-merch-trend-tooltip-delta", "data-role": "trend-tooltip-delta", text: "" }),
      ),
    ),
    el("p", { className: "fw-empty", text: "Hover or use left/right arrows for week-level detail." }),
  );
  state.runtime.trendInteractionCleanup = mountNativeTrendInteraction(panel, {
    series,
    metricConfig,
    currentLabel,
    baselineLabelText,
    width,
    chartLeft,
    chartRight,
    chartTop,
    chartBottom,
    plotHeight,
    xFor,
    yFor,
    fallbackIndex,
  });
  return panel;
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
  teardownMerchVisualControllers();
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
  const strategyContext = normalizeStrategyContext(state.payload.strategy_context);
  const strategyScopedStores = Array.isArray(strategyContext?.scope_store_options)
    ? strategyContext.scope_store_options
    : [];
  const showStrategyStorePicker = Boolean(strategyContext?.packet_id && strategyScopedStores.length);
  const selectedStrategyStoreId = (() => {
    const current = String(state.ui.strategyStoreId || store?.id || strategyContext?.handoff_store_id || "").trim();
    if (!showStrategyStorePicker) {
      return current;
    }
    const matched = strategyScopedStores.find((option) => option.value === current);
    if (matched) {
      return matched.value;
    }
    return strategyScopedStores[0].value;
  })();
  state.ui.strategyStoreId = selectedStrategyStoreId;

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
      markFilterChange();
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

  const brandOptions = Array.isArray(state.payload.uiHints.brandOptions) ? state.payload.uiHints.brandOptions : [];
  const brandMultiSelect = buildBrandMultiSelect(state.ui.brand, brandOptions);

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

  const compareStoreBaseOptions =
    Array.isArray(state.payload.uiHints.compareStoreOptions) && state.payload.uiHints.compareStoreOptions.length
      ? state.payload.uiHints.compareStoreOptions
      : [{ label: "Auto peer set", value: "" }];
  const compareStoreOptions = compareStoreBaseOptions.map((option) =>
    option.value === "" ? { ...option, label: autoPeerSetLabel(result) } : option,
  );
  const autoPeerIds = Array.isArray(result?.peer_store_ids) ? normalizeSelectionList(result.peer_store_ids) : [];
  const compareStoreMultiSelect = buildCompareStoreMultiSelect(
    state.ui.compareStoreIds,
    compareStoreOptions.filter((option) => option.value),
    autoPeerSetLabel(result),
    autoPeerIds,
  );
  const lookbackPresetSelect = buildSelect(state.ui.lookbackPreset, LOOKBACK_PRESET_OPTIONS, (value) => {
    state.ui.lookbackPreset = value;
    if (value !== "custom") {
      state.ui.lookbackDays = value;
    }
  });
  if (state.ui.lookbackPreset === "custom") {
    lookbackPresetSelect.title = `Current custom window: ${normalizeLookbackDays(state.ui.lookbackDays)} days`;
  }

  const tabDefinitions = [
    { id: "actions", tool: "fashion_merch_action_recommendations", label: "Prioritize" },
    { id: "diagnostics", tool: "fashion_merch_diagnostics", label: "Diagnostics" },
    { id: "trends", tool: "fashion_merch_trend_summary", label: "Trends" },
  ];
  const activeTabIndex = Math.max(
    0,
    tabDefinitions.findIndex((tab) => tab.tool === activeTool),
  );
  const handleTabKeyDown = (event, index) => {
    if (state.ui.isLoading) {
      return;
    }
    let nextIndex = null;
    if (event.key === "ArrowRight") {
      nextIndex = (index + 1) % tabDefinitions.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex = (index - 1 + tabDefinitions.length) % tabDefinitions.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = tabDefinitions.length - 1;
    }
    if (nextIndex === null) {
      return;
    }
    event.preventDefault();
    void refreshMerch(tabDefinitions[nextIndex].tool);
  };
  const tabButtons = tabDefinitions.map((tab, index) => {
    const isActive = index === activeTabIndex;
    return el(
      "button",
      {
        id: `fw-tab-${tab.id}`,
        className: `fw-tab fw-merch-tab ${isActive ? "active" : ""}`,
        type: "button",
        role: "tab",
        "aria-selected": isActive ? "true" : "false",
        "aria-controls": "fw-merch-view-panel",
        tabindex: isActive ? "0" : "-1",
        disabled: state.ui.isLoading ? "true" : null,
        onKeydown: (event) => {
          handleTabKeyDown(event, index);
        },
        onClick: () => {
          void refreshMerch(tab.tool);
        },
      },
      state.ui.isLoading && isActive ? "Loading..." : tab.label,
    );
  });
  const activeTabId = tabDefinitions[activeTabIndex] ? `fw-tab-${tabDefinitions[activeTabIndex].id}` : "fw-tab-actions";
  const strategyStoreSelect = showStrategyStorePicker
    ? buildSelect(
        selectedStrategyStoreId,
        strategyScopedStores,
        (value) => {
          const nextStoreId = String(value || "").trim();
          state.ui.strategyStoreId = nextStoreId;
          void switchStrategyStore(strategyContext, nextStoreId);
        },
        { markDirty: false, persist: false, rerender: false, disabled: state.ui.isLoading },
      )
    : null;

  const controlsPanel = el(
    "section",
    { className: "fw-panel fw-controls-panel" },
    el("h2", { className: "fw-panel-title", text: showStrategyStorePicker ? "Strategy Store" : store ? `Store: ${store.name}` : "Merch Workspace" }),
    notice,
    showStrategyStorePicker
      ? el(
          "div",
          { className: "fw-field fw-strategy-store-field" },
          el("label", { className: "fw-label", text: "Strategy Store" }),
          strategyStoreSelect,
          el("p", {
            className: "fw-empty fw-inline-meta fw-strategy-store-help",
            text: "Evaluations apply to selected strategy store.",
          }),
          store
            ? el("p", {
                className: "fw-empty",
                text: `${store.city}, ${store.state} • profile ${humanizeToken(store.profile_type)}`,
              })
            : null,
        )
      : store
        ? el("p", {
            className: "fw-empty",
            text: `${store.city}, ${store.state} • profile ${humanizeToken(store.profile_type)}`,
          })
        : el("p", { className: "fw-empty", text: "No store resolved yet. Open from a store query in chat." }),
    renderStrategyContextCard(),
    el(
      "div",
      { className: "fw-grid merch-filters fw-merch-clean-filters" },
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
        brandMultiSelect,
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
            markFilterChange();
            state.ui.occasion = event.target.value;
            persistWidgetState();
          },
        }),
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Time Window" }),
        lookbackPresetSelect,
        state.ui.lookbackPreset === "custom"
          ? el("input", {
              className: "fw-input",
              type: "number",
              min: "7",
              max: "730",
              value: state.ui.lookbackDays,
              placeholder: "Custom days",
              onInput: (event) => {
                markFilterChange();
                state.ui.lookbackDays = event.target.value;
                persistWidgetState();
              },
            })
          : null,
      ),
    ),
    el(
      "details",
      {
        className: "fw-advanced-controls",
        open: state.ui.merchAdvancedOpen ? "true" : null,
        onToggle: (event) => {
          state.ui.merchAdvancedOpen = event.currentTarget.open;
          persistWidgetState();
          render();
        },
      },
      el("summary", { className: "fw-advanced-summary", text: "Advanced Merch Controls" }),
      el(
        "div",
        { className: "fw-grid merch-filters fw-merch-clean-filters fw-merch-advanced-grid" },
        el(
          "div",
          { className: "fw-field fw-span-full" },
          el("label", { className: "fw-label", text: "Context (Optional)" }),
          questionInput,
          el("p", {
            className: "fw-empty fw-merch-question-hint",
            text: "Use this for nuance not captured by filters. It guides the analysis on refresh and never overrides selected controls.",
          }),
        ),
        el("div", { className: "fw-field" }, el("label", { className: "fw-label", text: "Compare Mode" }), compareModeSelect),
        el("div", { className: "fw-field" }, el("label", { className: "fw-label", text: "Peer Mode" }), peerModeSelect),
        el(
          "div",
          { className: "fw-field" },
          el("label", { className: "fw-label", text: "Compare Stores" }),
          compareStoreMultiSelect,
        ),
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
              markFilterChange();
              state.ui.topK = event.target.value;
              persistWidgetState();
            },
          }),
        ),
      ),
    ),
    el(
      "div",
      { className: "fw-merch-nav" },
      el(
        "div",
        {
          className: "fw-tabs fw-merch-tabs",
          role: "tablist",
          "aria-label": "Merchandising views",
          "aria-orientation": "horizontal",
        },
        ...tabButtons,
      ),
      el(
        "div",
        { className: "fw-toolbar fw-toolbar-merch" },
        el(
          "button",
          {
            className: `fw-button ${state.runtime.filtersDirty ? "" : "secondary"}`,
            type: "button",
            disabled: state.ui.isLoading ? "true" : null,
            onClick: () => {
              void refreshActiveView();
            },
          },
          state.ui.isLoading ? "Refreshing..." : state.runtime.filtersDirty ? "Refresh Results" : "Refresh",
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
    ),
    state.runtime.filtersDirty
      ? el("p", { className: "fw-empty fw-merch-refresh-hint", text: "Filters changed. Click Refresh Results to rerun the active tab." })
      : null,
    activeTool === "fashion_merch_action_recommendations"
      ? el(
          "p",
          {
            className: "fw-empty",
            text: `Feature: ${state.payload.uiHints.actionDefinitions.feature} Featured Campaign: ${state.payload.uiHints.actionDefinitions.promote} Deprioritize: ${state.payload.uiHints.actionDefinitions.deprioritize} All filters (brand, category, price band, occasion, and lookback) apply to all three sections.`,
          },
        )
      : null,
  );

  const contextPanel = el(
    "section",
    { className: "fw-panel", id: "fw-merch-view-panel", role: "tabpanel", "aria-labelledby": activeTabId },
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
