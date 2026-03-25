from __future__ import annotations

import csv
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    Customer,
    CustomerCommunication,
    Order,
    OrderItem,
    Product,
    ProductEmbedding,
    SupplierProductOffer,
    Store,
    StoreDailyMetric,
    SyntheticRun,
)
from app.services.operator_cache import clear_operator_caches

ENTITY_MODEL_MAP = {
    "stores": Store,
    "customers": Customer,
    "products": Product,
    "orders": Order,
    "order_items": OrderItem,
    "store_daily_metrics": StoreDailyMetric,
    "supplier_product_offers": SupplierProductOffer,
}


DATETIME_FIELDS = {"joined_at", "ordered_at", "started_at", "completed_at", "embedded_at", "created_at", "updated_at"}
DATE_FIELDS = {"metric_date", "available_on"}
DECIMAL_FIELDS = {
    "price",
    "subtotal",
    "discount_amount",
    "tax_amount",
    "total_amount",
    "unit_price",
    "line_total",
    "revenue",
    "aov",
}
FLOAT_FIELDS = {
    "price_sensitivity",
    "margin_pct",
    "objective_weight",
    "sell_through",
    "margin_rate",
    "latitude",
    "longitude",
}
INT_FIELDS = {"inventory_qty", "quantity", "units_sold", "seed"}
BOOL_FIELDS = {"returned"}
JSON_FIELDS = {
    "services",
    "raw_source",
    "occasion_affinity",
    "style_vector",
    "size_preferences",
    "metadata_json",
    "config",
}


def _convert_value(field: str, value: str):
    if value == "" or value is None:
        return None

    if field in DATETIME_FIELDS:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    if field in DATE_FIELDS:
        return date.fromisoformat(value)
    if field in DECIMAL_FIELDS:
        return Decimal(value)
    if field in FLOAT_FIELDS:
        return float(value)
    if field in INT_FIELDS:
        return int(value)
    if field in BOOL_FIELDS:
        return value.lower() in {"true", "1", "yes"}
    if field in JSON_FIELDS:
        return json.loads(value)

    return value


def _parse_csv(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", newline="", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            parsed = {k: _convert_value(k, v) for k, v in row.items()}
            rows.append(parsed)
    return rows


def reset_synthetic_tables(db: Session) -> None:
    # FK-safe delete order.
    db.execute(delete(OrderItem))
    db.execute(delete(Order))
    db.execute(delete(CustomerCommunication))
    db.execute(delete(ProductEmbedding))
    db.execute(delete(StoreDailyMetric))
    db.execute(delete(SupplierProductOffer))
    db.execute(delete(Product))
    db.execute(delete(Customer))
    db.execute(delete(Store))
    db.commit()
    clear_operator_caches()


def assert_synthetic_tables_empty(db: Session) -> None:
    table_counts = {
        "order_items": db.scalar(select(func.count()).select_from(OrderItem)) or 0,
        "orders": db.scalar(select(func.count()).select_from(Order)) or 0,
        "customer_communications": db.scalar(select(func.count()).select_from(CustomerCommunication)) or 0,
        "product_embeddings": db.scalar(select(func.count()).select_from(ProductEmbedding)) or 0,
        "store_daily_metrics": db.scalar(select(func.count()).select_from(StoreDailyMetric)) or 0,
        "supplier_product_offers": db.scalar(select(func.count()).select_from(SupplierProductOffer)) or 0,
        "products": db.scalar(select(func.count()).select_from(Product)) or 0,
        "customers": db.scalar(select(func.count()).select_from(Customer)) or 0,
        "stores": db.scalar(select(func.count()).select_from(Store)) or 0,
    }
    remaining = {name: count for name, count in table_counts.items() if count}
    if remaining:
        formatted = ", ".join(f"{name}={count}" for name, count in sorted(remaining.items()))
        raise ValueError(f"Synthetic reset failed; rows remain after reset: {formatted}")


def load_entity_csv(db: Session, run_id: str, data_dir: Path, entity: str) -> int:
    if entity not in ENTITY_MODEL_MAP:
        raise ValueError(f"Unsupported entity: {entity}")

    model = ENTITY_MODEL_MAP[entity]
    csv_path = data_dir / run_id / f"{entity}.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"Missing CSV for entity {entity}: {csv_path}")

    rows = _parse_csv(csv_path)

    # Clear previous rows from this run where applicable.
    if entity in {"stores", "customers", "products", "orders", "store_daily_metrics", "supplier_product_offers"}:
        db.execute(delete(model).where(model.seed_run_id == run_id))
    elif entity == "order_items":
        order_ids = [r["id"] for r in _parse_csv(data_dir / run_id / "orders.csv")]
        if order_ids:
            chunk_size = 5000
            for start in range(0, len(order_ids), chunk_size):
                chunk = order_ids[start : start + chunk_size]
                db.execute(delete(OrderItem).where(OrderItem.order_id.in_(chunk)))
    elif entity == "product_embeddings":
        db.execute(delete(ProductEmbedding).where(ProductEmbedding.seed_run_id == run_id))

    # Deterministic synthetic IDs may collide across different runs.
    # Remove existing rows with the same primary key values before insert.
    if rows:
        pk_columns = [col.name for col in model.__table__.primary_key.columns]
        if len(pk_columns) == 1:
            pk = pk_columns[0]
            if pk in rows[0]:
                pk_values = [r[pk] for r in rows if r.get(pk) is not None]
                if pk_values:
                    chunk_size = 5000
                    for start in range(0, len(pk_values), chunk_size):
                        chunk = pk_values[start : start + chunk_size]
                        db.execute(delete(model).where(getattr(model, pk).in_(chunk)))

    if rows:
        db.bulk_insert_mappings(model, rows)
    db.commit()
    return len(rows)


def finalize_run(db: Session, run_id: str, status: str = "loaded") -> None:
    run = db.get(SyntheticRun, run_id)
    if not run:
        raise ValueError(f"Synthetic run not found: {run_id}")
    run.status = status
    run.completed_at = datetime.now(timezone.utc)
    db.add(run)
    db.commit()


def current_loaded_counts(db: Session, run_id: str) -> dict[str, int]:
    counts = {}
    counts["stores"] = db.scalar(select(func.count()).select_from(Store).where(Store.seed_run_id == run_id)) or 0
    counts["customers"] = db.scalar(select(func.count()).select_from(Customer).where(Customer.seed_run_id == run_id)) or 0
    counts["products"] = db.scalar(select(func.count()).select_from(Product).where(Product.seed_run_id == run_id)) or 0
    counts["orders"] = db.scalar(select(func.count()).select_from(Order).where(Order.seed_run_id == run_id)) or 0
    order_ids_subq = select(Order.id).where(Order.seed_run_id == run_id)
    counts["order_items"] = db.scalar(select(func.count()).select_from(OrderItem).where(OrderItem.order_id.in_(order_ids_subq))) or 0
    counts["store_daily_metrics"] = (
        db.scalar(select(func.count()).select_from(StoreDailyMetric).where(StoreDailyMetric.seed_run_id == run_id)) or 0
    )
    counts["supplier_product_offers"] = (
        db.scalar(select(func.count()).select_from(SupplierProductOffer).where(SupplierProductOffer.seed_run_id == run_id)) or 0
    )
    return counts


def read_generated_counts(data_dir: Path, run_id: str) -> dict[str, int]:
    manifest = data_dir / run_id / "manifest.json"
    if not manifest.exists():
        return {}
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    return payload.get("row_counts", {})
