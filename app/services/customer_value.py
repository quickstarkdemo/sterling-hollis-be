from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Order
from app.schemas import (
    CustomerForecastPoint,
    CustomerPurchasePoint,
    CustomerValueMetrics,
    CustomerValuePoint,
    CustomerValueSummaryResponse,
    PurchaseScope,
)
from app.services.lookup import resolve_customer

_ROLLING_WEEKS = 12
_FORECAST_BASELINE_WEEKS = 8

_MONETARY_TARGET = 2500.0
_FREQUENCY_TARGET = 12.0
_RECENCY_TARGET_DAYS = 180.0
_AOV_TARGET = 600.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _score_components(spend: float, orders: int, recency_days: float | None, aov: float) -> tuple[float, float, float, float]:
    monetary = _clamp((spend / _MONETARY_TARGET) * 100.0, 0.0, 100.0)
    frequency = _clamp((orders / _FREQUENCY_TARGET) * 100.0, 0.0, 100.0)
    if recency_days is None:
        recency = 0.0
    else:
        recency = _clamp(100.0 - (recency_days / _RECENCY_TARGET_DAYS) * 100.0, 0.0, 100.0)
    aov_component = _clamp((aov / _AOV_TARGET) * 100.0, 0.0, 100.0)
    return monetary, frequency, recency, aov_component


def _value_score(spend: float, orders: int, recency_days: float | None, aov: float) -> float:
    monetary, frequency, recency, aov_component = _score_components(spend, orders, recency_days, aov)
    score = (0.40 * monetary) + (0.25 * frequency) + (0.25 * recency) + (0.10 * aov_component)
    return round(_clamp(score, 0.0, 100.0), 4)


def _value_tier(score: float) -> str:
    if score >= 75.0:
        return "high"
    if score >= 50.0:
        return "medium"
    return "low"


def _weekly_buckets(
    ordered_rows: list[tuple[datetime, float]],
    *,
    since: datetime,
    lookback_days: int,
) -> tuple[list[datetime], list[float], list[int]]:
    week_count = max(1, math.ceil(lookback_days / 7))
    starts = [since + timedelta(days=idx * 7) for idx in range(week_count)]
    spend = [0.0 for _ in range(week_count)]
    orders = [0 for _ in range(week_count)]
    for ordered_at, amount in ordered_rows:
        if ordered_at < since:
            continue
        idx = int((ordered_at - since).days // 7)
        idx = max(0, min(week_count - 1, idx))
        spend[idx] += float(amount or 0.0)
        orders[idx] += 1
    return starts, spend, orders


def _build_value_series(
    starts: list[datetime],
    spend: list[float],
    orders: list[int],
    order_times: list[datetime],
) -> list[CustomerValuePoint]:
    if not starts:
        return []
    values: list[CustomerValuePoint] = []
    order_idx = 0
    last_seen: datetime | None = None

    for idx, period_start in enumerate(starts):
        period_end = period_start + timedelta(days=7)
        while order_idx < len(order_times) and order_times[order_idx] < period_end:
            last_seen = order_times[order_idx]
            order_idx += 1

        window_start_idx = max(0, idx - (_ROLLING_WEEKS - 1))
        window_spend = sum(spend[window_start_idx : idx + 1])
        window_orders = sum(orders[window_start_idx : idx + 1])
        window_aov = window_spend / window_orders if window_orders > 0 else 0.0
        recency_days = ((period_end - last_seen).total_seconds() / 86400.0) if last_seen else None
        score = _value_score(window_spend, window_orders, recency_days, window_aov)
        values.append(CustomerValuePoint(period_start=period_start.date().isoformat(), value_score=score))
    return values


def _linear_regression(values: list[float]) -> tuple[float, float]:
    count = len(values)
    if count < 2:
        return 0.0, values[0] if count == 1 else 0.0
    x_values = [float(idx) for idx in range(count)]
    x_mean = sum(x_values) / count
    y_mean = sum(values) / count
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, values))
    denominator = sum((x - x_mean) ** 2 for x in x_values)
    if denominator == 0:
        return 0.0, y_mean
    slope = numerator / denominator
    intercept = y_mean - (slope * x_mean)
    return slope, intercept


def _residual_stddev(values: list[float], slope: float, intercept: float) -> float:
    if not values:
        return 0.0
    residuals = []
    for idx, value in enumerate(values):
        predicted = intercept + (slope * float(idx))
        residuals.append(value - predicted)
    variance = sum(res * res for res in residuals) / float(len(residuals))
    return math.sqrt(max(variance, 0.0))


def _build_forecast_series(
    starts: list[datetime],
    spend: list[float],
    *,
    forecast_weeks: int,
) -> list[CustomerForecastPoint]:
    if forecast_weeks <= 0:
        return []
    if starts:
        next_start = starts[-1] + timedelta(days=7)
    else:
        now = datetime.now(timezone.utc)
        next_start = now - timedelta(days=now.weekday())

    training = spend[-_FORECAST_BASELINE_WEEKS:] if spend else []
    non_zero_points = [value for value in training if value > 0]
    if len(training) < 2 or len(non_zero_points) < 2:
        baseline = float(training[-1]) if training else 0.0
        stddev = 0.0
        points: list[CustomerForecastPoint] = []
        for step in range(forecast_weeks):
            projected = max(0.0, baseline)
            period_start = (next_start + timedelta(days=step * 7)).date().isoformat()
            points.append(
                CustomerForecastPoint(
                    period_start=period_start,
                    projected_spend=round(projected, 4),
                    low_spend=round(max(0.0, projected - stddev), 4),
                    high_spend=round(max(0.0, projected + stddev), 4),
                )
            )
        return points

    slope, intercept = _linear_regression(training)
    stddev = _residual_stddev(training, slope, intercept)
    start_x = len(training) - 1
    points = []
    for step in range(1, forecast_weeks + 1):
        x_value = start_x + step
        projected = max(0.0, intercept + (slope * float(x_value)))
        period_start = (next_start + timedelta(days=(step - 1) * 7)).date().isoformat()
        points.append(
            CustomerForecastPoint(
                period_start=period_start,
                projected_spend=round(projected, 4),
                low_spend=round(max(0.0, projected - stddev), 4),
                high_spend=round(max(0.0, projected + stddev), 4),
            )
        )
    return points


def customer_value_summary(
    session: Session,
    *,
    customer_id: str,
    lookback_days: int = 180,
    forecast_weeks: int = 8,
    purchase_scope: PurchaseScope = PurchaseScope.all_stores,
) -> CustomerValueSummaryResponse:
    bounded_lookback = max(30, min(int(lookback_days), 730))
    bounded_forecast = max(1, min(int(forecast_weeks), 26))
    resolved_customer = resolve_customer(session, customer_id=customer_id).resolved
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=bounded_lookback)

    base_filters = [Order.customer_id == resolved_customer.id, Order.returned.is_(False)]
    lifetime_row = session.execute(
        select(
            func.coalesce(func.sum(Order.total_amount), 0.0),
            func.count(Order.id),
            func.max(Order.ordered_at),
        ).where(*base_filters)
    ).one()
    lookback_row = session.execute(
        select(
            func.coalesce(func.sum(Order.total_amount), 0.0),
            func.count(Order.id),
        ).where(*base_filters, Order.ordered_at >= since, Order.ordered_at < now)
    ).one()
    ordered_rows = [
        (_as_utc(row.ordered_at), float(row.total_amount or 0.0))
        for row in session.execute(
            select(Order.ordered_at, Order.total_amount)
            .where(*base_filters, Order.ordered_at >= since, Order.ordered_at < now)
            .order_by(Order.ordered_at)
        ).all()
        if _as_utc(row.ordered_at) is not None
    ]

    lifetime_spend = float(lifetime_row[0] or 0.0)
    lifetime_orders = int(lifetime_row[1] or 0)
    latest_order_at = _as_utc(lifetime_row[2])
    lookback_spend = float(lookback_row[0] or 0.0)
    lookback_orders = int(lookback_row[1] or 0)
    aov = (lookback_spend / lookback_orders) if lookback_orders > 0 else (lifetime_spend / lifetime_orders if lifetime_orders > 0 else 0.0)
    recency_days = ((now - latest_order_at).total_seconds() / 86400.0) if latest_order_at else None
    score = _value_score(lookback_spend, lookback_orders, recency_days, aov)

    starts, weekly_spend, weekly_orders = _weekly_buckets(ordered_rows, since=since, lookback_days=bounded_lookback)
    purchase_series = [
        CustomerPurchasePoint(
            period_start=period_start.date().isoformat(),
            spend=round(weekly_spend[idx], 4),
            orders=int(weekly_orders[idx]),
        )
        for idx, period_start in enumerate(starts)
    ]
    value_series = _build_value_series(starts, weekly_spend, weekly_orders, [ordered_at for ordered_at, _ in ordered_rows if ordered_at is not None])
    forecast_series = _build_forecast_series(starts, weekly_spend, forecast_weeks=bounded_forecast)

    return CustomerValueSummaryResponse(
        customer=resolved_customer,
        lookback_days=bounded_lookback,
        forecast_weeks=bounded_forecast,
        purchase_scope=purchase_scope,
        metrics=CustomerValueMetrics(
            value_score=score,
            value_tier=_value_tier(score),
            lifetime_spend=round(lifetime_spend, 4),
            lookback_spend=round(lookback_spend, 4),
            lifetime_orders=lifetime_orders,
            lookback_orders=lookback_orders,
            aov=round(aov, 4),
            recency_days=round(recency_days, 4) if recency_days is not None else None,
        ),
        value_series=value_series,
        purchase_series=purchase_series,
        forecast_series=forecast_series,
    )
