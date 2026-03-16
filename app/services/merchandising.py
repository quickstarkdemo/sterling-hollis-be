from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models import Order, OrderItem, Product, Store
from app.schemas import (
    CompareMode,
    MerchAction,
    MerchActionRecommendationItem,
    MerchActionRecommendationsResponse,
    MerchDiagnosticInsight,
    MerchDiagnosticsResponse,
    MerchTrendHighlight,
    MerchTrendSummaryResponse,
    Objective,
    PeerMode,
    PriceBand,
    ResolvedStore,
)
from app.services.demo_assets import demo_image_url
from app.services.lookup import resolve_store
from app.services.operator_cache import peer_store_cache
from app.services.taxonomy import CATEGORY_TAXONOMY


@dataclass
class MerchQuery:
    objective: Objective
    action_hint: MerchAction | None
    category: str | None
    brand: str | None
    price_band: PriceBand | None
    occasion: str | None
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

_OCCASION_TERMS = ["wedding", "vacation", "workwear", "holiday party", "holiday_party", "everyday luxury", "everyday_luxury"]


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.lower().replace("-", " ").replace(",", " ").split())


def _price_band_bounds(price_band: PriceBand | None) -> tuple[float | None, float | None]:
    if price_band == PriceBand.under_250:
        return None, 250.0
    if price_band == PriceBand.band_250_500:
        return 250.0, 500.0
    if price_band == PriceBand.band_500_1000:
        return 500.0, 1000.0
    if price_band == PriceBand.band_1000_plus:
        return 1000.0, None
    return None, None


def _price_band_for_value(price: float) -> PriceBand:
    if price < 250:
        return PriceBand.under_250
    if price < 500:
        return PriceBand.band_250_500
    if price < 1000:
        return PriceBand.band_500_1000
    return PriceBand.band_1000_plus


def parse_merch_query(
    question: str | None,
    objective: Objective = Objective.sell_through,
    lookback_days: int = 90,
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
) -> MerchQuery:
    normalized = _normalize(question)
    parsed_objective = objective
    parsed_action: MerchAction | None = None
    parsed_category = category
    parsed_brand = brand
    parsed_price_band = price_band
    parsed_occasion = occasion

    for candidate, terms in _OBJECTIVE_TERMS.items():
        if any(term in normalized for term in terms):
            parsed_objective = candidate
            break

    for candidate, terms in _ACTION_TERMS.items():
        if any(term in normalized for term in terms):
            parsed_action = candidate
            break

    if parsed_category is None:
        for category_key, cfg in CATEGORY_TAXONOMY.items():
            if category_key.replace("_", " ") in normalized or cfg["label"].lower() in normalized:
                parsed_category = category_key
                break

    if parsed_brand is None and "brand " in normalized:
        after = normalized.split("brand ", 1)[1].strip()
        parsed_brand = after[:80] if after else None

    if parsed_price_band is None:
        if "under 250" in normalized or "under $250" in normalized:
            parsed_price_band = PriceBand.under_250
        elif "250 to 500" in normalized or "$250 to $500" in normalized or "250-500" in normalized:
            parsed_price_band = PriceBand.band_250_500
        elif "500 to 1000" in normalized or "$500 to $1000" in normalized or "500-1000" in normalized:
            parsed_price_band = PriceBand.band_500_1000
        elif "over 1000" in normalized or "over $1000" in normalized or "1000 plus" in normalized:
            parsed_price_band = PriceBand.band_1000_plus

    if parsed_occasion is None:
        for occasion_term in _OCCASION_TERMS:
            if occasion_term in normalized:
                parsed_occasion = occasion_term.replace(" ", "_")
                break

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
        category=parsed_category,
        brand=parsed_brand,
        price_band=parsed_price_band,
        occasion=parsed_occasion,
        lookback_days=max(7, min(lookback_days, 730)),
        intent=intent,
    )


def _resolved_store(session: Session, store_query: str | None = None, store_id: str | None = None) -> ResolvedStore:
    return resolve_store(session, store_query=store_query, store_id=store_id).resolved


def peer_store_ids(session: Session, store_id: str, peer_mode: PeerMode = PeerMode.state_and_profile) -> list[str]:
    cache_key = (store_id, peer_mode.value)
    cached = peer_store_cache.get(cache_key)
    if cached is not None:
        return list(cached)

    store = session.get(Store, store_id)
    if not store:
        raise ValueError(f"Store {store_id} was not found.")

    if peer_mode == PeerMode.state_and_profile:
        peers = session.scalars(
            select(Store)
            .where(Store.id != store.id, Store.profile_type == store.profile_type, Store.state == store.state)
            .order_by(Store.id)
        ).all()
        if peers:
            result = [peer.id for peer in peers[:5]]
            peer_store_cache.set(cache_key, list(result))
            return result

    if peer_mode in {PeerMode.state_and_profile, PeerMode.profile_type}:
        peers = session.scalars(
            select(Store).where(Store.id != store.id, Store.profile_type == store.profile_type).order_by(Store.id)
        ).all()
        if peers:
            result = [peer.id for peer in peers[:5]]
            peer_store_cache.set(cache_key, list(result))
            return result

    peers = session.scalars(select(Store).where(Store.id != store.id).order_by(Store.id)).all()
    result = [peer.id for peer in peers[:5]]
    peer_store_cache.set(cache_key, list(result))
    return result


def _base_query(
    store_ids: list[str],
    since: datetime,
    until: datetime,
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
):
    query = (
        select(
            Product.id,
            Product.store_id,
            Product.title,
            Product.brand,
            Product.category,
            Product.link,
            Product.price,
            Product.inventory_qty,
            Product.margin_pct,
            Product.objective_weight,
            func.sum(OrderItem.quantity).label("units"),
            func.sum(OrderItem.line_total).label("revenue"),
        )
        .join(OrderItem, OrderItem.product_id == Product.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.store_id.in_(store_ids),
            Order.ordered_at >= since,
            Order.ordered_at < until,
        )
        .group_by(
            Product.id,
            Product.store_id,
            Product.title,
            Product.brand,
            Product.category,
            Product.link,
            Product.price,
            Product.inventory_qty,
            Product.margin_pct,
            Product.objective_weight,
        )
    )
    if category:
        query = query.where(Product.category == category)
    if brand:
        query = query.where(func.lower(Product.brand) == brand.lower())
    if occasion:
        query = query.where(Order.occasion == occasion)
    floor, ceiling = _price_band_bounds(price_band)
    if floor is not None:
        query = query.where(Product.price >= floor)
    if ceiling is not None:
        query = query.where(Product.price < ceiling)
    return query


def _product_metrics(
    session: Session,
    store_ids: list[str],
    since: datetime,
    until: datetime,
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
):
    return session.execute(_base_query(store_ids, since, until, category, brand, price_band, occasion)).all()


def _dimension_aggregate_query(
    store_ids: list[str],
    since: datetime,
    until: datetime,
    dimension: str,
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
):
    if dimension == "brand":
        dim_col = Product.brand.label("subject")
    else:
        dim_col = Product.category.label("subject")
    query = (
        select(
            dim_col,
            func.sum(OrderItem.quantity).label("units"),
            func.sum(OrderItem.line_total).label("revenue"),
            func.avg(Product.margin_pct).label("margin_pct"),
        )
        .join(OrderItem, OrderItem.product_id == Product.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.store_id.in_(store_ids), Order.ordered_at >= since, Order.ordered_at < until)
        .group_by(dim_col)
    )
    if category:
        query = query.where(Product.category == category)
    if brand:
        query = query.where(func.lower(Product.brand) == brand.lower())
    if occasion:
        query = query.where(Order.occasion == occasion)
    floor, ceiling = _price_band_bounds(price_band)
    if floor is not None:
        query = query.where(Product.price >= floor)
    if ceiling is not None:
        query = query.where(Product.price < ceiling)
    return query


def _dimension_aggregates(
    session: Session,
    store_ids: list[str],
    since: datetime,
    until: datetime,
    dimension: str,
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
):
    return session.execute(
        _dimension_aggregate_query(store_ids, since, until, dimension, category, brand, price_band, occasion)
    ).all()


def _by_key(rows, key_fn, value_fn):
    mapped = {}
    for row in rows:
        mapped[key_fn(row)] = value_fn(row)
    return mapped


def _select_metric_value(units: float, revenue: float, margin: float, objective_weight: float, objective: Objective) -> float:
    if objective == Objective.margin:
        return revenue * margin + objective_weight * 100
    if objective == Objective.revenue:
        return revenue + objective_weight * 25
    return units * 12 + objective_weight * 100


def merchandising_action_recommendations(
    session: Session,
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
    resolved_store = _resolved_store(session, store_query=store_query, store_id=store_id)
    parsed = parse_merch_query(
        question,
        objective=objective,
        lookback_days=lookback_days,
        category=category,
        brand=brand,
        price_band=price_band,
        occasion=occasion,
    )
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=parsed.lookback_days)
    prior_since = since - timedelta(days=parsed.lookback_days)
    peers = peer_store_ids(session, resolved_store.id, peer_mode=peer_mode)

    store_rows = _product_metrics(session, [resolved_store.id], since, now, parsed.category, parsed.brand, parsed.price_band, parsed.occasion)
    peer_rows = _product_metrics(session, peers, since, now, parsed.category, parsed.brand, parsed.price_band, parsed.occasion)
    prior_rows = _product_metrics(session, [resolved_store.id], prior_since, since, parsed.category, parsed.brand, parsed.price_band, parsed.occasion)

    peer_baselines = defaultdict(lambda: {"revenue": 0.0, "units": 0.0, "count": 0})
    for row in peer_rows:
        key = (row.category, row.brand)
        peer_baselines[key]["revenue"] += float(row.revenue or 0.0)
        peer_baselines[key]["units"] += float(row.units or 0.0)
        peer_baselines[key]["count"] += 1

    prior_by_product = _by_key(prior_rows, lambda row: row.id, lambda row: float(row.revenue or 0.0))

    recs: list[MerchActionRecommendationItem] = []
    action_order = [MerchAction.feature, MerchAction.promote, MerchAction.deprioritize]
    for row in store_rows:
        units = float(row.units or 0.0)
        revenue = float(row.revenue or 0.0)
        margin = float(row.margin_pct or 0.0)
        inventory = float(row.inventory_qty or 0.0)
        objective_weight = float(row.objective_weight or 0.0)
        peer_group = peer_baselines[(row.category, row.brand)]
        peer_avg_revenue = (
            peer_group["revenue"] / peer_group["count"] if peer_group["count"] else 0.0
        )
        peer_delta = revenue - peer_avg_revenue if compare_mode in {CompareMode.peer, CompareMode.peer_and_prior_period} else 0.0
        prior_delta = revenue - prior_by_product.get(row.id, 0.0) if compare_mode in {CompareMode.prior_period, CompareMode.peer_and_prior_period} else 0.0

        metric_value = _select_metric_value(units, revenue, margin, objective_weight, parsed.objective)
        positive_signal = max(peer_delta, 0.0) + max(prior_delta, 0.0)
        negative_signal = max(-peer_delta, 0.0) + max(-prior_delta, 0.0)

        # Distinct action intent:
        # - feature: full-price visibility for demand winners
        # - promote: campaign/offer candidates with inventory + margin headroom
        # - deprioritize: reduced exposure for weak movers with inventory pressure
        feature_score = metric_value + positive_signal * 1.2 - negative_signal * 0.5
        promote_score = (margin * 80) + inventory * 2.5 + negative_signal - positive_signal * 0.8
        deprioritize_score = inventory * 4 + negative_signal * 1.3 - (margin * 40) - positive_signal * 0.9

        action_scores = {
            MerchAction.feature: (
                feature_score,
                "strong demand and positive peer/prior signals support full-price featuring",
            ),
            MerchAction.promote: (
                promote_score,
                "inventory headroom with margin potential suggests a campaign or offer",
            ),
            MerchAction.deprioritize: (
                deprioritize_score,
                "weaker demand versus peer/prior with inventory pressure suggests lower floor priority",
            ),
        }

        selected_actions = [parsed.action_hint] if parsed.action_hint else None
        if selected_actions is None:
            best_action = max(
                action_order,
                key=lambda action: action_scores[action][0],
            )
            selected_actions = [best_action]

        for action in selected_actions:
            score, rationale = action_scores[action]
            recs.append(
                MerchActionRecommendationItem(
                    action=action,
                    product_id=row.id,
                    title=row.title,
                    brand=row.brand,
                    category=row.category,
                    price=round(float(row.price or 0.0), 2),
                    link=row.link,
                    image_url=demo_image_url(row.category, row.id, variant_hint=row.brand),
                    price_band=_price_band_for_value(float(row.price or 0.0)),
                    occasion=parsed.occasion,
                    metric_value=round(score, 4),
                    peer_delta=round(peer_delta, 4),
                    prior_period_delta=round(prior_delta, 4),
                    rationale=rationale,
                )
            )

    recs.sort(key=lambda item: item.metric_value, reverse=True)
    if parsed.action_hint:
        limited = recs[:top_k]
    else:
        grouped: dict[MerchAction, list[MerchActionRecommendationItem]] = defaultdict(list)
        for item in recs:
            grouped[item.action].append(item)

        limited: list[MerchActionRecommendationItem] = []
        idx = 0
        while len(limited) < top_k:
            appended = False
            for action in action_order:
                bucket = grouped[action]
                if idx < len(bucket):
                    limited.append(bucket[idx])
                    appended = True
                    if len(limited) >= top_k:
                        break
            if not appended:
                break
            idx += 1

    return MerchActionRecommendationsResponse(
        store=resolved_store,
        objective=parsed.objective,
        compare_mode=compare_mode,
        peer_mode=peer_mode,
        lookback_days=parsed.lookback_days,
        category=parsed.category,
        brand=parsed.brand,
        price_band=parsed.price_band,
        occasion=parsed.occasion,
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
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
) -> MerchDiagnosticsResponse:
    resolved_store = _resolved_store(session, store_query=store_query, store_id=store_id)
    parsed = parse_merch_query(
        question,
        objective=Objective.sell_through,
        lookback_days=lookback_days,
        category=category,
        brand=brand,
        price_band=price_band,
        occasion=occasion,
    )
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=parsed.lookback_days)
    prior_since = since - timedelta(days=parsed.lookback_days)
    peers = peer_store_ids(session, resolved_store.id, peer_mode=peer_mode)

    dimension = "brand" if parsed.brand else "category"
    current_rows = _dimension_aggregates(session, [resolved_store.id], since, now, dimension, parsed.category, parsed.brand, parsed.price_band, parsed.occasion)
    peer_rows = _dimension_aggregates(session, peers, since, now, dimension, parsed.category, parsed.brand, parsed.price_band, parsed.occasion)
    prior_rows = _dimension_aggregates(session, [resolved_store.id], prior_since, since, dimension, parsed.category, parsed.brand, parsed.price_band, parsed.occasion)

    peer_by_subject = _by_key(peer_rows, lambda row: row.subject, lambda row: float(row.revenue or 0.0))
    prior_by_subject = _by_key(prior_rows, lambda row: row.subject, lambda row: float(row.revenue or 0.0))

    insights: list[MerchDiagnosticInsight] = []
    for row in current_rows:
        current_value = float(row.revenue or 0.0)
        peer_value = peer_by_subject.get(row.subject) if compare_mode in {CompareMode.peer, CompareMode.peer_and_prior_period} else None
        prior_value = prior_by_subject.get(row.subject) if compare_mode in {CompareMode.prior_period, CompareMode.peer_and_prior_period} else None
        if peer_value is not None:
            delta = current_value - peer_value
            status = "overperforming" if delta >= 0 else "underperforming"
            rationale = f"Current revenue is {'above' if delta >= 0 else 'below'} peer average."
        elif prior_value is not None:
            delta = current_value - prior_value
            status = "improving" if delta >= 0 else "declining"
            rationale = f"Current revenue is {'above' if delta >= 0 else 'below'} the prior period."
        else:
            delta = current_value
            status = "observed"
            rationale = "Current period observation."
        insights.append(
            MerchDiagnosticInsight(
                dimension=dimension,
                subject=row.subject,
                status=status,
                current_value=round(current_value, 4),
                peer_value=round(peer_value, 4) if peer_value is not None else None,
                prior_value=round(prior_value, 4) if prior_value is not None else None,
                delta=round(delta, 4),
                rationale=rationale,
            )
        )

    insights.sort(key=lambda item: abs(item.delta), reverse=True)
    summary = (
        f"Diagnostics for {resolved_store.name} over the last {parsed.lookback_days} days with {dimension}-level comparison."
    )
    return MerchDiagnosticsResponse(
        store=resolved_store,
        compare_mode=compare_mode,
        peer_mode=peer_mode,
        lookback_days=parsed.lookback_days,
        category=parsed.category,
        brand=parsed.brand,
        price_band=parsed.price_band,
        occasion=parsed.occasion,
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
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
) -> MerchTrendSummaryResponse:
    resolved_store = _resolved_store(session, store_query=store_query, store_id=store_id)
    parsed = parse_merch_query(
        question,
        objective=Objective.sell_through,
        lookback_days=lookback_days,
        category=category,
        brand=brand,
        price_band=price_band,
        occasion=occasion,
    )
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=parsed.lookback_days)
    prior_since = since - timedelta(days=parsed.lookback_days)
    peers = peer_store_ids(session, resolved_store.id, peer_mode=peer_mode)

    dimension = "brand" if parsed.brand else "category"
    current_rows = _dimension_aggregates(session, [resolved_store.id], since, now, dimension, parsed.category, parsed.brand, parsed.price_band, parsed.occasion)
    peer_rows = _dimension_aggregates(session, peers, since, now, dimension, parsed.category, parsed.brand, parsed.price_band, parsed.occasion)
    prior_rows = _dimension_aggregates(session, [resolved_store.id], prior_since, since, dimension, parsed.category, parsed.brand, parsed.price_band, parsed.occasion)

    peer_by_subject = _by_key(peer_rows, lambda row: row.subject, lambda row: float(row.revenue or 0.0))
    prior_by_subject = _by_key(prior_rows, lambda row: row.subject, lambda row: float(row.revenue or 0.0))

    highlights: list[MerchTrendHighlight] = []
    for row in current_rows:
        current_value = float(row.revenue or 0.0)
        prior_value = prior_by_subject.get(row.subject) if compare_mode in {CompareMode.prior_period, CompareMode.peer_and_prior_period} else None
        peer_value = peer_by_subject.get(row.subject) if compare_mode in {CompareMode.peer, CompareMode.peer_and_prior_period} else None
        baseline = prior_value if prior_value is not None else peer_value if peer_value is not None else 0.0
        pct_change = ((current_value - baseline) / baseline * 100.0) if baseline else 0.0
        rationale = "Change versus peer baseline." if prior_value is None else "Change versus prior period."
        highlights.append(
            MerchTrendHighlight(
                subject=row.subject,
                current_value=round(current_value, 4),
                peer_value=round(peer_value, 4) if peer_value is not None else None,
                prior_value=round(prior_value, 4) if prior_value is not None else None,
                pct_change=round(pct_change, 4),
                rationale=rationale,
            )
        )

    highlights.sort(key=lambda item: abs(item.pct_change), reverse=True)
    summary = f"Trend summary for {resolved_store.name} over the last {parsed.lookback_days} days."
    return MerchTrendSummaryResponse(
        store=resolved_store,
        compare_mode=compare_mode,
        peer_mode=peer_mode,
        lookback_days=parsed.lookback_days,
        category=parsed.category,
        brand=parsed.brand,
        price_band=parsed.price_band,
        occasion=parsed.occasion,
        summary=summary,
        highlights=highlights[:10],
    )
