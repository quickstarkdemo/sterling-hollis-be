from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from datetime import date

from app.config import get_settings
from app.database import SessionLocal
from app.observability.logging import configure_datadog_logging
from app.services.daily_synthetic_orders import DailyOrderGenerationOptions, generate_daily_synthetic_orders

logger = logging.getLogger(__name__)


def _date_arg(value: str | None) -> date | None:
    if value is None or not str(value).strip():
        return None
    return date.fromisoformat(value)


def _result_payload(result) -> dict:
    payload = asdict(result)
    payload["target_days"] = [
        {"date": day.target_date.isoformat(), "orders": day.order_count}
        for day in result.target_days
    ]
    payload["latest_order_date"] = result.latest_order_date.isoformat() if result.latest_order_date else None
    payload["requested_start_date"] = result.requested_start_date.isoformat() if result.requested_start_date else None
    payload["requested_through_date"] = result.requested_through_date.isoformat()
    payload["planned_orders"] = result.planned_orders
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Append daily synthetic order volume to the loaded demo dataset.")
    parser.add_argument("--from-date", type=_date_arg, default=None, help="First date to generate, inclusive.")
    parser.add_argument("--through-date", type=_date_arg, default=None, help="Last date to generate, inclusive. Defaults to yesterday UTC.")
    parser.add_argument("--max-days", type=int, default=None, help="Maximum number of dates to generate in one run.")
    parser.add_argument("--min-orders", type=int, default=None, help="Lower bound for generated orders per date.")
    parser.add_argument("--max-orders", type=int, default=None, help="Upper bound for generated orders per date.")
    parser.add_argument("--base-orders", type=int, default=None, help="Neutral daily baseline before seasonal multipliers. Defaults to observed data.")
    parser.add_argument("--seed", type=int, default=None, help="Deterministic seed for generated daily orders.")
    parser.add_argument("--dry-run", action="store_true", help="Report the planned dates/counts without mutating the database.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    return parser


def main() -> None:
    configure_datadog_logging()
    args = build_parser().parse_args()
    settings = get_settings()
    options = DailyOrderGenerationOptions(
        seed=args.seed if args.seed is not None else settings.synthetic_daily_seed,
        from_date=args.from_date,
        through_date=args.through_date,
        max_days=args.max_days if args.max_days is not None else settings.synthetic_daily_max_catchup_days,
        min_orders=args.min_orders if args.min_orders is not None else settings.synthetic_daily_min_orders,
        max_orders=args.max_orders if args.max_orders is not None else settings.synthetic_daily_max_orders,
        base_orders=args.base_orders if args.base_orders is not None else settings.synthetic_daily_base_orders,
        dry_run=args.dry_run,
    )
    with SessionLocal() as db:
        result = generate_daily_synthetic_orders(db, options)

    payload = _result_payload(result)
    logger.info(
        "daily synthetic order command completed",
        extra={
            "run_id": result.run_id,
            "dry_run": result.dry_run,
            "planned_orders": result.planned_orders,
            "inserted_orders": result.inserted_orders,
            "inserted_items": result.inserted_items,
            "metrics_refreshed": result.metrics_refreshed,
            "validation_failures": result.validation_failures,
            "skipped_reason": result.skipped_reason,
        },
    )
    print(json.dumps(payload, indent=2 if args.pretty else None, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
