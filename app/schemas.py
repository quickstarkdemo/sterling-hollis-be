from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator


class SyntheticVolumes(BaseModel):
    stores: int = 36
    products: int = 4000
    customers: int = 12000
    orders: int = 80000


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
        default_factory=lambda: ["stores", "customers", "products", "orders", "order_items", "store_daily_metrics"]
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


class CustomerRecommendationResponse(BaseModel):
    store_id: str
    strategy: str
    recommendations: list[ProductRecommendation]
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


class MerchWorkspaceFilters(BaseModel):
    question: str | None = None
    category: str | None = None
    brand: str | None = None
    price_band: PriceBand | None = None
    occasion: str | None = None
    lookback_days: int = 90
    compare_mode: CompareMode = CompareMode.peer_and_prior_period
    peer_mode: PeerMode = PeerMode.state_and_profile
    top_k: int = 9


class MerchAction(str, Enum):
    feature = "feature"
    deprioritize = "deprioritize"
    promote = "promote"


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
    peer_store_ids: list[str]
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
    peer_store_ids: list[str]
    summary: str
    insights: list[MerchDiagnosticInsight]


class MerchTrendHighlight(BaseModel):
    subject: str
    current_value: float
    peer_value: float | None = None
    prior_value: float | None = None
    pct_change: float
    rationale: str


class MerchTrendSummaryResponse(BaseModel):
    store: ResolvedStore
    compare_mode: CompareMode
    peer_mode: PeerMode
    lookback_days: int
    category: str | None = None
    brand: str | None = None
    price_band: PriceBand | None = None
    occasion: str | None = None
    summary: str
    highlights: list[MerchTrendHighlight]


class MerchWorkspaceBootstrapResponse(BaseModel):
    store: ResolvedStore
    filters: MerchWorkspaceFilters
    initial_result: MerchActionRecommendationsResponse
    last_result: MerchActionRecommendationsResponse | MerchDiagnosticsResponse | MerchTrendSummaryResponse | None = None
    last_tool: str | None = None
