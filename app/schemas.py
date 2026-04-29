from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator


class SyntheticVolumes(BaseModel):
    stores: int = 36
    products: int = 6000
    customers: int = 12000
    orders: int = 80000
    supplier_product_offers: int = 1200


class SyntheticGenerateRequest(BaseModel):
    seed: int = 20260313
    trailing_months: int = Field(default=24, ge=1, le=60)
    volumes: SyntheticVolumes = Field(default_factory=SyntheticVolumes)
    profile_overrides: dict[str, float] = Field(default_factory=dict)


class SyntheticGenerateResponse(BaseModel):
    run_id: str
    seed: int
    output_dir: str
    row_counts: dict[str, int]
    stores_discovered: int


class SyntheticLoadRequest(BaseModel):
    run_id: str
    entities: list[str] = Field(
        default_factory=lambda: [
            "stores",
            "customers",
            "products",
            "orders",
            "order_items",
            "store_daily_metrics",
            "supplier_product_offers",
        ]
    )


class SyntheticLoadResponse(BaseModel):
    run_id: str
    loaded_rows: dict[str, int]


class IndexProductsRequest(BaseModel):
    run_id: str
    batch_size: int = Field(default=128, ge=1, le=1000)


class IndexProductsResponse(BaseModel):
    run_id: str
    attempted: int
    indexed: int
    failed: int
    status_breakdown: dict[str, int]


class RunReportResponse(BaseModel):
    run_id: str
    status: str
    generated_counts: dict[str, int]
    loaded_counts: dict[str, int]
    embedding_coverage: dict[str, int | float]
    validation_failures: int
    generated_at: datetime


class VectorProviderStatus(BaseModel):
    configured: bool
    client_available: bool
    enabled: bool
    probe_attempted: bool
    probe_ok: bool | None = None
    probe_error: str | None = None
    model: str | None = None
    dimension: int | None = None
    index_name: str | None = None
    cloud: str | None = None
    region: str | None = None


class VectorStatusResponse(BaseModel):
    mode: str
    openai: VectorProviderStatus
    pinecone: VectorProviderStatus


class Objective(str, Enum):
    sell_through = "sell_through"
    margin = "margin"
    revenue = "revenue"


class CompareMode(str, Enum):
    peer = "peer"
    prior_period = "prior_period"
    peer_and_prior_period = "peer_and_prior_period"


class PeerMode(str, Enum):
    profile_type = "profile_type"
    state_and_profile = "state_and_profile"
    all_profile_matches = "all_profile_matches"


class PriceBand(str, Enum):
    under_250 = "under_250"
    band_250_500 = "250_500"
    band_500_1000 = "500_1000"
    band_1000_plus = "1000_plus"


class RetrievalMode(str, Enum):
    auto = "auto"
    fast = "fast"
    semantic = "semantic"


class IndexJobStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class StyleConstraints(BaseModel):
    constraint_source: str | None = None
    target_categories: list[str] = Field(default_factory=list)
    exclude_categories: list[str] = Field(default_factory=list)
    target_genders: list[str] = Field(default_factory=list)
    style_keywords: list[str] = Field(default_factory=list)

    @staticmethod
    def _clean_string_list(values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = str(value).strip().lower()
            if not normalized or normalized in seen:
                continue
            cleaned.append(normalized)
            seen.add(normalized)
        return cleaned

    @model_validator(mode="after")
    def normalize(self) -> "StyleConstraints":
        self.target_categories = self._clean_string_list(self.target_categories)
        self.exclude_categories = self._clean_string_list(self.exclude_categories)
        self.style_keywords = self._clean_string_list(self.style_keywords)

        mapped_genders: list[str] = []
        for value in self.target_genders:
            token = str(value).strip().lower()
            if token in {"male", "man", "men", "m", "boys", "boy"}:
                mapped_genders.append("male")
            elif token in {"female", "woman", "women", "f", "girls", "girl"}:
                mapped_genders.append("female")
            elif token in {"unisex", "neutral", "gender_neutral", "gender-neutral"}:
                mapped_genders.append("unisex")
        self.target_genders = self._clean_string_list(mapped_genders)

        if self.constraint_source is not None:
            source = str(self.constraint_source).strip().lower()
            self.constraint_source = source or None
        return self

    def is_empty(self) -> bool:
        return not (
            self.target_categories
            or self.exclude_categories
            or self.target_genders
            or self.style_keywords
        )


class CustomerRecommendationRequest(BaseModel):
    store_id: str
    customer_id: str | None = None
    occasion: str | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    top_k: int = Field(default=12, ge=1, le=50)
    style_constraints: StyleConstraints | None = None

    @model_validator(mode="after")
    def validate_budget(self) -> "CustomerRecommendationRequest":
        if self.budget_min is not None and self.budget_max is not None and self.budget_min > self.budget_max:
            raise ValueError("budget_min cannot be greater than budget_max")
        return self


class ProductRecommendation(BaseModel):
    product_id: str
    title: str
    brand: str
    category: str
    price: float
    availability: str
    link: str | None = None
    image_url: str | None = None
    score: float
    reasons: list[str]
    execution_tags: list[str] = Field(default_factory=list)


class StrategyTagIntensity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class CustomerRecommendationResponse(BaseModel):
    store_id: str
    strategy: str
    recommendations: list[ProductRecommendation]
    strategy_packet_id: str | None = None
    strategy_tag_intensity: StrategyTagIntensity | None = None
    applied_style_constraints: StyleConstraints | None = None
    constraint_source: str | None = None
    constraint_stage: str | None = None


class MerchandisingRecommendationRequest(BaseModel):
    store_id: str
    objective: Objective = Objective.sell_through
    lookback_days: int = Field(default=90, ge=7, le=730)
    top_k: int = Field(default=20, ge=1, le=100)


class MerchandisingRecommendationRow(BaseModel):
    product_id: str
    title: str
    category: str
    metric_value: float
    rationale: str


class MerchandisingRecommendationResponse(BaseModel):
    store_id: str
    objective: Objective
    recommendations: list[MerchandisingRecommendationRow]


class ResolvedStore(BaseModel):
    id: str
    name: str
    city: str
    state: str
    profile_type: str
    match_reason: str
    match_score: float


class StoreResolutionResponse(BaseModel):
    query: str
    resolved: ResolvedStore
    alternatives: list[ResolvedStore] = Field(default_factory=list)


class ResolvedCustomer(BaseModel):
    id: str
    email: str
    phone_e164: str
    full_name: str
    first_name: str
    last_name: str
    home_store_id: str
    home_store_name: str
    loyalty_tier: str
    sex: str | None = None
    preferred_categories: list[str] = Field(default_factory=list)
    preferred_occasions: list[str] = Field(default_factory=list)
    size_preferences: dict[str, str] = Field(default_factory=dict)
    match_reason: str


class CustomerSearchResult(ResolvedCustomer):
    masked_phone: str
    match_score: float


class CustomerSearchResponse(BaseModel):
    query: str
    results: list[CustomerSearchResult]


class CustomerResolutionResponse(BaseModel):
    query: str
    resolved: ResolvedCustomer


class CustomerLookupResponse(BaseModel):
    query: str
    mode: str
    resolved: ResolvedCustomer | None = None
    candidates: list[CustomerSearchResult] = Field(default_factory=list)


class PurchaseScope(str, Enum):
    all_stores = "all_stores"


class CustomerValueMetrics(BaseModel):
    value_score: float
    value_tier: str
    lifetime_spend: float
    lookback_spend: float
    lifetime_orders: int
    lookback_orders: int
    aov: float
    recency_days: float | None = None


class CustomerValuePoint(BaseModel):
    period_start: str
    value_score: float


class CustomerPurchasePoint(BaseModel):
    period_start: str
    spend: float
    orders: int


class CustomerForecastPoint(BaseModel):
    period_start: str
    projected_spend: float
    low_spend: float
    high_spend: float


class CustomerValueSummaryRequest(BaseModel):
    customer_id: str
    lookback_days: int = Field(default=180, ge=30, le=730)
    forecast_weeks: int = Field(default=8, ge=1, le=26)
    purchase_scope: PurchaseScope = PurchaseScope.all_stores


class CustomerValueSummaryResponse(BaseModel):
    customer: ResolvedCustomer
    lookback_days: int
    forecast_weeks: int
    purchase_scope: PurchaseScope
    metrics: CustomerValueMetrics
    value_series: list[CustomerValuePoint] = Field(default_factory=list)
    purchase_series: list[CustomerPurchasePoint] = Field(default_factory=list)
    forecast_series: list[CustomerForecastPoint] = Field(default_factory=list)


class StoreAssociateRecommendationResponse(BaseModel):
    store: ResolvedStore
    customer: ResolvedCustomer
    recommendation: CustomerRecommendationResponse
    retrieval_mode: RetrievalMode = RetrievalMode.auto


class CustomerCommunicationStatus(str, Enum):
    draft = "draft"
    sent = "sent"
    failed = "failed"


class CustomerCommunicationRecord(BaseModel):
    id: str
    customer_id: str
    customer_email: str
    customer_phone_e164: str
    store_id: str
    channel: str
    status: CustomerCommunicationStatus
    destination_e164: str
    subject: str | None = None
    body_text: str
    product_ids: list[str]
    twilio_message_sid: str | None = None
    error_message: str | None = None
    created_at: datetime
    sent_at: datetime | None = None


class UiProductCard(BaseModel):
    product_id: str
    title: str
    brand: str
    category: str
    price: float | None = None
    availability: str | None = None
    link: str | None = None
    image_url: str | None = None
    reasons: list[str] = Field(default_factory=list)


class CustomerCommunicationDraftResponse(BaseModel):
    message: CustomerCommunicationRecord
    store: ResolvedStore
    customer: ResolvedCustomer
    recommendation: CustomerRecommendationResponse
    retrieval_mode: RetrievalMode = RetrievalMode.auto


class CustomerCommunicationUpdateResponse(BaseModel):
    message: CustomerCommunicationRecord
    store: ResolvedStore
    customer: ResolvedCustomer


class CustomerCommunicationHistoryResponse(BaseModel):
    customer: ResolvedCustomer
    messages: list[CustomerCommunicationRecord]


class CustomerEmailSendResponse(BaseModel):
    message: CustomerCommunicationRecord
    store: ResolvedStore
    customer: ResolvedCustomer
    destination_email: str
    subject: str
    selected_products: list[UiProductCard] = Field(default_factory=list)
    provider_message_id: str | None = None


class CustomerEmailDraftResponse(BaseModel):
    message: CustomerCommunicationRecord
    store: ResolvedStore
    customer: ResolvedCustomer
    destination_email: str
    subject: str
    selected_products: list[UiProductCard] = Field(default_factory=list)
    recommendation: CustomerRecommendationResponse | None = None
    retrieval_mode: RetrievalMode = RetrievalMode.auto


class TwilioSmokeTestRecord(BaseModel):
    id: str
    destination_e164: str
    body_text: str
    status: CustomerCommunicationStatus
    twilio_message_sid: str | None = None
    error_message: str | None = None
    created_at: datetime
    sent_at: datetime | None = None


class TwilioSmokeTestResponse(BaseModel):
    result: TwilioSmokeTestRecord


class IndexJobResponse(BaseModel):
    id: str
    run_id: str
    batch_size: int
    status: IndexJobStatus
    attempted: int
    indexed: int
    failed_count: int
    status_breakdown: dict[str, int]
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class IndexJobListResponse(BaseModel):
    jobs: list[IndexJobResponse]


class ImageGenerationJobRequest(BaseModel):
    run_id: str | None = None
    store_id: str | None = None
    product_id: str | None = None
    variant_id: str | None = None
    category: str | None = None
    brand: str | None = None
    limit: int = Field(default=20, ge=1, le=500)
    detail_count: int | None = Field(default=None, ge=1, le=10)
    thumbnail_size: int | None = Field(default=None, ge=96, le=1024)
    overwrite: bool = False
    missing_images_only: bool = True
    model: str | None = None
    size: str | None = None
    quality: str | None = None
    output_format: str | None = None


class ImageGenerationJobResponse(BaseModel):
    id: str
    run_id: str | None = None
    store_id: str | None = None
    product_id: str | None = None
    variant_id: str | None = None
    category: str | None = None
    brand: str | None = None
    limit: int
    detail_count: int
    thumbnail_size: int
    overwrite: bool
    missing_images_only: bool
    model: str
    size: str
    quality: str
    output_format: str
    status: IndexJobStatus
    attempted: int
    generated: int
    skipped: int
    failed_count: int
    status_breakdown: dict[str, int]
    result_sample: list[dict] = Field(default_factory=list)
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ImageGenerationJobListResponse(BaseModel):
    jobs: list[ImageGenerationJobResponse]


class AssociateWorkspaceFilters(BaseModel):
    occasion: str | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    top_k: int = 5
    retrieval_mode: RetrievalMode = RetrievalMode.auto


class AssociateWorkspaceBootstrapResponse(BaseModel):
    store: ResolvedStore
    filters: AssociateWorkspaceFilters
    customer_query: str = ""
    customer_results: list[CustomerSearchResult] = Field(default_factory=list)
    selected_customer: ResolvedCustomer | None = None
    recommendation: StoreAssociateRecommendationResponse | None = None
    last_draft: CustomerCommunicationDraftResponse | None = None
    selected_product_ids: list[str] = Field(default_factory=list)


class SmsReviewBootstrapResponse(BaseModel):
    message: CustomerCommunicationRecord
    store: ResolvedStore
    customer: ResolvedCustomer
    selected_products: list[UiProductCard] = Field(default_factory=list)
    history: list[CustomerCommunicationRecord] = Field(default_factory=list)


class InventoryScope(str, Enum):
    current = "current"
    potential = "potential"
    combined = "combined"


class SupplierOfferStatus(str, Enum):
    potential = "potential"
    committed = "committed"
    launched = "launched"


class MerchFinalAction(str, Enum):
    feature = "feature"
    promote = "promote"
    deprioritize = "deprioritize"
    drop = "drop"


class MerchPriorityTier(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class MerchRecommendationOverride(BaseModel):
    product_id: str
    final_action: MerchFinalAction
    priority_tier: MerchPriorityTier
    override_note: str | None = None


class MerchWorkspaceFilters(BaseModel):
    question: str | None = None
    objective: Objective = Objective.margin
    category: str | None = None
    brand: str | None = None
    price_band: PriceBand | None = None
    occasion: str | None = None
    occasions: list[str] = Field(default_factory=list)
    lookback_days: int = 90
    inventory_scope: InventoryScope = InventoryScope.combined
    future_window_days: int = Field(default=120, ge=1, le=365)
    compare_mode: CompareMode = CompareMode.peer_and_prior_period
    peer_mode: PeerMode = PeerMode.state_and_profile
    compare_store_id: str | None = None
    top_k: int = 9


class MerchAction(str, Enum):
    feature = "feature"
    deprioritize = "deprioritize"
    promote = "promote"


class MerchWorkspaceView(str, Enum):
    actions = "actions"
    diagnostics = "diagnostics"
    trends = "trends"
    inventory = "inventory"
    mix_analysis = "mix_analysis"


class MerchActionRecommendationItem(BaseModel):
    action: MerchAction
    product_id: str
    title: str
    brand: str
    category: str
    price: float | None = None
    link: str | None = None
    image_url: str | None = None
    price_band: PriceBand | None = None
    occasion: str | None = None
    metric_value: float
    peer_delta: float
    prior_period_delta: float | None = None
    rationale: str


class MerchActionRecommendationsResponse(BaseModel):
    store: ResolvedStore
    objective: Objective
    compare_mode: CompareMode
    peer_mode: PeerMode
    lookback_days: int
    category: str | None = None
    brand: str | None = None
    price_band: PriceBand | None = None
    occasion: str | None = None
    occasions: list[str] = Field(default_factory=list)
    peer_store_ids: list[str]
    compare_store_id: str | None = None
    compare_store_name: str | None = None
    parsed_intent: str
    recommendations: list[MerchActionRecommendationItem]


class MerchDiagnosticInsight(BaseModel):
    dimension: str
    subject: str
    status: str
    current_value: float
    peer_value: float | None = None
    prior_value: float | None = None
    delta: float
    current_units: float | None = None
    peer_units: float | None = None
    prior_units: float | None = None
    current_margin_pct: float | None = None
    peer_margin_pct: float | None = None
    prior_margin_pct: float | None = None
    rationale: str


class MerchDiagnosticsResponse(BaseModel):
    store: ResolvedStore
    compare_mode: CompareMode
    peer_mode: PeerMode
    lookback_days: int
    category: str | None = None
    brand: str | None = None
    price_band: PriceBand | None = None
    occasion: str | None = None
    occasions: list[str] = Field(default_factory=list)
    peer_store_ids: list[str]
    compare_store_id: str | None = None
    compare_store_name: str | None = None
    summary: str
    insights: list[MerchDiagnosticInsight]


class MerchTrendHighlight(BaseModel):
    subject: str
    current_value: float
    peer_value: float | None = None
    prior_value: float | None = None
    pct_change: float
    rationale: str


class MerchTrendPoint(BaseModel):
    period_start: str
    current_revenue: float
    baseline_revenue: float | None = None
    current_units: float
    baseline_units: float | None = None


class MerchTrendSummaryResponse(BaseModel):
    store: ResolvedStore
    compare_mode: CompareMode
    peer_mode: PeerMode
    lookback_days: int
    category: str | None = None
    brand: str | None = None
    price_band: PriceBand | None = None
    occasion: str | None = None
    occasions: list[str] = Field(default_factory=list)
    peer_store_ids: list[str] = Field(default_factory=list)
    compare_store_id: str | None = None
    compare_store_name: str | None = None
    summary: str
    highlights: list[MerchTrendHighlight]
    time_series: list[MerchTrendPoint] = Field(default_factory=list)


class MerchWorkspaceBootstrapResponse(BaseModel):
    store: ResolvedStore
    filters: MerchWorkspaceFilters
    initial_result: (
        MerchActionRecommendationsResponse
        | MerchDiagnosticsResponse
        | MerchTrendSummaryResponse
        | MerchInventoryViewResponse
        | MerchProductMixRecommendationsResponse
    )
    last_result: (
        MerchActionRecommendationsResponse
        | MerchDiagnosticsResponse
        | MerchTrendSummaryResponse
        | MerchInventoryViewResponse
        | MerchProductMixRecommendationsResponse
        | None
    ) = None
    last_tool: str | None = None


class MerchInventoryRowType(str, Enum):
    current_inventory = "current_inventory"
    potential_offer = "potential_offer"


class MerchInventoryViewRow(BaseModel):
    row_type: MerchInventoryRowType
    product_id: str | None = None
    offer_id: str | None = None
    title: str
    brand: str | None = None
    category: str | None = None
    size: str | None = None
    price: float | None = None
    availability: str | None = None
    stock_state: str | None = None
    inventory_qty: int = 0
    available_on: str | None = None
    offer_status: SupplierOfferStatus | None = None
    link: str | None = None
    image_url: str | None = None
    perf_revenue: float = 0.0
    perf_units: float = 0.0
    perf_margin_rate: float = 0.0


class MerchInventoryViewResponse(BaseModel):
    summary: str
    store: ResolvedStore
    lookback_days: int
    category: str | None = None
    brand: str | None = None
    price_band: PriceBand | None = None
    occasion: str | None = None
    occasions: list[str] = Field(default_factory=list)
    inventory_scope: InventoryScope
    future_window_days: int
    rows: list[MerchInventoryViewRow] = Field(default_factory=list)
    total_rows: int = 0
    current_rows: int = 0
    potential_rows: int = 0


class MerchMixAction(str, Enum):
    add = "add"
    hold = "hold"
    reduce = "reduce"
    swap = "swap"


class MerchProductMixRecommendationRow(BaseModel):
    action: MerchMixAction
    fit_score: float
    expected_mix_impact: float
    rationale: str
    brand: str | None = None
    category: str | None = None
    current_product_id: str | None = None
    current_title: str | None = None
    current_revenue: float | None = None
    current_units: float | None = None
    offer_id: str | None = None
    offer_title: str | None = None
    offer_status: SupplierOfferStatus | None = None
    available_on: str | None = None
    offer_price: float | None = None


class MerchProductMixRecommendationsResponse(BaseModel):
    summary: str
    store: ResolvedStore
    lookback_days: int
    top_k: int
    category: str | None = None
    brand: str | None = None
    price_band: PriceBand | None = None
    occasion: str | None = None
    occasions: list[str] = Field(default_factory=list)
    inventory_scope: InventoryScope
    future_window_days: int
    rows: list[MerchProductMixRecommendationRow] = Field(default_factory=list)


class MerchInventoryViewRequest(BaseModel):
    store_query: str | None = None
    store_id: str | None = None
    lookback_days: int = Field(default=90, ge=7, le=730)
    category: str | None = None
    brand: str | None = None
    price_band: PriceBand | None = None
    occasion: str | None = None
    occasions: list[str] = Field(default_factory=list)
    inventory_scope: InventoryScope = InventoryScope.combined
    future_window_days: int = Field(default=120, ge=1, le=365)
    limit: int = Field(default=200, ge=1, le=2000)

    @model_validator(mode="after")
    def validate_store_target(self) -> "MerchInventoryViewRequest":
        if not self.store_query and not self.store_id:
            raise ValueError("Provide store_query or store_id.")
        return self


class MerchProductMixRecommendationsRequest(BaseModel):
    store_query: str | None = None
    store_id: str | None = None
    lookback_days: int = Field(default=90, ge=7, le=730)
    top_k: int = Field(default=12, ge=1, le=100)
    category: str | None = None
    brand: str | None = None
    price_band: PriceBand | None = None
    occasion: str | None = None
    occasions: list[str] = Field(default_factory=list)
    inventory_scope: InventoryScope = InventoryScope.combined
    future_window_days: int = Field(default=120, ge=1, le=365)
    recommendation_overrides: list[MerchRecommendationOverride] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_store_target(self) -> "MerchProductMixRecommendationsRequest":
        if not self.store_query and not self.store_id:
            raise ValueError("Provide store_query or store_id.")
        return self


class UnifiedWorkspaceView(str, Enum):
    executive_overview = "executive_overview"
    inventory = "inventory"
    recommendations = "recommendations"
    mix_analysis = "mix_analysis"


class UnifiedRowMode(str, Enum):
    store_product = "store_product"
    aggregated = "aggregated"


class OverrideScope(str, Enum):
    store = "store"
    global_scope = "global"


class UnifiedRecommendationOverride(BaseModel):
    product_id: str
    store_id: str | None = None
    final_action: MerchFinalAction
    priority_tier: MerchPriorityTier
    override_note: str | None = None

    @model_validator(mode="after")
    def normalize_fields(self) -> "UnifiedRecommendationOverride":
        product_id = str(self.product_id or "").strip()
        if not product_id:
            raise ValueError("product_id is required.")
        self.product_id = product_id
        store_id = str(self.store_id or "").strip()
        self.store_id = store_id or None
        if self.override_note is not None:
            note = str(self.override_note).strip()
            self.override_note = note or None
        return self


class UnifiedWorkspaceFilters(BaseModel):
    store_ids: list[str] = Field(default_factory=list)
    active_store_id: str | None = None
    lookback_days: int = Field(default=90, ge=7, le=730)
    category: str | None = None
    brands: list[str] = Field(default_factory=list)
    occasions: list[str] = Field(default_factory=list)
    price_band: PriceBand | None = None
    objective: Objective = Objective.revenue
    top_k: int = Field(default=12, ge=1, le=100)
    inventory_scope: InventoryScope = InventoryScope.combined
    future_window_days: int = Field(default=120, ge=1, le=365)
    row_mode: UnifiedRowMode = UnifiedRowMode.store_product
    override_scope: OverrideScope = OverrideScope.store
    question: str | None = None

    @model_validator(mode="after")
    def normalize_fields(self) -> "UnifiedWorkspaceFilters":
        normalized_store_ids: list[str] = []
        for value in self.store_ids:
            token = str(value or "").strip()
            if token and token not in normalized_store_ids:
                normalized_store_ids.append(token)
        self.store_ids = normalized_store_ids

        active_store_id = str(self.active_store_id or "").strip()
        self.active_store_id = active_store_id or None

        normalized_brands: list[str] = []
        for value in self.brands:
            token = str(value or "").strip()
            if token and token not in normalized_brands:
                normalized_brands.append(token)
        self.brands = normalized_brands

        normalized_occasions: list[str] = []
        for value in self.occasions:
            token = str(value or "").strip()
            if token and token not in normalized_occasions:
                normalized_occasions.append(token)
        self.occasions = normalized_occasions
        return self


class UnifiedStoreScopeRequest(BaseModel):
    store_query: str | None = None
    store_id: str | None = None
    store_ids: list[str] = Field(default_factory=list)
    active_store_id: str | None = None
    lookback_days: int = Field(default=90, ge=7, le=730)
    category: str | None = None
    brand: str | None = None
    brands: list[str] = Field(default_factory=list)
    occasion: str | None = None
    occasions: list[str] = Field(default_factory=list)
    price_band: PriceBand | None = None

    @model_validator(mode="after")
    def normalize_scope(self) -> "UnifiedStoreScopeRequest":
        deduped_store_ids: list[str] = []
        for value in self.store_ids:
            token = str(value or "").strip()
            if token and token not in deduped_store_ids:
                deduped_store_ids.append(token)
        self.store_ids = deduped_store_ids

        active_store_id = str(self.active_store_id or "").strip()
        self.active_store_id = active_store_id or None

        deduped_brands: list[str] = []
        for value in self.brands:
            token = str(value or "").strip()
            if token and token not in deduped_brands:
                deduped_brands.append(token)
        if self.brand:
            for value in str(self.brand).replace(";", ",").replace("|", ",").split(","):
                token = value.strip()
                if token and token not in deduped_brands:
                    deduped_brands.append(token)
        self.brands = deduped_brands

        deduped_occasions: list[str] = []
        for value in self.occasions:
            token = str(value or "").strip()
            if token and token not in deduped_occasions:
                deduped_occasions.append(token)
        if self.occasion:
            for value in str(self.occasion).replace(";", ",").replace("|", ",").split(","):
                token = value.strip()
                if token and token not in deduped_occasions:
                    deduped_occasions.append(token)
        self.occasions = deduped_occasions
        return self


class UnifiedOverviewRequest(UnifiedStoreScopeRequest):
    objective: Objective = Objective.revenue
    top_k_stores: int = Field(default=12, ge=1, le=50)


class UnifiedInventoryViewRequest(UnifiedStoreScopeRequest):
    row_mode: UnifiedRowMode = UnifiedRowMode.store_product
    inventory_scope: InventoryScope = InventoryScope.combined
    future_window_days: int = Field(default=120, ge=1, le=365)
    limit: int = Field(default=300, ge=1, le=2000)


class UnifiedActionRecommendationsRequest(UnifiedStoreScopeRequest):
    question: str | None = None
    objective: Objective = Objective.margin
    top_k: int = Field(default=9, ge=1, le=100)
    row_mode: UnifiedRowMode = UnifiedRowMode.store_product
    override_scope: OverrideScope = OverrideScope.store
    recommendation_overrides: list[UnifiedRecommendationOverride] = Field(default_factory=list)
    compare_mode: CompareMode = CompareMode.peer_and_prior_period
    peer_mode: PeerMode = PeerMode.state_and_profile
    compare_store_id: str | None = None

    @model_validator(mode="after")
    def dedupe_overrides(self) -> "UnifiedActionRecommendationsRequest":
        deduped: list[UnifiedRecommendationOverride] = []
        seen: set[str] = set()
        for override in self.recommendation_overrides:
            key = f"{override.store_id or ''}|{override.product_id}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append(override)
        self.recommendation_overrides = deduped
        return self


class UnifiedProductMixRecommendationsRequest(UnifiedStoreScopeRequest):
    top_k: int = Field(default=12, ge=1, le=100)
    row_mode: UnifiedRowMode = UnifiedRowMode.store_product
    override_scope: OverrideScope = OverrideScope.store
    inventory_scope: InventoryScope = InventoryScope.combined
    future_window_days: int = Field(default=120, ge=1, le=365)
    recommendation_overrides: list[UnifiedRecommendationOverride] = Field(default_factory=list)

    @model_validator(mode="after")
    def dedupe_overrides(self) -> "UnifiedProductMixRecommendationsRequest":
        deduped: list[UnifiedRecommendationOverride] = []
        seen: set[str] = set()
        for override in self.recommendation_overrides:
            key = f"{override.store_id or ''}|{override.product_id}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append(override)
        self.recommendation_overrides = deduped
        return self


class UnifiedInventoryViewRow(BaseModel):
    row_type: MerchInventoryRowType
    store_id: str | None = None
    store_name: str | None = None
    store_count: int = 1
    product_id: str | None = None
    offer_id: str | None = None
    title: str
    brand: str | None = None
    category: str | None = None
    size: str | None = None
    price: float | None = None
    availability: str | None = None
    stock_state: str | None = None
    inventory_qty: int = 0
    available_on: str | None = None
    offer_status: SupplierOfferStatus | None = None
    link: str | None = None
    image_url: str | None = None
    perf_revenue: float = 0.0
    perf_units: float = 0.0
    perf_margin_rate: float = 0.0


class UnifiedInventoryViewResponse(BaseModel):
    summary: str
    store_ids: list[str] = Field(default_factory=list)
    active_store_id: str | None = None
    lookback_days: int
    category: str | None = None
    brands: list[str] = Field(default_factory=list)
    price_band: PriceBand | None = None
    occasions: list[str] = Field(default_factory=list)
    row_mode: UnifiedRowMode
    inventory_scope: InventoryScope
    future_window_days: int
    rows: list[UnifiedInventoryViewRow] = Field(default_factory=list)
    total_rows: int = 0
    current_rows: int = 0
    potential_rows: int = 0


class UnifiedActionRecommendationRow(BaseModel):
    store_id: str | None = None
    store_name: str | None = None
    store_count: int = 1
    product_id: str
    title: str
    brand: str
    category: str
    price: float | None = None
    link: str | None = None
    image_url: str | None = None
    price_band: PriceBand | None = None
    occasion: str | None = None
    metric_value: float
    peer_delta: float
    prior_period_delta: float | None = None
    rationale: str
    model_action: MerchAction
    model_priority_tier: MerchPriorityTier
    final_action: MerchFinalAction
    final_priority_tier: MerchPriorityTier
    override_note: str | None = None


class UnifiedActionRecommendationsResponse(BaseModel):
    summary: str
    store_ids: list[str] = Field(default_factory=list)
    active_store_id: str | None = None
    objective: Objective
    lookback_days: int
    category: str | None = None
    brands: list[str] = Field(default_factory=list)
    price_band: PriceBand | None = None
    occasions: list[str] = Field(default_factory=list)
    row_mode: UnifiedRowMode
    override_scope: OverrideScope
    recommendations: list[UnifiedActionRecommendationRow] = Field(default_factory=list)


class UnifiedProductMixRecommendationRow(BaseModel):
    store_id: str | None = None
    store_name: str | None = None
    store_count: int = 1
    action: MerchMixAction
    fit_score: float
    expected_mix_impact: float
    rationale: str
    brand: str | None = None
    category: str | None = None
    current_product_id: str | None = None
    current_title: str | None = None
    current_revenue: float | None = None
    current_units: float | None = None
    offer_id: str | None = None
    offer_title: str | None = None
    offer_status: SupplierOfferStatus | None = None
    available_on: str | None = None
    offer_price: float | None = None


class UnifiedProductMixRecommendationsResponse(BaseModel):
    summary: str
    store_ids: list[str] = Field(default_factory=list)
    active_store_id: str | None = None
    lookback_days: int
    top_k: int
    category: str | None = None
    brands: list[str] = Field(default_factory=list)
    price_band: PriceBand | None = None
    occasions: list[str] = Field(default_factory=list)
    row_mode: UnifiedRowMode
    override_scope: OverrideScope
    inventory_scope: InventoryScope
    future_window_days: int
    rows: list[UnifiedProductMixRecommendationRow] = Field(default_factory=list)


class UnifiedOverviewResponse(BaseModel):
    summary: str
    lookback_days: int
    objective: Objective
    generated_at: datetime
    store_ids: list[str] = Field(default_factory=list)
    active_store_id: str | None = None
    total_revenue: float
    total_units: float
    margin_rate: float
    prior_revenue: float | None = None
    prior_margin_rate: float | None = None
    revenue_delta_pct: float | None = None
    store_count: int
    stores: list[ExecutiveStoreInsight] = Field(default_factory=list)
    trend: list[ExecutiveTrendPoint] = Field(default_factory=list)


class UnifiedWorkspaceBootstrapResponse(BaseModel):
    filters: UnifiedWorkspaceFilters
    active_view: UnifiedWorkspaceView = UnifiedWorkspaceView.executive_overview
    initial_result: (
        UnifiedOverviewResponse
        | UnifiedInventoryViewResponse
        | UnifiedActionRecommendationsResponse
        | UnifiedProductMixRecommendationsResponse
    )
    last_result: (
        UnifiedOverviewResponse
        | UnifiedInventoryViewResponse
        | UnifiedActionRecommendationsResponse
        | UnifiedProductMixRecommendationsResponse
        | None
    ) = None
    last_tool: str | None = None
    initial_notice: str | None = None


class UnifiedExportCsvRequest(UnifiedStoreScopeRequest):
    view: UnifiedWorkspaceView = UnifiedWorkspaceView.executive_overview
    question: str | None = None
    objective: Objective = Objective.revenue
    top_k: int = Field(default=12, ge=1, le=100)
    top_k_stores: int = Field(default=50, ge=1, le=50)
    row_mode: UnifiedRowMode = UnifiedRowMode.store_product
    override_scope: OverrideScope = OverrideScope.store
    inventory_scope: InventoryScope = InventoryScope.combined
    future_window_days: int = Field(default=120, ge=1, le=365)
    limit: int = Field(default=300, ge=1, le=2000)
    recommendation_overrides: list[UnifiedRecommendationOverride] = Field(default_factory=list)
    compare_mode: CompareMode = CompareMode.peer_and_prior_period
    peer_mode: PeerMode = PeerMode.state_and_profile
    compare_store_id: str | None = None

    @model_validator(mode="after")
    def dedupe_overrides(self) -> "UnifiedExportCsvRequest":
        deduped: list[UnifiedRecommendationOverride] = []
        seen: set[str] = set()
        for override in self.recommendation_overrides:
            key = f"{override.store_id or ''}|{override.product_id}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append(override)
        self.recommendation_overrides = deduped
        return self


class UnifiedExportCsvRow(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


class UnifiedExportCsvResponse(BaseModel):
    view: UnifiedWorkspaceView
    row_mode: UnifiedRowMode
    override_scope: OverrideScope
    filename: str
    headers: list[str] = Field(default_factory=list)
    rows: list[UnifiedExportCsvRow] = Field(default_factory=list)
    row_count: int = 0
    csv_text: str
    generated_at: datetime


class ExecutiveWorkspaceFilters(BaseModel):
    lookback_days: int = Field(default=90, ge=7, le=730)
    objective: Objective = Objective.revenue
    top_k_stores: int = Field(default=12, ge=1, le=50)
    events: list[str] = Field(default_factory=lambda: ["wedding", "holiday_party", "workwear"])
    brands: list[str] = Field(default_factory=list)
    store_id: str | None = None
    store_ids: list[str] = Field(default_factory=list)
    discount_pct: float = Field(default=0.0, ge=0.0, le=60.0)
    floor_space_shift_pct: float = Field(default=0.0, ge=-40.0, le=40.0)
    from_category: str | None = None
    to_category: str | None = None
    to_email: str | None = None
    autopilot_top_k: int = Field(default=6, ge=1, le=20)
    optimize_discount_min_pct: float = Field(default=0.0, ge=0.0, le=60.0)
    optimize_discount_max_pct: float = Field(default=20.0, ge=0.0, le=60.0)
    optimize_discount_step_pct: float = Field(default=5.0, ge=1.0, le=20.0)
    optimize_shift_min_pct: float = Field(default=0.0, ge=-40.0, le=40.0)
    optimize_shift_max_pct: float = Field(default=20.0, ge=-40.0, le=40.0)
    optimize_shift_step_pct: float = Field(default=5.0, ge=1.0, le=20.0)
    optimize_top_k_scenarios: int = Field(default=3, ge=1, le=10)
    min_margin_rate: float = Field(default=0.40, ge=0.0, le=1.0)
    max_discount_pct: float = Field(default=20.0, ge=0.0, le=60.0)
    strategy_packet_id: str | None = None


class ExecutiveRiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class ExecutiveCampaignAction(str, Enum):
    promotion = "promotion"
    transfer = "transfer"
    monitor = "monitor"


class ExecutiveCampaignStatus(str, Enum):
    draft = "draft"
    sent = "sent"
    failed = "failed"


class ExecutiveStoreInsight(BaseModel):
    store_id: str
    store_name: str
    city: str
    state: str
    revenue: float
    units: float
    margin_rate: float
    revenue_share_pct: float
    revenue_delta_pct: float | None = None
    rank: int


class ExecutiveTrendPoint(BaseModel):
    period_start: str
    revenue: float
    units: float
    margin_rate: float


class ExecutiveOverviewResponse(BaseModel):
    summary: str
    lookback_days: int
    objective: Objective
    generated_at: datetime
    total_revenue: float
    total_units: float
    margin_rate: float
    prior_revenue: float | None = None
    prior_margin_rate: float | None = None
    revenue_delta_pct: float | None = None
    store_count: int
    stores: list[ExecutiveStoreInsight] = Field(default_factory=list)
    trend: list[ExecutiveTrendPoint] = Field(default_factory=list)


class ExecutiveReadinessRecommendation(BaseModel):
    action: ExecutiveCampaignAction
    source_store_id: str | None = None
    source_store_name: str | None = None
    suggested_discount_pct: float | None = None
    rationale: str


class ExecutiveReadinessRow(BaseModel):
    event: str
    store_id: str
    store_name: str
    city: str
    state: str
    risk_score: float
    risk_level: ExecutiveRiskLevel
    coverage_weeks: float
    inventory_units: float
    recent_units: float
    prior_units: float
    demand_change_pct: float | None = None
    margin_rate: float
    recommendation: ExecutiveReadinessRecommendation


class ExecutiveEventReadinessRadarResponse(BaseModel):
    summary: str
    lookback_days: int
    generated_at: datetime
    events: list[str] = Field(default_factory=list)
    rows: list[ExecutiveReadinessRow] = Field(default_factory=list)


class ExecutiveWhatIfCategoryAllocation(BaseModel):
    category: str
    baseline_revenue: float
    projected_revenue: float
    baseline_revenue_share_pct: float
    projected_revenue_share_pct: float
    baseline_space_share_pct: float
    projected_space_share_pct: float
    applied_discount_pct: float = 0.0


class ExecutiveWhatIfStoreAllocation(BaseModel):
    store_id: str
    store_name: str
    city: str
    state: str
    categories: list[ExecutiveWhatIfCategoryAllocation] = Field(default_factory=list)


class ExecutiveWhatIfSimulatorResponse(BaseModel):
    summary: str
    lookback_days: int
    generated_at: datetime
    baseline_revenue: float
    baseline_margin_rate: float
    expected_revenue: float
    expected_margin_rate: float
    revenue_delta: float
    margin_rate_delta: float
    confidence_interval_low: float
    confidence_interval_high: float
    category_allocations: list[ExecutiveWhatIfCategoryAllocation] = Field(default_factory=list)
    store_allocations: list[ExecutiveWhatIfStoreAllocation] = Field(default_factory=list)


class ExecutiveAutoOptimizeScenario(BaseModel):
    scenario_id: str
    discount_pct: float
    floor_space_shift_pct: float
    from_category: str | None = None
    to_category: str | None = None
    expected_revenue: float
    expected_margin_rate: float
    revenue_delta: float
    margin_rate_delta: float
    confidence_interval_low: float
    confidence_interval_high: float
    objective_score: float
    guardrail_passed: bool
    guardrail_reasons: list[str] = Field(default_factory=list)
    rationale: str


class ExecutiveAutoOptimizeRequest(BaseModel):
    store_query: str | None = None
    store_id: str | None = None
    store_ids: list[str] = Field(default_factory=list)
    lookback_days: int = Field(default=90, ge=7, le=730)
    objective: Objective = Objective.revenue
    brands: list[str] = Field(default_factory=list)
    from_category: str | None = None
    to_category: str | None = None
    discount_min_pct: float = Field(default=0.0, ge=0.0, le=60.0)
    discount_max_pct: float = Field(default=20.0, ge=0.0, le=60.0)
    discount_step_pct: float = Field(default=5.0, ge=1.0, le=20.0)
    shift_min_pct: float = Field(default=0.0, ge=-40.0, le=40.0)
    shift_max_pct: float = Field(default=20.0, ge=-40.0, le=40.0)
    shift_step_pct: float = Field(default=5.0, ge=1.0, le=20.0)
    top_k_scenarios: int = Field(default=3, ge=1, le=10)
    min_margin_rate: float = Field(default=0.40, ge=0.0, le=1.0)
    max_discount_pct: float = Field(default=20.0, ge=0.0, le=60.0)

    @model_validator(mode="after")
    def validate_ranges(self) -> "ExecutiveAutoOptimizeRequest":
        if self.discount_min_pct > self.discount_max_pct:
            raise ValueError("discount_min_pct cannot be greater than discount_max_pct")
        if self.shift_min_pct > self.shift_max_pct:
            raise ValueError("shift_min_pct cannot be greater than shift_max_pct")
        return self


class ExecutiveAutoOptimizeResponse(BaseModel):
    summary: str
    objective: Objective
    lookback_days: int
    generated_at: datetime
    scope_label: str
    scope_store_ids: list[str] = Field(default_factory=list)
    baseline_revenue: float
    baseline_margin_rate: float
    scenarios: list[ExecutiveAutoOptimizeScenario] = Field(default_factory=list)


class StrategyCore(BaseModel):
    objective: Objective = Objective.revenue
    lookback_days: int = Field(default=90, ge=7, le=730)
    category: str | None = None
    brands: list[str] = Field(default_factory=list)
    discount_pct: float = Field(default=0.0, ge=0.0, le=60.0)
    floor_space_shift_pct: float = Field(default=0.0, ge=-40.0, le=40.0)
    min_margin_rate: float = Field(default=0.40, ge=0.0, le=1.0)
    max_discount_pct: float = Field(default=20.0, ge=0.0, le=60.0)


class ExecutiveStrategyPacketStatus(str, Enum):
    published = "published"


class ExecutiveStrategyPacketEmailStatus(str, Enum):
    draft = "draft"
    sent = "sent"
    failed = "failed"


class ExecutivePublishStrategyPacketRequest(BaseModel):
    scenario: ExecutiveAutoOptimizeScenario
    objective: Objective = Objective.revenue
    lookback_days: int = Field(default=90, ge=7, le=730)
    store_query: str | None = None
    store_id: str | None = None
    store_ids: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    from_category: str | None = None
    to_category: str | None = None
    min_margin_rate: float = Field(default=0.40, ge=0.0, le=1.0)
    max_discount_pct: float = Field(default=20.0, ge=0.0, le=60.0)
    title: str | None = None
    summary: str | None = None


class ExecutiveStrategyPacketResponse(BaseModel):
    packet_id: str
    status: ExecutiveStrategyPacketStatus
    title: str
    summary: str
    objective: Objective
    lookback_days: int
    scope_label: str
    scope_store_ids: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    from_category: str | None = None
    to_category: str | None = None
    strategy_core: StrategyCore
    tag_intensity: StrategyTagIntensity = StrategyTagIntensity.medium
    min_margin_rate: float
    max_discount_pct: float
    scenario: ExecutiveAutoOptimizeScenario
    created_at: datetime
    updated_at: datetime
    email_status: ExecutiveStrategyPacketEmailStatus
    to_email: str | None = None
    email_subject: str | None = None
    email_body_text: str | None = None
    provider_message_id: str | None = None
    email_error_message: str | None = None
    sent_at: datetime | None = None


class ExecutiveStrategyPacketEmailDraftResponse(BaseModel):
    packet_id: str
    email_status: ExecutiveStrategyPacketEmailStatus
    to_email: str
    subject: str
    body_text: str
    generated_at: datetime


class ExecutiveStrategyPacketEmailSendResponse(BaseModel):
    packet_id: str
    email_status: ExecutiveStrategyPacketEmailStatus
    to_email: str
    provider_message_id: str | None = None
    error_message: str | None = None
    sent_at: datetime | None = None


class MerchEffectiveStrategyResponse(BaseModel):
    store_id: str
    strategy_packet_id: str | None = None
    source: str = "none"
    strategy_core: StrategyCore | None = None
    tag_intensity: StrategyTagIntensity = StrategyTagIntensity.medium
    override_active: bool = False
    override_updated_at: datetime | None = None


class ProductPerformanceDimension(str, Enum):
    product = "product"
    brand = "brand"


class ProductPerformanceOpportunityRow(BaseModel):
    dimension: ProductPerformanceDimension
    key: str
    product_id: str | None = None
    title: str | None = None
    brand: str
    category: str | None = None
    store_id: str | None = None
    store_name: str | None = None
    current_revenue: float
    prior_revenue: float
    revenue_delta: float
    revenue_delta_pct: float | None = None
    current_units: float
    prior_units: float
    unit_delta: float
    unit_delta_pct: float | None = None
    margin_rate: float
    opportunity_score: float
    rationale: str


class ProductPerformanceSummaryRequest(BaseModel):
    dimension: ProductPerformanceDimension = ProductPerformanceDimension.product
    store_query: str | None = None
    store_id: str | None = None
    store_ids: list[str] = Field(default_factory=list)
    lookback_days: int = Field(default=90, ge=14, le=730)
    min_margin_rate: float = Field(default=0.50, ge=0.0, le=1.0)
    min_revenue_drop_pct: float = Field(default=10.0, ge=0.0, le=100.0)
    top_k: int = Field(default=15, ge=1, le=100)
    category: str | None = None
    brand: str | None = None


class ProductPerformanceSummaryResponse(BaseModel):
    summary: str
    dimension: ProductPerformanceDimension
    scope_label: str
    scope_store_ids: list[str] = Field(default_factory=list)
    lookback_days: int
    current_window_start: str
    current_window_end: str
    prior_window_start: str
    prior_window_end: str
    min_margin_rate: float
    min_revenue_drop_pct: float
    category: str | None = None
    brand: str | None = None
    generated_at: datetime
    rows: list[ProductPerformanceOpportunityRow] = Field(default_factory=list)


class ExecutiveCampaignCandidate(BaseModel):
    store_id: str
    store_name: str
    city: str
    state: str
    event: str
    risk_score: float
    risk_level: ExecutiveRiskLevel
    coverage_weeks: float
    margin_rate: float
    action: ExecutiveCampaignAction
    suggested_discount_pct: float | None = None
    source_store_id: str | None = None
    source_store_name: str | None = None
    rationale: str


class ExecutiveCampaignAutopilotDraftResponse(BaseModel):
    draft_id: str
    status: ExecutiveCampaignStatus
    to_email: str
    subject: str
    body_text: str
    lookback_days: int
    generated_at: datetime
    guardrails: dict[str, float | int | str]
    candidates: list[ExecutiveCampaignCandidate] = Field(default_factory=list)


class ExecutiveCampaignAutopilotSendResponse(BaseModel):
    draft_id: str
    status: ExecutiveCampaignStatus
    to_email: str
    provider_message_id: str | None = None
    error_message: str | None = None
    sent_at: datetime | None = None


class ExecutiveExportCsvView(str, Enum):
    store_performance = "store_performance"


class ExecutiveExportCsvRequest(BaseModel):
    view: ExecutiveExportCsvView = ExecutiveExportCsvView.store_performance
    store_query: str | None = None
    store_id: str | None = None
    store_ids: list[str] = Field(default_factory=list)
    lookback_days: int = Field(default=90, ge=7, le=730)
    objective: Objective = Objective.revenue
    top_k_stores: int = Field(default=50, ge=1, le=50)

    @model_validator(mode="after")
    def validate_store_scope(self) -> "ExecutiveExportCsvRequest":
        normalized_store_ids = []
        for value in self.store_ids:
            token = str(value or "").strip()
            if token and token not in normalized_store_ids:
                normalized_store_ids.append(token)
        self.store_ids = normalized_store_ids
        if self.store_ids and (self.store_id or self.store_query):
            raise ValueError("Provide store_ids or store_query/store_id, not both.")
        return self


class ExecutiveExportCsvRow(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


class ExecutiveExportCsvResponse(BaseModel):
    view: ExecutiveExportCsvView
    filename: str
    lookback_days: int
    objective: Objective
    headers: list[str] = Field(default_factory=list)
    rows: list[ExecutiveExportCsvRow] = Field(default_factory=list)
    row_count: int = 0
    csv_text: str
    generated_at: datetime


class MerchExportMode(str, Enum):
    legacy_combined = "legacy_combined"
    view_only = "view_only"


class MerchExportCsvRequest(BaseModel):
    view: MerchWorkspaceView = MerchWorkspaceView.actions
    export_mode: MerchExportMode = MerchExportMode.legacy_combined
    store_query: str | None = None
    store_id: str | None = None
    question: str | None = None
    objective: Objective = Objective.margin
    lookback_days: int = Field(default=90, ge=7, le=730)
    top_k: int = Field(default=9, ge=1, le=50)
    category: str | None = None
    brand: str | None = None
    price_band: PriceBand | None = None
    occasion: str | None = None
    occasions: list[str] = Field(default_factory=list)
    inventory_scope: InventoryScope = InventoryScope.combined
    future_window_days: int = Field(default=120, ge=1, le=365)
    recommendation_overrides: list[MerchRecommendationOverride] = Field(default_factory=list)
    compare_mode: CompareMode = CompareMode.peer_and_prior_period
    peer_mode: PeerMode = PeerMode.state_and_profile
    compare_store_id: str | None = None

    @model_validator(mode="after")
    def validate_store_target(self) -> "MerchExportCsvRequest":
        if not self.store_query and not self.store_id:
            raise ValueError("Provide store_query or store_id.")
        deduped: list[MerchRecommendationOverride] = []
        seen: set[str] = set()
        for override in self.recommendation_overrides:
            key = str(override.product_id or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(override)
        self.recommendation_overrides = deduped
        return self


class MerchExportCsvRow(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


class MerchExportCsvResponse(BaseModel):
    view: MerchWorkspaceView
    store: ResolvedStore
    filename: str
    headers: list[str] = Field(default_factory=list)
    rows: list[MerchExportCsvRow] = Field(default_factory=list)
    row_count: int = 0
    csv_text: str
    generated_at: datetime


class InventoryFacet(str, Enum):
    brand = "brand"
    category = "category"
    size = "size"


class InventoryByStoreRow(BaseModel):
    store_id: str
    store_name: str
    city: str
    state: str
    units_in_stock: int
    sku_count: int


class InventoryByStoreResponse(BaseModel):
    product_query: str | None = None
    product_id: str | None = None
    brand: str | None = None
    category: str | None = None
    size: str | None = None
    rows: list[InventoryByStoreRow] = Field(default_factory=list)
    total_units_in_stock: int = 0
    total_skus: int = 0


class InventoryFacetRow(BaseModel):
    facet_value: str
    units_in_stock: int
    sku_count: int


class InventoryFacetsResponse(BaseModel):
    facet: InventoryFacet
    store: ResolvedStore | None = None
    product_query: str | None = None
    product_id: str | None = None
    brand: str | None = None
    category: str | None = None
    size: str | None = None
    rows: list[InventoryFacetRow] = Field(default_factory=list)
    total_units_in_stock: int = 0
    total_skus: int = 0


class InventoryCheckByStoreRow(BaseModel):
    store_id: str
    store_name: str
    city: str
    state: str
    sku_count: int
    in_stock_skus: int
    preorder_skus: int
    out_of_stock_skus: int
    not_in_stock_skus: int
    not_in_stock_rate_pct: float
    in_stock_units: int
    preorder_units: int


class InventoryCheckByStoreResponse(BaseModel):
    store: ResolvedStore | None = None
    product_query: str | None = None
    product_id: str | None = None
    brand: str | None = None
    category: str | None = None
    size: str | None = None
    rows: list[InventoryCheckByStoreRow] = Field(default_factory=list)
    total_skus: int = 0
    total_in_stock_skus: int = 0
    total_preorder_skus: int = 0
    total_out_of_stock_skus: int = 0
    total_not_in_stock_skus: int = 0
    total_in_stock_units: int = 0
    total_preorder_units: int = 0


class InventoryProductRow(BaseModel):
    product_id: str
    title: str
    brand: str | None = None
    category: str | None = None
    size: str | None = None
    price: float | None = None
    availability: str | None = None
    stock_state: str
    inventory_qty: int
    link: str | None = None
    image_url: str | None = None


class InventoryProductsResponse(BaseModel):
    store: ResolvedStore | None = None
    product_query: str | None = None
    product_id: str | None = None
    brand: str | None = None
    category: str | None = None
    size: str | None = None
    rows: list[InventoryProductRow] = Field(default_factory=list)
    row_count: int = 0
    total_inventory_units: int = 0
