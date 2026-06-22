from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import Customer, Order, OrderItem, Product, Store, StoreDailyMetric, SyntheticRun
from app.services.synthetic_generator import (
    _month_order_multiplier,
    _occasion_for_month,
    _season_match_multiplier,
    _weighted_choice,
)
from app.services.validation import run_validation_checks

logger = logging.getLogger(__name__)

DAILY_RUN_PREFIX = "daily_orders"
DAILY_ORDER_PREFIX = "daily_ord"
DAILY_ITEM_PREFIX = "daily_item"


@dataclass(frozen=True)
class DailyOrderGenerationOptions:
    seed: int = 20260313
    from_date: date | None = None
    through_date: date | None = None
    max_days: int = 14
    min_orders: int = 25
    max_orders: int = 220
    base_orders: int | None = None
    dry_run: bool = False
    now: datetime | None = None


@dataclass(frozen=True)
class DailyOrderPlanDay:
    target_date: date
    order_count: int


@dataclass(frozen=True)
class DailyOrderPlan:
    latest_order_date: date | None
    requested_start_date: date | None
    requested_through_date: date
    target_days: list[DailyOrderPlanDay]
    base_orders: int
    capped_days: int
    skipped_reason: str | None = None

    @property
    def planned_orders(self) -> int:
        return sum(day.order_count for day in self.target_days)


@dataclass(frozen=True)
class DailySyntheticOrderResult:
    run_id: str | None
    dry_run: bool
    latest_order_date: date | None
    requested_start_date: date | None
    requested_through_date: date
    target_days: list[DailyOrderPlanDay]
    base_orders: int
    inserted_orders: int = 0
    inserted_items: int = 0
    metrics_refreshed: int = 0
    validation_failures: int = 0
    skipped_reason: str | None = None

    @property
    def planned_orders(self) -> int:
        return sum(day.order_count for day in self.target_days)


@dataclass
class _GeneratedOrder:
    order: Order
    items: list[OrderItem]


@dataclass
class _MetricAccumulator:
    revenue: Decimal = Decimal("0.00")
    units: int = 0
    margin_total: Decimal = Decimal("0.00")


def _utc_now(options: DailyOrderGenerationOptions) -> datetime:
    now = options.now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _latest_order_date(db: Session) -> date | None:
    latest = db.scalar(select(func.max(Order.ordered_at)))
    if latest is None:
        return None
    if isinstance(latest, str):
        latest = datetime.fromisoformat(latest.replace("Z", "+00:00"))
    return latest.date()


def _coerce_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _daily_volume_multiplier(target_date: date) -> float:
    weekday = target_date.weekday()
    if weekday in {4, 5}:
        weekday_factor = 1.08
    elif weekday == 6:
        weekday_factor = 0.97
    else:
        weekday_factor = 1.0
    return _month_order_multiplier(target_date.month) * weekday_factor


def _bounded_count(value: float, *, min_orders: int, max_orders: int) -> int:
    lower = max(0, int(min_orders))
    upper = max(lower, int(max_orders))
    return max(lower, min(upper, int(round(value))))


def _derived_base_orders(db: Session, *, explicit: int | None, latest_order_date: date | None) -> int:
    if explicit is not None:
        return max(1, int(explicit))
    if latest_order_date is None:
        return 75

    since = datetime.combine(latest_order_date - timedelta(days=89), time.min, tzinfo=timezone.utc)
    rows = db.execute(
        select(func.date(Order.ordered_at).label("order_date"), func.count(Order.id).label("count"))
        .where(Order.ordered_at >= since)
        .group_by(func.date(Order.ordered_at))
    ).all()
    normalized_counts: list[float] = []
    for row in rows:
        order_date = _coerce_date(row.order_date)
        multiplier = max(_daily_volume_multiplier(order_date), 0.01)
        normalized_counts.append(float(row.count or 0) / multiplier)
    if not normalized_counts:
        return 75
    return max(1, int(round(sum(normalized_counts) / len(normalized_counts))))


def plan_daily_synthetic_orders(db: Session, options: DailyOrderGenerationOptions) -> DailyOrderPlan:
    current_now = _utc_now(options)
    through_date = options.through_date or (current_now.date() - timedelta(days=1))
    latest_date = _latest_order_date(db)

    if options.from_date is not None:
        start_date = options.from_date
    elif latest_date is not None:
        start_date = latest_date + timedelta(days=1)
    else:
        start_date = None

    base_orders = _derived_base_orders(db, explicit=options.base_orders, latest_order_date=latest_date)
    if start_date is None:
        return DailyOrderPlan(
            latest_order_date=latest_date,
            requested_start_date=None,
            requested_through_date=through_date,
            target_days=[],
            base_orders=base_orders,
            capped_days=0,
            skipped_reason="No existing orders found; pass from_date for an initial daily-order backfill.",
        )
    if start_date > through_date:
        return DailyOrderPlan(
            latest_order_date=latest_date,
            requested_start_date=start_date,
            requested_through_date=through_date,
            target_days=[],
            base_orders=base_orders,
            capped_days=0,
            skipped_reason="Synthetic orders are already current for the requested range.",
        )

    all_dates = [start_date + timedelta(days=offset) for offset in range((through_date - start_date).days + 1)]
    max_days = max(1, int(options.max_days))
    target_dates = all_dates[-max_days:]
    rng = random.Random(int(options.seed) + int(through_date.strftime("%Y%m%d")))
    target_days = []
    for target_date in target_dates:
        jitter = rng.uniform(0.92, 1.08)
        order_count = _bounded_count(
            base_orders * _daily_volume_multiplier(target_date) * jitter,
            min_orders=options.min_orders,
            max_orders=options.max_orders,
        )
        target_days.append(DailyOrderPlanDay(target_date=target_date, order_count=order_count))

    return DailyOrderPlan(
        latest_order_date=latest_date,
        requested_start_date=start_date,
        requested_through_date=through_date,
        target_days=target_days,
        base_orders=base_orders,
        capped_days=max(0, len(all_dates) - len(target_days)),
    )


def _run_id_for_plan(plan: DailyOrderPlan) -> str:
    if not plan.target_days:
        raise ValueError("Cannot build a run_id for an empty daily order plan.")
    first = plan.target_days[0].target_date.strftime("%Y%m%d")
    last = plan.target_days[-1].target_date.strftime("%Y%m%d")
    return f"{DAILY_RUN_PREFIX}_{first}_{last}"


def _require_source_data(db: Session) -> tuple[list[Store], list[Customer], list[Product]]:
    stores = list(db.scalars(select(Store).order_by(Store.id)).all())
    customers = list(db.scalars(select(Customer).order_by(Customer.id)).all())
    products = list(db.scalars(select(Product).order_by(Product.id)).all())
    missing = []
    if not stores:
        missing.append("stores")
    if not customers:
        missing.append("customers")
    if not products:
        missing.append("products")
    if missing:
        raise ValueError(
            "Daily synthetic orders require loaded synthetic "
            f"{', '.join(missing)}. Run the existing synthetic generate/load flow first."
        )
    return stores, customers, products


def _customer_weights(customers: list[Customer]) -> list[tuple[Customer, float]]:
    loyalty_weight = {"standard": 1.0, "silver": 1.6, "gold": 2.3, "platinum": 3.0}
    return [(customer, loyalty_weight.get(customer.loyalty_tier, 1.0)) for customer in customers]


def _product_weight(product: Product, month: int) -> float:
    availability = str(product.availability or "").lower()
    if availability == "in stock":
        availability_factor = 1.0
    elif availability == "preorder":
        availability_factor = 0.65
    else:
        availability_factor = 0.18
    return availability_factor * _season_match_multiplier(product.season, month)


def _generate_order_for_day(
    *,
    rng: random.Random,
    run_id: str,
    target_date: date,
    order_index: int,
    first_item_index: int,
    stores: list[Store],
    customer_weights: list[tuple[Customer, float]],
    products_by_store: dict[str, list[Product]],
    products_by_store_category: dict[tuple[str, str], list[Product]],
    all_products: list[Product],
) -> _GeneratedOrder:
    customer = _weighted_choice(rng, customer_weights)
    if rng.random() < 0.82:
        store_id = customer.home_store_id
    else:
        store_id = rng.choice(stores).id

    ordered_at = datetime.combine(target_date, time.min, tzinfo=timezone.utc) + timedelta(
        hours=rng.randint(8, 21),
        minutes=rng.randint(0, 59),
    )
    occasion = _occasion_for_month(rng, ordered_at.month)
    channel = customer.channel_preference
    if channel == "hybrid":
        channel = _weighted_choice(rng, [("online", 0.52), ("in_store", 0.48)])

    if ordered_at.month in {11, 12}:
        basket_options = [("1", 0.36), ("2", 0.36), ("3", 0.22), ("4", 0.06)]
    else:
        basket_options = [("1", 0.45), ("2", 0.34), ("3", 0.17), ("4", 0.04)]
    n_items = int(_weighted_choice(rng, basket_options))

    from app.services.taxonomy import CATEGORY_TAXONOMY, OCCASION_TO_CATEGORY

    base_categories = OCCASION_TO_CATEGORY.get(occasion, list(CATEGORY_TAXONOMY.keys()))
    order_id = f"{DAILY_ORDER_PREFIX}_{target_date.strftime('%Y%m%d')}_{order_index:05d}"
    subtotal = Decimal("0.00")
    items: list[OrderItem] = []
    item_index = first_item_index
    for _ in range(n_items):
        category = rng.choice(base_categories) if rng.random() < 0.74 else rng.choice(list(CATEGORY_TAXONOMY.keys()))
        pool = products_by_store_category.get((store_id, category)) or products_by_store.get(store_id) or all_products
        weighted_pool = [(product, _product_weight(product, ordered_at.month)) for product in pool]
        product = _weighted_choice(rng, weighted_pool)
        quantity = 1 if rng.random() < 0.88 else 2
        unit_price = Decimal(product.price).quantize(Decimal("0.01"))

        discount_prob = 0.34 if ordered_at.month in {11, 12} else 0.27 if ordered_at.month in {1, 2} else 0.20
        if occasion == "holiday_party":
            discount_prob += 0.04
        discount_rate = 0.0
        if rng.random() < min(discount_prob, 0.68):
            discount_rate = rng.choice([0.10, 0.15, 0.20, 0.25] if ordered_at.month in {11, 12, 1} else [0.05, 0.10, 0.15, 0.20])
        discount_amount = (unit_price * Decimal(quantity) * Decimal(str(discount_rate))).quantize(Decimal("0.01"))
        line_total = (unit_price * Decimal(quantity) - discount_amount).quantize(Decimal("0.01"))
        subtotal += line_total

        items.append(
            OrderItem(
                id=f"{DAILY_ITEM_PREFIX}_{target_date.strftime('%Y%m%d')}_{item_index:06d}",
                order_id=order_id,
                product_id=product.id,
                quantity=quantity,
                unit_price=unit_price,
                discount_amount=discount_amount,
                line_total=line_total,
            )
        )
        item_index += 1

    tax_amount = (subtotal * Decimal("0.0825")).quantize(Decimal("0.01"))
    returned_prob = 0.07 if occasion in {"wedding", "holiday_party"} else 0.12
    returned = rng.random() < returned_prob
    order = Order(
        id=order_id,
        seed_run_id=run_id,
        customer_id=customer.id,
        store_id=store_id,
        ordered_at=ordered_at,
        status="returned" if returned else "completed",
        occasion=occasion,
        channel=channel,
        subtotal=subtotal,
        discount_amount=Decimal("0.00"),
        tax_amount=tax_amount,
        total_amount=(subtotal + tax_amount).quantize(Decimal("0.01")),
        returned=returned,
    )
    return _GeneratedOrder(order=order, items=items)


def _delete_existing_daily_rows(db: Session, target_dates: list[date]) -> None:
    order_ids: list[str] = []
    for target_date in target_dates:
        prefix = f"{DAILY_ORDER_PREFIX}_{target_date.strftime('%Y%m%d')}_%"
        order_ids.extend(db.scalars(select(Order.id).where(Order.id.like(prefix))).all())
    if order_ids:
        db.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
        db.execute(delete(Order).where(Order.id.in_(order_ids)))
    db.execute(
        delete(StoreDailyMetric).where(
            StoreDailyMetric.seed_run_id.like(f"{DAILY_RUN_PREFIX}_%"),
            StoreDailyMetric.metric_date.in_(target_dates),
        )
    )


def _build_metrics(run_id: str, generated: list[_GeneratedOrder], products_by_id: dict[str, Product]) -> list[StoreDailyMetric]:
    aggregates: dict[tuple[str, date], _MetricAccumulator] = {}
    for generated_order in generated:
        metric_date = generated_order.order.ordered_at.date()
        key = (generated_order.order.store_id, metric_date)
        accumulator = aggregates.setdefault(key, _MetricAccumulator())
        for item in generated_order.items:
            product = products_by_id[item.product_id]
            line_total = Decimal(item.line_total)
            quantity = int(item.quantity)
            accumulator.revenue += line_total
            accumulator.units += quantity
            accumulator.margin_total += line_total * Decimal(product.margin_pct)

    metrics = []
    for (store_id, metric_date), values in sorted(aggregates.items()):
        revenue = values.revenue.quantize(Decimal("0.01"))
        units = values.units
        aov = (revenue / Decimal(max(units, 1))).quantize(Decimal("0.01"))
        margin_rate = (values.margin_total / revenue).quantize(Decimal("0.0001")) if revenue > 0 else Decimal("0.0000")
        sell_through = min(Decimal("0.9999"), Decimal(units) / Decimal(max(120, units + 20))).quantize(Decimal("0.0001"))
        metrics.append(
            StoreDailyMetric(
                seed_run_id=run_id,
                store_id=store_id,
                metric_date=metric_date,
                revenue=revenue,
                units_sold=units,
                sell_through=sell_through,
                aov=aov,
                margin_rate=margin_rate,
            )
        )
    return metrics


def generate_daily_synthetic_orders(db: Session, options: DailyOrderGenerationOptions) -> DailySyntheticOrderResult:
    plan = plan_daily_synthetic_orders(db, options)
    if not plan.target_days:
        return DailySyntheticOrderResult(
            run_id=None,
            dry_run=options.dry_run,
            latest_order_date=plan.latest_order_date,
            requested_start_date=plan.requested_start_date,
            requested_through_date=plan.requested_through_date,
            target_days=plan.target_days,
            base_orders=plan.base_orders,
            skipped_reason=plan.skipped_reason,
        )

    _require_source_data(db)
    run_id = _run_id_for_plan(plan)
    if options.dry_run:
        return DailySyntheticOrderResult(
            run_id=run_id,
            dry_run=True,
            latest_order_date=plan.latest_order_date,
            requested_start_date=plan.requested_start_date,
            requested_through_date=plan.requested_through_date,
            target_days=plan.target_days,
            base_orders=plan.base_orders,
        )

    target_dates = [day.target_date for day in plan.target_days]
    started_at = datetime.now(timezone.utc)
    run = db.get(SyntheticRun, run_id)
    if run is None:
        run = SyntheticRun(id=run_id, seed=options.seed, status="generating", started_at=started_at, config={})
    run.seed = options.seed
    run.status = "generating"
    run.started_at = started_at
    run.completed_at = None
    run.notes = None
    run.config = {
        "kind": "daily_synthetic_orders",
        "seed": options.seed,
        "from_date": target_dates[0].isoformat(),
        "through_date": target_dates[-1].isoformat(),
        "requested_start_date": plan.requested_start_date.isoformat() if plan.requested_start_date else None,
        "requested_through_date": plan.requested_through_date.isoformat(),
        "base_orders": plan.base_orders,
        "min_orders": options.min_orders,
        "max_orders": options.max_orders,
        "capped_days": plan.capped_days,
        "target_days": [{"date": day.target_date.isoformat(), "orders": day.order_count} for day in plan.target_days],
    }
    db.add(run)
    db.flush()

    try:
        stores, customers, products = _require_source_data(db)
        customer_weights = _customer_weights(customers)
        products_by_store: dict[str, list[Product]] = {}
        products_by_store_category: dict[tuple[str, str], list[Product]] = {}
        products_by_id = {product.id: product for product in products}
        for product in products:
            products_by_store.setdefault(product.store_id, []).append(product)
            products_by_store_category.setdefault((product.store_id, product.category), []).append(product)

        _delete_existing_daily_rows(db, target_dates)
        generated: list[_GeneratedOrder] = []
        item_index = 1
        for day in plan.target_days:
            rng = random.Random(options.seed + int(day.target_date.strftime("%Y%m%d")))
            for order_index in range(1, day.order_count + 1):
                generated_order = _generate_order_for_day(
                    rng=rng,
                    run_id=run_id,
                    target_date=day.target_date,
                    order_index=order_index,
                    first_item_index=item_index,
                    stores=stores,
                    customer_weights=customer_weights,
                    products_by_store=products_by_store,
                    products_by_store_category=products_by_store_category,
                    all_products=products,
                )
                generated.append(generated_order)
                item_index += len(generated_order.items)

        orders = [generated_order.order for generated_order in generated]
        items = [item for generated_order in generated for item in generated_order.items]
        metrics = _build_metrics(run_id, generated, products_by_id)
        db.add_all(orders)
        db.add_all(items)
        db.add_all(metrics)
        db.flush()

        validation_failures = run_validation_checks(db, run_id)
        run.status = "loaded"
        run.completed_at = datetime.now(timezone.utc)
        db.add(run)
        db.commit()
        logger.info(
            "daily synthetic orders generated",
            extra={
                "run_id": run_id,
                "target_start_date": target_dates[0].isoformat(),
                "target_end_date": target_dates[-1].isoformat(),
                "inserted_orders": len(orders),
                "inserted_items": len(items),
                "metrics_refreshed": len(metrics),
                "validation_failures": validation_failures,
            },
        )
        return DailySyntheticOrderResult(
            run_id=run_id,
            dry_run=False,
            latest_order_date=plan.latest_order_date,
            requested_start_date=plan.requested_start_date,
            requested_through_date=plan.requested_through_date,
            target_days=plan.target_days,
            base_orders=plan.base_orders,
            inserted_orders=len(orders),
            inserted_items=len(items),
            metrics_refreshed=len(metrics),
            validation_failures=validation_failures,
        )
    except Exception as exc:
        run.status = "failed"
        run.notes = str(exc)[:2000]
        run.completed_at = datetime.now(timezone.utc)
        db.add(run)
        db.commit()
        logger.exception("daily synthetic order generation failed", extra={"run_id": run_id})
        raise
