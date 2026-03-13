from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Order, OrderItem, Product, Store
from app.schemas import (
    MerchAction,
    MerchActionRecommendationItem,
    MerchActionRecommendationsResponse,
    MerchDiagnosticInsight,
    MerchDiagnosticsResponse,
    MerchTrendHighlight,
    MerchTrendSummaryResponse,
    Objective,
    ResolvedStore,
)
from app.services.lookup import resolve_store
from app.services.taxonomy import CATEGORY_TAXONOMY


@dataclass
class MerchQuery:
    objective: Objective
    action_hint: MerchAction | None
    category: str | None
    brand: str | None
    lookback_days: int
    intent: str


_ACTION_TERMS = {
    MerchAction.feature: ["feature", "push", "showcase", "front", "hero"],
    MerchAction.deprioritize: ["deprioritize", "pull back", "reduce", "slow", "underperform"],
    MerchAction.promote: ["promote", "discount", "markdown", "sale", "offer"],
}

_OBJECTIVE_TERMS = {
    Objective.margin: ["margin", "profit", "profitable"],
    Objective.revenue: ["revenue", "sales", "dollars"],
    Objective.sell_through: ["sell through", "sell-through", "velocity", "units"],
}


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.lower().replace("-", " ").replace(",", " ").split())


def parse_merch_query(question: str | None, objective: Objective = Objective.sell_through, lookback_days: int = 90) -> MerchQuery:
    normalized = _normalize(question)
    parsed_objective = objective
    parsed_action: MerchAction | None = None
    category: str | None = None
    brand: str | None = None

    for candidate, terms in _OBJECTIVE_TERMS.items():
        if any(term in normalized for term in terms):
            parsed_objective = candidate
            break

    for candidate, terms in _ACTION_TERMS.items():
        if any(term in normalized for term in terms):
            parsed_action = candidate
            break

    for category_key, cfg in CATEGORY_TAXONOMY.items():
        if category_key.replace("_", " ") in normalized or cfg["label"].lower() in normalized:
            category = category_key
            break

    if "brand " in normalized:
        after = normalized.split("brand ", 1)[1].strip()
        brand = after[:80] if after else None

    if "last " in normalized and " days" in normalized:
        try:
            lookback_days = int(normalized.split("last ", 1)[1].split(" days", 1)[0].strip())
        except Exception:
            pass
    elif "last " in normalized and " weeks" in normalized:
        try:
            lookback_days = int(normalized.split("last ", 1)[1].split(" weeks", 1)[0].strip()) * 7
        except Exception:
            pass
    elif "last " in normalized and " months" in normalized:
        try:
            lookback_days = int(normalized.split("last ", 1)[1].split(" months", 1)[0].strip()) * 30
        except Exception:
            pass

    intent = question or f"{parsed_objective.value} merchandising review"
    return MerchQuery(
        objective=parsed_objective,
        action_hint=parsed_action,
        category=category,
        brand=brand,
        lookback_days=max(7, min(lookback_days, 730)),
        intent=intent,
    )


def _resolved_store(session: Session, store_query: str | None = None, store_id: str | None = None) -> ResolvedStore:
    return resolve_store(session, store_query=store_query, store_id=store_id).resolved


def peer_store_ids(session: Session, store_id: str) -> list[str]:
    store = session.get(Store, store_id)
    if not store:
        raise ValueError(f"Store {store_id} was not found.")

    peers = session.scalars(
        select(Store).where(Store.id != store.id, Store.profile_type == store.profile_type).order_by(Store.id)
    ).all()

    same_state = [peer.id for peer in peers if peer.state == store.state]
    if same_state:
        return same_state[:5]
    if peers:
        return [peer.id for peer in peers[:5]]

    fallback = session.scalars(select(Store).where(Store.id != store.id).order_by(Store.id)).all()
    return [peer.id for peer in fallback[:5]]


def _product_metrics(session: Session, store_ids: list[str], since: datetime, category: str | None = None, brand: str | None = None):
    query = (
        select(
            Product.id,
            Product.store_id,
            Product.title,
            Product.brand,
            Product.category,
            Product.inventory_qty,
            Product.margin_pct,
            Product.objective_weight,
            func.sum(OrderItem.quantity).label("units"),
            func.sum(OrderItem.line_total).label("revenue"),
        )
        .join(OrderItem, OrderItem.product_id == Product.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.store_id.in_(store_ids), Order.ordered_at >= since)
        .group_by(
            Product.id,
            Product.store_id,
            Product.title,
            Product.brand,
            Product.category,
            Product.inventory_qty,
            Product.margin_pct,
            Product.objective_weight,
        )
    )
    if category:
        query = query.where(Product.category == category)
    if brand:
        query = query.where(func.lower(Product.brand) == brand.lower())
    return session.execute(query).all()


def _peer_category_baseline(peer_rows) -> dict[str, dict[str, float]]:
    grouped: dict[str, dict[str, float]] = defaultdict(lambda: {"units": 0.0, "revenue": 0.0, "margin": 0.0, "count": 0.0})
    for row in peer_rows:
        bucket = grouped[row.category]
        bucket["units"] += float(row.units or 0)
        bucket["revenue"] += float(row.revenue or 0)
        bucket["margin"] += float(row.margin_pct or 0)
        bucket["count"] += 1

    baselines: dict[str, dict[str, float]] = {}
    for category, bucket in grouped.items():
        count = max(bucket["count"], 1)
        baselines[category] = {
            "avg_units": bucket["units"] / count,
            "avg_revenue": bucket["revenue"] / count,
            "avg_margin": bucket["margin"] / count,
        }
    return baselines


def merchandising_action_recommendations(
    session: Session,
    store_query: str | None = None,
    store_id: str | None = None,
    question: str | None = None,
    objective: Objective = Objective.sell_through,
    lookback_days: int = 90,
    top_k: int = 9,
) -> MerchActionRecommendationsResponse:
    resolved_store = _resolved_store(session, store_query=store_query, store_id=store_id)
    parsed = parse_merch_query(question, objective=objective, lookback_days=lookback_days)
    since = datetime.now(timezone.utc) - timedelta(days=parsed.lookback_days)
    peers = peer_store_ids(session, resolved_store.id)

    store_rows = _product_metrics(session, [resolved_store.id], since, category=parsed.category, brand=parsed.brand)
    peer_rows = _product_metrics(session, peers, since, category=parsed.category, brand=parsed.brand)
    peer_baselines = _peer_category_baseline(peer_rows)

    recs: list[MerchActionRecommendationItem] = []
    for row in store_rows:
        units = float(row.units or 0)
        revenue = float(row.revenue or 0)
        margin = float(row.margin_pct or 0)
        inventory = float(row.inventory_qty or 0)
        objective_weight = float(row.objective_weight or 0)
        baseline = peer_baselines.get(row.category, {"avg_units": 0.0, "avg_revenue": 0.0, "avg_margin": 0.0})
        peer_delta = revenue - float(baseline["avg_revenue"])

        feature_score = revenue * (0.25 + margin) + units * 10 + objective_weight * 100 + max(peer_delta, 0)
        deprioritize_score = inventory * 4 + max(-peer_delta, 0) + max(0, baseline["avg_units"] - units) * 8
        promote_score = inventory * 3 + margin * 80 + max(0, baseline["avg_revenue"] - revenue)

        action_scores = {
            MerchAction.feature: (feature_score, "strong contribution with supportive peer context"),
            MerchAction.deprioritize: (deprioritize_score, "low productivity relative to inventory and peers"),
            MerchAction.promote: (promote_score, "good margin headroom with room to stimulate demand"),
        }

        selected_actions = [parsed.action_hint] if parsed.action_hint else list(MerchAction)
        for action in selected_actions:
            score, rationale = action_scores[action]
            recs.append(
                MerchActionRecommendationItem(
                    action=action,
                    product_id=row.id,
                    title=row.title,
                    brand=row.brand,
                    category=row.category,
                    metric_value=round(score, 4),
                    peer_delta=round(peer_delta, 4),
                    rationale=rationale,
                )
            )

    recs.sort(key=lambda item: item.metric_value, reverse=True)
    if parsed.action_hint:
        limited = recs[:top_k]
    else:
        grouped: dict[MerchAction, list[MerchActionRecommendationItem]] = defaultdict(list)
        for item in recs:
            if len(grouped[item.action]) < max(2, top_k // 3):
                grouped[item.action].append(item)
        limited = grouped[MerchAction.feature] + grouped[MerchAction.deprioritize] + grouped[MerchAction.promote]

    return MerchActionRecommendationsResponse(
        store=resolved_store,
        objective=parsed.objective,
        lookback_days=parsed.lookback_days,
        peer_store_ids=peers,
        parsed_intent=parsed.intent,
        recommendations=limited,
    )


def merchandising_diagnostics(
    session: Session,
    store_query: str | None = None,
    store_id: str | None = None,
    question: str | None = None,
    lookback_days: int = 90,
) -> MerchDiagnosticsResponse:
    resolved_store = _resolved_store(session, store_query=store_query, store_id=store_id)
    parsed = parse_merch_query(question, lookback_days=lookback_days)
    since = datetime.now(timezone.utc) - timedelta(days=parsed.lookback_days)
    peers = peer_store_ids(session, resolved_store.id)

    store_rows = _product_metrics(session, [resolved_store.id], since)
    peer_rows = _product_metrics(session, peers, since)

    store_category: dict[str, dict[str, float]] = defaultdict(lambda: {"units": 0.0, "revenue": 0.0})
    peer_category: dict[str, dict[str, float]] = defaultdict(lambda: {"units": 0.0, "revenue": 0.0, "count": 0.0})
    store_brand: dict[str, float] = defaultdict(float)
    peer_brand: dict[str, dict[str, float]] = defaultdict(lambda: {"revenue": 0.0, "count": 0.0})

    for row in store_rows:
        store_category[row.category]["units"] += float(row.units or 0)
        store_category[row.category]["revenue"] += float(row.revenue or 0)
        store_brand[row.brand] += float(row.revenue or 0)
    for row in peer_rows:
        peer_category[row.category]["units"] += float(row.units or 0)
        peer_category[row.category]["revenue"] += float(row.revenue or 0)
        peer_category[row.category]["count"] += 1
        peer_brand[row.brand]["revenue"] += float(row.revenue or 0)
        peer_brand[row.brand]["count"] += 1

    insights: list[MerchDiagnosticInsight] = []
    for category, values in store_category.items():
        peer_values = peer_category.get(category, {"units": 0.0, "revenue": 0.0, "count": 1.0})
        peer_avg = peer_values["revenue"] / max(peer_values["count"], 1.0)
        delta = values["revenue"] - peer_avg
        status = "outperforming" if delta >= 0 else "underperforming"
        insights.append(
            MerchDiagnosticInsight(
                dimension="category",
                subject=category,
                status=status,
                current_value=round(values["revenue"], 4),
                peer_value=round(peer_avg, 4),
                delta=round(delta, 4),
                rationale=f"{category} is {status} peer revenue averages over the selected lookback window.",
            )
        )

    for brand, revenue in sorted(store_brand.items(), key=lambda item: item[1], reverse=True)[:5]:
        peer_values = peer_brand.get(brand, {"revenue": 0.0, "count": 1.0})
        peer_avg = peer_values["revenue"] / max(peer_values["count"], 1.0)
        delta = revenue - peer_avg
        status = "outperforming" if delta >= 0 else "underperforming"
        insights.append(
            MerchDiagnosticInsight(
                dimension="brand",
                subject=brand,
                status=status,
                current_value=round(revenue, 4),
                peer_value=round(peer_avg, 4),
                delta=round(delta, 4),
                rationale=f"{brand} is {status} peer brand revenue averages in comparable stores.",
            )
        )

    insights.sort(key=lambda insight: abs(insight.delta), reverse=True)
    summary = f"Diagnostics for {resolved_store.name} over the last {parsed.lookback_days} days with {len(peers)} peer stores."
    return MerchDiagnosticsResponse(
        store=resolved_store,
        lookback_days=parsed.lookback_days,
        peer_store_ids=peers,
        summary=summary,
        insights=insights[:10],
    )


def merchandising_trend_summary(
    session: Session,
    store_query: str | None = None,
    store_id: str | None = None,
    question: str | None = None,
    lookback_days: int = 90,
) -> MerchTrendSummaryResponse:
    resolved_store = _resolved_store(session, store_query=store_query, store_id=store_id)
    parsed = parse_merch_query(question, lookback_days=lookback_days)

    now = datetime.now(timezone.utc)
    current_since = now - timedelta(days=parsed.lookback_days)
    prior_since = current_since - timedelta(days=parsed.lookback_days)

    def _window_rows(start: datetime, end: datetime):
        query = (
            select(Product.category, func.sum(OrderItem.line_total).label("revenue"))
            .join(OrderItem, OrderItem.product_id == Product.id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.store_id == resolved_store.id, Order.ordered_at >= start, Order.ordered_at < end)
            .group_by(Product.category)
        )
        return session.execute(query).all()

    current = {row.category: float(row.revenue or 0) for row in _window_rows(current_since, now)}
    prior = {row.category: float(row.revenue or 0) for row in _window_rows(prior_since, current_since)}

    highlights: list[MerchTrendHighlight] = []
    for category in sorted(set(current) | set(prior)):
        current_value = current.get(category, 0.0)
        prior_value = prior.get(category, 0.0)
        if prior_value <= 0:
            pct_change = 100.0 if current_value > 0 else 0.0
        else:
            pct_change = ((current_value - prior_value) / prior_value) * 100.0
        direction = "up" if pct_change >= 0 else "down"
        highlights.append(
            MerchTrendHighlight(
                subject=category,
                current_value=round(current_value, 4),
                prior_value=round(prior_value, 4),
                pct_change=round(pct_change, 2),
                rationale=f"{category} revenue is {direction} versus the prior comparable period.",
            )
        )

    highlights.sort(key=lambda item: abs(item.pct_change), reverse=True)
    summary = f"Trend summary for {resolved_store.name} covering the last {parsed.lookback_days} days versus the prior period."
    return MerchTrendSummaryResponse(
        store=resolved_store,
        lookback_days=parsed.lookback_days,
        summary=summary,
        highlights=highlights[:8],
    )
