const root = document.getElementById('fashion-widget-root');
const meta = window.__FASHION_WIDGET__ || {};
const state = { kind: meta.kind || 'unknown', payload: {} };
const HYDRATION_ATTEMPTS = 12;
const HYDRATION_DELAY_MS = 250;

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined) continue;
    if (key === 'className') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key === 'value') node.value = value;
    else if (key === 'checked') node.checked = Boolean(value);
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
    else node.setAttribute(key, String(value));
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

function clear(node) {
  node.innerHTML = '';
  return node;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function payload(result) {
  if (!result) return null;
  if (result.structuredContent) return result.structuredContent;
  try {
    const text = result.content && result.content[0] && result.content[0].text;
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
}

async function callTool(name, args = {}) {
  if (!window.openai || !window.openai.callTool) {
    return { __toolError: 'Tool bridge is unavailable in this environment.' };
  }
  try {
    return await window.openai.callTool(name, args);
  } catch (error) {
    return {
      __toolError: error instanceof Error ? error.message : 'Tool invocation failed.',
    };
  }
}

function toolError(result) {
  return result && typeof result === 'object' ? result.__toolError || null : null;
}

function isPlainObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value);
}

function normalizeHydratedState(raw) {
  if (!isPlainObject(raw)) return null;
  const candidate = isPlainObject(raw.structuredContent) ? raw.structuredContent : raw;
  if (!isPlainObject(candidate)) return null;
  if (candidate.kind && isPlainObject(candidate.payload)) return candidate;
  if (candidate.payload && isPlainObject(candidate.payload)) {
    return { kind: meta.kind || 'unknown', payload: candidate.payload };
  }
  if (candidate.result && isPlainObject(candidate.result)) {
    return normalizeHydratedState(candidate.result);
  }
  if (candidate.content && Array.isArray(candidate.content) && candidate.structuredContent) {
    return normalizeHydratedState(candidate.structuredContent);
  }
  return { kind: meta.kind || 'unknown', payload: candidate };
}

async function hydrateState() {
  const bridge = window.openai || {};
  let hydrated = normalizeHydratedState(bridge.toolOutput) || normalizeHydratedState(bridge.widgetState);
  let sessionId = hydrated?.payload?.widgetSessionId || hydrated?.widgetSessionId || meta.widgetSessionId;

  for (let attempt = 0; attempt < HYDRATION_ATTEMPTS; attempt += 1) {
    if (!hydrated && sessionId && meta.sessionEndpointBase) {
      try {
        const response = await fetch(`${meta.sessionEndpointBase}/${encodeURIComponent(sessionId)}.json`, {
          credentials: 'omit',
        });
        if (response.ok) hydrated = normalizeHydratedState(await response.json());
      } catch {
        // Best-effort rehydration only.
      }
    }

    if (hydrated) break;

    if (attempt < HYDRATION_ATTEMPTS - 1) {
      await sleep(HYDRATION_DELAY_MS);
      const nextBridge = window.openai || {};
      hydrated = normalizeHydratedState(nextBridge.toolOutput) || normalizeHydratedState(nextBridge.widgetState);
      sessionId = hydrated?.payload?.widgetSessionId || hydrated?.widgetSessionId || sessionId || meta.widgetSessionId;
    }
  }

  if (hydrated) {
    state.kind = hydrated.kind || state.kind;
    state.payload = hydrated.payload || {};
    sessionId = state.payload.widgetSessionId || sessionId;
  }

  if (sessionId && !state.payload.widgetSessionId) {
    state.payload.widgetSessionId = sessionId;
  }
}

function applyHydratedState(raw) {
  const hydrated = normalizeHydratedState(raw);
  if (!hydrated) return false;
  state.kind = hydrated.kind || state.kind;
  state.payload = hydrated.payload || {};
  if (state.payload.widgetSessionId || hydrated.widgetSessionId) {
    state.payload.widgetSessionId = state.payload.widgetSessionId || hydrated.widgetSessionId;
  }
  return true;
}

function attachHostListeners() {
  window.addEventListener('openai:set_globals', (event) => {
    const globals = event.detail?.globals || event.detail || {};
    if (applyHydratedState(globals.toolOutput) || applyHydratedState(globals.widgetState) || applyHydratedState(globals)) {
      boot();
    }
  });

  window.addEventListener('message', (event) => {
    const data = event.data;
    if (!data || typeof data !== 'object') return;
    const method = data.method || data.type;
    if (method === 'ui/notifications/tool-result' || method === 'tool_result') {
      const params = data.params || data.detail || data.result || data;
      if (applyHydratedState(params)) {
        boot();
      }
    }
  });
}

async function syncWidgetState() {
  if (window.openai && window.openai.setWidgetState) {
    try {
      await window.openai.setWidgetState(state.payload);
    } catch {
      // Best effort only.
    }
  }
}

function maskPhone(phone) {
  const digits = String(phone || '').replace(/\D/g, '');
  return digits.length >= 4 ? `(***) ***-${digits.slice(-4)}` : phone || '';
}

function money(value) {
  if (value === null || value === undefined || value === '') return 'Price unavailable';
  return `$${Number(value).toFixed(2)}`;
}

function compactNumber(value) {
  if (value === null || value === undefined || value === '') return '—';
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  return new Intl.NumberFormat('en-US', {
    maximumFractionDigits: Math.abs(numeric) >= 1000 ? 0 : 2,
  }).format(numeric);
}

function humanizeToken(value) {
  if (value === null || value === undefined || value === '') return '—';
  const raw = String(value).trim();
  const aliases = {
    womens_apparel: "Women's Apparel",
    mens_apparel: "Men's Apparel",
    under_250: 'Under $250',
    '250_500': '$250-$500',
    '500_1000': '$500-$1000',
    '1000_plus': '$1000+',
    peer_and_prior_period: 'Peer + Prior Period',
    prior_period: 'Prior Period',
    state_and_profile: 'State + Profile',
    profile_type: 'Profile Only',
    all_profile_matches: 'All Profile Matches',
  };
  if (aliases[raw]) return aliases[raw];
  return raw
    .replaceAll('_', ' ')
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function kpi(label, value) {
  return el('div', { className: 'fw-kpi' },
    el('span', { className: 'fw-kpi-label', text: label }),
    el('strong', { className: 'fw-kpi-value', text: value }),
  );
}

function sectionTitle(eyebrow, title, subtitle) {
  return el('div', { className: 'fw-section-head' },
    el('div', {},
      eyebrow ? el('div', { className: 'fw-kicker', text: eyebrow }) : null,
      el('h2', { text: title }),
      subtitle ? el('p', { className: 'fw-meta', text: subtitle }) : null,
    ),
  );
}

function hero() {
  return el('section', { className: 'fw-hero' },
    el('div', { className: 'fw-kicker', text: humanizeToken(state.kind) }),
    el('h1', { className: 'fw-title', text: meta.title || 'Operator Workspace' }),
    el('p', { className: 'fw-subtitle', text: meta.summary || defaultSummary() }),
  );
}

function defaultSummary() {
  if (state.kind === 'associate_workspace') {
    const customer = state.payload.selectedCustomer;
    return customer ? `${customer.full_name} • associate styling workspace` : 'Associate styling workspace';
  }
  if (state.kind === 'sms') {
    const customer = state.payload.customer;
    return customer ? `${customer.full_name} • SMS review` : 'SMS review workspace';
  }
  if (state.kind === 'merch') {
    const store = state.payload.store;
    return store ? `${store.name} • merchandising board` : 'Merchandising board';
  }
  return 'Operator workspace';
}

function renderFailure(message, detail = '') {
  renderShell(
    el('section', { className: 'fw-panel' },
      sectionTitle('widget state', meta.title || 'Workspace', message),
      el('div', { className: 'fw-empty', text: detail || 'The workspace mounted, but no renderable state reached the component.' }),
      state.payload.widgetSessionId ? el('div', { className: 'fw-meta', text: `session ${state.payload.widgetSessionId}` }) : null,
    ),
  );
}

function setUiNotice(payloadState, message, tone = 'info') {
  payloadState.uiNotice = message ? { message, tone } : null;
}

function noticeBanner(payloadState) {
  if (!payloadState.uiNotice?.message) return null;
  const tone = payloadState.uiNotice.tone === 'error' ? 'error' : 'info';
  return el('div', { className: `fw-banner ${tone === 'error' ? 'error' : ''}`, text: payloadState.uiNotice.message });
}

function productCard(product, selectedIds = [], onToggle = null) {
  const selected = selectedIds.includes(product.product_id);
  const actions = [];
  if (onToggle) {
    actions.push(el('button', {
      className: `fw-button ${selected ? 'alt' : 'muted'}`,
      text: selected ? 'Selected' : 'Select',
      onclick: () => onToggle(product.product_id),
      type: 'button',
    }));
  }
  if (product.link) {
    actions.push(el('a', { className: 'fw-button ghost', href: product.link, target: '_blank', rel: 'noreferrer' }, 'Open Product'));
  }
  const reasons = product.reasons && product.reasons.length
    ? el('ul', { className: 'fw-list' }, ...product.reasons.map((reason) => el('li', { text: reason })))
    : el('p', { className: 'fw-meta', text: 'Curated for fit, availability, and commercial relevance.' });
  return el('article', { className: 'fw-card fw-product-card' },
    el('div', { className: 'fw-product-visual' },
      el('img', { src: product.image_url || `${meta.assetBaseUrl}/demo/editorial-fallback.svg`, alt: product.title })
    ),
    el('div', { className: 'fw-chip-row' },
      el('span', { className: 'fw-chip subtle', text: humanizeToken(product.category || 'curated edit') }),
      product.availability ? el('span', { className: 'fw-chip subtle', text: product.availability }) : null,
    ),
    el('h3', { className: 'fw-card-title', text: product.title }),
    el('div', { className: 'fw-meta', text: `${product.brand} • ${money(product.price)}` }),
    reasons,
    el('div', { className: 'fw-card-actions' }, ...actions),
  );
}

function selectedCard(product, onRemove = null) {
  return el('article', { className: 'fw-selected-card' },
    el('img', { src: product.image_url || `${meta.assetBaseUrl}/demo/editorial-fallback.svg`, alt: product.title }),
    el('div', { className: 'fw-kicker', text: humanizeToken(product.category) }),
    el('h3', { className: 'fw-card-title', text: product.title }),
    el('div', { className: 'fw-meta', text: `${product.brand} • ${money(product.price)}` }),
    onRemove ? el('div', { className: 'fw-card-actions' },
      el('button', { className: 'fw-button ghost', text: 'Remove', onclick: () => onRemove(product.product_id), type: 'button' })
    ) : null,
  );
}

function customerCandidateCard(customer, onSelect) {
  return el('article', { className: 'fw-result fw-customer-card' },
    el('div', { className: 'fw-customer-top' },
      el('div', {},
        el('div', { className: 'fw-kicker', text: humanizeToken(customer.match_reason) }),
        el('div', { className: 'fw-customer-name', text: customer.full_name }),
      ),
      el('div', { className: 'fw-score', text: `match ${Number(customer.match_score || 0).toFixed(1)}` }),
    ),
    el('div', { className: 'fw-meta', text: `${customer.email} • ${customer.masked_phone || maskPhone(customer.phone_e164)}` }),
    el('div', { className: 'fw-meta', text: `${customer.home_store_name} • ${humanizeToken(customer.loyalty_tier)}` }),
    el('div', { className: 'fw-card-actions' },
      el('button', { className: 'fw-button', text: 'Open Styling Session', onclick: () => onSelect(customer), type: 'button' })
    ),
  );
}

function associateSummaryKpis(payloadState, filters, recommendations) {
  const selectedCustomer = payloadState.selectedCustomer;
  if (!selectedCustomer) return null;
  const budget = filters.budget_max ? `Up to ${money(filters.budget_max)}` : 'Open budget';
  return el('div', { className: 'fw-kpi-strip associate' },
    kpi('Loyalty tier', humanizeToken(selectedCustomer.loyalty_tier)),
    kpi('Retrieval mode', humanizeToken(filters.retrieval_mode || 'auto')),
    kpi('Budget', budget),
    kpi('Recommended', String(recommendations.length || 0)),
  );
}

function renderShell(...sections) {
  const container = clear(root);
  container.appendChild(el('div', { className: 'fw-root' }, hero(), ...sections));
}

async function renderAssociateWorkspace() {
  const payloadState = state.payload;
  if (!payloadState.store) {
    renderFailure('Associate workspace data did not hydrate.', 'Retry the tool call in a fresh message. If this persists, the host is not passing initial widget state to the component.');
    return;
  }
  const filters = payloadState.filters || {};
  const selectedProductIds = payloadState.selectedProductIds || [];

  const searchInput = el('input', { className: 'fw-input', placeholder: 'Search by name, email, or phone', value: payloadState.customerQuery || '' });
  const occasionInput = el('input', { className: 'fw-input', placeholder: 'Wedding guest, gala, business dinner...', value: filters.occasion || '' });
  const budgetMinInput = el('input', { className: 'fw-input', type: 'number', step: '0.01', placeholder: '0', value: filters.budget_min ?? '' });
  const budgetMaxInput = el('input', { className: 'fw-input', type: 'number', step: '0.01', placeholder: '900', value: filters.budget_max ?? '' });
  const topKInput = el('input', { className: 'fw-input', type: 'number', min: '1', max: '12', value: filters.top_k ?? 5 });
  const recommendations = payloadState.recommendation?.recommendation?.recommendations || [];

  function currentFilterArgs() {
    return {
      occasion: occasionInput.value.trim() || null,
      budget_min: budgetMinInput.value ? Number(budgetMinInput.value) : null,
      budget_max: budgetMaxInput.value ? Number(budgetMaxInput.value) : null,
      top_k: topKInput.value ? Number(topKInput.value) : 5,
      retrieval_mode: filters.retrieval_mode || 'auto',
    };
  }

  function selectedProductsFromRecommendations() {
    const byId = new Map(recommendations.map((item) => [item.product_id, item]));
    return selectedProductIds.map((id) => byId.get(id)).filter(Boolean);
  }

  async function toggleProduct(productId) {
    const next = new Set(payloadState.selectedProductIds || []);
    if (next.has(productId)) next.delete(productId);
    else next.add(productId);
    payloadState.selectedProductIds = [...next];
    setUiNotice(payloadState, `${payloadState.selectedProductIds.length} products selected for the draft.`);
    await syncWidgetState();
    await renderAssociateWorkspace();
  }

  async function searchCustomers() {
    const query = searchInput.value.trim();
    if (!query) {
      setUiNotice(payloadState, 'Enter a name, email, or phone number to search.', 'error');
      await syncWidgetState();
      await renderAssociateWorkspace();
      return;
    }
    payloadState.customerQuery = query;
    setUiNotice(payloadState, `Searching for ${query}…`);
    await syncWidgetState();
    const result = await callTool('fashion_lookup_customer', { query, limit: 10 });
    if (toolError(result)) {
      setUiNotice(payloadState, toolError(result), 'error');
      await syncWidgetState();
      await renderAssociateWorkspace();
      return;
    }
    const resolved = payload(result);
    if (!resolved) {
      setUiNotice(payloadState, 'Search returned no usable payload.', 'error');
      await syncWidgetState();
      await renderAssociateWorkspace();
      return;
    }
    if (resolved.mode === 'resolved' && resolved.resolved) {
      const bootstrapResult = await callTool('fashion_associate_workspace_bootstrap', {
        store_id: payloadState.store.id,
        customer_id: resolved.resolved.id,
        ...currentFilterArgs(),
      });
      const bootstrap = payload(bootstrapResult);
      if (bootstrap) {
        state.payload = {
          ...payloadState,
          store: bootstrap.store,
          filters: bootstrap.filters,
          customerQuery: query,
          customerResults: [],
          selectedCustomer: bootstrap.selected_customer,
          recommendation: bootstrap.recommendation,
          lastDraft: bootstrap.last_draft,
          selectedProductIds: bootstrap.selected_product_ids || [],
          uiNotice: { message: `Opened styling session for ${bootstrap.selected_customer?.full_name || query}.`, tone: 'info' },
        };
        await syncWidgetState();
        await renderAssociateWorkspace();
      }
      return;
    }
    payloadState.customerResults = resolved.candidates || [];
    payloadState.selectedCustomer = null;
    payloadState.recommendation = null;
    payloadState.selectedProductIds = [];
    setUiNotice(payloadState, `${payloadState.customerResults.length} customer matches found. Choose one to continue.`);
    await syncWidgetState();
    await renderAssociateWorkspace();
  }

  async function selectCustomer(candidate) {
    const bootstrapResult = await callTool('fashion_associate_workspace_bootstrap', {
      store_id: payloadState.store.id,
      customer_id: candidate.id,
      ...currentFilterArgs(),
    });
    const bootstrap = payload(bootstrapResult);
    if (!bootstrap) return;
    state.payload = {
      ...payloadState,
      store: bootstrap.store,
      filters: bootstrap.filters,
      customerResults: [],
      selectedCustomer: bootstrap.selected_customer,
      recommendation: bootstrap.recommendation,
      lastDraft: bootstrap.last_draft,
      selectedProductIds: bootstrap.selected_product_ids || [],
      uiNotice: { message: `Loaded recommendations for ${bootstrap.selected_customer?.full_name || candidate.full_name}.`, tone: 'info' },
    };
    await syncWidgetState();
    await renderAssociateWorkspace();
  }

  async function refreshRecommendations() {
    if (!payloadState.selectedCustomer) {
      setUiNotice(payloadState, 'Select a customer before refreshing recommendations.', 'error');
      await syncWidgetState();
      await renderAssociateWorkspace();
      return;
    }
    setUiNotice(payloadState, 'Refreshing recommendations…');
    await syncWidgetState();
    const bootstrapResult = await callTool('fashion_associate_workspace_bootstrap', {
      store_id: payloadState.store.id,
      customer_id: payloadState.selectedCustomer.id,
      ...currentFilterArgs(),
    });
    if (toolError(bootstrapResult)) {
      setUiNotice(payloadState, toolError(bootstrapResult), 'error');
      await syncWidgetState();
      await renderAssociateWorkspace();
      return;
    }
    const bootstrap = payload(bootstrapResult);
    if (!bootstrap) return;
    state.payload = {
      ...payloadState,
      store: bootstrap.store,
      filters: bootstrap.filters,
      selectedCustomer: bootstrap.selected_customer,
      recommendation: bootstrap.recommendation,
      lastDraft: bootstrap.last_draft,
      selectedProductIds: bootstrap.selected_product_ids || [],
      uiNotice: { message: `Recommendations refreshed for ${bootstrap.selected_customer?.full_name || payloadState.selectedCustomer.full_name}.`, tone: 'info' },
    };
    await syncWidgetState();
    await renderAssociateWorkspace();
  }

  async function draftSms() {
    if (!payloadState.selectedCustomer) {
      setUiNotice(payloadState, 'Select a customer before drafting SMS.', 'error');
      await syncWidgetState();
      await renderAssociateWorkspace();
      return;
    }
    setUiNotice(payloadState, 'Creating draft…');
    await syncWidgetState();
    const result = await callTool('fashion_prepare_customer_sms', {
      store_id: payloadState.store.id,
      customer_id: payloadState.selectedCustomer.id,
      ...currentFilterArgs(),
      selected_product_ids: payloadState.selectedProductIds,
    });
    if (toolError(result)) {
      setUiNotice(payloadState, toolError(result), 'error');
      await syncWidgetState();
      await renderAssociateWorkspace();
      return;
    }
    const draft = payload(result);
    if (!draft) {
      setUiNotice(payloadState, 'Draft creation returned no usable payload.', 'error');
      await syncWidgetState();
      await renderAssociateWorkspace();
      return;
    }
    payloadState.lastDraft = draft;
    setUiNotice(payloadState, `Draft ${draft.message.id} is ready for review.`);
    await syncWidgetState();
    await renderAssociateWorkspace();
  }

  async function openDraftReview() {
    if (!payloadState.lastDraft?.message?.id) {
      setUiNotice(payloadState, 'Create a draft before opening SMS review.', 'error');
      await syncWidgetState();
      await renderAssociateWorkspace();
      return;
    }
    const result = await callTool('fashion_render_sms_review', { message_id: payloadState.lastDraft.message.id });
    if (toolError(result)) {
      setUiNotice(payloadState, toolError(result), 'error');
      await syncWidgetState();
      await renderAssociateWorkspace();
    }
  }

  const selectedCustomer = payloadState.selectedCustomer;
  const leftColumn = el('div', { className: 'fw-column' },
    el('section', { className: 'fw-panel' },
      sectionTitle('client search', 'Customer Search', 'Use name, email, or phone. The lookup tool handles exact matches and ambiguous candidates.'),
      el('div', { className: 'fw-toolbar' },
        searchInput,
        el('button', { className: 'fw-button alt', text: 'Search', onclick: searchCustomers, type: 'button' }),
      ),
      el('div', { className: 'fw-search-results' },
        ...(payloadState.customerResults && payloadState.customerResults.length
          ? payloadState.customerResults.map((customer) => customerCandidateCard(customer, selectCustomer))
          : [el('div', { className: 'fw-empty', text: 'Search results will appear here. Exact hits open directly into the styling workspace.' })]
        ),
      ),
    ),
    el('section', { className: 'fw-panel' },
      sectionTitle('session controls', 'Styling Filters', 'Structured requests stay fast by default and skip vector retrieval when they already have enough context.'),
      noticeBanner(payloadState),
      el('div', { className: 'fw-grid associate-filters' },
        el('div', { className: 'fw-field' }, el('label', { className: 'fw-label', text: 'Occasion' }), occasionInput),
        el('div', { className: 'fw-field' }, el('label', { className: 'fw-label', text: 'Top picks' }), topKInput),
        el('div', { className: 'fw-field' }, el('label', { className: 'fw-label', text: 'Budget min' }), budgetMinInput),
        el('div', { className: 'fw-field' }, el('label', { className: 'fw-label', text: 'Budget max' }), budgetMaxInput),
      ),
      el('div', { className: 'fw-toolbar' },
        el('button', { className: 'fw-button', text: 'Refresh Recommendations', onclick: refreshRecommendations, type: 'button' }),
        selectedCustomer ? el('button', { className: 'fw-button alt', text: 'Draft SMS', onclick: draftSms, type: 'button' }) : null,
        payloadState.lastDraft ? el('button', { className: 'fw-button ghost', text: 'Open SMS Review', onclick: openDraftReview, type: 'button' }) : null,
      ),
    ),
    el('section', { className: 'fw-panel' },
      sectionTitle('current client', selectedCustomer ? selectedCustomer.full_name : 'No customer selected', selectedCustomer ? `${selectedCustomer.email} • ${maskPhone(selectedCustomer.phone_e164)} • ${selectedCustomer.home_store_name}` : 'Select a customer to start a styling session.'),
      selectedCustomer ? el('div', { className: 'fw-chip-row' },
        el('span', { className: 'fw-chip', text: humanizeToken(selectedCustomer.loyalty_tier) }),
        el('span', { className: 'fw-chip subtle', text: `Store ${payloadState.store.name}` }),
        el('span', { className: 'fw-chip subtle', text: `${humanizeToken(filters.retrieval_mode || 'auto')} retrieval` }),
      ) : el('div', { className: 'fw-empty', text: 'The associate workspace stays empty until a customer is selected.' }),
      selectedCustomer ? associateSummaryKpis(payloadState, filters, recommendations) : null,
      payloadState.lastDraft ? el('div', { className: 'fw-banner', text: `Draft ${payloadState.lastDraft.message.id} is ready. Live sends still go only to the configured test number.` }) : null,
    ),
  );

  const recommendationCards = recommendations.length
    ? recommendations.map((product) => productCard(product, payloadState.selectedProductIds || [], (productId) => { void toggleProduct(productId); }))
    : [el('div', { className: 'fw-empty', text: 'Recommendations appear after selecting a customer or resolving an exact lookup.' })];

  const selectedCards = selectedProductsFromRecommendations();
  const rightColumn = el('div', { className: 'fw-column' },
    el('section', { className: 'fw-panel' },
      sectionTitle('curated edit', 'Recommendations', recommendations.length ? `${recommendations.length} products ready to review` : 'Awaiting recommendations'),
      el('div', { className: 'fw-grid cards' }, ...recommendationCards),
    ),
    el('section', { className: 'fw-panel' },
      sectionTitle('selected products', 'Client Tray', selectedCards.length ? `${selectedCards.length} products selected for draft handoff` : 'Select products from the recommendation cards.'),
      el('div', { className: 'fw-selected-tray' },
        ...(selectedCards.length ? selectedCards.map((product) => selectedCard(product, (productId) => { void toggleProduct(productId); })) : [el('div', { className: 'fw-empty', text: 'No products selected yet.' })])
      ),
    ),
  );

  renderShell(el('div', { className: 'fw-split' }, leftColumn, rightColumn));
}

async function renderSmsReview() {
  const payloadState = state.payload;
  if (!payloadState.message || !payloadState.customer || !payloadState.store) {
    renderFailure('SMS review data did not hydrate.', 'The draft exists, but the widget did not receive the initial review payload.');
    return;
  }
  const message = payloadState.message;
  const selectedProducts = payloadState.selectedProducts || [];
  const textarea = el('textarea', { className: 'fw-textarea' });
  textarea.value = message.body_text || '';

  async function removeProduct(productId) {
    payloadState.selectedProducts = selectedProducts.filter((item) => item.product_id !== productId);
    payloadState.message.product_ids = payloadState.selectedProducts.map((item) => item.product_id);
    setUiNotice(payloadState, `${payloadState.selectedProducts.length} products remain in this draft.`);
    await syncWidgetState();
    await renderSmsReview();
  }

  async function saveDraft() {
    const result = await callTool('fashion_update_customer_sms_draft', {
      message_id: message.id,
      body_text: textarea.value,
      selected_product_ids: (payloadState.selectedProducts || []).map((item) => item.product_id),
    });
    if (toolError(result)) {
      setUiNotice(payloadState, toolError(result), 'error');
      await syncWidgetState();
      await renderSmsReview();
      return;
    }
    const updated = payload(result);
    if (!updated) {
      setUiNotice(payloadState, 'Draft update returned no usable payload.', 'error');
      await syncWidgetState();
      await renderSmsReview();
      return;
    }
    payloadState.message = updated.message;
    setUiNotice(payloadState, `Draft ${updated.message.id} saved.`);
    await syncWidgetState();
    await renderSmsReview();
  }

  async function sendDraft() {
    const result = await callTool('fashion_send_customer_sms', { message_id: message.id });
    if (toolError(result)) {
      setUiNotice(payloadState, toolError(result), 'error');
      await syncWidgetState();
      await renderSmsReview();
      return;
    }
    const updated = payload(result);
    if (!updated) {
      setUiNotice(payloadState, 'Send returned no usable payload.', 'error');
      await syncWidgetState();
      await renderSmsReview();
      return;
    }
    payloadState.message = updated;
    setUiNotice(payloadState, `Message ${updated.id} sent to the configured test number.`);
    await loadHistory();
  }

  async function loadHistory() {
    const result = await callTool('fashion_customer_message_history', { customer_id: payloadState.customer.id, limit: 10 });
    const history = payload(result);
    payloadState.history = history ? history.messages : [];
    await syncWidgetState();
    await renderSmsReview();
  }

  renderShell(
    el('div', { className: 'fw-split' },
      el('div', { className: 'fw-column' },
        el('section', { className: 'fw-panel' },
          sectionTitle('draft review', 'SMS Review', `${payloadState.customer.full_name} • ${payloadState.customer.email} • ${maskPhone(payloadState.customer.phone_e164)}`),
          el('div', { className: 'fw-banner', text: `This flow is operating in test mode. The live destination is ${message.destination_e164}, not the customer record.` }),
          noticeBanner(payloadState),
          el('div', { className: 'fw-chip-row' },
            el('span', { className: 'fw-chip', text: `Status ${message.status}` }),
            message.twilio_message_sid ? el('span', { className: 'fw-chip subtle', text: `SID ${message.twilio_message_sid}` }) : null,
            message.error_message ? el('span', { className: 'fw-chip subtle', text: `Error ${message.error_message}` }) : null,
          ),
          textarea,
          el('div', { className: 'fw-toolbar' },
            el('button', { className: 'fw-button', text: 'Save Draft', onclick: saveDraft, type: 'button' }),
            el('button', { className: 'fw-button alt', text: 'Send Test SMS', onclick: sendDraft, type: 'button' }),
            el('button', { className: 'fw-button ghost', text: 'Refresh History', onclick: loadHistory, type: 'button' }),
          ),
        )
      ),
      el('div', { className: 'fw-column' },
        el('section', { className: 'fw-panel' },
          sectionTitle('selected products', 'Included Products', selectedProducts.length ? `${selectedProducts.length} products in this message` : 'No selected products'),
          el('div', { className: 'fw-selected-tray' },
            ...(selectedProducts.length ? selectedProducts.map((product) => selectedCard(product, (productId) => { void removeProduct(productId); })) : [el('div', { className: 'fw-empty', text: 'No products selected for this draft.' })])
          ),
        ),
        el('section', { className: 'fw-panel' },
          sectionTitle('recent activity', 'Communication History', 'Latest drafts and sends for this customer.'),
          el('div', { className: 'fw-history' },
            ...((payloadState.history || []).length
              ? payloadState.history.map((item) => el('article', { className: 'fw-history-item' },
                  el('div', { className: 'fw-chip-row' },
                    el('span', { className: 'fw-chip', text: item.status }),
                    el('span', { className: 'fw-chip subtle', text: item.destination_e164 }),
                  ),
                  el('div', { className: 'fw-small', text: item.body_text.slice(0, 220) }),
                ))
              : [el('div', { className: 'fw-empty', text: 'Load history to view recent outreach.' })]
            ),
          ),
        ),
      ),
    ),
  );
}

async function renderMerchBoard() {
  const payloadState = state.payload;
  if (!payloadState.store || !payloadState.filters) {
    renderFailure('Merchandising board data did not hydrate.', 'The board mounted, but the initial merchandising payload is missing.');
    return;
  }
  const filters = payloadState.filters || {};
  const questionInput = el('input', { className: 'fw-input', placeholder: 'What should this store feature, promote, or deprioritize?', value: filters.question || '' });
  const categoryInput = el('input', { className: 'fw-input', placeholder: "Women's apparel, handbags...", value: filters.category || '' });
  const brandInput = el('input', { className: 'fw-input', placeholder: 'Brand', value: filters.brand || '' });
  const occasionInput = el('input', { className: 'fw-input', placeholder: 'wedding, vacation, workwear', value: filters.occasion || '' });
  const lookbackInput = el('input', { className: 'fw-input', type: 'number', min: '7', max: '730', value: filters.lookback_days ?? 90 });
  const topKInput = el('input', { className: 'fw-input', type: 'number', min: '3', max: '24', value: filters.top_k ?? 9 });

  function buildSelect(current, options) {
    const node = el('select', { className: 'fw-select' });
    for (const [label, value] of options) {
      const option = el('option', { value, text: label });
      if ((current || '') === value) option.selected = true;
      node.appendChild(option);
    }
    return node;
  }

  const priceBandSelect = buildSelect(filters.price_band || '', [
    ['Any', ''],
    ['Under $250', 'under_250'],
    ['$250-$500', '250_500'],
    ['$500-$1000', '500_1000'],
    ['$1000+', '1000_plus'],
  ]);
  const compareSelect = buildSelect(filters.compare_mode || 'peer_and_prior_period', [
    ['Peer + prior period', 'peer_and_prior_period'],
    ['Peer only', 'peer'],
    ['Prior period only', 'prior_period'],
  ]);
  const peerSelect = buildSelect(filters.peer_mode || 'state_and_profile', [
    ['State + profile', 'state_and_profile'],
    ['Profile only', 'profile_type'],
    ['All profile matches', 'all_profile_matches'],
  ]);

  function nextFilterState() {
    return {
      question: questionInput.value.trim() || null,
      category: categoryInput.value.trim() || null,
      brand: brandInput.value.trim() || null,
      price_band: priceBandSelect.value || null,
      occasion: occasionInput.value.trim() || null,
      lookback_days: Number(lookbackInput.value || 90),
      compare_mode: compareSelect.value,
      peer_mode: peerSelect.value,
      top_k: Number(topKInput.value || 9),
    };
  }

  async function refresh(toolName) {
    payloadState.filters = nextFilterState();
    setUiNotice(payloadState, `Loading ${toolName.replace('fashion_', '').replaceAll('_', ' ')}…`);
    await syncWidgetState();
    const result = await callTool(toolName, { store_id: payloadState.store.id, ...payloadState.filters });
    if (toolError(result)) {
      setUiNotice(payloadState, toolError(result), 'error');
      await syncWidgetState();
      await renderMerchBoard();
      return;
    }
    const data = payload(result);
    if (!data) {
      setUiNotice(payloadState, `The ${toolName.replace('fashion_', '').replaceAll('_', ' ')} tool returned no usable payload.`, 'error');
      await syncWidgetState();
      await renderMerchBoard();
      return;
    }
    payloadState.lastTool = toolName;
    payloadState.lastResult = data;
    setUiNotice(payloadState, `${humanizeToken(toolName.replace('fashion_merch_', '').replace('_summary', ''))} loaded.`);
    await syncWidgetState();
    await renderMerchBoard();
  }

  const activeTool = payloadState.lastTool || 'fashion_merch_action_recommendations';
  const activeResult = payloadState.lastResult || payloadState.initialResult;

  const actionsView = activeTool === 'fashion_merch_action_recommendations'
    ? (() => {
        const groups = { feature: [], promote: [], deprioritize: [] };
        for (const item of activeResult.recommendations || []) groups[item.action].push(item);
        return el('div', { className: 'fw-merch-groups' },
          ...['feature', 'promote', 'deprioritize'].map((action) =>
            el('section', { className: 'fw-panel' },
              el('div', { className: 'fw-section-head' },
                el('div', {}, el('div', { className: 'fw-kicker', text: action }), el('h3', { text: action[0].toUpperCase() + action.slice(1) })),
                el('span', { className: 'fw-chip subtle', text: `${groups[action].length} products` }),
              ),
              el('div', { className: 'fw-grid cards' },
                ...(groups[action].length
                  ? groups[action].map((item) => productCard({
                      product_id: item.product_id,
                      title: item.title,
                      brand: item.brand,
                      category: item.category,
                      price: item.price,
                      availability: item.price_band ? humanizeToken(item.price_band) : null,
                      link: item.link,
                      image_url: item.image_url,
                      reasons: [
                        item.rationale,
                        `Peer delta ${compactNumber(item.peer_delta)}`,
                        item.prior_period_delta !== null && item.prior_period_delta !== undefined
                          ? `Prior-period delta ${compactNumber(item.prior_period_delta)}`
                          : null,
                      ].filter(Boolean),
                    }))
                  : [el('div', { className: 'fw-empty', text: 'No products in this action group for the current filters.' })]
                ),
              ),
            )
          ),
        );
      })()
    : null;

  const diagnosticsView = activeTool === 'fashion_merch_diagnostics'
    ? el('div', { className: 'fw-history' },
        ...((activeResult.insights || []).map((item) =>
          el('article', { className: 'fw-result' },
            el('div', { className: 'fw-chip-row' },
              el('span', { className: 'fw-chip', text: item.status }),
              el('span', { className: 'fw-chip subtle', text: item.dimension }),
            ),
            el('h3', { className: 'fw-card-title', text: item.subject }),
            el('p', { className: 'fw-meta', text: item.rationale }),
            el('div', { className: 'fw-kpi-strip' },
              kpi('Current', compactNumber(item.current_value)),
              kpi('Peer', compactNumber(item.peer_value)),
              kpi('Prior', compactNumber(item.prior_value)),
            ),
          )
        ))
      )
    : null;

  const trendsView = activeTool === 'fashion_merch_trend_summary'
    ? el('div', { className: 'fw-history' },
        ...((activeResult.highlights || []).map((item) =>
          el('article', { className: 'fw-result' },
            el('div', { className: 'fw-chip-row' },
              el('span', { className: 'fw-chip', text: `Δ ${item.pct_change}%` }),
              el('span', { className: 'fw-chip subtle', text: humanizeToken(activeResult.compare_mode || 'comparison') }),
            ),
            el('h3', { className: 'fw-card-title', text: item.subject }),
            el('p', { className: 'fw-meta', text: item.rationale }),
            el('div', { className: 'fw-kpi-strip' },
              kpi('Current', compactNumber(item.current_value)),
              kpi('Peer', compactNumber(item.peer_value)),
              kpi('Prior', compactNumber(item.prior_value)),
            ),
          )
        ))
      )
    : null;

  renderShell(
    el('div', { className: 'fw-root' },
      el('div', { className: 'fw-split' },
        el('div', { className: 'fw-column' },
          el('section', { className: 'fw-panel' },
            sectionTitle('filters', 'Merchandising Board', `Store ${payloadState.store.name} • peer-aware and prior-period aware analysis`),
            noticeBanner(payloadState),
            el('div', { className: 'fw-grid merch-filters' },
              el('div', { className: 'fw-field fw-span-full' }, el('label', { className: 'fw-label', text: 'Question' }), questionInput),
              el('div', { className: 'fw-field' }, el('label', { className: 'fw-label', text: 'Category' }), categoryInput),
              el('div', { className: 'fw-field' }, el('label', { className: 'fw-label', text: 'Brand' }), brandInput),
              el('div', { className: 'fw-field' }, el('label', { className: 'fw-label', text: 'Price band' }), priceBandSelect),
              el('div', { className: 'fw-field' }, el('label', { className: 'fw-label', text: 'Occasion' }), occasionInput),
              el('div', { className: 'fw-field' }, el('label', { className: 'fw-label', text: 'Lookback days' }), lookbackInput),
              el('div', { className: 'fw-field' }, el('label', { className: 'fw-label', text: 'Compare mode' }), compareSelect),
              el('div', { className: 'fw-field' }, el('label', { className: 'fw-label', text: 'Peer mode' }), peerSelect),
              el('div', { className: 'fw-field' }, el('label', { className: 'fw-label', text: 'Top K' }), topKInput),
            ),
            el('div', { className: 'fw-toolbar' },
              el('button', { className: `fw-tab ${activeTool === 'fashion_merch_action_recommendations' ? 'active' : ''}`, text: 'Actions', onclick: () => refresh('fashion_merch_action_recommendations'), type: 'button' }),
              el('button', { className: `fw-tab ${activeTool === 'fashion_merch_diagnostics' ? 'active' : ''}`, text: 'Diagnostics', onclick: () => refresh('fashion_merch_diagnostics'), type: 'button' }),
              el('button', { className: `fw-tab ${activeTool === 'fashion_merch_trend_summary' ? 'active' : ''}`, text: 'Trends', onclick: () => refresh('fashion_merch_trend_summary'), type: 'button' }),
            ),
          ),
          activeResult ? el('section', { className: 'fw-panel' },
            sectionTitle('context', 'Current Frame', activeResult.summary || activeResult.parsed_intent || 'Commercial context'),
            el('div', { className: 'fw-kpi-strip' },
              kpi('Compare mode', humanizeToken(activeResult.compare_mode || filters.compare_mode || '—')),
              kpi('Peer stores', String((activeResult.peer_store_ids || []).length || 0)),
              kpi('Window', `${activeResult.lookback_days || filters.lookback_days || 90}d`),
            ),
          ) : null,
        ),
        el('div', { className: 'fw-column' },
          actionsView || diagnosticsView || trendsView || el('section', { className: 'fw-panel' }, el('div', { className: 'fw-empty', text: 'Run an action, diagnostic, or trend view to populate the board.' })),
        ),
      ),
    ),
  );
}

let booting = false;
async function boot() {
  if (booting) return;
  booting = true;
  await hydrateState();
  if (!Object.keys(state.payload || {}).length) {
    renderFailure('Workspace mounted without initial state.', 'This usually means the host did not pass tool output or session state to the widget.');
    booting = false;
    return;
  }
  if (state.kind === 'associate_workspace') {
    booting = false;
    return renderAssociateWorkspace();
  }
  if (state.kind === 'sms') {
    booting = false;
    return renderSmsReview();
  }
  if (state.kind === 'merch') {
    booting = false;
    return renderMerchBoard();
  }
  renderShell(el('section', { className: 'fw-panel' }, el('div', { className: 'fw-empty', text: 'Unknown widget state.' })));
  booting = false;
}

attachHostListeners();
boot();
