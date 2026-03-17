from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ExecutiveCampaignDraft, Order, OrderItem, Product, Store, StoreDailyMetric
from app.schemas import (
    ExecutiveCampaignAction,
    ExecutiveCampaignAutopilotDraftResponse,
    ExecutiveCampaignAutopilotSendResponse,
    ExecutiveCampaignCandidate,
    ExecutiveCampaignStatus,
    ExecutiveEventReadinessRadarResponse,
    ExecutiveOverviewResponse,
    ExecutiveReadinessRecommendation,
    ExecutiveReadinessRow,
    ExecutiveRiskLevel,
    ExecutiveStoreInsight,
    ExecutiveTrendPoint,
    ExecutiveWhatIfComponent,
    ExecutiveWhatIfSimulatorResponse,
    Objective,
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
) -> dict[str, float]:
    if not store_ids:
        return {}
    query = (
        select(Product.store_id.label("store_id"), func.sum(Product.inventory_qty).label("units"))
        .where(Product.store_id.in_(store_ids), Product.category.in_(categories))
        .group_by(Product.store_id)
    )
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
    rows = session.execute(
        select(StoreDailyMetric.metric_date, StoreDailyMetric.revenue, StoreDailyMetric.units_sold, StoreDailyMetric.margin_rate).where(
            StoreDailyMetric.store_id.in_(store_ids),
            StoreDailyMetric.metric_date >= since.date(),
            StoreDailyMetric.metric_date < until.date(),
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
    scope_label = resolved.name if resolved is not None else "company-wide network"
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
) -> ExecutiveEventReadinessRadarResponse:
    bounded_lookback = _bounded_lookback(lookback_days)
    normalized_events = _normalized_events(events)
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
        )
        prior = _store_sales(
            session,
            store_ids=store_ids,
            since=prior_since,
            until=since,
            occasion=event,
            categories=categories,
        )
        inventory = _inventory_units_by_store(session, store_ids=store_ids, categories=categories)

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
    scope_label = resolved.name if resolved is not None else "company-wide network"
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
) -> dict[str, dict[str, float]]:
    if not store_ids:
        return {}
    rows = session.execute(
        select(
            Product.category.label("category"),
            func.sum(OrderItem.line_total).label("revenue"),
            func.sum(OrderItem.quantity).label("units"),
            func.sum(OrderItem.line_total * Product.margin_pct).label("margin_value"),
        )
        .join(OrderItem, OrderItem.product_id == Product.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.store_id.in_(store_ids), Order.ordered_at >= since, Order.ordered_at < until)
        .group_by(Product.category)
    ).all()
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
) -> dict[str, float]:
    if not store_ids:
        return {}
    rows = session.execute(
        select(Product.category.label("category"), func.sum(Product.inventory_qty).label("inventory_units"))
        .where(Product.store_id.in_(store_ids))
        .group_by(Product.category)
    ).all()
    return {row.category: float(row.inventory_units or 0.0) for row in rows}


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
) -> ExecutiveWhatIfSimulatorResponse:
    bounded_lookback = _bounded_lookback(lookback_days)
    bounded_discount = max(0.0, min(float(discount_pct), 60.0))
    bounded_shift = max(-40.0, min(float(floor_space_shift_pct), 40.0))

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=bounded_lookback)
    store_ids, resolved = _resolved_scope(session, store_query=store_query, store_id=store_id, store_ids=store_ids)

    sales = _category_sales(session, store_ids=store_ids, since=since, until=now)
    inventory = _category_inventory(session, store_ids=store_ids)
    baseline_revenue = sum(row["revenue"] for row in sales.values())
    baseline_margin_value = sum(row["margin_value"] for row in sales.values())
    baseline_margin_rate = (baseline_margin_value / baseline_revenue) if baseline_revenue > 0 else 0.0

    discount_component_revenue = 0.0
    discount_component_margin_value = 0.0
    if bounded_discount > 0:
        discount_fraction = bounded_discount / 100.0
        target_category = from_category if from_category in sales else None
        base_revenue = sales[target_category]["revenue"] if target_category else baseline_revenue
        base_margin_rate = sales[target_category]["margin_rate"] if target_category else baseline_margin_rate
        elasticity = 1.6
        volume_lift = elasticity * discount_fraction
        discounted_revenue = base_revenue * (1.0 - discount_fraction) * (1.0 + volume_lift)
        discount_component_revenue = discounted_revenue - base_revenue
        adjusted_margin_rate = max(0.0, base_margin_rate - discount_fraction * 0.9)
        discount_component_margin_value = (discounted_revenue * adjusted_margin_rate) - (base_revenue * base_margin_rate)

    space_component_revenue = 0.0
    space_component_margin_value = 0.0
    if (
        bounded_shift != 0
        and from_category
        and to_category
        and from_category != to_category
        and from_category in sales
        and to_category in sales
    ):
        direction = 1.0
        src_category = from_category
        dst_category = to_category
        if bounded_shift < 0:
            direction = -1.0
            src_category = to_category
            dst_category = from_category

        shift_fraction = abs(bounded_shift) / 100.0
        src_revenue = sales[src_category]["revenue"]
        src_margin_rate = sales[src_category]["margin_rate"]
        dst_margin_rate = sales[dst_category]["margin_rate"]
        src_inventory = max(inventory.get(src_category, 0.0), 1.0)
        dst_inventory = max(inventory.get(dst_category, 0.0), 1.0)
        src_productivity = src_revenue / src_inventory
        dst_productivity = sales[dst_category]["revenue"] / dst_inventory

        realloc_revenue_base = src_revenue * shift_fraction
        productivity_delta = (dst_productivity - src_productivity) / max(src_productivity, 1e-6)
        # Apply only a portion of measured productivity spread to keep v1 conservative.
        space_component_revenue = direction * realloc_revenue_base * productivity_delta * 0.35
        effective_margin_rate = (dst_margin_rate - src_margin_rate) * 0.6 + src_margin_rate
        space_component_margin_value = space_component_revenue * effective_margin_rate

    expected_revenue = baseline_revenue + discount_component_revenue + space_component_revenue
    expected_margin_value = baseline_margin_value + discount_component_margin_value + space_component_margin_value
    expected_margin_rate = (expected_margin_value / expected_revenue) if expected_revenue > 0 else 0.0

    revenue_delta = expected_revenue - baseline_revenue
    margin_rate_delta = expected_margin_rate - baseline_margin_rate
    uncertainty = min(0.22, 0.06 + (bounded_discount * 0.003) + (abs(bounded_shift) * 0.0025))
    confidence_low = expected_revenue * (1.0 - uncertainty)
    confidence_high = expected_revenue * (1.0 + uncertainty)

    scope_label = resolved.name if resolved is not None else "company-wide network"
    summary = (
        f"What-if simulation for {scope_label} over the last {bounded_lookback} days "
        f"using discount and category-exposure proxy assumptions."
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
        components=[
            ExecutiveWhatIfComponent(
                name="discount",
                revenue_delta=round(discount_component_revenue, 4),
                margin_rate_delta=round(discount_component_margin_value / baseline_revenue, 4) if baseline_revenue else 0.0,
                rationale="Elasticity-based demand lift offset by price reduction.",
            ),
            ExecutiveWhatIfComponent(
                name="floor_space_proxy",
                revenue_delta=round(space_component_revenue, 4),
                margin_rate_delta=round(space_component_margin_value / baseline_revenue, 4) if baseline_revenue else 0.0,
                rationale="Category exposure shift estimated from revenue-per-inventory productivity spread.",
            ),
        ],
    )


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
