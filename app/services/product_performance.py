from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Order, OrderItem, Product, Store
from app.schemas import (
    ProductPerformanceDimension,
    ProductPerformanceOpportunityRow,
    ProductPerformanceSummaryResponse,
    ResolvedStore,
)
from app.services.lookup import resolve_store


@dataclass
class _PerformanceAggregate:
    key: str
    product_id: str | None
    title: str | None
    brand: str
    category: str | None
    store_id: str | None
    store_name: str | None
    revenue: float
    units: float
    margin_value: float
    catalog_margin_rate: float


def _pct_delta(current: float, baseline: float) -> float | None:
    if baseline == 0:
        return None
    return ((current - baseline) / abs(baseline)) * 100.0


def _normalized_tokens(raw: str | None, *, normalize_underscores: bool = False) -> list[str]:
    if not raw:
        return []
    tokens: list[str] = []
    for chunk in str(raw).replace(";", ",").replace("|", ",").split(","):
        token = chunk.strip().lower()
        if normalize_underscores:
            token = token.replace(" ", "_")
        if not token:
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens


def _resolved_scope(
    session: Session,
    *,
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
) -> tuple[list[str], ResolvedStore | None]:
    normalized_store_ids = [str(value).strip() for value in (store_ids or []) if str(value).strip()]
    if normalized_store_ids:
        existing = set(session.scalars(select(Store.id).where(Store.id.in_(normalized_store_ids))).all())
        missing = [value for value in normalized_store_ids if value not in existing]
        if missing:
            raise ValueError(f"Unknown store_ids: {', '.join(missing)}")
        return list(dict.fromkeys(normalized_store_ids)), None

    if store_id or store_query:
        resolved = resolve_store(session, store_query=store_query, store_id=store_id).resolved
        return [resolved.id], resolved

    all_store_ids = list(session.scalars(select(Store.id).order_by(Store.id)).all())
    return all_store_ids, None


def _scope_label(
    *,
    resolved_store: ResolvedStore | None,
    explicit_store_ids: list[str],
    scoped_store_count: int,
) -> str:
    if resolved_store is not None:
        return resolved_store.name
    if explicit_store_ids:
        if len(explicit_store_ids) == 1:
            return "1 selected store"
        return f"{len(explicit_store_ids)} selected stores"
    if scoped_store_count == 1:
        return "1 store"
    return "company-wide network"


def _query_aggregates(
    session: Session,
    *,
    dimension: ProductPerformanceDimension,
    store_ids: list[str],
    since: datetime,
    until: datetime,
    brands: list[str],
    categories: list[str],
) -> dict[str, _PerformanceAggregate]:
    if not store_ids:
        return {}

    if dimension == ProductPerformanceDimension.brand:
        brand_key = func.lower(Product.brand)
        query = (
            select(
                brand_key.label("brand_key"),
                func.min(Product.brand).label("brand"),
                func.sum(OrderItem.line_total).label("revenue"),
                func.sum(OrderItem.quantity).label("units"),
                func.sum(OrderItem.line_total * Product.margin_pct).label("margin_value"),
                func.avg(Product.margin_pct).label("catalog_margin_rate"),
            )
            .join(Order, Order.id == OrderItem.order_id)
            .join(Product, Product.id == OrderItem.product_id)
            .where(
                Order.store_id.in_(store_ids),
                Order.returned.is_(False),
                Order.ordered_at >= since,
                Order.ordered_at < until,
            )
            .group_by(brand_key)
        )
        if brands:
            query = query.where(brand_key.in_(brands))
        if categories:
            query = query.where(func.lower(Product.category).in_(categories))

        rows = session.execute(query).all()
        mapped: dict[str, _PerformanceAggregate] = {}
        for row in rows:
            key = str(row.brand_key or row.brand or "unknown")
            mapped[key] = _PerformanceAggregate(
                key=key,
                product_id=None,
                title=None,
                brand=str(row.brand or "unknown"),
                category=None,
                store_id=None,
                store_name=None,
                revenue=float(row.revenue or 0.0),
                units=float(row.units or 0.0),
                margin_value=float(row.margin_value or 0.0),
                catalog_margin_rate=float(row.catalog_margin_rate or 0.0),
            )
        return mapped

    query = (
        select(
            Product.id.label("product_id"),
            Product.title.label("title"),
            Product.brand.label("brand"),
            Product.category.label("category"),
            Store.id.label("store_id"),
            Store.name.label("store_name"),
            func.sum(OrderItem.line_total).label("revenue"),
            func.sum(OrderItem.quantity).label("units"),
            func.sum(OrderItem.line_total * Product.margin_pct).label("margin_value"),
            func.avg(Product.margin_pct).label("catalog_margin_rate"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .join(Store, Store.id == Order.store_id)
        .where(
            Order.store_id.in_(store_ids),
            Order.returned.is_(False),
            Order.ordered_at >= since,
            Order.ordered_at < until,
        )
        .group_by(Product.id, Product.title, Product.brand, Product.category, Store.id, Store.name)
    )
    if brands:
        query = query.where(func.lower(Product.brand).in_(brands))
    if categories:
        query = query.where(func.lower(Product.category).in_(categories))

    rows = session.execute(query).all()
    mapped = {}
    for row in rows:
        product_id = str(row.product_id)
        store_id = str(row.store_id)
        key = f"{store_id}::{product_id}"
        mapped[key] = _PerformanceAggregate(
            key=key,
            product_id=product_id,
            title=str(row.title),
            brand=str(row.brand),
            category=str(row.category),
            store_id=store_id,
            store_name=str(row.store_name),
            revenue=float(row.revenue or 0.0),
            units=float(row.units or 0.0),
            margin_value=float(row.margin_value or 0.0),
            catalog_margin_rate=float(row.catalog_margin_rate or 0.0),
        )
    return mapped


def product_margin_sales_opportunities(
    session: Session,
    *,
    dimension: ProductPerformanceDimension = ProductPerformanceDimension.product,
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    lookback_days: int = 90,
    min_margin_rate: float = 0.50,
    min_revenue_drop_pct: float = 10.0,
    top_k: int = 15,
    brand: str | None = None,
    category: str | None = None,
) -> ProductPerformanceSummaryResponse:
    bounded_lookback = max(14, min(int(lookback_days), 730))
    bounded_top_k = max(1, min(int(top_k), 100))
    bounded_margin_rate = max(0.0, min(float(min_margin_rate), 1.0))
    bounded_drop_pct = max(0.0, min(float(min_revenue_drop_pct), 100.0))

    explicit_store_ids = [str(value).strip() for value in (store_ids or []) if str(value).strip()]
    scope_store_ids, resolved_store = _resolved_scope(
        session,
        store_query=store_query,
        store_id=store_id,
        store_ids=explicit_store_ids,
    )

    now = datetime.now(timezone.utc)
    current_since = now - timedelta(days=bounded_lookback)
    prior_since = current_since - timedelta(days=bounded_lookback)

    brands = _normalized_tokens(brand)
    categories = _normalized_tokens(category, normalize_underscores=True)

    current = _query_aggregates(
        session,
        dimension=dimension,
        store_ids=scope_store_ids,
        since=current_since,
        until=now,
        brands=brands,
        categories=categories,
    )
    prior = _query_aggregates(
        session,
        dimension=dimension,
        store_ids=scope_store_ids,
        since=prior_since,
        until=current_since,
        brands=brands,
        categories=categories,
    )

    rows: list[ProductPerformanceOpportunityRow] = []
    for key in sorted(set(current.keys()) | set(prior.keys())):
        current_row = current.get(key)
        prior_row = prior.get(key)

        current_revenue = current_row.revenue if current_row else 0.0
        prior_revenue = prior_row.revenue if prior_row else 0.0
        current_units = current_row.units if current_row else 0.0
        prior_units = prior_row.units if prior_row else 0.0

        revenue_delta = current_revenue - prior_revenue
        revenue_delta_pct = _pct_delta(current_revenue, prior_revenue)
        unit_delta = current_units - prior_units
        unit_delta_pct = _pct_delta(current_units, prior_units)

        margin_rate = 0.0
        if current_row and current_revenue > 0:
            margin_rate = current_row.margin_value / current_revenue
        elif prior_row and prior_revenue > 0:
            margin_rate = prior_row.margin_value / prior_revenue
        elif current_row:
            margin_rate = current_row.catalog_margin_rate
        elif prior_row:
            margin_rate = prior_row.catalog_margin_rate

        if prior_revenue <= 0:
            continue
        if margin_rate < bounded_margin_rate:
            continue
        if revenue_delta_pct is None or revenue_delta_pct > (-bounded_drop_pct):
            continue

        reference = current_row or prior_row
        if reference is None:
            continue

        revenue_at_risk = max(prior_revenue - current_revenue, 0.0)
        opportunity_score = (revenue_at_risk * max(margin_rate, 0.0)) + (abs(revenue_delta_pct) * 0.1)

        rows.append(
            ProductPerformanceOpportunityRow(
                dimension=dimension,
                key=reference.key,
                product_id=reference.product_id,
                title=reference.title,
                brand=reference.brand,
                category=reference.category,
                store_id=reference.store_id,
                store_name=reference.store_name,
                current_revenue=round(current_revenue, 4),
                prior_revenue=round(prior_revenue, 4),
                revenue_delta=round(revenue_delta, 4),
                revenue_delta_pct=round(revenue_delta_pct, 4) if revenue_delta_pct is not None else None,
                current_units=round(current_units, 4),
                prior_units=round(prior_units, 4),
                unit_delta=round(unit_delta, 4),
                unit_delta_pct=round(unit_delta_pct, 4) if unit_delta_pct is not None else None,
                margin_rate=round(margin_rate, 4),
                opportunity_score=round(opportunity_score, 4),
                rationale=(
                    f"Margin {margin_rate * 100:.1f}% and revenue down {abs(revenue_delta_pct or 0.0):.1f}% "
                    f"versus prior {bounded_lookback}-day window."
                ),
            )
        )

    rows.sort(key=lambda row: (row.opportunity_score, abs(row.revenue_delta_pct or 0.0), row.prior_revenue), reverse=True)

    scope_label = _scope_label(
        resolved_store=resolved_store,
        explicit_store_ids=explicit_store_ids,
        scoped_store_count=len(scope_store_ids),
    )
    dimension_label = "products" if dimension == ProductPerformanceDimension.product else "brands"
    summary = (
        f"Found {len(rows[:bounded_top_k])} high-margin, low-sales {dimension_label} in {scope_label} "
        f"over the last {bounded_lookback} days versus the prior window."
    )

    return ProductPerformanceSummaryResponse(
        summary=summary,
        dimension=dimension,
        scope_label=scope_label,
        scope_store_ids=scope_store_ids,
        lookback_days=bounded_lookback,
        current_window_start=current_since.date().isoformat(),
        current_window_end=now.date().isoformat(),
        prior_window_start=prior_since.date().isoformat(),
        prior_window_end=current_since.date().isoformat(),
        min_margin_rate=round(bounded_margin_rate, 4),
        min_revenue_drop_pct=round(bounded_drop_pct, 4),
        category=category,
        brand=brand,
        generated_at=now,
        rows=rows[:bounded_top_k],
    )
