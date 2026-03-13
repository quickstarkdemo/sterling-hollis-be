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


class CustomerRecommendationRequest(BaseModel):
    store_id: str
    customer_id: str | None = None
    occasion: str | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    top_k: int = Field(default=12, ge=1, le=50)

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
    score: float
    reasons: list[str]


class CustomerRecommendationResponse(BaseModel):
    store_id: str
    strategy: str
    recommendations: list[ProductRecommendation]


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
    first_name: str
    last_name: str
    home_store_id: str
    home_store_name: str
    loyalty_tier: str
    match_reason: str


class CustomerResolutionResponse(BaseModel):
    query: str
    resolved: ResolvedCustomer


class StoreAssociateRecommendationResponse(BaseModel):
    store: ResolvedStore
    customer: ResolvedCustomer
    recommendation: CustomerRecommendationResponse


class CustomerCommunicationStatus(str, Enum):
    draft = "draft"
    sent = "sent"
    failed = "failed"


class CustomerCommunicationRecord(BaseModel):
    id: str
    customer_id: str
    customer_email: str
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


class CustomerCommunicationDraftResponse(BaseModel):
    message: CustomerCommunicationRecord
    store: ResolvedStore
    customer: ResolvedCustomer
    recommendation: CustomerRecommendationResponse


class CustomerCommunicationHistoryResponse(BaseModel):
    customer: ResolvedCustomer
    messages: list[CustomerCommunicationRecord]


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
    metric_value: float
    peer_delta: float
    rationale: str


class MerchActionRecommendationsResponse(BaseModel):
    store: ResolvedStore
    objective: Objective
    lookback_days: int
    peer_store_ids: list[str]
    parsed_intent: str
    recommendations: list[MerchActionRecommendationItem]


class MerchDiagnosticInsight(BaseModel):
    dimension: str
    subject: str
    status: str
    current_value: float
    peer_value: float
    delta: float
    rationale: str


class MerchDiagnosticsResponse(BaseModel):
    store: ResolvedStore
    lookback_days: int
    peer_store_ids: list[str]
    summary: str
    insights: list[MerchDiagnosticInsight]


class MerchTrendHighlight(BaseModel):
    subject: str
    current_value: float
    prior_value: float
    pct_change: float
    rationale: str


class MerchTrendSummaryResponse(BaseModel):
    store: ResolvedStore
    lookback_days: int
    summary: str
    highlights: list[MerchTrendHighlight]
