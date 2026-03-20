from __future__ import annotations

import hashlib
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.services.taxonomy import CATEGORY_TAXONOMY

ANALYST_STORE_CATEGORY_V1_FILE = "analyst_store_category_v1.csv"

ANALYST_STORE_CATEGORY_V1_HEADERS = [
    "as_of_date",
    "lookback_days",
    "prior_lookback_days",
    "store_id",
    "store_name",
    "state",
    "profile_type",
    "category",
    "category_label",
    "current_revenue",
    "prior_revenue",
    "revenue_delta_pct",
    "current_units",
    "prior_units",
    "units_delta_pct",
    "current_discount_rate_pct",
    "current_margin_rate_pct",
    "analyst_priority",
    "analyst_recommended_discount_pct",
    "analyst_floor_space_shift_pct",
    "analyst_from_category",
    "analyst_to_category",
    "analyst_confidence",
    "divergence_flag",
    "analyst_rationale",
]


@dataclass
class _MetricBucket:
    revenue: float = 0.0
    units: float = 0.0
    gross: float = 0.0
    discount: float = 0.0
    margin_value: float = 0.0


@dataclass
class _Candidate:
    store_id: str
    store_name: str
    state: str
    profile_type: str
    category: str
    category_label: str
    current_revenue: float
    prior_revenue: float
    revenue_delta_pct: float
    current_units: float
    prior_units: float
    units_delta_pct: float
    current_discount_rate_pct: float
    current_margin_rate_pct: float
    impact_score: float


@dataclass
class _AnalystAction:
    priority: str
    recommended_discount_pct: float
    floor_space_shift_pct: float
    from_category: str
    to_category: str
    confidence: float
    divergence_flag: str
    rationale: str


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _as_bool(value) -> bool:
    token = str(value or "").strip().lower()
    return token in {"true", "1", "yes"}


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _pct_delta(current: float, prior: float) -> float:
    if prior <= 0:
        if current > 0:
            return 100.0
        return 0.0
    return ((current - prior) / abs(prior)) * 100.0


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _stable_unit(value: str) -> float:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return int(digest, 16) / float(16**12 - 1)


def _aligned_action(
    candidate: _Candidate,
    *,
    best_category_by_store: dict[str, str],
    worst_category_by_store: dict[str, str],
) -> _AnalystAction:
    revenue_drop = max(-candidate.revenue_delta_pct, 0.0)
    units_drop = max(-candidate.units_delta_pct, 0.0)

    if candidate.revenue_delta_pct <= -10.0:
        if candidate.current_margin_rate_pct >= 50.0:
            priority = "grow"
            discount_pct = _clamp(6.0 + (revenue_drop * 0.23), 6.0, 18.0)
            floor_shift = _clamp(3.0 + (revenue_drop * 0.28), 3.0, 14.0)
        else:
            priority = "deprioritize"
            discount_pct = _clamp(2.0 + (revenue_drop * 0.12), 0.0, 12.0)
            floor_shift = -_clamp(4.0 + (revenue_drop * 0.25), 4.0, 16.0)
    elif candidate.revenue_delta_pct >= 8.0 and candidate.current_margin_rate_pct >= 52.0:
        priority = "protect"
        discount_pct = _clamp(candidate.current_discount_rate_pct * 0.4, 0.0, 6.0)
        floor_shift = 0.0
    elif candidate.units_delta_pct <= -12.0 and candidate.current_margin_rate_pct >= 48.0:
        priority = "grow"
        discount_pct = _clamp(5.0 + (units_drop * 0.22), 5.0, 14.0)
        floor_shift = _clamp(3.0 + (units_drop * 0.22), 3.0, 12.0)
    else:
        priority = "protect"
        discount_pct = _clamp(candidate.current_discount_rate_pct * 0.5, 0.0, 8.0)
        floor_shift = 0.0

    if priority == "grow":
        from_category = worst_category_by_store.get(candidate.store_id, "")
        if from_category == candidate.category:
            from_category = ""
        to_category = candidate.category
        rationale = (
            f"Aligned view: revenue is down {abs(candidate.revenue_delta_pct):.1f}% vs prior 90d with "
            f"{candidate.current_margin_rate_pct:.1f}% margin, so grow {candidate.category_label} with "
            f"{discount_pct:.1f}% discount support and {floor_shift:+.1f}% floor-space shift."
        )
    elif priority == "deprioritize":
        from_category = candidate.category
        to_category = best_category_by_store.get(candidate.store_id, "")
        if to_category == candidate.category:
            to_category = ""
        rationale = (
            f"Aligned view: persistent demand softness ({candidate.revenue_delta_pct:.1f}% revenue delta) "
            f"supports deprioritize action and {floor_shift:+.1f}% floor-space rotation out of "
            f"{candidate.category_label}."
        )
    else:
        from_category = ""
        to_category = ""
        rationale = (
            f"Aligned view: protect {candidate.category_label} with controlled pricing; current discount "
            f"rate is {candidate.current_discount_rate_pct:.1f}% and margin remains {candidate.current_margin_rate_pct:.1f}%."
        )

    confidence_seed = _stable_unit(f"{candidate.store_id}:{candidate.category}:aligned")
    confidence = _clamp(0.62 + confidence_seed * 0.25, 0.62, 0.89)

    return _AnalystAction(
        priority=priority,
        recommended_discount_pct=discount_pct,
        floor_space_shift_pct=floor_shift,
        from_category=from_category,
        to_category=to_category,
        confidence=confidence,
        divergence_flag="aligned",
        rationale=rationale,
    )


def _contrarian_action(
    candidate: _Candidate,
    *,
    aligned: _AnalystAction,
    best_category_by_store: dict[str, str],
    worst_category_by_store: dict[str, str],
) -> _AnalystAction:
    if aligned.priority == "grow":
        priority = "protect"
        discount_pct = _clamp(aligned.recommended_discount_pct - 5.0, 0.0, 8.0)
        floor_shift = 0.0
        from_category = ""
        to_category = ""
        rationale = (
            f"Contrarian view: despite the {abs(candidate.revenue_delta_pct):.1f}% decline, protect "
            f"{candidate.category_label} margin ahead of expected demand normalization and avoid a broad markdown."
        )
    elif aligned.priority == "deprioritize":
        priority = "grow"
        discount_pct = _clamp(aligned.recommended_discount_pct + 5.0, 6.0, 16.0)
        floor_shift = _clamp(abs(aligned.floor_space_shift_pct) + 4.0, 4.0, 14.0)
        from_category = worst_category_by_store.get(candidate.store_id, "")
        if from_category == candidate.category:
            from_category = ""
        to_category = candidate.category
        rationale = (
            f"Contrarian view: invest into {candidate.category_label} now with selective clienteling and "
            f"{discount_pct:.1f}% incentives to recover share rather than reducing exposure."
        )
    else:
        if candidate.revenue_delta_pct >= 8.0:
            priority = "deprioritize"
            discount_pct = 0.0
            floor_shift = -_clamp(4.0 + (candidate.revenue_delta_pct * 0.2), 4.0, 12.0)
            from_category = candidate.category
            to_category = best_category_by_store.get(candidate.store_id, "")
            if to_category == candidate.category:
                to_category = ""
            rationale = (
                f"Contrarian view: after strong gains (+{candidate.revenue_delta_pct:.1f}%), harvest margin and "
                f"rotate {abs(floor_shift):.1f}% floor space from {candidate.category_label} into weaker categories."
            )
        else:
            priority = "grow"
            discount_pct = _clamp(max(6.0, aligned.recommended_discount_pct + 4.0), 6.0, 14.0)
            floor_shift = _clamp(5.0 + max(-candidate.revenue_delta_pct, 0.0) * 0.15, 4.0, 12.0)
            from_category = worst_category_by_store.get(candidate.store_id, "")
            if from_category == candidate.category:
                from_category = ""
            to_category = candidate.category
            rationale = (
                f"Contrarian view: accelerate {candidate.category_label} with {discount_pct:.1f}% targeted offers "
                f"and {floor_shift:+.1f}% space expansion to preempt peer promotions."
            )

    confidence = _clamp(aligned.confidence - 0.10, 0.52, 0.79)
    return _AnalystAction(
        priority=priority,
        recommended_discount_pct=discount_pct,
        floor_space_shift_pct=floor_shift,
        from_category=from_category,
        to_category=to_category,
        confidence=confidence,
        divergence_flag="contrarian",
        rationale=rationale,
    )


def _aggregate_store_category_metrics(
    *,
    as_of: datetime,
    lookback_days: int,
    prior_lookback_days: int,
    products: list[dict],
    orders: list[dict],
    order_items: list[dict],
) -> tuple[dict[tuple[str, str], _MetricBucket], dict[tuple[str, str], _MetricBucket]]:
    current_since = as_of - timedelta(days=lookback_days)
    prior_since = current_since - timedelta(days=prior_lookback_days)

    products_by_id = {str(row.get("id")): row for row in products if row.get("id") is not None}
    orders_by_id = {str(row.get("id")): row for row in orders if row.get("id") is not None}

    current: dict[tuple[str, str], _MetricBucket] = defaultdict(_MetricBucket)
    prior: dict[tuple[str, str], _MetricBucket] = defaultdict(_MetricBucket)

    for item in order_items:
        order = orders_by_id.get(str(item.get("order_id")))
        if not order or _as_bool(order.get("returned")):
            continue
        product = products_by_id.get(str(item.get("product_id")))
        if not product:
            continue
        category = str(product.get("category") or "").strip()
        if category not in CATEGORY_TAXONOMY:
            continue

        ordered_at = _parse_datetime(str(order.get("ordered_at") or ""))
        if ordered_at is None:
            continue

        bucket: dict[tuple[str, str], _MetricBucket] | None = None
        if current_since <= ordered_at < as_of:
            bucket = current
        elif prior_since <= ordered_at < current_since:
            bucket = prior
        if bucket is None:
            continue

        store_id = str(order.get("store_id") or "").strip()
        if not store_id:
            continue
        key = (store_id, category)
        entry = bucket[key]

        quantity = _safe_int(item.get("quantity"), 0)
        unit_price = _safe_float(item.get("unit_price"), 0.0)
        line_total = _safe_float(item.get("line_total"), 0.0)
        discount_amount = _safe_float(item.get("discount_amount"), 0.0)
        margin_pct = _safe_float(product.get("margin_pct"), 0.0)

        entry.revenue += line_total
        entry.units += float(quantity)
        entry.gross += unit_price * quantity
        entry.discount += discount_amount
        entry.margin_value += line_total * margin_pct

    return current, prior


def _build_candidates(
    *,
    stores: list[dict],
    current: dict[tuple[str, str], _MetricBucket],
    prior: dict[tuple[str, str], _MetricBucket],
) -> list[_Candidate]:
    stores_by_id = {str(row.get("id")): row for row in stores if row.get("id")}
    keys = sorted(set(current.keys()) | set(prior.keys()))
    candidates: list[_Candidate] = []

    for store_id, category in keys:
        store = stores_by_id.get(store_id)
        if store is None:
            continue
        if category not in CATEGORY_TAXONOMY:
            continue

        cur = current.get((store_id, category), _MetricBucket())
        prv = prior.get((store_id, category), _MetricBucket())
        if cur.revenue <= 0 and prv.revenue <= 0:
            continue

        revenue_delta_pct = _pct_delta(cur.revenue, prv.revenue)
        units_delta_pct = _pct_delta(cur.units, prv.units)
        discount_rate_pct = (cur.discount / cur.gross * 100.0) if cur.gross > 0 else 0.0
        margin_rate_pct = (cur.margin_value / cur.revenue * 100.0) if cur.revenue > 0 else 0.0

        revenue_at_risk = max(prv.revenue - cur.revenue, 0.0)
        momentum_penalty = max(-revenue_delta_pct, 0.0) + max(-units_delta_pct, 0.0) * 0.35
        impact_score = (revenue_at_risk * (margin_rate_pct / 100.0 + 0.1)) + (momentum_penalty * 120.0) + (cur.revenue * 0.02)

        candidates.append(
            _Candidate(
                store_id=store_id,
                store_name=str(store.get("name") or store_id),
                state=str(store.get("state") or ""),
                profile_type=str(store.get("profile_type") or ""),
                category=category,
                category_label=str(CATEGORY_TAXONOMY[category]["label"]),
                current_revenue=cur.revenue,
                prior_revenue=prv.revenue,
                revenue_delta_pct=revenue_delta_pct,
                current_units=cur.units,
                prior_units=prv.units,
                units_delta_pct=units_delta_pct,
                current_discount_rate_pct=discount_rate_pct,
                current_margin_rate_pct=margin_rate_pct,
                impact_score=impact_score,
            )
        )

    return candidates


def _select_rows(candidates: list[_Candidate], *, target_rows: int) -> list[_Candidate]:
    if target_rows <= 0 or not candidates:
        return []

    by_store: dict[str, list[_Candidate]] = defaultdict(list)
    for row in candidates:
        by_store[row.store_id].append(row)
    for store_rows in by_store.values():
        store_rows.sort(key=lambda row: row.impact_score, reverse=True)

    store_order = sorted(
        by_store.keys(),
        key=lambda store_id: sum(item.impact_score for item in by_store[store_id]),
        reverse=True,
    )

    selected: list[_Candidate] = []
    selected_keys: set[tuple[str, str]] = set()
    index = 0
    while len(selected) < target_rows:
        appended = False
        for store_id in store_order:
            rows = by_store[store_id]
            if index >= len(rows):
                continue
            candidate = rows[index]
            key = (candidate.store_id, candidate.category)
            if key in selected_keys:
                continue
            selected.append(candidate)
            selected_keys.add(key)
            appended = True
            if len(selected) >= target_rows:
                break
        if not appended:
            break
        index += 1

    if len(selected) < target_rows:
        remaining = sorted(candidates, key=lambda row: row.impact_score, reverse=True)
        for candidate in remaining:
            key = (candidate.store_id, candidate.category)
            if key in selected_keys:
                continue
            selected.append(candidate)
            selected_keys.add(key)
            if len(selected) >= target_rows:
                break

    return selected[:target_rows]


def _store_category_extremes(candidates: list[_Candidate]) -> tuple[dict[str, str], dict[str, str]]:
    best: dict[str, str] = {}
    worst: dict[str, str] = {}
    by_store: dict[str, list[_Candidate]] = defaultdict(list)
    for row in candidates:
        by_store[row.store_id].append(row)

    for store_id, rows in by_store.items():
        best_row = max(rows, key=lambda row: (row.revenue_delta_pct, row.current_revenue))
        worst_row = min(rows, key=lambda row: (row.revenue_delta_pct, -row.current_revenue))
        best[store_id] = best_row.category
        worst[store_id] = worst_row.category

    return best, worst


def _contrarian_indexes(selected: list[_Candidate]) -> set[int]:
    n = len(selected)
    if n == 0:
        return set()

    min_count = int(math.ceil(n * 0.20))
    max_count = int(math.floor(n * 0.40))
    if max_count < min_count:
        max_count = min_count
    target_count = int(round(n * 0.30))
    target_count = min(max(target_count, min_count), max_count)

    indexes_by_impact = sorted(range(n), key=lambda idx: selected[idx].impact_score, reverse=True)
    chosen: list[int] = []
    for pos, idx in enumerate(indexes_by_impact):
        if (pos + 1) % 3 == 0:
            chosen.append(idx)
        if len(chosen) >= target_count:
            break
    if len(chosen) < target_count:
        for idx in indexes_by_impact:
            if idx in chosen:
                continue
            chosen.append(idx)
            if len(chosen) >= target_count:
                break
    return set(chosen[:target_count])


def _format_money(value: float) -> str:
    return f"{value:.2f}"


def _format_pct(value: float) -> str:
    return f"{value:.2f}"


def _format_units(value: float) -> str:
    return str(int(round(value)))


def generate_analyst_store_category_v1_rows(
    *,
    run_id: str,
    seed: int,
    as_of: datetime,
    stores: list[dict],
    products: list[dict],
    orders: list[dict],
    order_items: list[dict],
    lookback_days: int = 90,
    prior_lookback_days: int = 90,
    target_rows: int = 30,
) -> list[dict]:
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    else:
        as_of = as_of.astimezone(timezone.utc)

    current, prior = _aggregate_store_category_metrics(
        as_of=as_of,
        lookback_days=lookback_days,
        prior_lookback_days=prior_lookback_days,
        products=products,
        orders=orders,
        order_items=order_items,
    )
    candidates = _build_candidates(stores=stores, current=current, prior=prior)
    selected = _select_rows(candidates, target_rows=target_rows)
    if not selected:
        return []

    best_category_by_store, worst_category_by_store = _store_category_extremes(candidates)
    contrarian_idx = _contrarian_indexes(selected)
    rng = random.Random(f"analyst:{seed}:{run_id}:{as_of.date().isoformat()}:{len(selected)}")

    rows: list[dict] = []
    for idx, candidate in enumerate(selected):
        aligned = _aligned_action(
            candidate,
            best_category_by_store=best_category_by_store,
            worst_category_by_store=worst_category_by_store,
        )
        if idx in contrarian_idx:
            action = _contrarian_action(
                candidate,
                aligned=aligned,
                best_category_by_store=best_category_by_store,
                worst_category_by_store=worst_category_by_store,
            )
        else:
            action = aligned

        # Keep confidence deterministic but avoid repeated values in Sheets.
        confidence_jitter = (rng.random() - 0.5) * 0.04
        confidence = _clamp(action.confidence + confidence_jitter, 0.50, 0.92)

        rows.append(
            {
                "as_of_date": as_of.date().isoformat(),
                "lookback_days": str(int(lookback_days)),
                "prior_lookback_days": str(int(prior_lookback_days)),
                "store_id": candidate.store_id,
                "store_name": candidate.store_name,
                "state": candidate.state,
                "profile_type": candidate.profile_type,
                "category": candidate.category,
                "category_label": candidate.category_label,
                "current_revenue": _format_money(candidate.current_revenue),
                "prior_revenue": _format_money(candidate.prior_revenue),
                "revenue_delta_pct": _format_pct(candidate.revenue_delta_pct),
                "current_units": _format_units(candidate.current_units),
                "prior_units": _format_units(candidate.prior_units),
                "units_delta_pct": _format_pct(candidate.units_delta_pct),
                "current_discount_rate_pct": _format_pct(candidate.current_discount_rate_pct),
                "current_margin_rate_pct": _format_pct(candidate.current_margin_rate_pct),
                "analyst_priority": action.priority,
                "analyst_recommended_discount_pct": _format_pct(action.recommended_discount_pct),
                "analyst_floor_space_shift_pct": _format_pct(action.floor_space_shift_pct),
                "analyst_from_category": action.from_category,
                "analyst_to_category": action.to_category,
                "analyst_confidence": _format_pct(confidence),
                "divergence_flag": action.divergence_flag,
                "analyst_rationale": action.rationale,
            }
        )

    rows.sort(key=lambda row: (row["store_id"], row["category"]))
    return rows[:target_rows]
