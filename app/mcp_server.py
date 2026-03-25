from __future__ import annotations

from collections import Counter
import csv
import hashlib
from datetime import datetime, timedelta, timezone
from enum import Enum
from io import StringIO
from pathlib import Path
import re

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, case, desc, func, or_, select

from app.config import get_settings
from app.database import SessionLocal
from app.models import Order, OrderItem, Product, ProductEmbedding, Store, SupplierProductOffer, SyntheticRun
from app.schemas import (
    CompareMode,
    CustomerCommunicationDraftResponse,
    CustomerCommunicationHistoryResponse,
    CustomerCommunicationStatus,
    CustomerCommunicationUpdateResponse,
    CustomerEmailDraftResponse,
    CustomerEmailSendResponse,
    CustomerLookupResponse,
    CustomerRecommendationRequest,
    CustomerRecommendationResponse,
    CustomerResolutionResponse,
    CustomerSearchResponse,
    CustomerValueSummaryRequest,
    CustomerValueSummaryResponse,
    ExecutiveAutoOptimizeRequest,
    ExecutiveAutoOptimizeResponse,
    ExecutiveAutoOptimizeScenario,
    ExecutiveCampaignAutopilotDraftResponse,
    ExecutiveCampaignAutopilotSendResponse,
    ExecutiveExportCsvRequest,
    ExecutiveExportCsvResponse,
    ExecutiveExportCsvRow,
    ExecutiveExportCsvView,
    ExecutivePublishStrategyPacketRequest,
    ExecutiveEventReadinessRadarResponse,
    ExecutiveOverviewResponse,
    ExecutiveStrategyPacketEmailDraftResponse,
    ExecutiveStrategyPacketEmailSendResponse,
    ExecutiveStrategyPacketResponse,
    ExecutiveWhatIfSimulatorResponse,
    ExecutiveWorkspaceFilters,
    IndexJobListResponse,
    IndexJobResponse,
    IndexProductsRequest,
    IndexProductsResponse,
    InventoryScope,
    InventoryByStoreResponse,
    InventoryByStoreRow,
    InventoryCheckByStoreResponse,
    InventoryCheckByStoreRow,
    InventoryFacet,
    InventoryFacetRow,
    InventoryFacetsResponse,
    InventoryProductRow,
    InventoryProductsResponse,
    MerchActionRecommendationsResponse,
    MerchAction,
    MerchDiagnosticsResponse,
    MerchEffectiveStrategyResponse,
    MerchExportMode,
    MerchExportCsvRequest,
    MerchExportCsvResponse,
    MerchExportCsvRow,
    MerchInventoryViewRequest,
    MerchInventoryViewResponse,
    MerchInventoryViewRow,
    MerchInventoryRowType,
    MerchMixAction,
    MerchProductMixRecommendationRow,
    MerchProductMixRecommendationsRequest,
    MerchProductMixRecommendationsResponse,
    MerchRecommendationOverride,
    MerchFinalAction,
    MerchPriorityTier,
    MerchTrendSummaryResponse,
    MerchWorkspaceFilters,
    MerchWorkspaceView,
    MerchandisingRecommendationRequest,
    MerchandisingRecommendationResponse,
    Objective,
    OverrideScope,
    PeerMode,
    ProductPerformanceDimension,
    ProductPerformanceSummaryRequest,
    ProductPerformanceSummaryResponse,
    PurchaseScope,
    PriceBand,
    RetrievalMode,
    RunReportResponse,
    StoreAssociateRecommendationResponse,
    StoreResolutionResponse,
    StyleConstraints,
    StrategyCore,
    StrategyTagIntensity,
    SyntheticGenerateRequest,
    SyntheticGenerateResponse,
    SyntheticLoadRequest,
    SyntheticLoadResponse,
    TwilioSmokeTestResponse,
    UnifiedActionRecommendationsRequest,
    UnifiedActionRecommendationsResponse,
    UnifiedActionRecommendationRow,
    UnifiedExportCsvRequest,
    UnifiedExportCsvResponse,
    UnifiedExportCsvRow,
    UnifiedInventoryViewRequest,
    UnifiedInventoryViewResponse,
    UnifiedInventoryViewRow,
    UnifiedOverviewRequest,
    UnifiedOverviewResponse,
    UnifiedProductMixRecommendationsRequest,
    UnifiedProductMixRecommendationsResponse,
    UnifiedProductMixRecommendationRow,
    UnifiedRecommendationOverride,
    UnifiedRowMode,
    UnifiedWorkspaceBootstrapResponse,
    UnifiedWorkspaceFilters,
    UnifiedWorkspaceView,
    VectorStatusResponse,
)
from app.services.apps_ui import render_widget_html
from app.services.communications import (
    customer_message_history,
    get_customer_email_draft,
    prepare_customer_sms,
    prepare_customer_email_draft,
    send_customer_email_draft,
    send_customer_recommendations_email,
    send_customer_sms,
    twilio_smoke_test,
    update_customer_email_draft,
    update_customer_sms_draft,
)
from app.services.indexing import index_products_for_run
from app.services.index_jobs import enqueue_index_job, get_index_job, list_index_jobs
from app.services.inventory_status import (
    is_in_stock,
    is_out_of_stock,
    is_preorder,
    sql_is_in_stock,
    sql_is_not_in_stock,
    sql_is_out_of_stock,
    sql_is_preorder,
)
from app.services.loader import (
    assert_synthetic_tables_empty,
    current_loaded_counts,
    load_entity_csv,
    read_generated_counts,
    reset_synthetic_tables,
)
from app.services.lookup import find_customers, resolve_customer, resolve_store
from app.services.customer_value import customer_value_summary
from app.services.executive import (
    apply_execution_tags_for_store,
    auto_optimize_strategy,
    get_effective_merch_strategy,
    get_strategy_packet,
    campaign_autopilot_prepare,
    campaign_autopilot_send,
    event_readiness_radar,
    executive_overview,
    get_campaign_autopilot_draft,
    prepare_strategy_packet_email,
    publish_strategy_packet,
    save_merch_strategy_override,
    send_strategy_packet_email,
    what_if_simulator,
)
from app.services.merchandising import (
    merchandising_action_recommendations,
    merchandising_diagnostics,
    merchandising_trend_summary,
)
from app.services.product_performance import product_margin_sales_opportunities
from app.services.recommendations import customer_recommendations, merchandising_recommendations
from app.services.store_source import fetch_store_snapshot, normalize_stores
from app.services.synthetic_generator import GenerationVolumes, generate_synthetic_dataset, new_run_id
from app.services.system_status import vector_status_payload
from app.services.taxonomy import CATEGORY_TAXONOMY, OCCASION_TO_CATEGORY


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


settings = get_settings()
mcp = FastMCP(
    "fashion_db_mcp",
    json_response=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_split_csv(settings.mcp_allowed_hosts),
        allowed_origins=_split_csv(settings.mcp_allowed_origins),
    ),
)


_WIDGET_RESOURCE_META = {
    "openai/widgetPrefersBorder": True,
    "openai/widgetCSP": {"connect_domains": [settings.public_base_url], "resource_domains": [settings.public_base_url]},
}
_WIDGET_TOOL_META = {
    "openai/widgetAccessible": True,
    "openai/visibility": "public",
    "ui": {"visibility": "public"},
}
_CUSTOMER_SEARCH_WIDGET_TEMPLATE_BASE = "ui://widgets/customer-search/workspace"
_MERCH_WORKSPACE_TEMPLATE_BASE = "ui://widgets/merch/workspace"
_EXEC_WORKSPACE_TEMPLATE_BASE = "ui://widgets/exec/workspace"
_UNIFIED_WORKSPACE_TEMPLATE_BASE = "ui://widgets/unified/workspace"
_DEFAULT_EXEC_TO_EMAIL = "djn12313@gmail.com"
_WIDGET_BUILD_TAG = re.sub(r"[^A-Za-z0-9._-]", "-", settings.app_build_version or "dev")
_WIDGET_ASSET_DIR = Path(__file__).resolve().parent / "static" / "chatgpt-ui"


def _widget_asset_hash(filename: str) -> str:
    asset_path = _WIDGET_ASSET_DIR / filename
    try:
        digest = hashlib.sha1(asset_path.read_bytes()).hexdigest()[:8]
    except OSError:
        digest = "missing"
    return digest


_CUSTOMER_WIDGET_TAG = (
    f"{_WIDGET_BUILD_TAG}-{_widget_asset_hash('widget.js')}-{_widget_asset_hash('widget.css')}"
)
_MERCH_WIDGET_TAG = (
    f"{_WIDGET_BUILD_TAG}-{_widget_asset_hash('merch-widget.js')}-{_widget_asset_hash('widget.css')}"
)
_EXEC_WIDGET_TAG = (
    f"{_WIDGET_BUILD_TAG}-{_widget_asset_hash('exec-widget.js')}-{_widget_asset_hash('widget.css')}"
)
_UNIFIED_WIDGET_TAG = (
    f"{_WIDGET_BUILD_TAG}-{_widget_asset_hash('unified-widget.js')}-{_widget_asset_hash('widget.css')}"
)
_CUSTOMER_SEARCH_WIDGET_RESOURCE_URI = f"{_CUSTOMER_SEARCH_WIDGET_TEMPLATE_BASE}-{_CUSTOMER_WIDGET_TAG}.html"
_MERCH_WORKSPACE_RESOURCE_URI = f"{_MERCH_WORKSPACE_TEMPLATE_BASE}-{_MERCH_WIDGET_TAG}.html"
_EXEC_WORKSPACE_RESOURCE_URI = f"{_EXEC_WORKSPACE_TEMPLATE_BASE}-{_EXEC_WIDGET_TAG}.html"
_UNIFIED_WORKSPACE_RESOURCE_URI = f"{_UNIFIED_WORKSPACE_TEMPLATE_BASE}-{_UNIFIED_WIDGET_TAG}.html"


class LatestRunResponse(BaseModel):
    id: str
    seed: int
    status: str
    started_at: datetime
    completed_at: datetime | None = None


class ProductFeedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str | None = Field(default=None, description="Optional store identifier such as '1001'.")
    limit: int = Field(default=200, ge=1, le=5000, description="Maximum number of products to return.")


def _tool_annotations(read_only: bool, idempotent: bool, open_world: bool = False) -> dict:
    return {
        "readOnlyHint": read_only,
        "destructiveHint": False,
        "idempotentHint": idempotent,
        "openWorldHint": open_world,
    }


def _render_tool_meta(resource_uri: str, invoking: str, invoked: str) -> dict:
    return {
        **_WIDGET_TOOL_META,
        "ui": {**_WIDGET_TOOL_META["ui"], "resourceUri": resource_uri},
        "openai/outputTemplate": resource_uri,
        "openai/toolInvocation/invoking": invoking,
        "openai/toolInvocation/invoked": invoked,
    }


def _calltool_result(text: str, payload: dict | None = None, meta: dict | None = None) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=payload,
        _meta=meta,
        isError=False,
    )


def _as_scalar(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float):
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text if text else "0"
    return str(value)


def _humanize_token(value: str | None) -> str:
    token = str(value or "").strip()
    if not token:
        return "-"
    return token.replace("_", " ").replace("-", " ").title()


def _csv_text(headers: list[str], rows: list[dict[str, str]]) -> str:
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers)
    writer.writeheader()
    for row in rows:
        writer.writerow({header: row.get(header, "") for header in headers})
    return buffer.getvalue()


def _int_value(value) -> int:
    try:
        return int(round(float(value or 0)))
    except Exception:
        return 0


def _pct_delta(current: float, baseline: float) -> float | None:
    if baseline == 0:
        return None
    return ((current - baseline) / abs(baseline)) * 100.0


def _ensure_feature_enabled(enabled: bool, feature_name: str) -> None:
    if enabled:
        return
    raise ValueError(f"{feature_name} is disabled. Enable the matching feature flag to use this capability.")


def _strategy_context_payload(
    packet: ExecutiveStrategyPacketResponse,
    effective: MerchEffectiveStrategyResponse | None = None,
    *,
    scope_store_options: list[dict[str, str]] | None = None,
    handoff_store_id: str | None = None,
    current_store_id: str | None = None,
) -> dict:
    scenario = packet.scenario
    effective_core = effective.strategy_core if effective and effective.strategy_core else packet.strategy_core
    effective_tag_intensity = effective.tag_intensity if effective else packet.tag_intensity
    return {
        "packet_id": packet.packet_id,
        "title": packet.title,
        "summary": packet.summary,
        "objective": packet.objective.value,
        "lookback_days": packet.lookback_days,
        "scope_label": packet.scope_label,
        "scope_store_ids": list(packet.scope_store_ids or []),
        "scope_store_options": list(scope_store_options or []),
        "handoff_store_id": handoff_store_id,
        "current_store_id": current_store_id,
        "brands": list(packet.brands or []),
        "from_category": packet.from_category,
        "to_category": packet.to_category,
        "min_margin_rate": packet.min_margin_rate,
        "max_discount_pct": packet.max_discount_pct,
        "strategy_core": packet.strategy_core.model_dump(mode="json"),
        "tag_intensity": packet.tag_intensity.value,
        "scenario": scenario.model_dump(mode="json"),
        "effective_strategy_core": effective_core.model_dump(mode="json"),
        "effective_tag_intensity": effective_tag_intensity.value,
        "override_active": bool(effective.override_active) if effective else False,
        "effective_source": effective.source if effective else "packet",
        "override_updated_at": effective.override_updated_at.isoformat() if effective and effective.override_updated_at else None,
        "updated_at": packet.updated_at.isoformat(),
    }


def _merch_category_options() -> list[dict[str, str]]:
    rows = []
    for token, cfg in CATEGORY_TAXONOMY.items():
        label = str(cfg.get("label") or token.replace("_", " ").title())
        rows.append({"value": token, "label": label})
    rows.sort(key=lambda item: item["label"])
    return rows


def _merch_brand_options(store_id: str) -> list[dict[str, str]]:
    with SessionLocal() as db:
        rows = db.execute(
            select(Product.brand)
            .where(Product.store_id == store_id, Product.brand.is_not(None), func.length(func.trim(Product.brand)) > 0)
            .group_by(Product.brand)
            .order_by(Product.brand.asc())
            .limit(300)
        ).all()
    return [{"value": str(row.brand), "label": str(row.brand)} for row in rows if row.brand]


def _merch_compare_store_options(store_id: str) -> list[dict[str, str]]:
    with SessionLocal() as db:
        stores = db.scalars(select(Store).where(Store.id != store_id).order_by(Store.name.asc(), Store.id.asc())).all()
        return [
            {"value": "", "label": "Auto peer set"},
            *[
                {
                    "value": store.id,
                    "label": f"{store.name} ({store.city}, {store.state})",
                }
                for store in stores
            ],
        ]


def _exec_store_options() -> list[dict[str, str]]:
    with SessionLocal() as db:
        stores = db.scalars(select(Store).order_by(Store.name.asc(), Store.id.asc())).all()
        return [
            {
                "value": store.id,
                "label": f"{store.name} ({store.city}, {store.state})",
            }
            for store in stores
        ]


def _strategy_scope_store_options(db: Session, store_ids: list[str]) -> list[dict[str, str]]:
    normalized_ids = [str(value).strip() for value in (store_ids or []) if str(value).strip()]
    if not normalized_ids:
        return []
    rows = db.scalars(select(Store).where(Store.id.in_(normalized_ids))).all()
    by_id = {row.id: row for row in rows}
    options: list[dict[str, str]] = []
    for store_id in normalized_ids:
        row = by_id.get(store_id)
        if row is None:
            options.append({"value": store_id, "label": store_id})
            continue
        options.append(
            {
                "value": row.id,
                "label": f"{row.name} ({row.city}, {row.state})",
            }
        )
    return options


def _exec_brand_options() -> list[dict[str, str]]:
    with SessionLocal() as db:
        rows = db.execute(
            select(Product.brand)
            .where(Product.brand.is_not(None), func.length(func.trim(Product.brand)) > 0)
            .group_by(Product.brand)
            .order_by(Product.brand.asc())
            .limit(500)
        ).all()
        return [{"value": str(row.brand), "label": str(row.brand)} for row in rows if row.brand]


def _exec_event_options() -> list[dict[str, str]]:
    taxonomy_tokens = {
        re.sub(r"[\s\-]+", "_", str(token).strip().lower())
        for token in OCCASION_TO_CATEGORY.keys()
        if str(token).strip()
    }
    with SessionLocal() as db:
        rows = db.execute(
            select(Order.occasion)
            .where(Order.occasion.is_not(None), func.length(func.trim(Order.occasion)) > 0)
            .group_by(Order.occasion)
            .order_by(Order.occasion.asc())
            .limit(200)
        ).all()
    observed_tokens = set()
    for row in rows:
        raw = str(row.occasion or "").strip().lower()
        if not raw:
            continue
        token = re.sub(r"[\s\-]+", "_", raw)
        token = re.sub(r"[^a-z0-9_]", "", token).strip("_")
        if token:
            observed_tokens.add(token)
    tokens = sorted(taxonomy_tokens | observed_tokens)
    if not tokens:
        tokens = ["wedding", "holiday_party", "workwear"]
    return [{"value": token, "label": token.replace("_", " ").title()} for token in tokens]


def _normalize_occasion_token(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    token = re.sub(r"[\s\-]+", "_", raw)
    token = re.sub(r"[^a-z0-9_]", "", token).strip("_")
    return token


def _normalized_occasion_tokens(
    *,
    occasion: str | None = None,
    occasions: list[str] | None = None,
) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    if occasion:
        for chunk in str(occasion).replace(";", ",").replace("|", ",").split(","):
            token = _normalize_occasion_token(chunk)
            if token and token not in seen:
                seen.add(token)
                tokens.append(token)
    for value in occasions or []:
        token = _normalize_occasion_token(value)
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


def _apply_order_occasion_filter(query, *, occasion: str | None = None, occasions: list[str] | None = None):
    tokens = _normalized_occasion_tokens(occasion=occasion, occasions=occasions)
    if not tokens:
        return query
    normalized_occasion_col = func.replace(func.replace(func.lower(func.trim(Order.occasion)), "-", "_"), " ", "_")
    return query.where(normalized_occasion_col.in_(tokens))


def _apply_inventory_filters(
    query,
    *,
    product_query: str | None = None,
    product_id: str | None = None,
    brand: str | None = None,
    category: str | None = None,
    size: str | None = None,
    store_id: str | None = None,
    in_stock_only: bool = True,
):
    if product_id:
        query = query.where(Product.id == product_id.strip())
    if product_query:
        token = product_query.strip().lower()
        if token:
            query = query.where(func.lower(Product.title).like(f"%{token}%"))
    if brand:
        query = query.where(func.lower(Product.brand) == brand.strip().lower())
    if category:
        query = query.where(func.lower(Product.category) == category.strip().lower())
    if size:
        query = query.where(func.lower(Product.size) == size.strip().lower())
    if store_id:
        query = query.where(Product.store_id == store_id)
    if in_stock_only:
        query = query.where(sql_is_in_stock(Product.availability, Product.inventory_qty))
    return query


def _inventory_check_by_store_impl(
    *,
    store_query: str | None = None,
    store_id: str | None = None,
    product_query: str | None = None,
    product_id: str | None = None,
    brand: str | None = None,
    category: str | None = None,
    size: str | None = None,
    limit: int = 100,
) -> InventoryCheckByStoreResponse:
    effective_limit = max(1, min(limit, 500))
    with SessionLocal() as db:
        resolved_store = None
        if store_id or store_query:
            resolved_store = resolve_store(db, store_query=store_query, store_id=store_id).resolved

        in_stock_sku_expr = func.sum(case((sql_is_in_stock(Product.availability, Product.inventory_qty), 1), else_=0))
        preorder_sku_expr = func.sum(case((sql_is_preorder(Product.availability), 1), else_=0))
        out_of_stock_sku_expr = func.sum(case((sql_is_out_of_stock(Product.availability), 1), else_=0))
        not_in_stock_sku_expr = func.sum(case((sql_is_not_in_stock(Product.availability, Product.inventory_qty), 1), else_=0))
        in_stock_units_expr = func.sum(case((sql_is_in_stock(Product.availability, Product.inventory_qty), Product.inventory_qty), else_=0))
        preorder_units_expr = func.sum(case((sql_is_preorder(Product.availability), Product.inventory_qty), else_=0))

        query = (
            select(
                Product.store_id.label("store_id"),
                func.count(Product.id).label("sku_count"),
                in_stock_sku_expr.label("in_stock_skus"),
                preorder_sku_expr.label("preorder_skus"),
                out_of_stock_sku_expr.label("out_of_stock_skus"),
                not_in_stock_sku_expr.label("not_in_stock_skus"),
                in_stock_units_expr.label("in_stock_units"),
                preorder_units_expr.label("preorder_units"),
            )
            .group_by(Product.store_id)
            .order_by(not_in_stock_sku_expr.desc(), func.count(Product.id).desc(), Product.store_id.asc())
        )
        query = _apply_inventory_filters(
            query,
            product_query=product_query,
            product_id=product_id,
            brand=brand,
            category=category,
            size=size,
            store_id=resolved_store.id if resolved_store else None,
            in_stock_only=False,
        )

        grouped_rows = db.execute(query.limit(effective_limit)).all()
        store_ids = [row.store_id for row in grouped_rows if row.store_id]
        stores = db.scalars(select(Store).where(Store.id.in_(store_ids))).all() if store_ids else []
        store_map = {store.id: store for store in stores}

        rows: list[InventoryCheckByStoreRow] = []
        total_skus = 0
        total_in_stock_skus = 0
        total_preorder_skus = 0
        total_out_of_stock_skus = 0
        total_not_in_stock_skus = 0
        total_in_stock_units = 0
        total_preorder_units = 0
        for row in grouped_rows:
            store = store_map.get(row.store_id)
            sku_count = _int_value(row.sku_count)
            in_stock_skus = _int_value(row.in_stock_skus)
            preorder_skus = _int_value(row.preorder_skus)
            out_of_stock_skus = _int_value(row.out_of_stock_skus)
            not_in_stock_skus = _int_value(row.not_in_stock_skus)
            in_stock_units = _int_value(row.in_stock_units)
            preorder_units = _int_value(row.preorder_units)
            not_in_stock_rate_pct = round((not_in_stock_skus / sku_count) * 100.0, 2) if sku_count else 0.0

            total_skus += sku_count
            total_in_stock_skus += in_stock_skus
            total_preorder_skus += preorder_skus
            total_out_of_stock_skus += out_of_stock_skus
            total_not_in_stock_skus += not_in_stock_skus
            total_in_stock_units += in_stock_units
            total_preorder_units += preorder_units

            rows.append(
                InventoryCheckByStoreRow(
                    store_id=row.store_id,
                    store_name=store.name if store else row.store_id,
                    city=store.city if store else "",
                    state=store.state if store else "",
                    sku_count=sku_count,
                    in_stock_skus=in_stock_skus,
                    preorder_skus=preorder_skus,
                    out_of_stock_skus=out_of_stock_skus,
                    not_in_stock_skus=not_in_stock_skus,
                    not_in_stock_rate_pct=not_in_stock_rate_pct,
                    in_stock_units=in_stock_units,
                    preorder_units=preorder_units,
                )
            )

        return InventoryCheckByStoreResponse(
            store=resolved_store,
            product_query=product_query,
            product_id=product_id,
            brand=brand,
            category=category,
            size=size,
            rows=rows,
            total_skus=total_skus,
            total_in_stock_skus=total_in_stock_skus,
            total_preorder_skus=total_preorder_skus,
            total_out_of_stock_skus=total_out_of_stock_skus,
            total_not_in_stock_skus=total_not_in_stock_skus,
            total_in_stock_units=total_in_stock_units,
            total_preorder_units=total_preorder_units,
        )


def _stock_state_label(availability: str | None, inventory_qty: int | None) -> str:
    if is_in_stock(availability, inventory_qty):
        return "in_stock"
    if is_preorder(availability):
        return "preorder"
    if is_out_of_stock(availability, inventory_qty):
        return "out_of_stock"
    return "not_in_stock"


def _inventory_products_impl(
    *,
    store_query: str | None = None,
    store_id: str | None = None,
    product_query: str | None = None,
    product_id: str | None = None,
    brand: str | None = None,
    category: str | None = None,
    size: str | None = None,
    limit: int = 80,
) -> InventoryProductsResponse:
    effective_limit = max(1, min(limit, 500))
    with SessionLocal() as db:
        resolved_store = None
        if store_id or store_query:
            resolved_store = resolve_store(db, store_query=store_query, store_id=store_id).resolved
        query = select(Product)
        query = _apply_inventory_filters(
            query,
            product_query=product_query,
            product_id=product_id,
            brand=brand,
            category=category,
            size=size,
            store_id=resolved_store.id if resolved_store else None,
            in_stock_only=False,
        )
        products = db.scalars(query.order_by(func.lower(Product.title), Product.id).limit(effective_limit)).all()

        rows: list[InventoryProductRow] = []
        total_inventory_units = 0
        for item in products:
            qty = _int_value(item.inventory_qty)
            total_inventory_units += qty
            rows.append(
                InventoryProductRow(
                    product_id=item.id,
                    title=item.title,
                    brand=item.brand,
                    category=item.category,
                    size=item.size,
                    price=float(item.price) if item.price is not None else None,
                    availability=item.availability,
                    stock_state=_stock_state_label(item.availability, item.inventory_qty),
                    inventory_qty=qty,
                    link=item.link,
                    image_url=item.image_link,
                )
            )

        return InventoryProductsResponse(
            store=resolved_store,
            product_query=product_query,
            product_id=product_id,
            brand=brand,
            category=category,
            size=size,
            rows=rows,
            row_count=len(rows),
            total_inventory_units=total_inventory_units,
        )


def _inventory_check_summary_payload(check: InventoryCheckByStoreResponse, current_store_id: str | None) -> dict | None:
    current_row = None
    if current_store_id:
        current_row = next((row for row in check.rows if row.store_id == current_store_id), None)
    if current_row is None and check.rows:
        current_row = check.rows[0]
    if current_row is None:
        return None

    top_risk_rows = sorted(
        [row for row in check.rows if row.not_in_stock_skus > 0],
        key=lambda row: (row.not_in_stock_rate_pct, row.not_in_stock_skus, row.sku_count),
        reverse=True,
    )[:5]

    network_rate = round((check.total_not_in_stock_skus / check.total_skus) * 100.0, 2) if check.total_skus else 0.0
    summary = (
        f"{current_row.store_name}: {current_row.not_in_stock_skus}/{current_row.sku_count} SKUs not currently in stock "
        f"({current_row.not_in_stock_rate_pct:.2f}%). Preorder {current_row.preorder_skus}, out of stock {current_row.out_of_stock_skus}. "
        f"Network risk rate: {network_rate:.2f}%."
    )

    return {
        "summary": summary,
        "current_store": current_row.model_dump(mode="json"),
        "top_risk_stores": [row.model_dump(mode="json") for row in top_risk_rows],
        "totals": {
            "sku_count": check.total_skus,
            "in_stock_skus": check.total_in_stock_skus,
            "preorder_skus": check.total_preorder_skus,
            "out_of_stock_skus": check.total_out_of_stock_skus,
            "not_in_stock_skus": check.total_not_in_stock_skus,
            "not_in_stock_rate_pct": network_rate,
            "in_stock_units": check.total_in_stock_units,
            "preorder_units": check.total_preorder_units,
        },
    }


def _resolve_associate_context(
    session,
    *,
    store_query: str | None = None,
    store_id: str | None = None,
    customer_email: str | None = None,
    customer_id: str | None = None,
    customer_phone_e164: str | None = None,
    phone_last4: str | None = None,
):
    resolved_customer = None
    if customer_email or customer_id or customer_phone_e164 or phone_last4:
        resolved_customer = resolve_customer(
            session,
            email=customer_email,
            customer_id=customer_id,
            phone_e164=customer_phone_e164,
            phone_last4=phone_last4,
        ).resolved

    if store_id or store_query:
        resolved_store = resolve_store(session, store_query=store_query, store_id=store_id).resolved
    elif resolved_customer is not None:
        resolved_store = resolve_store(session, store_id=resolved_customer.home_store_id).resolved
    else:
        raise ValueError("Provide a store or a uniquely resolved customer.")

    return resolved_store, resolved_customer


def _resolve_retrieval_mode(
    retrieval_mode: RetrievalMode,
    customer_resolved: bool,
    occasion: str | None,
    budget_min: float | None,
    budget_max: float | None,
) -> RetrievalMode:
    if retrieval_mode != RetrievalMode.auto:
        return retrieval_mode
    if customer_resolved and (occasion or budget_min is not None or budget_max is not None):
        return RetrievalMode.fast
    return RetrievalMode.semantic


def _associate_recommendation_impl(
    store_query: str | None = None,
    store_id: str | None = None,
    customer_email: str | None = None,
    customer_id: str | None = None,
    customer_phone_e164: str | None = None,
    phone_last4: str | None = None,
    occasion: str | None = None,
    budget_min: float | None = None,
    budget_max: float | None = None,
    top_k: int = 5,
    retrieval_mode: RetrievalMode = RetrievalMode.auto,
    style_constraints: StyleConstraints | None = None,
) -> StoreAssociateRecommendationResponse:
    with SessionLocal() as db:
        resolved_store, resolved_customer = _resolve_associate_context(
            db,
            store_query=store_query,
            store_id=store_id,
            customer_email=customer_email,
            customer_id=customer_id,
            customer_phone_e164=customer_phone_e164,
            phone_last4=phone_last4,
        )
        if resolved_customer is None:
            raise ValueError("Customer was not found. Provide a valid email, customer_id, phone_e164, or phone_last4.")
        effective_retrieval_mode = _resolve_retrieval_mode(
            retrieval_mode,
            customer_resolved=True,
            occasion=occasion,
            budget_min=budget_min,
            budget_max=budget_max,
        )
        req = CustomerRecommendationRequest(
            store_id=resolved_store.id,
            customer_id=resolved_customer.id,
            occasion=occasion,
            budget_min=budget_min,
            budget_max=budget_max,
            top_k=top_k,
            style_constraints=style_constraints,
        )
        rows, strategy, applied_constraints, constraint_stage = customer_recommendations(
            db, req, retrieval_mode=effective_retrieval_mode
        )
        strategy_packet_id = None
        strategy_tag_intensity = None
        if settings.associate_priority_tags_enabled:
            strategy_packet_id, strategy_tag_intensity, rows = apply_execution_tags_for_store(
                db,
                store_id=resolved_store.id,
                recommendations=rows,
            )
        recommendation = CustomerRecommendationResponse(
            store_id=resolved_store.id,
            strategy=strategy,
            recommendations=rows,
            strategy_packet_id=strategy_packet_id,
            strategy_tag_intensity=strategy_tag_intensity,
            applied_style_constraints=applied_constraints,
            constraint_source=applied_constraints.constraint_source if applied_constraints else None,
            constraint_stage=constraint_stage,
        )
        return StoreAssociateRecommendationResponse(
            store=resolved_store,
            customer=resolved_customer,
            recommendation=recommendation,
            retrieval_mode=effective_retrieval_mode,
        )


def _generate_synthetic_impl(params: SyntheticGenerateRequest) -> SyntheticGenerateResponse:
    settings = get_settings()
    with SessionLocal() as db:
        run_id = new_run_id()
        run = SyntheticRun(
            id=run_id,
            seed=params.seed,
            status="generating",
            started_at=datetime.now(timezone.utc),
            config={
                "seed": params.seed,
                "trailing_months": params.trailing_months,
                "volumes": params.volumes.model_dump(),
                "profile_overrides": params.profile_overrides,
            },
        )
        db.add(run)
        db.commit()

        try:
            snapshot = fetch_store_snapshot()
            normalized_stores = normalize_stores(snapshot=snapshot, seed_run_id=run_id)
            volumes = GenerationVolumes(**params.volumes.model_dump())
            artifacts = generate_synthetic_dataset(
                seed=params.seed,
                run_id=run_id,
                stores=normalized_stores,
                volumes=volumes,
                trailing_months=params.trailing_months,
                output_root=Path(settings.data_dir),
                raw_snapshot=snapshot,
            )
            run.status = "generated"
            run.completed_at = datetime.now(timezone.utc)
            db.add(run)
            db.commit()
        except Exception as exc:
            run.status = "failed"
            run.notes = str(exc)[:2000]
            db.add(run)
            db.commit()
            raise ValueError(f"Synthetic generation failed: {exc}") from exc

        return SyntheticGenerateResponse(
            run_id=run_id,
            seed=params.seed,
            output_dir=str(artifacts.output_dir),
            row_counts=artifacts.row_counts,
            stores_discovered=len(normalized_stores),
        )


def _load_synthetic_impl(params: SyntheticLoadRequest) -> SyntheticLoadResponse:
    settings = get_settings()
    with SessionLocal() as db:
        run = db.get(SyntheticRun, params.run_id)
        if not run:
            raise ValueError("run_id not found")

        reset_synthetic_tables(db)
        assert_synthetic_tables_empty(db)
        ordered_entities = ["stores", "customers", "products", "orders", "order_items", "store_daily_metrics"]
        ordered_entities.append("supplier_product_offers")
        requested = set(params.entities)
        entities = [entity for entity in ordered_entities if entity in requested]

        loaded_rows: dict[str, int] = {}
        for entity in entities:
            loaded_rows[entity] = load_entity_csv(db, params.run_id, Path(settings.data_dir), entity)

        run.status = "loaded"
        run.completed_at = datetime.now(timezone.utc)
        db.add(run)
        db.commit()
        return SyntheticLoadResponse(run_id=params.run_id, loaded_rows=loaded_rows)


def _index_products_impl(params: IndexProductsRequest) -> IndexProductsResponse:
    with SessionLocal() as db:
        run = db.get(SyntheticRun, params.run_id)
        if not run:
            raise ValueError("run_id not found")

        stats = index_products_for_run(db, run_id=params.run_id, batch_size=params.batch_size)
        run.status = "indexed"
        run.completed_at = datetime.now(timezone.utc)
        db.add(run)
        db.commit()
        return IndexProductsResponse(run_id=params.run_id, **stats)


@mcp.resource(
    _CUSTOMER_SEARCH_WIDGET_RESOURCE_URI,
    mime_type="text/html;profile=mcp-app",
    meta={**_WIDGET_RESOURCE_META, "openai/widgetDescription": "Minimal customer search workspace for reliable lookup and selection."},
)
def customer_search_widget_resource() -> str:
    return render_widget_html("Customer Workspace", "customer_search_workspace")


@mcp.resource(
    _MERCH_WORKSPACE_RESOURCE_URI,
    mime_type="text/html;profile=mcp-app",
    meta={**_WIDGET_RESOURCE_META, "openai/widgetDescription": "Merchandising workspace for store performance, diagnostics, and trends."},
)
def merch_workspace_resource() -> str:
    return render_widget_html("Merchandising Workspace", "merch_workspace")


@mcp.resource(
    _EXEC_WORKSPACE_RESOURCE_URI,
    mime_type="text/html;profile=mcp-app",
    meta={
        **_WIDGET_RESOURCE_META,
        "openai/widgetDescription": "Executive workspace for company overview, readiness radar, what-if simulation, and campaign approvals.",
    },
)
def exec_workspace_resource() -> str:
    return render_widget_html("Executive Overview Workspace", "exec_workspace")


@mcp.resource(
    _UNIFIED_WORKSPACE_RESOURCE_URI,
    mime_type="text/html;profile=mcp-app",
    meta={
        **_WIDGET_RESOURCE_META,
        "openai/widgetDescription": "Unified executive and merchandising workspace with shared filters and export parity.",
    },
)
def unified_workspace_resource() -> str:
    return render_widget_html("Unified Workspace", "unified_workspace")


@mcp.tool(name="fashion_vector_status", annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True))
def fashion_vector_status(probe: bool = False) -> VectorStatusResponse:
    """Return the current embedding and Pinecone runtime mode, with optional live provider probes."""
    return VectorStatusResponse(**vector_status_payload(probe=probe))


@mcp.tool(name="fashion_latest_run", annotations=_tool_annotations(read_only=True, idempotent=True))
def fashion_latest_run() -> LatestRunResponse:
    """Return the most recent synthetic run currently stored in Postgres."""
    with SessionLocal() as db:
        run = db.scalar(select(SyntheticRun).order_by(SyntheticRun.started_at.desc()).limit(1))
        if not run:
            raise ValueError("No synthetic runs found.")
        return LatestRunResponse(
            id=run.id,
            seed=run.seed,
            status=run.status,
            started_at=run.started_at,
            completed_at=run.completed_at,
        )


@mcp.tool(name="fashion_generate_synthetic", annotations=_tool_annotations(read_only=False, idempotent=False, open_world=True))
def fashion_generate_synthetic(
    seed: int = 20260313,
    trailing_months: int = 24,
    stores: int = 36,
    products: int = 6000,
    customers: int = 12000,
    orders: int = 80000,
    supplier_product_offers: int = 1200,
    profile_overrides: dict[str, float] | None = None,
) -> SyntheticGenerateResponse:
    """Generate a new synthetic retail dataset and persist CSV artifacts for a run."""
    params = SyntheticGenerateRequest(
        seed=seed,
        trailing_months=trailing_months,
        volumes={
            "stores": stores,
            "products": products,
            "customers": customers,
            "orders": orders,
            "supplier_product_offers": supplier_product_offers,
        },
        profile_overrides=profile_overrides or {},
    )
    return _generate_synthetic_impl(params)


@mcp.tool(name="fashion_load_synthetic", annotations=_tool_annotations(read_only=False, idempotent=False))
def fashion_load_synthetic(
    run_id: str,
    entities: list[str] | None = None,
) -> SyntheticLoadResponse:
    """Load generated CSV artifacts for a run into Postgres in parent-to-child order."""
    params = SyntheticLoadRequest(
        run_id=run_id,
        entities=entities
        or ["stores", "customers", "products", "orders", "order_items", "store_daily_metrics", "supplier_product_offers"],
    )
    return _load_synthetic_impl(params)


@mcp.tool(name="fashion_index_products", annotations=_tool_annotations(read_only=False, idempotent=False, open_world=True))
def fashion_index_products(
    run_id: str,
    batch_size: int = 128,
) -> IndexProductsResponse:
    """Generate product embeddings and upsert vectors into Pinecone for a run. This legacy tool runs synchronously; prefer fashion_start_index_products for timeout resilience."""
    return _index_products_impl(IndexProductsRequest(run_id=run_id, batch_size=batch_size))


@mcp.tool(name="fashion_start_index_products", annotations=_tool_annotations(read_only=False, idempotent=False, open_world=True))
def fashion_start_index_products(
    run_id: str,
    batch_size: int = 128,
) -> IndexJobResponse:
    """Queue a background product-indexing job for a run and return immediately."""
    with SessionLocal() as db:
        return enqueue_index_job(db, run_id=run_id, batch_size=batch_size)


@mcp.tool(name="fashion_get_index_job", annotations=_tool_annotations(read_only=True, idempotent=True))
def fashion_get_index_job(job_id: str) -> IndexJobResponse:
    """Return the current status for a background indexing job."""
    with SessionLocal() as db:
        return get_index_job(db, job_id)


@mcp.tool(name="fashion_list_index_jobs", annotations=_tool_annotations(read_only=True, idempotent=True))
def fashion_list_index_jobs(
    run_id: str | None = None,
    limit: int = 20,
) -> IndexJobListResponse:
    """List recent background indexing jobs, optionally filtered by run_id."""
    with SessionLocal() as db:
        return list_index_jobs(db, run_id=run_id, limit=limit)


@mcp.tool(name="fashion_get_run_report", annotations=_tool_annotations(read_only=True, idempotent=True))
def fashion_get_run_report(run_id: str) -> RunReportResponse:
    """Return generated counts, loaded counts, embedding coverage, and validation status for a run."""
    from app.services.validation import run_validation_checks

    settings = get_settings()
    with SessionLocal() as db:
        run = db.get(SyntheticRun, run_id)
        if not run:
            raise ValueError("run_id not found")

        generated_counts = read_generated_counts(Path(settings.data_dir), run_id)
        loaded_counts = current_loaded_counts(db, run_id)
        failure_count = run_validation_checks(db, run_id)

        product_count = db.scalar(select(func.count()).select_from(Product).where(Product.seed_run_id == run_id)) or 0
        embedding_total = (
            db.scalar(select(func.count()).select_from(ProductEmbedding).where(ProductEmbedding.seed_run_id == run_id)) or 0
        )
        embedding_indexed = (
            db.scalar(
                select(func.count())
                .select_from(ProductEmbedding)
                .where(ProductEmbedding.seed_run_id == run_id, ProductEmbedding.status.in_(["indexed", "local_only"]))
            )
            or 0
        )

        return RunReportResponse(
            run_id=run_id,
            status=run.status,
            generated_counts=generated_counts,
            loaded_counts=loaded_counts,
            embedding_coverage={
                "products": product_count,
                "embeddings": embedding_total,
                "indexed_or_local": embedding_indexed,
                "coverage_pct": round((embedding_indexed / product_count) * 100.0, 2) if product_count else 0.0,
            },
            validation_failures=failure_count,
            generated_at=run.started_at,
        )


@mcp.tool(name="fashion_customer_recommendations", annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True))
def fashion_customer_recommendations(
    store_id: str,
    customer_id: str | None = None,
    occasion: str | None = None,
    budget_min: float | None = None,
    budget_max: float | None = None,
    top_k: int = 12,
    style_constraints: StyleConstraints | None = None,
) -> CustomerRecommendationResponse:
    """Return customer-facing product recommendations.

    If the user uploaded an image in chat, extract style cues and pass them via
    `style_constraints` so recommendations can be guided by those image-derived
    attributes.
    """
    params = CustomerRecommendationRequest(
        store_id=store_id,
        customer_id=customer_id,
        occasion=occasion,
        budget_min=budget_min,
        budget_max=budget_max,
        top_k=top_k,
        style_constraints=style_constraints,
    )
    with SessionLocal() as db:
        rows, strategy, applied_constraints, constraint_stage = customer_recommendations(db, params)
        strategy_packet_id = None
        strategy_tag_intensity = None
        if settings.associate_priority_tags_enabled:
            strategy_packet_id, strategy_tag_intensity, rows = apply_execution_tags_for_store(
                db,
                store_id=params.store_id,
                recommendations=rows,
            )
        return CustomerRecommendationResponse(
            store_id=params.store_id,
            strategy=strategy,
            recommendations=rows,
            strategy_packet_id=strategy_packet_id,
            strategy_tag_intensity=strategy_tag_intensity,
            applied_style_constraints=applied_constraints,
            constraint_source=applied_constraints.constraint_source if applied_constraints else None,
            constraint_stage=constraint_stage,
        )


@mcp.tool(name="fashion_merchandising_recommendations", annotations=_tool_annotations(read_only=True, idempotent=True))
def fashion_merchandising_recommendations(
    store_id: str,
    objective: Objective = Objective.sell_through,
    lookback_days: int = 90,
    top_k: int = 20,
) -> MerchandisingRecommendationResponse:
    """Return merchandising recommendations for a store using sell-through, margin, or revenue objectives."""
    params = MerchandisingRecommendationRequest(
        store_id=store_id,
        objective=objective,
        lookback_days=lookback_days,
        top_k=top_k,
    )
    with SessionLocal() as db:
        rows = merchandising_recommendations(db, params)
        return MerchandisingRecommendationResponse(store_id=params.store_id, objective=params.objective, recommendations=rows)


@mcp.tool(
    name="fashion_resolve_store",
    annotations=_tool_annotations(read_only=True, idempotent=True),
)
def fashion_resolve_store(store_query: str) -> StoreResolutionResponse:
    """Resolve a fuzzy store query such as a city, store name, or store identifier to a canonical store."""
    with SessionLocal() as db:
        match = resolve_store(db, store_query=store_query)
        return StoreResolutionResponse(query=store_query, resolved=match.resolved, alternatives=match.alternatives)


@mcp.tool(
    name="fashion_resolve_customer",
    annotations=_tool_annotations(read_only=True, idempotent=True),
)
def fashion_resolve_customer(
    email: str | None = None,
    customer_id: str | None = None,
    phone_e164: str | None = None,
    phone_last4: str | None = None,
) -> CustomerResolutionResponse:
    """Resolve a customer by exact email, customer_id, full synthetic phone, or unique phone last4."""
    with SessionLocal() as db:
        match = resolve_customer(
            db,
            email=email,
            customer_id=customer_id,
            phone_e164=phone_e164,
            phone_last4=phone_last4,
        )
        query = email or customer_id or phone_e164 or phone_last4 or ""
        return CustomerResolutionResponse(query=query, resolved=match.resolved)


@mcp.tool(
    name="fashion_lookup_customer",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_lookup_customer(query: str, limit: int = 10) -> CustomerLookupResponse:
    """Lookup a customer from a single operator query. Exact identifiers resolve directly; partial or ambiguous queries return candidates."""
    normalized = query.strip()
    digits_only = "".join(ch for ch in normalized if ch.isdigit())
    with SessionLocal() as db:
        try:
            if "@" in normalized:
                resolved = resolve_customer(db, email=normalized).resolved
                return CustomerLookupResponse(query=query, mode="resolved", resolved=resolved)
            if normalized.startswith("cust_"):
                resolved = resolve_customer(db, customer_id=normalized).resolved
                return CustomerLookupResponse(query=query, mode="resolved", resolved=resolved)
            if len(digits_only) >= 10:
                resolved = resolve_customer(db, phone_e164=normalized).resolved
                return CustomerLookupResponse(query=query, mode="resolved", resolved=resolved)
        except ValueError:
            pass

        candidates = find_customers(db, query=query, limit=limit).results
        if len(candidates) == 1 and candidates[0].match_score >= 95:
            return CustomerLookupResponse(query=query, mode="resolved", resolved=candidates[0])
        return CustomerLookupResponse(query=query, mode="candidates", candidates=candidates)


@mcp.tool(
    name="fashion_find_customers",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_find_customers(query: str, limit: int = 10) -> CustomerSearchResponse:
    """Search customers by name, email, full phone, or phone last4 for associate workflows."""
    with SessionLocal() as db:
        return find_customers(db, query=query, limit=limit)


@mcp.tool(
    name="fashion_customer_value_summary",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_customer_value_summary(
    customer_id: str,
    lookback_days: int = 180,
    forecast_weeks: int = 8,
    purchase_scope: PurchaseScope = PurchaseScope.all_stores,
) -> CustomerValueSummaryResponse:
    """Return customer value metrics, history, and baseline spend projection."""
    params = CustomerValueSummaryRequest(
        customer_id=customer_id,
        lookback_days=lookback_days,
        forecast_weeks=forecast_weeks,
        purchase_scope=purchase_scope,
    )
    with SessionLocal() as db:
        return customer_value_summary(
            db,
            customer_id=params.customer_id,
            lookback_days=params.lookback_days,
            forecast_weeks=params.forecast_weeks,
            purchase_scope=params.purchase_scope,
        )


def _customer_search_workspace_payload(
    query: str | None,
    limit: int,
    selected_customer_id: str | None = None,
    initial_style_constraints: StyleConstraints | None = None,
    initial_notice: str | None = None,
    initial_email_draft_id: str | None = None,
    initial_email_subject: str | None = None,
    initial_email_body: str | None = None,
    initial_recommendation_response: StoreAssociateRecommendationResponse | None = None,
    initial_selected_product_ids: list[str] | None = None,
) -> dict:
    normalized_query = (query or "").strip()
    initial_constraints_payload = (
        initial_style_constraints.model_dump(mode="json") if initial_style_constraints is not None else None
    )
    initial_recommendation_payload = (
        initial_recommendation_response.model_dump(mode="json")
        if initial_recommendation_response is not None
        else None
    )
    normalized_initial_product_ids: list[str] = []
    for value in initial_selected_product_ids or []:
        token = str(value or "").strip()
        if token and token not in normalized_initial_product_ids:
            normalized_initial_product_ids.append(token)
    if not normalized_query:
        seeded_results: list[dict] = []
        seeded_resolved: dict | None = None
        if selected_customer_id:
            lookup = fashion_lookup_customer(selected_customer_id, limit=1)
            if lookup.mode == "resolved" and lookup.resolved is not None:
                seeded_resolved = lookup.resolved.model_dump(mode="json")
                seeded_results = [seeded_resolved]
        return {
            "query": "",
            "mode": "resolved" if seeded_resolved else "idle",
            "resolved": seeded_resolved,
            "results": seeded_results,
            "selected_customer_id": selected_customer_id,
            "initial_style_constraints": initial_constraints_payload,
            "initial_notice": initial_notice,
            "initial_email_draft_id": initial_email_draft_id,
            "initial_email_subject": initial_email_subject,
            "initial_email_body": initial_email_body,
            "initial_recommendation_response": initial_recommendation_payload,
            "initial_selected_product_ids": normalized_initial_product_ids,
            "uiHints": {
                "searchPlaceholder": "Search by name, email, or phone",
                "emptyState": "Type a customer name, email, or phone number and run search.",
            },
        }

    lookup = fashion_lookup_customer(normalized_query, limit=limit)
    if lookup.mode == "resolved" and lookup.resolved is not None:
        results = [lookup.resolved.model_dump(mode="json")]
    else:
        results = [candidate.model_dump(mode="json") for candidate in lookup.candidates]

    return {
        "query": normalized_query,
        "mode": lookup.mode,
        "resolved": lookup.resolved.model_dump(mode="json") if lookup.resolved else None,
        "results": results,
        "selected_customer_id": selected_customer_id,
        "initial_style_constraints": initial_constraints_payload,
        "initial_notice": initial_notice,
        "initial_email_draft_id": initial_email_draft_id,
        "initial_email_subject": initial_email_subject,
        "initial_email_body": initial_email_body,
        "initial_recommendation_response": initial_recommendation_payload,
        "initial_selected_product_ids": normalized_initial_product_ids,
        "uiHints": {
            "searchPlaceholder": "Search by name, email, or phone",
            "emptyState": "No customers matched the current query.",
        },
    }


def _merch_workspace_payload(
    *,
    store_query: str | None = None,
    store_id: str | None = None,
    question: str | None = None,
    objective: Objective = Objective.margin,
    lookback_days: int = 90,
    top_k: int = 9,
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
    occasions: list[str] | None = None,
    inventory_scope: InventoryScope = InventoryScope.combined,
    future_window_days: int = 120,
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
    compare_store_id: str | None = None,
    strategy_packet_id: str | None = None,
    initial_notice: str | None = None,
) -> dict:
    bounded_lookback = max(7, min(lookback_days, 730))
    bounded_top_k = max(1, min(top_k, 50))
    bounded_future_window_days = max(1, min(int(future_window_days or 120), 365))
    resolved_occasions = _normalized_occasion_tokens(occasion=occasion, occasions=occasions)
    resolved_occasion = resolved_occasions[0] if resolved_occasions else None
    effective_objective = objective
    effective_lookback = bounded_lookback
    effective_brand = brand
    effective_category = category
    effective_store_id = store_id
    handoff_store_id = store_id
    scope_store_options: list[dict[str, str]] = []
    strategy_context = None
    packet = None
    effective_strategy = None
    if settings.merch_strategy_context_enabled and strategy_packet_id:
        with SessionLocal() as db:
            packet = get_strategy_packet(db, strategy_packet_id)
            if not effective_store_id and store_query:
                resolved_store = resolve_store(db, store_query=store_query).resolved
                effective_store_id = resolved_store.id
            if not effective_store_id and packet.scope_store_ids:
                effective_store_id = packet.scope_store_ids[0]
            if not handoff_store_id:
                handoff_store_id = effective_store_id
            scope_store_options = _strategy_scope_store_options(db, packet.scope_store_ids)
            if effective_store_id:
                effective_strategy = get_effective_merch_strategy(
                    db,
                    store_id=effective_store_id,
                    strategy_packet_id=packet.packet_id,
                )
        source_core = effective_strategy.strategy_core if effective_strategy and effective_strategy.strategy_core else packet.strategy_core
        if objective == Objective.margin:
            effective_objective = source_core.objective
        if bounded_lookback == 90:
            effective_lookback = source_core.lookback_days
        if not effective_brand and source_core.brands:
            effective_brand = ", ".join(source_core.brands)
        if not effective_category and source_core.category:
            effective_category = source_core.category

    initial_actions = fashion_merch_action_recommendations(
        store_query=store_query,
        store_id=effective_store_id,
        question=question,
        objective=effective_objective,
        lookback_days=effective_lookback,
        top_k=bounded_top_k,
        category=effective_category,
        brand=effective_brand,
        price_band=price_band,
        occasion=resolved_occasion,
        occasions=resolved_occasions,
        compare_mode=compare_mode,
        peer_mode=peer_mode,
        compare_store_id=compare_store_id,
    )
    initial_inventory = _merch_inventory_view_impl(
        MerchInventoryViewRequest(
            store_id=initial_actions.store.id,
            lookback_days=effective_lookback,
            category=effective_category,
            brand=effective_brand,
            price_band=price_band,
            occasion=resolved_occasion,
            occasions=resolved_occasions,
            inventory_scope=inventory_scope,
            future_window_days=bounded_future_window_days,
            limit=200,
        )
    )
    if settings.merch_strategy_context_enabled and strategy_packet_id:
        with SessionLocal() as db:
            if packet is None:
                packet = get_strategy_packet(db, strategy_packet_id)
            if not scope_store_options:
                scope_store_options = _strategy_scope_store_options(db, packet.scope_store_ids)
            if effective_strategy is None:
                effective_strategy = get_effective_merch_strategy(
                    db,
                    store_id=initial_actions.store.id,
                    strategy_packet_id=packet.packet_id,
                )
        strategy_context = _strategy_context_payload(
            packet,
            effective_strategy,
            scope_store_options=scope_store_options,
            handoff_store_id=handoff_store_id or initial_actions.store.id,
            current_store_id=initial_actions.store.id,
        )
    inventory_brand = None
    if effective_brand:
        inventory_brand = str(effective_brand).split(",", 1)[0].strip() or None
    inventory_check = _inventory_check_by_store_impl(
        store_id=initial_actions.store.id,
        brand=inventory_brand,
        category=effective_category,
        limit=12,
    )
    inventory_products = _inventory_products_impl(
        store_id=initial_actions.store.id,
        brand=inventory_brand,
        category=effective_category,
        limit=80,
    )
    inventory_check_payload = _inventory_check_summary_payload(inventory_check, current_store_id=initial_actions.store.id)
    compare_store_options = _merch_compare_store_options(initial_actions.store.id)
    brand_options = _merch_brand_options(initial_actions.store.id)
    return {
        "store": initial_actions.store.model_dump(mode="json"),
        "filters": MerchWorkspaceFilters(
            question=question,
            objective=effective_objective,
            category=effective_category,
            brand=effective_brand,
            price_band=price_band,
            occasion=resolved_occasion,
            occasions=resolved_occasions,
            lookback_days=effective_lookback,
            inventory_scope=inventory_scope,
            future_window_days=bounded_future_window_days,
            compare_mode=compare_mode,
            peer_mode=peer_mode,
            compare_store_id=compare_store_id,
            top_k=bounded_top_k,
        ).model_dump(mode="json"),
        "initial_result": initial_inventory.model_dump(mode="json"),
        "last_result": None,
        "last_tool": "fashion_merch_inventory_view",
        "initial_notice": initial_notice,
        "strategy_context": strategy_context,
        "inventory_check": inventory_check_payload,
        "inventory_products": inventory_products.model_dump(mode="json"),
        "uiHints": {
            "questionPlaceholder": "Optional context (e.g., wedding occasion, protect margin, next 8 weeks)",
            "emptyState": "Use Inventory filters to view/export current and potential assortment rows.",
            "categoryOptions": _merch_category_options(),
            "brandOptions": brand_options,
            "occasionOptions": _exec_event_options(),
            "compareStoreOptions": compare_store_options,
            "features": {
                "merchStrategyContextEnabled": settings.merch_strategy_context_enabled,
            },
            "actionDefinitions": {
                "feature": "Strongest demand momentum versus baseline with healthy margin/inventory for full-price placement.",
                "promote": "Featured Campaign candidates: margin >= 42%, inventory >= 6 units, and softer demand that can respond to campaign support.",
                "deprioritize": "Inventory pressure plus below-baseline demand; reduce exposure and floor priority.",
            },
        },
    }


def _exec_workspace_payload(
    *,
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    lookback_days: int = 90,
    objective: Objective = Objective.revenue,
    top_k_stores: int = 12,
    events: list[str] | None = None,
    brands: list[str] | None = None,
    discount_pct: float = 10.0,
    floor_space_shift_pct: float = 5.0,
    from_category: str | None = "womens_apparel",
    to_category: str | None = "shoes",
    to_email: str | None = None,
    autopilot_top_k: int = 6,
    optimize_discount_min_pct: float = 0.0,
    optimize_discount_max_pct: float = 20.0,
    optimize_discount_step_pct: float = 5.0,
    optimize_shift_min_pct: float = 0.0,
    optimize_shift_max_pct: float = 20.0,
    optimize_shift_step_pct: float = 5.0,
    optimize_top_k_scenarios: int = 3,
    min_margin_rate: float = 0.40,
    max_discount_pct: float = 20.0,
    strategy_packet_id: str | None = None,
    initial_notice: str | None = None,
) -> dict:
    bounded_lookback = max(7, min(lookback_days, 730))
    bounded_top_k = max(1, min(top_k_stores, 50))
    bounded_autopilot_top_k = max(1, min(autopilot_top_k, 20))
    bounded_optimize_top_k = max(1, min(optimize_top_k_scenarios, 10))
    event_options = _exec_event_options()
    default_events = [item["value"] for item in event_options]

    with SessionLocal() as db:
        initial_result = executive_overview(
            db,
            store_query=store_query,
            store_id=store_id,
            store_ids=store_ids,
            lookback_days=bounded_lookback,
            objective=objective,
            top_k_stores=bounded_top_k,
        )

    effective_store_ids = [value for value in (store_ids or []) if str(value).strip()]
    effective_to_email = (to_email or "").strip().lower() or _DEFAULT_EXEC_TO_EMAIL
    return {
        "filters": ExecutiveWorkspaceFilters(
            lookback_days=bounded_lookback,
            objective=objective,
            top_k_stores=bounded_top_k,
            events=events or default_events,
            brands=[str(value).strip() for value in (brands or []) if str(value).strip()],
            store_id=store_id,
            store_ids=effective_store_ids,
            discount_pct=discount_pct,
            floor_space_shift_pct=floor_space_shift_pct,
            from_category=from_category,
            to_category=to_category,
            to_email=effective_to_email,
            autopilot_top_k=bounded_autopilot_top_k,
            optimize_discount_min_pct=max(0.0, min(float(optimize_discount_min_pct), 60.0)),
            optimize_discount_max_pct=max(0.0, min(float(optimize_discount_max_pct), 60.0)),
            optimize_discount_step_pct=max(1.0, min(float(optimize_discount_step_pct), 20.0)),
            optimize_shift_min_pct=max(-40.0, min(float(optimize_shift_min_pct), 40.0)),
            optimize_shift_max_pct=max(-40.0, min(float(optimize_shift_max_pct), 40.0)),
            optimize_shift_step_pct=max(1.0, min(float(optimize_shift_step_pct), 20.0)),
            optimize_top_k_scenarios=bounded_optimize_top_k,
            min_margin_rate=max(0.0, min(float(min_margin_rate), 1.0)),
            max_discount_pct=max(0.0, min(float(max_discount_pct), 60.0)),
            strategy_packet_id=strategy_packet_id,
        ).model_dump(mode="json"),
        "initial_result": initial_result.model_dump(mode="json"),
        "last_result": None,
        "last_tool": "fashion_exec_overview",
        "initial_notice": initial_notice,
        "uiHints": {
            "emptyState": "Run Overview, Radar, Simulator, or Autopilot to populate this workspace.",
            "events": event_options,
            "categoryOptions": _merch_category_options(),
            "brandOptions": _exec_brand_options(),
            "storeOptions": _exec_store_options(),
            "features": {
                "execAutoOptimizeEnabled": settings.exec_auto_optimize_enabled,
                "strategyPacketEnabled": settings.strategy_packet_enabled,
            },
        },
    }


def _normalized_string_tokens(values: list[str] | None) -> list[str]:
    tokens: list[str] = []
    for value in values or []:
        token = str(value or "").strip()
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def _normalized_unified_brands(*, brand: str | None = None, brands: list[str] | None = None) -> list[str]:
    normalized = _normalized_string_tokens(brands)
    if brand:
        for chunk in str(brand).replace(";", ",").replace("|", ",").split(","):
            token = chunk.strip()
            if token and token not in normalized:
                normalized.append(token)
    return normalized


def _resolve_unified_store_scope(
    db,
    *,
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    active_store_id: str | None = None,
) -> tuple[list[str], str | None, dict[str, Store]]:
    resolved_store_ids = _normalized_string_tokens(store_ids)
    resolved_active_store_id = str(active_store_id or "").strip() or None

    if store_id or store_query:
        resolved = resolve_store(db, store_query=store_query, store_id=store_id).resolved
        if resolved.id not in resolved_store_ids:
            resolved_store_ids.append(resolved.id)
        if resolved_active_store_id is None:
            resolved_active_store_id = resolved.id

    if not resolved_store_ids:
        resolved_store_ids = list(db.scalars(select(Store.id).order_by(Store.id.asc())).all())

    if resolved_active_store_id and resolved_active_store_id not in resolved_store_ids:
        resolved_store_ids.insert(0, resolved_active_store_id)

    rows = db.scalars(select(Store).where(Store.id.in_(resolved_store_ids))).all() if resolved_store_ids else []
    store_map = {row.id: row for row in rows}
    missing_ids = [value for value in resolved_store_ids if value not in store_map]
    if missing_ids:
        raise ValueError(f"Unknown store_ids: {', '.join(missing_ids)}")

    ordered_store_map = {store_id_value: store_map[store_id_value] for store_id_value in resolved_store_ids}
    if resolved_active_store_id is None and resolved_store_ids:
        resolved_active_store_id = resolved_store_ids[0]
    if resolved_active_store_id and resolved_active_store_id not in ordered_store_map:
        raise ValueError(f"active_store_id {resolved_active_store_id} is not in the selected store scope.")
    return resolved_store_ids, resolved_active_store_id, ordered_store_map


def _unified_inventory_group_key(row: UnifiedInventoryViewRow) -> tuple:
    if row.row_type == MerchInventoryRowType.potential_offer:
        return (
            row.row_type.value,
            str(row.offer_id or ""),
            str(row.title or "").strip().lower(),
            str(row.brand or "").strip().lower(),
            str(row.category or "").strip().lower(),
            str(row.available_on or ""),
        )
    return (
        row.row_type.value,
        str(row.product_id or ""),
        str(row.title or "").strip().lower(),
        str(row.brand or "").strip().lower(),
        str(row.category or "").strip().lower(),
        str(row.size or "").strip().lower(),
        _as_scalar(row.price),
    )


def _aggregate_unified_inventory_rows(rows: list[UnifiedInventoryViewRow]) -> list[UnifiedInventoryViewRow]:
    grouped: dict[tuple, dict] = {}
    for row in rows:
        key = _unified_inventory_group_key(row)
        payload = grouped.get(key)
        if payload is None:
            payload = {
                "row": UnifiedInventoryViewRow(
                    row_type=row.row_type,
                    store_id=None,
                    store_name=None,
                    store_count=0,
                    product_id=row.product_id,
                    offer_id=row.offer_id,
                    title=row.title,
                    brand=row.brand,
                    category=row.category,
                    size=row.size,
                    price=row.price,
                    availability=row.availability,
                    stock_state=row.stock_state,
                    inventory_qty=0,
                    available_on=row.available_on,
                    offer_status=row.offer_status,
                    link=row.link,
                    image_url=row.image_url,
                    perf_revenue=0.0,
                    perf_units=0.0,
                    perf_margin_rate=0.0,
                ),
                "store_ids": set(),
                "margin_value": 0.0,
                "margin_rate_sum": 0.0,
                "margin_rate_count": 0,
            }
            grouped[key] = payload

        item: UnifiedInventoryViewRow = payload["row"]
        store_id = str(row.store_id or "").strip()
        if store_id:
            payload["store_ids"].add(store_id)
        item.store_count = len(payload["store_ids"]) if payload["store_ids"] else max(item.store_count, 1)
        item.inventory_qty = int(item.inventory_qty or 0) + int(row.inventory_qty or 0)
        item.perf_revenue = float(item.perf_revenue or 0.0) + float(row.perf_revenue or 0.0)
        item.perf_units = float(item.perf_units or 0.0) + float(row.perf_units or 0.0)
        payload["margin_value"] += float(row.perf_margin_rate or 0.0) * float(row.perf_revenue or 0.0)
        payload["margin_rate_sum"] += float(row.perf_margin_rate or 0.0)
        payload["margin_rate_count"] += 1

    aggregated: list[UnifiedInventoryViewRow] = []
    for payload in grouped.values():
        item: UnifiedInventoryViewRow = payload["row"]
        revenue = float(item.perf_revenue or 0.0)
        if revenue > 0:
            item.perf_margin_rate = float(payload["margin_value"] / revenue)
        elif payload["margin_rate_count"] > 0:
            item.perf_margin_rate = float(payload["margin_rate_sum"] / payload["margin_rate_count"])
        item.store_count = len(payload["store_ids"]) if payload["store_ids"] else max(int(item.store_count or 0), 1)
        aggregated.append(item)

    aggregated.sort(
        key=lambda row: (
            row.row_type.value,
            -float(row.perf_revenue or 0.0),
            str(row.brand or "").lower(),
            str(row.title or "").lower(),
            str(row.product_id or row.offer_id or ""),
        )
    )
    return aggregated


def _aggregate_unified_recommendation_rows(
    rows: list[UnifiedActionRecommendationRow],
    top_k: int,
) -> list[UnifiedActionRecommendationRow]:
    grouped: dict[str, dict] = {}
    for row in rows:
        key = str(row.product_id or "").strip() or "|".join(
            [
                str(row.title or "").strip().lower(),
                str(row.brand or "").strip().lower(),
                str(row.category or "").strip().lower(),
            ]
        )
        payload = grouped.get(key)
        if payload is None:
            payload = {
                "representative": row,
                "store_ids": set(),
                "metric_value": 0.0,
                "peer_delta": 0.0,
                "prior_period_delta": 0.0,
                "has_prior_delta": False,
                "model_action_counter": Counter(),
                "final_action_counter": Counter(),
                "model_priority_counter": Counter(),
                "final_priority_counter": Counter(),
                "override_notes": set(),
            }
            grouped[key] = payload

        if row.store_id:
            payload["store_ids"].add(row.store_id)
        payload["metric_value"] += float(row.metric_value or 0.0)
        payload["peer_delta"] += float(row.peer_delta or 0.0)
        if row.prior_period_delta is not None:
            payload["prior_period_delta"] += float(row.prior_period_delta or 0.0)
            payload["has_prior_delta"] = True
        payload["model_action_counter"][row.model_action.value] += 1
        payload["final_action_counter"][row.final_action.value] += 1
        payload["model_priority_counter"][row.model_priority_tier.value] += 1
        payload["final_priority_counter"][row.final_priority_tier.value] += 1
        if row.override_note:
            payload["override_notes"].add(row.override_note.strip())
        if float(row.metric_value or 0.0) > float(payload["representative"].metric_value or 0.0):
            payload["representative"] = row

    aggregated: list[UnifiedActionRecommendationRow] = []
    for payload in grouped.values():
        representative: UnifiedActionRecommendationRow = payload["representative"]
        model_action_value = payload["model_action_counter"].most_common(1)[0][0]
        final_action_value = payload["final_action_counter"].most_common(1)[0][0]
        model_priority_value = payload["model_priority_counter"].most_common(1)[0][0]
        final_priority_value = payload["final_priority_counter"].most_common(1)[0][0]
        notes = sorted(payload["override_notes"])
        aggregated.append(
            UnifiedActionRecommendationRow(
                store_id=None,
                store_name=None,
                store_count=max(1, len(payload["store_ids"])),
                product_id=representative.product_id,
                title=representative.title,
                brand=representative.brand,
                category=representative.category,
                price=representative.price,
                link=representative.link,
                image_url=representative.image_url,
                price_band=representative.price_band,
                occasion=representative.occasion,
                metric_value=float(payload["metric_value"]),
                peer_delta=float(payload["peer_delta"]),
                prior_period_delta=(
                    float(payload["prior_period_delta"])
                    if payload["has_prior_delta"]
                    else None
                ),
                rationale=representative.rationale,
                model_action=MerchAction(model_action_value),
                model_priority_tier=MerchPriorityTier(model_priority_value),
                final_action=MerchFinalAction(final_action_value),
                final_priority_tier=MerchPriorityTier(final_priority_value),
                override_note=notes[0] if len(notes) == 1 else None,
            )
        )

    aggregated.sort(
        key=lambda row: (
            -float(row.metric_value or 0.0),
            str(row.brand or "").lower(),
            str(row.title or "").lower(),
            str(row.product_id or ""),
        )
    )
    return aggregated[: max(1, min(int(top_k or 12), 100))]


def _aggregate_unified_mix_rows(
    rows: list[UnifiedProductMixRecommendationRow],
    top_k: int,
) -> list[UnifiedProductMixRecommendationRow]:
    grouped: dict[tuple, dict] = {}
    for row in rows:
        key = (
            row.action.value,
            str(row.current_product_id or ""),
            str(row.offer_id or ""),
            str(row.brand or "").strip().lower(),
            str(row.category or "").strip().lower(),
            str(row.current_title or "").strip().lower(),
            str(row.offer_title or "").strip().lower(),
        )
        payload = grouped.get(key)
        if payload is None:
            payload = {
                "representative": row,
                "store_ids": set(),
                "fit_score_sum": 0.0,
                "count": 0,
                "expected_mix_impact_sum": 0.0,
            }
            grouped[key] = payload
        if row.store_id:
            payload["store_ids"].add(row.store_id)
        payload["fit_score_sum"] += float(row.fit_score or 0.0)
        payload["expected_mix_impact_sum"] += float(row.expected_mix_impact or 0.0)
        payload["count"] += 1

    aggregated: list[UnifiedProductMixRecommendationRow] = []
    for payload in grouped.values():
        representative: UnifiedProductMixRecommendationRow = payload["representative"]
        count = max(1, int(payload["count"] or 1))
        aggregated.append(
            UnifiedProductMixRecommendationRow(
                store_id=None,
                store_name=None,
                store_count=max(1, len(payload["store_ids"])),
                action=representative.action,
                fit_score=float(payload["fit_score_sum"] / count),
                expected_mix_impact=float(payload["expected_mix_impact_sum"]),
                rationale=representative.rationale,
                brand=representative.brand,
                category=representative.category,
                current_product_id=representative.current_product_id,
                current_title=representative.current_title,
                current_revenue=representative.current_revenue,
                current_units=representative.current_units,
                offer_id=representative.offer_id,
                offer_title=representative.offer_title,
                offer_status=representative.offer_status,
                available_on=representative.available_on,
                offer_price=representative.offer_price,
            )
        )

    action_rank = {"swap": 0, "add": 1, "hold": 2, "reduce": 3}
    aggregated.sort(
        key=lambda row: (
            action_rank.get(row.action.value, 9),
            -float(row.fit_score or 0.0),
            -float(row.expected_mix_impact or 0.0),
            str(row.offer_title or row.current_title or "").lower(),
        )
    )
    return aggregated[: max(1, min(int(top_k or 12), 100))]


def _unified_override_maps(
    overrides: list[UnifiedRecommendationOverride],
) -> tuple[dict[str, UnifiedRecommendationOverride], dict[tuple[str, str], UnifiedRecommendationOverride]]:
    global_map: dict[str, UnifiedRecommendationOverride] = {}
    store_map: dict[tuple[str, str], UnifiedRecommendationOverride] = {}
    for override in overrides or []:
        product_id = str(override.product_id or "").strip()
        store_id = str(override.store_id or "").strip()
        if not product_id:
            continue
        if store_id:
            store_map[(store_id, product_id)] = override
        else:
            global_map[product_id] = override
    return global_map, store_map


def _resolve_unified_override(
    *,
    override_scope: OverrideScope,
    store_id: str,
    product_id: str,
    global_map: dict[str, UnifiedRecommendationOverride],
    store_map: dict[tuple[str, str], UnifiedRecommendationOverride],
) -> UnifiedRecommendationOverride | None:
    if override_scope == OverrideScope.global_scope:
        return global_map.get(product_id)
    return store_map.get((store_id, product_id))


def _unified_overview_impl(params: UnifiedOverviewRequest) -> UnifiedOverviewResponse:
    with SessionLocal() as db:
        resolved_store_ids, resolved_active_store_id, _ = _resolve_unified_store_scope(
            db,
            store_query=params.store_query,
            store_id=params.store_id,
            store_ids=params.store_ids,
            active_store_id=params.active_store_id,
        )
        overview = executive_overview(
            db,
            store_ids=resolved_store_ids,
            lookback_days=params.lookback_days,
            objective=params.objective,
            top_k_stores=params.top_k_stores,
        )

    return UnifiedOverviewResponse(
        summary=overview.summary,
        lookback_days=overview.lookback_days,
        objective=overview.objective,
        generated_at=overview.generated_at,
        store_ids=resolved_store_ids,
        active_store_id=resolved_active_store_id,
        total_revenue=overview.total_revenue,
        total_units=overview.total_units,
        margin_rate=overview.margin_rate,
        prior_revenue=overview.prior_revenue,
        prior_margin_rate=overview.prior_margin_rate,
        revenue_delta_pct=overview.revenue_delta_pct,
        store_count=overview.store_count,
        stores=overview.stores,
        trend=overview.trend,
    )


def _unified_inventory_view_impl(params: UnifiedInventoryViewRequest) -> UnifiedInventoryViewResponse:
    with SessionLocal() as db:
        resolved_store_ids, resolved_active_store_id, store_map = _resolve_unified_store_scope(
            db,
            store_query=params.store_query,
            store_id=params.store_id,
            store_ids=params.store_ids,
            active_store_id=params.active_store_id,
        )

    brand_values = _normalized_unified_brands(brand=params.brand, brands=params.brands)
    brand_csv = ", ".join(brand_values) if brand_values else None
    occasion_values = _normalized_occasion_tokens(occasion=params.occasion, occasions=params.occasions)
    resolved_occasion = occasion_values[0] if occasion_values else None

    store_rows: list[UnifiedInventoryViewRow] = []
    for store_id_value in resolved_store_ids:
        inventory_view = _merch_inventory_view_impl(
            MerchInventoryViewRequest(
                store_id=store_id_value,
                lookback_days=params.lookback_days,
                category=params.category,
                brand=brand_csv,
                price_band=params.price_band,
                occasion=resolved_occasion,
                occasions=occasion_values,
                inventory_scope=params.inventory_scope,
                future_window_days=params.future_window_days,
                limit=params.limit,
            )
        )
        store = store_map.get(store_id_value)
        store_name = store.name if store is not None else store_id_value
        for row in inventory_view.rows:
            store_rows.append(
                UnifiedInventoryViewRow(
                    row_type=row.row_type,
                    store_id=store_id_value,
                    store_name=store_name,
                    store_count=1,
                    product_id=row.product_id,
                    offer_id=row.offer_id,
                    title=row.title,
                    brand=row.brand,
                    category=row.category,
                    size=row.size,
                    price=row.price,
                    availability=row.availability,
                    stock_state=row.stock_state,
                    inventory_qty=row.inventory_qty,
                    available_on=row.available_on,
                    offer_status=row.offer_status,
                    link=row.link,
                    image_url=row.image_url,
                    perf_revenue=row.perf_revenue,
                    perf_units=row.perf_units,
                    perf_margin_rate=row.perf_margin_rate,
                )
            )

    if params.row_mode == UnifiedRowMode.aggregated:
        rows = _aggregate_unified_inventory_rows(store_rows)
    else:
        rows = sorted(
            store_rows,
            key=lambda row: (
                str(row.store_name or "").lower(),
                row.row_type.value,
                str(row.brand or "").lower(),
                str(row.title or "").lower(),
                str(row.product_id or row.offer_id or ""),
            ),
        )

    current_rows = sum(1 for row in rows if row.row_type == MerchInventoryRowType.current_inventory)
    potential_rows = sum(1 for row in rows if row.row_type == MerchInventoryRowType.potential_offer)
    summary = (
        f"Unified inventory ({params.row_mode.value}) across {len(resolved_store_ids)} stores: "
        f"{len(rows)} rows (current {current_rows}, potential {potential_rows})."
    )
    return UnifiedInventoryViewResponse(
        summary=summary,
        store_ids=resolved_store_ids,
        active_store_id=resolved_active_store_id,
        lookback_days=params.lookback_days,
        category=params.category,
        brands=brand_values,
        price_band=params.price_band,
        occasions=occasion_values,
        row_mode=params.row_mode,
        inventory_scope=params.inventory_scope,
        future_window_days=params.future_window_days,
        rows=rows,
        total_rows=len(rows),
        current_rows=current_rows,
        potential_rows=potential_rows,
    )


def _unified_action_recommendations_impl(
    params: UnifiedActionRecommendationsRequest,
) -> UnifiedActionRecommendationsResponse:
    brand_values = _normalized_unified_brands(brand=params.brand, brands=params.brands)
    brand_csv = ", ".join(brand_values) if brand_values else None
    occasion_values = _normalized_occasion_tokens(occasion=params.occasion, occasions=params.occasions)
    resolved_occasion = occasion_values[0] if occasion_values else None
    global_override_map, store_override_map = _unified_override_maps(params.recommendation_overrides)

    with SessionLocal() as db:
        resolved_store_ids, resolved_active_store_id, store_map = _resolve_unified_store_scope(
            db,
            store_query=params.store_query,
            store_id=params.store_id,
            store_ids=params.store_ids,
            active_store_id=params.active_store_id,
        )

        store_rows: list[UnifiedActionRecommendationRow] = []
        for store_id_value in resolved_store_ids:
            result = merchandising_action_recommendations(
                db,
                store_id=store_id_value,
                question=params.question,
                objective=params.objective,
                lookback_days=params.lookback_days,
                top_k=params.top_k,
                category=params.category,
                brand=brand_csv,
                price_band=params.price_band,
                occasion=resolved_occasion,
                occasions=occasion_values,
                compare_mode=params.compare_mode,
                peer_mode=params.peer_mode,
                compare_store_id=params.compare_store_id,
            )
            total_recommendations = max(1, len(result.recommendations))
            store = store_map.get(store_id_value)
            store_name = store.name if store is not None else store_id_value
            for index, item in enumerate(result.recommendations):
                model_priority_tier = _priority_tier_for_rank(index, total_recommendations)
                override = _resolve_unified_override(
                    override_scope=params.override_scope,
                    store_id=store_id_value,
                    product_id=item.product_id,
                    global_map=global_override_map,
                    store_map=store_override_map,
                )
                model_action = item.action
                default_final_action = MerchFinalAction(item.action.value)
                final_action = override.final_action if override else default_final_action
                final_priority_tier = override.priority_tier if override else model_priority_tier
                store_rows.append(
                    UnifiedActionRecommendationRow(
                        store_id=store_id_value,
                        store_name=store_name,
                        store_count=1,
                        product_id=item.product_id,
                        title=item.title,
                        brand=item.brand,
                        category=item.category,
                        price=item.price,
                        link=item.link,
                        image_url=item.image_url,
                        price_band=item.price_band,
                        occasion=item.occasion,
                        metric_value=item.metric_value,
                        peer_delta=item.peer_delta,
                        prior_period_delta=item.prior_period_delta,
                        rationale=item.rationale,
                        model_action=model_action,
                        model_priority_tier=model_priority_tier,
                        final_action=final_action,
                        final_priority_tier=final_priority_tier,
                        override_note=override.override_note if override else None,
                    )
                )

    if params.row_mode == UnifiedRowMode.aggregated:
        rows = _aggregate_unified_recommendation_rows(store_rows, top_k=params.top_k)
    else:
        rows = sorted(
            store_rows,
            key=lambda row: (
                str(row.store_name or "").lower(),
                -float(row.metric_value or 0.0),
                str(row.title or "").lower(),
                str(row.product_id or ""),
            ),
        )

    summary = (
        f"Unified recommendations ({params.row_mode.value}) generated across {len(resolved_store_ids)} stores "
        f"with override scope {params.override_scope.value}."
    )
    return UnifiedActionRecommendationsResponse(
        summary=summary,
        store_ids=resolved_store_ids,
        active_store_id=resolved_active_store_id,
        objective=params.objective,
        lookback_days=params.lookback_days,
        category=params.category,
        brands=brand_values,
        price_band=params.price_band,
        occasions=occasion_values,
        row_mode=params.row_mode,
        override_scope=params.override_scope,
        recommendations=rows,
    )


def _mix_overrides_for_store(
    *,
    store_id: str,
    override_scope: OverrideScope,
    overrides: list[UnifiedRecommendationOverride],
) -> list[MerchRecommendationOverride]:
    scoped: list[MerchRecommendationOverride] = []
    for override in overrides:
        if override_scope == OverrideScope.store and str(override.store_id or "").strip() != store_id:
            continue
        if override_scope == OverrideScope.global_scope and str(override.store_id or "").strip():
            continue
        scoped.append(
            MerchRecommendationOverride(
                product_id=override.product_id,
                final_action=override.final_action,
                priority_tier=override.priority_tier,
                override_note=override.override_note,
            )
        )
    return scoped


def _unified_mix_recommendations_impl(
    params: UnifiedProductMixRecommendationsRequest,
) -> UnifiedProductMixRecommendationsResponse:
    brand_values = _normalized_unified_brands(brand=params.brand, brands=params.brands)
    brand_csv = ", ".join(brand_values) if brand_values else None
    occasion_values = _normalized_occasion_tokens(occasion=params.occasion, occasions=params.occasions)
    resolved_occasion = occasion_values[0] if occasion_values else None

    with SessionLocal() as db:
        resolved_store_ids, resolved_active_store_id, store_map = _resolve_unified_store_scope(
            db,
            store_query=params.store_query,
            store_id=params.store_id,
            store_ids=params.store_ids,
            active_store_id=params.active_store_id,
        )

    store_rows: list[UnifiedProductMixRecommendationRow] = []
    for store_id_value in resolved_store_ids:
        mix_result = _merch_mix_analysis_impl(
            MerchProductMixRecommendationsRequest(
                store_id=store_id_value,
                lookback_days=params.lookback_days,
                top_k=params.top_k,
                category=params.category,
                brand=brand_csv,
                price_band=params.price_band,
                occasion=resolved_occasion,
                occasions=occasion_values,
                inventory_scope=params.inventory_scope,
                future_window_days=params.future_window_days,
                recommendation_overrides=_mix_overrides_for_store(
                    store_id=store_id_value,
                    override_scope=params.override_scope,
                    overrides=params.recommendation_overrides,
                ),
            )
        )
        store = store_map.get(store_id_value)
        store_name = store.name if store is not None else store_id_value
        for row in mix_result.rows:
            store_rows.append(
                UnifiedProductMixRecommendationRow(
                    store_id=store_id_value,
                    store_name=store_name,
                    store_count=1,
                    action=row.action,
                    fit_score=row.fit_score,
                    expected_mix_impact=row.expected_mix_impact,
                    rationale=row.rationale,
                    brand=row.brand,
                    category=row.category,
                    current_product_id=row.current_product_id,
                    current_title=row.current_title,
                    current_revenue=row.current_revenue,
                    current_units=row.current_units,
                    offer_id=row.offer_id,
                    offer_title=row.offer_title,
                    offer_status=row.offer_status,
                    available_on=row.available_on,
                    offer_price=row.offer_price,
                )
            )

    if params.row_mode == UnifiedRowMode.aggregated:
        rows = _aggregate_unified_mix_rows(store_rows, top_k=params.top_k)
    else:
        rows = sorted(
            store_rows,
            key=lambda row: (
                str(row.store_name or "").lower(),
                {"swap": 0, "add": 1, "hold": 2, "reduce": 3}.get(row.action.value, 9),
                -float(row.fit_score or 0.0),
                str(row.offer_title or row.current_title or "").lower(),
            ),
        )

    summary = (
        f"Unified mix analysis ({params.row_mode.value}) generated across {len(resolved_store_ids)} stores "
        f"with override scope {params.override_scope.value}."
    )
    return UnifiedProductMixRecommendationsResponse(
        summary=summary,
        store_ids=resolved_store_ids,
        active_store_id=resolved_active_store_id,
        lookback_days=params.lookback_days,
        top_k=params.top_k,
        category=params.category,
        brands=brand_values,
        price_band=params.price_band,
        occasions=occasion_values,
        row_mode=params.row_mode,
        override_scope=params.override_scope,
        inventory_scope=params.inventory_scope,
        future_window_days=params.future_window_days,
        rows=rows,
    )


def _unified_export_csv_impl(params: UnifiedExportCsvRequest) -> UnifiedExportCsvResponse:
    generated_at = datetime.now(timezone.utc)
    rows: list[dict[str, str]] = []
    headers: list[str] = []

    if params.view == UnifiedWorkspaceView.executive_overview:
        result = _unified_overview_impl(
            UnifiedOverviewRequest(
                store_query=params.store_query,
                store_id=params.store_id,
                store_ids=params.store_ids,
                active_store_id=params.active_store_id,
                lookback_days=params.lookback_days,
                category=params.category,
                brand=params.brand,
                brands=params.brands,
                occasion=params.occasion,
                occasions=params.occasions,
                price_band=params.price_band,
                objective=params.objective,
                top_k_stores=params.top_k_stores,
            )
        )
        headers = [
            "view",
            "row_type",
            "store_id",
            "store_name",
            "city",
            "state",
            "rank",
            "revenue",
            "units",
            "margin_rate",
            "revenue_share_pct",
            "revenue_delta_pct",
            "lookback_days",
            "objective",
            "generated_at",
        ]
        rows = [
            {
                "view": params.view.value,
                "row_type": "store_summary",
                "store_id": _as_scalar(item.store_id),
                "store_name": _as_scalar(item.store_name),
                "city": _as_scalar(item.city),
                "state": _as_scalar(item.state),
                "rank": _as_scalar(item.rank),
                "revenue": _as_scalar(item.revenue),
                "units": _as_scalar(item.units),
                "margin_rate": _as_scalar(item.margin_rate),
                "revenue_share_pct": _as_scalar(item.revenue_share_pct),
                "revenue_delta_pct": _as_scalar(item.revenue_delta_pct),
                "lookback_days": _as_scalar(result.lookback_days),
                "objective": _as_scalar(result.objective),
                "generated_at": result.generated_at.isoformat(),
            }
            for item in result.stores
        ]
    elif params.view == UnifiedWorkspaceView.inventory:
        result = _unified_inventory_view_impl(
            UnifiedInventoryViewRequest(
                store_query=params.store_query,
                store_id=params.store_id,
                store_ids=params.store_ids,
                active_store_id=params.active_store_id,
                lookback_days=params.lookback_days,
                category=params.category,
                brand=params.brand,
                brands=params.brands,
                occasion=params.occasion,
                occasions=params.occasions,
                price_band=params.price_band,
                row_mode=params.row_mode,
                inventory_scope=params.inventory_scope,
                future_window_days=params.future_window_days,
                limit=params.limit,
            )
        )
        headers = [
            "view",
            "row_mode",
            "row_type",
            "store_id",
            "store_name",
            "store_count",
            "product_id",
            "offer_id",
            "title",
            "brand",
            "category",
            "size",
            "price",
            "availability",
            "stock_state",
            "inventory_qty",
            "available_on",
            "offer_status",
            "perf_revenue",
            "perf_units",
            "perf_margin_rate",
            "inventory_scope",
            "future_window_days",
            "lookback_days",
            "link",
            "image_url",
        ]
        rows = [
            {
                "view": params.view.value,
                "row_mode": _as_scalar(result.row_mode),
                "row_type": _as_scalar(item.row_type),
                "store_id": _as_scalar(item.store_id),
                "store_name": _as_scalar(item.store_name),
                "store_count": _as_scalar(item.store_count),
                "product_id": _as_scalar(item.product_id),
                "offer_id": _as_scalar(item.offer_id),
                "title": _as_scalar(item.title),
                "brand": _as_scalar(item.brand),
                "category": _as_scalar(item.category),
                "size": _as_scalar(item.size),
                "price": _as_scalar(item.price),
                "availability": _as_scalar(item.availability),
                "stock_state": _as_scalar(item.stock_state),
                "inventory_qty": _as_scalar(item.inventory_qty),
                "available_on": _as_scalar(item.available_on),
                "offer_status": _as_scalar(item.offer_status),
                "perf_revenue": _as_scalar(item.perf_revenue),
                "perf_units": _as_scalar(item.perf_units),
                "perf_margin_rate": _as_scalar(item.perf_margin_rate),
                "inventory_scope": _as_scalar(result.inventory_scope),
                "future_window_days": _as_scalar(result.future_window_days),
                "lookback_days": _as_scalar(result.lookback_days),
                "link": _as_scalar(item.link),
                "image_url": _as_scalar(item.image_url),
            }
            for item in result.rows
        ]
    elif params.view == UnifiedWorkspaceView.recommendations:
        result = _unified_action_recommendations_impl(
            UnifiedActionRecommendationsRequest(
                store_query=params.store_query,
                store_id=params.store_id,
                store_ids=params.store_ids,
                active_store_id=params.active_store_id,
                lookback_days=params.lookback_days,
                category=params.category,
                brand=params.brand,
                brands=params.brands,
                occasion=params.occasion,
                occasions=params.occasions,
                price_band=params.price_band,
                question=params.question,
                objective=params.objective,
                top_k=params.top_k,
                row_mode=params.row_mode,
                override_scope=params.override_scope,
                recommendation_overrides=params.recommendation_overrides,
                compare_mode=params.compare_mode,
                peer_mode=params.peer_mode,
                compare_store_id=params.compare_store_id,
            )
        )
        headers = [
            "view",
            "row_mode",
            "override_scope",
            "store_id",
            "store_name",
            "store_count",
            "product_id",
            "title",
            "brand",
            "category",
            "price",
            "price_band",
            "metric_value",
            "peer_delta",
            "prior_period_delta",
            "model_action",
            "model_priority_tier",
            "final_action",
            "final_priority_tier",
            "override_note",
            "rationale",
            "link",
            "image_url",
        ]
        rows = [
            {
                "view": params.view.value,
                "row_mode": _as_scalar(result.row_mode),
                "override_scope": _as_scalar(result.override_scope),
                "store_id": _as_scalar(item.store_id),
                "store_name": _as_scalar(item.store_name),
                "store_count": _as_scalar(item.store_count),
                "product_id": _as_scalar(item.product_id),
                "title": _as_scalar(item.title),
                "brand": _as_scalar(item.brand),
                "category": _as_scalar(item.category),
                "price": _as_scalar(item.price),
                "price_band": _as_scalar(item.price_band),
                "metric_value": _as_scalar(item.metric_value),
                "peer_delta": _as_scalar(item.peer_delta),
                "prior_period_delta": _as_scalar(item.prior_period_delta),
                "model_action": _as_scalar(item.model_action),
                "model_priority_tier": _as_scalar(item.model_priority_tier),
                "final_action": _as_scalar(item.final_action),
                "final_priority_tier": _as_scalar(item.final_priority_tier),
                "override_note": _as_scalar(item.override_note),
                "rationale": _as_scalar(item.rationale),
                "link": _as_scalar(item.link),
                "image_url": _as_scalar(item.image_url),
            }
            for item in result.recommendations
        ]
    else:
        result = _unified_mix_recommendations_impl(
            UnifiedProductMixRecommendationsRequest(
                store_query=params.store_query,
                store_id=params.store_id,
                store_ids=params.store_ids,
                active_store_id=params.active_store_id,
                lookback_days=params.lookback_days,
                category=params.category,
                brand=params.brand,
                brands=params.brands,
                occasion=params.occasion,
                occasions=params.occasions,
                price_band=params.price_band,
                top_k=params.top_k,
                row_mode=params.row_mode,
                override_scope=params.override_scope,
                inventory_scope=params.inventory_scope,
                future_window_days=params.future_window_days,
                recommendation_overrides=params.recommendation_overrides,
            )
        )
        headers = [
            "view",
            "row_mode",
            "override_scope",
            "store_id",
            "store_name",
            "store_count",
            "action",
            "fit_score",
            "expected_mix_impact",
            "brand",
            "category",
            "current_product_id",
            "current_title",
            "current_revenue",
            "current_units",
            "offer_id",
            "offer_title",
            "offer_status",
            "available_on",
            "offer_price",
            "inventory_scope",
            "future_window_days",
            "lookback_days",
            "rationale",
        ]
        rows = [
            {
                "view": params.view.value,
                "row_mode": _as_scalar(result.row_mode),
                "override_scope": _as_scalar(result.override_scope),
                "store_id": _as_scalar(item.store_id),
                "store_name": _as_scalar(item.store_name),
                "store_count": _as_scalar(item.store_count),
                "action": _as_scalar(item.action),
                "fit_score": _as_scalar(item.fit_score),
                "expected_mix_impact": _as_scalar(item.expected_mix_impact),
                "brand": _as_scalar(item.brand),
                "category": _as_scalar(item.category),
                "current_product_id": _as_scalar(item.current_product_id),
                "current_title": _as_scalar(item.current_title),
                "current_revenue": _as_scalar(item.current_revenue),
                "current_units": _as_scalar(item.current_units),
                "offer_id": _as_scalar(item.offer_id),
                "offer_title": _as_scalar(item.offer_title),
                "offer_status": _as_scalar(item.offer_status),
                "available_on": _as_scalar(item.available_on),
                "offer_price": _as_scalar(item.offer_price),
                "inventory_scope": _as_scalar(result.inventory_scope),
                "future_window_days": _as_scalar(result.future_window_days),
                "lookback_days": _as_scalar(result.lookback_days),
                "rationale": _as_scalar(item.rationale),
            }
            for item in result.rows
        ]

    csv_text = _csv_text(headers, rows)
    filename = f"unified_{params.view.value}_{generated_at.strftime('%Y%m%d_%H%M%S')}.csv"
    return UnifiedExportCsvResponse(
        view=params.view,
        row_mode=params.row_mode,
        override_scope=params.override_scope,
        filename=filename,
        headers=headers,
        rows=[UnifiedExportCsvRow(values=row) for row in rows],
        row_count=len(rows),
        csv_text=csv_text,
        generated_at=generated_at,
    )


def _unified_tool_for_view(view: UnifiedWorkspaceView) -> str:
    if isinstance(view, str):
        try:
            view = UnifiedWorkspaceView(view)
        except Exception:
            view = UnifiedWorkspaceView.executive_overview
    if view == UnifiedWorkspaceView.inventory:
        return "fashion_unified_inventory_view"
    if view == UnifiedWorkspaceView.recommendations:
        return "fashion_unified_action_recommendations"
    if view == UnifiedWorkspaceView.mix_analysis:
        return "fashion_unified_product_mix_recommendations"
    return "fashion_unified_overview"


def _unified_workspace_payload(
    *,
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    active_store_id: str | None = None,
    initial_view: UnifiedWorkspaceView = UnifiedWorkspaceView.executive_overview,
    lookback_days: int = 90,
    objective: Objective = Objective.revenue,
    top_k: int = 12,
    category: str | None = None,
    brand: str | None = None,
    brands: list[str] | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
    occasions: list[str] | None = None,
    row_mode: UnifiedRowMode = UnifiedRowMode.store_product,
    override_scope: OverrideScope = OverrideScope.store,
    inventory_scope: InventoryScope = InventoryScope.combined,
    future_window_days: int = 120,
    question: str | None = None,
    recommendation_overrides: list[UnifiedRecommendationOverride] | None = None,
    initial_notice: str | None = None,
) -> dict:
    try:
        resolved_initial_view = (
            initial_view
            if isinstance(initial_view, UnifiedWorkspaceView)
            else UnifiedWorkspaceView(str(initial_view or "").strip() or UnifiedWorkspaceView.executive_overview.value)
        )
    except Exception:
        resolved_initial_view = UnifiedWorkspaceView.executive_overview
    try:
        resolved_row_mode = (
            row_mode
            if isinstance(row_mode, UnifiedRowMode)
            else UnifiedRowMode(str(row_mode or "").strip() or UnifiedRowMode.store_product.value)
        )
    except Exception:
        resolved_row_mode = UnifiedRowMode.store_product
    try:
        resolved_override_scope = (
            override_scope
            if isinstance(override_scope, OverrideScope)
            else OverrideScope(str(override_scope or "").strip() or OverrideScope.store.value)
        )
    except Exception:
        resolved_override_scope = OverrideScope.store

    bounded_lookback = max(7, min(int(lookback_days or 90), 730))
    bounded_top_k = max(1, min(int(top_k or 12), 100))
    bounded_future_window = max(1, min(int(future_window_days or 120), 365))
    normalized_brands = _normalized_unified_brands(brand=brand, brands=brands)
    normalized_occasions = _normalized_occasion_tokens(occasion=occasion, occasions=occasions)

    with SessionLocal() as db:
        resolved_store_ids, resolved_active_store_id, _ = _resolve_unified_store_scope(
            db,
            store_query=store_query,
            store_id=store_id,
            store_ids=store_ids,
            active_store_id=active_store_id,
        )

    filters = UnifiedWorkspaceFilters(
        store_ids=resolved_store_ids,
        active_store_id=resolved_active_store_id,
        lookback_days=bounded_lookback,
        category=category,
        brands=normalized_brands,
        occasions=normalized_occasions,
        price_band=price_band,
        objective=objective,
        top_k=bounded_top_k,
        inventory_scope=inventory_scope,
        future_window_days=bounded_future_window,
        row_mode=resolved_row_mode,
        override_scope=resolved_override_scope,
        question=question,
    )

    overrides = recommendation_overrides or []
    if resolved_initial_view == UnifiedWorkspaceView.inventory:
        initial_result = _unified_inventory_view_impl(
            UnifiedInventoryViewRequest(
                store_ids=resolved_store_ids,
                active_store_id=resolved_active_store_id,
                lookback_days=bounded_lookback,
                category=category,
                brands=normalized_brands,
                occasions=normalized_occasions,
                price_band=price_band,
                row_mode=resolved_row_mode,
                inventory_scope=inventory_scope,
                future_window_days=bounded_future_window,
                limit=300,
            )
        )
    elif resolved_initial_view == UnifiedWorkspaceView.recommendations:
        initial_result = _unified_action_recommendations_impl(
            UnifiedActionRecommendationsRequest(
                store_ids=resolved_store_ids,
                active_store_id=resolved_active_store_id,
                lookback_days=bounded_lookback,
                category=category,
                brands=normalized_brands,
                occasions=normalized_occasions,
                price_band=price_band,
                question=question,
                objective=objective,
                top_k=bounded_top_k,
                row_mode=resolved_row_mode,
                override_scope=resolved_override_scope,
                recommendation_overrides=overrides,
            )
        )
    elif resolved_initial_view == UnifiedWorkspaceView.mix_analysis:
        initial_result = _unified_mix_recommendations_impl(
            UnifiedProductMixRecommendationsRequest(
                store_ids=resolved_store_ids,
                active_store_id=resolved_active_store_id,
                lookback_days=bounded_lookback,
                top_k=bounded_top_k,
                category=category,
                brands=normalized_brands,
                occasions=normalized_occasions,
                price_band=price_band,
                row_mode=resolved_row_mode,
                override_scope=resolved_override_scope,
                inventory_scope=inventory_scope,
                future_window_days=bounded_future_window,
                recommendation_overrides=overrides,
            )
        )
    else:
        initial_result = _unified_overview_impl(
            UnifiedOverviewRequest(
                store_ids=resolved_store_ids,
                active_store_id=resolved_active_store_id,
                lookback_days=bounded_lookback,
                category=category,
                brands=normalized_brands,
                occasions=normalized_occasions,
                price_band=price_band,
                objective=objective,
                top_k_stores=max(1, min(len(resolved_store_ids) or 12, 50)),
            )
        )

    return {
        "filters": filters.model_dump(mode="json"),
        "active_view": resolved_initial_view.value,
        "initial_result": initial_result.model_dump(mode="json"),
        "last_result": None,
        "last_tool": _unified_tool_for_view(resolved_initial_view),
        "initial_notice": initial_notice,
        "uiHints": {
            "emptyState": "Select a tab and refresh to load unified workspace data.",
            "categoryOptions": _merch_category_options(),
            "brandOptions": _exec_brand_options(),
            "occasionOptions": _exec_event_options(),
            "storeOptions": _exec_store_options(),
            "features": {
                "execAutoOptimizeEnabled": settings.exec_auto_optimize_enabled,
                "strategyPacketEnabled": settings.strategy_packet_enabled,
                "merchStrategyContextEnabled": settings.merch_strategy_context_enabled,
            },
        },
    }


def _normalized_brand_tokens(raw_brand: str | None) -> list[str]:
    if not raw_brand:
        return []
    tokens: list[str] = []
    for value in str(raw_brand).split(","):
        token = value.strip().lower()
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def _apply_price_band_filter_column(query, price_column, price_band: PriceBand | None):
    if price_band == PriceBand.under_250:
        return query.where(price_column < 250)
    if price_band == PriceBand.band_250_500:
        return query.where(price_column >= 250, price_column < 500)
    if price_band == PriceBand.band_500_1000:
        return query.where(price_column >= 500, price_column < 1000)
    if price_band == PriceBand.band_1000_plus:
        return query.where(price_column >= 1000)
    return query


def _apply_price_band_filter(query, price_band: PriceBand | None):
    return _apply_price_band_filter_column(query, Product.price, price_band)


def _merch_inventory_performance_by_product(
    db,
    *,
    store_id: str,
    product_ids: list[str],
    lookback_days: int,
    occasion: str | None,
    occasions: list[str] | None = None,
) -> dict[str, dict[str, float]]:
    if not product_ids:
        return {}
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=max(7, min(int(lookback_days or 90), 730)))
    query = (
        select(
            OrderItem.product_id.label("product_id"),
            func.sum(OrderItem.line_total).label("revenue"),
            func.sum(OrderItem.quantity).label("units"),
            func.sum(OrderItem.line_total * Product.margin_pct).label("margin_value"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(
            Order.store_id == store_id,
            OrderItem.product_id.in_(product_ids),
            Order.ordered_at >= since,
            Order.ordered_at < now,
        )
        .group_by(OrderItem.product_id)
    )
    query = _apply_order_occasion_filter(query, occasion=occasion, occasions=occasions)
    rows = db.execute(query).all()
    perf_map: dict[str, dict[str, float]] = {}
    for row in rows:
        revenue = float(row.revenue or 0.0)
        units = float(row.units or 0.0)
        margin_value = float(row.margin_value or 0.0)
        perf_map[row.product_id] = {
            "revenue": revenue,
            "units": units,
            "margin_rate": (margin_value / revenue) if revenue > 0 else 0.0,
        }
    return perf_map


def _merch_category_perf_map(
    db,
    *,
    store_id: str,
    lookback_days: int,
    occasion: str | None,
    occasions: list[str] | None = None,
) -> dict[str, float]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=max(7, min(int(lookback_days or 90), 730)))
    query = (
        select(
            func.lower(Product.category).label("category"),
            func.sum(OrderItem.line_total).label("revenue"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(Order.store_id == store_id, Order.ordered_at >= since, Order.ordered_at < now)
        .group_by(func.lower(Product.category))
    )
    query = _apply_order_occasion_filter(query, occasion=occasion, occasions=occasions)
    rows = db.execute(query).all()
    return {str(row.category or "").strip().lower(): float(row.revenue or 0.0) for row in rows if row.category}


def _merch_brand_category_perf_map(
    db,
    *,
    store_id: str,
    lookback_days: int,
    occasion: str | None,
    occasions: list[str] | None = None,
) -> dict[tuple[str, str], dict[str, float]]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=max(7, min(int(lookback_days or 90), 730)))
    query = (
        select(
            func.lower(Product.brand).label("brand"),
            func.lower(Product.category).label("category"),
            func.sum(OrderItem.line_total).label("revenue"),
            func.sum(OrderItem.quantity).label("units"),
            func.sum(OrderItem.line_total * Product.margin_pct).label("margin_value"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(Order.store_id == store_id, Order.ordered_at >= since, Order.ordered_at < now)
        .group_by(func.lower(Product.brand), func.lower(Product.category))
    )
    query = _apply_order_occasion_filter(query, occasion=occasion, occasions=occasions)
    rows = db.execute(query).all()
    perf_map: dict[tuple[str, str], dict[str, float]] = {}
    for row in rows:
        brand_token = str(row.brand or "").strip().lower()
        category_token = str(row.category or "").strip().lower()
        if not brand_token or not category_token:
            continue
        revenue = float(row.revenue or 0.0)
        units = float(row.units or 0.0)
        margin_value = float(row.margin_value or 0.0)
        perf_map[(brand_token, category_token)] = {
            "revenue": revenue,
            "units": units,
            "margin_rate": (margin_value / revenue) if revenue > 0 else 0.0,
        }
    return perf_map


def _merch_current_inventory_rows(
    db,
    *,
    store_id: str,
    category: str | None,
    brand: str | None,
    price_band: PriceBand | None,
    occasion: str | None,
    occasions: list[str] | None = None,
    lookback_days: int,
    limit: int,
) -> list[MerchInventoryViewRow]:
    category_token = str(category or "").strip().lower()
    brand_tokens = _normalized_brand_tokens(brand)
    occasion_tokens = _normalized_occasion_tokens(occasion=occasion, occasions=occasions)
    query = select(Product).where(Product.store_id == store_id)
    if category_token:
        query = query.where(func.lower(Product.category) == category_token)
    if brand_tokens:
        query = query.where(func.lower(Product.brand).in_(brand_tokens))
    query = _apply_price_band_filter(query, price_band)
    products = db.scalars(query.order_by(func.lower(Product.title), Product.id).limit(limit)).all()
    if not products:
        return []
    perf_by_product = _merch_inventory_performance_by_product(
        db,
        store_id=store_id,
        product_ids=[item.id for item in products],
        lookback_days=lookback_days,
        occasion=occasion,
        occasions=occasion_tokens,
    )
    rows: list[MerchInventoryViewRow] = []
    for item in products:
        if occasion_tokens and item.id not in perf_by_product:
            continue
        perf = perf_by_product.get(item.id, {"revenue": 0.0, "units": 0.0, "margin_rate": 0.0})
        rows.append(
            MerchInventoryViewRow(
                row_type=MerchInventoryRowType.current_inventory,
                product_id=item.id,
                offer_id=None,
                title=item.title,
                brand=item.brand,
                category=item.category,
                size=item.size,
                price=float(item.price) if item.price is not None else None,
                availability=item.availability,
                stock_state=_stock_state_label(item.availability, item.inventory_qty),
                inventory_qty=_int_value(item.inventory_qty),
                available_on=None,
                offer_status=None,
                link=item.link,
                image_url=item.image_link,
                perf_revenue=float(perf.get("revenue", 0.0)),
                perf_units=float(perf.get("units", 0.0)),
                perf_margin_rate=float(perf.get("margin_rate", 0.0)),
            )
        )
    return rows


def _merch_potential_offer_rows(
    db,
    *,
    category: str | None,
    brand: str | None,
    price_band: PriceBand | None,
    occasion: str | None,
    occasions: list[str] | None = None,
    lookback_days: int,
    store_id: str,
    future_window_days: int,
    limit: int,
) -> list[MerchInventoryViewRow]:
    category_token = str(category or "").strip().lower()
    brand_tokens = _normalized_brand_tokens(brand)
    occasion_tokens = _normalized_occasion_tokens(occasion=occasion, occasions=occasions)
    today = datetime.now(timezone.utc).date()
    latest_available = today + timedelta(days=max(1, min(int(future_window_days or 120), 365)))

    query = select(SupplierProductOffer)
    if category_token:
        query = query.where(func.lower(SupplierProductOffer.category) == category_token)
    if brand_tokens:
        query = query.where(func.lower(SupplierProductOffer.brand).in_(brand_tokens))
    query = _apply_price_band_filter_column(query, SupplierProductOffer.price, price_band)
    query = query.where(
        and_(
            or_(SupplierProductOffer.available_on.is_(None), SupplierProductOffer.available_on >= today),
            or_(SupplierProductOffer.available_on.is_(None), SupplierProductOffer.available_on <= latest_available),
        )
    )
    offers = db.scalars(
        query.order_by(
            case((SupplierProductOffer.available_on.is_(None), 1), else_=0).asc(),
            SupplierProductOffer.available_on.asc(),
            func.lower(SupplierProductOffer.brand).asc(),
            func.lower(SupplierProductOffer.title).asc(),
            SupplierProductOffer.id.asc(),
        ).limit(limit)
    ).all()
    if not offers:
        return []

    perf_by_brand_category = _merch_brand_category_perf_map(
        db,
        store_id=store_id,
        lookback_days=lookback_days,
        occasion=occasion,
        occasions=occasion_tokens,
    )
    rows: list[MerchInventoryViewRow] = []
    for item in offers:
        perf_key = (str(item.brand or "").strip().lower(), str(item.category or "").strip().lower())
        if occasion_tokens and perf_key not in perf_by_brand_category:
            continue
        perf = perf_by_brand_category.get(perf_key, {"revenue": 0.0, "units": 0.0, "margin_rate": 0.0})
        rows.append(
            MerchInventoryViewRow(
                row_type=MerchInventoryRowType.potential_offer,
                product_id=None,
                offer_id=item.id,
                title=item.title,
                brand=item.brand,
                category=item.category,
                size=item.size,
                price=float(item.price) if item.price is not None else None,
                availability=None,
                stock_state=None,
                inventory_qty=0,
                available_on=item.available_on.isoformat() if item.available_on else None,
                offer_status=item.status,
                link=item.link,
                image_url=item.image_link,
                perf_revenue=float(perf.get("revenue", 0.0)),
                perf_units=float(perf.get("units", 0.0)),
                perf_margin_rate=float(perf.get("margin_rate", 0.0)),
            )
        )
    return rows


def _merch_inventory_view_impl(params: MerchInventoryViewRequest) -> MerchInventoryViewResponse:
    with SessionLocal() as db:
        resolved_store = resolve_store(db, store_query=params.store_query, store_id=params.store_id).resolved
        effective_limit = max(1, min(int(params.limit or 200), 2000))
        occasion_tokens = _normalized_occasion_tokens(occasion=params.occasion, occasions=params.occasions)
        resolved_occasion = occasion_tokens[0] if occasion_tokens else None
        current_rows: list[MerchInventoryViewRow] = []
        potential_rows: list[MerchInventoryViewRow] = []
        if params.inventory_scope in {InventoryScope.current, InventoryScope.combined}:
            current_rows = _merch_current_inventory_rows(
                db,
                store_id=resolved_store.id,
                category=params.category,
                brand=params.brand,
                price_band=params.price_band,
                occasion=resolved_occasion,
                occasions=occasion_tokens,
                lookback_days=params.lookback_days,
                limit=effective_limit,
            )
        if params.inventory_scope in {InventoryScope.potential, InventoryScope.combined}:
            potential_rows = _merch_potential_offer_rows(
                db,
                category=params.category,
                brand=params.brand,
                price_band=params.price_band,
                occasion=resolved_occasion,
                occasions=occasion_tokens,
                lookback_days=params.lookback_days,
                store_id=resolved_store.id,
                future_window_days=params.future_window_days,
                limit=effective_limit,
            )

        if params.inventory_scope == InventoryScope.current:
            rows = list(current_rows[:effective_limit])
        elif params.inventory_scope == InventoryScope.potential:
            rows = list(potential_rows[:effective_limit])
        else:
            rows = []
            max_length = max(len(current_rows), len(potential_rows))
            for index in range(max_length):
                if index < len(current_rows):
                    rows.append(current_rows[index])
                if len(rows) >= effective_limit:
                    break
                if index < len(potential_rows):
                    rows.append(potential_rows[index])
                if len(rows) >= effective_limit:
                    break
        current_count = sum(1 for row in rows if row.row_type == MerchInventoryRowType.current_inventory)
        potential_count = sum(1 for row in rows if row.row_type == MerchInventoryRowType.potential_offer)
        summary = (
            f"{resolved_store.name}: {len(rows)} inventory rows (current {current_count}, potential {potential_count}) "
            f"for scope {params.inventory_scope.value} in the next {params.future_window_days} days."
        )
        return MerchInventoryViewResponse(
            summary=summary,
            store=resolved_store,
            lookback_days=params.lookback_days,
            category=params.category,
            brand=params.brand,
            price_band=params.price_band,
            occasion=resolved_occasion,
            occasions=occasion_tokens,
            inventory_scope=params.inventory_scope,
            future_window_days=params.future_window_days,
            rows=rows,
            total_rows=len(rows),
            current_rows=current_count,
            potential_rows=potential_count,
        )


def _merch_price_band_token(price: float | None) -> str:
    if price is None:
        return "unknown"
    if price < 250:
        return "under_250"
    if price < 500:
        return "250_500"
    if price < 1000:
        return "500_1000"
    return "1000_plus"


def _merch_price_fit_score(offer_price: float | None, category_prices: list[float]) -> float:
    if offer_price is None or not category_prices:
        return 0.5
    sorted_prices = sorted(category_prices)
    median = sorted_prices[len(sorted_prices) // 2]
    if median <= 0:
        return 0.5
    delta = abs(offer_price - median) / median
    if delta <= 0.2:
        return 1.0
    if delta <= 0.4:
        return 0.7
    if delta <= 0.6:
        return 0.45
    return 0.25


def _merch_override_map(overrides: list[MerchRecommendationOverride]) -> dict[str, MerchRecommendationOverride]:
    mapped: dict[str, MerchRecommendationOverride] = {}
    for item in overrides or []:
        product_id = str(item.product_id or "").strip()
        if not product_id:
            continue
        mapped[product_id] = item
    return mapped


def _priority_tier_for_rank(index: int, total: int) -> MerchPriorityTier:
    if total <= 0:
        return MerchPriorityTier.medium
    percentile = (index + 1) / total
    if percentile <= 0.34:
        return MerchPriorityTier.high
    if percentile <= 0.67:
        return MerchPriorityTier.medium
    return MerchPriorityTier.low


def _merch_mix_analysis_impl(params: MerchProductMixRecommendationsRequest) -> MerchProductMixRecommendationsResponse:
    occasion_tokens = _normalized_occasion_tokens(occasion=params.occasion, occasions=params.occasions)
    resolved_occasion = occasion_tokens[0] if occasion_tokens else None
    inventory_view = _merch_inventory_view_impl(
        MerchInventoryViewRequest(
            store_query=params.store_query,
            store_id=params.store_id,
            lookback_days=params.lookback_days,
            category=params.category,
            brand=params.brand,
            price_band=params.price_band,
            occasion=resolved_occasion,
            occasions=occasion_tokens,
            inventory_scope=params.inventory_scope,
            future_window_days=params.future_window_days,
            limit=max(200, params.top_k * 8),
        )
    )
    override_map = _merch_override_map(params.recommendation_overrides)
    current_rows = [row for row in inventory_view.rows if row.row_type == MerchInventoryRowType.current_inventory]
    offer_rows = [row for row in inventory_view.rows if row.row_type == MerchInventoryRowType.potential_offer]

    category_revenue: dict[str, float] = {}
    for row in current_rows:
        category_token = str(row.category or "").strip().lower()
        category_revenue[category_token] = category_revenue.get(category_token, 0.0) + float(row.perf_revenue or 0.0)
    total_category_revenue = sum(category_revenue.values()) or 1.0
    current_brands = {str(row.brand or "").strip().lower() for row in current_rows if row.brand}
    prices_by_category: dict[str, list[float]] = {}
    for row in current_rows:
        category_token = str(row.category or "").strip().lower()
        if row.price is None:
            continue
        prices_by_category.setdefault(category_token, []).append(float(row.price))

    add_candidates: list[tuple[float, MerchInventoryViewRow]] = []
    for row in offer_rows:
        category_token = str(row.category or "").strip().lower()
        category_share = category_revenue.get(category_token, 0.0) / total_category_revenue
        trend_gap = max(0.0, min(1.0, 1.0 - category_share))
        brand_token = str(row.brand or "").strip().lower()
        brand_whitespace = 1.0 if brand_token and brand_token not in current_brands else 0.35
        price_fit = _merch_price_fit_score(row.price, prices_by_category.get(category_token, []))
        fit_score = round((trend_gap * 0.45 + brand_whitespace * 0.35 + price_fit * 0.20) * 100.0, 2)
        add_candidates.append((fit_score, row))
    add_candidates.sort(
        key=lambda item: (
            -item[0],
            (item[1].available_on or "9999-12-31"),
            str(item[1].brand or "").lower(),
            str(item[1].title or "").lower(),
            str(item[1].offer_id or ""),
        )
    )

    reduce_candidates = sorted(
        current_rows,
        key=lambda row: (
            float(row.perf_revenue or 0.0),
            float(row.perf_units or 0.0),
            float(row.perf_margin_rate or 0.0),
            float(row.inventory_qty or 0.0),
            str(row.title or "").lower(),
            str(row.product_id or ""),
        ),
    )
    hold_candidates = sorted(
        current_rows,
        key=lambda row: (
            -float(row.perf_revenue or 0.0),
            -float(row.perf_margin_rate or 0.0),
            -float(row.perf_units or 0.0),
            str(row.title or "").lower(),
            str(row.product_id or ""),
        ),
    )

    mix_rows: list[MerchProductMixRecommendationRow] = []
    used_offer_ids: set[str] = set()
    used_current_ids: set[str] = set()

    max_swaps = max(1, min(params.top_k // 3, len(add_candidates), len(reduce_candidates))) if add_candidates and reduce_candidates else 0
    for idx in range(max_swaps):
        fit_score, offer = add_candidates[idx]
        replacement = reduce_candidates[idx]
        offer_id = str(offer.offer_id or "")
        current_id = str(replacement.product_id or "")
        if offer_id:
            used_offer_ids.add(offer_id)
        if current_id:
            used_current_ids.add(current_id)
        expected_impact = round((fit_score * 0.45) - (float(replacement.perf_revenue or 0.0) * 0.02), 2)
        mix_rows.append(
            MerchProductMixRecommendationRow(
                action=MerchMixAction.swap,
                fit_score=fit_score,
                expected_mix_impact=expected_impact,
                rationale=(
                    f"Swap out slower current item '{replacement.title}' for near-term offer '{offer.title}' "
                    "to close category/brand whitespace while protecting price-band fit."
                ),
                brand=offer.brand,
                category=offer.category,
                current_product_id=replacement.product_id,
                current_title=replacement.title,
                current_revenue=float(replacement.perf_revenue or 0.0),
                current_units=float(replacement.perf_units or 0.0),
                offer_id=offer.offer_id,
                offer_title=offer.title,
                offer_status=offer.offer_status,
                available_on=offer.available_on,
                offer_price=offer.price,
            )
        )

    for fit_score, offer in add_candidates:
        offer_id = str(offer.offer_id or "")
        if offer_id and offer_id in used_offer_ids:
            continue
        expected_impact = round(fit_score * 0.52, 2)
        mix_rows.append(
            MerchProductMixRecommendationRow(
                action=MerchMixAction.add,
                fit_score=fit_score,
                expected_mix_impact=expected_impact,
                rationale=(
                    f"Add potential offer '{offer.title}' for {_humanize_token(offer.category)}. "
                    "Fit score reflects category trend gap, brand whitespace, and price-band alignment."
                ),
                brand=offer.brand,
                category=offer.category,
                offer_id=offer.offer_id,
                offer_title=offer.title,
                offer_status=offer.offer_status,
                available_on=offer.available_on,
                offer_price=offer.price,
            )
        )

    for row in reduce_candidates:
        product_id = str(row.product_id or "")
        if product_id and product_id in used_current_ids:
            continue
        override = override_map.get(product_id)
        force_reduce = override is not None and override.final_action in {MerchFinalAction.deprioritize, MerchFinalAction.drop}
        weak_perf = float(row.perf_revenue or 0.0) <= 0.0 or float(row.perf_units or 0.0) <= 1.0
        if not force_reduce and not weak_perf:
            continue
        fit_score = round(max(10.0, 100.0 - (float(row.perf_revenue or 0.0) * 0.05)), 2)
        expected_impact = round(max(0.0, (float(row.inventory_qty or 0.0) * 0.8) - float(row.perf_revenue or 0.0) * 0.01), 2)
        rationale = (
            f"Reduce '{row.title}' due to inventory pressure vs. demand."
            if not force_reduce
            else f"Reduce '{row.title}' to honor manual override ({override.final_action.value})."
        )
        mix_rows.append(
            MerchProductMixRecommendationRow(
                action=MerchMixAction.reduce,
                fit_score=fit_score,
                expected_mix_impact=expected_impact,
                rationale=rationale,
                brand=row.brand,
                category=row.category,
                current_product_id=row.product_id,
                current_title=row.title,
                current_revenue=float(row.perf_revenue or 0.0),
                current_units=float(row.perf_units or 0.0),
            )
        )

    for index, row in enumerate(hold_candidates):
        product_id = str(row.product_id or "")
        override = override_map.get(product_id)
        if override and override.final_action in {MerchFinalAction.feature, MerchFinalAction.promote}:
            keep = True
        else:
            keep = float(row.perf_revenue or 0.0) > 0 and float(row.perf_margin_rate or 0.0) >= 0.4
        if not keep:
            continue
        fit_score = round(min(99.0, 45.0 + float(row.perf_margin_rate or 0.0) * 55.0), 2)
        expected_impact = round(float(row.perf_revenue or 0.0) * 0.015, 2)
        rationale = (
            f"Hold '{row.title}' as a strong core performer."
            if override is None
            else f"Hold '{row.title}' to align with manual override ({override.final_action.value})."
        )
        mix_rows.append(
            MerchProductMixRecommendationRow(
                action=MerchMixAction.hold,
                fit_score=fit_score,
                expected_mix_impact=expected_impact,
                rationale=rationale,
                brand=row.brand,
                category=row.category,
                current_product_id=row.product_id,
                current_title=row.title,
                current_revenue=float(row.perf_revenue or 0.0),
                current_units=float(row.perf_units or 0.0),
            )
        )
        if index >= params.top_k * 2:
            break

    mix_rows.sort(
        key=lambda item: (
            {"swap": 0, "add": 1, "hold": 2, "reduce": 3}.get(item.action.value, 9),
            -float(item.fit_score or 0.0),
            -(float(item.expected_mix_impact or 0.0)),
            str(item.offer_title or item.current_title or "").lower(),
        )
    )
    limited_rows = mix_rows[: max(1, min(params.top_k, 100))]
    summary = (
        f"{inventory_view.store.name}: generated {len(limited_rows)} mix recommendations "
        f"from scope {params.inventory_scope.value} using {len(override_map)} manual overrides."
    )
    return MerchProductMixRecommendationsResponse(
        summary=summary,
        store=inventory_view.store,
        lookback_days=params.lookback_days,
        top_k=params.top_k,
        category=params.category,
        brand=params.brand,
        price_band=params.price_band,
        occasion=resolved_occasion,
        occasions=occasion_tokens,
        inventory_scope=params.inventory_scope,
        future_window_days=params.future_window_days,
        rows=limited_rows,
    )


def _merch_inventory_snapshot_rows(
    db,
    *,
    store_id: str,
    store_name: str,
    view: MerchWorkspaceView,
    lookback_days: int,
    category: str | None,
    brand: str | None,
    price_band: PriceBand | None,
    occasion: str | None,
    occasions: list[str] | None = None,
) -> list[dict[str, str]]:
    occasion_tokens = _normalized_occasion_tokens(occasion=occasion, occasions=occasions)
    product_query = select(Product).where(Product.store_id == store_id)
    if category:
        product_query = product_query.where(func.lower(Product.category) == category.strip().lower())
    brand_tokens = _normalized_brand_tokens(brand)
    if brand_tokens:
        product_query = product_query.where(func.lower(Product.brand).in_(brand_tokens))
    product_query = _apply_price_band_filter(product_query, price_band)
    products = db.scalars(product_query.order_by(func.lower(Product.title), Product.id).limit(500)).all()
    if not products:
        return []

    product_ids = [item.id for item in products]
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=max(7, min(int(lookback_days or 90), 730)))

    perf_query = (
        select(
            OrderItem.product_id.label("product_id"),
            func.sum(OrderItem.line_total).label("revenue"),
            func.sum(OrderItem.quantity).label("units"),
            func.sum(OrderItem.line_total * Product.margin_pct).label("margin_value"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(
            Order.store_id == store_id,
            OrderItem.product_id.in_(product_ids),
            Order.ordered_at >= since,
            Order.ordered_at < now,
        )
        .group_by(OrderItem.product_id)
    )
    perf_query = _apply_order_occasion_filter(perf_query, occasion=occasion, occasions=occasion_tokens)
    perf_rows = db.execute(perf_query).all()
    perf_map: dict[str, dict[str, float]] = {}
    for row in perf_rows:
        revenue = float(row.revenue or 0.0)
        units = float(row.units or 0.0)
        margin_value = float(row.margin_value or 0.0)
        perf_map[row.product_id] = {
            "revenue": revenue,
            "units": units,
            "margin_rate": (margin_value / revenue) if revenue > 0 else 0.0,
        }

    rows: list[dict[str, str]] = []
    for item in products:
        if occasion_tokens and item.id not in perf_map:
            continue
        perf = perf_map.get(item.id, {"revenue": 0.0, "units": 0.0, "margin_rate": 0.0})
        rows.append(
            {
                "store_id": store_id,
                "store_name": store_name,
                "view": view.value,
                "export_row_type": "inventory_product_snapshot",
                "inventory_product_id": _as_scalar(item.id),
                "inventory_title": _as_scalar(item.title),
                "inventory_brand": _as_scalar(item.brand),
                "inventory_category": _as_scalar(item.category),
                "inventory_price": _as_scalar(item.price),
                "inventory_availability": _as_scalar(item.availability),
                "inventory_stock_state": _stock_state_label(item.availability, item.inventory_qty),
                "inventory_qty": _as_scalar(item.inventory_qty),
                "inventory_perf_revenue": _as_scalar(perf.get("revenue", 0.0)),
                "inventory_perf_units": _as_scalar(perf.get("units", 0.0)),
                "inventory_perf_margin_rate": _as_scalar(perf.get("margin_rate", 0.0)),
            }
        )
    return rows


def _merch_export_csv_impl(params: MerchExportCsvRequest) -> MerchExportCsvResponse:
    generated_at = datetime.now(timezone.utc)
    override_map = _merch_override_map(params.recommendation_overrides or [])
    occasion_tokens = _normalized_occasion_tokens(occasion=params.occasion, occasions=params.occasions)
    resolved_occasion = occasion_tokens[0] if occasion_tokens else None
    with SessionLocal() as db:
        if params.view == MerchWorkspaceView.actions:
            result = merchandising_action_recommendations(
                db,
                store_query=params.store_query,
                store_id=params.store_id,
                question=params.question,
                objective=params.objective,
                lookback_days=params.lookback_days,
                top_k=params.top_k,
                category=params.category,
                brand=params.brand,
                price_band=params.price_band,
                occasion=resolved_occasion,
                occasions=occasion_tokens,
                compare_mode=params.compare_mode,
                peer_mode=params.peer_mode,
                compare_store_id=params.compare_store_id,
            )
            headers = [
                "store_id",
                "store_name",
                "view",
                "compare_store_id",
                "compare_store_name",
                "action",
                "product_id",
                "title",
                "brand",
                "category",
                "price",
                "price_band",
                "metric_value",
                "peer_delta",
                "prior_period_delta",
                "objective",
                "compare_mode",
                "peer_mode",
                "lookback_days",
                "model_priority_tier",
                "final_action",
                "final_priority_tier",
                "override_note",
                "rationale",
                "link",
                "image_url",
            ]
            rows = []
            total_recommendations = max(1, len(result.recommendations))
            for index, item in enumerate(result.recommendations):
                model_priority_tier = _priority_tier_for_rank(index, total_recommendations)
                override = override_map.get(item.product_id)
                final_action = override.final_action.value if override else item.action.value
                final_priority = override.priority_tier.value if override else model_priority_tier.value
                rows.append(
                    {
                    "store_id": result.store.id,
                    "store_name": result.store.name,
                    "view": MerchWorkspaceView.actions.value,
                    "compare_store_id": _as_scalar(result.compare_store_id),
                    "compare_store_name": _as_scalar(result.compare_store_name),
                    "action": _as_scalar(item.action),
                    "product_id": _as_scalar(item.product_id),
                    "title": _as_scalar(item.title),
                    "brand": _as_scalar(item.brand),
                    "category": _as_scalar(item.category),
                    "price": _as_scalar(item.price),
                    "price_band": _as_scalar(item.price_band),
                    "metric_value": _as_scalar(item.metric_value),
                    "peer_delta": _as_scalar(item.peer_delta),
                    "prior_period_delta": _as_scalar(item.prior_period_delta),
                    "objective": _as_scalar(result.objective),
                    "compare_mode": _as_scalar(result.compare_mode),
                    "peer_mode": _as_scalar(result.peer_mode),
                    "lookback_days": _as_scalar(result.lookback_days),
                    "model_priority_tier": model_priority_tier.value,
                    "final_action": final_action,
                    "final_priority_tier": final_priority,
                    "override_note": _as_scalar(override.override_note if override else None),
                    "rationale": _as_scalar(item.rationale),
                    "link": _as_scalar(item.link),
                    "image_url": _as_scalar(item.image_url),
                }
                )
            store = result.store
            lookback_for_snapshot = result.lookback_days
        elif params.view == MerchWorkspaceView.diagnostics:
            result = merchandising_diagnostics(
                db,
                store_query=params.store_query,
                store_id=params.store_id,
                question=params.question,
                lookback_days=params.lookback_days,
                category=params.category,
                brand=params.brand,
                price_band=params.price_band,
                occasion=resolved_occasion,
                occasions=occasion_tokens,
                compare_mode=params.compare_mode,
                peer_mode=params.peer_mode,
                compare_store_id=params.compare_store_id,
            )
            headers = [
                "store_id",
                "store_name",
                "view",
                "compare_store_id",
                "compare_store_name",
                "dimension",
                "subject",
                "status",
                "current_value",
                "peer_value",
                "prior_value",
                "delta",
                "current_units",
                "peer_units",
                "prior_units",
                "current_margin_pct",
                "peer_margin_pct",
                "prior_margin_pct",
                "compare_mode",
                "peer_mode",
                "lookback_days",
                "rationale",
            ]
            rows = [
                {
                    "store_id": result.store.id,
                    "store_name": result.store.name,
                    "view": MerchWorkspaceView.diagnostics.value,
                    "compare_store_id": _as_scalar(result.compare_store_id),
                    "compare_store_name": _as_scalar(result.compare_store_name),
                    "dimension": _as_scalar(item.dimension),
                    "subject": _as_scalar(item.subject),
                    "status": _as_scalar(item.status),
                    "current_value": _as_scalar(item.current_value),
                    "peer_value": _as_scalar(item.peer_value),
                    "prior_value": _as_scalar(item.prior_value),
                    "delta": _as_scalar(item.delta),
                    "current_units": _as_scalar(item.current_units),
                    "peer_units": _as_scalar(item.peer_units),
                    "prior_units": _as_scalar(item.prior_units),
                    "current_margin_pct": _as_scalar(item.current_margin_pct),
                    "peer_margin_pct": _as_scalar(item.peer_margin_pct),
                    "prior_margin_pct": _as_scalar(item.prior_margin_pct),
                    "compare_mode": _as_scalar(result.compare_mode),
                    "peer_mode": _as_scalar(result.peer_mode),
                    "lookback_days": _as_scalar(result.lookback_days),
                    "rationale": _as_scalar(item.rationale),
                }
                for item in result.insights
            ]
            store = result.store
            lookback_for_snapshot = result.lookback_days
        elif params.view == MerchWorkspaceView.trends:
            result = merchandising_trend_summary(
                db,
                store_query=params.store_query,
                store_id=params.store_id,
                question=params.question,
                lookback_days=params.lookback_days,
                category=params.category,
                brand=params.brand,
                price_band=params.price_band,
                occasion=resolved_occasion,
                occasions=occasion_tokens,
                compare_mode=params.compare_mode,
                peer_mode=params.peer_mode,
                compare_store_id=params.compare_store_id,
            )
            headers = [
                "store_id",
                "store_name",
                "view",
                "row_type",
                "compare_store_id",
                "compare_store_name",
                "subject",
                "period_start",
                "current_value",
                "peer_value",
                "prior_value",
                "pct_change",
                "current_revenue",
                "baseline_revenue",
                "current_units",
                "baseline_units",
                "compare_mode",
                "peer_mode",
                "lookback_days",
                "rationale",
            ]
            rows = [
                {
                    "store_id": result.store.id,
                    "store_name": result.store.name,
                    "view": MerchWorkspaceView.trends.value,
                    "row_type": "highlight",
                    "compare_store_id": _as_scalar(result.compare_store_id),
                    "compare_store_name": _as_scalar(result.compare_store_name),
                    "subject": _as_scalar(item.subject),
                    "period_start": "",
                    "current_value": _as_scalar(item.current_value),
                    "peer_value": _as_scalar(item.peer_value),
                    "prior_value": _as_scalar(item.prior_value),
                    "pct_change": _as_scalar(item.pct_change),
                    "current_revenue": "",
                    "baseline_revenue": "",
                    "current_units": "",
                    "baseline_units": "",
                    "compare_mode": _as_scalar(result.compare_mode),
                    "peer_mode": _as_scalar(result.peer_mode),
                    "lookback_days": _as_scalar(result.lookback_days),
                    "rationale": _as_scalar(item.rationale),
                }
                for item in result.highlights
            ]
            rows.extend(
                {
                    "store_id": result.store.id,
                    "store_name": result.store.name,
                    "view": MerchWorkspaceView.trends.value,
                    "row_type": "timeseries",
                    "compare_store_id": _as_scalar(result.compare_store_id),
                    "compare_store_name": _as_scalar(result.compare_store_name),
                    "subject": "",
                    "period_start": _as_scalar(point.period_start),
                    "current_value": "",
                    "peer_value": "",
                    "prior_value": "",
                    "pct_change": "",
                    "current_revenue": _as_scalar(point.current_revenue),
                    "baseline_revenue": _as_scalar(point.baseline_revenue),
                    "current_units": _as_scalar(point.current_units),
                    "baseline_units": _as_scalar(point.baseline_units),
                    "compare_mode": _as_scalar(result.compare_mode),
                    "peer_mode": _as_scalar(result.peer_mode),
                    "lookback_days": _as_scalar(result.lookback_days),
                    "rationale": "Weekly trend point.",
                }
                for point in result.time_series
            )
            store = result.store
            lookback_for_snapshot = result.lookback_days
        elif params.view == MerchWorkspaceView.inventory:
            result = _merch_inventory_view_impl(
                MerchInventoryViewRequest(
                    store_query=params.store_query,
                    store_id=params.store_id,
                    lookback_days=params.lookback_days,
                    category=params.category,
                    brand=params.brand,
                    price_band=params.price_band,
                    occasion=resolved_occasion,
                    occasions=occasion_tokens,
                    inventory_scope=params.inventory_scope,
                    future_window_days=params.future_window_days,
                    limit=300,
                )
            )
            headers = [
                "store_id",
                "store_name",
                "view",
                "row_type",
                "product_id",
                "offer_id",
                "title",
                "brand",
                "category",
                "size",
                "price",
                "availability",
                "stock_state",
                "inventory_qty",
                "available_on",
                "offer_status",
                "perf_revenue",
                "perf_units",
                "perf_margin_rate",
                "inventory_scope",
                "future_window_days",
                "lookback_days",
                "link",
                "image_url",
            ]
            rows = [
                {
                    "store_id": result.store.id,
                    "store_name": result.store.name,
                    "view": MerchWorkspaceView.inventory.value,
                    "row_type": _as_scalar(item.row_type),
                    "product_id": _as_scalar(item.product_id),
                    "offer_id": _as_scalar(item.offer_id),
                    "title": _as_scalar(item.title),
                    "brand": _as_scalar(item.brand),
                    "category": _as_scalar(item.category),
                    "size": _as_scalar(item.size),
                    "price": _as_scalar(item.price),
                    "availability": _as_scalar(item.availability),
                    "stock_state": _as_scalar(item.stock_state),
                    "inventory_qty": _as_scalar(item.inventory_qty),
                    "available_on": _as_scalar(item.available_on),
                    "offer_status": _as_scalar(item.offer_status),
                    "perf_revenue": _as_scalar(item.perf_revenue),
                    "perf_units": _as_scalar(item.perf_units),
                    "perf_margin_rate": _as_scalar(item.perf_margin_rate),
                    "inventory_scope": _as_scalar(result.inventory_scope),
                    "future_window_days": _as_scalar(result.future_window_days),
                    "lookback_days": _as_scalar(result.lookback_days),
                    "link": _as_scalar(item.link),
                    "image_url": _as_scalar(item.image_url),
                }
                for item in result.rows
            ]
            store = result.store
            lookback_for_snapshot = result.lookback_days
        else:
            result = _merch_mix_analysis_impl(
                MerchProductMixRecommendationsRequest(
                    store_query=params.store_query,
                    store_id=params.store_id,
                    lookback_days=params.lookback_days,
                    top_k=params.top_k,
                    category=params.category,
                    brand=params.brand,
                    price_band=params.price_band,
                    occasion=resolved_occasion,
                    occasions=occasion_tokens,
                    inventory_scope=params.inventory_scope,
                    future_window_days=params.future_window_days,
                    recommendation_overrides=params.recommendation_overrides,
                )
            )
            headers = [
                "store_id",
                "store_name",
                "view",
                "action",
                "fit_score",
                "expected_mix_impact",
                "brand",
                "category",
                "current_product_id",
                "current_title",
                "current_revenue",
                "current_units",
                "offer_id",
                "offer_title",
                "offer_status",
                "available_on",
                "offer_price",
                "inventory_scope",
                "future_window_days",
                "lookback_days",
                "rationale",
            ]
            rows = [
                {
                    "store_id": result.store.id,
                    "store_name": result.store.name,
                    "view": MerchWorkspaceView.mix_analysis.value,
                    "action": _as_scalar(item.action),
                    "fit_score": _as_scalar(item.fit_score),
                    "expected_mix_impact": _as_scalar(item.expected_mix_impact),
                    "brand": _as_scalar(item.brand),
                    "category": _as_scalar(item.category),
                    "current_product_id": _as_scalar(item.current_product_id),
                    "current_title": _as_scalar(item.current_title),
                    "current_revenue": _as_scalar(item.current_revenue),
                    "current_units": _as_scalar(item.current_units),
                    "offer_id": _as_scalar(item.offer_id),
                    "offer_title": _as_scalar(item.offer_title),
                    "offer_status": _as_scalar(item.offer_status),
                    "available_on": _as_scalar(item.available_on),
                    "offer_price": _as_scalar(item.offer_price),
                    "inventory_scope": _as_scalar(result.inventory_scope),
                    "future_window_days": _as_scalar(result.future_window_days),
                    "lookback_days": _as_scalar(result.lookback_days),
                    "rationale": _as_scalar(item.rationale),
                }
                for item in result.rows
            ]
            store = result.store
            lookback_for_snapshot = result.lookback_days

        if params.export_mode == MerchExportMode.legacy_combined and params.view in {
            MerchWorkspaceView.actions,
            MerchWorkspaceView.diagnostics,
            MerchWorkspaceView.trends,
        }:
            inventory_export_headers = [
                "export_row_type",
                "inventory_product_id",
                "inventory_title",
                "inventory_brand",
                "inventory_category",
                "inventory_price",
                "inventory_availability",
                "inventory_stock_state",
                "inventory_qty",
                "inventory_perf_revenue",
                "inventory_perf_units",
                "inventory_perf_margin_rate",
            ]
            for header in inventory_export_headers:
                if header not in headers:
                    headers.append(header)
            rows.extend(
                _merch_inventory_snapshot_rows(
                    db,
                    store_id=store.id,
                    store_name=store.name,
                    view=params.view,
                    lookback_days=lookback_for_snapshot,
                    category=params.category,
                    brand=params.brand,
                    price_band=params.price_band,
                    occasion=resolved_occasion,
                    occasions=occasion_tokens,
                )
            )

    csv_payload = _csv_text(headers, rows)
    filename = f"merch_{store.id}_{params.view.value}_{generated_at.strftime('%Y%m%d_%H%M%S')}.csv"
    return MerchExportCsvResponse(
        view=params.view,
        store=store,
        filename=filename,
        headers=headers,
        rows=[MerchExportCsvRow(values=row) for row in rows],
        row_count=len(rows),
        csv_text=csv_payload,
        generated_at=generated_at,
    )


def _exec_export_csv_impl(params: ExecutiveExportCsvRequest) -> ExecutiveExportCsvResponse:
    generated_at = datetime.now(timezone.utc)
    with SessionLocal() as db:
        overview = executive_overview(
            db,
            store_query=params.store_query,
            store_id=params.store_id,
            store_ids=params.store_ids or None,
            lookback_days=params.lookback_days,
            objective=params.objective,
            top_k_stores=params.top_k_stores,
        )
        scoped_store_ids = [item.store_id for item in overview.stores]
        current_by_store_category: dict[tuple[str, str], dict[str, float]] = {}
        prior_revenue_by_store_category: dict[tuple[str, str], float] = {}
        if scoped_store_ids:
            since = overview.generated_at - timedelta(days=overview.lookback_days)
            prior_since = since - timedelta(days=overview.lookback_days)
            current_rows = db.execute(
                select(
                    Order.store_id.label("store_id"),
                    Product.category.label("category"),
                    func.sum(OrderItem.line_total).label("revenue"),
                    func.sum(OrderItem.quantity).label("units"),
                    func.sum(OrderItem.line_total * Product.margin_pct).label("margin_value"),
                )
                .join(OrderItem, OrderItem.order_id == Order.id)
                .join(Product, Product.id == OrderItem.product_id)
                .where(Order.store_id.in_(scoped_store_ids), Order.ordered_at >= since, Order.ordered_at < overview.generated_at)
                .group_by(Order.store_id, Product.category)
            ).all()
            prior_rows = db.execute(
                select(
                    Order.store_id.label("store_id"),
                    Product.category.label("category"),
                    func.sum(OrderItem.line_total).label("revenue"),
                )
                .join(OrderItem, OrderItem.order_id == Order.id)
                .join(Product, Product.id == OrderItem.product_id)
                .where(Order.store_id.in_(scoped_store_ids), Order.ordered_at >= prior_since, Order.ordered_at < since)
                .group_by(Order.store_id, Product.category)
            ).all()
            for row in current_rows:
                revenue = float(row.revenue or 0.0)
                units = float(row.units or 0.0)
                margin_value = float(row.margin_value or 0.0)
                current_by_store_category[(row.store_id, row.category)] = {
                    "revenue": revenue,
                    "units": units,
                    "margin_rate": (margin_value / revenue) if revenue > 0 else 0.0,
                }
            for row in prior_rows:
                prior_revenue_by_store_category[(row.store_id, row.category)] = float(row.revenue or 0.0)

    headers = [
        "data_mode",
        "view",
        "row_type",
        "store_id",
        "store_name",
        "city",
        "state",
        "rank",
        "category",
        "revenue",
        "units",
        "margin_rate",
        "revenue_share_pct",
        "revenue_delta_pct",
        "lookback_days",
        "objective",
        "generated_at",
    ]
    rows = [
        {
            "data_mode": "raw",
            "view": params.view.value,
            "row_type": "store_summary",
            "store_id": _as_scalar(item.store_id),
            "store_name": _as_scalar(item.store_name),
            "city": _as_scalar(item.city),
            "state": _as_scalar(item.state),
            "rank": _as_scalar(item.rank),
            "category": "",
            "revenue": _as_scalar(item.revenue),
            "units": _as_scalar(item.units),
            "margin_rate": _as_scalar(item.margin_rate),
            "revenue_share_pct": _as_scalar(item.revenue_share_pct),
            "revenue_delta_pct": _as_scalar(item.revenue_delta_pct),
            "lookback_days": _as_scalar(overview.lookback_days),
            "objective": _as_scalar(overview.objective),
            "generated_at": overview.generated_at.isoformat(),
        }
        for item in overview.stores
    ]
    for item in overview.stores:
        store_revenue = float(item.revenue or 0.0)
        category_rows = [
            (category, payload)
            for (store_id, category), payload in current_by_store_category.items()
            if store_id == item.store_id
        ]
        category_rows.sort(key=lambda entry: entry[1].get("revenue", 0.0), reverse=True)
        for category, payload in category_rows:
            category_revenue = float(payload.get("revenue", 0.0))
            prior_revenue = float(prior_revenue_by_store_category.get((item.store_id, category), 0.0))
            category_delta_pct = _pct_delta(category_revenue, prior_revenue)
            rows.append(
                {
                    "data_mode": "raw",
                    "view": params.view.value,
                    "row_type": "category_performance",
                    "store_id": _as_scalar(item.store_id),
                    "store_name": _as_scalar(item.store_name),
                    "city": _as_scalar(item.city),
                    "state": _as_scalar(item.state),
                    "rank": _as_scalar(item.rank),
                    "category": _as_scalar(category),
                    "revenue": _as_scalar(category_revenue),
                    "units": _as_scalar(payload.get("units", 0.0)),
                    "margin_rate": _as_scalar(payload.get("margin_rate", 0.0)),
                    "revenue_share_pct": _as_scalar(((category_revenue / store_revenue) * 100.0) if store_revenue > 0 else 0.0),
                    "revenue_delta_pct": _as_scalar(category_delta_pct),
                    "lookback_days": _as_scalar(overview.lookback_days),
                    "objective": _as_scalar(overview.objective),
                    "generated_at": overview.generated_at.isoformat(),
                }
            )

    csv_payload = _csv_text(headers, rows)
    scope_label = "selected" if params.store_ids or params.store_id or params.store_query else "network"
    filename = f"exec_{scope_label}_{params.view.value}_{generated_at.strftime('%Y%m%d_%H%M%S')}.csv"
    return ExecutiveExportCsvResponse(
        view=params.view,
        filename=filename,
        lookback_days=overview.lookback_days,
        objective=overview.objective,
        headers=headers,
        rows=[ExecutiveExportCsvRow(values=row) for row in rows],
        row_count=len(rows),
        csv_text=csv_payload,
        generated_at=generated_at,
    )


@mcp.tool(
    name="fashion_render_customer_search_workspace",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_render_tool_meta(
        _CUSTOMER_SEARCH_WIDGET_RESOURCE_URI,
        invoking="Opening customer workspace...",
        invoked="Customer workspace ready.",
    ),
    structured_output=False,
)
def fashion_render_customer_search_workspace(
    query: str | None = None,
    limit: int = 10,
    selected_customer_id: str | None = None,
    initial_style_constraints: StyleConstraints | None = None,
    initial_notice: str | None = None,
    initial_email_draft_id: str | None = None,
    initial_email_subject: str | None = None,
    initial_email_body: str | None = None,
    initial_recommendation_response: StoreAssociateRecommendationResponse | None = None,
    initial_selected_product_ids: list[str] | None = None,
) -> CallToolResult:
    """Render the customer workspace inside ChatGPT.

    Optional hydration fields (`selected_customer_id`, `initial_style_constraints`,
    `initial_notice`, initial recommendation fields, and initial email draft fields) can seed the UI state from
    prior model/tool context.
    """
    effective_limit = max(1, min(limit, 25))
    workspace_payload = _customer_search_workspace_payload(
        query=query,
        limit=effective_limit,
        selected_customer_id=selected_customer_id,
        initial_style_constraints=initial_style_constraints,
        initial_notice=initial_notice,
        initial_email_draft_id=initial_email_draft_id,
        initial_email_subject=initial_email_subject,
        initial_email_body=initial_email_body,
        initial_recommendation_response=initial_recommendation_response,
        initial_selected_product_ids=initial_selected_product_ids,
    )
    structured_payload = {"kind": "customer_search_workspace", "payload": workspace_payload}
    summary = (
        f"Opened customer workspace with query '{workspace_payload['query']}'."
        if workspace_payload["query"]
        else "Opened customer workspace."
    )
    return _calltool_result(
        text=summary,
        payload=structured_payload,
        meta=_render_tool_meta(
            _CUSTOMER_SEARCH_WIDGET_RESOURCE_URI,
            invoking="Opening customer workspace...",
            invoked="Customer workspace ready.",
        ),
    )


@mcp.tool(
    name="fashion_open_customer_workspace",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_render_tool_meta(
        _CUSTOMER_SEARCH_WIDGET_RESOURCE_URI,
        invoking="Opening customer workspace...",
        invoked="Customer workspace ready.",
    ),
    structured_output=False,
)
def fashion_open_customer_workspace(
    customer_query: str,
    style_constraints: StyleConstraints | None = None,
    initial_notice: str | None = None,
    initial_email_draft_id: str | None = None,
    initial_email_subject: str | None = None,
    initial_email_body: str | None = None,
    occasion: str | None = None,
    budget_min: float | None = None,
    budget_max: float | None = None,
    top_k: int = 6,
    retrieval_mode: RetrievalMode = RetrievalMode.auto,
    seed_recommendations: bool = True,
    limit: int = 10,
) -> CallToolResult:
    """Resolve customer query and open a hydrated customer workspace in one call.

    Prefer this as the default workspace entrypoint for chat flows. When an image
    is uploaded in chat, extract cues and pass them as `style_constraints`.
    For chat-first email flows, pass `initial_email_draft_id` (and optional
    `initial_email_subject`/`initial_email_body`) to hydrate draft context.
    By default this also seeds recommendation rows so the opened workspace aligns
    with the chat-visible recommendation list.
    """
    normalized_query = customer_query.strip()
    if not normalized_query:
        raise ValueError("customer_query is required.")

    effective_limit = max(1, min(limit, 25))
    effective_top_k = max(1, min(top_k, 12))
    lookup = fashion_lookup_customer(normalized_query, limit=effective_limit)

    selected_customer_id: str | None = None
    if lookup.mode == "resolved" and lookup.resolved is not None:
        selected_customer_id = lookup.resolved.id
    elif len(lookup.candidates) == 1:
        selected_customer_id = lookup.candidates[0].id

    initial_recommendation_response: StoreAssociateRecommendationResponse | None = None
    initial_selected_product_ids: list[str] = []
    if selected_customer_id and seed_recommendations:
        initial_recommendation_response = _associate_recommendation_impl(
            customer_id=selected_customer_id,
            occasion=occasion,
            budget_min=budget_min,
            budget_max=budget_max,
            top_k=effective_top_k,
            retrieval_mode=retrieval_mode,
            style_constraints=style_constraints,
        )
        for row in initial_recommendation_response.recommendation.recommendations[:3]:
            product_id = str(row.product_id or "").strip()
            if product_id and product_id not in initial_selected_product_ids:
                initial_selected_product_ids.append(product_id)

    notice = initial_notice.strip() if initial_notice and initial_notice.strip() else None
    if notice is None and style_constraints is not None and not style_constraints.is_empty():
        if style_constraints.constraint_source == "chat_image":
            notice = "Image guidance loaded from this chat turn."
        else:
            notice = "Style guidance loaded from this chat turn."

    return fashion_render_customer_search_workspace(
        query=None if selected_customer_id else normalized_query,
        limit=effective_limit,
        selected_customer_id=selected_customer_id,
        initial_style_constraints=style_constraints,
        initial_notice=notice,
        initial_email_draft_id=initial_email_draft_id,
        initial_email_subject=initial_email_subject,
        initial_email_body=initial_email_body,
        initial_recommendation_response=initial_recommendation_response,
        initial_selected_product_ids=initial_selected_product_ids,
    )


@mcp.tool(
    name="fashion_open_customer_workspace_with_email_draft",
    annotations=_tool_annotations(read_only=False, idempotent=False, open_world=True),
    meta=_render_tool_meta(
        _CUSTOMER_SEARCH_WIDGET_RESOURCE_URI,
        invoking="Preparing draft and opening customer workspace...",
        invoked="Customer workspace ready with draft.",
    ),
    structured_output=False,
)
def fashion_open_customer_workspace_with_email_draft(
    customer_query: str,
    style_constraints: StyleConstraints | None = None,
    occasion: str | None = None,
    budget_min: float | None = None,
    budget_max: float | None = None,
    top_k: int = 6,
    retrieval_mode: RetrievalMode = RetrievalMode.auto,
    selected_product_ids: list[str] | None = None,
    to_email: str | None = None,
    subject: str | None = None,
    initial_notice: str | None = None,
    limit: int = 10,
) -> CallToolResult:
    """Prepare a customer email draft, then open a hydrated workspace with aligned recommendations and draft content."""
    normalized_query = customer_query.strip()
    if not normalized_query:
        raise ValueError("customer_query is required.")

    effective_limit = max(1, min(limit, 25))
    effective_top_k = max(1, min(top_k, 12))
    lookup = fashion_lookup_customer(normalized_query, limit=effective_limit)
    selected_customer_id: str | None = None
    if lookup.mode == "resolved" and lookup.resolved is not None:
        selected_customer_id = lookup.resolved.id
    elif len(lookup.candidates) == 1:
        selected_customer_id = lookup.candidates[0].id

    if not selected_customer_id:
        notice = (
            initial_notice.strip()
            if initial_notice and initial_notice.strip()
            else "Multiple customer matches found. Select one customer before preparing an email draft."
        )
        return fashion_render_customer_search_workspace(
            query=normalized_query,
            limit=effective_limit,
            selected_customer_id=None,
            initial_style_constraints=style_constraints,
            initial_notice=notice,
        )

    recommendation = _associate_recommendation_impl(
        customer_id=selected_customer_id,
        occasion=occasion,
        budget_min=budget_min,
        budget_max=budget_max,
        top_k=effective_top_k,
        retrieval_mode=retrieval_mode,
        style_constraints=style_constraints,
    )

    normalized_selected_product_ids: list[str] = []
    for value in selected_product_ids or []:
        token = str(value or "").strip()
        if token and token not in normalized_selected_product_ids:
            normalized_selected_product_ids.append(token)
    if not normalized_selected_product_ids:
        for row in recommendation.recommendation.recommendations[:3]:
            token = str(row.product_id or "").strip()
            if token and token not in normalized_selected_product_ids:
                normalized_selected_product_ids.append(token)

    with SessionLocal() as db:
        draft = prepare_customer_email_draft(
            db,
            store_id=recommendation.store.id,
            customer_id=selected_customer_id,
            occasion=occasion,
            budget_min=budget_min,
            budget_max=budget_max,
            top_k=effective_top_k,
            retrieval_mode=retrieval_mode,
            selected_product_ids=normalized_selected_product_ids,
            to_email=to_email,
            subject=subject,
            style_constraints=style_constraints,
        )

    notice = initial_notice.strip() if initial_notice and initial_notice.strip() else None
    if notice is None:
        notice = f"Prepared draft {draft.message.id} from current recommendations."

    return fashion_render_customer_search_workspace(
        query=None,
        limit=effective_limit,
        selected_customer_id=selected_customer_id,
        initial_style_constraints=style_constraints,
        initial_notice=notice,
        initial_email_draft_id=draft.message.id,
        initial_email_subject=draft.subject,
        initial_email_body=draft.message.body_text,
        initial_recommendation_response=recommendation,
        initial_selected_product_ids=normalized_selected_product_ids,
    )


@mcp.tool(
    name="fashion_render_unified_workspace",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_render_tool_meta(
        _UNIFIED_WORKSPACE_RESOURCE_URI,
        invoking="Opening unified workspace...",
        invoked="Unified workspace ready.",
    ),
    structured_output=False,
)
def fashion_render_unified_workspace(
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    active_store_id: str | None = None,
    initial_view: UnifiedWorkspaceView = UnifiedWorkspaceView.executive_overview,
    lookback_days: int = 90,
    objective: Objective = Objective.revenue,
    top_k: int = 12,
    category: str | None = None,
    brand: str | None = None,
    brands: list[str] | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
    occasions: list[str] | None = None,
    row_mode: UnifiedRowMode = UnifiedRowMode.store_product,
    override_scope: OverrideScope = OverrideScope.store,
    inventory_scope: InventoryScope = InventoryScope.combined,
    future_window_days: int = 120,
    question: str | None = None,
    recommendation_overrides: list[UnifiedRecommendationOverride] | None = None,
    initial_notice: str | None = None,
) -> CallToolResult:
    """Render the unified executive + merchandising workspace inside ChatGPT."""
    workspace_payload = _unified_workspace_payload(
        store_query=store_query,
        store_id=store_id,
        store_ids=store_ids,
        active_store_id=active_store_id,
        initial_view=initial_view,
        lookback_days=lookback_days,
        objective=objective,
        top_k=top_k,
        category=category,
        brand=brand,
        brands=brands,
        price_band=price_band,
        occasion=occasion,
        occasions=occasions,
        row_mode=row_mode,
        override_scope=override_scope,
        inventory_scope=inventory_scope,
        future_window_days=future_window_days,
        question=question,
        recommendation_overrides=recommendation_overrides,
        initial_notice=initial_notice,
    )
    structured_payload = {"kind": "unified_workspace", "payload": workspace_payload}
    return _calltool_result(
        text="Opened unified workspace.",
        payload=structured_payload,
        meta=_render_tool_meta(
            _UNIFIED_WORKSPACE_RESOURCE_URI,
            invoking="Opening unified workspace...",
            invoked="Unified workspace ready.",
        ),
    )


@mcp.tool(
    name="fashion_open_unified_workspace",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_render_tool_meta(
        _UNIFIED_WORKSPACE_RESOURCE_URI,
        invoking="Opening unified workspace...",
        invoked="Unified workspace ready.",
    ),
    structured_output=False,
)
def fashion_open_unified_workspace(
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    active_store_id: str | None = None,
    initial_view: UnifiedWorkspaceView = UnifiedWorkspaceView.executive_overview,
    lookback_days: int = 90,
    objective: Objective = Objective.revenue,
    top_k: int = 12,
    category: str | None = None,
    brand: str | None = None,
    brands: list[str] | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
    occasions: list[str] | None = None,
    row_mode: UnifiedRowMode = UnifiedRowMode.store_product,
    override_scope: OverrideScope = OverrideScope.store,
    inventory_scope: InventoryScope = InventoryScope.combined,
    future_window_days: int = 120,
    question: str | None = None,
    recommendation_overrides: list[UnifiedRecommendationOverride] | None = None,
    initial_notice: str | None = None,
) -> CallToolResult:
    """Open unified workspace with optional store resolution and seeded tab context."""
    notice = initial_notice.strip() if initial_notice and initial_notice.strip() else None
    resolved_store_id = store_id

    if store_query and not resolved_store_id:
        normalized_query = store_query.strip()
        if normalized_query:
            resolved = fashion_resolve_store(normalized_query).resolved
            resolved_store_id = resolved.id
            if notice is None:
                notice = f"Resolved store {resolved.name} from '{normalized_query}'."
    elif resolved_store_id and notice is None:
        with SessionLocal() as db:
            resolved = resolve_store(db, store_id=resolved_store_id).resolved
        notice = f"Loaded store {resolved.name} in unified workspace."

    return fashion_render_unified_workspace(
        store_id=resolved_store_id,
        store_ids=store_ids,
        active_store_id=active_store_id or resolved_store_id,
        initial_view=initial_view,
        lookback_days=lookback_days,
        objective=objective,
        top_k=top_k,
        category=category,
        brand=brand,
        brands=brands,
        price_band=price_band,
        occasion=occasion,
        occasions=occasions,
        row_mode=row_mode,
        override_scope=override_scope,
        inventory_scope=inventory_scope,
        future_window_days=future_window_days,
        question=question,
        recommendation_overrides=recommendation_overrides,
        initial_notice=notice,
    )


@mcp.tool(
    name="fashion_render_merch_workspace",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_render_tool_meta(
        _MERCH_WORKSPACE_RESOURCE_URI,
        invoking="Opening merchandising workspace...",
        invoked="Merchandising workspace ready.",
    ),
    structured_output=False,
)
def fashion_render_merch_workspace(
    store_query: str | None = None,
    store_id: str | None = None,
    question: str | None = None,
    objective: Objective = Objective.margin,
    lookback_days: int = 90,
    top_k: int = 9,
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
    occasions: list[str] | None = None,
    inventory_scope: InventoryScope = InventoryScope.combined,
    future_window_days: int = 120,
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
    compare_store_id: str | None = None,
    strategy_packet_id: str | None = None,
    initial_notice: str | None = None,
) -> CallToolResult:
    """Render the merchandising workspace inside ChatGPT."""
    if not store_query and not store_id:
        raise ValueError("Provide store_query or store_id.")

    workspace_payload = _merch_workspace_payload(
        store_query=store_query,
        store_id=store_id,
        category=category,
        brand=brand,
        price_band=price_band,
        occasion=occasion,
        occasions=occasions,
        question=question,
        objective=objective,
        lookback_days=lookback_days,
        top_k=top_k,
        inventory_scope=inventory_scope,
        future_window_days=future_window_days,
        compare_mode=compare_mode,
        peer_mode=peer_mode,
        compare_store_id=compare_store_id,
        strategy_packet_id=strategy_packet_id,
        initial_notice=initial_notice,
    )
    structured_payload = {"kind": "merch_workspace", "payload": workspace_payload}
    summary = f"Opened merchandising workspace for {workspace_payload['store']['name']}."
    return _calltool_result(
        text=summary,
        payload=structured_payload,
        meta=_render_tool_meta(
            _MERCH_WORKSPACE_RESOURCE_URI,
            invoking="Opening merchandising workspace...",
            invoked="Merchandising workspace ready.",
        ),
    )


@mcp.tool(
    name="fashion_open_merch_workspace",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_render_tool_meta(
        _MERCH_WORKSPACE_RESOURCE_URI,
        invoking="Opening merchandising workspace...",
        invoked="Merchandising workspace ready.",
    ),
    structured_output=False,
)
def fashion_open_merch_workspace(
    store_query: str | None = None,
    store_id: str | None = None,
    question: str | None = None,
    objective: Objective = Objective.margin,
    lookback_days: int = 90,
    top_k: int = 9,
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
    occasions: list[str] | None = None,
    inventory_scope: InventoryScope = InventoryScope.combined,
    future_window_days: int = 120,
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
    compare_store_id: str | None = None,
    strategy_packet_id: str | None = None,
    initial_notice: str | None = None,
) -> CallToolResult:
    """Open unified merchandising workspace with optional store focus; defaults to all-store scope when no store is provided."""
    notice = initial_notice.strip() if initial_notice and initial_notice.strip() else None
    resolved_store = None
    if store_id:
        with SessionLocal() as db:
            resolved_store = resolve_store(db, store_id=store_id).resolved
        if not notice:
            notice = f"Opened merchandising workspace for {resolved_store.name}."
    else:
        normalized_query = (store_query or "").strip()
        if normalized_query:
            resolved_store = fashion_resolve_store(normalized_query).resolved
            if not notice:
                notice = f"Resolved store {resolved_store.name} from '{normalized_query}'."
        elif not notice:
            notice = "Opened merchandising workspace across all stores. Use Store Search + Stores selector to focus scope."

    return fashion_open_unified_workspace(
        active_store_id=resolved_store.id if resolved_store else None,
        initial_view=UnifiedWorkspaceView.inventory,
        question=question,
        objective=objective,
        lookback_days=lookback_days,
        top_k=top_k,
        category=category,
        brand=brand,
        price_band=price_band,
        occasion=occasion,
        occasions=occasions,
        row_mode=UnifiedRowMode.store_product,
        override_scope=OverrideScope.store,
        inventory_scope=inventory_scope,
        future_window_days=future_window_days,
        initial_notice=notice,
    )


@mcp.tool(
    name="fashion_merch_get_effective_strategy",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
)
def fashion_merch_get_effective_strategy(
    store_id: str,
    strategy_packet_id: str | None = None,
) -> MerchEffectiveStrategyResponse:
    """Return effective strategy for a store from packet defaults plus optional merch override."""
    _ensure_feature_enabled(settings.merch_strategy_context_enabled, "Merch strategy context")
    with SessionLocal() as db:
        return get_effective_merch_strategy(
            db,
            store_id=store_id,
            strategy_packet_id=strategy_packet_id,
        )


@mcp.tool(
    name="fashion_merch_save_strategy_override",
    annotations=_tool_annotations(read_only=False, idempotent=False, open_world=True),
)
def fashion_merch_save_strategy_override(
    packet_id: str,
    store_id: str,
    strategy_core: StrategyCore | None = None,
    tag_intensity: StrategyTagIntensity = StrategyTagIntensity.medium,
    use_packet_defaults: bool = False,
) -> MerchEffectiveStrategyResponse:
    """Save or clear store-level merchandising overrides for a published strategy packet."""
    _ensure_feature_enabled(settings.strategy_packet_enabled, "Strategy packet")
    _ensure_feature_enabled(settings.merch_strategy_context_enabled, "Merch strategy context")
    normalized_core = StrategyCore.model_validate(strategy_core) if isinstance(strategy_core, dict) else strategy_core
    with SessionLocal() as db:
        return save_merch_strategy_override(
            db,
            packet_id=packet_id,
            store_id=store_id,
            strategy_core=normalized_core,
            tag_intensity=tag_intensity,
            use_packet_defaults=use_packet_defaults,
        )


@mcp.tool(
    name="fashion_render_exec_workspace",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_render_tool_meta(
        _EXEC_WORKSPACE_RESOURCE_URI,
        invoking="Opening executive workspace...",
        invoked="Executive workspace ready.",
    ),
    structured_output=False,
)
def fashion_render_exec_workspace(
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    lookback_days: int = 90,
    objective: Objective = Objective.revenue,
    top_k_stores: int = 12,
    events: list[str] | None = None,
    brands: list[str] | None = None,
    discount_pct: float = 10.0,
    floor_space_shift_pct: float = 5.0,
    from_category: str | None = "womens_apparel",
    to_category: str | None = "shoes",
    to_email: str | None = None,
    autopilot_top_k: int = 6,
    optimize_discount_min_pct: float = 0.0,
    optimize_discount_max_pct: float = 20.0,
    optimize_discount_step_pct: float = 5.0,
    optimize_shift_min_pct: float = 0.0,
    optimize_shift_max_pct: float = 20.0,
    optimize_shift_step_pct: float = 5.0,
    optimize_top_k_scenarios: int = 3,
    min_margin_rate: float = 0.40,
    max_discount_pct: float = 20.0,
    strategy_packet_id: str | None = None,
    initial_notice: str | None = None,
) -> CallToolResult:
    """Render the executive workspace inside ChatGPT."""
    workspace_payload = _exec_workspace_payload(
        store_query=store_query,
        store_id=store_id,
        store_ids=store_ids,
        lookback_days=lookback_days,
        objective=objective,
        top_k_stores=top_k_stores,
        events=events,
        brands=brands,
        discount_pct=discount_pct,
        floor_space_shift_pct=floor_space_shift_pct,
        from_category=from_category,
        to_category=to_category,
        to_email=to_email,
        autopilot_top_k=autopilot_top_k,
        optimize_discount_min_pct=optimize_discount_min_pct,
        optimize_discount_max_pct=optimize_discount_max_pct,
        optimize_discount_step_pct=optimize_discount_step_pct,
        optimize_shift_min_pct=optimize_shift_min_pct,
        optimize_shift_max_pct=optimize_shift_max_pct,
        optimize_shift_step_pct=optimize_shift_step_pct,
        optimize_top_k_scenarios=optimize_top_k_scenarios,
        min_margin_rate=min_margin_rate,
        max_discount_pct=max_discount_pct,
        strategy_packet_id=strategy_packet_id,
        initial_notice=initial_notice,
    )
    structured_payload = {"kind": "exec_workspace", "payload": workspace_payload}
    summary = "Opened executive workspace."
    return _calltool_result(
        text=summary,
        payload=structured_payload,
        meta=_render_tool_meta(
            _EXEC_WORKSPACE_RESOURCE_URI,
            invoking="Opening executive workspace...",
            invoked="Executive workspace ready.",
        ),
    )


@mcp.tool(
    name="fashion_open_exec_workspace",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_render_tool_meta(
        _EXEC_WORKSPACE_RESOURCE_URI,
        invoking="Opening executive workspace...",
        invoked="Executive workspace ready.",
    ),
    structured_output=False,
)
def fashion_open_exec_workspace(
    lookback_days: int = 90,
    objective: Objective = Objective.revenue,
    top_k_stores: int = 12,
    optimize_discount_min_pct: float = 0.0,
    optimize_discount_max_pct: float = 20.0,
    optimize_discount_step_pct: float = 5.0,
    optimize_shift_min_pct: float = 0.0,
    optimize_shift_max_pct: float = 20.0,
    optimize_shift_step_pct: float = 5.0,
    optimize_top_k_scenarios: int = 3,
    min_margin_rate: float = 0.40,
    max_discount_pct: float = 20.0,
    initial_notice: str | None = None,
) -> CallToolResult:
    """Open the executive workspace with company-wide defaults."""
    notice = initial_notice.strip() if initial_notice and initial_notice.strip() else None
    if notice is None:
        notice = "Company-wide unified executive scope loaded."
    return fashion_open_unified_workspace(
        initial_view=UnifiedWorkspaceView.executive_overview,
        lookback_days=lookback_days,
        objective=objective,
        top_k=top_k_stores,
        initial_notice=notice,
    )


@mcp.tool(
    name="fashion_store_associate_recommend",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_store_associate_recommend(
    store_query: str | None = None,
    store_id: str | None = None,
    customer_email: str | None = None,
    customer_id: str | None = None,
    customer_phone_e164: str | None = None,
    phone_last4: str | None = None,
    occasion: str | None = None,
    budget_min: float | None = None,
    budget_max: float | None = None,
    top_k: int = 5,
    retrieval_mode: RetrievalMode = RetrievalMode.auto,
    style_constraints: StyleConstraints | None = None,
) -> StoreAssociateRecommendationResponse:
    """Main store-associate recommendation workflow.

    When an image is uploaded in chat, populate `style_constraints` from image
    cues (for example categories, gender cues, and style keywords).
    """
    return _associate_recommendation_impl(
        store_query=store_query,
        store_id=store_id,
        customer_email=customer_email,
        customer_id=customer_id,
        customer_phone_e164=customer_phone_e164,
        phone_last4=phone_last4,
        occasion=occasion,
        budget_min=budget_min,
        budget_max=budget_max,
        top_k=top_k,
        retrieval_mode=retrieval_mode,
        style_constraints=style_constraints,
    )


@mcp.tool(
    name="fashion_prepare_customer_sms",
    annotations=_tool_annotations(read_only=False, idempotent=False, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_prepare_customer_sms(
    store_query: str | None = None,
    store_id: str | None = None,
    customer_email: str | None = None,
    customer_id: str | None = None,
    customer_phone_e164: str | None = None,
    phone_last4: str | None = None,
    occasion: str | None = None,
    budget_min: float | None = None,
    budget_max: float | None = None,
    top_k: int = 5,
    retrieval_mode: RetrievalMode = RetrievalMode.auto,
    selected_product_ids: list[str] | None = None,
) -> CustomerCommunicationDraftResponse:
    """Create a persisted SMS draft from store-associate recommendations without sending it yet."""
    effective_retrieval_mode = _resolve_retrieval_mode(
        retrieval_mode,
        customer_resolved=bool(customer_email or customer_id or customer_phone_e164 or phone_last4),
        occasion=occasion,
        budget_min=budget_min,
        budget_max=budget_max,
    )
    with SessionLocal() as db:
        return prepare_customer_sms(
            db,
            store_query=store_query,
            store_id=store_id,
            customer_email=customer_email,
            customer_id=customer_id,
            customer_phone_e164=customer_phone_e164,
            phone_last4=phone_last4,
            occasion=occasion,
            budget_min=budget_min,
            budget_max=budget_max,
            top_k=top_k,
            retrieval_mode=effective_retrieval_mode,
            selected_product_ids=selected_product_ids,
        )


@mcp.tool(
    name="fashion_send_customer_sms",
    annotations=_tool_annotations(read_only=False, idempotent=False, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_send_customer_sms(message_id: str):
    """Send a previously prepared SMS draft through Twilio to the configured global test number."""
    with SessionLocal() as db:
        return send_customer_sms(db, message_id)


@mcp.tool(
    name="fashion_prepare_customer_email_draft",
    annotations=_tool_annotations(read_only=False, idempotent=False, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_prepare_customer_email_draft(
    message_id: str | None = None,
    store_query: str | None = None,
    store_id: str | None = None,
    customer_email: str | None = None,
    customer_id: str | None = None,
    customer_phone_e164: str | None = None,
    phone_last4: str | None = None,
    occasion: str | None = None,
    budget_min: float | None = None,
    budget_max: float | None = None,
    top_k: int = 6,
    retrieval_mode: RetrievalMode = RetrievalMode.auto,
    selected_product_ids: list[str] | None = None,
    to_email: str | None = None,
    subject: str | None = None,
    style_constraints: StyleConstraints | None = None,
) -> CustomerEmailDraftResponse:
    """Create or regenerate an email draft that can be reviewed in workspace or Canvas before send."""
    effective_retrieval_mode = _resolve_retrieval_mode(
        retrieval_mode,
        customer_resolved=bool(customer_email or customer_id or customer_phone_e164 or phone_last4),
        occasion=occasion,
        budget_min=budget_min,
        budget_max=budget_max,
    )
    with SessionLocal() as db:
        return prepare_customer_email_draft(
            db,
            message_id=message_id,
            store_query=store_query,
            store_id=store_id,
            customer_email=customer_email,
            customer_id=customer_id,
            customer_phone_e164=customer_phone_e164,
            phone_last4=phone_last4,
            occasion=occasion,
            budget_min=budget_min,
            budget_max=budget_max,
            top_k=top_k,
            retrieval_mode=effective_retrieval_mode,
            selected_product_ids=selected_product_ids,
            to_email=to_email,
            subject=subject,
            style_constraints=style_constraints,
        )


@mcp.tool(
    name="fashion_update_customer_email_draft",
    annotations=_tool_annotations(read_only=False, idempotent=False, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_update_customer_email_draft(
    message_id: str,
    subject: str | None = None,
    body_text: str | None = None,
    to_email: str | None = None,
    selected_product_ids: list[str] | None = None,
) -> CustomerEmailDraftResponse:
    """Update an email draft's subject/body/destination/products before send."""
    with SessionLocal() as db:
        return update_customer_email_draft(
            db,
            message_id=message_id,
            subject=subject,
            body_text=body_text,
            to_email=to_email,
            selected_product_ids=selected_product_ids,
        )


@mcp.tool(
    name="fashion_get_customer_email_draft",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_get_customer_email_draft(message_id: str) -> CustomerEmailDraftResponse:
    """Fetch the latest persisted state for an email draft."""
    with SessionLocal() as db:
        return get_customer_email_draft(db, message_id)


@mcp.tool(
    name="fashion_send_customer_email_draft",
    annotations=_tool_annotations(read_only=False, idempotent=False, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_send_customer_email_draft(message_id: str) -> CustomerEmailSendResponse:
    """Send a persisted email draft through Amazon SES."""
    with SessionLocal() as db:
        return send_customer_email_draft(db, message_id)


@mcp.tool(
    name="fashion_send_customer_recommendations_email",
    annotations=_tool_annotations(read_only=False, idempotent=False, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_send_customer_recommendations_email(
    store_query: str | None = None,
    store_id: str | None = None,
    customer_email: str | None = None,
    customer_id: str | None = None,
    customer_phone_e164: str | None = None,
    phone_last4: str | None = None,
    occasion: str | None = None,
    budget_min: float | None = None,
    budget_max: float | None = None,
    top_k: int = 6,
    retrieval_mode: RetrievalMode = RetrievalMode.auto,
    selected_product_ids: list[str] | None = None,
    to_email: str | None = None,
    subject: str | None = None,
) -> CustomerEmailSendResponse:
    """Send selected recommendation products to a customer via Amazon SES email."""
    effective_retrieval_mode = _resolve_retrieval_mode(
        retrieval_mode,
        customer_resolved=bool(customer_email or customer_id or customer_phone_e164 or phone_last4),
        occasion=occasion,
        budget_min=budget_min,
        budget_max=budget_max,
    )
    with SessionLocal() as db:
        return send_customer_recommendations_email(
            db,
            store_query=store_query,
            store_id=store_id,
            customer_email=customer_email,
            customer_id=customer_id,
            customer_phone_e164=customer_phone_e164,
            phone_last4=phone_last4,
            occasion=occasion,
            budget_min=budget_min,
            budget_max=budget_max,
            top_k=top_k,
            retrieval_mode=effective_retrieval_mode,
            selected_product_ids=selected_product_ids,
            to_email=to_email,
            subject=subject,
        )


@mcp.tool(
    name="fashion_update_customer_sms_draft",
    annotations=_tool_annotations(read_only=False, idempotent=False, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_update_customer_sms_draft(
    message_id: str,
    body_text: str,
    selected_product_ids: list[str] | None = None,
) -> CustomerCommunicationUpdateResponse:
    """Update a draft SMS body or selected products before sending."""
    with SessionLocal() as db:
        return update_customer_sms_draft(
            db,
            message_id=message_id,
            body_text=body_text,
            selected_product_ids=selected_product_ids,
        )


@mcp.tool(
    name="fashion_customer_message_history",
    annotations=_tool_annotations(read_only=True, idempotent=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_customer_message_history(
    customer_email: str | None = None,
    customer_id: str | None = None,
    phone_e164: str | None = None,
    phone_last4: str | None = None,
    limit: int = 20,
    status: str | None = None,
) -> CustomerCommunicationHistoryResponse:
    """Return recent SMS drafts and send results for a customer."""
    with SessionLocal() as db:
        status_enum = None
        if status:
            status_enum = CustomerCommunicationStatus(status)
        return customer_message_history(
            db,
            customer_email=customer_email,
            customer_id=customer_id,
            phone_e164=phone_e164,
            phone_last4=phone_last4,
            limit=limit,
            status=status_enum,
        )


@mcp.tool(
    name="fashion_twilio_smoke_test",
    annotations=_tool_annotations(read_only=False, idempotent=False, open_world=True),
)
def fashion_twilio_smoke_test(body_text: str | None = None) -> TwilioSmokeTestResponse:
    """Send a safe smoke-test SMS to the configured global test number."""
    with SessionLocal() as db:
        return twilio_smoke_test(db, body_text=body_text)


@mcp.tool(
    name="fashion_merch_action_recommendations",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_merch_action_recommendations(
    store_query: str | None = None,
    store_id: str | None = None,
    question: str | None = None,
    objective: Objective = Objective.sell_through,
    lookback_days: int = 90,
    top_k: int = 9,
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
    occasions: list[str] | None = None,
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
    compare_store_id: str | None = None,
) -> MerchActionRecommendationsResponse:
    """Recommend what a merchandiser should feature, deprioritize, or promote for a store."""
    with SessionLocal() as db:
        return merchandising_action_recommendations(
            db,
            store_query=store_query,
            store_id=store_id,
            question=question,
            objective=objective,
            lookback_days=lookback_days,
            top_k=top_k,
            category=category,
            brand=brand,
            price_band=price_band,
            occasion=occasion,
            occasions=occasions,
            compare_mode=compare_mode,
            peer_mode=peer_mode,
            compare_store_id=compare_store_id,
        )


@mcp.tool(
    name="fashion_merch_diagnostics",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_merch_diagnostics(
    store_query: str | None = None,
    store_id: str | None = None,
    question: str | None = None,
    lookback_days: int = 90,
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
    occasions: list[str] | None = None,
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
    compare_store_id: str | None = None,
) -> MerchDiagnosticsResponse:
    """Explain why a store, category, or brand is overperforming or underperforming versus peers."""
    with SessionLocal() as db:
        return merchandising_diagnostics(
            db,
            store_query=store_query,
            store_id=store_id,
            question=question,
            lookback_days=lookback_days,
            category=category,
            brand=brand,
            price_band=price_band,
            occasion=occasion,
            occasions=occasions,
            compare_mode=compare_mode,
            peer_mode=peer_mode,
            compare_store_id=compare_store_id,
        )


@mcp.tool(
    name="fashion_merch_trend_summary",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_merch_trend_summary(
    store_query: str | None = None,
    store_id: str | None = None,
    question: str | None = None,
    lookback_days: int = 90,
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
    occasions: list[str] | None = None,
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
    compare_store_id: str | None = None,
) -> MerchTrendSummaryResponse:
    """Summarize store-category revenue trends for a merchandiser over a selected time window."""
    with SessionLocal() as db:
        return merchandising_trend_summary(
            db,
            store_query=store_query,
            store_id=store_id,
            question=question,
            lookback_days=lookback_days,
            category=category,
            brand=brand,
            price_band=price_band,
            occasion=occasion,
            occasions=occasions,
            compare_mode=compare_mode,
            peer_mode=peer_mode,
            compare_store_id=compare_store_id,
        )


@mcp.tool(
    name="fashion_merch_inventory_view",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_merch_inventory_view(
    store_query: str | None = None,
    store_id: str | None = None,
    lookback_days: int = 90,
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
    occasions: list[str] | None = None,
    inventory_scope: InventoryScope = InventoryScope.combined,
    future_window_days: int = 120,
    limit: int = 200,
) -> MerchInventoryViewResponse:
    """Return inventory-first rows for merch workflows, including optional potential supplier offers."""
    params = MerchInventoryViewRequest(
        store_query=store_query,
        store_id=store_id,
        lookback_days=lookback_days,
        category=category,
        brand=brand,
        price_band=price_band,
        occasion=occasion,
        occasions=occasions or [],
        inventory_scope=inventory_scope,
        future_window_days=future_window_days,
        limit=limit,
    )
    return _merch_inventory_view_impl(params)


@mcp.tool(
    name="fashion_merch_product_mix_recommendations",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_merch_product_mix_recommendations(
    store_query: str | None = None,
    store_id: str | None = None,
    lookback_days: int = 90,
    top_k: int = 12,
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
    occasions: list[str] | None = None,
    inventory_scope: InventoryScope = InventoryScope.combined,
    future_window_days: int = 120,
    recommendation_overrides: list[MerchRecommendationOverride] | None = None,
) -> MerchProductMixRecommendationsResponse:
    """Generate deterministic product-mix add/hold/reduce/swap recommendations from active merch filters."""
    params = MerchProductMixRecommendationsRequest(
        store_query=store_query,
        store_id=store_id,
        lookback_days=lookback_days,
        top_k=top_k,
        category=category,
        brand=brand,
        price_band=price_band,
        occasion=occasion,
        occasions=occasions or [],
        inventory_scope=inventory_scope,
        future_window_days=future_window_days,
        recommendation_overrides=recommendation_overrides or [],
    )
    return _merch_mix_analysis_impl(params)


@mcp.tool(
    name="fashion_unified_overview",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_unified_overview(
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    active_store_id: str | None = None,
    lookback_days: int = 90,
    objective: Objective = Objective.revenue,
    top_k_stores: int = 12,
    category: str | None = None,
    brand: str | None = None,
    brands: list[str] | None = None,
    occasion: str | None = None,
    occasions: list[str] | None = None,
    price_band: PriceBand | None = None,
) -> UnifiedOverviewResponse:
    """Return unified executive overview for selected stores with shared filter envelope."""
    params = UnifiedOverviewRequest(
        store_query=store_query,
        store_id=store_id,
        store_ids=store_ids or [],
        active_store_id=active_store_id,
        lookback_days=lookback_days,
        category=category,
        brand=brand,
        brands=brands or [],
        occasion=occasion,
        occasions=occasions or [],
        price_band=price_band,
        objective=objective,
        top_k_stores=top_k_stores,
    )
    return _unified_overview_impl(params)


@mcp.tool(
    name="fashion_unified_inventory_view",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_unified_inventory_view(
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    active_store_id: str | None = None,
    lookback_days: int = 90,
    category: str | None = None,
    brand: str | None = None,
    brands: list[str] | None = None,
    occasion: str | None = None,
    occasions: list[str] | None = None,
    price_band: PriceBand | None = None,
    row_mode: UnifiedRowMode = UnifiedRowMode.store_product,
    inventory_scope: InventoryScope = InventoryScope.combined,
    future_window_days: int = 120,
    limit: int = 300,
) -> UnifiedInventoryViewResponse:
    """Return unified inventory view across one or many stores with row-mode toggle semantics."""
    params = UnifiedInventoryViewRequest(
        store_query=store_query,
        store_id=store_id,
        store_ids=store_ids or [],
        active_store_id=active_store_id,
        lookback_days=lookback_days,
        category=category,
        brand=brand,
        brands=brands or [],
        occasion=occasion,
        occasions=occasions or [],
        price_band=price_band,
        row_mode=row_mode,
        inventory_scope=inventory_scope,
        future_window_days=future_window_days,
        limit=limit,
    )
    return _unified_inventory_view_impl(params)


@mcp.tool(
    name="fashion_unified_action_recommendations",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_unified_action_recommendations(
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    active_store_id: str | None = None,
    question: str | None = None,
    objective: Objective = Objective.margin,
    lookback_days: int = 90,
    top_k: int = 12,
    category: str | None = None,
    brand: str | None = None,
    brands: list[str] | None = None,
    occasion: str | None = None,
    occasions: list[str] | None = None,
    price_band: PriceBand | None = None,
    row_mode: UnifiedRowMode = UnifiedRowMode.store_product,
    override_scope: OverrideScope = OverrideScope.store,
    recommendation_overrides: list[UnifiedRecommendationOverride] | None = None,
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
    compare_store_id: str | None = None,
) -> UnifiedActionRecommendationsResponse:
    """Return unified merchandising action recommendations with row-mode and override-scope semantics."""
    params = UnifiedActionRecommendationsRequest(
        store_query=store_query,
        store_id=store_id,
        store_ids=store_ids or [],
        active_store_id=active_store_id,
        question=question,
        objective=objective,
        lookback_days=lookback_days,
        top_k=top_k,
        category=category,
        brand=brand,
        brands=brands or [],
        occasion=occasion,
        occasions=occasions or [],
        price_band=price_band,
        row_mode=row_mode,
        override_scope=override_scope,
        recommendation_overrides=recommendation_overrides or [],
        compare_mode=compare_mode,
        peer_mode=peer_mode,
        compare_store_id=compare_store_id,
    )
    return _unified_action_recommendations_impl(params)


@mcp.tool(
    name="fashion_unified_product_mix_recommendations",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_unified_product_mix_recommendations(
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    active_store_id: str | None = None,
    lookback_days: int = 90,
    top_k: int = 12,
    category: str | None = None,
    brand: str | None = None,
    brands: list[str] | None = None,
    occasion: str | None = None,
    occasions: list[str] | None = None,
    price_band: PriceBand | None = None,
    row_mode: UnifiedRowMode = UnifiedRowMode.store_product,
    override_scope: OverrideScope = OverrideScope.store,
    inventory_scope: InventoryScope = InventoryScope.combined,
    future_window_days: int = 120,
    recommendation_overrides: list[UnifiedRecommendationOverride] | None = None,
) -> UnifiedProductMixRecommendationsResponse:
    """Return unified product-mix recommendations across selected stores with row-mode semantics."""
    params = UnifiedProductMixRecommendationsRequest(
        store_query=store_query,
        store_id=store_id,
        store_ids=store_ids or [],
        active_store_id=active_store_id,
        lookback_days=lookback_days,
        top_k=top_k,
        category=category,
        brand=brand,
        brands=brands or [],
        occasion=occasion,
        occasions=occasions or [],
        price_band=price_band,
        row_mode=row_mode,
        override_scope=override_scope,
        inventory_scope=inventory_scope,
        future_window_days=future_window_days,
        recommendation_overrides=recommendation_overrides or [],
    )
    return _unified_mix_recommendations_impl(params)


@mcp.tool(
    name="fashion_unified_export_csv",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_unified_export_csv(
    view: UnifiedWorkspaceView = UnifiedWorkspaceView.executive_overview,
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    active_store_id: str | None = None,
    question: str | None = None,
    objective: Objective = Objective.revenue,
    lookback_days: int = 90,
    top_k: int = 12,
    top_k_stores: int = 50,
    category: str | None = None,
    brand: str | None = None,
    brands: list[str] | None = None,
    occasion: str | None = None,
    occasions: list[str] | None = None,
    price_band: PriceBand | None = None,
    row_mode: UnifiedRowMode = UnifiedRowMode.store_product,
    override_scope: OverrideScope = OverrideScope.store,
    inventory_scope: InventoryScope = InventoryScope.combined,
    future_window_days: int = 120,
    limit: int = 300,
    recommendation_overrides: list[UnifiedRecommendationOverride] | None = None,
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
    compare_store_id: str | None = None,
) -> UnifiedExportCsvResponse:
    """Export unified workspace CSV where rows match active tab + filters + row_mode + override_scope."""
    params = UnifiedExportCsvRequest(
        view=view,
        store_query=store_query,
        store_id=store_id,
        store_ids=store_ids or [],
        active_store_id=active_store_id,
        question=question,
        objective=objective,
        lookback_days=lookback_days,
        top_k=top_k,
        top_k_stores=top_k_stores,
        category=category,
        brand=brand,
        brands=brands or [],
        occasion=occasion,
        occasions=occasions or [],
        price_band=price_band,
        row_mode=row_mode,
        override_scope=override_scope,
        inventory_scope=inventory_scope,
        future_window_days=future_window_days,
        limit=limit,
        recommendation_overrides=recommendation_overrides or [],
        compare_mode=compare_mode,
        peer_mode=peer_mode,
        compare_store_id=compare_store_id,
    )
    return _unified_export_csv_impl(params)


@mcp.tool(
    name="fashion_product_margin_sales_opportunities",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_product_margin_sales_opportunities(
    dimension: ProductPerformanceDimension = ProductPerformanceDimension.product,
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    lookback_days: int = 90,
    min_margin_rate: float = 0.50,
    min_revenue_drop_pct: float = 10.0,
    top_k: int = 15,
    category: str | None = None,
    brand: str | None = None,
) -> ProductPerformanceSummaryResponse:
    """Find high-margin products or brands with recent sales decline; defaults to enterprise scope when no store is provided."""
    params = ProductPerformanceSummaryRequest(
        dimension=dimension,
        store_query=store_query,
        store_id=store_id,
        store_ids=store_ids or [],
        lookback_days=lookback_days,
        min_margin_rate=min_margin_rate,
        min_revenue_drop_pct=min_revenue_drop_pct,
        top_k=top_k,
        category=category,
        brand=brand,
    )
    with SessionLocal() as db:
        return product_margin_sales_opportunities(
            db,
            dimension=params.dimension,
            store_query=params.store_query,
            store_id=params.store_id,
            store_ids=params.store_ids,
            lookback_days=params.lookback_days,
            min_margin_rate=params.min_margin_rate,
            min_revenue_drop_pct=params.min_revenue_drop_pct,
            top_k=params.top_k,
            category=params.category,
            brand=params.brand,
        )


@mcp.tool(
    name="fashion_exec_auto_optimize_strategy",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_exec_auto_optimize_strategy(
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    lookback_days: int = 90,
    objective: Objective = Objective.revenue,
    brands: list[str] | None = None,
    from_category: str | None = None,
    to_category: str | None = None,
    discount_min_pct: float = 0.0,
    discount_max_pct: float = 20.0,
    discount_step_pct: float = 5.0,
    shift_min_pct: float = 0.0,
    shift_max_pct: float = 20.0,
    shift_step_pct: float = 5.0,
    top_k_scenarios: int = 3,
    min_margin_rate: float = 0.40,
    max_discount_pct: float = 20.0,
) -> ExecutiveAutoOptimizeResponse:
    """Recommend top deterministic what-if scenarios under explicit guardrails; does not mutate data."""
    _ensure_feature_enabled(settings.exec_auto_optimize_enabled, "Executive auto-optimize")
    params = ExecutiveAutoOptimizeRequest(
        store_query=store_query,
        store_id=store_id,
        store_ids=store_ids or [],
        lookback_days=lookback_days,
        objective=objective,
        brands=brands or [],
        from_category=from_category,
        to_category=to_category,
        discount_min_pct=discount_min_pct,
        discount_max_pct=discount_max_pct,
        discount_step_pct=discount_step_pct,
        shift_min_pct=shift_min_pct,
        shift_max_pct=shift_max_pct,
        shift_step_pct=shift_step_pct,
        top_k_scenarios=top_k_scenarios,
        min_margin_rate=min_margin_rate,
        max_discount_pct=max_discount_pct,
    )
    with SessionLocal() as db:
        return auto_optimize_strategy(
            db,
            store_query=params.store_query,
            store_id=params.store_id,
            store_ids=params.store_ids,
            lookback_days=params.lookback_days,
            objective=params.objective,
            brands=params.brands,
            from_category=params.from_category,
            to_category=params.to_category,
            discount_min_pct=params.discount_min_pct,
            discount_max_pct=params.discount_max_pct,
            discount_step_pct=params.discount_step_pct,
            shift_min_pct=params.shift_min_pct,
            shift_max_pct=params.shift_max_pct,
            shift_step_pct=params.shift_step_pct,
            top_k_scenarios=params.top_k_scenarios,
            min_margin_rate=params.min_margin_rate,
            max_discount_pct=params.max_discount_pct,
        )


@mcp.tool(
    name="fashion_exec_publish_strategy_packet",
    annotations=_tool_annotations(read_only=False, idempotent=False, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_exec_publish_strategy_packet(
    scenario: ExecutiveAutoOptimizeScenario,
    objective: Objective = Objective.revenue,
    lookback_days: int = 90,
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    brands: list[str] | None = None,
    from_category: str | None = None,
    to_category: str | None = None,
    min_margin_rate: float = 0.40,
    max_discount_pct: float = 20.0,
    title: str | None = None,
    summary: str | None = None,
) -> ExecutiveStrategyPacketResponse:
    """Publish a strategy packet from an approved optimization scenario for downstream merch/associate workflows."""
    _ensure_feature_enabled(settings.strategy_packet_enabled, "Strategy packet")
    params = ExecutivePublishStrategyPacketRequest(
        scenario=scenario,
        objective=objective,
        lookback_days=lookback_days,
        store_query=store_query,
        store_id=store_id,
        store_ids=store_ids or [],
        brands=brands or [],
        from_category=from_category,
        to_category=to_category,
        min_margin_rate=min_margin_rate,
        max_discount_pct=max_discount_pct,
        title=title,
        summary=summary,
    )
    with SessionLocal() as db:
        return publish_strategy_packet(
            db,
            scenario=params.scenario,
            objective=params.objective,
            lookback_days=params.lookback_days,
            store_query=params.store_query,
            store_id=params.store_id,
            store_ids=params.store_ids,
            brands=params.brands,
            from_category=params.from_category,
            to_category=params.to_category,
            min_margin_rate=params.min_margin_rate,
            max_discount_pct=params.max_discount_pct,
            title=params.title,
            summary=params.summary,
        )


@mcp.tool(
    name="fashion_exec_get_strategy_packet",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_exec_get_strategy_packet(packet_id: str) -> ExecutiveStrategyPacketResponse:
    """Fetch an existing strategy packet by id."""
    _ensure_feature_enabled(settings.strategy_packet_enabled, "Strategy packet")
    with SessionLocal() as db:
        return get_strategy_packet(db, packet_id=packet_id)


@mcp.tool(
    name="fashion_exec_prepare_strategy_packet_email",
    annotations=_tool_annotations(read_only=False, idempotent=False, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_exec_prepare_strategy_packet_email(
    packet_id: str,
    to_email: str | None = None,
) -> ExecutiveStrategyPacketEmailDraftResponse:
    """Prepare an approval-gated strategy packet email draft for merchandising leadership."""
    _ensure_feature_enabled(settings.strategy_packet_enabled, "Strategy packet")
    destination = (to_email or "").strip().lower() or _DEFAULT_EXEC_TO_EMAIL
    with SessionLocal() as db:
        return prepare_strategy_packet_email(
            db,
            packet_id=packet_id,
            to_email=destination,
        )


@mcp.tool(
    name="fashion_exec_send_strategy_packet_email",
    annotations=_tool_annotations(read_only=False, idempotent=False, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_exec_send_strategy_packet_email(
    packet_id: str,
    approved: bool = False,
) -> ExecutiveStrategyPacketEmailSendResponse:
    """Send a prepared strategy packet email only with explicit approval."""
    _ensure_feature_enabled(settings.strategy_packet_enabled, "Strategy packet")
    with SessionLocal() as db:
        return send_strategy_packet_email(
            db,
            packet_id=packet_id,
            approved=approved,
        )


@mcp.tool(
    name="fashion_exec_export_csv",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_exec_export_csv(
    view: ExecutiveExportCsvView = ExecutiveExportCsvView.store_performance,
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    lookback_days: int = 90,
    objective: Objective = Objective.revenue,
    top_k_stores: int = 50,
) -> ExecutiveExportCsvResponse:
    """Export raw executive store performance CSV, including per-store category performance rows."""
    params = ExecutiveExportCsvRequest(
        view=view,
        store_query=store_query,
        store_id=store_id,
        store_ids=store_ids or [],
        lookback_days=lookback_days,
        objective=objective,
        top_k_stores=top_k_stores,
    )
    return _exec_export_csv_impl(params)


@mcp.tool(
    name="fashion_exec_overview",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_exec_overview(
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    lookback_days: int = 90,
    objective: Objective = Objective.revenue,
    top_k_stores: int = 12,
) -> ExecutiveOverviewResponse:
    """Return company-wide executive KPIs with store contribution and trend context."""
    with SessionLocal() as db:
        return executive_overview(
            db,
            store_query=store_query,
            store_id=store_id,
            store_ids=store_ids,
            lookback_days=lookback_days,
            objective=objective,
            top_k_stores=top_k_stores,
        )


@mcp.tool(
    name="fashion_exec_event_readiness_radar",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_exec_event_readiness_radar(
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    lookback_days: int = 56,
    events: list[str] | None = None,
    brands: list[str] | None = None,
) -> ExecutiveEventReadinessRadarResponse:
    """Return proactive risk flags for event demand readiness with transfer/promotion recommendations."""
    with SessionLocal() as db:
        return event_readiness_radar(
            db,
            store_query=store_query,
            store_id=store_id,
            store_ids=store_ids,
            lookback_days=lookback_days,
            events=events,
            brands=brands,
        )


@mcp.tool(
    name="fashion_exec_what_if_simulator",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_exec_what_if_simulator(
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    lookback_days: int = 90,
    discount_pct: float = 0.0,
    floor_space_shift_pct: float = 0.0,
    from_category: str | None = None,
    to_category: str | None = None,
    brands: list[str] | None = None,
) -> ExecutiveWhatIfSimulatorResponse:
    """Simulate discount and category-exposure shifts with expected revenue/margin impact."""
    with SessionLocal() as db:
        return what_if_simulator(
            db,
            store_query=store_query,
            store_id=store_id,
            store_ids=store_ids,
            lookback_days=lookback_days,
            discount_pct=discount_pct,
            floor_space_shift_pct=floor_space_shift_pct,
            from_category=from_category,
            to_category=to_category,
            brands=brands,
        )


@mcp.tool(
    name="fashion_exec_campaign_autopilot_prepare",
    annotations=_tool_annotations(read_only=False, idempotent=False, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_exec_campaign_autopilot_prepare(
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    to_email: str | None = None,
    lookback_days: int = 56,
    top_k: int = 6,
    events: list[str] | None = None,
    brands: list[str] | None = None,
    min_margin_rate: float = 0.40,
    max_discount_pct: float = 20.0,
) -> ExecutiveCampaignAutopilotDraftResponse:
    """Generate a guardrailed weekly campaign shortlist and draft comms package for approval."""
    with SessionLocal() as db:
        return campaign_autopilot_prepare(
            db,
            store_query=store_query,
            store_id=store_id,
            store_ids=store_ids,
            to_email=(to_email or "").strip().lower() or _DEFAULT_EXEC_TO_EMAIL,
            lookback_days=lookback_days,
            top_k=top_k,
            events=events,
            brands=brands,
            min_margin_rate=min_margin_rate,
            max_discount_pct=max_discount_pct,
        )


@mcp.tool(
    name="fashion_exec_campaign_autopilot_send",
    annotations=_tool_annotations(read_only=False, idempotent=False, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_exec_campaign_autopilot_send(
    draft_id: str,
    approved: bool = False,
) -> ExecutiveCampaignAutopilotSendResponse:
    """Send a prepared campaign package only when explicit approval is provided."""
    with SessionLocal() as db:
        return campaign_autopilot_send(db, draft_id=draft_id, approved=approved)


@mcp.tool(
    name="fashion_exec_get_campaign_autopilot_draft",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_exec_get_campaign_autopilot_draft(draft_id: str) -> ExecutiveCampaignAutopilotDraftResponse:
    """Fetch an existing campaign autopilot draft for workspace or chat review."""
    with SessionLocal() as db:
        return get_campaign_autopilot_draft(db, draft_id)


@mcp.tool(
    name="fashion_inventory_check_by_store",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_inventory_check_by_store(
    store_query: str | None = None,
    store_id: str | None = None,
    product_query: str | None = None,
    product_id: str | None = None,
    brand: str | None = None,
    category: str | None = None,
    size: str | None = None,
    limit: int = 100,
) -> InventoryCheckByStoreResponse:
    """Return inventory health by store, including in-stock, preorder, and out-of-stock breakdowns."""
    return _inventory_check_by_store_impl(
        store_query=store_query,
        store_id=store_id,
        product_query=product_query,
        product_id=product_id,
        brand=brand,
        category=category,
        size=size,
        limit=limit,
    )


@mcp.tool(
    name="fashion_inventory_products",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_inventory_products(
    store_query: str | None = None,
    store_id: str | None = None,
    product_query: str | None = None,
    product_id: str | None = None,
    brand: str | None = None,
    category: str | None = None,
    size: str | None = None,
    limit: int = 80,
) -> InventoryProductsResponse:
    """List raw inventory products with availability and quantity for operational review."""
    return _inventory_products_impl(
        store_query=store_query,
        store_id=store_id,
        product_query=product_query,
        product_id=product_id,
        brand=brand,
        category=category,
        size=size,
        limit=limit,
    )


@mcp.tool(
    name="fashion_inventory_by_store",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_inventory_by_store(
    product_query: str | None = None,
    product_id: str | None = None,
    brand: str | None = None,
    category: str | None = None,
    size: str | None = None,
    in_stock_only: bool = True,
    limit: int = 100,
) -> InventoryByStoreResponse:
    """Return on-hand inventory units and SKU counts per store for optional product filters."""
    effective_limit = max(1, min(limit, 500))
    with SessionLocal() as db:
        query = (
            select(
                Product.store_id.label("store_id"),
                func.sum(Product.inventory_qty).label("units_in_stock"),
                func.count(Product.id).label("sku_count"),
            )
            .group_by(Product.store_id)
            .order_by(func.sum(Product.inventory_qty).desc(), func.count(Product.id).desc(), Product.store_id)
        )
        query = _apply_inventory_filters(
            query,
            product_query=product_query,
            product_id=product_id,
            brand=brand,
            category=category,
            size=size,
            in_stock_only=in_stock_only,
        )
        grouped_rows = db.execute(query.limit(effective_limit)).all()
        store_ids = [row.store_id for row in grouped_rows if row.store_id]
        stores = db.scalars(select(Store).where(Store.id.in_(store_ids))).all() if store_ids else []
        store_map = {store.id: store for store in stores}

        rows: list[InventoryByStoreRow] = []
        total_units = 0
        total_skus = 0
        for row in grouped_rows:
            store = store_map.get(row.store_id)
            units = _int_value(row.units_in_stock)
            skus = _int_value(row.sku_count)
            total_units += units
            total_skus += skus
            rows.append(
                InventoryByStoreRow(
                    store_id=row.store_id,
                    store_name=store.name if store else row.store_id,
                    city=store.city if store else "",
                    state=store.state if store else "",
                    units_in_stock=units,
                    sku_count=skus,
                )
            )

        return InventoryByStoreResponse(
            product_query=product_query,
            product_id=product_id,
            brand=brand,
            category=category,
            size=size,
            rows=rows,
            total_units_in_stock=total_units,
            total_skus=total_skus,
        )


@mcp.tool(
    name="fashion_inventory_facets",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_inventory_facets(
    facet: InventoryFacet = InventoryFacet.brand,
    store_query: str | None = None,
    store_id: str | None = None,
    product_query: str | None = None,
    product_id: str | None = None,
    brand: str | None = None,
    category: str | None = None,
    size: str | None = None,
    in_stock_only: bool = True,
    limit: int = 25,
) -> InventoryFacetsResponse:
    """Return inventory units and SKU counts grouped by brand, category, or size."""
    effective_limit = max(1, min(limit, 200))
    with SessionLocal() as db:
        resolved_store = None
        if store_id or store_query:
            resolved_store = resolve_store(db, store_query=store_query, store_id=store_id).resolved

        if facet == InventoryFacet.category:
            raw_col = Product.category
        elif facet == InventoryFacet.size:
            raw_col = Product.size
        else:
            raw_col = Product.brand

        facet_col = func.coalesce(raw_col, "unknown")
        query = (
            select(
                facet_col.label("facet_value"),
                func.sum(Product.inventory_qty).label("units_in_stock"),
                func.count(Product.id).label("sku_count"),
            )
            .group_by(facet_col)
            .order_by(func.sum(Product.inventory_qty).desc(), func.count(Product.id).desc(), facet_col.asc())
        )
        query = _apply_inventory_filters(
            query,
            product_query=product_query,
            product_id=product_id,
            brand=brand,
            category=category,
            size=size,
            store_id=resolved_store.id if resolved_store else None,
            in_stock_only=in_stock_only,
        )
        grouped_rows = db.execute(query.limit(effective_limit)).all()

        rows: list[InventoryFacetRow] = []
        total_units = 0
        total_skus = 0
        for row in grouped_rows:
            units = _int_value(row.units_in_stock)
            skus = _int_value(row.sku_count)
            total_units += units
            total_skus += skus
            rows.append(
                InventoryFacetRow(
                    facet_value=str(row.facet_value or "unknown"),
                    units_in_stock=units,
                    sku_count=skus,
                )
            )

        return InventoryFacetsResponse(
            facet=facet,
            store=resolved_store,
            product_query=product_query,
            product_id=product_id,
            brand=brand,
            category=category,
            size=size,
            rows=rows,
            total_units_in_stock=total_units,
            total_skus=total_skus,
        )


@mcp.tool(
    name="fashion_merch_export_csv",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_merch_export_csv(
    view: MerchWorkspaceView = MerchWorkspaceView.actions,
    export_mode: MerchExportMode = MerchExportMode.legacy_combined,
    store_query: str | None = None,
    store_id: str | None = None,
    question: str | None = None,
    objective: Objective = Objective.margin,
    lookback_days: int = 90,
    top_k: int = 9,
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
    occasions: list[str] | None = None,
    inventory_scope: InventoryScope = InventoryScope.combined,
    future_window_days: int = 120,
    recommendation_overrides: list[MerchRecommendationOverride] | None = None,
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
    compare_store_id: str | None = None,
) -> MerchExportCsvResponse:
    """Export merch workspace results as deterministic CSV text for copy/paste into spreadsheets."""
    params = MerchExportCsvRequest(
        view=view,
        export_mode=export_mode,
        store_query=store_query,
        store_id=store_id,
        question=question,
        objective=objective,
        lookback_days=lookback_days,
        top_k=top_k,
        category=category,
        brand=brand,
        price_band=price_band,
        occasion=occasion,
        occasions=occasions or [],
        inventory_scope=inventory_scope,
        future_window_days=future_window_days,
        recommendation_overrides=recommendation_overrides or [],
        compare_mode=compare_mode,
        peer_mode=peer_mode,
        compare_store_id=compare_store_id,
    )
    return _merch_export_csv_impl(params)


@mcp.tool(name="fashion_get_product_feed", annotations=_tool_annotations(read_only=True, idempotent=True))
def fashion_get_product_feed(
    store_id: str | None = None,
    limit: int = 200,
) -> dict:
    """Return OpenAI-commerce-style product feed rows for all products or one store."""
    params = ProductFeedInput(store_id=store_id, limit=limit)
    with SessionLocal() as db:
        query = select(Product)
        if params.store_id:
            query = query.where(Product.store_id == params.store_id)

        products = db.scalars(query.limit(params.limit)).all()
        items = [
            {
                "id": product.id,
                "title": product.title,
                "description": product.description,
                "link": product.link,
                "image_link": product.image_link,
                "price": f"{product.price:.2f} USD",
                "availability": product.availability,
                "brand": product.brand,
                "category": product.category,
                "color": product.color,
                "size": product.size,
                "material": product.material,
                "gender": product.gender,
            }
            for product in products
        ]
        return {"count": len(items), "items": items}
