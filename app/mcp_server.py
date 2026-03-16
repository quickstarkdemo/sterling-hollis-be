from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.config import get_settings
from app.database import SessionLocal
from app.models import Product, ProductEmbedding, SyntheticRun
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
    IndexJobListResponse,
    IndexJobResponse,
    IndexProductsRequest,
    IndexProductsResponse,
    MerchActionRecommendationsResponse,
    MerchDiagnosticsResponse,
    MerchTrendSummaryResponse,
    MerchandisingRecommendationRequest,
    MerchandisingRecommendationResponse,
    Objective,
    PeerMode,
    PriceBand,
    RetrievalMode,
    RunReportResponse,
    StoreAssociateRecommendationResponse,
    StoreResolutionResponse,
    StyleConstraints,
    SyntheticGenerateRequest,
    SyntheticGenerateResponse,
    SyntheticLoadRequest,
    SyntheticLoadResponse,
    TwilioSmokeTestResponse,
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
_CUSTOMER_SEARCH_WIDGET_TEMPLATE_BASE = "ui://widgets/customer-search/workspace"
_WIDGET_BUILD_TAG = re.sub(r"[^A-Za-z0-9._-]", "-", settings.app_build_version or "dev")
_CUSTOMER_SEARCH_WIDGET_RESOURCE_URI = f"{_CUSTOMER_SEARCH_WIDGET_TEMPLATE_BASE}-{_WIDGET_BUILD_TAG}.html"


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
        recommendation = CustomerRecommendationResponse(
            store_id=resolved_store.id,
            strategy=strategy,
            recommendations=rows,
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
        return CustomerRecommendationResponse(
            store_id=params.store_id,
            strategy=strategy,
            recommendations=rows,
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


def _customer_search_workspace_payload(
    query: str | None,
    limit: int,
    selected_customer_id: str | None = None,
    initial_style_constraints: StyleConstraints | None = None,
    initial_notice: str | None = None,
    initial_email_draft_id: str | None = None,
    initial_email_subject: str | None = None,
    initial_email_body: str | None = None,
) -> dict:
    normalized_query = (query or "").strip()
    initial_constraints_payload = (
        initial_style_constraints.model_dump(mode="json") if initial_style_constraints is not None else None
    )
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
        "uiHints": {
            "searchPlaceholder": "Search by name, email, or phone",
            "emptyState": "No customers matched the current query.",
        },
    }


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
) -> CallToolResult:
    """Render the customer workspace inside ChatGPT.

    Optional hydration fields (`selected_customer_id`, `initial_style_constraints`,
    `initial_notice`, and initial email draft fields) can seed the UI state from
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
    limit: int = 10,
) -> CallToolResult:
    """Resolve customer query and open a hydrated customer workspace in one call.

    Prefer this as the default workspace entrypoint for chat flows. When an image
    is uploaded in chat, extract cues and pass them as `style_constraints`.
    For chat-first email flows, pass `initial_email_draft_id` (and optional
    `initial_email_subject`/`initial_email_body`) to hydrate draft context.
    """
    normalized_query = customer_query.strip()
    if not normalized_query:
        raise ValueError("customer_query is required.")

    effective_limit = max(1, min(limit, 25))
    lookup = fashion_lookup_customer(normalized_query, limit=effective_limit)

    selected_customer_id: str | None = None
    if lookup.mode == "resolved" and lookup.resolved is not None:
        selected_customer_id = lookup.resolved.id
    elif len(lookup.candidates) == 1:
        selected_customer_id = lookup.candidates[0].id

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
