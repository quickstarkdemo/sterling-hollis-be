const root = document.getElementById('fashion-widget-root');
const rawState = document.getElementById('fashion-widget-state');
const meta = window.__FASHION_WIDGET__ || {};
const state = rawState ? JSON.parse(rawState.textContent || '{}') : { kind: 'unknown', payload: {} };

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
    alert('Tool bridge is unavailable in this environment.');
    return null;
  }
  return await window.openai.callTool(name, args);
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
    el('div', { className: 'fw-kicker', text: state.kind.replaceAll('_', ' ') }),
    el('h1', { className: 'fw-title', text: meta.title || 'Operator Workspace' }),
    el('p', { className: 'fw-subtitle', text: meta.summary || '' }),
  );
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
      el('span', { className: 'fw-chip subtle', text: (product.category || '').replaceAll('_', ' ') || 'curated edit' }),
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
    el('div', { className: 'fw-kicker', text: (product.category || '').replaceAll('_', ' ') }),
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
        el('div', { className: 'fw-kicker', text: customer.match_reason }),
        el('div', { className: 'fw-customer-name', text: customer.full_name }),
      ),
      el('div', { className: 'fw-score', text: `score ${Number(customer.match_score || 0).toFixed(1)}` }),
    ),
    el('div', { className: 'fw-meta', text: `${customer.email} • ${customer.masked_phone || maskPhone(customer.phone_e164)}` }),
    el('div', { className: 'fw-meta', text: `${customer.home_store_name} • ${customer.loyalty_tier}` }),
    el('div', { className: 'fw-card-actions' },
      el('button', { className: 'fw-button', text: 'Open Styling Session', onclick: () => onSelect(customer), type: 'button' })
    ),
  );
}

function renderShell(...sections) {
  const container = clear(root);
  container.appendChild(el('div', { className: 'fw-root' }, hero(), ...sections));
}

async function renderAssociateWorkspace() {
  const payloadState = state.payload;
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

  function toggleProduct(productId) {
    const next = new Set(payloadState.selectedProductIds || []);
    if (next.has(productId)) next.delete(productId);
    else next.add(productId);
    payloadState.selectedProductIds = [...next];
    syncWidgetState();
    renderAssociateWorkspace();
  }

  async function searchCustomers() {
    const query = searchInput.value.trim();
    if (!query) return;
    payloadState.customerQuery = query;
    const result = await callTool('fashion_lookup_customer', { query, limit: 10 });
    const resolved = payload(result);
    if (!resolved) return;
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
    };
    await syncWidgetState();
    await renderAssociateWorkspace();
  }

  async function refreshRecommendations() {
    if (!payloadState.selectedCustomer) return;
    const bootstrapResult = await callTool('fashion_associate_workspace_bootstrap', {
      store_id: payloadState.store.id,
      customer_id: payloadState.selectedCustomer.id,
      ...currentFilterArgs(),
    });
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
    };
    await syncWidgetState();
    await renderAssociateWorkspace();
  }

  async function draftSms() {
    if (!payloadState.selectedCustomer) return;
    const result = await callTool('fashion_prepare_customer_sms', {
      store_id: payloadState.store.id,
      customer_id: payloadState.selectedCustomer.id,
      ...currentFilterArgs(),
      selected_product_ids: payloadState.selectedProductIds,
    });
    const draft = payload(result);
    if (!draft) return;
    payloadState.lastDraft = draft;
    await syncWidgetState();
    await renderAssociateWorkspace();
  }

  async function openDraftReview() {
    if (!payloadState.lastDraft?.message?.id) return;
    await callTool('fashion_render_sms_review', { message_id: payloadState.lastDraft.message.id });
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
      el('div', { className: 'fw-grid two' },
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
        el('span', { className: 'fw-chip', text: selectedCustomer.loyalty_tier }),
        el('span', { className: 'fw-chip subtle', text: `Store ${payloadState.store.name}` }),
        el('span', { className: 'fw-chip subtle', text: `Mode ${filters.retrieval_mode || 'auto'}` }),
      ) : el('div', { className: 'fw-empty', text: 'The associate workspace stays empty until a customer is selected.' }),
      payloadState.lastDraft ? el('div', { className: 'fw-banner', text: `Draft ${payloadState.lastDraft.message.id} is ready. Live sends still go only to the configured test number.` }) : null,
    ),
  );

  const recommendationCards = recommendations.length
    ? recommendations.map((product) => productCard(product, payloadState.selectedProductIds || [], toggleProduct))
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
        ...(selectedCards.length ? selectedCards.map((product) => selectedCard(product, toggleProduct)) : [el('div', { className: 'fw-empty', text: 'No products selected yet.' })])
      ),
    ),
  );

  renderShell(el('div', { className: 'fw-split' }, leftColumn, rightColumn));
}

async function renderSmsReview() {
  const payloadState = state.payload;
  const message = payloadState.message;
  const selectedProducts = payloadState.selectedProducts || [];
  const textarea = el('textarea', { className: 'fw-textarea' });
  textarea.value = message.body_text || '';

  function removeProduct(productId) {
    payloadState.selectedProducts = selectedProducts.filter((item) => item.product_id !== productId);
    payloadState.message.product_ids = payloadState.selectedProducts.map((item) => item.product_id);
    syncWidgetState();
    renderSmsReview();
  }

  async function saveDraft() {
    const result = await callTool('fashion_update_customer_sms_draft', {
      message_id: message.id,
      body_text: textarea.value,
      selected_product_ids: (payloadState.selectedProducts || []).map((item) => item.product_id),
    });
    const updated = payload(result);
    if (!updated) return;
    payloadState.message = updated.message;
    await syncWidgetState();
    await renderSmsReview();
  }

  async function sendDraft() {
    const result = await callTool('fashion_send_customer_sms', { message_id: message.id });
    const updated = payload(result);
    if (!updated) return;
    payloadState.message = updated;
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
            ...(selectedProducts.length ? selectedProducts.map((product) => selectedCard(product, removeProduct)) : [el('div', { className: 'fw-empty', text: 'No products selected for this draft.' })])
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
  const filters = payloadState.filters || {};
  const questionInput = el('input', { className: 'fw-input', placeholder: 'What should this store feature, promote, or deprioritize?', value: filters.question || '' });
  const categoryInput = el('input', { className: 'fw-input', placeholder: 'womens_apparel, handbags...', value: filters.category || '' });
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
    const result = await callTool(toolName, { store_id: payloadState.store.id, ...payloadState.filters });
    const data = payload(result);
    if (!data) return;
    payloadState.lastTool = toolName;
    payloadState.lastResult = data;
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
                      availability: item.price_band ? item.price_band.replaceAll('_', ' ') : null,
                      link: item.link,
                      image_url: item.image_url,
                      reasons: [item.rationale, `Peer delta ${item.peer_delta}`, item.prior_period_delta !== null && item.prior_period_delta !== undefined ? `Prior-period delta ${item.prior_period_delta}` : null].filter(Boolean),
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
              el('div', { className: 'fw-kpi' }, el('span', { className: 'fw-small', text: 'Current' }), el('strong', { text: String(item.current_value) })),
              el('div', { className: 'fw-kpi' }, el('span', { className: 'fw-small', text: 'Peer' }), el('strong', { text: item.peer_value ?? '—' })),
              el('div', { className: 'fw-kpi' }, el('span', { className: 'fw-small', text: 'Prior' }), el('strong', { text: item.prior_value ?? '—' })),
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
              el('span', { className: 'fw-chip subtle', text: activeResult.compare_mode || 'comparison' }),
            ),
            el('h3', { className: 'fw-card-title', text: item.subject }),
            el('p', { className: 'fw-meta', text: item.rationale }),
            el('div', { className: 'fw-kpi-strip' },
              el('div', { className: 'fw-kpi' }, el('span', { className: 'fw-small', text: 'Current' }), el('strong', { text: String(item.current_value) })),
              el('div', { className: 'fw-kpi' }, el('span', { className: 'fw-small', text: 'Peer' }), el('strong', { text: item.peer_value ?? '—' })),
              el('div', { className: 'fw-kpi' }, el('span', { className: 'fw-small', text: 'Prior' }), el('strong', { text: item.prior_value ?? '—' })),
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
            el('div', { className: 'fw-grid two' },
              el('div', { className: 'fw-field' }, el('label', { className: 'fw-label', text: 'Question' }), questionInput),
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
              el('div', { className: 'fw-kpi' }, el('span', { className: 'fw-small', text: 'Compare mode' }), el('strong', { text: activeResult.compare_mode || filters.compare_mode || '—' })),
              el('div', { className: 'fw-kpi' }, el('span', { className: 'fw-small', text: 'Peer stores' }), el('strong', { text: String((activeResult.peer_store_ids || []).length || 0) })),
              el('div', { className: 'fw-kpi' }, el('span', { className: 'fw-small', text: 'Window' }), el('strong', { text: `${activeResult.lookback_days || filters.lookback_days || 90}d` })),
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

function boot() {
  if (state.kind === 'associate_workspace') return renderAssociateWorkspace();
  if (state.kind === 'sms') return renderSmsReview();
  if (state.kind === 'merch') return renderMerchBoard();
  renderShell(el('section', { className: 'fw-panel' }, el('div', { className: 'fw-empty', text: 'Unknown widget state.' })));
}

boot();
