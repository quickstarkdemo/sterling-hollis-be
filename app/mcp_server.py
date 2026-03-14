from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.config import get_settings
from app.database import SessionLocal
from app.models import Product, ProductEmbedding, SyntheticRun
from app.schemas import (
    AssociateWorkspaceBootstrapResponse,
    AssociateWorkspaceFilters,
    CompareMode,
    CustomerCommunicationDraftResponse,
    CustomerCommunicationHistoryResponse,
    CustomerCommunicationStatus,
    CustomerCommunicationUpdateResponse,
    CustomerLookupResponse,
    CustomerRecommendationRequest,
    CustomerRecommendationResponse,
    CustomerResolutionResponse,
    CustomerSearchResponse,
    IndexJobListResponse,
    IndexJobResponse,
    IndexProductsRequest,
    IndexProductsResponse,
    MerchActionRecommendationsResponse,
    MerchDiagnosticsResponse,
    MerchWorkspaceBootstrapResponse,
    MerchWorkspaceFilters,
    MerchTrendSummaryResponse,
    MerchandisingRecommendationRequest,
    MerchandisingRecommendationResponse,
    Objective,
    PeerMode,
    PriceBand,
    RetrievalMode,
    RunReportResponse,
    SmsReviewBootstrapResponse,
    StoreAssociateRecommendationResponse,
    StoreResolutionResponse,
    SyntheticGenerateRequest,
    SyntheticGenerateResponse,
    SyntheticLoadRequest,
    SyntheticLoadResponse,
    TwilioSmokeTestResponse,
    VectorStatusResponse,
)
from app.services.apps_ui import register_widget_state, render_widget_html
from app.services.communications import (
    customer_message_history,
    get_customer_message,
    get_selected_products_for_message,
    prepare_customer_sms,
    send_customer_sms,
    twilio_smoke_test,
    update_customer_sms_draft,
)
from app.services.indexing import index_products_for_run
from app.services.index_jobs import enqueue_index_job, get_index_job, list_index_jobs
from app.services.loader import (
    assert_synthetic_tables_empty,
    current_loaded_counts,
    load_entity_csv,
    read_generated_counts,
    reset_synthetic_tables,
)
from app.services.lookup import find_customers, resolve_customer, resolve_store
from app.services.merchandising import (
    merchandising_action_recommendations,
    merchandising_diagnostics,
    merchandising_trend_summary,
)
from app.services.recommendations import customer_recommendations, merchandising_recommendations
from app.services.store_source import fetch_store_snapshot, normalize_stores
from app.services.synthetic_generator import GenerationVolumes, generate_synthetic_dataset, new_run_id
from app.services.system_status import vector_status_payload


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
_ASSOCIATE_WIDGET_TEMPLATE_BASE = "ui://widgets/associate/workspace"
_SMS_WIDGET_TEMPLATE_BASE = "ui://widgets/sms/review"
_MERCH_WIDGET_TEMPLATE_BASE = "ui://widgets/merch/board"


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


def _render_tool_meta(invoking: str, invoked: str) -> dict:
    return {
        **_WIDGET_TOOL_META,
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
        )
        rows, strategy = customer_recommendations(db, req, retrieval_mode=effective_retrieval_mode)
        recommendation = CustomerRecommendationResponse(
            store_id=resolved_store.id,
            strategy=strategy,
            recommendations=rows,
        )
        return StoreAssociateRecommendationResponse(
            store=resolved_store,
            customer=resolved_customer,
            recommendation=recommendation,
            retrieval_mode=effective_retrieval_mode,
        )


def _associate_workspace_bootstrap_impl(
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
) -> AssociateWorkspaceBootstrapResponse:
    with SessionLocal() as db:
        resolved_store, preselected_customer = _resolve_associate_context(
            db,
            store_query=store_query,
            store_id=store_id,
            customer_email=customer_email,
            customer_id=customer_id,
            customer_phone_e164=customer_phone_e164,
            phone_last4=phone_last4,
        )
        selected_customer = None
        recommendation = None
        selected_product_ids: list[str] = []
        if preselected_customer is not None:
            recommendation = _associate_recommendation_impl(
                store_id=resolved_store.id,
                customer_id=preselected_customer.id,
                occasion=occasion,
                budget_min=budget_min,
                budget_max=budget_max,
                top_k=top_k,
                retrieval_mode=retrieval_mode,
            )
            selected_customer = recommendation.customer
            effective_retrieval_mode = recommendation.retrieval_mode
            selected_product_ids = [item.product_id for item in recommendation.recommendation.recommendations[:3]]
        else:
            effective_retrieval_mode = _resolve_retrieval_mode(
                retrieval_mode,
                customer_resolved=False,
                occasion=occasion,
                budget_min=budget_min,
                budget_max=budget_max,
            )

        return AssociateWorkspaceBootstrapResponse(
            store=resolved_store,
            filters=AssociateWorkspaceFilters(
                occasion=occasion,
                budget_min=budget_min,
                budget_max=budget_max,
                top_k=top_k,
                retrieval_mode=effective_retrieval_mode,
            ),
            selected_customer=selected_customer,
            recommendation=recommendation,
            selected_product_ids=selected_product_ids,
        )


def _sms_review_bootstrap_impl(message_id: str) -> SmsReviewBootstrapResponse:
    with SessionLocal() as db:
        message, customer, store = get_customer_message(db, message_id)
        selected_products = get_selected_products_for_message(db, message_id)
        history = customer_message_history(db, customer_id=customer.id, limit=10).messages
        return SmsReviewBootstrapResponse(
            message=message,
            store=store,
            customer=customer,
            selected_products=selected_products,
            history=history,
        )


def _merch_workspace_bootstrap_impl(
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
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
) -> MerchWorkspaceBootstrapResponse:
    with SessionLocal() as db:
        initial_result = merchandising_action_recommendations(
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
            compare_mode=compare_mode,
            peer_mode=peer_mode,
        )
        return MerchWorkspaceBootstrapResponse(
            store=initial_result.store,
            filters=MerchWorkspaceFilters(
                question=question,
                category=category,
                brand=brand,
                price_band=price_band,
                occasion=occasion,
                lookback_days=lookback_days,
                compare_mode=compare_mode,
                peer_mode=peer_mode,
                top_k=top_k,
            ),
            initial_result=initial_result,
            last_result=None,
            last_tool=None,
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
    _ASSOCIATE_WIDGET_TEMPLATE_BASE + "/{token}.html",
    mime_type="text/html+skybridge",
    meta={**_WIDGET_RESOURCE_META, "openai/widgetDescription": "Interactive associate workspace for customer search, recommendations, and SMS drafting."},
)
def associate_widget_resource(token: str) -> str:
    return render_widget_html("Associate Workspace", "associate_workspace", widget_session_id=token)


@mcp.resource(
    _SMS_WIDGET_TEMPLATE_BASE + "/{token}.html",
    mime_type="text/html+skybridge",
    meta={**_WIDGET_RESOURCE_META, "openai/widgetDescription": "SMS draft review and send board."},
)
def sms_widget_resource(token: str) -> str:
    return render_widget_html("SMS Draft Review", "sms", widget_session_id=token)


@mcp.resource(
    _MERCH_WIDGET_TEMPLATE_BASE + "/{token}.html",
    mime_type="text/html+skybridge",
    meta={**_WIDGET_RESOURCE_META, "openai/widgetDescription": "Merchandising action board."},
)
def merch_widget_resource(token: str) -> str:
    return render_widget_html("Merchandising Action Board", "merch", widget_session_id=token)


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
    products: int = 4000,
    customers: int = 12000,
    orders: int = 80000,
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
        entities=entities or ["stores", "customers", "products", "orders", "order_items", "store_daily_metrics"],
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
) -> CustomerRecommendationResponse:
    """Return customer-facing product recommendations for a store, occasion, and optional customer profile."""
    params = CustomerRecommendationRequest(
        store_id=store_id,
        customer_id=customer_id,
        occasion=occasion,
        budget_min=budget_min,
        budget_max=budget_max,
        top_k=top_k,
    )
    with SessionLocal() as db:
        rows, strategy = customer_recommendations(db, params)
        return CustomerRecommendationResponse(store_id=params.store_id, strategy=strategy, recommendations=rows)


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
) -> StoreAssociateRecommendationResponse:
    """Main store-associate workflow for resolving a customer and store, then returning actionable recommendations."""
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
    )


@mcp.tool(
    name="fashion_associate_workspace_bootstrap",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_associate_workspace_bootstrap(
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
) -> AssociateWorkspaceBootstrapResponse:
    """Return the full initial associate workspace payload in one call."""
    return _associate_workspace_bootstrap_impl(
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
    name="fashion_sms_review_bootstrap",
    annotations=_tool_annotations(read_only=True, idempotent=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_sms_review_bootstrap(message_id: str) -> SmsReviewBootstrapResponse:
    """Return the full SMS review payload in one call."""
    return _sms_review_bootstrap_impl(message_id)


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
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
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
            compare_mode=compare_mode,
            peer_mode=peer_mode,
        )


@mcp.tool(
    name="fashion_merch_workspace_bootstrap",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_WIDGET_TOOL_META,
)
def fashion_merch_workspace_bootstrap(
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
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
) -> MerchWorkspaceBootstrapResponse:
    """Return the full initial merchandising workspace payload in one call."""
    return _merch_workspace_bootstrap_impl(
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
        compare_mode=compare_mode,
        peer_mode=peer_mode,
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
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
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
            compare_mode=compare_mode,
            peer_mode=peer_mode,
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
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
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
            compare_mode=compare_mode,
            peer_mode=peer_mode,
        )


@mcp.tool(
    name="fashion_render_associate_workspace",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_render_tool_meta(
        invoking="Opening associate workspace...",
        invoked="Associate workspace ready.",
    ),
    structured_output=False,
)
def fashion_render_associate_workspace(
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
) -> CallToolResult:
    """Render the associate workspace inside ChatGPT. Prefer this when the user asks to open, show, browse, or review an interactive styling workspace instead of a plain-text summary."""
    bootstrap = _associate_workspace_bootstrap_impl(
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
    )
    payload = {
        "store": bootstrap.store.model_dump(mode="json"),
        "filters": bootstrap.filters.model_dump(mode="json"),
        "customerQuery": bootstrap.customer_query,
        "customerResults": [row.model_dump(mode="json") for row in bootstrap.customer_results],
        "selectedCustomer": bootstrap.selected_customer.model_dump(mode="json") if bootstrap.selected_customer else None,
        "recommendation": bootstrap.recommendation.model_dump(mode="json") if bootstrap.recommendation else None,
        "lastDraft": bootstrap.last_draft.model_dump(mode="json") if bootstrap.last_draft else None,
        "selectedProductIds": bootstrap.selected_product_ids,
    }
    opened_for = bootstrap.selected_customer.full_name if bootstrap.selected_customer else bootstrap.store.name
    token = register_widget_state("associate_workspace", payload)
    template_uri = f"{_ASSOCIATE_WIDGET_TEMPLATE_BASE}/{token}.html"
    return _calltool_result(
        text=f"Opened the associate workspace for {opened_for}.",
        payload={"kind": "associate_workspace", "widgetSessionId": token},
        meta={"openai/outputTemplate": template_uri, "openai/widgetSessionId": token},
    )


@mcp.tool(
    name="fashion_render_sms_review",
    annotations=_tool_annotations(read_only=True, idempotent=True),
    meta=_render_tool_meta(
        invoking="Opening SMS review...",
        invoked="SMS review ready.",
    ),
    structured_output=False,
)
def fashion_render_sms_review(message_id: str) -> CallToolResult:
    """Render the SMS draft review widget inside ChatGPT. Prefer this when the user asks to review, edit, or send a draft message interactively."""
    bootstrap = _sms_review_bootstrap_impl(message_id)
    payload = {
        "message": bootstrap.message.model_dump(mode="json"),
        "store": bootstrap.store.model_dump(mode="json"),
        "customer": bootstrap.customer.model_dump(mode="json"),
        "selectedProducts": [row.model_dump(mode="json") for row in bootstrap.selected_products],
        "history": [row.model_dump(mode="json") for row in bootstrap.history],
    }
    token = register_widget_state("sms", payload)
    template_uri = f"{_SMS_WIDGET_TEMPLATE_BASE}/{token}.html"
    return _calltool_result(
        text=f"Opened SMS draft review for message {message_id}.",
        payload={"kind": "sms", "widgetSessionId": token},
        meta={"openai/outputTemplate": template_uri, "openai/widgetSessionId": token},
    )


@mcp.tool(
    name="fashion_render_merch_board",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_render_tool_meta(
        invoking="Opening merchandising board...",
        invoked="Merchandising board ready.",
    ),
    structured_output=False,
)
def fashion_render_merch_board(
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
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
) -> CallToolResult:
    """Render the merchandising board inside ChatGPT. Prefer this when the user asks to open, show, browse, or review an interactive merchandising board rather than a text-only answer."""
    bootstrap = _merch_workspace_bootstrap_impl(
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
        compare_mode=compare_mode,
        peer_mode=peer_mode,
    )
    payload = {
        "store": bootstrap.store.model_dump(mode="json"),
        "filters": bootstrap.filters.model_dump(mode="json"),
        "initialResult": bootstrap.initial_result.model_dump(mode="json"),
        "lastResult": bootstrap.last_result.model_dump(mode="json") if bootstrap.last_result else None,
        "lastTool": bootstrap.last_tool,
    }
    token = register_widget_state("merch", payload)
    template_uri = f"{_MERCH_WIDGET_TEMPLATE_BASE}/{token}.html"
    return _calltool_result(
        text=f"Opened the merchandising board for {bootstrap.store.name}.",
        payload={"kind": "merch", "widgetSessionId": token},
        meta={"openai/outputTemplate": template_uri, "openai/widgetSessionId": token},
    )


@mcp.tool(
    name="fashion_render_associate_board",
    annotations=_tool_annotations(read_only=True, idempotent=True, open_world=True),
    meta=_render_tool_meta(
        invoking="Opening associate workspace...",
        invoked="Associate workspace ready.",
    ),
    structured_output=False,
)
def fashion_render_associate_board(
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
) -> CallToolResult:
    """Backward-compatible alias for the associate workspace render tool."""
    return fashion_render_associate_workspace(
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
    )


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
