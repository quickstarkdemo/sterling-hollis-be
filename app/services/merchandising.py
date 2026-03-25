from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import re

from sqlalchemy import func, select
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
    MerchTrendPoint,
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
    occasions: list[str]
    lookback_days: int
    intent: str


_ACTION_TERMS = {
    MerchAction.feature: ["feature", "push", "showcase", "front", "hero"],
    MerchAction.deprioritize: ["deprioritize", "pull back", "reduce", "slow", "underperform"],
    MerchAction.promote: ["promote", "campaign", "featured campaign", "discount", "markdown", "sale", "offer"],
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


def _normalize_occasion_token(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    token = re.sub(r"[\s\-]+", "_", raw)
    token = re.sub(r"[^a-z0-9_]", "", token).strip("_")
    return token


def _normalized_occasion_tokens(
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
    objective: Objective | None = Objective.sell_through,
    lookback_days: int | None = 90,
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasion: str | None = None,
    occasions: list[str] | None = None,
) -> MerchQuery:
    normalized = _normalize(question)
    parsed_objective = objective or Objective.sell_through
    parsed_action: MerchAction | None = None
    parsed_category = category
    parsed_brand = brand
    parsed_price_band = price_band
    parsed_occasions = _normalized_occasion_tokens(occasion=occasion, occasions=occasions)
    parsed_occasion = parsed_occasions[0] if parsed_occasions else None

    if objective is None:
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

    if not parsed_occasions and parsed_occasion is None:
        for occasion_term in _OCCASION_TERMS:
            if occasion_term in normalized:
                parsed_occasion = occasion_term.replace(" ", "_")
                parsed_occasions = [parsed_occasion]
                break

    resolved_lookback = lookback_days if lookback_days is not None else 90
    if lookback_days is None:
        if "last " in normalized and " days" in normalized:
            try:
                resolved_lookback = int(normalized.split("last ", 1)[1].split(" days", 1)[0].strip())
            except Exception:
                pass
        elif "last " in normalized and " weeks" in normalized:
            try:
                resolved_lookback = int(normalized.split("last ", 1)[1].split(" weeks", 1)[0].strip()) * 7
            except Exception:
                pass
        elif "last " in normalized and " months" in normalized:
            try:
                resolved_lookback = int(normalized.split("last ", 1)[1].split(" months", 1)[0].strip()) * 30
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
        occasions=parsed_occasions,
        lookback_days=max(7, min(resolved_lookback, 730)),
        intent=intent,
    )


def _brand_filters(brand: str | None) -> list[str]:
    if not brand:
        return []
    tokens: list[str] = []
    for chunk in str(brand).replace(";", ",").replace("|", ",").split(","):
        value = chunk.strip().lower()
        if not value:
            continue
        if value not in tokens:
            tokens.append(value)
    return tokens


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


def _comparison_peers(
    session: Session,
    *,
    store_id: str,
    peer_mode: PeerMode,
    compare_store_id: str | None = None,
) -> tuple[list[str], str | None, str | None]:
    explicit_peer_ids: list[str] = []
    for chunk in str(compare_store_id or "").replace(";", ",").replace("|", ",").split(","):
        candidate = chunk.strip()
        if not candidate or candidate == store_id or candidate in explicit_peer_ids:
            continue
        explicit_peer_ids.append(candidate)
    if explicit_peer_ids:
        explicit_peers = session.scalars(select(Store).where(Store.id.in_(explicit_peer_ids))).all()
        peer_by_id = {peer.id: peer for peer in explicit_peers}
        missing_peer_ids = [peer_id for peer_id in explicit_peer_ids if peer_id not in peer_by_id]
        if missing_peer_ids:
            if len(missing_peer_ids) == 1:
                raise ValueError(f"Compare store {missing_peer_ids[0]} was not found.")
            raise ValueError(f"Compare stores {', '.join(missing_peer_ids)} were not found.")
        peer_names = [peer_by_id[peer_id].name for peer_id in explicit_peer_ids]
        compare_label = peer_names[0] if len(peer_names) == 1 else f"{peer_names[0]} +{len(peer_names) - 1}"
        return explicit_peer_ids, explicit_peer_ids[0], compare_label

    peers = peer_store_ids(session, store_id, peer_mode=peer_mode)
    return peers, None, None


def _base_query(
    store_ids: list[str],
    since: datetime,
    until: datetime,
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasions: list[str] | None = None,
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
    brand_values = _brand_filters(brand)
    if brand_values:
        query = query.where(func.lower(Product.brand).in_(brand_values))
    normalized_occasions = _normalized_occasion_tokens(occasions=occasions)
    if normalized_occasions:
        normalized_occasion_col = func.replace(func.replace(func.lower(func.trim(Order.occasion)), "-", "_"), " ", "_")
        query = query.where(normalized_occasion_col.in_(normalized_occasions))
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
    occasions: list[str] | None = None,
):
    return session.execute(_base_query(store_ids, since, until, category, brand, price_band, occasions)).all()


def _dimension_aggregate_query(
    store_ids: list[str],
    since: datetime,
    until: datetime,
    dimension: str,
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasions: list[str] | None = None,
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
    brand_values = _brand_filters(brand)
    if brand_values:
        query = query.where(func.lower(Product.brand).in_(brand_values))
    normalized_occasions = _normalized_occasion_tokens(occasions=occasions)
    if normalized_occasions:
        normalized_occasion_col = func.replace(func.replace(func.lower(func.trim(Order.occasion)), "-", "_"), " ", "_")
        query = query.where(normalized_occasion_col.in_(normalized_occasions))
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
    occasions: list[str] | None = None,
):
    return session.execute(
        _dimension_aggregate_query(store_ids, since, until, dimension, category, brand, price_band, occasions)
    ).all()


def _sales_events(
    session: Session,
    store_ids: list[str],
    since: datetime,
    until: datetime,
    category: str | None = None,
    brand: str | None = None,
    price_band: PriceBand | None = None,
    occasions: list[str] | None = None,
):
    query = (
        select(
            Order.store_id.label("store_id"),
            Order.ordered_at.label("ordered_at"),
            OrderItem.quantity.label("quantity"),
            OrderItem.line_total.label("line_total"),
        )
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(Order.store_id.in_(store_ids), Order.ordered_at >= since, Order.ordered_at < until)
    )
    if category:
        query = query.where(Product.category == category)
    brand_values = _brand_filters(brand)
    if brand_values:
        query = query.where(func.lower(Product.brand).in_(brand_values))
    normalized_occasions = _normalized_occasion_tokens(occasions=occasions)
    if normalized_occasions:
        normalized_occasion_col = func.replace(func.replace(func.lower(func.trim(Order.occasion)), "-", "_"), " ", "_")
        query = query.where(normalized_occasion_col.in_(normalized_occasions))
    floor, ceiling = _price_band_bounds(price_band)
    if floor is not None:
        query = query.where(Product.price >= floor)
    if ceiling is not None:
        query = query.where(Product.price < ceiling)
    return session.execute(query).all()


def _weekly_rollup(
    rows,
    *,
    origin: datetime,
    horizon_days: int,
    divide_by: float = 1.0,
) -> dict[int, dict[str, float | str]]:
    buckets: dict[int, dict[str, float | str]] = {}
    for row in rows:
        ordered_at = row.ordered_at
        if isinstance(ordered_at, str):
            ordered_at = datetime.fromisoformat(ordered_at.replace("Z", "+00:00"))
        day_offset = (ordered_at.date() - origin.date()).days
        if day_offset < 0:
            continue
        idx = day_offset // 7
        bucket_start = origin + timedelta(days=idx * 7)
        entry = buckets.setdefault(
            idx,
            {
                "period_start": bucket_start.date().isoformat(),
                "revenue": 0.0,
                "units": 0.0,
            },
        )
        entry["revenue"] = float(entry["revenue"]) + (float(row.line_total or 0.0) / max(divide_by, 1.0))
        entry["units"] = float(entry["units"]) + (float(row.quantity or 0.0) / max(divide_by, 1.0))

    max_idx = max(0, math.ceil(horizon_days / 7) - 1)
    for idx in range(max_idx + 1):
        if idx in buckets:
            continue
        bucket_start = origin + timedelta(days=idx * 7)
        buckets[idx] = {"period_start": bucket_start.date().isoformat(), "revenue": 0.0, "units": 0.0}
    return buckets


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
    occasions: list[str] | None = None,
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
    compare_store_id: str | None = None,
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
        occasions=occasions,
    )
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=parsed.lookback_days)
    prior_since = since - timedelta(days=parsed.lookback_days)
    peer_ids: list[str] = []
    resolved_compare_store_id: str | None = None
    resolved_compare_store_name: str | None = None
    if compare_mode in {CompareMode.peer, CompareMode.peer_and_prior_period}:
        peer_ids, resolved_compare_store_id, resolved_compare_store_name = _comparison_peers(
            session,
            store_id=resolved_store.id,
            peer_mode=peer_mode,
            compare_store_id=compare_store_id,
        )

    store_rows = _product_metrics(session, [resolved_store.id], since, now, parsed.category, parsed.brand, parsed.price_band, parsed.occasions)
    peer_rows = (
        _product_metrics(session, peer_ids, since, now, parsed.category, parsed.brand, parsed.price_band, parsed.occasions)
        if peer_ids
        else []
    )
    prior_rows = _product_metrics(session, [resolved_store.id], prior_since, since, parsed.category, parsed.brand, parsed.price_band, parsed.occasions)

    peer_baselines = defaultdict(lambda: {"revenue": 0.0, "units": 0.0, "count": 0})
    for row in peer_rows:
        key = (row.category, row.brand)
        peer_baselines[key]["revenue"] += float(row.revenue or 0.0)
        peer_baselines[key]["units"] += float(row.units or 0.0)
        peer_baselines[key]["count"] += 1

    prior_by_product = _by_key(prior_rows, lambda row: row.id, lambda row: float(row.revenue or 0.0))

    recs: list[MerchActionRecommendationItem] = []
    action_order = [MerchAction.feature, MerchAction.promote, MerchAction.deprioritize]
    campaign_margin_min = 0.42
    campaign_inventory_min = 6.0
    deprioritize_negative_floor = 150.0
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
        # - feature: demand winners suitable for full-price visibility.
        # - promote: featured campaign candidates with margin + inventory headroom.
        # - deprioritize: weaker movers with inventory pressure.
        feature_score = metric_value + (positive_signal * 1.05) - (negative_signal * 0.35) + (margin * 35)
        campaign_pressure = negative_signal - (positive_signal * 0.55)
        promote_score = (margin * 120) + (inventory * 3.2) + max(campaign_pressure, 0.0) * 0.7
        deprioritize_score = (inventory * 4.2) + (negative_signal * 1.25) - (margin * 28) - (positive_signal * 0.6)

        is_deprioritize = inventory >= 4 and negative_signal > max(positive_signal * 1.15, deprioritize_negative_floor)
        is_campaign = (
            margin >= campaign_margin_min
            and inventory >= campaign_inventory_min
            and (negative_signal > positive_signal * 0.55 or positive_signal < 250)
        )

        comparison_bits: list[str] = []
        if compare_mode in {CompareMode.peer, CompareMode.peer_and_prior_period}:
            comparison_bits.append(f"peer {peer_delta:+.0f}")
        if compare_mode in {CompareMode.prior_period, CompareMode.peer_and_prior_period}:
            comparison_bits.append(f"prior {prior_delta:+.0f}")
        baseline_signal = ", ".join(comparison_bits) if comparison_bits else "baseline n/a"

        action_scores = {
            MerchAction.feature: (
                feature_score,
                f"Criteria: strongest demand momentum ({baseline_signal}), margin {margin * 100:.1f}%, inventory {inventory:.0f} units, and full-price visibility potential.",
            ),
            MerchAction.promote: (
                promote_score,
                f"Criteria: Featured Campaign candidate with margin {margin * 100:.1f}% (>= {campaign_margin_min * 100:.0f}%), inventory {inventory:.0f} units (>= {campaign_inventory_min:.0f}), softer demand signal ({baseline_signal}), and room for a campaign or offer.",
            ),
            MerchAction.deprioritize: (
                deprioritize_score,
                f"Criteria: inventory pressure ({inventory:.0f} units) with below-baseline demand ({baseline_signal}); reduce floor priority.",
            ),
        }

        selected_actions = [parsed.action_hint] if parsed.action_hint else None
        if selected_actions is None:
            if is_deprioritize:
                best_action = MerchAction.deprioritize
            elif is_campaign:
                best_action = MerchAction.promote
            else:
                best_action = MerchAction.feature
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
        occasions=parsed.occasions,
        peer_store_ids=peer_ids,
        compare_store_id=resolved_compare_store_id,
        compare_store_name=resolved_compare_store_name,
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
    occasions: list[str] | None = None,
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
    compare_store_id: str | None = None,
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
        occasions=occasions,
    )
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=parsed.lookback_days)
    prior_since = since - timedelta(days=parsed.lookback_days)
    peer_ids: list[str] = []
    resolved_compare_store_id: str | None = None
    resolved_compare_store_name: str | None = None
    if compare_mode in {CompareMode.peer, CompareMode.peer_and_prior_period}:
        peer_ids, resolved_compare_store_id, resolved_compare_store_name = _comparison_peers(
            session,
            store_id=resolved_store.id,
            peer_mode=peer_mode,
            compare_store_id=compare_store_id,
        )

    dimension = "brand" if parsed.brand else "category"
    current_rows = _dimension_aggregates(
        session,
        [resolved_store.id],
        since,
        now,
        dimension,
        parsed.category,
        parsed.brand,
        parsed.price_band,
        parsed.occasions,
    )
    peer_rows = (
        _dimension_aggregates(
            session,
            peer_ids,
            since,
            now,
            dimension,
            parsed.category,
            parsed.brand,
            parsed.price_band,
            parsed.occasions,
        )
        if peer_ids
        else []
    )
    prior_rows = _dimension_aggregates(
        session,
        [resolved_store.id],
        prior_since,
        since,
        dimension,
        parsed.category,
        parsed.brand,
        parsed.price_band,
        parsed.occasions,
    )

    peer_divisor = max(len(peer_ids), 1)
    peer_by_subject = _by_key(
        peer_rows,
        lambda row: row.subject,
        lambda row: (
            float(row.revenue or 0.0) / peer_divisor,
            float(row.units or 0.0) / peer_divisor,
            float(row.margin_pct or 0.0),
        ),
    )
    prior_by_subject = _by_key(
        prior_rows,
        lambda row: row.subject,
        lambda row: (
            float(row.revenue or 0.0),
            float(row.units or 0.0),
            float(row.margin_pct or 0.0),
        ),
    )

    insights: list[MerchDiagnosticInsight] = []
    for row in current_rows:
        current_value = float(row.revenue or 0.0)
        current_units = float(row.units or 0.0)
        current_margin = float(row.margin_pct or 0.0)

        peer_tuple = peer_by_subject.get(row.subject) if compare_mode in {CompareMode.peer, CompareMode.peer_and_prior_period} else None
        prior_tuple = prior_by_subject.get(row.subject) if compare_mode in {CompareMode.prior_period, CompareMode.peer_and_prior_period} else None

        peer_value = peer_tuple[0] if peer_tuple else None
        peer_units = peer_tuple[1] if peer_tuple else None
        peer_margin = peer_tuple[2] if peer_tuple else None
        prior_value = prior_tuple[0] if prior_tuple else None
        prior_units = prior_tuple[1] if prior_tuple else None
        prior_margin = prior_tuple[2] if prior_tuple else None

        baseline_value = peer_value if peer_value is not None else prior_value
        baseline_units = peer_units if peer_units is not None else prior_units
        baseline_margin = peer_margin if peer_margin is not None else prior_margin

        if baseline_value is not None:
            delta = current_value - baseline_value
            units_delta = current_units - float(baseline_units or 0.0)
            margin_delta = current_margin - float(baseline_margin or 0.0)
            if delta < 0 and margin_delta < 0:
                status = "margin_risk"
                rationale = "Revenue and margin are both below baseline; investigate markdown pressure and mix quality."
            elif delta < 0 and units_delta < 0:
                status = "velocity_gap"
                rationale = "Units and revenue trail baseline; demand is softer and floor placement may need adjustment."
            elif delta < 0:
                status = "conversion_gap"
                rationale = "Revenue trails baseline despite stable units, suggesting lower ASP or promo leakage."
            elif margin_delta < 0:
                status = "discount_led_growth"
                rationale = "Revenue is ahead of baseline but margin rate is lower; growth appears promo-led."
            else:
                status = "healthy_momentum"
                rationale = "Revenue, units, and margin are holding above baseline."
        else:
            delta = current_value
            status = "observed"
            rationale = "Current period diagnostic with no peer/prior baseline."
        insights.append(
            MerchDiagnosticInsight(
                dimension=dimension,
                subject=row.subject,
                status=status,
                current_value=round(current_value, 4),
                peer_value=round(peer_value, 4) if peer_value is not None else None,
                prior_value=round(prior_value, 4) if prior_value is not None else None,
                delta=round(delta, 4),
                current_units=round(current_units, 4),
                peer_units=round(peer_units, 4) if peer_units is not None else None,
                prior_units=round(prior_units, 4) if prior_units is not None else None,
                current_margin_pct=round(current_margin, 4),
                peer_margin_pct=round(peer_margin, 4) if peer_margin is not None else None,
                prior_margin_pct=round(prior_margin, 4) if prior_margin is not None else None,
                rationale=rationale,
            )
        )

    insights.sort(key=lambda item: abs(item.delta), reverse=True)
    comparison_label = "peer/prior baseline" if compare_mode == CompareMode.peer_and_prior_period else (
        "peer baseline" if compare_mode == CompareMode.peer else "prior-period baseline"
    )
    summary = f"Diagnostics for {resolved_store.name} over the last {parsed.lookback_days} days with {dimension}-level drivers versus {comparison_label}."
    return MerchDiagnosticsResponse(
        store=resolved_store,
        compare_mode=compare_mode,
        peer_mode=peer_mode,
        lookback_days=parsed.lookback_days,
        category=parsed.category,
        brand=parsed.brand,
        price_band=parsed.price_band,
        occasion=parsed.occasion,
        occasions=parsed.occasions,
        peer_store_ids=peer_ids,
        compare_store_id=resolved_compare_store_id,
        compare_store_name=resolved_compare_store_name,
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
    occasions: list[str] | None = None,
    compare_mode: CompareMode = CompareMode.peer_and_prior_period,
    peer_mode: PeerMode = PeerMode.state_and_profile,
    compare_store_id: str | None = None,
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
        occasions=occasions,
    )
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=parsed.lookback_days)
    prior_since = since - timedelta(days=parsed.lookback_days)
    peer_ids: list[str] = []
    resolved_compare_store_id: str | None = None
    resolved_compare_store_name: str | None = None
    if compare_mode in {CompareMode.peer, CompareMode.peer_and_prior_period}:
        peer_ids, resolved_compare_store_id, resolved_compare_store_name = _comparison_peers(
            session,
            store_id=resolved_store.id,
            peer_mode=peer_mode,
            compare_store_id=compare_store_id,
        )

    dimension = "brand" if parsed.brand else "category"
    current_rows = _dimension_aggregates(
        session,
        [resolved_store.id],
        since,
        now,
        dimension,
        parsed.category,
        parsed.brand,
        parsed.price_band,
        parsed.occasions,
    )
    peer_rows = (
        _dimension_aggregates(
            session,
            peer_ids,
            since,
            now,
            dimension,
            parsed.category,
            parsed.brand,
            parsed.price_band,
            parsed.occasions,
        )
        if peer_ids
        else []
    )
    prior_rows = _dimension_aggregates(
        session,
        [resolved_store.id],
        prior_since,
        since,
        dimension,
        parsed.category,
        parsed.brand,
        parsed.price_band,
        parsed.occasions,
    )

    peer_divisor = max(len(peer_ids), 1)
    peer_by_subject = _by_key(peer_rows, lambda row: row.subject, lambda row: float(row.revenue or 0.0) / peer_divisor)
    prior_by_subject = _by_key(prior_rows, lambda row: row.subject, lambda row: float(row.revenue or 0.0))

    highlights: list[MerchTrendHighlight] = []
    for row in current_rows:
        current_value = float(row.revenue or 0.0)
        prior_value = prior_by_subject.get(row.subject) if compare_mode in {CompareMode.prior_period, CompareMode.peer_and_prior_period} else None
        peer_value = peer_by_subject.get(row.subject) if compare_mode in {CompareMode.peer, CompareMode.peer_and_prior_period} else None
        baseline = peer_value if peer_value is not None else prior_value if prior_value is not None else 0.0
        pct_change = ((current_value - baseline) / baseline * 100.0) if baseline else 0.0
        if peer_value is not None and prior_value is not None:
            rationale = "Change versus peer baseline, with prior-period context included."
        elif prior_value is not None:
            rationale = "Change versus prior-period baseline."
        elif peer_value is not None:
            rationale = "Change versus peer baseline."
        else:
            rationale = "Current trend with no baseline."
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
    current_events = _sales_events(
        session,
        [resolved_store.id],
        since,
        now,
        parsed.category,
        parsed.brand,
        parsed.price_band,
        parsed.occasions,
    )
    baseline_events = []
    baseline_divisor = 1.0
    if compare_mode in {CompareMode.peer, CompareMode.peer_and_prior_period} and peer_ids:
        baseline_events = _sales_events(
            session,
            peer_ids,
            since,
            now,
            parsed.category,
            parsed.brand,
            parsed.price_band,
            parsed.occasions,
        )
        baseline_divisor = float(max(len(peer_ids), 1))
        baseline_origin = since
    elif compare_mode in {CompareMode.prior_period, CompareMode.peer_and_prior_period}:
        baseline_events = _sales_events(
            session,
            [resolved_store.id],
            prior_since,
            since,
            parsed.category,
            parsed.brand,
            parsed.price_band,
            parsed.occasions,
        )
        baseline_origin = prior_since
    else:
        baseline_origin = since

    current_buckets = _weekly_rollup(current_events, origin=since, horizon_days=parsed.lookback_days)
    baseline_buckets = (
        _weekly_rollup(
            baseline_events,
            origin=baseline_origin,
            horizon_days=parsed.lookback_days,
            divide_by=baseline_divisor,
        )
        if baseline_events
        else {}
    )

    time_series: list[MerchTrendPoint] = []
    for idx in sorted(current_buckets.keys()):
        current_bucket = current_buckets[idx]
        baseline_bucket = baseline_buckets.get(idx)
        time_series.append(
            MerchTrendPoint(
                period_start=str(current_bucket["period_start"]),
                current_revenue=round(float(current_bucket["revenue"]), 4),
                baseline_revenue=(
                    round(float(baseline_bucket["revenue"]), 4) if baseline_bucket is not None else None
                ),
                current_units=round(float(current_bucket["units"]), 4),
                baseline_units=(
                    round(float(baseline_bucket["units"]), 4) if baseline_bucket is not None else None
                ),
            )
        )

    summary = f"Trend summary for {resolved_store.name} over the last {parsed.lookback_days} days with weekly time-series."
    return MerchTrendSummaryResponse(
        store=resolved_store,
        compare_mode=compare_mode,
        peer_mode=peer_mode,
        lookback_days=parsed.lookback_days,
        category=parsed.category,
        brand=parsed.brand,
        price_band=parsed.price_band,
        occasion=parsed.occasion,
        occasions=parsed.occasions,
        peer_store_ids=peer_ids,
        compare_store_id=resolved_compare_store_id,
        compare_store_name=resolved_compare_store_name,
        summary=summary,
        highlights=highlights[:10],
        time_series=time_series,
    )
