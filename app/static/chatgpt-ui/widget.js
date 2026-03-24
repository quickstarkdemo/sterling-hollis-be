const root = document.getElementById("fashion-widget-root");
const meta = window.__FASHION_WIDGET__ || {};
const MODEL_CONTEXT_SUBJECT_MAX_CHARS = 200;
const MODEL_CONTEXT_BODY_MAX_CHARS = 2000;
const MODEL_CONTEXT_UPDATE_DEBOUNCE_MS = 600;
const DRAFT_POLL_INTERVAL_MS = 3500;
const CUSTOMER_VALUE_LOOKBACK_OPTIONS = ["90", "180", "365"];
const CUSTOMER_VALUE_FORECAST_OPTIONS = ["4", "8", "12"];

const state = {
  payload: {
    query: "",
    mode: "idle",
    resolved: null,
    results: [],
    selected_customer_id: null,
    initial_style_constraints: null,
    initial_notice: null,
    initial_email_draft_id: null,
    initial_email_subject: null,
    initial_email_body: null,
    uiHints: {
      searchPlaceholder: "Search by name, email, or phone",
      emptyState: "Type a customer name, email, or phone number and run search.",
    },
  },
  ui: {
    query: "",
    selectedCustomerId: null,
    customerTab: "recommendations",
    resultsExpanded: false,
    occasion: "",
    budgetMax: "",
    emailTo: "",
    emailSubject: "",
    emailBody: "",
    emailDraftId: null,
    styleConstraints: null,
    notice: "",
    noticeTone: "info",
    isSearching: false,
  },
  recommendation: {
    customerId: null,
    response: null,
    error: "",
    isLoading: false,
    isPreparingEmailDraft: false,
    isSendingEmailDraft: false,
    isRefreshingEmailDraft: false,
    isUpdatingEmailDraft: false,
    selectedProductIds: [],
    inventoryByProduct: {},
    seedAttemptedCustomerId: null,
    seedAttemptedDraftCustomerId: null,
  },
  analytics: {
    customerId: null,
    response: null,
    error: "",
    isLoading: false,
    lookbackDays: "180",
    forecastWeeks: "8",
    loadedKey: null,
    requestedKey: null,
    chartCleanupFns: [],
  },
  runtime: {
    toolOutputApplied: false,
    userInteracted: false,
    draftHydrationRequestedForId: null,
    modelContextHash: "",
    modelContextTimer: null,
    draftPollTimer: null,
    draftPollInFlight: false,
    lastDraftFingerprint: "",
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

function parsePositiveInt(value, fallback, min, max) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  const rounded = Math.round(parsed);
  return Math.max(min, Math.min(max, rounded));
}

function registerAnalyticsChartCleanup(cleanup) {
  if (typeof cleanup !== "function") {
    return;
  }
  state.analytics.chartCleanupFns.push(cleanup);
}

function teardownAnalyticsCharts() {
  const cleanups = Array.isArray(state.analytics.chartCleanupFns) ? [...state.analytics.chartCleanupFns] : [];
  state.analytics.chartCleanupFns = [];
  cleanups.forEach((cleanup) => {
    try {
      cleanup();
    } catch {
      // Best-effort cleanup.
    }
  });
}

function normalizeProductIds(raw) {
  if (!Array.isArray(raw)) {
    return [];
  }
  const unique = [];
  for (const value of raw) {
    if (typeof value !== "string") {
      continue;
    }
    const productId = value.trim();
    if (!productId || unique.includes(productId)) {
      continue;
    }
    unique.push(productId);
  }
  return unique;
}

function truncateForModelContext(raw, maxChars) {
  const text = String(raw || "").trim();
  if (!text) {
    return { text: null, truncated: false, length: 0 };
  }
  if (text.length <= maxChars) {
    return { text, truncated: false, length: text.length };
  }
  return { text: `${text.slice(0, maxChars)}...`, truncated: true, length: text.length };
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

function normalizeWorkspacePayload(raw) {
  if (!raw) {
    return null;
  }
  if (isObject(raw.structuredContent)) {
    return normalizeWorkspacePayload(raw.structuredContent);
  }
  if (isObject(raw.payload) && (raw.kind === "customer_search_workspace" || !raw.kind)) {
    return normalizeWorkspacePayload(raw.payload);
  }
  if (!isObject(raw)) {
    return null;
  }
  const hasWorkspaceFields =
    Object.prototype.hasOwnProperty.call(raw, "mode") ||
    Object.prototype.hasOwnProperty.call(raw, "results") ||
    Object.prototype.hasOwnProperty.call(raw, "resolved") ||
    Object.prototype.hasOwnProperty.call(raw, "uiHints");
  if (!hasWorkspaceFields) {
    return null;
  }

  const next = {
    query: typeof raw.query === "string" ? raw.query : "",
    mode: typeof raw.mode === "string" ? raw.mode : "idle",
    resolved: isObject(raw.resolved) ? clone(raw.resolved) : null,
    results: Array.isArray(raw.results)
      ? clone(raw.results)
      : Array.isArray(raw.candidates)
        ? clone(raw.candidates)
        : [],
    selected_customer_id: typeof raw.selected_customer_id === "string" ? raw.selected_customer_id : null,
    initial_style_constraints: normalizeStyleConstraints(raw.initial_style_constraints),
    initial_notice: typeof raw.initial_notice === "string" ? raw.initial_notice : null,
    initial_email_draft_id: typeof raw.initial_email_draft_id === "string" ? raw.initial_email_draft_id : null,
    initial_email_subject: typeof raw.initial_email_subject === "string" ? raw.initial_email_subject : null,
    initial_email_body: typeof raw.initial_email_body === "string" ? raw.initial_email_body : null,
    uiHints: {
      searchPlaceholder:
        raw.uiHints && typeof raw.uiHints.searchPlaceholder === "string"
          ? raw.uiHints.searchPlaceholder
          : "Search by name, email, or phone",
      emptyState:
        raw.uiHints && typeof raw.uiHints.emptyState === "string"
          ? raw.uiHints.emptyState
          : "No customers matched the current query.",
    },
  };

  return next;
}

function applyWorkspacePayload(raw) {
  const payload = normalizeWorkspacePayload(raw);
  if (!payload) {
    return false;
  }
  state.payload = payload;
  state.ui.resultsExpanded = Array.isArray(payload.results) && payload.results.length > 0 && !payload.selected_customer_id;
  if (!state.ui.query && payload.query) {
    state.ui.query = payload.query;
  }
  if (payload.selected_customer_id) {
    state.ui.selectedCustomerId = payload.selected_customer_id;
  }
  if (payload.initial_style_constraints) {
    state.ui.styleConstraints = payload.initial_style_constraints;
  }
  if (payload.initial_notice) {
    setNotice(payload.initial_notice);
  }
  if (payload.initial_email_draft_id) {
    state.ui.emailDraftId = payload.initial_email_draft_id;
    state.runtime.draftHydrationRequestedForId = null;
  }
  if (payload.initial_email_subject) {
    state.ui.emailSubject = payload.initial_email_subject;
  }
  if (payload.initial_email_body) {
    state.ui.emailBody = payload.initial_email_body;
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

function applyUiWidgetState(raw) {
  if (!isObject(raw)) {
    return false;
  }
  let changed = false;
  if (typeof raw.query === "string" && raw.query !== state.ui.query) {
    state.ui.query = raw.query;
    changed = true;
  }
  if (
    typeof raw.selectedCustomerId === "string" &&
    raw.selectedCustomerId !== state.ui.selectedCustomerId
  ) {
    state.ui.selectedCustomerId = raw.selectedCustomerId;
    changed = true;
  }
  if (typeof raw.customerTab === "string") {
    const nextTab = raw.customerTab === "value" ? "value" : "recommendations";
    if (nextTab !== state.ui.customerTab) {
      state.ui.customerTab = nextTab;
      changed = true;
    }
  }
  if (typeof raw.occasion === "string" && raw.occasion !== state.ui.occasion) {
    state.ui.occasion = raw.occasion;
    changed = true;
  }
  if (typeof raw.budgetMax === "string" && raw.budgetMax !== state.ui.budgetMax) {
    state.ui.budgetMax = raw.budgetMax;
    changed = true;
  }
  if (typeof raw.emailTo === "string" && raw.emailTo !== state.ui.emailTo) {
    state.ui.emailTo = raw.emailTo;
    changed = true;
  }
  if (typeof raw.emailSubject === "string" && raw.emailSubject !== state.ui.emailSubject) {
    state.ui.emailSubject = raw.emailSubject;
    changed = true;
  }
  if (typeof raw.emailBody === "string" && raw.emailBody !== state.ui.emailBody) {
    state.ui.emailBody = raw.emailBody;
    changed = true;
  }
  if (typeof raw.emailDraftId === "string" && raw.emailDraftId !== state.ui.emailDraftId) {
    state.ui.emailDraftId = raw.emailDraftId;
    changed = true;
  }
  if (isObject(raw.styleConstraints) || raw.styleConstraints === null) {
    const nextConstraints = normalizeStyleConstraints(raw.styleConstraints);
    if (JSON.stringify(nextConstraints) !== JSON.stringify(state.ui.styleConstraints)) {
      state.ui.styleConstraints = nextConstraints;
      changed = true;
    }
  }
  if (Array.isArray(raw.selectedProductIds)) {
    const next = normalizeProductIds(raw.selectedProductIds);
    if (JSON.stringify(next) !== JSON.stringify(state.recommendation.selectedProductIds)) {
      state.recommendation.selectedProductIds = next;
      changed = true;
    }
  }
  if (typeof raw.customerValueLookbackDays === "string") {
    const nextLookback = String(parsePositiveInt(raw.customerValueLookbackDays, 180, 30, 730));
    if (nextLookback !== state.analytics.lookbackDays) {
      state.analytics.lookbackDays = nextLookback;
      state.analytics.loadedKey = null;
      changed = true;
    }
  }
  if (typeof raw.customerValueForecastWeeks === "string") {
    const nextForecast = String(parsePositiveInt(raw.customerValueForecastWeeks, 8, 1, 26));
    if (nextForecast !== state.analytics.forecastWeeks) {
      state.analytics.forecastWeeks = nextForecast;
      state.analytics.loadedKey = null;
      changed = true;
    }
  }
  return changed;
}

function loadWidgetState() {
  if (!window.openai || !isObject(window.openai.widgetState)) {
    return;
  }
  const widgetState = window.openai.widgetState;
  if (typeof widgetState.query === "string") {
    state.ui.query = widgetState.query;
  }
  if (typeof widgetState.selectedCustomerId === "string") {
    state.ui.selectedCustomerId = widgetState.selectedCustomerId;
  }
  if (typeof widgetState.customerTab === "string") {
    state.ui.customerTab = widgetState.customerTab === "value" ? "value" : "recommendations";
  }
  if (typeof widgetState.occasion === "string") {
    state.ui.occasion = widgetState.occasion;
  }
  if (typeof widgetState.budgetMax === "string") {
    state.ui.budgetMax = widgetState.budgetMax;
  }
  if (typeof widgetState.emailTo === "string") {
    state.ui.emailTo = widgetState.emailTo;
  }
  if (typeof widgetState.emailSubject === "string") {
    state.ui.emailSubject = widgetState.emailSubject;
  }
  if (typeof widgetState.emailBody === "string") {
    state.ui.emailBody = widgetState.emailBody;
  }
  if (typeof widgetState.emailDraftId === "string") {
    state.ui.emailDraftId = widgetState.emailDraftId;
  }
  if (isObject(widgetState.styleConstraints) || widgetState.styleConstraints === null) {
    state.ui.styleConstraints = normalizeStyleConstraints(widgetState.styleConstraints);
  }
  if (Array.isArray(widgetState.selectedProductIds)) {
    state.recommendation.selectedProductIds = normalizeProductIds(widgetState.selectedProductIds);
  }
  if (typeof widgetState.customerValueLookbackDays === "string") {
    state.analytics.lookbackDays = String(parsePositiveInt(widgetState.customerValueLookbackDays, 180, 30, 730));
  }
  if (typeof widgetState.customerValueForecastWeeks === "string") {
    state.analytics.forecastWeeks = String(parsePositiveInt(widgetState.customerValueForecastWeeks, 8, 1, 26));
  }
}

function persistWidgetState() {
  if (!window.openai || typeof window.openai.setWidgetState !== "function") {
    queueModelContextUpdate();
    return;
  }
  try {
    window.openai.setWidgetState({
      query: state.ui.query,
      selectedCustomerId: state.ui.selectedCustomerId,
      customerTab: state.ui.customerTab,
      occasion: state.ui.occasion,
      budgetMax: state.ui.budgetMax,
      emailTo: state.ui.emailTo,
      emailSubject: state.ui.emailSubject,
      emailBody: state.ui.emailBody,
      emailDraftId: state.ui.emailDraftId,
      styleConstraints: state.ui.styleConstraints,
      selectedProductIds: state.recommendation.selectedProductIds,
      customerValueLookbackDays: state.analytics.lookbackDays,
      customerValueForecastWeeks: state.analytics.forecastWeeks,
    });
  } catch {
    // Best-effort only.
  }
  queueModelContextUpdate();
}

function buildModelContextPayload() {
  const results = Array.isArray(state.payload.results) ? state.payload.results : [];
  const selected = selectedCustomer(results);
  const analyticsResponse =
    state.analytics.response && state.analytics.customerId === selected?.id ? state.analytics.response : null;
  const recommendationResponse =
    state.recommendation.response && state.recommendation.customerId === selected?.id ? state.recommendation.response : null;
  const draftSubject = truncateForModelContext(state.ui.emailSubject, MODEL_CONTEXT_SUBJECT_MAX_CHARS);
  const draftBody = truncateForModelContext(state.ui.emailBody, MODEL_CONTEXT_BODY_MAX_CHARS);
  const emailTo = (state.ui.emailTo || selected?.email || "").trim();
  return {
    workspace: "customer_search",
    customer_workspace_tab: state.ui.customerTab === "value" ? "value" : "recommendations",
    selected_customer_id: selected?.id || null,
    selected_customer_name: selected?.full_name || null,
    occasion: state.ui.occasion.trim() || null,
    budget_max: parseBudgetMax(state.ui.budgetMax),
    selected_product_ids: normalizeProductIds(state.recommendation.selectedProductIds),
    style_constraints: normalizeStyleConstraints(state.ui.styleConstraints),
    email_draft_id: state.ui.emailDraftId || null,
    email_draft_to: emailTo || null,
    email_draft_subject: draftSubject.text,
    email_draft_subject_truncated: draftSubject.truncated,
    email_draft_body: draftBody.text,
    email_draft_body_truncated: draftBody.truncated,
    email_draft_body_length: draftBody.length,
    email_draft_body_hash: stableTextHash(state.ui.emailBody),
    customer_value_lookback_days: parsePositiveInt(state.analytics.lookbackDays, 180, 30, 730),
    customer_value_forecast_weeks: parsePositiveInt(state.analytics.forecastWeeks, 8, 1, 26),
    customer_value_score: analyticsResponse?.metrics?.value_score ?? null,
    customer_value_tier: analyticsResponse?.metrics?.value_tier ?? null,
    strategy_packet_id: recommendationResponse?.recommendation?.strategy_packet_id || null,
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

function customerLabel(customer) {
  const email = customer.email || "";
  const phone = customer.phone_e164 || customer.masked_phone || "";
  if (!email && !phone) {
    return "";
  }
  if (email && phone) {
    return `${email} • ${phone}`;
  }
  return email || phone;
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

function kpi(label, value) {
  return el(
    "div",
    { className: "fw-kpi" },
    el("span", { className: "fw-kpi-label", text: label }),
    el("strong", { className: "fw-kpi-value", text: value }),
  );
}

function humanizeToken(value) {
  if (value === null || value === undefined) {
    return "";
  }
  return String(value).replace(/[_-]+/g, " ").trim();
}

function normalizeTextList(raw) {
  if (!Array.isArray(raw)) {
    return [];
  }
  const seen = new Set();
  const values = [];
  for (const item of raw) {
    const token = String(item || "").trim().toLowerCase();
    if (!token || seen.has(token)) {
      continue;
    }
    seen.add(token);
    values.push(token);
  }
  return values;
}

function normalizeStyleConstraints(raw) {
  if (!isObject(raw)) {
    return null;
  }
  const constraintSource =
    typeof raw.constraint_source === "string" && raw.constraint_source.trim()
      ? raw.constraint_source.trim().toLowerCase()
      : null;
  const targetCategories = normalizeTextList(raw.target_categories);
  const excludeCategories = normalizeTextList(raw.exclude_categories);
  const styleKeywords = normalizeTextList(raw.style_keywords);
  const targetGenders = normalizeTextList(raw.target_genders)
    .map((value) => {
      if (["male", "man", "men", "m", "boys", "boy"].includes(value)) {
        return "male";
      }
      if (["female", "woman", "women", "f", "girls", "girl"].includes(value)) {
        return "female";
      }
      if (["unisex", "neutral", "gender_neutral", "gender-neutral"].includes(value)) {
        return "unisex";
      }
      return null;
    })
    .filter(Boolean);

  if (!targetCategories.length && !excludeCategories.length && !targetGenders.length && !styleKeywords.length) {
    return null;
  }
  return {
    constraint_source: constraintSource,
    target_categories: targetCategories,
    exclude_categories: excludeCategories,
    target_genders: targetGenders,
    style_keywords: styleKeywords,
  };
}

function hasStyleConstraints(raw) {
  return normalizeStyleConstraints(raw) !== null;
}

function parseBudgetMax(value) {
  if (!value || !value.trim()) {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function clearAnalyticsState() {
  teardownAnalyticsCharts();
  state.analytics.customerId = null;
  state.analytics.response = null;
  state.analytics.error = "";
  state.analytics.isLoading = false;
  state.analytics.loadedKey = null;
  state.analytics.requestedKey = null;
}

function clearRecommendationState() {
  state.recommendation.customerId = null;
  state.recommendation.response = null;
  state.recommendation.error = "";
  state.recommendation.isPreparingEmailDraft = false;
  state.recommendation.isSendingEmailDraft = false;
  state.recommendation.isRefreshingEmailDraft = false;
  state.recommendation.isUpdatingEmailDraft = false;
  state.recommendation.selectedProductIds = [];
  state.recommendation.inventoryByProduct = {};
  state.recommendation.seedAttemptedCustomerId = null;
  state.recommendation.seedAttemptedDraftCustomerId = null;
  state.runtime.draftHydrationRequestedForId = null;
  state.runtime.lastDraftFingerprint = "";
  stopDraftPolling();
  state.ui.emailDraftId = null;
  state.ui.emailSubject = "";
  state.ui.emailBody = "";
  queueModelContextUpdate({ immediate: true });
}

function selectCustomerId(customerId, options = {}) {
  const collapseResults = options.collapseResults === true;
  const keepStyleConstraints = options.keepStyleConstraints === true;
  markUserInteraction();
  if (customerId !== state.ui.selectedCustomerId) {
    state.ui.selectedCustomerId = customerId;
    state.ui.emailTo = "";
    if (!keepStyleConstraints) {
      state.ui.styleConstraints = null;
    }
    clearRecommendationState();
    clearAnalyticsState();
  }
  if (collapseResults) {
    state.ui.resultsExpanded = false;
  }
  persistWidgetState();
}

function resolveRowSelection(results) {
  if (!results.length) {
    state.ui.selectedCustomerId = null;
    state.ui.resultsExpanded = false;
    clearRecommendationState();
    clearAnalyticsState();
    return;
  }
  if (state.ui.selectedCustomerId && results.some((customer) => customer.id === state.ui.selectedCustomerId)) {
    return;
  }
  selectCustomerId(results[0].id, { collapseResults: false });
}

async function runSearch() {
  markUserInteraction();
  const query = state.ui.query.trim();
  if (!query) {
    setNotice("Enter a customer name, email, or phone before searching.", "error");
    render();
    return;
  }

  state.ui.isSearching = true;
  setNotice(`Searching for '${query}'...`);
  persistWidgetState();
  render();

  const result = await callTool("fashion_lookup_customer", { query, limit: 10 });
  state.ui.isSearching = false;
  if (result.__toolError) {
    setNotice(result.__toolError, "error");
    render();
    return;
  }

  const lookup = result.structuredContent || result;
  if (!lookup || typeof lookup.mode !== "string") {
    setNotice("Search returned an unexpected payload.", "error");
    render();
    return;
  }

  let nextResults = [];
  let resolved = null;
  if (lookup.mode === "resolved" && lookup.resolved) {
    resolved = lookup.resolved;
    nextResults = [lookup.resolved];
    state.ui.resultsExpanded = false;
    setNotice(`Resolved customer ${lookup.resolved.full_name || lookup.resolved.id}.`);
  } else {
    nextResults = Array.isArray(lookup.candidates) ? lookup.candidates : [];
    state.ui.resultsExpanded = nextResults.length > 0;
    if (nextResults.length) {
      setNotice(`Found ${nextResults.length} matching customer(s).`);
    } else {
      setNotice("No customers matched that query.", "error");
    }
  }

  state.payload = {
    ...state.payload,
    query,
    mode: lookup.mode,
    resolved,
    results: nextResults,
  };
  clearRecommendationState();

  if (resolved && resolved.id) {
    selectCustomerId(resolved.id, { collapseResults: true });
  } else if (nextResults.length) {
    if (!nextResults.some((row) => row.id === state.ui.selectedCustomerId)) {
      selectCustomerId(nextResults[0].id, { collapseResults: false });
    }
  } else {
    state.ui.selectedCustomerId = null;
    state.ui.resultsExpanded = false;
    state.ui.emailTo = "";
    clearAnalyticsState();
  }

  persistWidgetState();
  render();
}

function renderCustomerRow(customer) {
  const selected = customer.id === state.ui.selectedCustomerId;
  return el(
    "button",
    {
      className: `fw-result ${selected ? "selected" : ""}`,
      type: "button",
      onClick: () => {
        selectCustomerId(customer.id, { collapseResults: true });
        render();
      },
    },
    el("div", { className: "fw-result-title", text: customer.full_name || customer.id || "Unknown customer" }),
    el("div", { className: "fw-result-meta", text: customerLabel(customer) }),
    el(
      "div",
      { className: "fw-chip-row" },
      customer.match_reason ? el("span", { className: "fw-chip", text: customer.match_reason }) : null,
      customer.home_store_name ? el("span", { className: "fw-chip subtle", text: customer.home_store_name }) : null,
      customer.match_score !== undefined ? el("span", { className: "fw-chip subtle", text: `score ${customer.match_score}` }) : null,
    ),
  );
}

function selectedCustomer(results) {
  if (!results.length || !state.ui.selectedCustomerId) {
    return null;
  }
  return results.find((customer) => customer.id === state.ui.selectedCustomerId) || null;
}

async function loadRecommendations(selected, options = {}) {
  const autoSeed = options.autoSeed === true;
  if (!autoSeed) {
    markUserInteraction();
  }
  if (!selected) {
    setNotice("Select a customer before requesting recommendations.", "error");
    render();
    return;
  }

  const args = {
    store_id: selected.home_store_id,
    customer_id: selected.id,
    top_k: autoSeed ? 3 : 6,
    retrieval_mode: "auto",
  };
  const occasion = state.ui.occasion.trim();
  if (occasion) {
    args.occasion = occasion;
  }
  const budgetMax = parseBudgetMax(state.ui.budgetMax);
  if (budgetMax !== null) {
    args.budget_max = budgetMax;
  }
  const styleConstraints = normalizeStyleConstraints(state.ui.styleConstraints);
  if (styleConstraints) {
    args.style_constraints = styleConstraints;
  }

  state.recommendation.isLoading = true;
  state.recommendation.error = "";
  state.recommendation.inventoryByProduct = {};
  setNotice(autoSeed ? "Loading starter recommendations..." : "Loading recommendations...");
  persistWidgetState();
  render();

  const result = await callTool("fashion_store_associate_recommend", args);
  state.recommendation.isLoading = false;
  if (result.__toolError) {
    state.recommendation.error = result.__toolError;
    state.recommendation.response = null;
    state.recommendation.inventoryByProduct = {};
    setNotice(result.__toolError, "error");
    render();
    return;
  }

  const response = normalizeRecommendationResponse(result);
  const rows = recommendationRows(response);
  if (!Array.isArray(rows)) {
    state.recommendation.error = "Recommendation tool returned an unexpected payload.";
    state.recommendation.response = null;
    state.recommendation.inventoryByProduct = {};
    setNotice(state.recommendation.error, "error");
    render();
    return;
  }

  state.recommendation.customerId = selected.id;
  state.recommendation.response = response;
  state.recommendation.error = "";
  state.recommendation.inventoryByProduct = {};
  const appliedConstraints = normalizeStyleConstraints(response?.recommendation?.applied_style_constraints);
  state.ui.styleConstraints = appliedConstraints;
  state.recommendation.selectedProductIds = syncSelectedProducts(rows, state.recommendation.selectedProductIds);
  if (!state.ui.emailTo && selected.email) {
    state.ui.emailTo = selected.email;
  }
  persistWidgetState();
  setNotice(
    autoSeed
      ? `Loaded ${rows.length} starter recommendations for ${selected.full_name || selected.id}.`
      : `Loaded ${rows.length} recommendations for ${selected.full_name || selected.id}.`,
  );
  render();
}

async function clearStyleGuidance(selected) {
  state.ui.styleConstraints = null;
  persistWidgetState();
  setNotice("Cleared uploaded image guidance. Reloading baseline recommendations...");
  if (!selected) {
    render();
    return;
  }
  await loadRecommendations(selected);
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

function customerValueLookbackDays() {
  return parsePositiveInt(state.analytics.lookbackDays, 180, 30, 730);
}

function customerValueForecastWeeks() {
  return parsePositiveInt(state.analytics.forecastWeeks, 8, 1, 26);
}

function customerValueRequestKey(customerId, lookbackDays, forecastWeeks) {
  return `${customerId}|${lookbackDays}|${forecastWeeks}`;
}

function normalizeCustomerValueSummaryResponse(raw) {
  if (isObject(raw?.structuredContent)) {
    return normalizeCustomerValueSummaryResponse(raw.structuredContent);
  }
  if (!isObject(raw)) {
    return null;
  }
  if (!isObject(raw.customer) || !isObject(raw.metrics)) {
    const parsed = parseJsonContentPayload(raw);
    if (parsed) {
      return normalizeCustomerValueSummaryResponse(parsed);
    }
    return null;
  }

  const normalizeDatePoint = (point) => {
    if (!isObject(point)) {
      return null;
    }
    const periodStart = typeof point.period_start === "string" ? point.period_start : "";
    if (!periodStart) {
      return null;
    }
    return periodStart;
  };

  const valueSeries = Array.isArray(raw.value_series)
    ? raw.value_series
        .map((point) => {
          const periodStart = normalizeDatePoint(point);
          if (!periodStart) {
            return null;
          }
          const score = Number(point.value_score);
          return {
            period_start: periodStart,
            value_score: Number.isFinite(score) ? score : null,
          };
        })
        .filter((point) => point && point.value_score !== null)
    : [];

  const purchaseSeries = Array.isArray(raw.purchase_series)
    ? raw.purchase_series
        .map((point) => {
          const periodStart = normalizeDatePoint(point);
          if (!periodStart) {
            return null;
          }
          const spend = Number(point.spend);
          const orders = Number(point.orders);
          return {
            period_start: periodStart,
            spend: Number.isFinite(spend) ? spend : 0,
            orders: Number.isFinite(orders) ? Math.max(0, Math.round(orders)) : 0,
          };
        })
        .filter(Boolean)
    : [];

  const forecastSeries = Array.isArray(raw.forecast_series)
    ? raw.forecast_series
        .map((point) => {
          const periodStart = normalizeDatePoint(point);
          if (!periodStart) {
            return null;
          }
          const projected = Number(point.projected_spend);
          const low = Number(point.low_spend);
          const high = Number(point.high_spend);
          return {
            period_start: periodStart,
            projected_spend: Number.isFinite(projected) ? projected : 0,
            low_spend: Number.isFinite(low) ? low : 0,
            high_spend: Number.isFinite(high) ? high : 0,
          };
        })
        .filter(Boolean)
    : [];

  const metrics = {
    value_score: Number.isFinite(Number(raw.metrics.value_score)) ? Number(raw.metrics.value_score) : 0,
    value_tier: typeof raw.metrics.value_tier === "string" ? raw.metrics.value_tier : "low",
    lifetime_spend: Number.isFinite(Number(raw.metrics.lifetime_spend)) ? Number(raw.metrics.lifetime_spend) : 0,
    lookback_spend: Number.isFinite(Number(raw.metrics.lookback_spend)) ? Number(raw.metrics.lookback_spend) : 0,
    lifetime_orders: Number.isFinite(Number(raw.metrics.lifetime_orders)) ? Math.max(0, Math.round(Number(raw.metrics.lifetime_orders))) : 0,
    lookback_orders: Number.isFinite(Number(raw.metrics.lookback_orders)) ? Math.max(0, Math.round(Number(raw.metrics.lookback_orders))) : 0,
    aov: Number.isFinite(Number(raw.metrics.aov)) ? Number(raw.metrics.aov) : 0,
    recency_days:
      raw.metrics.recency_days === null || raw.metrics.recency_days === undefined
        ? null
        : Number.isFinite(Number(raw.metrics.recency_days))
          ? Number(raw.metrics.recency_days)
          : null,
  };

  return {
    customer: clone(raw.customer),
    lookback_days: parsePositiveInt(raw.lookback_days, 180, 30, 730),
    forecast_weeks: parsePositiveInt(raw.forecast_weeks, 8, 1, 26),
    purchase_scope: typeof raw.purchase_scope === "string" ? raw.purchase_scope : "all_stores",
    metrics,
    value_series: valueSeries,
    purchase_series: purchaseSeries,
    forecast_series: forecastSeries,
  };
}

async function loadCustomerValueSummary(selected, options = {}) {
  if (!selected || !selected.id) {
    return;
  }
  const lookbackDays = customerValueLookbackDays();
  const forecastWeeks = customerValueForecastWeeks();
  const requestKey = customerValueRequestKey(selected.id, lookbackDays, forecastWeeks);
  if (state.analytics.isLoading && state.analytics.requestedKey === requestKey) {
    return;
  }
  const quiet = options.quiet === true;
  state.analytics.isLoading = true;
  state.analytics.error = "";
  state.analytics.requestedKey = requestKey;
  if (!quiet) {
    setNotice("Loading customer value analytics...");
  }
  render();

  const result = await callTool("fashion_customer_value_summary", {
    customer_id: selected.id,
    lookback_days: lookbackDays,
    forecast_weeks: forecastWeeks,
    purchase_scope: "all_stores",
  });

  if (state.analytics.requestedKey !== requestKey || state.ui.selectedCustomerId !== selected.id) {
    return;
  }
  state.analytics.isLoading = false;
  if (result.__toolError) {
    state.analytics.error = result.__toolError;
    state.analytics.response = null;
    state.analytics.loadedKey = null;
    if (!quiet) {
      setNotice(result.__toolError, "error");
    }
    render();
    return;
  }

  const response = normalizeCustomerValueSummaryResponse(result);
  if (!response) {
    state.analytics.error = "Customer value tool returned an unexpected payload.";
    state.analytics.response = null;
    state.analytics.loadedKey = null;
    if (!quiet) {
      setNotice(state.analytics.error, "error");
    }
    render();
    return;
  }

  state.analytics.customerId = selected.id;
  state.analytics.response = response;
  state.analytics.error = "";
  state.analytics.loadedKey = requestKey;
  if (!quiet) {
    setNotice(`Loaded customer value analytics for ${selected.full_name || selected.id}.`);
  }
  render();
}

function queueSeedCustomerValue(selected) {
  if (!selected || !selected.id) {
    return;
  }
  const lookbackDays = customerValueLookbackDays();
  const forecastWeeks = customerValueForecastWeeks();
  const requestKey = customerValueRequestKey(selected.id, lookbackDays, forecastWeeks);
  if (state.analytics.loadedKey === requestKey || state.analytics.requestedKey === requestKey || state.analytics.isLoading) {
    return;
  }
  state.analytics.requestedKey = requestKey;
  window.setTimeout(() => {
    const activeSelected = selectedCustomer(Array.isArray(state.payload.results) ? state.payload.results : []);
    if (!activeSelected || activeSelected.id !== selected.id) {
      return;
    }
    const activeKey = customerValueRequestKey(activeSelected.id, customerValueLookbackDays(), customerValueForecastWeeks());
    if (activeKey !== requestKey) {
      return;
    }
    void loadCustomerValueSummary(activeSelected, { quiet: true });
  }, 0);
}

function normalizeRecommendationResponse(raw) {
  if (isObject(raw?.structuredContent)) {
    return normalizeRecommendationResponse(raw.structuredContent);
  }
  if (isObject(raw?.result)) {
    return normalizeRecommendationResponse(raw.result);
  }
  if (isObject(raw?.recommendation) && isObject(raw.recommendation.recommendation)) {
    return normalizeRecommendationResponse(raw.recommendation);
  }
  if (!isObject(raw)) {
    return null;
  }
  if (isObject(raw.recommendation) && Array.isArray(raw.recommendation.recommendations)) {
    return raw;
  }
  if (Array.isArray(raw.recommendations)) {
    return { recommendation: raw, retrieval_mode: raw.retrieval_mode || "auto" };
  }
  const parsed = parseJsonContentPayload(raw);
  if (parsed) {
    return normalizeRecommendationResponse(parsed);
  }
  return null;
}

function recommendationRows(response) {
  if (!isObject(response?.recommendation) || !Array.isArray(response.recommendation.recommendations)) {
    return [];
  }
  return response.recommendation.recommendations;
}

function recommendationProductId(item) {
  if (!isObject(item)) {
    return null;
  }
  if (typeof item.product_id === "string" && item.product_id.trim()) {
    return item.product_id.trim();
  }
  if (typeof item.id === "string" && item.id.trim()) {
    return item.id.trim();
  }
  return null;
}

function normalizeAvailabilityToken(value) {
  return String(value || "").trim().toLowerCase();
}

function recommendationIsPreorder(item) {
  return normalizeAvailabilityToken(item?.availability) === "preorder";
}

function recommendationIsInStock(item) {
  return normalizeAvailabilityToken(item?.availability) === "in stock";
}

function normalizeInventoryCheckResponse(raw) {
  if (isObject(raw?.structuredContent)) {
    return normalizeInventoryCheckResponse(raw.structuredContent);
  }
  if (isObject(raw?.result)) {
    return normalizeInventoryCheckResponse(raw.result);
  }
  if (!isObject(raw)) {
    return null;
  }
  if (Array.isArray(raw.rows)) {
    return raw;
  }
  const parsed = parseJsonContentPayload(raw);
  if (parsed) {
    return normalizeInventoryCheckResponse(parsed);
  }
  return null;
}

function inventoryCheckState(productId) {
  if (!productId) {
    return null;
  }
  return isObject(state.recommendation.inventoryByProduct?.[productId]) ? state.recommendation.inventoryByProduct[productId] : null;
}

function inventoryCheckSummaryLine(response) {
  if (!isObject(response) || !Array.isArray(response.rows) || !response.rows.length) {
    return "";
  }
  const rows = response.rows;
  const inStockStores = rows.filter((row) => Number(row.in_stock_skus || 0) > 0).length;
  const top = rows.slice(0, 3).map((row) => {
    const name = typeof row.store_name === "string" ? row.store_name : String(row.store_id || "store");
    const inStock = compactNumber(row.in_stock_skus || 0);
    const preorder = compactNumber(row.preorder_skus || 0);
    const risk = compactNumber(row.not_in_stock_rate_pct || 0);
    return `${name} (in stock ${inStock}, preorder ${preorder}, risk ${risk}%)`;
  });
  return `In stock in ${inStockStores}/${rows.length} stores. Top: ${top.join(" • ")}.`;
}

async function checkInventoryByStore(item) {
  const productId = recommendationProductId(item);
  if (!productId) {
    return;
  }
  markUserInteraction();
  state.recommendation.inventoryByProduct = {
    ...state.recommendation.inventoryByProduct,
    [productId]: { isLoading: true, error: "", response: null },
  };
  render();

  const result = await callTool("fashion_inventory_check_by_store", { product_id: productId, limit: 8 });
  if (result.__toolError) {
    state.recommendation.inventoryByProduct = {
      ...state.recommendation.inventoryByProduct,
      [productId]: { isLoading: false, error: result.__toolError, response: null },
    };
    render();
    return;
  }

  const response = normalizeInventoryCheckResponse(result);
  if (!response) {
    state.recommendation.inventoryByProduct = {
      ...state.recommendation.inventoryByProduct,
      [productId]: { isLoading: false, error: "Inventory check returned an unexpected payload.", response: null },
    };
    render();
    return;
  }

  state.recommendation.inventoryByProduct = {
    ...state.recommendation.inventoryByProduct,
    [productId]: { isLoading: false, error: "", response },
  };
  setNotice(`Checked inventory by store for ${item?.title || productId}.`);
  render();
}

function syncSelectedProducts(rows, existingSelection = [], options = {}) {
  const seedWhenEmpty = options.seedWhenEmpty !== false;
  const validIds = new Set(
    rows
      .map((item) => recommendationProductId(item))
      .filter((value) => typeof value === "string" && value),
  );
  const filtered = normalizeProductIds(existingSelection).filter((productId) => validIds.has(productId));
  if (filtered.length || !seedWhenEmpty) {
    return filtered;
  }
  return rows
    .slice(0, 3)
    .map((item) => recommendationProductId(item))
    .filter((value) => typeof value === "string" && value);
}

function toggleSelectedProduct(productId) {
  markUserInteraction();
  if (!productId) {
    return;
  }
  const current = normalizeProductIds(state.recommendation.selectedProductIds);
  const idx = current.indexOf(productId);
  if (idx >= 0) {
    current.splice(idx, 1);
  } else {
    current.push(productId);
  }
  state.recommendation.selectedProductIds = current;
  persistWidgetState();
}

function selectAllProducts(response) {
  markUserInteraction();
  const rows = recommendationRows(response);
  state.recommendation.selectedProductIds = normalizeProductIds(
    rows.map((item) => recommendationProductId(item)),
  );
  persistWidgetState();
}

function deselectAllProducts() {
  markUserInteraction();
  state.recommendation.selectedProductIds = [];
  persistWidgetState();
}

function normalizeEmailDraftResponse(raw) {
  if (isObject(raw?.structuredContent)) {
    return normalizeEmailDraftResponse(raw.structuredContent);
  }
  if (!isObject(raw)) {
    return null;
  }
  if (isObject(raw.message) && isObject(raw.customer) && isObject(raw.store)) {
    return raw;
  }
  const parsed = parseJsonContentPayload(raw);
  if (parsed) {
    return normalizeEmailDraftResponse(parsed);
  }
  return null;
}

function draftMatchesCurrentWorkspace(response) {
  if (!response || !isObject(response.message) || response.message.channel !== "email") {
    return false;
  }
  const messageId = typeof response.message.id === "string" ? response.message.id : null;
  const responseCustomerId = typeof response.customer?.id === "string" ? response.customer.id : null;
  const results = Array.isArray(state.payload.results) ? state.payload.results : [];
  const selected = selectedCustomer(results);
  if (messageId && state.ui.emailDraftId && messageId === state.ui.emailDraftId) {
    return true;
  }
  if (selected && responseCustomerId && selected.id === responseCustomerId) {
    return true;
  }
  return false;
}

function draftResponseFingerprint(response) {
  if (!response || !isObject(response.message)) {
    return "";
  }
  const destination =
    typeof response.destination_email === "string"
      ? response.destination_email
      : typeof response.message.destination_e164 === "string"
        ? response.message.destination_e164
        : "";
  const subject =
    typeof response.subject === "string"
      ? response.subject
      : typeof response.message.subject === "string"
        ? response.message.subject
        : "";
  const body = typeof response.message.body_text === "string" ? response.message.body_text : "";
  const status = typeof response.message.status === "string" ? response.message.status : "";
  const productIds = normalizeProductIds(response.message.product_ids || []);
  return JSON.stringify({
    id: response.message.id || "",
    destination: destination.trim().toLowerCase(),
    subject: subject.trim(),
    body,
    status,
    product_ids: productIds,
  });
}

function applyEmailDraftResponse(selected, response) {
  if (!response || !isObject(response.message)) {
    return false;
  }
  state.ui.emailDraftId = typeof response.message.id === "string" ? response.message.id : state.ui.emailDraftId;
  const destination = typeof response.destination_email === "string" ? response.destination_email : response.message.destination_e164;
  if (typeof destination === "string" && destination.trim()) {
    state.ui.emailTo = destination.trim();
  }
  const subject = typeof response.subject === "string" ? response.subject : response.message.subject;
  state.ui.emailSubject = typeof subject === "string" ? subject : state.ui.emailSubject;
  if (typeof response.message.body_text === "string") {
    state.ui.emailBody = response.message.body_text;
  }
  const rows = recommendationRows(state.recommendation.response);
  state.recommendation.selectedProductIds = syncSelectedProducts(rows, response.message.product_ids || []);
  state.runtime.lastDraftFingerprint = draftResponseFingerprint(response);
  persistWidgetState();
  return true;
}

function stopDraftPolling() {
  if (state.runtime.draftPollTimer) {
    window.clearInterval(state.runtime.draftPollTimer);
    state.runtime.draftPollTimer = null;
  }
  state.runtime.draftPollInFlight = false;
}

function applyIncomingDraftUpdate(incomingDraft, source = "chat") {
  if (!incomingDraft || !draftMatchesCurrentWorkspace(incomingDraft)) {
    return false;
  }
  const results = Array.isArray(state.payload.results) ? state.payload.results : [];
  const selected = selectedCustomer(results);
  if (!applyEmailDraftResponse(selected, incomingDraft)) {
    return false;
  }
  const incomingStatus = typeof incomingDraft.message?.status === "string" ? incomingDraft.message.status : "";
  if (incomingStatus === "sent") {
    state.ui.emailDraftId = null;
    state.runtime.lastDraftFingerprint = "";
    stopDraftPolling();
    persistWidgetState();
    setNotice("Draft was sent from chat. Workspace draft state updated.");
  } else if (source === "poll") {
    setNotice(`Draft ${incomingDraft.message.id} synced from backend.`);
  } else {
    setNotice(`Draft ${incomingDraft.message.id} updated from chat.`);
  }
  return true;
}

async function pollActiveDraft(selected) {
  if (!selected || !state.ui.emailDraftId) {
    return;
  }
  if (state.runtime.draftPollInFlight) {
    return;
  }
  if (
    state.recommendation.isPreparingEmailDraft ||
    state.recommendation.isRefreshingEmailDraft ||
    state.recommendation.isUpdatingEmailDraft ||
    state.recommendation.isSendingEmailDraft
  ) {
    return;
  }
  state.runtime.draftPollInFlight = true;
  const result = await callTool("fashion_get_customer_email_draft", { message_id: state.ui.emailDraftId });
  state.runtime.draftPollInFlight = false;
  if (result.__toolError) {
    return;
  }
  const incomingDraft = normalizeEmailDraftResponse(result);
  if (!incomingDraft) {
    return;
  }
  const incomingFingerprint = draftResponseFingerprint(incomingDraft);
  if (incomingFingerprint && incomingFingerprint === state.runtime.lastDraftFingerprint) {
    return;
  }
  if (applyIncomingDraftUpdate(incomingDraft, "poll")) {
    render();
  }
}

function ensureDraftPolling(selected) {
  const shouldPoll = Boolean(selected && state.ui.emailDraftId);
  if (!shouldPoll) {
    stopDraftPolling();
    return;
  }
  if (state.runtime.draftPollTimer) {
    return;
  }
  state.runtime.draftPollTimer = window.setInterval(() => {
    const results = Array.isArray(state.payload.results) ? state.payload.results : [];
    const activeSelected = selectedCustomer(results);
    if (!activeSelected || !state.ui.emailDraftId) {
      stopDraftPolling();
      return;
    }
    void pollActiveDraft(activeSelected);
  }, DRAFT_POLL_INTERVAL_MS);
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

function buildEmailDraftClipboardText(selected) {
  const destination = (state.ui.emailTo || selected.email || "").trim();
  const subject = (state.ui.emailSubject || "").trim();
  const body = state.ui.emailBody || "";
  return [`To: ${destination}`, `Subject: ${subject}`, "", body].join("\n");
}

function buildEmailDraftArgs(selected, options = {}) {
  const rows = recommendationRows(state.recommendation.response);
  const selectedIds = syncSelectedProducts(rows, state.recommendation.selectedProductIds, {
    seedWhenEmpty: false,
  });
  const destination = (state.ui.emailTo || selected.email || "").trim();
  const args = {
    store_id: selected.home_store_id,
    customer_id: selected.id,
    selected_product_ids: selectedIds,
    to_email: destination || undefined,
  };
  if (options.includeMessageId && state.ui.emailDraftId) {
    args.message_id = state.ui.emailDraftId;
  }
  const occasion = state.ui.occasion.trim();
  if (occasion) {
    args.occasion = occasion;
  }
  const budgetMax = parseBudgetMax(state.ui.budgetMax);
  if (budgetMax !== null) {
    args.budget_max = budgetMax;
  }
  const styleConstraints = normalizeStyleConstraints(state.ui.styleConstraints);
  if (styleConstraints) {
    args.style_constraints = styleConstraints;
  }
  const subject = state.ui.emailSubject.trim();
  if (subject) {
    args.subject = subject;
  }
  return args;
}

async function hydrateDraftById(selected, messageId, options = {}) {
  if (!selected || !messageId) {
    return null;
  }
  if (!options.quiet) {
    state.recommendation.isRefreshingEmailDraft = true;
    if (!options.silent) {
      setNotice(`Refreshing draft ${messageId}...`);
    }
    render();
  }
  const result = await callTool("fashion_get_customer_email_draft", { message_id: messageId });
  if (!options.quiet) {
    state.recommendation.isRefreshingEmailDraft = false;
  }
  if (result.__toolError) {
    if (!options.silent) {
      setNotice(result.__toolError, "error");
    }
    render();
    return null;
  }
  const response = normalizeEmailDraftResponse(result);
  if (!response) {
    if (!options.silent) {
      setNotice("Draft refresh returned an unexpected payload.", "error");
    }
    render();
    return null;
  }
  applyEmailDraftResponse(selected, response);
  if (!options.silent) {
    setNotice(`Refreshed draft ${response.message.id}.`);
  }
  render();
  return response;
}

async function prepareEmailDraft(selected, options = {}) {
  if (!selected) {
    setNotice("Select a customer before preparing an email draft.", "error");
    render();
    return null;
  }
  const rows = recommendationRows(state.recommendation.response);
  if (!rows.length) {
    setNotice("Load recommendations before preparing an email draft.", "error");
    render();
    return null;
  }
  const selectedIds = syncSelectedProducts(rows, state.recommendation.selectedProductIds, {
    seedWhenEmpty: false,
  });
  state.recommendation.selectedProductIds = selectedIds;
  const destination = (state.ui.emailTo || selected.email || "").trim();
  if (!destination) {
    setNotice("Enter a destination email before preparing draft.", "error");
    render();
    return null;
  }

  const quiet = options.quiet === true;
  state.recommendation.isPreparingEmailDraft = true;
  if (!quiet) {
    setNotice(options.silent ? "Preparing draft..." : `Preparing draft for ${selected.full_name || selected.id}...`);
  }
  persistWidgetState();
  render();

  const result = await callTool("fashion_prepare_customer_email_draft", buildEmailDraftArgs(selected, options));
  state.recommendation.isPreparingEmailDraft = false;
  if (result.__toolError) {
    setNotice(result.__toolError, "error");
    render();
    return null;
  }
  const response = normalizeEmailDraftResponse(result);
  if (!response) {
    setNotice("Email draft tool returned an unexpected payload.", "error");
    render();
    return null;
  }
  applyEmailDraftResponse(selected, response);
  if (!options.silent && !quiet) {
    setNotice(`Prepared email draft ${response.message.id}.`);
  }
  render();
  return response;
}

async function refreshEmailDraft(selected) {
  markUserInteraction();
  state.recommendation.isRefreshingEmailDraft = true;
  const rows = recommendationRows(state.recommendation.response);
  if (selected && rows.length) {
    const rebuilt = await prepareEmailDraft(selected, { includeMessageId: true, silent: true });
    if (rebuilt) {
      setNotice(`Rebuilt draft ${rebuilt.message.id} from current recommendations.`);
    }
    state.recommendation.isRefreshingEmailDraft = false;
    render();
    return;
  }
  if (!state.ui.emailDraftId) {
    state.recommendation.isRefreshingEmailDraft = false;
    setNotice("No draft available. Prepare recommendations first.", "error");
    render();
    return;
  }
  await hydrateDraftById(selected, state.ui.emailDraftId);
  state.recommendation.isRefreshingEmailDraft = false;
  render();
}

async function syncEmailDraftEdits(selected) {
  if (!selected) {
    return null;
  }
  if (!state.ui.emailDraftId) {
    return prepareEmailDraft(selected, { includeMessageId: false, silent: true });
  }
  const rows = recommendationRows(state.recommendation.response);
  const selectedIds = syncSelectedProducts(rows, state.recommendation.selectedProductIds, {
    seedWhenEmpty: false,
  });
  const destination = (state.ui.emailTo || selected.email || "").trim();
  if (!destination) {
    setNotice("Enter a destination email before updating draft.", "error");
    render();
    return null;
  }
  state.recommendation.isUpdatingEmailDraft = true;
  const result = await callTool("fashion_update_customer_email_draft", {
    message_id: state.ui.emailDraftId,
    subject: state.ui.emailSubject,
    body_text: state.ui.emailBody,
    to_email: destination,
    selected_product_ids: selectedIds,
  });
  state.recommendation.isUpdatingEmailDraft = false;
  if (result.__toolError) {
    setNotice(result.__toolError, "error");
    render();
    return null;
  }
  const response = normalizeEmailDraftResponse(result);
  if (!response) {
    setNotice("Draft update returned an unexpected payload.", "error");
    render();
    return null;
  }
  applyEmailDraftResponse(selected, response);
  return response;
}

async function copyEmailDraft(selected) {
  markUserInteraction();
  if (!selected) {
    setNotice("Select a customer before copying the draft.", "error");
    render();
    return;
  }
  if (!state.ui.emailBody.trim()) {
    const seeded = await syncEmailDraftEdits(selected);
    if (!seeded) {
      return;
    }
  }
  const copied = await copyTextToClipboard(buildEmailDraftClipboardText(selected));
  if (!copied) {
    setNotice("Clipboard copy failed in this browser.", "error");
    render();
    return;
  }
  setNotice("Copied draft email to clipboard.");
  render();
}

async function sendEmailDraft(selected) {
  markUserInteraction();
  if (!selected) {
    setNotice("Select a customer before sending email.", "error");
    render();
    return;
  }
  const synced = await syncEmailDraftEdits(selected);
  if (!synced) {
    return;
  }

  state.recommendation.isSendingEmailDraft = true;
  setNotice(`Sending draft ${synced.message.id}...`);
  render();
  const result = await callTool("fashion_send_customer_email_draft", { message_id: synced.message.id });
  state.recommendation.isSendingEmailDraft = false;
  if (result.__toolError) {
    setNotice(result.__toolError, "error");
    render();
    return;
  }
  const payload = isObject(result.structuredContent) ? result.structuredContent : result;
  const status = typeof payload?.message?.status === "string" ? payload.message.status : null;
  if (!status) {
    setNotice("Email send tool returned an unexpected payload.", "error");
    render();
    return;
  }
  const sentTo = typeof payload.destination_email === "string" ? payload.destination_email : state.ui.emailTo;
  if (status === "sent") {
    const providerId = payload.provider_message_id ? ` (${payload.provider_message_id})` : "";
    state.ui.emailDraftId = null;
    state.runtime.lastDraftFingerprint = "";
    stopDraftPolling();
    persistWidgetState();
    setNotice(`Sent recommendations email to ${sentTo}${providerId}.`);
  } else {
    const errorMessage =
      typeof payload?.message?.error_message === "string" && payload.message.error_message
        ? payload.message.error_message
        : "Email send failed.";
    setNotice(errorMessage, "error");
  }
  render();
}

function queueSeedRecommendations(selected) {
  if (!selected || !selected.id) {
    return;
  }
  if (state.recommendation.isLoading) {
    return;
  }
  if (state.recommendation.customerId === selected.id && state.recommendation.response) {
    return;
  }
  if (state.recommendation.seedAttemptedCustomerId === selected.id) {
    return;
  }
  state.recommendation.seedAttemptedCustomerId = selected.id;
  window.setTimeout(() => {
    const activeSelected = selectedCustomer(Array.isArray(state.payload.results) ? state.payload.results : []);
    if (!activeSelected || activeSelected.id !== selected.id) {
      return;
    }
    if (state.recommendation.customerId === activeSelected.id && state.recommendation.response) {
      return;
    }
    void loadRecommendations(activeSelected, { autoSeed: true });
  }, 0);
}

function queueSeedEmailDraft(selected) {
  if (!selected || !selected.id) {
    return;
  }
  if (
    state.recommendation.isPreparingEmailDraft ||
    state.recommendation.isRefreshingEmailDraft ||
    state.recommendation.isUpdatingEmailDraft ||
    state.recommendation.isSendingEmailDraft
  ) {
    return;
  }
  if (state.ui.emailDraftId) {
    return;
  }
  const initialDraftId = typeof state.payload.initial_email_draft_id === "string"
    ? state.payload.initial_email_draft_id.trim()
    : "";
  if (initialDraftId) {
    return;
  }
  const response = state.recommendation.customerId === selected.id ? state.recommendation.response : null;
  if (!response || !recommendationRows(response).length) {
    return;
  }
  const destination = (state.ui.emailTo || selected.email || "").trim();
  if (!destination) {
    return;
  }
  if (state.recommendation.seedAttemptedDraftCustomerId === selected.id) {
    return;
  }
  state.recommendation.seedAttemptedDraftCustomerId = selected.id;
  window.setTimeout(() => {
    const activeSelected = selectedCustomer(Array.isArray(state.payload.results) ? state.payload.results : []);
    if (!activeSelected || activeSelected.id !== selected.id) {
      return;
    }
    if (state.ui.emailDraftId || state.recommendation.isPreparingEmailDraft) {
      return;
    }
    void prepareEmailDraft(activeSelected, { includeMessageId: false, silent: true, quiet: true });
  }, 0);
}

function queueHydrateInitialEmailDraft(selected) {
  if (!selected || !selected.id) {
    return;
  }
  const draftId = typeof state.payload.initial_email_draft_id === "string"
    ? state.payload.initial_email_draft_id.trim()
    : "";
  if (!draftId) {
    return;
  }
  if (state.runtime.draftHydrationRequestedForId === draftId) {
    return;
  }
  if (
    state.recommendation.isPreparingEmailDraft ||
    state.recommendation.isRefreshingEmailDraft ||
    state.recommendation.isUpdatingEmailDraft ||
    state.recommendation.isSendingEmailDraft
  ) {
    return;
  }
  state.runtime.draftHydrationRequestedForId = draftId;
  window.setTimeout(() => {
    const activeSelected = selectedCustomer(Array.isArray(state.payload.results) ? state.payload.results : []);
    if (!activeSelected || activeSelected.id !== selected.id) {
      return;
    }
    state.ui.emailDraftId = draftId;
    if (state.payload.initial_email_subject && !state.ui.emailSubject) {
      state.ui.emailSubject = state.payload.initial_email_subject;
    }
    if (state.payload.initial_email_body && !state.ui.emailBody) {
      state.ui.emailBody = state.payload.initial_email_body;
    }
    persistWidgetState();
    void hydrateDraftById(activeSelected, draftId, { silent: true, quiet: true });
  }, 0);
}

function styleConstraintChips(constraints, source, stage) {
  const normalized = normalizeStyleConstraints(constraints);
  if (!normalized) {
    return [];
  }
  const chips = [];
  const effectiveSource = source || normalized.constraint_source;
  chips.push(
    el(
      "span",
      { className: "fw-chip", text: effectiveSource === "chat_image" ? "From uploaded image" : "Image guidance" },
    ),
  );
  if (stage) {
    chips.push(el("span", { className: "fw-chip subtle", text: stage.replace(/_/g, " ") }));
  }
  normalized.target_categories.slice(0, 2).forEach((value) => {
    chips.push(el("span", { className: "fw-chip subtle", text: `category: ${humanizeToken(value)}` }));
  });
  normalized.target_genders.slice(0, 2).forEach((value) => {
    chips.push(el("span", { className: "fw-chip subtle", text: `gender: ${humanizeToken(value)}` }));
  });
  normalized.style_keywords.slice(0, 2).forEach((value) => {
    chips.push(el("span", { className: "fw-chip subtle", text: `style: ${humanizeToken(value)}` }));
  });
  normalized.exclude_categories.slice(0, 1).forEach((value) => {
    chips.push(el("span", { className: "fw-chip subtle", text: `exclude: ${humanizeToken(value)}` }));
  });
  return chips;
}

function recommendationCards(response) {
  const rows = recommendationRows(response);
  const selectedIds = new Set(normalizeProductIds(state.recommendation.selectedProductIds));
  if (!rows.length) {
    return [el("p", { className: "fw-empty", text: "No recommendations returned for the selected filters." })];
  }
  return rows.map((item) => {
    const itemId = recommendationProductId(item) || "";
    const isPreorder = recommendationIsPreorder(item);
    const stockCheck = inventoryCheckState(itemId);
    const stockSummary = stockCheck?.response ? inventoryCheckSummaryLine(stockCheck.response) : "";
    const executionTags = Array.isArray(item.execution_tags)
      ? item.execution_tags
          .map((tag) => String(tag || "").trim())
          .filter(Boolean)
          .slice(0, 3)
      : [];
    const reasonText = Array.isArray(item.reasons)
      ? item.reasons
          .slice(0, 2)
          .map((reason) => String(reason || "").trim())
          .filter(Boolean)
          .join(" • ")
      : "";
    return el(
      "article",
      { className: "fw-rec-card" },
      el(
        "div",
        { className: "fw-rec-layout" },
        el(
          "label",
          { className: "fw-rec-select-inline" },
          el("input", {
            type: "checkbox",
            checked: selectedIds.has(itemId),
            onChange: () => {
              toggleSelectedProduct(itemId);
              render();
            },
          }),
        ),
        el(
          "div",
          { className: "fw-rec-image-wrap" },
          el("img", {
            className: "fw-rec-image",
            src: item.image_url || `${meta.assetBaseUrl}/demo/editorial-fallback.svg`,
            alt: item.title || itemId || "Product image",
            loading: "lazy",
          }),
        ),
        el(
          "div",
          { className: "fw-rec-content" },
          el("h3", { className: "fw-rec-title", text: item.title || itemId }),
          el("p", { className: "fw-rec-brand", text: item.brand || "Unknown brand" }),
          executionTags.length
            ? el(
                "div",
                { className: "fw-chip-row" },
                ...executionTags.map((tag) => el("span", { className: "fw-chip subtle", text: tag })),
              )
            : null,
          reasonText ? el("p", { className: "fw-rec-reason-inline", text: reasonText }) : null,
          isPreorder
            ? el("p", {
                className: "fw-empty fw-inline-meta",
                text: "Preorder item: not currently in stock at this store.",
              })
            : null,
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
          itemId
            ? el(
                "button",
                {
                  className: "fw-text-button",
                  type: "button",
                  disabled: stockCheck?.isLoading ? "true" : null,
                  onClick: () => {
                    void checkInventoryByStore(item);
                  },
                },
                stockCheck?.isLoading ? "Checking stores..." : "Check by store",
              )
            : null,
          stockCheck?.error ? el("p", { className: "fw-empty fw-inline-meta", text: stockCheck.error }) : null,
          stockSummary ? el("p", { className: "fw-empty fw-inline-meta", text: stockSummary }) : null,
        ),
        el(
          "div",
          { className: "fw-rec-side" },
          el("p", { className: "fw-rec-price", text: money(item.price) }),
          el(
            "div",
            { className: "fw-chip-row fw-chip-row-right" },
            item.category ? el("span", { className: "fw-chip subtle", text: item.category }) : null,
            item.availability
              ? el(
                  "span",
                  {
                    className: `fw-chip subtle ${isPreorder ? "fw-chip-stock-risk" : ""}`,
                    text: item.availability,
                  },
                )
              : null,
            item.score !== undefined
              ? el("span", { className: "fw-chip", text: `score ${Number(item.score).toFixed(2)}` })
              : null,
          ),
        ),
      ),
    );
  });
}

function renderCustomerValuePanel(selected) {
  if (!selected) {
    return null;
  }
  const response = state.analytics.customerId === selected.id ? state.analytics.response : null;
  const metrics = response?.metrics || null;
  const effectiveLookbackDays = Number(response?.lookback_days || customerValueLookbackDays());
  const valueSeries = Array.isArray(response?.value_series) ? response.value_series : [];
  const purchaseSeries = Array.isArray(response?.purchase_series) ? response.purchase_series : [];
  const forecastSeries = Array.isArray(response?.forecast_series) ? response.forecast_series : [];
  const hasChartJs = typeof window?.Chart === "function";
  const panel = el(
    "section",
    {
      className: "fw-panel fw-customer-value-panel",
      id: "fw-customer-view-panel",
      role: "tabpanel",
      "aria-labelledby": "fw-customer-tab-value",
    },
    el(
      "div",
      { className: "fw-section-head" },
      el(
        "div",
        {},
        el("h2", { className: "fw-panel-title", text: "Customer Value" }),
        el("p", {
          className: "fw-empty",
          text: "All-store purchase history with weekly value trend and baseline projection.",
        }),
      ),
      el(
        "div",
        { className: "fw-customer-value-controls" },
        el(
          "div",
          { className: "fw-customer-value-control-grid" },
          el(
            "div",
            { className: "fw-field" },
            el("label", { className: "fw-label", text: "Lookback" }),
            el(
              "select",
              {
                className: "fw-input fw-select",
                value: state.analytics.lookbackDays,
                onChange: (event) => {
                  markUserInteraction();
                  state.analytics.lookbackDays = String(parsePositiveInt(event.target.value, 180, 30, 730));
                  state.analytics.loadedKey = null;
                  state.analytics.requestedKey = null;
                  persistWidgetState();
                  render();
                },
              },
              ...CUSTOMER_VALUE_LOOKBACK_OPTIONS.map((option) =>
                el("option", {
                  value: option,
                  selected: state.analytics.lookbackDays === option ? "true" : null,
                  text: `${option} days`,
                }),
              ),
            ),
          ),
          el(
            "div",
            { className: "fw-field" },
            el("label", { className: "fw-label", text: "Forecast" }),
            el(
              "select",
              {
                className: "fw-input fw-select",
                value: state.analytics.forecastWeeks,
                onChange: (event) => {
                  markUserInteraction();
                  state.analytics.forecastWeeks = String(parsePositiveInt(event.target.value, 8, 1, 26));
                  state.analytics.loadedKey = null;
                  state.analytics.requestedKey = null;
                  persistWidgetState();
                  render();
                },
              },
              ...CUSTOMER_VALUE_FORECAST_OPTIONS.map((option) =>
                el("option", {
                  value: option,
                  selected: state.analytics.forecastWeeks === option ? "true" : null,
                  text: `${option} weeks`,
                }),
              ),
            ),
          ),
        ),
        el(
          "div",
          { className: "fw-customer-value-control-actions" },
          el(
            "button",
            {
              className: "fw-button secondary",
              type: "button",
              disabled: state.analytics.isLoading ? "true" : null,
              onClick: () => {
                markUserInteraction();
                state.analytics.loadedKey = null;
                state.analytics.requestedKey = null;
                void loadCustomerValueSummary(selected);
              },
            },
            state.analytics.isLoading ? "Refreshing..." : "Refresh",
          ),
        ),
      ),
    ),
  );

  if (state.analytics.error) {
    panel.appendChild(el("p", { className: "fw-empty", text: state.analytics.error }));
  }

  if (!response) {
    panel.appendChild(
      el(
        "p",
        {
          className: "fw-empty",
          text: state.analytics.isLoading
            ? "Loading customer value analytics..."
            : "Value analytics will load when a customer is selected.",
        },
      ),
    );
    return panel;
  }

  const tierText = String(metrics?.value_tier || "low").replace(/_/g, " ");
  const tierTone = tierText === "high" ? "is-high" : tierText === "medium" ? "is-medium" : "is-low";
  const metricPill = (label, value, toneClass = "") =>
    el(
      "span",
      { className: `fw-metric-pill ${toneClass}`.trim() },
      el("span", { className: "fw-metric-pill-label", text: label }),
      el("strong", { className: "fw-metric-pill-value", text: value }),
    );
  panel.appendChild(
    el(
      "div",
      { className: "fw-customer-value-metrics" },
      metricPill("Value Score", compactNumber(metrics?.value_score)),
      metricPill("Value Tier", tierText, tierTone),
      metricPill(`Spend (${effectiveLookbackDays}d)`, formatCurrencyCompact(metrics?.lookback_spend)),
      metricPill(`Orders (${effectiveLookbackDays}d)`, compactNumber(metrics?.lookback_orders)),
      metricPill("Lifetime Spend", formatCurrencyCompact(metrics?.lifetime_spend)),
      metricPill("AOV", formatCurrencyCompact(metrics?.aov)),
      metricPill(
        "Recency",
        metrics?.recency_days === null || metrics?.recency_days === undefined ? "-" : `${compactNumber(metrics.recency_days)}d`,
      ),
    ),
  );
  panel.appendChild(
    el("p", {
      className: "fw-empty fw-inline-meta",
      text: `Lookback metrics are calculated across the selected ${effectiveLookbackDays}-day window.`,
    }),
  );

  if (!hasChartJs) {
    panel.appendChild(el("p", { className: "fw-empty", text: "Chart.js unavailable in this host. Value metrics remain available." }));
    return panel;
  }

  const chartsWrap = el(
    "div",
    { className: "fw-customer-value-charts" },
    el(
      "section",
      { className: "fw-panel fw-customer-chart-panel" },
      el("h3", { className: "fw-panel-title", text: "Value Score Trend" }),
      el(
        "div",
        { className: "fw-merch-chart-wrap" },
        el("canvas", { className: "fw-merch-chart-canvas fw-customer-chart-canvas", "data-role": "customer-value-score-chart" }),
      ),
    ),
    el(
      "section",
      { className: "fw-panel fw-customer-chart-panel" },
      el("h3", { className: "fw-panel-title", text: "Past Purchases" }),
      el(
        "div",
        { className: "fw-merch-chart-wrap" },
        el("canvas", { className: "fw-merch-chart-canvas fw-customer-chart-canvas", "data-role": "customer-purchase-history-chart" }),
      ),
    ),
    el(
      "section",
      { className: "fw-panel fw-customer-chart-panel" },
      el("h3", { className: "fw-panel-title", text: "Baseline Spend Projection" }),
      el(
        "div",
        { className: "fw-merch-chart-wrap" },
        el("canvas", { className: "fw-merch-chart-canvas fw-customer-chart-canvas", "data-role": "customer-forecast-chart" }),
      ),
    ),
  );
  panel.appendChild(chartsWrap);

  const ChartCtor = window.Chart;
  const baseChartOptions = {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    plugins: {
      legend: { display: false },
    },
  };

  const valueCanvas = panel.querySelector("[data-role='customer-value-score-chart']");
  if (valueCanvas) {
    const labels = valueSeries.map((point) => formatDateLabel(point.period_start, false));
    const data = valueSeries.map((point) => point.value_score);
    const chart = new ChartCtor(valueCanvas.getContext("2d"), {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            data,
            borderColor: "#1f5d8f",
            backgroundColor: "rgba(31,93,143,0.12)",
            fill: true,
            pointRadius: 2.2,
            pointHoverRadius: 3.8,
            borderWidth: 2.2,
            tension: 0.3,
          },
        ],
      },
      options: {
        ...baseChartOptions,
        scales: {
          y: {
            min: 0,
            max: 100,
            grid: { color: "#e4edf6" },
            ticks: { color: "#617386" },
          },
          x: {
            grid: { display: false },
            ticks: { color: "#617386", maxTicksLimit: 4, autoSkip: true },
          },
        },
      },
    });
    registerAnalyticsChartCleanup(() => chart.destroy());
  }

  const purchaseCanvas = panel.querySelector("[data-role='customer-purchase-history-chart']");
  if (purchaseCanvas) {
    const labels = purchaseSeries.map((point) => formatDateLabel(point.period_start, false));
    const spendValues = purchaseSeries.map((point) => point.spend);
    const orderValues = purchaseSeries.map((point) => point.orders);
    const chart = new ChartCtor(purchaseCanvas.getContext("2d"), {
      data: {
        labels,
        datasets: [
          {
            type: "bar",
            label: "Orders",
            data: orderValues,
            yAxisID: "y1",
            backgroundColor: "rgba(125,142,159,0.6)",
            borderRadius: 4,
            borderSkipped: false,
          },
          {
            type: "line",
            label: "Spend",
            data: spendValues,
            yAxisID: "y",
            borderColor: "#1f5d8f",
            backgroundColor: "rgba(31,93,143,0.08)",
            fill: false,
            pointRadius: 2.1,
            pointHoverRadius: 3.5,
            borderWidth: 2.0,
            tension: 0.24,
          },
        ],
      },
      options: {
        ...baseChartOptions,
        scales: {
          y: {
            grid: { color: "#e4edf6" },
            ticks: { color: "#617386", callback: (value) => formatCurrencyCompact(value) },
          },
          y1: {
            position: "right",
            grid: { drawOnChartArea: false },
            ticks: { color: "#617386" },
          },
          x: {
            grid: { display: false },
            ticks: { color: "#617386", maxTicksLimit: 4, autoSkip: true },
          },
        },
      },
    });
    registerAnalyticsChartCleanup(() => chart.destroy());
  }

  const forecastCanvas = panel.querySelector("[data-role='customer-forecast-chart']");
  if (forecastCanvas) {
    const historyPoints = purchaseSeries.slice(-12);
    const labels = [
      ...historyPoints.map((point) => formatDateLabel(point.period_start, false)),
      ...forecastSeries.map((point) => formatDateLabel(point.period_start, false)),
    ];
    const historyData = [...historyPoints.map((point) => point.spend), ...forecastSeries.map(() => null)];
    const projectedData = [...historyPoints.map(() => null), ...forecastSeries.map((point) => point.projected_spend)];
    const lowData = [...historyPoints.map(() => null), ...forecastSeries.map((point) => point.low_spend)];
    const highData = [...historyPoints.map(() => null), ...forecastSeries.map((point) => point.high_spend)];
    const chart = new ChartCtor(forecastCanvas.getContext("2d"), {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Historical Spend",
            data: historyData,
            borderColor: "#7d8e9f",
            borderWidth: 1.8,
            pointRadius: 2,
            pointHoverRadius: 3.2,
            tension: 0.24,
            spanGaps: true,
          },
          {
            label: "Projected Spend",
            data: projectedData,
            borderColor: "#1f5d8f",
            backgroundColor: "rgba(31,93,143,0.1)",
            borderWidth: 2.2,
            pointRadius: 2.2,
            pointHoverRadius: 3.4,
            tension: 0.24,
            spanGaps: true,
          },
          {
            label: "Low Band",
            data: lowData,
            borderColor: "#aab9c8",
            borderDash: [5, 4],
            borderWidth: 1.2,
            pointRadius: 0,
            tension: 0.2,
            spanGaps: true,
          },
          {
            label: "High Band",
            data: highData,
            borderColor: "#aab9c8",
            borderDash: [5, 4],
            borderWidth: 1.2,
            pointRadius: 0,
            tension: 0.2,
            spanGaps: true,
          },
        ],
      },
      options: {
        ...baseChartOptions,
        plugins: {
          legend: { display: true, labels: { boxWidth: 10, color: "#576b7f" } },
        },
        scales: {
          y: {
            grid: { color: "#e4edf6" },
            ticks: { color: "#617386", callback: (value) => formatCurrencyCompact(value) },
          },
          x: {
            grid: { display: false },
            ticks: { color: "#617386", maxTicksLimit: 5, autoSkip: true },
          },
        },
      },
    });
    registerAnalyticsChartCleanup(() => chart.destroy());
  }

  return panel;
}

function render() {
  teardownAnalyticsCharts();
  const container = clear(root);
  const results = Array.isArray(state.payload.results) ? state.payload.results : [];
  resolveRowSelection(results);
  const selected = selectedCustomer(results);
  const activeStyleConstraints = normalizeStyleConstraints(state.ui.styleConstraints);
  if (selected && !state.ui.emailTo && selected.email) {
    state.ui.emailTo = selected.email;
    persistWidgetState();
  }

  const searchInput = el("input", {
    className: "fw-input",
    type: "text",
    value: state.ui.query,
    placeholder: state.payload.uiHints.searchPlaceholder,
    onInput: (event) => {
      markUserInteraction();
      state.ui.query = event.target.value;
      persistWidgetState();
    },
    onKeydown: (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        void runSearch();
      }
    },
  });

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
      el("h1", { className: "fw-title", text: meta.title || "Customer Workspace" }),
      buildLabel ? el("span", { className: "fw-version", text: buildLabel }) : null,
    ),
    el(
      "p",
      {
        className: "fw-subtitle",
        text:
          meta.summary ||
          "Start with one reliable action: search a customer by name, email, or phone and select the best match.",
      },
    ),
  );

  const toolbar = el(
    "div",
    { className: "fw-toolbar" },
    searchInput,
    el(
      "button",
      {
        className: "fw-button",
        type: "button",
        onClick: () => {
          void runSearch();
        },
        disabled: state.ui.isSearching ? "true" : null,
      },
      state.ui.isSearching ? "Searching..." : "Search",
    ),
  );

  const recommendationControls = selected
    ? el(
        "div",
        { className: "fw-control-row fw-customer-control-row" },
        el(
          "div",
          { className: "fw-field" },
          el("label", { className: "fw-label", text: "Occasion" }),
          el("input", {
            className: "fw-input",
            type: "text",
            value: state.ui.occasion,
            placeholder: "wedding, workwear, vacation...",
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
          el("label", { className: "fw-label", text: "Max Price" }),
          el("input", {
            className: "fw-input",
            type: "number",
            step: "0.01",
            min: "0",
            value: state.ui.budgetMax,
            placeholder: "900",
            onInput: (event) => {
              markUserInteraction();
              state.ui.budgetMax = event.target.value;
              persistWidgetState();
            },
          }),
        ),
        el(
          "div",
          { className: "fw-field actions" },
          el("label", { className: "fw-label", text: "Recommendations" }),
          el(
            "div",
            { className: "fw-button-stack" },
            el(
              "button",
              {
                className: "fw-button",
                type: "button",
                disabled: state.recommendation.isLoading ? "true" : null,
                onClick: () => {
                  void loadRecommendations(selected);
                },
              },
              state.recommendation.isLoading ? "Loading..." : "Refresh Recommendations",
            ),
            activeStyleConstraints
              ? el(
                  "button",
                  {
                    className: "fw-button secondary",
                    type: "button",
                    disabled: state.recommendation.isLoading ? "true" : null,
                    onClick: () => {
                      void clearStyleGuidance(selected);
                    },
                  },
                  "Clear Image Guidance",
                )
              : null,
          ),
        ),
      )
    : null;

  const resultList = results.length
    ? state.ui.resultsExpanded
      ? el("div", { className: "fw-list" }, ...results.map(renderCustomerRow))
      : el(
          "div",
          { className: "fw-toolbar" },
          el("p", { className: "fw-empty", text: `${results.length} result(s) hidden after selection.` }),
          el(
            "button",
            {
              className: "fw-button secondary",
              type: "button",
              onClick: () => {
                markUserInteraction();
                state.ui.resultsExpanded = true;
                render();
              },
            },
            "Show Results",
          ),
        )
    : el("div", { className: "fw-empty", text: state.payload.uiHints.emptyState });

  const detailPanel = selected
    ? el(
        "section",
        { className: "fw-panel fw-selected-customer-panel" },
        el("h2", { className: "fw-panel-title", text: "Selected Customer" }),
        el("p", { className: "fw-selected-name", text: selected.full_name || selected.id }),
        el("p", { className: "fw-selected-meta", text: customerLabel(selected) }),
        el(
          "div",
          { className: "fw-chip-row" },
          selected.home_store_name ? el("span", { className: "fw-chip", text: selected.home_store_name }) : null,
          selected.loyalty_tier ? el("span", { className: "fw-chip subtle", text: selected.loyalty_tier }) : null,
          selected.sex ? el("span", { className: "fw-chip subtle", text: `sex: ${humanizeToken(selected.sex)}` }) : null,
          ...(Array.isArray(selected.preferred_categories)
            ? selected.preferred_categories
                .slice(0, 2)
                .map((category) =>
                  el("span", { className: "fw-chip subtle", text: `pref: ${humanizeToken(category)}` }),
                )
            : []),
          selected.match_reason ? el("span", { className: "fw-chip subtle", text: selected.match_reason }) : null,
        ),
        selected.preferred_occasions && selected.preferred_occasions.length
          ? el(
              "p",
              {
                className: "fw-empty fw-inline-meta",
                text: `Occasion preferences: ${selected.preferred_occasions
                  .slice(0, 3)
                  .map((item) => humanizeToken(item))
                  .join(", ")}`,
              },
            )
          : null,
        selected.size_preferences && Object.keys(selected.size_preferences).length
          ? el(
              "p",
              {
                className: "fw-empty fw-inline-meta",
                text: `Size preferences: ${Object.entries(selected.size_preferences)
                  .slice(0, 4)
                  .map(([key, value]) => `${humanizeToken(key)} ${value}`)
                  .join(", ")}`,
              },
            )
          : null,
        recommendationControls,
      )
    : el(
        "section",
        { className: "fw-panel fw-selected-customer-panel" },
        el("h2", { className: "fw-panel-title", text: "Selected Customer" }),
        el("p", { className: "fw-empty", text: "Pick a result to inspect details here." }),
      );
  const customerTab = state.ui.customerTab === "value" ? "value" : "recommendations";
  const customerTabs = [
    { id: "recommendations", label: "Recommendations" },
    { id: "value", label: "Value" },
  ];
  const activeCustomerTabIndex = Math.max(0, customerTabs.findIndex((tab) => tab.id === customerTab));
  const handleCustomerTabKeyDown = (event, index) => {
    let nextIndex = null;
    if (event.key === "ArrowRight") {
      nextIndex = (index + 1) % customerTabs.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex = (index - 1 + customerTabs.length) % customerTabs.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = customerTabs.length - 1;
    }
    if (nextIndex === null) {
      return;
    }
    event.preventDefault();
    const nextTabId = customerTabs[nextIndex].id;
    if (nextTabId === state.ui.customerTab) {
      return;
    }
    markUserInteraction();
    state.ui.customerTab = nextTabId;
    persistWidgetState();
    render();
  };
  const customerTabPanel = selected
    ? el(
        "section",
        { className: "fw-panel fw-customer-tabs-panel" },
        el(
          "div",
          {
            className: "fw-tabs fw-merch-tabs",
            role: "tablist",
            "aria-label": "Customer workspace views",
            "aria-orientation": "horizontal",
          },
          ...customerTabs.map((tab, index) => {
            const isActive = index === activeCustomerTabIndex;
            return el(
              "button",
              {
                id: `fw-customer-tab-${tab.id}`,
                className: `fw-tab fw-merch-tab ${isActive ? "active" : ""}`,
                type: "button",
                role: "tab",
                "aria-selected": isActive ? "true" : "false",
                "aria-controls": "fw-customer-view-panel",
                tabindex: isActive ? "0" : "-1",
                onKeydown: (event) => {
                  handleCustomerTabKeyDown(event, index);
                },
                onClick: () => {
                  if (tab.id === state.ui.customerTab) {
                    return;
                  }
                  markUserInteraction();
                  state.ui.customerTab = tab.id;
                  persistWidgetState();
                  render();
                },
              },
              tab.label,
            );
          }),
        ),
        el("p", {
          className: "fw-empty fw-inline-meta fw-customer-tab-hint",
          text:
            customerTab === "value"
              ? "Value is informational. Lookback controls determine spend and order totals."
              : "Recommendations is the primary associate workflow. Use Value as supporting context.",
        }),
      )
    : null;
  const customerValuePanel = selected && customerTab === "value"
    ? renderCustomerValuePanel(selected)
    : null;

  const recommendationPanel = selected && customerTab === "recommendations"
    ? (() => {
        const response =
          state.recommendation.customerId === selected.id ? state.recommendation.response : null;
        const rows = recommendationRows(response);
        const selectedProductIds = syncSelectedProducts(rows, state.recommendation.selectedProductIds, {
          seedWhenEmpty: false,
        });
        if (JSON.stringify(selectedProductIds) !== JSON.stringify(state.recommendation.selectedProductIds)) {
          state.recommendation.selectedProductIds = selectedProductIds;
          persistWidgetState();
        }
        const selectedProductCount = selectedProductIds.length;
        const allProductsSelected = rows.length > 0 && selectedProductCount === rows.length;
        const strategy = response?.recommendation?.strategy;
        const responseConstraints = normalizeStyleConstraints(response?.recommendation?.applied_style_constraints);
        const displayConstraints = responseConstraints || activeStyleConstraints;
        const constraintSource =
          response?.recommendation?.constraint_source ||
          (displayConstraints ? displayConstraints.constraint_source : null);
        const constraintStage = response?.recommendation?.constraint_stage;
        const strategyTagIntensity = response?.recommendation?.strategy_tag_intensity;
        const retrievalModeRaw = response?.retrieval_mode;
        const retrievalMode =
          typeof retrievalModeRaw === "string"
            ? retrievalModeRaw
            : typeof retrievalModeRaw?.value === "string"
              ? retrievalModeRaw.value
              : null;
        const preorderCount = rows.filter((item) => recommendationIsPreorder(item)).length;
        const inStockCount = rows.filter((item) => recommendationIsInStock(item)).length;
        const storeName =
          response?.store?.name ||
          selected.home_store_name ||
          selected.home_store_id ||
          "selected store";
        const stockSummaryText = rows.length
          ? preorderCount > 0
            ? `${storeName}: ${preorderCount} of ${rows.length} recommendations are preorder and not currently in stock. Out-of-stock items are excluded.`
            : `${storeName}: all ${inStockCount} recommendations are currently in stock. Out-of-stock items are excluded.`
          : "Out-of-stock items are excluded. Run recommendations to inspect preorder risk.";
        const emailDraftBusy =
          state.recommendation.isPreparingEmailDraft ||
          state.recommendation.isRefreshingEmailDraft ||
          state.recommendation.isUpdatingEmailDraft ||
          state.recommendation.isSendingEmailDraft;
        return el(
          "section",
          {
            className: "fw-panel",
            id: "fw-customer-view-panel",
            role: "tabpanel",
            "aria-labelledby": "fw-customer-tab-recommendations",
          },
          el("h2", { className: "fw-panel-title", text: "Product Recommendations" }),
          response
            ? el(
                "div",
                { className: "fw-chip-row" },
                strategy ? el("span", { className: "fw-chip", text: strategy }) : null,
                retrievalMode ? el("span", { className: "fw-chip subtle", text: retrievalMode }) : null,
                response?.recommendation?.strategy_packet_id
                  ? el("span", { className: "fw-chip subtle", text: `Strategy ${response.recommendation.strategy_packet_id}` })
                  : null,
                strategyTagIntensity
                  ? el("span", { className: "fw-chip subtle", text: `Intensity ${humanizeToken(strategyTagIntensity)}` })
                  : null,
              )
            : null,
          displayConstraints
            ? el(
                "div",
                { className: "fw-chip-row" },
                ...styleConstraintChips(displayConstraints, constraintSource, constraintStage),
              )
            : null,
          el("p", { className: "fw-empty fw-inline-meta", text: stockSummaryText }),
          response
            ? el(
                "div",
                { className: "fw-draft-grid" },
                el(
                  "div",
                  { className: "fw-draft-meta" },
                  el(
                    "p",
                    {
                      className: "fw-empty",
                      text: state.ui.emailDraftId
                        ? `Draft ID: ${state.ui.emailDraftId}`
                        : "Draft ID: not created yet",
                    },
                  ),
                ),
                el(
                  "div",
                  { className: "fw-field" },
                  el("label", { className: "fw-label", text: "Email To" }),
                  el("input", {
                    className: "fw-input",
                    type: "email",
                    value: state.ui.emailTo,
                    placeholder: "customer@example.com",
                    onInput: (event) => {
                      markUserInteraction();
                      state.ui.emailTo = event.target.value;
                      persistWidgetState();
                    },
                  }),
                ),
                el(
                  "div",
                  { className: "fw-field" },
                  el("label", { className: "fw-label", text: "Subject" }),
                  el("input", {
                    className: "fw-input",
                    type: "text",
                    value: state.ui.emailSubject,
                    placeholder: "A few picks I thought you'd love",
                    onInput: (event) => {
                      markUserInteraction();
                      state.ui.emailSubject = event.target.value;
                      persistWidgetState();
                    },
                  }),
                ),
                el(
                  "div",
                  { className: "fw-field" },
                  el("label", { className: "fw-label", text: "Body" }),
                  el("textarea", {
                    className: "fw-textarea",
                    rows: "8",
                    value: state.ui.emailBody,
                    placeholder: "Draft body text...",
                    onInput: (event) => {
                      markUserInteraction();
                      state.ui.emailBody = event.target.value;
                      persistWidgetState();
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
                      disabled: emailDraftBusy || !selectedProductCount || !state.ui.emailTo.trim() ? "true" : null,
                      onClick: () => {
                        void copyEmailDraft(selected);
                      },
                    },
                    "Copy Draft",
                  ),
                  el(
                    "button",
                    {
                      className: "fw-button secondary",
                      type: "button",
                      disabled: emailDraftBusy || !state.ui.emailDraftId ? "true" : null,
                      onClick: () => {
                        void refreshEmailDraft(selected);
                      },
                    },
                    state.recommendation.isRefreshingEmailDraft ? "Rebuilding..." : "Rebuild Draft",
                  ),
                  el(
                    "button",
                    {
                      className: "fw-button",
                      type: "button",
                      disabled: emailDraftBusy || !selectedProductCount || !state.ui.emailTo.trim() ? "true" : null,
                      onClick: () => {
                        void sendEmailDraft(selected);
                      },
                    },
                    state.recommendation.isSendingEmailDraft ? "Sending..." : "Send Draft Email",
                  ),
                ),
              )
            : null,
          state.recommendation.error
            ? el("p", { className: "fw-empty", text: state.recommendation.error })
            : null,
          response
            ? el(
                "div",
                { className: "fw-rec-bulk-row" },
                el("p", { className: "fw-empty", text: `${selectedProductCount} selected` }),
                el(
                  "div",
                  { className: "fw-rec-bulk-actions" },
                  el(
                    "button",
                    {
                      className: "fw-text-button",
                      type: "button",
                      disabled: !rows.length || allProductsSelected ? "true" : null,
                      onClick: () => {
                        selectAllProducts(response);
                        render();
                      },
                    },
                    "Select all",
                  ),
                  el(
                    "button",
                    {
                      className: "fw-text-button",
                      type: "button",
                      disabled: !selectedProductCount ? "true" : null,
                      onClick: () => {
                        deselectAllProducts();
                        render();
                      },
                    },
                    "Deselect",
                  ),
                ),
              )
            : null,
          ...(response
            ? recommendationCards(response)
            : [
                el(
                  "p",
                  {
                    className: "fw-empty",
                    text: "Use the controls above to generate recommendations for this customer.",
                  },
                ),
              ]),
        );
      })()
    : null;

  container.appendChild(
    el(
      "div",
      { className: "fw-root" },
      header,
      el(
        "section",
        { className: "fw-panel fw-controls-panel" },
        el("h2", { className: "fw-panel-title", text: "Customer Search" }),
        notice,
        toolbar,
        resultList,
      ),
      detailPanel,
      customerTabPanel,
      customerValuePanel,
      recommendationPanel,
    ),
  );
  if (selected) {
    queueSeedCustomerValue(selected);
    queueSeedRecommendations(selected);
    queueHydrateInitialEmailDraft(selected);
    queueSeedEmailDraft(selected);
    ensureDraftPolling(selected);
  } else {
    stopDraftPolling();
  }
}

function boot() {
  applyWorkspacePayload(meta.initialPayload);
  applyInitialToolOutput(window.openai && window.openai.toolOutput, { force: true });
  loadWidgetState();
  if (!state.ui.query && state.payload.query) {
    state.ui.query = state.payload.query;
  }
  queueModelContextUpdate({ force: true, immediate: true });
  render();
}

window.addEventListener(
  "openai:set_globals",
  (event) => {
    const globals = (event && event.detail && event.detail.globals) || {};
    const payloadChanged = applyInitialToolOutput(globals.toolOutput);
    const incomingDraft = normalizeEmailDraftResponse(globals.toolOutput);
    const draftChanged = applyIncomingDraftUpdate(incomingDraft, "chat");
    const uiChanged = applyUiWidgetState(globals.widgetState);
    if (uiChanged) {
      queueModelContextUpdate();
    }
    if (payloadChanged || draftChanged || uiChanged) {
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
    const incomingDraft = normalizeEmailDraftResponse(data.params);
    const draftChanged = applyIncomingDraftUpdate(incomingDraft, "chat");
    if (payloadChanged || draftChanged) {
      render();
    }
  },
  { passive: true },
);

window.addEventListener(
  "beforeunload",
  () => {
    stopDraftPolling();
    teardownAnalyticsCharts();
  },
  { passive: true },
);

boot();
