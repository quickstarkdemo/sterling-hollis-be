const root = document.getElementById("fashion-widget-root");
const meta = window.__FASHION_WIDGET__ || {};

const TOOL_BY_VIEW = {
  executive_overview: "fashion_unified_overview",
  inventory: "fashion_unified_inventory_view",
  recommendations: "fashion_unified_action_recommendations",
  mix_analysis: "fashion_unified_product_mix_recommendations",
};

const TOOL_TO_VIEW = {
  fashion_unified_overview: "executive_overview",
  fashion_unified_inventory_view: "inventory",
  fashion_unified_action_recommendations: "recommendations",
  fashion_unified_product_mix_recommendations: "mix_analysis",
};
const INVENTORY_VIEW_LIMIT = 500;

const DEFAULT_PAYLOAD = {
  filters: {
    store_ids: [],
    active_store_id: null,
    lookback_days: 90,
    category: null,
    brands: [],
    occasions: [],
    price_band: null,
    objective: "revenue",
    top_k: 12,
    inventory_scope: "combined",
    future_window_days: 120,
    row_mode: "store_product",
    override_scope: "store",
    question: null,
  },
  active_view: "executive_overview",
  initial_result: null,
  last_result: null,
  last_tool: "fashion_unified_overview",
  initial_notice: null,
  uiHints: {
    emptyState: "Run a tab to load unified workspace data.",
    categoryOptions: [],
    brandOptions: [],
    occasionOptions: [],
    storeOptions: [],
    features: {
      execAutoOptimizeEnabled: false,
      strategyPacketEnabled: false,
      merchStrategyContextEnabled: false,
    },
  },
};

const state = {
  payload: clone(DEFAULT_PAYLOAD),
  ui: {
    activeView: "executive_overview",
    selectedStoreIds: [],
    activeStoreId: "",
    storeSearch: "",
    lookbackDays: "90",
    category: "",
    selectedBrands: [],
    selectedOccasions: [],
    priceBand: "",
    objective: "revenue",
    topK: "12",
    inventoryScope: "combined",
    futureWindowDays: "120",
    rowMode: "store_product",
    overrideScope: "store",
    question: "",
    notice: "",
    noticeTone: "info",
    isLoading: false,
    isExporting: false,
    filtersDirty: false,
    csvText: "",
    inventoryPage: 1,
    inventoryPageSize: "50",
  },
  data: {
    executive_overview: null,
    inventory: null,
    recommendations: null,
    mix_analysis: null,
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
  return Math.max(min, Math.min(max, Math.round(parsed)));
}

function normalizeStoreIds(values) {
  if (!Array.isArray(values)) {
    return [];
  }
  const deduped = [];
  values.forEach((value) => {
    const token = String(value || "").trim();
    if (token && !deduped.includes(token)) {
      deduped.push(token);
    }
  });
  return deduped;
}

function normalizeTokenSelections(values, options = {}) {
  const lowerCase = options.lowerCase !== false;
  const normalizeSpaces = options.normalizeSpaces === true;
  const source = Array.isArray(values) ? values : [];
  const deduped = [];
  const seen = new Set();
  source.forEach((value) => {
    let token = String(value || "").trim();
    if (!token) {
      return;
    }
    if (normalizeSpaces) {
      token = token.replace(/\s+/g, "_");
    }
    if (lowerCase) {
      token = token.toLowerCase();
    }
    if (seen.has(token)) {
      return;
    }
    seen.add(token);
    deduped.push(token);
  });
  return deduped;
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
  return null;
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
  if (!isObject(raw) || !isObject(raw.filters)) {
    return null;
  }

  const filters = raw.filters;
  const uiHints = isObject(raw.uiHints) ? raw.uiHints : isObject(raw.ui_hints) ? raw.ui_hints : {};
  const uiFeatures = isObject(uiHints.features) ? uiHints.features : {};
  return {
    filters: {
      store_ids: normalizeStoreIds(filters.store_ids),
      active_store_id: typeof filters.active_store_id === "string" ? filters.active_store_id : null,
      lookback_days: parsePositiveInt(filters.lookback_days, 90, 7, 730),
      category: typeof filters.category === "string" ? filters.category : null,
      brands: normalizeTokenSelections(filters.brands, { lowerCase: false }),
      occasions: normalizeTokenSelections(filters.occasions, { lowerCase: true, normalizeSpaces: true }),
      price_band: typeof filters.price_band === "string" ? filters.price_band : null,
      objective: typeof filters.objective === "string" ? filters.objective : "revenue",
      top_k: parsePositiveInt(filters.top_k, 12, 1, 100),
      inventory_scope: typeof filters.inventory_scope === "string" ? filters.inventory_scope : "combined",
      future_window_days: parsePositiveInt(filters.future_window_days, 120, 1, 365),
      row_mode: typeof filters.row_mode === "string" ? filters.row_mode : "store_product",
      override_scope: typeof filters.override_scope === "string" ? filters.override_scope : "store",
      question: typeof filters.question === "string" ? filters.question : null,
    },
    active_view: typeof raw.active_view === "string" ? raw.active_view : "executive_overview",
    initial_result: isObject(raw.initial_result) ? clone(raw.initial_result) : null,
    last_result: isObject(raw.last_result) ? clone(raw.last_result) : null,
    last_tool: typeof raw.last_tool === "string" ? raw.last_tool : "fashion_unified_overview",
    initial_notice: typeof raw.initial_notice === "string" ? raw.initial_notice : null,
    uiHints: {
      emptyState:
        typeof uiHints.emptyState === "string"
          ? uiHints.emptyState
          : typeof uiHints.empty_state === "string"
            ? uiHints.empty_state
          : DEFAULT_PAYLOAD.uiHints.emptyState,
      categoryOptions: normalizeOptions(uiHints.categoryOptions || uiHints.category_options),
      brandOptions: normalizeOptions(uiHints.brandOptions || uiHints.brand_options),
      occasionOptions: normalizeOptions(uiHints.occasionOptions || uiHints.occasion_options),
      storeOptions: normalizeOptions(uiHints.storeOptions || uiHints.store_options),
      features: {
        execAutoOptimizeEnabled: Boolean(uiFeatures.execAutoOptimizeEnabled || uiFeatures.exec_auto_optimize_enabled),
        strategyPacketEnabled: Boolean(uiFeatures.strategyPacketEnabled || uiFeatures.strategy_packet_enabled),
        merchStrategyContextEnabled: Boolean(
          uiFeatures.merchStrategyContextEnabled || uiFeatures.merch_strategy_context_enabled,
        ),
      },
    },
  };
}

function applyWorkspacePayload(rawInput) {
  const payload = normalizeWorkspacePayload(rawInput);
  if (!payload) {
    return false;
  }
  state.payload = payload;
  state.ui.activeView = payload.active_view || TOOL_TO_VIEW[payload.last_tool] || "executive_overview";
  state.ui.selectedStoreIds = normalizeStoreIds(payload.filters.store_ids);
  state.ui.activeStoreId = payload.filters.active_store_id || state.ui.selectedStoreIds[0] || "";
  state.ui.storeSearch = "";
  state.ui.lookbackDays = String(payload.filters.lookback_days || 90);
  state.ui.category = payload.filters.category || "";
  state.ui.selectedBrands = normalizeTokenSelections(payload.filters.brands, { lowerCase: false });
  state.ui.selectedOccasions = normalizeTokenSelections(payload.filters.occasions, { lowerCase: true, normalizeSpaces: true });
  state.ui.priceBand = payload.filters.price_band || "";
  state.ui.objective = payload.filters.objective || "revenue";
  state.ui.topK = String(payload.filters.top_k || 12);
  state.ui.inventoryScope = payload.filters.inventory_scope || "combined";
  state.ui.futureWindowDays = String(payload.filters.future_window_days || 120);
  state.ui.rowMode = payload.filters.row_mode || "store_product";
  state.ui.overrideScope = payload.filters.override_scope || "store";
  state.ui.question = payload.filters.question || "";
  state.ui.inventoryPage = 1;
  state.ui.filtersDirty = false;

  const initial = payload.last_result || payload.initial_result;
  const initialView = TOOL_TO_VIEW[payload.last_tool] || payload.active_view || "executive_overview";
  if (initial && state.data[initialView] === null) {
    state.data[initialView] = clone(initial);
  }
  if (payload.initial_notice) {
    setNotice(payload.initial_notice, "info");
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

function humanizeToken(value) {
  const raw = String(value || "").trim();
  if (!raw) {
    return "-";
  }
  const aliases = {
    executive_overview: "Executive Overview",
    mix_analysis: "Mix Analysis",
    store_product: "Store Product",
    peer_and_prior_period: "Peer + Prior Period",
    state_and_profile: "State + Profile",
    under_250: "Under $250",
    "250_500": "$250-$500",
    "500_1000": "$500-$1000",
    "1000_plus": "$1000+",
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

function normalizeSelectionValues(values) {
  if (!Array.isArray(values)) {
    return [];
  }
  const deduped = [];
  const seen = new Set();
  values.forEach((value) => {
    const token = String(value || "").trim();
    const key = token.toLowerCase();
    if (!token || seen.has(key)) {
      return;
    }
    seen.add(key);
    deduped.push(token);
  });
  return deduped;
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
  autoApply = false,
  onDone = null,
}) {
  const normalizedOptions = normalizeOptions(options);
  const selected = new Set(normalizeSelectionValues(selectedValues));
  const details = el("details", { className: "fw-multi-select" });
  const optionsById = new Map(normalizedOptions.map((item) => [item.value, item.label]));
  const summary = el("summary", {
    className: "fw-input fw-multi-select-summary",
    text: multiSelectSummary(Array.from(selected), optionsById, labels),
  });
  const list = el("div", { className: "fw-multi-select-list" });
  const commitSelection = () => {
    const nextValues = normalizeSelectionValues(Array.from(selected));
    summary.textContent = multiSelectSummary(nextValues, optionsById, labels);
    onApply(nextValues);
  };

  if (!normalizedOptions.length) {
    list.appendChild(el("p", { className: "fw-empty", text: noOptionsText }));
  } else {
    normalizedOptions.forEach((option) => {
      const checkbox = el("input", { type: "checkbox", checked: selected.has(option.value) ? "true" : null });
      const label = el("label", { className: "fw-multi-select-option" }, checkbox, el("span", { text: option.label }));
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) {
          selected.add(option.value);
        } else {
          selected.delete(option.value);
        }
        if (autoApply) {
          commitSelection();
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
          if (autoApply) {
            commitSelection();
          }
        },
      },
      "Clear",
    ),
    autoApply
      ? el(
          "button",
          {
            className: "fw-button secondary",
            type: "button",
            onClick: () => {
              details.open = false;
              if (typeof onDone === "function") {
                onDone();
              }
            },
          },
          "Done",
        )
      : el(
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

function markFiltersDirty() {
  state.ui.filtersDirty = true;
}

function buildSelectControl(currentValue, options, onChange) {
  return el(
    "select",
    {
      className: "fw-input fw-select",
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
}

function buildStoreMultiSelect(selectedStoreValues, options, noOptionsText = "No stores available.") {
  return buildCheckMultiSelect({
    selectedValues: normalizeStoreIds(selectedStoreValues),
    options,
    labels: { all: "All stores", singular: "store", plural: "stores" },
    noOptionsText,
    autoApply: true,
    onDone: () => {
      render();
    },
    onApply: (nextValues) => {
      state.ui.selectedStoreIds = normalizeStoreIds(nextValues);
      if (!state.ui.selectedStoreIds.includes(state.ui.activeStoreId)) {
        state.ui.activeStoreId = state.ui.selectedStoreIds[0] || "";
      }
      markFiltersDirty();
    },
  });
}

function deriveFallbackStoreOptions() {
  const byId = new Map();
  const addOption = (value, label) => {
    const token = String(value || "").trim();
    if (!token || byId.has(token)) {
      return;
    }
    const resolvedLabel = String(label || token).trim() || token;
    byId.set(token, { value: token, label: resolvedLabel });
  };

  const overview = state.data.executive_overview;
  if (isObject(overview) && Array.isArray(overview.stores)) {
    overview.stores.forEach((row) => {
      if (!isObject(row)) {
        return;
      }
      const storeId = String(row.store_id || "").trim();
      const storeName = String(row.store_name || storeId).trim();
      const city = String(row.city || "").trim();
      const region = String(row.state || "").trim();
      const decoratedLabel = city && region ? `${storeName} (${city}, ${region})` : storeName;
      addOption(storeId, decoratedLabel);
    });
  }

  ["inventory", "recommendations", "mix_analysis"].forEach((view) => {
    const payload = state.data[view];
    if (!isObject(payload) || !Array.isArray(payload.rows)) {
      return;
    }
    payload.rows.forEach((row) => {
      if (!isObject(row)) {
        return;
      }
      const storeId = String(row.store_id || "").trim();
      const storeName = String(row.store_name || storeId).trim();
      addOption(storeId, storeName || storeId);
    });
  });

  normalizeStoreIds(state.ui.selectedStoreIds).forEach((storeId) => addOption(storeId, storeId));
  normalizeStoreIds(state.payload?.filters?.store_ids).forEach((storeId) => addOption(storeId, storeId));
  return Array.from(byId.values());
}

function buildBrandsMultiSelect(selectedValues, options) {
  return buildCheckMultiSelect({
    selectedValues: normalizeTokenSelections(selectedValues, { lowerCase: false }),
    options,
    labels: { all: "All brands", singular: "brand", plural: "brands" },
    noOptionsText: "No brands available.",
    onApply: (nextValues) => {
      state.ui.selectedBrands = normalizeTokenSelections(nextValues, { lowerCase: false });
      markFiltersDirty();
      render();
    },
  });
}

function buildOccasionsMultiSelect(selectedValues, options) {
  return buildCheckMultiSelect({
    selectedValues: normalizeTokenSelections(selectedValues, { lowerCase: true, normalizeSpaces: true }),
    options,
    labels: { all: "All occasions", singular: "occasion", plural: "occasions" },
    noOptionsText: "No occasions available.",
    onApply: (nextValues) => {
      state.ui.selectedOccasions = normalizeTokenSelections(nextValues, { lowerCase: true, normalizeSpaces: true });
      markFiltersDirty();
      render();
    },
  });
}

function currentViewResult() {
  return state.data[state.ui.activeView] || null;
}

function serializeOverrideKey(productId, storeId) {
  if (state.ui.overrideScope === "global") {
    return `global::${productId}`;
  }
  return `${String(storeId || "")}::${productId}`;
}

function recommendationOverridesPayload() {
  return Object.values(state.recommendationOverrides)
    .filter((entry) => isObject(entry) && entry.product_id)
    .map((entry) => ({
      product_id: entry.product_id,
      store_id: entry.store_id || undefined,
      final_action: entry.final_action,
      priority_tier: entry.priority_tier,
      override_note: entry.override_note || undefined,
    }));
}

function buildCommonArgs() {
  const args = {
    store_ids: normalizeStoreIds(state.ui.selectedStoreIds),
    active_store_id: state.ui.activeStoreId || undefined,
    lookback_days: parsePositiveInt(state.ui.lookbackDays, 90, 7, 730),
    category: state.ui.category.trim() || undefined,
    brands: normalizeTokenSelections(state.ui.selectedBrands, { lowerCase: false }),
    occasions: normalizeTokenSelections(state.ui.selectedOccasions, { lowerCase: true, normalizeSpaces: true }),
    price_band: state.ui.priceBand || undefined,
  };
  return Object.fromEntries(Object.entries(args).filter(([, value]) => {
    if (Array.isArray(value)) {
      return value.length > 0;
    }
    return value !== undefined && value !== "";
  }));
}

function buildViewArgs(view) {
  const common = buildCommonArgs();
  if (view === "executive_overview") {
    return {
      ...common,
      objective: state.ui.objective || "revenue",
      top_k_stores: Math.max(1, Math.min(50, state.ui.selectedStoreIds.length || 12)),
    };
  }
  if (view === "inventory") {
    return {
      ...common,
      row_mode: state.ui.rowMode || "store_product",
      inventory_scope: state.ui.inventoryScope || "combined",
      future_window_days: parsePositiveInt(state.ui.futureWindowDays, 120, 1, 365),
      limit: INVENTORY_VIEW_LIMIT,
    };
  }
  if (view === "recommendations") {
    return {
      ...common,
      question: state.ui.question.trim() || undefined,
      objective: state.ui.objective || "margin",
      top_k: parsePositiveInt(state.ui.topK, 12, 1, 100),
      row_mode: state.ui.rowMode || "store_product",
      override_scope: state.ui.overrideScope || "store",
      recommendation_overrides: recommendationOverridesPayload(),
    };
  }
  return {
    ...common,
    top_k: parsePositiveInt(state.ui.topK, 12, 1, 100),
    row_mode: state.ui.rowMode || "store_product",
    override_scope: state.ui.overrideScope || "store",
    inventory_scope: state.ui.inventoryScope || "combined",
    future_window_days: parsePositiveInt(state.ui.futureWindowDays, 120, 1, 365),
    limit: INVENTORY_VIEW_LIMIT,
    recommendation_overrides: recommendationOverridesPayload(),
  };
}

function buildExportArgs() {
  const common = buildCommonArgs();
  return {
    ...common,
    view: state.ui.activeView,
    question: state.ui.question.trim() || undefined,
    objective: state.ui.objective || "revenue",
    top_k: parsePositiveInt(state.ui.topK, 12, 1, 100),
    top_k_stores: Math.max(1, Math.min(50, state.ui.selectedStoreIds.length || 12)),
    row_mode: state.ui.rowMode || "store_product",
    override_scope: state.ui.overrideScope || "store",
    inventory_scope: state.ui.inventoryScope || "combined",
    future_window_days: parsePositiveInt(state.ui.futureWindowDays, 120, 1, 365),
    recommendation_overrides: recommendationOverridesPayload(),
  };
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
      // fallback below
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

async function loadView(view, options = {}) {
  const toolName = TOOL_BY_VIEW[view];
  if (!toolName) {
    return;
  }
  state.ui.activeView = view;
  state.ui.isLoading = true;
  if (!options.silentNotice) {
    setNotice(`Loading ${humanizeToken(view)}...`, "info");
  }
  render();

  const result = await callTool(toolName, buildViewArgs(view));
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
  if (view === "inventory") {
    state.ui.inventoryPage = 1;
  }
  state.payload.last_tool = toolName;
  state.payload.last_result = clone(payload);
  state.ui.filtersDirty = false;
  if (view === "recommendations") {
    applyDefaultRecommendationOverrides(payload.recommendations);
  }
  setNotice(`${humanizeToken(view)} loaded.`, "info");
  render();
}

async function exportCsv() {
  state.ui.isExporting = true;
  setNotice("Building CSV export...", "info");
  render();

  const result = await callTool("fashion_unified_export_csv", buildExportArgs());
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
  if (!Array.isArray(rows)) {
    return;
  }
  const total = rows.length;
  rows.forEach((row, index) => {
    if (!isObject(row) || !row.product_id) {
      return;
    }
    const key = serializeOverrideKey(row.product_id, row.store_id || "");
    if (state.recommendationOverrides[key]) {
      return;
    }
    state.recommendationOverrides[key] = {
      product_id: row.product_id,
      store_id: state.ui.overrideScope === "store" ? row.store_id || undefined : undefined,
      final_action: row.final_action || row.model_action || "feature",
      priority_tier: row.final_priority_tier || derivePriorityTier(index, total),
      override_note: row.override_note || "",
    };
  });
}

function updateRecommendationOverride(row, patch) {
  const productId = String(row?.product_id || "").trim();
  if (!productId) {
    return;
  }
  const storeId = row?.store_id ? String(row.store_id) : "";
  const key = serializeOverrideKey(productId, storeId);
  const existing = state.recommendationOverrides[key] || {
    product_id: productId,
    store_id: state.ui.overrideScope === "store" ? storeId || undefined : undefined,
    final_action: row?.final_action || row?.model_action || "feature",
    priority_tier: row?.final_priority_tier || "medium",
    override_note: "",
  };
  state.recommendationOverrides[key] = {
    ...existing,
    ...patch,
    product_id: productId,
    store_id: state.ui.overrideScope === "store" ? storeId || undefined : undefined,
  };
  state.ui.filtersDirty = true;
}

function renderNotice() {
  if (!state.ui.notice) {
    return null;
  }
  return el("div", { className: `fw-notice ${state.ui.noticeTone === "error" ? "error" : ""}`, text: state.ui.notice });
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
  const hintedStoreOptions = normalizeOptions(state.payload.uiHints.storeOptions);
  const storeOptions = hintedStoreOptions.length ? hintedStoreOptions : deriveFallbackStoreOptions();
  const storeSearchToken = String(state.ui.storeSearch || "").trim().toLowerCase();
  const filteredStoreOptions = storeSearchToken
    ? storeOptions.filter((item) => {
        const label = String(item.label || "").toLowerCase();
        const value = String(item.value || "").toLowerCase();
        return label.includes(storeSearchToken) || value.includes(storeSearchToken);
      })
    : storeOptions;
  const visibleStoreOptions = filteredStoreOptions;
  const storeNoOptionsText = storeSearchToken && storeOptions.length
    ? `No stores match "${state.ui.storeSearch.trim()}".`
    : "No stores available.";
  const categoryOptions = [{ value: "", label: "Any" }, ...normalizeOptions(state.payload.uiHints.categoryOptions)];
  const selectedStoreSummary = state.ui.selectedStoreIds.length
    ? `${state.ui.selectedStoreIds.length} stores selected`
    : "All stores selected";

  const activeStoreUniverse = state.ui.selectedStoreIds.length
    ? storeOptions.filter((item) => state.ui.selectedStoreIds.includes(item.value))
    : storeOptions;
  const activeStoreOptions = [{ value: "", label: "Auto" }, ...activeStoreUniverse];

  return el(
    "section",
    { className: "fw-panel fw-controls-panel" },
    el("h2", { className: "fw-panel-title", text: "Unified Workspace" }),
    renderNotice(),
    el("p", { className: "fw-empty", text: selectedStoreSummary }),
    el(
      "div",
      { className: "fw-grid merch-filters fw-merch-clean-filters" },
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Store Search" }),
        el("input", {
          className: "fw-input",
          type: "text",
          placeholder: "Search stores, city, state, or id",
          value: state.ui.storeSearch,
          onInput: (event) => {
            state.ui.storeSearch = event.target.value;
            render();
          },
        }),
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Stores" }),
        buildStoreMultiSelect(state.ui.selectedStoreIds, visibleStoreOptions, storeNoOptionsText),
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Active Store" }),
        buildSelectControl(state.ui.activeStoreId || "", activeStoreOptions.length ? activeStoreOptions : [{ value: "", label: "Auto" }], (value) => {
          state.ui.activeStoreId = value;
          markFiltersDirty();
        }),
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
        el("label", { className: "fw-label", text: "Category" }),
        buildSelectControl(state.ui.category, categoryOptions, (value) => {
          state.ui.category = value;
          markFiltersDirty();
        }),
      ),
      el("div", { className: "fw-field" }, el("label", { className: "fw-label", text: "Brand (Multi)" }), buildBrandsMultiSelect(state.ui.selectedBrands, state.payload.uiHints.brandOptions)),
      el("div", { className: "fw-field" }, el("label", { className: "fw-label", text: "Occasion (Multi)" }), buildOccasionsMultiSelect(state.ui.selectedOccasions, state.payload.uiHints.occasionOptions)),
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
        el("label", { className: "fw-label", text: "Objective" }),
        buildSelectControl(
          state.ui.objective,
          [
            { value: "revenue", label: "Revenue" },
            { value: "margin", label: "Margin" },
            { value: "sell_through", label: "Sell Through" },
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
        el("label", { className: "fw-label", text: "Row Mode" }),
        buildSelectControl(
          state.ui.rowMode,
          [
            { value: "store_product", label: "Store Product" },
            { value: "aggregated", label: "Aggregated" },
          ],
          (value) => {
            state.ui.rowMode = value;
            markFiltersDirty();
          },
        ),
      ),
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Override Scope" }),
        buildSelectControl(
          state.ui.overrideScope,
          [
            { value: "store", label: "Store" },
            { value: "global", label: "Global" },
          ],
          (value) => {
            state.ui.overrideScope = value;
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
        { className: "fw-field fw-span-full" },
        el("label", { className: "fw-label", text: "Context (Optional)" }),
        el("input", {
          className: "fw-input",
          type: "text",
          value: state.ui.question,
          placeholder: "Optional context for recommendations and mix analysis",
          onInput: (event) => {
            state.ui.question = event.target.value;
            markFiltersDirty();
          },
        }),
      ),
    ),
  );
}

function renderTabs() {
  const tabs = [
    { id: "executive_overview", label: "Executive Overview" },
    { id: "inventory", label: "Inventory" },
    { id: "recommendations", label: "Recommendations" },
    { id: "mix_analysis", label: "Mix Analysis" },
  ];
  return el(
    "div",
    { className: "fw-merch-nav" },
    el(
      "div",
      { className: "fw-tabs fw-merch-tabs", role: "tablist", "aria-label": "Unified views" },
      ...tabs.map((tab) =>
        el(
          "button",
          {
            id: `fw-unified-tab-${tab.id}`,
            className: `fw-tab fw-merch-tab ${state.ui.activeView === tab.id ? "active" : ""}`,
            type: "button",
            role: "tab",
            "aria-selected": state.ui.activeView === tab.id ? "true" : "false",
            disabled: state.ui.isLoading ? "true" : null,
            onClick: () => {
              void loadView(tab.id);
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
            void loadView(state.ui.activeView);
          },
        },
        state.ui.isLoading ? "Refreshing..." : state.ui.filtersDirty ? "Refresh Results" : "Refresh",
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

function renderOverviewResult() {
  const result = state.data.executive_overview;
  if (!isObject(result) || !Array.isArray(result.stores)) {
    return el("p", { className: "fw-empty", text: state.payload.uiHints.emptyState });
  }

  const table = el(
    "table",
    { className: "fw-table" },
    el(
      "thead",
      {},
      el("tr", {}, el("th", { text: "Rank" }), el("th", { text: "Store" }), el("th", { text: "Revenue" }), el("th", { text: "Units" }), el("th", { text: "Margin" }), el("th", { text: "Delta" })),
    ),
    el(
      "tbody",
      {},
      ...result.stores.map((row) =>
        el(
          "tr",
          {},
          el("td", { text: formatNumber(row.rank, 0) }),
          el("td", { text: `${row.store_name} (${row.city}, ${row.state})` }),
          el("td", { text: formatCurrency(row.revenue) }),
          el("td", { text: formatNumber(row.units, 0) }),
          el("td", { text: `${formatNumber(Number(row.margin_rate || 0) * 100, 1)}%` }),
          el("td", { text: row.revenue_delta_pct === null || row.revenue_delta_pct === undefined ? "-" : `${formatNumber(row.revenue_delta_pct, 1)}%` }),
        ),
      ),
    ),
  );

  return el(
    "section",
    { className: "fw-panel" },
    el("h2", { className: "fw-panel-title", text: "Executive Overview" }),
    el(
      "div",
      { className: "fw-kpi-strip" },
      kpi("Revenue", formatCurrency(result.total_revenue)),
      kpi("Units", formatNumber(result.total_units, 0)),
      kpi("Margin", `${formatNumber(Number(result.margin_rate || 0) * 100, 1)}%`),
      kpi("Stores", formatNumber(result.store_count, 0)),
    ),
    result.summary ? el("p", { className: "fw-empty", text: result.summary }) : null,
    table,
  );
}

function renderInventoryResult() {
  const result = state.data.inventory;
  if (!isObject(result) || !Array.isArray(result.rows)) {
    return el("p", { className: "fw-empty", text: "Run Inventory to view filtered rows." });
  }
  const showStore = state.ui.rowMode === "store_product";
  const pageSize = parsePositiveInt(state.ui.inventoryPageSize, 50, 10, 500);
  const totalRows = result.rows.length;
  const totalPages = Math.max(1, Math.ceil(totalRows / pageSize));
  const currentPage = Math.max(1, Math.min(parsePositiveInt(state.ui.inventoryPage, 1, 1, 10000), totalPages));
  if (currentPage !== state.ui.inventoryPage) {
    state.ui.inventoryPage = currentPage;
  }
  const start = (currentPage - 1) * pageSize;
  const end = Math.min(start + pageSize, totalRows);
  const pageRows = result.rows.slice(start, end);

  const table = el(
    "table",
    { className: "fw-table" },
    el(
      "thead",
      {},
      el(
        "tr",
        {},
        showStore ? el("th", { text: "Store" }) : el("th", { text: "Stores" }),
        el("th", { text: "Type" }),
        el("th", { text: "Title" }),
        el("th", { text: "Brand" }),
        el("th", { text: "Category" }),
        el("th", { text: "Availability" }),
        el("th", { text: "Qty" }),
        el("th", { text: "Available On" }),
        el("th", { text: "Revenue" }),
        el("th", { text: "Units" }),
      ),
    ),
    el(
      "tbody",
      {},
      ...pageRows.map((row) =>
        el(
          "tr",
          {},
          showStore
            ? el("td", { text: row.store_name || row.store_id || "-" })
            : el("td", { text: formatNumber(row.store_count || 1, 0) }),
          el("td", { text: humanizeToken(row.row_type) }),
          el("td", { text: row.title || "-" }),
          el("td", { text: row.brand || "-" }),
          el("td", { text: humanizeToken(row.category) }),
          el("td", { text: row.availability || row.offer_status || "-" }),
          el("td", { text: formatNumber(row.inventory_qty || 0, 0) }),
          el("td", { text: row.available_on ? formatDate(row.available_on) : "-" }),
          el("td", { text: formatCurrency(row.perf_revenue) }),
          el("td", { text: formatNumber(row.perf_units || 0, 1) }),
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
      kpi("Row Mode", humanizeToken(result.row_mode || state.ui.rowMode)),
    ),
    el(
      "div",
      { className: "fw-toolbar" },
      el(
        "div",
        { className: "fw-field" },
        el("label", { className: "fw-label", text: "Rows / Page" }),
        buildSelectControl(
          state.ui.inventoryPageSize,
          [
            { value: "25", label: "25" },
            { value: "50", label: "50" },
            { value: "100", label: "100" },
            { value: "200", label: "200" },
          ],
          (value) => {
            state.ui.inventoryPageSize = value;
            state.ui.inventoryPage = 1;
            render();
          },
        ),
      ),
      el("span", {
        className: "fw-empty",
        text: totalRows ? `Showing ${start + 1}-${end} of ${formatNumber(totalRows, 0)}` : "Showing 0 rows",
      }),
      el(
        "button",
        {
          className: "fw-button secondary",
          type: "button",
          disabled: currentPage <= 1 ? "true" : null,
          onClick: () => {
            state.ui.inventoryPage = Math.max(1, currentPage - 1);
            render();
          },
        },
        "Prev",
      ),
      el(
        "span",
        {
          className: "fw-empty",
          text: `Page ${formatNumber(currentPage, 0)} of ${formatNumber(totalPages, 0)}`,
        },
      ),
      el(
        "button",
        {
          className: "fw-button secondary",
          type: "button",
          disabled: currentPage >= totalPages ? "true" : null,
          onClick: () => {
            state.ui.inventoryPage = Math.min(totalPages, currentPage + 1);
            render();
          },
        },
        "Next",
      ),
    ),
    result.summary ? el("p", { className: "fw-empty", text: result.summary }) : null,
    table,
  );
}

function renderRecommendationsResult() {
  const result = state.data.recommendations;
  if (!isObject(result) || !Array.isArray(result.recommendations)) {
    return el("p", { className: "fw-empty", text: "Run Recommendations to evaluate actions and overrides." });
  }
  applyDefaultRecommendationOverrides(result.recommendations);
  const rows = result.recommendations;
  const showStore = state.ui.rowMode === "store_product";

  const table = el(
    "table",
    { className: "fw-table" },
    el(
      "thead",
      {},
      el(
        "tr",
        {},
        showStore ? el("th", { text: "Store" }) : el("th", { text: "Stores" }),
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
      ...rows.map((row, index) => {
        const key = serializeOverrideKey(row.product_id, row.store_id || "");
        const override = state.recommendationOverrides[key] || {
          product_id: row.product_id,
          store_id: state.ui.overrideScope === "store" ? row.store_id || undefined : undefined,
          final_action: row.final_action || row.model_action || "feature",
          priority_tier: row.final_priority_tier || derivePriorityTier(index, rows.length),
          override_note: "",
        };

        return el(
          "tr",
          {},
          showStore
            ? el("td", { text: row.store_name || row.store_id || "-" })
            : el("td", { text: formatNumber(row.store_count || 1, 0) }),
          el(
            "td",
            {},
            el("strong", { text: row.title || row.product_id }),
            el("div", { className: "fw-empty", text: `${row.brand || "-"} · ${humanizeToken(row.category)}` }),
          ),
          el("td", { text: row.model_action || "-" }),
          el("td", { text: formatNumber(row.metric_value, 2) }),
          el(
            "td",
            {},
            buildSelectControl(
              override.final_action,
              [
                { value: "feature", label: "feature" },
                { value: "promote", label: "promote" },
                { value: "deprioritize", label: "deprioritize" },
                { value: "drop", label: "drop" },
              ],
              (value) => {
                updateRecommendationOverride(row, { final_action: value });
              },
            ),
          ),
          el(
            "td",
            {},
            buildSelectControl(
              override.priority_tier,
              [
                { value: "high", label: "high" },
                { value: "medium", label: "medium" },
                { value: "low", label: "low" },
              ],
              (value) => {
                updateRecommendationOverride(row, { priority_tier: value });
              },
            ),
          ),
          el(
            "td",
            {},
            el("input", {
              className: "fw-input",
              type: "text",
              value: override.override_note || "",
              placeholder: "optional",
              onInput: (event) => {
                updateRecommendationOverride(row, { override_note: event.target.value });
              },
            }),
          ),
        );
      }),
    ),
  );

  return el(
    "section",
    { className: "fw-panel" },
    el("h2", { className: "fw-panel-title", text: "Recommendations" }),
    result.summary ? el("p", { className: "fw-empty", text: result.summary }) : null,
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
        text: `${recommendationOverridesPayload().length} overrides in session`,
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
  const showStore = state.ui.rowMode === "store_product";

  const table = el(
    "table",
    { className: "fw-table" },
    el(
      "thead",
      {},
      el(
        "tr",
        {},
        showStore ? el("th", { text: "Store" }) : el("th", { text: "Stores" }),
        el("th", { text: "Action" }),
        el("th", { text: "Fit" }),
        el("th", { text: "Impact" }),
        el("th", { text: "Current Product" }),
        el("th", { text: "Offer" }),
        el("th", { text: "Rationale" }),
      ),
    ),
    el(
      "tbody",
      {},
      ...result.rows.map((row) =>
        el(
          "tr",
          {},
          showStore
            ? el("td", { text: row.store_name || row.store_id || "-" })
            : el("td", { text: formatNumber(row.store_count || 1, 0) }),
          el("td", { text: humanizeToken(row.action) }),
          el("td", { text: formatNumber(row.fit_score, 2) }),
          el("td", { text: formatNumber(row.expected_mix_impact, 2) }),
          el("td", { text: row.current_title || "-" }),
          el("td", { text: row.offer_title || "-" }),
          el("td", { text: row.rationale || "-" }),
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
      kpi("Row Mode", humanizeToken(result.row_mode || state.ui.rowMode)),
      kpi("Scope", humanizeToken(result.inventory_scope || state.ui.inventoryScope)),
      kpi("Top K", formatNumber(result.top_k || state.ui.topK, 0)),
    ),
    table,
  );
}

function renderActiveResult() {
  if (state.ui.activeView === "inventory") {
    return renderInventoryResult();
  }
  if (state.ui.activeView === "recommendations") {
    return renderRecommendationsResult();
  }
  if (state.ui.activeView === "mix_analysis") {
    return renderMixResult();
  }
  return renderOverviewResult();
}

function renderCsvPanel() {
  if (!state.ui.csvText) {
    return null;
  }
  return el(
    "section",
    { className: "fw-panel" },
    el("h2", { className: "fw-panel-title", text: "CSV Export" }),
    el("p", { className: "fw-empty", text: "CSV rows match the active tab + current filters + row mode + override scope." }),
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
      el("h1", { className: "fw-title", text: meta.title || "Unified Workspace" }),
      buildLabel ? el("span", { className: "fw-version", text: buildLabel }) : null,
    ),
    el(
      "p",
      {
        className: "fw-subtitle",
        text:
          meta.summary ||
          "Unified executive + merchandising workspace with shared filters, multi-store row modes, and deterministic CSV export parity.",
      },
    ),
  );

  container.appendChild(
    el(
      "div",
      { className: "fw-root" },
      header,
      renderControlsPanel(),
      renderTabs(),
      renderActiveResult(),
      renderCsvPanel(),
    ),
  );
}

async function boot() {
  const seeded = applyWorkspacePayload(meta.initialPayload);
  if (!seeded && !applyWorkspacePayload(window.openai && window.openai.toolOutput)) {
    state.payload = clone(DEFAULT_PAYLOAD);
    setNotice("Open unified workspace to load context.", "info");
  }
  render();
  const view = state.ui.activeView || "executive_overview";
  if (!currentViewResult()) {
    await loadView(view, { silentNotice: true });
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
