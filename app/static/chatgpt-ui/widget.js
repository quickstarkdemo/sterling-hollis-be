const root = document.getElementById("fashion-widget-root");
const meta = window.__FASHION_WIDGET__ || {};

const state = {
  payload: {
    query: "",
    mode: "idle",
    resolved: null,
    results: [],
    uiHints: {
      searchPlaceholder: "Search by name, email, or phone",
      emptyState: "Type a customer name, email, or phone number and run search.",
    },
  },
  ui: {
    query: "",
    selectedCustomerId: null,
    notice: "",
    noticeTone: "info",
    isSearching: false,
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
  if (!state.ui.query) {
    state.ui.query = payload.query;
  }
  return true;
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
}

function persistWidgetState() {
  if (!window.openai || typeof window.openai.setWidgetState !== "function") {
    return;
  }
  try {
    window.openai.setWidgetState({
      query: state.ui.query,
      selectedCustomerId: state.ui.selectedCustomerId,
    });
  } catch {
    // Best-effort only.
  }
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

function resolveRowSelection(results) {
  if (!results.length) {
    state.ui.selectedCustomerId = null;
    return;
  }
  if (state.ui.selectedCustomerId && results.some((customer) => customer.id === state.ui.selectedCustomerId)) {
    return;
  }
  state.ui.selectedCustomerId = results[0].id;
  persistWidgetState();
}

async function runSearch() {
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
    setNotice(`Resolved customer ${lookup.resolved.full_name || lookup.resolved.id}.`);
  } else {
    nextResults = Array.isArray(lookup.candidates) ? lookup.candidates : [];
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

  if (resolved && resolved.id) {
    state.ui.selectedCustomerId = resolved.id;
  } else if (nextResults.length) {
    if (!nextResults.some((row) => row.id === state.ui.selectedCustomerId)) {
      state.ui.selectedCustomerId = nextResults[0].id;
    }
  } else {
    state.ui.selectedCustomerId = null;
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
        state.ui.selectedCustomerId = customer.id;
        persistWidgetState();
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

function render() {
  const container = clear(root);
  const results = Array.isArray(state.payload.results) ? state.payload.results : [];
  resolveRowSelection(results);
  const selected = selectedCustomer(results);

  const searchInput = el("input", {
    className: "fw-input",
    type: "text",
    value: state.ui.query,
    placeholder: state.payload.uiHints.searchPlaceholder,
    onInput: (event) => {
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

  const header = el(
    "header",
    { className: "fw-hero" },
    el("p", { className: "fw-kicker", text: meta.kind || "customer_search_workspace" }),
    el("h1", { className: "fw-title", text: meta.title || "Customer Search Workspace" }),
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

  const resultList = results.length
    ? el("div", { className: "fw-list" }, ...results.map(renderCustomerRow))
    : el("div", { className: "fw-empty", text: state.payload.uiHints.emptyState });

  const detailPanel = selected
    ? el(
        "section",
        { className: "fw-panel" },
        el("h2", { className: "fw-panel-title", text: "Selected Customer" }),
        el("p", { className: "fw-selected-name", text: selected.full_name || selected.id }),
        el("p", { className: "fw-selected-meta", text: customerLabel(selected) }),
        el(
          "div",
          { className: "fw-chip-row" },
          selected.home_store_name ? el("span", { className: "fw-chip", text: selected.home_store_name }) : null,
          selected.loyalty_tier ? el("span", { className: "fw-chip subtle", text: selected.loyalty_tier }) : null,
          selected.match_reason ? el("span", { className: "fw-chip subtle", text: selected.match_reason }) : null,
        ),
      )
    : el(
        "section",
        { className: "fw-panel" },
        el("h2", { className: "fw-panel-title", text: "Selected Customer" }),
        el("p", { className: "fw-empty", text: "Pick a result to inspect details here." }),
      );

  container.appendChild(
    el(
      "div",
      { className: "fw-root" },
      header,
      el(
        "section",
        { className: "fw-panel" },
        el("h2", { className: "fw-panel-title", text: "Customer Search" }),
        notice,
        toolbar,
        resultList,
      ),
      detailPanel,
    ),
  );
}

function boot() {
  applyWorkspacePayload(meta.initialPayload);
  applyWorkspacePayload(window.openai && window.openai.toolOutput);
  loadWidgetState();
  if (!state.ui.query && state.payload.query) {
    state.ui.query = state.payload.query;
  }
  render();
}

window.addEventListener(
  "openai:set_globals",
  (event) => {
    const globals = (event && event.detail && event.detail.globals) || {};
    // Do not re-apply globals.toolOutput here; widgetState updates can fire
    // frequently and stale toolOutput snapshots can clobber interactive results.
    const uiChanged = applyUiWidgetState(globals.widgetState);
    if (uiChanged) {
      render();
    }
  },
  { passive: true },
);

window.addEventListener(
  "message",
  (event) => {
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
    if (applyWorkspacePayload(data.params)) {
      render();
    }
  },
  { passive: true },
);

boot();
