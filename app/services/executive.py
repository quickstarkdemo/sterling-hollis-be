from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ExecutiveCampaignDraft, ExecutiveStrategyPacket, Order, OrderItem, Product, Store, StoreDailyMetric
from app.schemas import (
    ExecutiveAutoOptimizeRequest,
    ExecutiveAutoOptimizeResponse,
    ExecutiveAutoOptimizeScenario,
    ExecutiveCampaignAction,
    ExecutiveCampaignAutopilotDraftResponse,
    ExecutiveCampaignAutopilotSendResponse,
    ExecutiveCampaignCandidate,
    ExecutiveCampaignStatus,
    ExecutivePublishStrategyPacketRequest,
    ExecutiveEventReadinessRadarResponse,
    ExecutiveOverviewResponse,
    ExecutiveReadinessRecommendation,
    ExecutiveReadinessRow,
    ExecutiveRiskLevel,
    ExecutiveStrategyPacketEmailDraftResponse,
    ExecutiveStrategyPacketEmailSendResponse,
    ExecutiveStrategyPacketEmailStatus,
    ExecutiveStrategyPacketResponse,
    ExecutiveStrategyPacketStatus,
    ExecutiveStoreInsight,
    ExecutiveTrendPoint,
    ExecutiveWhatIfCategoryAllocation,
    ExecutiveWhatIfSimulatorResponse,
    ExecutiveWhatIfStoreAllocation,
    Objective,
    ProductRecommendation,
    ResolvedStore,
)
from app.services.email_service import SesEmailService
from app.services.lookup import resolve_store
from app.services.taxonomy import CATEGORY_TAXONOMY, OCCASION_TO_CATEGORY

DEFAULT_EXEC_EVENTS = ("wedding", "holiday_party", "workwear")


def _bounded_lookback(days: int) -> int:
    return max(7, min(int(days), 730))


def _pct_delta(current: float, baseline: float) -> float | None:
    if baseline == 0:
        return None
    return ((current - baseline) / abs(baseline)) * 100.0


def _risk_level(score: float) -> ExecutiveRiskLevel:
    if score >= 75:
        return ExecutiveRiskLevel.critical
    if score >= 55:
        return ExecutiveRiskLevel.high
    if score >= 30:
        return ExecutiveRiskLevel.medium
    return ExecutiveRiskLevel.low


def _normalized_events(events: list[str] | None) -> list[str]:
    values = events or list(DEFAULT_EXEC_EVENTS)
    normalized: list[str] = []
    for value in values:
        token = str(value or "").strip().lower().replace(" ", "_")
        if not token:
            continue
        if token not in normalized:
            normalized.append(token)
    return normalized or list(DEFAULT_EXEC_EVENTS)


def _normalized_brands(brands: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for value in brands or []:
        token = str(value or "").strip().lower()
        if not token:
            continue
        if token not in normalized:
            normalized.append(token)
    return normalized


def _scope_label(*, resolved: ResolvedStore | None, explicit_store_ids: list[str]) -> str:
    if resolved is not None:
        return resolved.name
    if explicit_store_ids:
        return "1 selected store" if len(explicit_store_ids) == 1 else f"{len(explicit_store_ids)} selected stores"
    return "company-wide network"


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
    store_ids = list(session.scalars(select(Store.id).order_by(Store.id)).all())
    return store_ids, None


def _store_map(session: Session, store_ids: list[str]) -> dict[str, Store]:
    if not store_ids:
        return {}
    rows = session.scalars(select(Store).where(Store.id.in_(store_ids))).all()
    return {row.id: row for row in rows}


def _store_sales(
    session: Session,
    *,
    store_ids: list[str],
    since: datetime,
    until: datetime,
    occasion: str | None = None,
    categories: list[str] | None = None,
    brands: list[str] | None = None,
) -> dict[str, dict[str, float]]:
    if not store_ids:
        return {}
    query = (
        select(
            Store.id.label("store_id"),
            func.sum(OrderItem.line_total).label("revenue"),
            func.sum(OrderItem.quantity).label("units"),
            func.sum(OrderItem.line_total * Product.margin_pct).label("margin_value"),
        )
        .join(Order, Order.store_id == Store.id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(Order.store_id.in_(store_ids), Order.ordered_at >= since, Order.ordered_at < until)
        .group_by(Store.id)
    )
    if occasion:
        query = query.where(Order.occasion == occasion)
    if categories:
        query = query.where(Product.category.in_(categories))
    if brands:
        query = query.where(func.lower(Product.brand).in_(brands))
    rows = session.execute(query).all()
    payload: dict[str, dict[str, float]] = {}
    for row in rows:
        revenue = float(row.revenue or 0.0)
        units = float(row.units or 0.0)
        margin_value = float(row.margin_value or 0.0)
        payload[row.store_id] = {
            "revenue": revenue,
            "units": units,
            "margin_value": margin_value,
            "margin_rate": (margin_value / revenue) if revenue > 0 else 0.0,
        }
    return payload


def _inventory_units_by_store(
    session: Session,
    *,
    store_ids: list[str],
    categories: list[str],
    brands: list[str] | None = None,
) -> dict[str, float]:
    if not store_ids:
        return {}
    query = select(Product.store_id.label("store_id"), func.sum(Product.inventory_qty).label("units")).where(
        Product.store_id.in_(store_ids), Product.category.in_(categories)
    )
    if brands:
        query = query.where(func.lower(Product.brand).in_(brands))
    query = query.group_by(Product.store_id)
    rows = session.execute(query).all()
    return {row.store_id: float(row.units or 0.0) for row in rows}


def _company_weekly_trend(
    session: Session,
    *,
    store_ids: list[str],
    since: datetime,
    until: datetime,
) -> list[ExecutiveTrendPoint]:
    if not store_ids:
        return []
    trend_until = until.date()
    current_week_start = trend_until - timedelta(days=trend_until.weekday())
    if current_week_start > since.date():
        trend_until = current_week_start
    if trend_until <= since.date():
        trend_until = until.date()
    rows = session.execute(
        select(StoreDailyMetric.metric_date, StoreDailyMetric.revenue, StoreDailyMetric.units_sold, StoreDailyMetric.margin_rate).where(
            StoreDailyMetric.store_id.in_(store_ids),
            StoreDailyMetric.metric_date >= since.date(),
            StoreDailyMetric.metric_date < trend_until,
        )
    ).all()

    buckets: dict[date, dict[str, float]] = {}
    for row in rows:
        metric_date = row.metric_date
        week_start = metric_date - timedelta(days=metric_date.weekday())
        entry = buckets.setdefault(week_start, {"revenue": 0.0, "units": 0.0, "margin_value": 0.0})
        revenue = float(row.revenue or 0.0)
        units = float(row.units_sold or 0.0)
        margin_rate = float(row.margin_rate or 0.0)
        entry["revenue"] += revenue
        entry["units"] += units
        entry["margin_value"] += revenue * margin_rate

    trend: list[ExecutiveTrendPoint] = []
    for week_start in sorted(buckets.keys()):
        payload = buckets[week_start]
        revenue = payload["revenue"]
        margin_rate = (payload["margin_value"] / revenue) if revenue > 0 else 0.0
        trend.append(
            ExecutiveTrendPoint(
                period_start=week_start.isoformat(),
                revenue=round(revenue, 4),
                units=round(payload["units"], 4),
                margin_rate=round(margin_rate, 4),
            )
        )
    return trend


def executive_overview(
    session: Session,
    *,
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    lookback_days: int = 90,
    objective: Objective = Objective.revenue,
    top_k_stores: int = 12,
) -> ExecutiveOverviewResponse:
    explicit_store_ids = [str(value).strip() for value in (store_ids or []) if str(value).strip()]
    bounded_lookback = _bounded_lookback(lookback_days)
    bounded_top_k = max(1, min(int(top_k_stores), 50))
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=bounded_lookback)
    prior_since = since - timedelta(days=bounded_lookback)
    store_ids, resolved = _resolved_scope(session, store_query=store_query, store_id=store_id, store_ids=store_ids)
    stores = _store_map(session, store_ids)
    current = _store_sales(session, store_ids=store_ids, since=since, until=now)
    prior = _store_sales(session, store_ids=store_ids, since=prior_since, until=since)

    total_revenue = sum(value["revenue"] for value in current.values())
    total_units = sum(value["units"] for value in current.values())
    total_margin_value = sum(value["margin_value"] for value in current.values())
    margin_rate = (total_margin_value / total_revenue) if total_revenue > 0 else 0.0

    prior_revenue = sum(value["revenue"] for value in prior.values())
    prior_margin_value = sum(value["margin_value"] for value in prior.values())
    prior_margin_rate = (prior_margin_value / prior_revenue) if prior_revenue > 0 else None
    revenue_delta_pct = _pct_delta(total_revenue, prior_revenue)

    ranked_store_ids = sorted(current.keys(), key=lambda key: current[key]["revenue"], reverse=True)
    insights: list[ExecutiveStoreInsight] = []
    for idx, sid in enumerate(ranked_store_ids[:bounded_top_k], start=1):
        store = stores.get(sid)
        if store is None:
            continue
        current_row = current[sid]
        prior_row = prior.get(sid, {"revenue": 0.0})
        insights.append(
            ExecutiveStoreInsight(
                store_id=sid,
                store_name=store.name,
                city=store.city,
                state=store.state,
                revenue=round(current_row["revenue"], 4),
                units=round(current_row["units"], 4),
                margin_rate=round(current_row["margin_rate"], 4),
                revenue_share_pct=round((current_row["revenue"] / total_revenue) * 100.0, 4) if total_revenue else 0.0,
                revenue_delta_pct=(
                    round(_pct_delta(current_row["revenue"], prior_row["revenue"]), 4)
                    if _pct_delta(current_row["revenue"], prior_row["revenue"]) is not None
                    else None
                ),
                rank=idx,
            )
        )

    trend = _company_weekly_trend(session, store_ids=store_ids, since=since, until=now)
    scope_label = _scope_label(resolved=resolved, explicit_store_ids=explicit_store_ids)
    summary = (
        f"Executive overview for {scope_label} across the last {bounded_lookback} days. "
        f"Objective: {objective.value.replace('_', ' ')}."
    )
    return ExecutiveOverviewResponse(
        summary=summary,
        lookback_days=bounded_lookback,
        objective=objective,
        generated_at=now,
        total_revenue=round(total_revenue, 4),
        total_units=round(total_units, 4),
        margin_rate=round(margin_rate, 4),
        prior_revenue=round(prior_revenue, 4) if prior_revenue else None,
        prior_margin_rate=round(prior_margin_rate, 4) if prior_margin_rate is not None else None,
        revenue_delta_pct=round(revenue_delta_pct, 4) if revenue_delta_pct is not None else None,
        store_count=len(store_ids),
        stores=insights,
        trend=trend,
    )


def event_readiness_radar(
    session: Session,
    *,
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    lookback_days: int = 56,
    events: list[str] | None = None,
    brands: list[str] | None = None,
) -> ExecutiveEventReadinessRadarResponse:
    explicit_store_ids = [str(value).strip() for value in (store_ids or []) if str(value).strip()]
    bounded_lookback = _bounded_lookback(lookback_days)
    normalized_events = _normalized_events(events)
    normalized_brands = _normalized_brands(brands)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=bounded_lookback)
    prior_since = since - timedelta(days=bounded_lookback)
    weeks = max(1.0, bounded_lookback / 7.0)

    store_ids, resolved = _resolved_scope(session, store_query=store_query, store_id=store_id, store_ids=store_ids)
    stores = _store_map(session, store_ids)
    rows: list[ExecutiveReadinessRow] = []

    for event in normalized_events:
        categories = list(OCCASION_TO_CATEGORY.get(event, CATEGORY_TAXONOMY.keys()))
        current = _store_sales(
            session,
            store_ids=store_ids,
            since=since,
            until=now,
            occasion=event,
            categories=categories,
            brands=normalized_brands,
        )
        prior = _store_sales(
            session,
            store_ids=store_ids,
            since=prior_since,
            until=since,
            occasion=event,
            categories=categories,
            brands=normalized_brands,
        )
        inventory = _inventory_units_by_store(
            session,
            store_ids=store_ids,
            categories=categories,
            brands=normalized_brands,
        )

        coverage_by_store: dict[str, float] = {}
        for sid in store_ids:
            recent_units = float(current.get(sid, {}).get("units", 0.0))
            inventory_units = float(inventory.get(sid, 0.0))
            weekly_run_rate = max(recent_units / weeks, 0.1)
            coverage_by_store[sid] = inventory_units / weekly_run_rate if inventory_units > 0 else 0.0

        donor_ids = [sid for sid in store_ids if coverage_by_store.get(sid, 0.0) >= 8.0]
        donor_ids.sort(key=lambda sid: coverage_by_store.get(sid, 0.0), reverse=True)

        for sid in store_ids:
            store = stores.get(sid)
            if store is None:
                continue
            recent_units = float(current.get(sid, {}).get("units", 0.0))
            prior_units = float(prior.get(sid, {}).get("units", 0.0))
            margin_rate = float(current.get(sid, {}).get("margin_rate", 0.0))
            inventory_units = float(inventory.get(sid, 0.0))
            coverage_weeks = float(coverage_by_store.get(sid, 0.0))
            demand_change_pct = _pct_delta(recent_units, prior_units)

            shortage_component = min(max((6.0 - coverage_weeks) / 6.0, 0.0), 1.0) * 60.0
            demand_component = min(max(max(demand_change_pct or 0.0, 0.0) / 120.0, 0.0), 1.0) * 25.0
            margin_component = min(max((0.42 - margin_rate) / 0.42, 0.0), 1.0) * 15.0
            risk_score = shortage_component + demand_component + margin_component
            risk_level = _risk_level(risk_score)

            recommendation: ExecutiveReadinessRecommendation
            if coverage_weeks < 3.0:
                donor_id = next((candidate for candidate in donor_ids if candidate != sid), None)
                donor_store = stores.get(donor_id) if donor_id else None
                recommendation = ExecutiveReadinessRecommendation(
                    action=ExecutiveCampaignAction.transfer,
                    source_store_id=donor_id,
                    source_store_name=donor_store.name if donor_store else None,
                    rationale=(
                        f"Coverage is {coverage_weeks:.1f} weeks for {event}; prioritize transfer replenishment before demand peak."
                    ),
                )
            elif coverage_weeks >= 4.0 and margin_rate >= 0.40 and risk_score < 65:
                suggested_discount = 12.0 if (demand_change_pct or 0.0) < 0 else 8.0
                recommendation = ExecutiveReadinessRecommendation(
                    action=ExecutiveCampaignAction.promotion,
                    suggested_discount_pct=suggested_discount,
                    rationale=(
                        f"Healthy cover ({coverage_weeks:.1f} weeks) with margin {margin_rate * 100:.1f}% supports a bounded campaign."
                    ),
                )
            else:
                recommendation = ExecutiveReadinessRecommendation(
                    action=ExecutiveCampaignAction.monitor,
                    rationale="Monitor event flow; risk and cover are not yet in transfer/promotion trigger bands.",
                )

            rows.append(
                ExecutiveReadinessRow(
                    event=event,
                    store_id=sid,
                    store_name=store.name,
                    city=store.city,
                    state=store.state,
                    risk_score=round(risk_score, 4),
                    risk_level=risk_level,
                    coverage_weeks=round(coverage_weeks, 4),
                    inventory_units=round(inventory_units, 4),
                    recent_units=round(recent_units, 4),
                    prior_units=round(prior_units, 4),
                    demand_change_pct=round(demand_change_pct, 4) if demand_change_pct is not None else None,
                    margin_rate=round(margin_rate, 4),
                    recommendation=recommendation,
                )
            )

    rows.sort(key=lambda row: row.risk_score, reverse=True)
    scope_label = _scope_label(resolved=resolved, explicit_store_ids=explicit_store_ids)
    summary = f"Event readiness radar for {scope_label} over the last {bounded_lookback} days."
    return ExecutiveEventReadinessRadarResponse(
        summary=summary,
        lookback_days=bounded_lookback,
        generated_at=now,
        events=normalized_events,
        rows=rows,
    )


def _category_sales(
    session: Session,
    *,
    store_ids: list[str],
    since: datetime,
    until: datetime,
    brands: list[str] | None = None,
) -> dict[str, dict[str, float]]:
    if not store_ids:
        return {}
    query = (
        select(
            Product.category.label("category"),
            func.sum(OrderItem.line_total).label("revenue"),
            func.sum(OrderItem.quantity).label("units"),
            func.sum(OrderItem.line_total * Product.margin_pct).label("margin_value"),
        )
        .join(OrderItem, OrderItem.product_id == Product.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.store_id.in_(store_ids), Order.ordered_at >= since, Order.ordered_at < until)
    )
    if brands:
        query = query.where(func.lower(Product.brand).in_(brands))
    rows = session.execute(query.group_by(Product.category)).all()
    return {
        row.category: {
            "revenue": float(row.revenue or 0.0),
            "units": float(row.units or 0.0),
            "margin_value": float(row.margin_value or 0.0),
            "margin_rate": (float(row.margin_value or 0.0) / float(row.revenue or 1.0)) if float(row.revenue or 0.0) > 0 else 0.0,
        }
        for row in rows
    }


def _category_inventory(
    session: Session,
    *,
    store_ids: list[str],
    brands: list[str] | None = None,
) -> dict[str, float]:
    if not store_ids:
        return {}
    query = select(Product.category.label("category"), func.sum(Product.inventory_qty).label("inventory_units")).where(
        Product.store_id.in_(store_ids)
    )
    if brands:
        query = query.where(func.lower(Product.brand).in_(brands))
    rows = session.execute(query.group_by(Product.category)).all()
    return {row.category: float(row.inventory_units or 0.0) for row in rows}


def _store_category_sales(
    session: Session,
    *,
    store_ids: list[str],
    since: datetime,
    until: datetime,
    brands: list[str] | None = None,
) -> dict[str, dict[str, dict[str, float]]]:
    if not store_ids:
        return {}
    query = (
        select(
            Order.store_id.label("store_id"),
            Product.category.label("category"),
            func.sum(OrderItem.line_total).label("revenue"),
            func.sum(OrderItem.quantity).label("units"),
            func.sum(OrderItem.line_total * Product.margin_pct).label("margin_value"),
        )
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(Order.store_id.in_(store_ids), Order.ordered_at >= since, Order.ordered_at < until)
        .group_by(Order.store_id, Product.category)
    )
    if brands:
        query = query.where(func.lower(Product.brand).in_(brands))
    rows = session.execute(query).all()
    payload: dict[str, dict[str, dict[str, float]]] = {}
    for row in rows:
        revenue = float(row.revenue or 0.0)
        margin_value = float(row.margin_value or 0.0)
        payload.setdefault(row.store_id, {})[row.category] = {
            "revenue": revenue,
            "units": float(row.units or 0.0),
            "margin_value": margin_value,
            "margin_rate": (margin_value / revenue) if revenue > 0 else 0.0,
        }
    return payload


def _store_category_inventory(
    session: Session,
    *,
    store_ids: list[str],
    brands: list[str] | None = None,
) -> dict[str, dict[str, float]]:
    if not store_ids:
        return {}
    query = (
        select(
            Product.store_id.label("store_id"),
            Product.category.label("category"),
            func.sum(Product.inventory_qty).label("inventory_units"),
        )
        .where(Product.store_id.in_(store_ids))
        .group_by(Product.store_id, Product.category)
    )
    if brands:
        query = query.where(func.lower(Product.brand).in_(brands))
    rows = session.execute(query).all()
    payload: dict[str, dict[str, float]] = {}
    for row in rows:
        payload.setdefault(row.store_id, {})[row.category] = float(row.inventory_units or 0.0)
    return payload


def _project_category_allocations(
    *,
    sales: dict[str, dict[str, float]],
    inventory: dict[str, float],
    discount_pct: float,
    floor_space_shift_pct: float,
    from_category: str | None,
    to_category: str | None,
) -> tuple[list[ExecutiveWhatIfCategoryAllocation], float, float]:
    categories = sorted(set(sales.keys()) | set(inventory.keys()))
    category_rows: dict[str, dict[str, float]] = {}
    discount_by_category: dict[str, float] = {}

    for category in categories:
        sales_row = sales.get(category, {})
        revenue = float(sales_row.get("revenue", 0.0))
        margin_value = float(sales_row.get("margin_value", 0.0))
        category_rows[category] = {
            "baseline_revenue": revenue,
            "baseline_margin_value": margin_value,
            "baseline_margin_rate": float(sales_row.get("margin_rate", 0.0)),
            "baseline_space_units": float(inventory.get(category, 0.0)),
            "projected_revenue": revenue,
            "projected_margin_value": margin_value,
            "projected_space_units": float(inventory.get(category, 0.0)),
        }
        discount_by_category[category] = 0.0

    bounded_discount = max(0.0, min(float(discount_pct), 60.0))
    bounded_shift = max(-40.0, min(float(floor_space_shift_pct), 40.0))
    discount_target = None
    if to_category and to_category in category_rows:
        discount_target = to_category
    elif from_category and from_category in category_rows:
        discount_target = from_category

    if bounded_discount > 0 and discount_target:
        discount_fraction = bounded_discount / 100.0
        base_revenue = category_rows[discount_target]["projected_revenue"]
        base_margin_rate = category_rows[discount_target]["baseline_margin_rate"]
        elasticity = 1.6
        volume_lift = elasticity * discount_fraction
        discounted_revenue = base_revenue * (1.0 - discount_fraction) * (1.0 + volume_lift)
        adjusted_margin_rate = max(0.0, base_margin_rate - discount_fraction * 0.9)
        category_rows[discount_target]["projected_revenue"] = discounted_revenue
        category_rows[discount_target]["projected_margin_value"] = discounted_revenue * adjusted_margin_rate
        discount_by_category[discount_target] = bounded_discount

    if (
        bounded_shift != 0
        and from_category
        and to_category
        and from_category != to_category
        and from_category in category_rows
        and to_category in category_rows
    ):
        source_category = from_category
        destination_category = to_category
        if bounded_shift < 0:
            source_category = to_category
            destination_category = from_category

        shift_fraction = abs(bounded_shift) / 100.0
        source_revenue = category_rows[source_category]["baseline_revenue"]
        source_margin_rate = category_rows[source_category]["baseline_margin_rate"]
        destination_margin_rate = category_rows[destination_category]["baseline_margin_rate"]
        base_realloc_revenue = min(source_revenue * shift_fraction, category_rows[source_category]["projected_revenue"])

        category_rows[source_category]["projected_revenue"] -= base_realloc_revenue
        category_rows[destination_category]["projected_revenue"] += base_realloc_revenue
        category_rows[source_category]["projected_margin_value"] -= base_realloc_revenue * source_margin_rate
        category_rows[destination_category]["projected_margin_value"] += base_realloc_revenue * destination_margin_rate

        source_space_units = category_rows[source_category]["baseline_space_units"]
        space_units_shift = source_space_units * shift_fraction
        category_rows[source_category]["projected_space_units"] = max(0.0, source_space_units - space_units_shift)
        category_rows[destination_category]["projected_space_units"] += space_units_shift

        source_inventory = max(category_rows[source_category]["baseline_space_units"], 1.0)
        destination_inventory = max(category_rows[destination_category]["baseline_space_units"], 1.0)
        source_productivity = source_revenue / source_inventory if source_inventory > 0 else 0.0
        destination_productivity = (
            category_rows[destination_category]["baseline_revenue"] / destination_inventory if destination_inventory > 0 else 0.0
        )
        productivity_delta = (
            (destination_productivity - source_productivity) / max(source_productivity, 1e-6) if source_productivity > 0 else 0.0
        )
        net_revenue_delta = base_realloc_revenue * productivity_delta * 0.35
        effective_margin_rate = (destination_margin_rate - source_margin_rate) * 0.6 + source_margin_rate
        category_rows[destination_category]["projected_revenue"] += net_revenue_delta
        category_rows[destination_category]["projected_margin_value"] += net_revenue_delta * effective_margin_rate

    for row in category_rows.values():
        if row["projected_revenue"] < 0:
            row["projected_revenue"] = 0.0
            row["projected_margin_value"] = 0.0

    baseline_total_revenue = sum(row["baseline_revenue"] for row in category_rows.values())
    projected_total_revenue = sum(row["projected_revenue"] for row in category_rows.values())
    baseline_total_space = sum(row["baseline_space_units"] for row in category_rows.values())
    projected_total_space = sum(row["projected_space_units"] for row in category_rows.values())
    projected_total_margin_value = sum(row["projected_margin_value"] for row in category_rows.values())

    allocations: list[ExecutiveWhatIfCategoryAllocation] = []
    for category, row in sorted(
        category_rows.items(),
        key=lambda item: item[1]["projected_revenue"],
        reverse=True,
    ):
        baseline_revenue = row["baseline_revenue"]
        projected_revenue = max(0.0, row["projected_revenue"])
        allocations.append(
            ExecutiveWhatIfCategoryAllocation(
                category=category,
                baseline_revenue=round(baseline_revenue, 4),
                projected_revenue=round(projected_revenue, 4),
                baseline_revenue_share_pct=round(
                    ((baseline_revenue / baseline_total_revenue) * 100.0) if baseline_total_revenue > 0 else 0.0, 4
                ),
                projected_revenue_share_pct=round(
                    ((projected_revenue / projected_total_revenue) * 100.0) if projected_total_revenue > 0 else 0.0, 4
                ),
                baseline_space_share_pct=round(
                    ((row["baseline_space_units"] / baseline_total_space) * 100.0) if baseline_total_space > 0 else 0.0, 4
                ),
                projected_space_share_pct=round(
                    ((max(0.0, row["projected_space_units"]) / projected_total_space) * 100.0) if projected_total_space > 0 else 0.0, 4
                ),
                applied_discount_pct=round(discount_by_category.get(category, 0.0), 4),
            )
        )

    return allocations, round(projected_total_revenue, 4), round(projected_total_margin_value, 4)


def what_if_simulator(
    session: Session,
    *,
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
    explicit_store_ids = [str(value).strip() for value in (store_ids or []) if str(value).strip()]
    bounded_lookback = _bounded_lookback(lookback_days)
    normalized_brands = _normalized_brands(brands)

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=bounded_lookback)
    store_ids, resolved = _resolved_scope(session, store_query=store_query, store_id=store_id, store_ids=store_ids)
    stores = _store_map(session, store_ids)

    sales = _category_sales(session, store_ids=store_ids, since=since, until=now, brands=normalized_brands)
    inventory = _category_inventory(session, store_ids=store_ids, brands=normalized_brands)
    baseline_revenue = sum(row["revenue"] for row in sales.values())
    baseline_margin_value = sum(row["margin_value"] for row in sales.values())
    baseline_margin_rate = (baseline_margin_value / baseline_revenue) if baseline_revenue > 0 else 0.0

    category_allocations, expected_revenue, expected_margin_value = _project_category_allocations(
        sales=sales,
        inventory=inventory,
        discount_pct=discount_pct,
        floor_space_shift_pct=floor_space_shift_pct,
        from_category=from_category,
        to_category=to_category,
    )
    expected_margin_rate = (expected_margin_value / expected_revenue) if expected_revenue > 0 else 0.0

    revenue_delta = expected_revenue - baseline_revenue
    margin_rate_delta = expected_margin_rate - baseline_margin_rate
    bounded_discount = max(0.0, min(float(discount_pct), 60.0))
    bounded_shift = max(-40.0, min(float(floor_space_shift_pct), 40.0))
    uncertainty = min(0.22, 0.06 + (bounded_discount * 0.003) + (abs(bounded_shift) * 0.0025))
    confidence_low = expected_revenue * (1.0 - uncertainty)
    confidence_high = expected_revenue * (1.0 + uncertainty)

    store_allocations: list[ExecutiveWhatIfStoreAllocation] = []
    if len(store_ids) == 1:
        store_category_sales = _store_category_sales(
            session,
            store_ids=store_ids,
            since=since,
            until=now,
            brands=normalized_brands,
        )
        store_category_inventory = _store_category_inventory(session, store_ids=store_ids, brands=normalized_brands)
        sid = store_ids[0]
        store = stores.get(sid)
        if store is not None:
            scoped_allocations, _, _ = _project_category_allocations(
                sales=store_category_sales.get(sid, {}),
                inventory=store_category_inventory.get(sid, {}),
                discount_pct=discount_pct,
                floor_space_shift_pct=floor_space_shift_pct,
                from_category=from_category,
                to_category=to_category,
            )
            store_allocations.append(
                ExecutiveWhatIfStoreAllocation(
                    store_id=sid,
                    store_name=store.name,
                    city=store.city,
                    state=store.state,
                    categories=scoped_allocations,
                )
            )

    scope_label = _scope_label(resolved=resolved, explicit_store_ids=explicit_store_ids)
    summary = (
        f"What-if simulation for {scope_label} over the last {bounded_lookback} days "
        f"using discount and floor-space allocation assumptions."
    )
    return ExecutiveWhatIfSimulatorResponse(
        summary=summary,
        lookback_days=bounded_lookback,
        generated_at=now,
        baseline_revenue=round(baseline_revenue, 4),
        baseline_margin_rate=round(baseline_margin_rate, 4),
        expected_revenue=round(expected_revenue, 4),
        expected_margin_rate=round(expected_margin_rate, 4),
        revenue_delta=round(revenue_delta, 4),
        margin_rate_delta=round(margin_rate_delta, 4),
        confidence_interval_low=round(confidence_low, 4),
        confidence_interval_high=round(confidence_high, 4),
        category_allocations=category_allocations,
        store_allocations=store_allocations,
    )


def _inclusive_range(min_value: float, max_value: float, step: float) -> list[float]:
    if step <= 0:
        return [round(min_value, 4)]
    values: list[float] = []
    cursor = min_value
    while cursor <= (max_value + 1e-9):
        values.append(round(cursor, 4))
        cursor += step
    if not values:
        values = [round(min_value, 4)]
    if values[-1] != round(max_value, 4):
        values.append(round(max_value, 4))
    return sorted(list(dict.fromkeys(values)))


def _objective_score(
    objective: Objective,
    *,
    revenue_delta: float,
    margin_rate_delta: float,
    expected_revenue: float,
    expected_margin_rate: float,
) -> float:
    if objective == Objective.margin:
        return (margin_rate_delta * 10000.0) + (revenue_delta * 0.05)
    if objective == Objective.sell_through:
        return (revenue_delta * 0.35) + (expected_revenue * 0.0005)
    return revenue_delta + (expected_margin_rate * 50.0)


def auto_optimize_strategy(
    session: Session,
    *,
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

    explicit_store_ids = [str(value).strip() for value in (params.store_ids or []) if str(value).strip()]
    scoped_store_ids, resolved = _resolved_scope(
        session,
        store_query=params.store_query,
        store_id=params.store_id,
        store_ids=explicit_store_ids,
    )
    scope_label = _scope_label(resolved=resolved, explicit_store_ids=explicit_store_ids)

    discount_values = _inclusive_range(params.discount_min_pct, params.discount_max_pct, params.discount_step_pct)
    if params.from_category and params.to_category and params.from_category != params.to_category:
        shift_values = _inclusive_range(params.shift_min_pct, params.shift_max_pct, params.shift_step_pct)
    else:
        shift_values = [0.0]

    candidates: list[ExecutiveAutoOptimizeScenario] = []
    scenario_counter = 0
    baseline_revenue = 0.0
    baseline_margin_rate = 0.0
    normalized_brands = _normalized_brands(params.brands)
    for discount in discount_values:
        for shift in shift_values:
            simulation = what_if_simulator(
                session,
                store_ids=scoped_store_ids,
                lookback_days=params.lookback_days,
                discount_pct=discount,
                floor_space_shift_pct=shift,
                from_category=params.from_category,
                to_category=params.to_category,
                brands=normalized_brands,
            )
            if scenario_counter == 0:
                baseline_revenue = simulation.baseline_revenue
                baseline_margin_rate = simulation.baseline_margin_rate

            guardrail_reasons: list[str] = []
            if float(discount) > float(params.max_discount_pct):
                guardrail_reasons.append(
                    f"Discount {discount:.1f}% exceeds max guardrail {params.max_discount_pct:.1f}%."
                )
            if simulation.expected_margin_rate < params.min_margin_rate:
                guardrail_reasons.append(
                    f"Projected margin {simulation.expected_margin_rate * 100:.1f}% is below guardrail {params.min_margin_rate * 100:.1f}%."
                )
            if simulation.revenue_delta <= 0:
                guardrail_reasons.append("Projected revenue lift is non-positive.")

            guardrail_passed = len(guardrail_reasons) == 0
            score = _objective_score(
                params.objective,
                revenue_delta=simulation.revenue_delta,
                margin_rate_delta=simulation.margin_rate_delta,
                expected_revenue=simulation.expected_revenue,
                expected_margin_rate=simulation.expected_margin_rate,
            )
            if not guardrail_passed:
                score -= 1_000_000.0

            scenario_counter += 1
            candidates.append(
                ExecutiveAutoOptimizeScenario(
                    scenario_id=f"opt_{scenario_counter:03d}",
                    discount_pct=round(discount, 4),
                    floor_space_shift_pct=round(shift, 4),
                    from_category=params.from_category,
                    to_category=params.to_category,
                    expected_revenue=simulation.expected_revenue,
                    expected_margin_rate=simulation.expected_margin_rate,
                    revenue_delta=simulation.revenue_delta,
                    margin_rate_delta=simulation.margin_rate_delta,
                    confidence_interval_low=simulation.confidence_interval_low,
                    confidence_interval_high=simulation.confidence_interval_high,
                    objective_score=round(score, 4),
                    guardrail_passed=guardrail_passed,
                    guardrail_reasons=guardrail_reasons,
                    rationale=(
                        f"Discount {discount:.1f}% and shift {shift:.1f}% maximize {params.objective.value.replace('_', ' ')} score "
                        f"under current constraints."
                    ),
                )
            )

    candidates.sort(
        key=lambda item: (
            1 if item.guardrail_passed else 0,
            item.objective_score,
            item.revenue_delta,
            item.margin_rate_delta,
        ),
        reverse=True,
    )
    scenarios = candidates[: params.top_k_scenarios]
    summary = (
        f"Auto-optimized {len(scenarios)} scenarios for {scope_label} over {params.lookback_days} days "
        f"using deterministic grid search."
    )
    return ExecutiveAutoOptimizeResponse(
        summary=summary,
        objective=params.objective,
        lookback_days=params.lookback_days,
        generated_at=datetime.now(timezone.utc),
        scope_label=scope_label,
        scope_store_ids=scoped_store_ids,
        baseline_revenue=round(baseline_revenue, 4),
        baseline_margin_rate=round(baseline_margin_rate, 4),
        scenarios=scenarios,
    )


def _strategy_packet_to_response(packet: ExecutiveStrategyPacket) -> ExecutiveStrategyPacketResponse:
    payload = dict(packet.payload_json or {})
    scenario_payload = payload.get("scenario", {})
    if not isinstance(scenario_payload, dict):
        scenario_payload = {}
    scenario = ExecutiveAutoOptimizeScenario.model_validate(scenario_payload)
    return ExecutiveStrategyPacketResponse(
        packet_id=packet.id,
        status=ExecutiveStrategyPacketStatus(packet.status),
        title=str(packet.title or ""),
        summary=str(packet.summary or ""),
        objective=Objective(payload.get("objective", Objective.revenue.value)),
        lookback_days=int(payload.get("lookback_days", 90)),
        scope_label=str(payload.get("scope_label") or "company-wide network"),
        scope_store_ids=[str(value) for value in payload.get("scope_store_ids", []) if str(value).strip()],
        brands=[str(value) for value in payload.get("brands", []) if str(value).strip()],
        from_category=payload.get("from_category"),
        to_category=payload.get("to_category"),
        min_margin_rate=float(payload.get("min_margin_rate", 0.40)),
        max_discount_pct=float(payload.get("max_discount_pct", 20.0)),
        scenario=scenario,
        created_at=packet.created_at,
        updated_at=packet.updated_at,
        email_status=ExecutiveStrategyPacketEmailStatus(packet.email_status),
        to_email=packet.to_email,
        email_subject=packet.email_subject,
        email_body_text=packet.email_body_text,
        provider_message_id=packet.provider_message_id,
        email_error_message=packet.email_error_message,
        sent_at=packet.sent_at,
    )


def publish_strategy_packet(
    session: Session,
    *,
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
    explicit_store_ids = [str(value).strip() for value in params.store_ids if str(value).strip()]
    scoped_store_ids, resolved = _resolved_scope(
        session,
        store_query=params.store_query,
        store_id=params.store_id,
        store_ids=explicit_store_ids,
    )
    scope_label = _scope_label(resolved=resolved, explicit_store_ids=explicit_store_ids)
    now = datetime.now(timezone.utc)
    packet_id = f"stratpkt_{uuid.uuid4().hex[:12]}"
    packet_title = (params.title or "").strip() or f"Strategy Packet - {now.date().isoformat()}"
    packet_summary = (params.summary or "").strip() or params.scenario.rationale

    payload = {
        "objective": params.objective.value,
        "lookback_days": params.lookback_days,
        "scope_label": scope_label,
        "scope_store_ids": scoped_store_ids,
        "brands": _normalized_brands(params.brands),
        "from_category": params.from_category,
        "to_category": params.to_category,
        "min_margin_rate": params.min_margin_rate,
        "max_discount_pct": params.max_discount_pct,
        "scenario": params.scenario.model_dump(mode="json"),
    }

    record = ExecutiveStrategyPacket(
        id=packet_id,
        status=ExecutiveStrategyPacketStatus.published.value,
        title=packet_title,
        summary=packet_summary,
        payload_json=payload,
        email_status=ExecutiveStrategyPacketEmailStatus.draft.value,
        created_at=now,
        updated_at=now,
    )
    session.add(record)
    session.commit()
    return _strategy_packet_to_response(record)


def get_strategy_packet(session: Session, packet_id: str) -> ExecutiveStrategyPacketResponse:
    record = session.get(ExecutiveStrategyPacket, packet_id)
    if not record:
        raise ValueError(f"Executive strategy packet {packet_id} was not found.")
    return _strategy_packet_to_response(record)


def _strategy_email_body(packet: ExecutiveStrategyPacketResponse) -> str:
    scenario = packet.scenario
    lines = [
        packet.title,
        "",
        f"Strategy Packet ID: {packet.packet_id}",
        "",
        packet.summary,
        "",
        f"Objective: {packet.objective.value.replace('_', ' ')}",
        f"Lookback: {packet.lookback_days} days",
        f"Scope: {packet.scope_label}",
        (
            f"Scoped Stores: {', '.join(packet.scope_store_ids)}"
            if packet.scope_store_ids
            else "Scoped Stores: all stores (company-wide)"
        ),
        f"Reallocate From: {packet.from_category or 'n/a'}",
        f"Reallocate To: {packet.to_category or 'n/a'}",
        f"Discount: {scenario.discount_pct:.1f}%",
        f"Floor Space Shift: {scenario.floor_space_shift_pct:.1f}%",
        f"Projected Revenue Delta: {scenario.revenue_delta:.2f}",
        f"Projected Margin Delta: {scenario.margin_rate_delta:.4f}",
        "",
        "Guardrails:",
        f"- Min margin rate: {packet.min_margin_rate * 100:.1f}%",
        f"- Max discount: {packet.max_discount_pct:.1f}%",
        "",
        "Merchandising handoff:",
        f"- Use strategy_packet_id={packet.packet_id} when opening Merch workspace.",
        "- Keep controls editable; packet values are the starting defaults.",
        "",
        "Execution note: apply strategy via merchandising controls and associate priority tags.",
    ]
    return "\n".join(lines)


def prepare_strategy_packet_email(
    session: Session,
    *,
    packet_id: str,
    to_email: str,
) -> ExecutiveStrategyPacketEmailDraftResponse:
    destination = (to_email or "").strip().lower()
    if not destination:
        raise ValueError("to_email is required.")
    record = session.get(ExecutiveStrategyPacket, packet_id)
    if not record:
        raise ValueError(f"Executive strategy packet {packet_id} was not found.")

    packet = _strategy_packet_to_response(record)
    record.to_email = destination
    record.email_subject = f"Strategy Packet {packet.packet_id} - {packet.title}"
    record.email_body_text = _strategy_email_body(packet)
    record.email_status = ExecutiveStrategyPacketEmailStatus.draft.value
    record.email_error_message = None
    record.updated_at = datetime.now(timezone.utc)
    session.add(record)
    session.commit()

    return ExecutiveStrategyPacketEmailDraftResponse(
        packet_id=record.id,
        email_status=ExecutiveStrategyPacketEmailStatus(record.email_status),
        to_email=destination,
        subject=str(record.email_subject or ""),
        body_text=str(record.email_body_text or ""),
        generated_at=record.updated_at,
    )


def send_strategy_packet_email(
    session: Session,
    *,
    packet_id: str,
    approved: bool = False,
) -> ExecutiveStrategyPacketEmailSendResponse:
    if not approved:
        raise ValueError("Explicit approval is required to send strategy packet emails.")

    record = session.get(ExecutiveStrategyPacket, packet_id)
    if not record:
        raise ValueError(f"Executive strategy packet {packet_id} was not found.")
    if record.email_status == ExecutiveStrategyPacketEmailStatus.sent.value:
        raise ValueError("Strategy packet email was already sent.")
    if not record.to_email or not record.email_subject or not record.email_body_text:
        raise ValueError("Prepare a strategy packet email draft before sending.")

    email_service = SesEmailService()
    provider_message_id = None
    try:
        payload = email_service.send_email(
            to_email=record.to_email,
            subject=record.email_subject,
            text_body=record.email_body_text,
        )
        provider_message_id = payload.get("message_id")
        record.email_status = ExecutiveStrategyPacketEmailStatus.sent.value
        record.provider_message_id = provider_message_id
        record.email_error_message = None
        record.sent_at = datetime.now(timezone.utc)
    except Exception as exc:
        record.email_status = ExecutiveStrategyPacketEmailStatus.failed.value
        record.email_error_message = str(exc)[:2000]
        record.sent_at = None
    finally:
        record.updated_at = datetime.now(timezone.utc)
        session.add(record)
        session.commit()

    return ExecutiveStrategyPacketEmailSendResponse(
        packet_id=record.id,
        email_status=ExecutiveStrategyPacketEmailStatus(record.email_status),
        to_email=record.to_email or "",
        provider_message_id=provider_message_id,
        error_message=record.email_error_message,
        sent_at=record.sent_at,
    )


def active_strategy_packet_for_store(session: Session, store_id: str) -> ExecutiveStrategyPacketResponse | None:
    records = session.scalars(
        select(ExecutiveStrategyPacket)
        .where(ExecutiveStrategyPacket.status == ExecutiveStrategyPacketStatus.published.value)
        .order_by(ExecutiveStrategyPacket.created_at.desc())
    ).all()
    for record in records:
        payload = dict(record.payload_json or {})
        scope_store_ids = [str(value).strip() for value in payload.get("scope_store_ids", []) if str(value).strip()]
        if not scope_store_ids or store_id in scope_store_ids:
            return _strategy_packet_to_response(record)
    return None


def apply_execution_tags_for_store(
    session: Session,
    *,
    store_id: str,
    recommendations: list[ProductRecommendation],
) -> tuple[str | None, list[ProductRecommendation]]:
    strategy_packet = active_strategy_packet_for_store(session, store_id)
    if strategy_packet is None or not recommendations:
        return None, recommendations

    focus_category = (strategy_packet.to_category or "").strip().lower()
    prioritized_brands = {value.strip().lower() for value in strategy_packet.brands if str(value).strip()}
    min_margin_rate = float(strategy_packet.min_margin_rate)
    product_ids = [item.product_id for item in recommendations if item.product_id]
    products = session.scalars(select(Product).where(Product.id.in_(product_ids))).all() if product_ids else []
    product_margin_map = {product.id: float(product.margin_pct or 0.0) for product in products}

    for item in recommendations:
        tags: list[str] = []
        item_category = str(item.category or "").strip().lower()
        item_brand = str(item.brand or "").strip().lower()
        if focus_category and item_category == focus_category:
            tags.append("Focus This Week")
        if product_margin_map.get(item.product_id, 0.0) >= min_margin_rate:
            tags.append("Margin Priority")
        if (focus_category and item_category == focus_category) or (item_brand and item_brand in prioritized_brands):
            tags.append("Campaign Assist")
        item.execution_tags = list(dict.fromkeys(tags))

    return strategy_packet.packet_id, recommendations


def campaign_autopilot_prepare(
    session: Session,
    *,
    store_query: str | None = None,
    store_id: str | None = None,
    store_ids: list[str] | None = None,
    to_email: str,
    lookback_days: int = 56,
    top_k: int = 6,
    events: list[str] | None = None,
    brands: list[str] | None = None,
    min_margin_rate: float = 0.40,
    max_discount_pct: float = 20.0,
) -> ExecutiveCampaignAutopilotDraftResponse:
    destination = (to_email or "").strip().lower()
    if not destination:
        raise ValueError("to_email is required.")

    bounded_top_k = max(1, min(int(top_k), 20))
    radar = event_readiness_radar(
        session,
        store_query=store_query,
        store_id=store_id,
        store_ids=store_ids,
        lookback_days=lookback_days,
        events=events,
        brands=brands,
    )
    scored: list[tuple[float, ExecutiveCampaignCandidate]] = []

    for row in radar.rows:
        action = row.recommendation.action
        if action == ExecutiveCampaignAction.monitor:
            continue
        if row.risk_level == ExecutiveRiskLevel.critical and row.coverage_weeks < 2.5:
            continue

        if action == ExecutiveCampaignAction.promotion:
            if row.margin_rate < min_margin_rate or row.coverage_weeks < 4.0:
                continue
            suggested_discount = row.recommendation.suggested_discount_pct or 8.0
            suggested_discount = min(max(5.0, suggested_discount), max_discount_pct)
            score = (
                (row.margin_rate * 100.0)
                + (max(0.0, -(row.demand_change_pct or 0.0)) * 0.4)
                + (row.coverage_weeks * 1.2)
                - (row.risk_score * 0.25)
            )
        else:
            if not row.recommendation.source_store_id:
                continue
            suggested_discount = None
            score = (
                (row.risk_score * 0.8)
                + (max(0.0, row.demand_change_pct or 0.0) * 0.2)
                + (max(0.0, 4.0 - row.coverage_weeks) * 8.0)
            )

        candidate = ExecutiveCampaignCandidate(
            store_id=row.store_id,
            store_name=row.store_name,
            city=row.city,
            state=row.state,
            event=row.event,
            risk_score=row.risk_score,
            risk_level=row.risk_level,
            coverage_weeks=row.coverage_weeks,
            margin_rate=row.margin_rate,
            action=action,
            suggested_discount_pct=suggested_discount,
            source_store_id=row.recommendation.source_store_id,
            source_store_name=row.recommendation.source_store_name,
            rationale=row.recommendation.rationale,
        )
        scored.append((score, candidate))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    shortlist = [item for _, item in scored[:bounded_top_k]]
    now = datetime.now(timezone.utc)
    subject = f"Weekly Featured Campaign Shortlist - {now.date().isoformat()}"
    if shortlist:
        lines = [
            "Executive Campaign Autopilot draft package",
            "",
            "Shortlist:",
        ]
        for idx, item in enumerate(shortlist, start=1):
            action_text = item.action.value
            if item.action == ExecutiveCampaignAction.transfer and item.source_store_name:
                action_text += f" from {item.source_store_name}"
            if item.suggested_discount_pct is not None:
                action_text += f" ({item.suggested_discount_pct:.0f}% discount)"
            lines.append(
                f"{idx}. {item.store_name} ({item.city}, {item.state}) - {item.event} - {action_text}. {item.rationale}"
            )
        lines.extend(
            [
                "",
                "Approval gate:",
                "- Review shortlist in workspace/chat.",
                "- Trigger explicit send approval to deliver this package.",
            ]
        )
    else:
        lines = [
            "Executive Campaign Autopilot draft package",
            "",
            "No candidates met guardrails for this run.",
            "Recommendation: review Event Readiness Radar for transfer-only mitigations.",
        ]
    body_text = "\n".join(lines)

    record = ExecutiveCampaignDraft(
        id=f"execdraft_{uuid.uuid4().hex[:12]}",
        status=ExecutiveCampaignStatus.draft.value,
        to_email=destination,
        subject=subject,
        body_text=body_text,
        payload_json={
            "lookback_days": _bounded_lookback(lookback_days),
            "guardrails": {
                "min_margin_rate": min_margin_rate,
                "max_discount_pct": max_discount_pct,
                "max_candidates": bounded_top_k,
                "exclude_critical_shortage": True,
            },
            "scope": {
                "store_query": store_query,
                "store_id": store_id,
                "store_ids": [value for value in (store_ids or []) if str(value).strip()],
            },
            "events": radar.events,
            "brands": [value for value in (brands or []) if str(value).strip()],
            "candidates": [item.model_dump(mode="json") for item in shortlist],
            "radar_summary": radar.summary,
        },
    )
    session.add(record)
    session.commit()

    return ExecutiveCampaignAutopilotDraftResponse(
        draft_id=record.id,
        status=ExecutiveCampaignStatus(record.status),
        to_email=record.to_email,
        subject=record.subject,
        body_text=record.body_text,
        lookback_days=int(record.payload_json.get("lookback_days", _bounded_lookback(lookback_days))),
        generated_at=record.created_at,
        guardrails=dict(record.payload_json.get("guardrails", {})),
        candidates=shortlist,
    )


def campaign_autopilot_send(
    session: Session,
    *,
    draft_id: str,
    approved: bool = False,
) -> ExecutiveCampaignAutopilotSendResponse:
    if not approved:
        raise ValueError("Explicit approval is required to send campaign autopilot drafts.")

    record = session.get(ExecutiveCampaignDraft, draft_id)
    if not record:
        raise ValueError(f"Executive campaign draft {draft_id} was not found.")
    if record.status == ExecutiveCampaignStatus.sent.value:
        raise ValueError("Draft was already sent.")

    email_service = SesEmailService()
    provider_message_id = None
    try:
        payload = email_service.send_email(
            to_email=record.to_email,
            subject=record.subject,
            text_body=record.body_text,
        )
        provider_message_id = payload.get("message_id")
        record.status = ExecutiveCampaignStatus.sent.value
        record.provider_message_id = provider_message_id
        record.error_message = None
        record.sent_at = datetime.now(timezone.utc)
    except Exception as exc:
        record.status = ExecutiveCampaignStatus.failed.value
        record.error_message = str(exc)[:2000]
    session.add(record)
    session.commit()

    return ExecutiveCampaignAutopilotSendResponse(
        draft_id=record.id,
        status=ExecutiveCampaignStatus(record.status),
        to_email=record.to_email,
        provider_message_id=provider_message_id,
        error_message=record.error_message,
        sent_at=record.sent_at,
    )


def get_campaign_autopilot_draft(session: Session, draft_id: str) -> ExecutiveCampaignAutopilotDraftResponse:
    record = session.get(ExecutiveCampaignDraft, draft_id)
    if not record:
        raise ValueError(f"Executive campaign draft {draft_id} was not found.")
    payload = dict(record.payload_json or {})
    candidates_raw = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    candidates = [ExecutiveCampaignCandidate.model_validate(item) for item in candidates_raw]
    return ExecutiveCampaignAutopilotDraftResponse(
        draft_id=record.id,
        status=ExecutiveCampaignStatus(record.status),
        to_email=record.to_email,
        subject=record.subject,
        body_text=record.body_text,
        lookback_days=int(payload.get("lookback_days", 56)),
        generated_at=record.created_at,
        guardrails=dict(payload.get("guardrails", {})),
        candidates=candidates,
    )
