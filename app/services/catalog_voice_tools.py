from __future__ import annotations

import hashlib
import time
from typing import Any, Literal

from fastapi import HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.catalog.admin_schemas import (
    CatalogSuggestionSetResponse,
    ProductDraftV3,
    product_draft_v3_from_snapshot,
)
from app.catalog.ai_schemas import CatalogAISuggestionCommandRequest
from app.catalog.workflow_schemas import WorkflowEventInput
from app.config import Settings
from app.models import (
    CatalogDraftRevision,
    CatalogProduct,
    CatalogWorkflow,
    CatalogWorkflowEvent,
    Product,
    ProductInventory,
    Store,
)
from app.services.auth.clerk import AuthenticatedPrincipal
from app.services.catalog_admin import draft_revision_version, get_product_readiness_v3
from app.services.catalog_ai import CatalogAISuggestionService
from app.services.catalog_realtime import CatalogRealtimeV3ToolCallRequest
from app.services.catalog_workflow import append_workflow_event
from app.services.lookup import resolve_store


LOW_STOCK_THRESHOLD = 5
INVENTORY_QUESTION_TOKENS = (
    "available",
    "availability",
    "candidate",
    "candidates",
    "discount",
    "inventory",
    "item",
    "items",
    "low",
    "markdown",
    "merchandise",
    "overstock",
    "product",
    "products",
    "promote",
    "recommend",
    "recommendation",
    "risk",
    "sale",
    "sku",
    "skus",
    "status",
    "stock",
    "store",
    "stores",
    "unit",
    "units",
)


class CatalogVoiceCitation(BaseModel):
    kind: Literal["product", "catalog", "inventory", "readiness"]
    source_id: str
    label: str
    value: Any


class CatalogVoiceToolResult(BaseModel):
    status: Literal["succeeded", "blocked"] = "succeeded"
    message: str
    mutation: Literal[False] = False
    citations: list[CatalogVoiceCitation] = Field(default_factory=list)
    suggestion_set: CatalogSuggestionSetResponse | None = None


def _owned_workflow(
    db: Session,
    *,
    workflow_id: str,
    principal: AuthenticatedPrincipal,
) -> CatalogWorkflow:
    workflow = db.scalar(
        select(CatalogWorkflow).where(
            CatalogWorkflow.id == workflow_id,
            CatalogWorkflow.owner_provider == principal.provider,
            CatalogWorkflow.owner_provider_user_id == principal.provider_user_id,
        )
    )
    if workflow is None:
        raise HTTPException(status_code=404, detail="Catalog Studio catalog workflow not found.")
    return workflow


def _latest_v3_session_context(
    db: Session,
    *,
    workflow_id: str,
    session_id: str,
    principal: AuthenticatedPrincipal,
) -> tuple[dict[str, Any], list[str]]:
    latest = db.scalar(
        select(CatalogWorkflowEvent)
        .join(CatalogWorkflow, CatalogWorkflow.id == CatalogWorkflowEvent.workflow_id)
        .where(
            CatalogWorkflow.owner_provider == principal.provider,
            CatalogWorkflow.owner_provider_user_id == principal.provider_user_id,
            CatalogWorkflowEvent.stage == "voice",
            CatalogWorkflowEvent.capability == "realtime",
            CatalogWorkflowEvent.request_json["input"]["action"].as_string()
            == "create_realtime_session",
        )
        .order_by(
            CatalogWorkflowEvent.created_at.desc(),
            CatalogWorkflowEvent.id.desc(),
        )
        .limit(1)
    )
    if (
        latest is None
        or latest.status != "succeeded"
        or latest.workflow_id != workflow_id
        or (latest.response_json or {}).get("session_id") != session_id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This Realtime session is no longer active for the product workbench.",
        )
    expires_at = int((latest.response_json or {}).get("expires_at") or 0)
    if expires_at <= int(time.time()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This Realtime session has expired.",
        )
    context = dict((latest.response_json or {}).get("context") or {})
    tool_names = list((latest.response_json or {}).get("tool_names") or [])
    return context, tool_names


def _validated_context_draft(
    db: Session,
    *,
    workflow: CatalogWorkflow,
    context: dict[str, Any],
    principal: AuthenticatedPrincipal,
) -> tuple[CatalogDraftRevision, ProductDraftV3]:
    draft_id = str(context.get("draft_id") or "")
    product_id = str(context.get("product_id") or "")
    revision = db.get(CatalogDraftRevision, draft_id)
    if (
        revision is None
        or revision.catalog_product_id != product_id
        or revision.created_by != principal.provider_user_id
        or workflow.draft_revision_id != revision.id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The Realtime product context is no longer current.",
        )
    actual_version = draft_revision_version(db, revision)
    if actual_version != int(context.get("expected_draft_version") or 0):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The Realtime draft version is no longer current.",
        )
    return revision, product_draft_v3_from_snapshot(revision.snapshot_json)


def _product_summary(product: ProductDraftV3) -> tuple[str, list[CatalogVoiceCitation]]:
    citations = [
        CatalogVoiceCitation(
            kind="product",
            source_id=product.product_id or "current-draft",
            label="Active product",
            value={
                "title": product.title,
                "brand": product.brand,
                "category": product.category,
                "color": product.color,
                "material": product.material,
            },
        )
    ]
    return (
        f"{product.title} is a {product.brand} {product.category.replace('_', ' ')} product.",
        citations,
    )


def _catalog_summary(db: Session) -> tuple[str, list[CatalogVoiceCitation]]:
    published_count = int(
        db.scalar(
            select(func.count(CatalogProduct.id)).where(
                CatalogProduct.lifecycle_status == "published"
            )
        )
        or 0
    )
    draft_product_count = int(
        db.scalar(select(func.count(func.distinct(CatalogDraftRevision.catalog_product_id))))
        or 0
    )
    citation = CatalogVoiceCitation(
        kind="catalog",
        source_id="catalog",
        label="Authorized catalog summary",
        value={
            "published_count": published_count,
            "draft_product_count": draft_product_count,
        },
    )
    return (
        f"The catalog contains {published_count} published and {draft_product_count} draft product record(s).",
        [citation],
    )


def _inventory_summary(
    db: Session, product: ProductDraftV3 | None, question: str | None = None
) -> tuple[str, list[CatalogVoiceCitation]]:
    if product is None:
        return _catalog_inventory_summary(db, question=question)
    store_ids = {row.store_id for row in product.inventory}
    stores = {
        row.id: row
        for row in db.scalars(select(Store).where(Store.id.in_(store_ids))).all()
    }
    citations = [
        CatalogVoiceCitation(
            kind="inventory",
            source_id=row.store_id,
            label=stores[row.store_id].name if row.store_id in stores else row.store_id,
            value={
                "size": row.size,
                "availability": row.availability,
                "inventory_qty": row.inventory_qty,
            },
        )
        for row in product.inventory
    ]
    low = [
        item
        for item in citations
        if item.value["availability"] == "low stock" or item.value["inventory_qty"] <= 5
    ]
    if not low:
        return "No store is currently marked low stock for this product.", citations
    names = ", ".join(item.label for item in low)
    return f"Low stock is reported at {names}.", citations


def _norm(value: str | None) -> str:
    return " ".join(str(value or "").casefold().replace("-", " ").split())


def _question_mentions_inventory(question: str) -> bool:
    normalized = _norm(question)
    return any(token in normalized for token in INVENTORY_QUESTION_TOKENS)


def _question_requests_product_detail(question: str | None) -> bool:
    normalized = _norm(question)
    detail_tokens = (
        "candidate",
        "candidates",
        "discount",
        "item",
        "items",
        "markdown",
        "overstock",
        "product",
        "products",
        "promote",
        "recommend",
        "recommendation",
        "sale",
        "sku",
        "skus",
    )
    return any(token in normalized for token in detail_tokens)


def _mentioned_store_query(db: Session, question: str) -> str | None:
    normalized = _norm(question)
    if not normalized:
        return None
    stores = db.scalars(select(Store).order_by(Store.name.asc(), Store.id.asc())).all()
    matches: list[tuple[int, str]] = []
    for store in stores:
        candidates = [
            store.id,
            store.name,
            store.city,
            f"{store.city} {store.state}",
            f"{store.name} {store.state}",
        ]
        for candidate in candidates:
            candidate_norm = _norm(candidate)
            if candidate_norm and candidate_norm in normalized:
                matches.append((len(candidate_norm), str(candidate)))
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]


def _store_inventory_citation(row: dict[str, Any]) -> CatalogVoiceCitation:
    return CatalogVoiceCitation(
        kind="inventory",
        source_id=row["store_id"],
        label=row["store_name"],
        value={
            "store_id": row["store_id"],
            "store_name": row["store_name"],
            "city": row["city"],
            "state": row["state"],
            "sku_count": row["sku_count"],
            "low_stock_skus": row["low_stock_skus"],
            "out_of_stock_skus": row["out_of_stock_skus"],
            "preorder_skus": row["preorder_skus"],
            "inventory_qty": row["units_in_stock"],
            "units_in_stock": row["units_in_stock"],
        },
    )


def _store_status_message(row: dict[str, Any]) -> str:
    return (
        f"{row['store_name']} has {row['low_stock_skus']}/{row['sku_count']} SKU(s) low or out of stock, "
        f"{row['units_in_stock']} unit(s) in stock, and {row['preorder_skus']} preorder SKU(s)."
    )


def _product_inventory_citation(product: Product, store: Store) -> CatalogVoiceCitation:
    margin_pct = float(product.margin_pct or 0.0)
    return CatalogVoiceCitation(
        kind="inventory",
        source_id=product.id,
        label=f"{store.name}: {product.title}",
        value={
            "product_id": product.id,
            "title": product.title,
            "store_id": store.id,
            "store_name": store.name,
            "brand": product.brand,
            "category": product.category,
            "size": product.size,
            "price": float(product.price) if product.price is not None else None,
            "availability": product.availability,
            "inventory_qty": int(product.inventory_qty or 0),
            "margin_pct": margin_pct,
            "objective_weight": float(product.objective_weight or 0.0),
            "link": product.link,
            "image_url": product.image_link,
        },
    )


def _store_product_inventory_detail(
    db: Session,
    *,
    store_id: str | None,
    limit: int = 6,
) -> list[CatalogVoiceCitation]:
    query = select(Product, Store).join(Store, Store.id == Product.store_id)
    if store_id:
        query = query.where(Product.store_id == store_id)
    products = db.execute(
        query.order_by(
            Product.inventory_qty.desc(),
            Product.margin_pct.desc(),
            Product.objective_weight.desc(),
            Product.title.asc(),
        ).limit(limit)
    ).all()
    return [_product_inventory_citation(product, store) for product, store in products]


def _product_detail_message(
    *,
    store_name: str | None,
    citations: list[CatalogVoiceCitation],
    question: str | None,
) -> str:
    if not citations:
        target = f" for {store_name}" if store_name else ""
        return f"No product-level inventory rows were found{target}."
    lead = citations[0].value
    price = lead.get("price")
    price_text = f"${price:.2f}" if isinstance(price, (int, float)) else "unpriced"
    margin_pct = float(lead.get("margin_pct") or 0.0) * 100.0
    discount_context = (
        " A targeted 10% to 15% discount offer is a reasonable starting point for review because it has inventory to work through and cited margin headroom."
        if "discount" in _norm(question) or "markdown" in _norm(question) or "sale" in _norm(question)
        else ""
    )
    suffix = " Additional product-level candidates are cited." if len(citations) > 1 else ""
    return (
        f"{lead['store_name']}: the strongest product-level candidate is {lead['title']} "
        f"({lead['brand']}, {lead['category']}) with {lead['inventory_qty']} unit(s), "
        f"{margin_pct:.1f}% margin, {lead['availability']}, and price {price_text}."
        f"{discount_context}{suffix}"
    )


def _stock_aggregate_expressions(quantity_col: Any, availability_col: Any) -> tuple[Any, Any, Any, Any]:
    availability = func.lower(func.coalesce(availability_col, ""))
    low_stock_skus = func.sum(
        case(
            (
                or_(
                    quantity_col <= LOW_STOCK_THRESHOLD,
                    availability.in_(("low stock", "out of stock")),
                ),
                1,
            ),
            else_=0,
        )
    )
    out_of_stock_skus = func.sum(
        case((or_(quantity_col <= 0, availability == "out of stock"), 1), else_=0)
    )
    preorder_skus = func.sum(case((availability == "preorder", 1), else_=0))
    units_in_stock = func.sum(
        case(
            (
                or_(availability == "in stock", availability == "low stock"),
                quantity_col,
            ),
            else_=0,
        )
    )
    return low_stock_skus, out_of_stock_skus, preorder_skus, units_in_stock


def _store_inventory_row_payload(row: Any) -> dict[str, Any]:
    return {
        "store_id": row.store_id,
        "store_name": row.store_name,
        "city": row.city,
        "state": row.state,
        "sku_count": int(row.sku_count or 0),
        "low_stock_skus": int(row.low_stock_skus or 0),
        "out_of_stock_skus": int(row.out_of_stock_skus or 0),
        "preorder_skus": int(row.preorder_skus or 0),
        "units_in_stock": int(row.units_in_stock or 0),
    }


def _public_store_inventory_rows(
    db: Session,
    *,
    store_id: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    low_stock_skus, out_of_stock_skus, preorder_skus, units_in_stock = (
        _stock_aggregate_expressions(Product.inventory_qty, Product.availability)
    )
    query = (
        select(
            Store.id.label("store_id"),
            Store.name.label("store_name"),
            Store.city.label("city"),
            Store.state.label("state"),
            func.count(Product.id).label("sku_count"),
            low_stock_skus.label("low_stock_skus"),
            out_of_stock_skus.label("out_of_stock_skus"),
            preorder_skus.label("preorder_skus"),
            units_in_stock.label("units_in_stock"),
        )
        .join(Store, Store.id == Product.store_id)
        .group_by(Store.id, Store.name, Store.city, Store.state)
    )
    if store_id:
        query = query.where(Product.store_id == store_id)
    query = query.order_by(
        low_stock_skus.desc(),
        out_of_stock_skus.desc(),
        func.count(Product.id).desc(),
        Store.name.asc(),
    )
    return [_store_inventory_row_payload(row) for row in db.execute(query.limit(limit)).all()]


def _canonical_store_inventory_rows(
    db: Session,
    *,
    store_id: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    low_stock_skus, out_of_stock_skus, preorder_skus, units_in_stock = (
        _stock_aggregate_expressions(
            ProductInventory.inventory_qty,
            ProductInventory.availability,
        )
    )
    query = (
        select(
            Store.id.label("store_id"),
            Store.name.label("store_name"),
            Store.city.label("city"),
            Store.state.label("state"),
            func.count(ProductInventory.id).label("sku_count"),
            low_stock_skus.label("low_stock_skus"),
            out_of_stock_skus.label("out_of_stock_skus"),
            preorder_skus.label("preorder_skus"),
            units_in_stock.label("units_in_stock"),
        )
        .join(CatalogProduct, CatalogProduct.id == ProductInventory.catalog_product_id)
        .join(Store, Store.id == ProductInventory.store_id)
        .where(CatalogProduct.lifecycle_status == "published")
        .group_by(Store.id, Store.name, Store.city, Store.state)
    )
    if store_id:
        query = query.where(ProductInventory.store_id == store_id)
    query = query.order_by(
        low_stock_skus.desc(),
        out_of_stock_skus.desc(),
        func.count(ProductInventory.id).desc(),
        Store.name.asc(),
    )
    return [_store_inventory_row_payload(row) for row in db.execute(query.limit(limit)).all()]


def _store_inventory_rows(
    db: Session,
    *,
    store_id: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    rows = _public_store_inventory_rows(db, store_id=store_id, limit=limit)
    if rows:
        return rows
    return _canonical_store_inventory_rows(db, store_id=store_id, limit=limit)


def _catalog_inventory_summary(
    db: Session,
    *,
    question: str | None = None,
) -> tuple[str, list[CatalogVoiceCitation]]:
    store_query = _mentioned_store_query(db, question or "")
    if store_query:
        try:
            resolved = resolve_store(db, store_query=store_query).resolved
            if _question_requests_product_detail(question):
                detail_citations = _store_product_inventory_detail(
                    db,
                    store_id=resolved.id,
                    limit=6,
                )
                return (
                    _product_detail_message(
                        store_name=resolved.name,
                        citations=detail_citations,
                        question=question,
                    ),
                    detail_citations,
                )
            rows = _store_inventory_rows(db, store_id=resolved.id, limit=1)
        except ValueError:
            rows = []
        if rows:
            row = rows[0]
            return _store_status_message(row), [_store_inventory_citation(row)]
        return f"No inventory rows were found for {store_query}.", []

    rows = _store_inventory_rows(db, limit=8)
    if _question_requests_product_detail(question):
        detail_citations = _store_product_inventory_detail(
            db,
            store_id=None,
            limit=8,
        )
        return (
            _product_detail_message(
                store_name=None,
                citations=detail_citations,
                question=question,
            ),
            detail_citations,
        )

    low_rows = [row for row in rows if row["low_stock_skus"] > 0]
    if low_rows:
        citations = [_store_inventory_citation(row) for row in low_rows]
        preview = "; ".join(_store_status_message(row) for row in low_rows[:3])
        suffix = " More store risk rows are cited." if len(citations) > 3 else ""
        return f"Store inventory risk: {preview}{suffix}", citations

    rows = [
        {
            "inventory": inventory,
            "product": product,
            "store": store,
        }
        for inventory, product, store in db.execute(
            select(ProductInventory, CatalogProduct, Store)
            .join(CatalogProduct, CatalogProduct.id == ProductInventory.catalog_product_id)
            .join(Store, Store.id == ProductInventory.store_id)
            .where(CatalogProduct.lifecycle_status == "published")
            .order_by(
                ProductInventory.inventory_qty.asc(),
                Store.name.asc(),
                CatalogProduct.title.asc(),
            )
            .limit(80)
        ).all()
    ]
    low_rows = [
        (row["inventory"], row["product"], row["store"])
        for row in rows
        if row["inventory"].availability == "low stock"
        or int(row["inventory"].inventory_qty or 0) <= LOW_STOCK_THRESHOLD
    ][:8]
    citations = [
        CatalogVoiceCitation(
            kind="inventory",
            source_id=f"{product.id}:{inventory.store_id}:{inventory.size_key}",
            label=f"{store.name}: {product.title}",
            value={
                "product_id": product.id,
                "title": product.title,
                "store_id": store.id,
                "store_name": store.name,
                "size": inventory.size,
                "availability": inventory.availability,
                "inventory_qty": inventory.inventory_qty,
            },
        )
        for inventory, product, store in low_rows
    ]
    if not citations:
        return "No low-stock inventory is currently reported across published products.", []
    preview = "; ".join(
        f"{item.value['store_name']} has {item.value['inventory_qty']} unit(s) of {item.value['title']}"
        for item in citations[:3]
    )
    suffix = " More low-stock rows are cited." if len(citations) > 3 else ""
    return f"Low stock appears across the catalog: {preview}.{suffix}", citations


def answer_catalog_question(
    db: Session,
    *,
    question: str,
    query_scopes: list[Literal["catalog", "inventory"]] | None = None,
) -> CatalogVoiceToolResult:
    scopes = query_scopes or ["catalog", "inventory"]
    if "inventory" in scopes and (
        _question_mentions_inventory(question) or _mentioned_store_query(db, question)
    ):
        message, citations = _catalog_inventory_summary(db, question=question)
    else:
        message, citations = _catalog_summary(db)
    return CatalogVoiceToolResult(message=message, citations=citations)


def _readiness_summary(
    db: Session,
    *,
    revision: CatalogDraftRevision,
    principal: AuthenticatedPrincipal,
) -> tuple[str, list[CatalogVoiceCitation]]:
    readiness = get_product_readiness_v3(
        db,
        product_id=revision.catalog_product_id,
        draft_id=revision.id,
        principal=principal,
    )
    citations = [
        CatalogVoiceCitation(
            kind="readiness",
            source_id=revision.id,
            label="Publish readiness",
            value={
                "ready": readiness.ready,
                "blocking_codes": [item.code for item in readiness.blocking_errors],
                "recommendation_codes": [item.code for item in readiness.recommendations],
            },
        )
    ]
    if readiness.ready:
        return "The current draft passes all blocking publish-readiness checks.", citations
    return (
        f"The current draft has {len(readiness.blocking_errors)} blocking readiness issue(s).",
        citations,
    )


def _record_tool_event(
    db: Session,
    *,
    workflow_id: str,
    request: CatalogRealtimeV3ToolCallRequest,
    result: CatalogVoiceToolResult,
    principal: AuthenticatedPrincipal,
    settings: Settings,
) -> None:
    source = (
        f"{principal.provider}:{principal.provider_user_id}:{workflow_id}:"
        f"{request.session_id}:{request.call_id}"
    )
    event_key = hashlib.sha256(source.encode()).hexdigest()[:32]
    append_workflow_event(
        db,
        workflow_id=workflow_id,
        principal=principal,
        settings=settings,
        event=WorkflowEventInput(
            client_event_id=f"realtime-v3-tool-{event_key}",
            stage="voice",
            capability="realtime",
            status=result.status,
            business_summary=(
                "The voice assistant was blocked before staging a field proposal."
                if result.status == "blocked"
                else (
                    "The voice assistant returned an authorized catalog projection."
                    if result.suggestion_set is None
                    else "The voice assistant staged one field proposal for merchant review."
                )
            ),
            model=settings.catalog_studio_realtime_model,
            request_payload={
                "input": {
                    "action": request.name,
                    "session_id": request.session_id,
                }
            },
            response_payload={
                "status": result.status,
                "mutation": False,
                "citation_ids": [item.source_id for item in result.citations],
                "suggestion_set_id": (
                    result.suggestion_set.id if result.suggestion_set else None
                ),
            },
        ),
    )


def execute_catalog_voice_tool(
    db: Session,
    *,
    workflow_id: str,
    request: CatalogRealtimeV3ToolCallRequest,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
    settings: Settings,
    suggestion_service: CatalogAISuggestionService,
) -> CatalogVoiceToolResult:
    workflow = _owned_workflow(db, workflow_id=workflow_id, principal=principal)
    context, tool_names = _latest_v3_session_context(
        db,
        workflow_id=workflow_id,
        session_id=request.session_id,
        principal=principal,
    )
    if request.name not in tool_names:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This tool is unavailable in the active Realtime context.",
        )
    revision = None
    product = None
    if context.get("draft_id") and context.get("product_id"):
        revision, product = _validated_context_draft(
            db,
            workflow=workflow,
            context=context,
            principal=principal,
        )

    suggestion_set = None
    result_status: Literal["succeeded", "blocked"] = "succeeded"
    if request.name == "read_product_summary":
        if product is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This Realtime session has no active product context.",
            )
        message, citations = _product_summary(product)
    elif request.name == "read_catalog_summary":
        message, citations = _catalog_summary(db)
    elif request.name == "read_inventory_status":
        message, citations = _inventory_summary(
            db,
            product,
            question=request.arguments.question,
        )
    elif request.name == "read_publish_readiness":
        if revision is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This Realtime session has no active product context.",
            )
        message, citations = _readiness_summary(
            db,
            revision=revision,
            principal=principal,
        )
    else:
        target_path = str(context.get("target_path") or "")
        generated = suggestion_service.execute(
            db,
            product_id=revision.catalog_product_id,
            command=CatalogAISuggestionCommandRequest(
                draft_id=revision.id,
                expected_draft_version=int(context["expected_draft_version"]),
                workflow_id=workflow_id,
                instruction=request.arguments.instruction or "",
                input_origin="voice",
                source_asset_ids=[],
                target_paths=[target_path],
            ),
            idempotency_key=idempotency_key,
            principal=principal,
        )
        suggestion_set = generated.suggestion_set
        result_status = generated.status
        message = generated.message
        citations = []

    result = CatalogVoiceToolResult(
        status=result_status,
        message=message,
        citations=citations,
        suggestion_set=suggestion_set,
    )
    _record_tool_event(
        db,
        workflow_id=workflow_id,
        request=request,
        result=result,
        principal=principal,
        settings=settings,
    )
    return result
