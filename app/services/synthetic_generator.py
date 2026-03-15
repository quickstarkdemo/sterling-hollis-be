from __future__ import annotations

import csv
import hashlib
import json
import random
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from app.services.taxonomy import (
    CATEGORY_TAXONOMY,
    KNOWN_COLORS,
    KNOWN_SEASONS,
    KNOWN_SIZES,
    OCCASION_TO_CATEGORY,
    REAL_BRANDS,
    STORE_ASSORTMENT_PROFILES,
    SYNTHETIC_BRANDS,
)
from app.services.demo_customer import (
    DEMO_CUSTOMER_EMAIL,
    DEMO_CUSTOMER_FIRST_NAME,
    DEMO_CUSTOMER_ID,
    DEMO_CUSTOMER_LAST_NAME,
    DEMO_CUSTOMER_PHONE_E164,
)

FIRST_NAMES = [
    "Avery", "Jordan", "Taylor", "Emerson", "Morgan", "Riley", "Parker", "Casey", "Alex", "Sydney",
    "Elliot", "Harper", "Reese", "Rowan", "Quinn", "Cameron", "Dakota", "Sawyer", "Finley", "Logan",
]
LAST_NAMES = [
    "Parker", "Sullivan", "Hughes", "Bennett", "Montgomery", "Reed", "Coleman", "Prescott", "Hayes", "Warren",
    "Kensington", "Foster", "Dalton", "Whitman", "Bishop", "Caldwell", "Sinclair", "Monroe", "Barrett", "Callahan",
]
LOYALTY_TIERS = ["standard", "silver", "gold", "platinum"]
CHANNELS = ["in_store", "online", "hybrid"]
OCCASIONS = ["wedding", "vacation", "workwear", "holiday_party", "everyday_luxury"]
@dataclass
class GenerationVolumes:
    stores: int = 36
    products: int = 4000
    customers: int = 12000
    orders: int = 80000


@dataclass
class SyntheticArtifacts:
    run_id: str
    output_dir: Path
    row_counts: dict[str, int]


def _weighted_choice(rng: random.Random, options: list[tuple[str, float]]) -> str:
    total = sum(weight for _, weight in options)
    roll = rng.random() * total
    acc = 0.0
    for value, weight in options:
        acc += weight
        if roll <= acc:
            return value
    return options[-1][0]


def _hash_token(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _to_decimal(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _json(value: dict | list) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _synthetic_phone_e164(idx: int) -> str:
    base_number = 2_000_000_000 + idx
    return f"+1{base_number:010d}"


def _pick_category_for_profile(rng: random.Random, profile_type: str) -> str:
    profile = STORE_ASSORTMENT_PROFILES.get(profile_type, STORE_ASSORTMENT_PROFILES["suburban_affluent"])
    return _weighted_choice(rng, list(profile.items()))


def _occasion_for_month(rng: random.Random, month: int) -> str:
    if month in {5, 6, 9, 10}:
        options = [("wedding", 0.34), ("vacation", 0.20), ("workwear", 0.20), ("everyday_luxury", 0.16), ("holiday_party", 0.10)]
    elif month in {11, 12}:
        options = [("holiday_party", 0.46), ("everyday_luxury", 0.24), ("wedding", 0.10), ("vacation", 0.12), ("workwear", 0.08)]
    elif month in {7, 8}:
        options = [("vacation", 0.38), ("everyday_luxury", 0.24), ("workwear", 0.18), ("wedding", 0.10), ("holiday_party", 0.10)]
    else:
        options = [("workwear", 0.31), ("everyday_luxury", 0.25), ("vacation", 0.16), ("wedding", 0.14), ("holiday_party", 0.14)]
    return _weighted_choice(rng, options)


def generate_customers(
    rng: random.Random,
    run_id: str,
    stores: list[dict],
    count: int,
    now: datetime,
) -> list[dict]:
    customers: list[dict] = []
    if count <= 0:
        return customers

    demo_store = stores[0]
    demo_joined_at = now - timedelta(days=540)
    customers.append(
        {
            "id": DEMO_CUSTOMER_ID,
            "seed_run_id": run_id,
            "home_store_id": demo_store["id"],
            "first_name": DEMO_CUSTOMER_FIRST_NAME,
            "last_name": DEMO_CUSTOMER_LAST_NAME,
            "email": DEMO_CUSTOMER_EMAIL,
            "phone_e164": DEMO_CUSTOMER_PHONE_E164,
            "city": demo_store["city"],
            "state": demo_store["state"],
            "joined_at": _iso(demo_joined_at),
            "loyalty_tier": "gold",
            "price_sensitivity": 0.38,
            "occasion_affinity": _json({"wedding": 0.95, "vacation": 0.55, "workwear": 0.35, "holiday_party": 0.82, "everyday_luxury": 0.74}),
            "style_vector": _json({cat: (0.85 if cat == "womens_apparel" else 0.45) for cat in CATEGORY_TAXONOMY.keys()}),
            "size_preferences": _json({"top": "M", "bottom": "8", "shoe": "8"}),
            "channel_preference": "hybrid",
            "pii_token": _hash_token(DEMO_CUSTOMER_EMAIL),
        }
    )

    for idx in range(max(count - 1, 0)):
        cid = f"cust_{idx + 1:06d}"
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        store = rng.choice(stores)

        loyalty = _weighted_choice(rng, [("standard", 0.46), ("silver", 0.28), ("gold", 0.18), ("platinum", 0.08)])
        tier_ps = {
            "standard": (0.60, 0.95),
            "silver": (0.45, 0.82),
            "gold": (0.30, 0.68),
            "platinum": (0.15, 0.52),
        }
        ps_low, ps_high = tier_ps[loyalty]
        price_sensitivity = round(rng.uniform(ps_low, ps_high), 4)

        joined_days_ago = rng.randint(30, 365 * 5)
        joined_at = now - timedelta(days=joined_days_ago)

        occ_affinity = {occ: round(rng.uniform(0.05, 1.0), 4) for occ in OCCASIONS}
        style_vector = {cat: round(rng.uniform(0.05, 1.0), 4) for cat in CATEGORY_TAXONOMY.keys()}
        size_preferences = {
            "top": rng.choice(KNOWN_SIZES),
            "bottom": rng.choice(KNOWN_SIZES),
            "shoe": rng.choice(["6", "7", "8", "9", "10", "11", "12"]),
        }
        channel_pref = _weighted_choice(rng, [("in_store", 0.35), ("online", 0.30), ("hybrid", 0.35)])

        email = f"{first.lower()}.{last.lower()}.{idx + 1}@example-fashion.test"
        phone_e164 = _synthetic_phone_e164(idx + 1)

        customers.append(
            {
                "id": cid,
                "seed_run_id": run_id,
                "home_store_id": store["id"],
                "first_name": first,
                "last_name": last,
                "email": email,
                "phone_e164": phone_e164,
                "city": store["city"],
                "state": store["state"],
                "joined_at": _iso(joined_at),
                "loyalty_tier": loyalty,
                "price_sensitivity": price_sensitivity,
                "occasion_affinity": _json(occ_affinity),
                "style_vector": _json(style_vector),
                "size_preferences": _json(size_preferences),
                "channel_preference": channel_pref,
                "pii_token": _hash_token(email),
            }
        )

    return customers


def _assert_unique_customers(customers: list[dict]) -> None:
    phones = [customer["phone_e164"] for customer in customers]
    emails = [customer["email"] for customer in customers]
    if len(phones) != len(set(phones)):
        raise ValueError("Generated duplicate phone_e164 values in customers.csv")
    if len(emails) != len(set(emails)):
        raise ValueError("Generated duplicate email values in customers.csv")
    if DEMO_CUSTOMER_PHONE_E164 in phones[1:] or phones.count(DEMO_CUSTOMER_PHONE_E164) != 1:
        raise ValueError("Demo customer phone number collision detected")


def generate_products(
    rng: random.Random,
    run_id: str,
    stores: list[dict],
    count: int,
) -> list[dict]:
    products: list[dict] = []
    brands = REAL_BRANDS + SYNTHETIC_BRANDS

    base_per_store = count // len(stores)
    extra = count % len(stores)

    product_idx = 1
    for sidx, store in enumerate(stores):
        target = base_per_store + (1 if sidx < extra else 0)
        for _ in range(target):
            category_key = _pick_category_for_profile(rng, store["profile_type"])
            cat_cfg = CATEGORY_TAXONOMY[category_key]

            item = rng.choice(cat_cfg["items"])
            material = rng.choice(cat_cfg["materials"])
            color = rng.choice(KNOWN_COLORS)
            gender = rng.choice(cat_cfg["genders"])
            season = rng.choice(KNOWN_SEASONS)

            price_min, price_max = cat_cfg["price"]
            price = round(rng.uniform(price_min, price_max), 2)

            brand = rng.choice(brands)
            title = f"{brand} {color} {item}"
            description = (
                f"{cat_cfg['label']} piece in {material}. Crafted for {season} dressing and occasion-led styling "
                f"across {store['city']} and online clients."
            )

            availability = _weighted_choice(rng, [("in stock", 0.82), ("out of stock", 0.08), ("preorder", 0.10)])
            inventory_qty = 0 if availability == "out of stock" else rng.randint(2, 40)
            margin_pct = round(rng.uniform(0.35, 0.72), 4)
            objective_weight = round(rng.uniform(0.1, 1.0), 4)

            pid = f"prod_{product_idx:06d}"
            products.append(
                {
                    "id": pid,
                    "seed_run_id": run_id,
                    "store_id": store["id"],
                    "title": title,
                    "description": description,
                    "link": f"https://fashion.example/products/{pid}",
                    "image_link": f"https://fashion.example/images/{pid}.jpg",
                    "price": f"{price:.2f}",
                    "availability": availability,
                    "brand": brand,
                    "category": category_key,
                    "color": color,
                    "size": rng.choice(KNOWN_SIZES),
                    "material": material,
                    "gender": gender,
                    "season": season,
                    "margin_pct": margin_pct,
                    "inventory_qty": inventory_qty,
                    "objective_weight": objective_weight,
                    "metadata_json": _json(
                        {
                            "profile": store["profile_type"],
                            "feed_label": cat_cfg["label"],
                        }
                    ),
                }
            )
            product_idx += 1

    return products


def generate_orders_and_items(
    rng: random.Random,
    run_id: str,
    stores: list[dict],
    customers: list[dict],
    products: list[dict],
    order_count: int,
    trailing_months: int,
    now: datetime,
) -> tuple[list[dict], list[dict]]:
    products_by_store: dict[str, list[dict]] = defaultdict(list)
    products_by_store_cat: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for p in products:
        products_by_store[p["store_id"]].append(p)
        products_by_store_cat[(p["store_id"], p["category"])].append(p)

    customer_map = {c["id"]: c for c in customers}

    loyalty_weight = {"standard": 1.0, "silver": 1.6, "gold": 2.3, "platinum": 3.0}
    cust_weighted: list[tuple[str, float]] = [
        (c["id"], loyalty_weight.get(c["loyalty_tier"], 1.0)) for c in customers
    ]

    orders: list[dict] = []
    items: list[dict] = []

    start_date = now - timedelta(days=30 * trailing_months)
    total_days = max(1, (now - start_date).days)

    item_idx = 1
    for oidx in range(order_count):
        customer_id = _weighted_choice(rng, cust_weighted)
        customer = customer_map[customer_id]

        if rng.random() < 0.82:
            store_id = customer["home_store_id"]
        else:
            store_id = rng.choice(stores)["id"]

        # Recency bias + seasonal spikes
        day_offset = int(total_days * (rng.random() ** 0.62))
        ordered_at = start_date + timedelta(days=day_offset, hours=rng.randint(8, 21), minutes=rng.randint(0, 59))

        occasion = _occasion_for_month(rng, ordered_at.month)
        channel = customer["channel_preference"]
        if channel == "hybrid":
            channel = _weighted_choice(rng, [("online", 0.52), ("in_store", 0.48)])

        base_categories = OCCASION_TO_CATEGORY.get(occasion, list(CATEGORY_TAXONOMY.keys()))
        n_items = _weighted_choice(rng, [("1", 0.45), ("2", 0.34), ("3", 0.17), ("4", 0.04)])
        n_items_int = int(n_items)

        subtotal = Decimal("0.00")
        order_id = f"ord_{oidx + 1:07d}"

        for _ in range(n_items_int):
            if rng.random() < 0.74:
                category = rng.choice(base_categories)
            else:
                category = rng.choice(list(CATEGORY_TAXONOMY.keys()))

            pool = products_by_store_cat.get((store_id, category)) or products_by_store.get(store_id)
            if not pool:
                continue

            product = rng.choice(pool)
            qty = 1 if rng.random() < 0.88 else 2
            unit_price = Decimal(product["price"])

            discount_rate = 0.0
            if rng.random() < 0.22:
                discount_rate = rng.choice([0.05, 0.10, 0.15, 0.20])

            discount_amount = (unit_price * Decimal(qty) * Decimal(str(discount_rate))).quantize(Decimal("0.01"))
            line_total = (unit_price * Decimal(qty) - discount_amount).quantize(Decimal("0.01"))
            subtotal += line_total

            item_id = f"item_{item_idx:09d}"
            item_idx += 1
            items.append(
                {
                    "id": item_id,
                    "order_id": order_id,
                    "product_id": product["id"],
                    "quantity": qty,
                    "unit_price": f"{unit_price:.2f}",
                    "discount_amount": f"{discount_amount:.2f}",
                    "line_total": f"{line_total:.2f}",
                }
            )

        if subtotal <= Decimal("0.00"):
            # safety fallback
            fallback = rng.choice(products_by_store.get(store_id) or products)
            unit_price = Decimal(fallback["price"])
            subtotal = unit_price
            item_id = f"item_{item_idx:09d}"
            item_idx += 1
            items.append(
                {
                    "id": item_id,
                    "order_id": order_id,
                    "product_id": fallback["id"],
                    "quantity": 1,
                    "unit_price": f"{unit_price:.2f}",
                    "discount_amount": "0.00",
                    "line_total": f"{unit_price:.2f}",
                }
            )

        tax_amount = (subtotal * Decimal("0.0825")).quantize(Decimal("0.01"))
        returned_prob = 0.07 if occasion in {"wedding", "holiday_party"} else 0.12
        returned = rng.random() < returned_prob
        total_amount = (subtotal + tax_amount).quantize(Decimal("0.01"))

        orders.append(
            {
                "id": order_id,
                "seed_run_id": run_id,
                "customer_id": customer_id,
                "store_id": store_id,
                "ordered_at": _iso(ordered_at),
                "status": "returned" if returned else "completed",
                "occasion": occasion,
                "channel": channel,
                "subtotal": f"{subtotal:.2f}",
                "discount_amount": "0.00",
                "tax_amount": f"{tax_amount:.2f}",
                "total_amount": f"{total_amount:.2f}",
                "returned": str(returned).lower(),
            }
        )

    return orders, items


def build_store_daily_metrics(run_id: str, orders: list[dict], items: list[dict], products: list[dict]) -> list[dict]:
    product_lookup = {p["id"]: p for p in products}
    order_lookup = {o["id"]: o for o in orders}
    agg: dict[tuple[str, date], dict] = defaultdict(lambda: {"revenue": Decimal("0.00"), "units": 0, "margin_total": Decimal("0.00")})

    for item in items:
        order = order_lookup[item["order_id"]]
        product = product_lookup[item["product_id"]]
        metric_date = datetime.fromisoformat(order["ordered_at"].replace("Z", "+00:00")).date()
        key = (order["store_id"], metric_date)

        qty = int(item["quantity"])
        line_total = Decimal(item["line_total"])
        margin_pct = Decimal(str(product["margin_pct"]))

        agg[key]["revenue"] += line_total
        agg[key]["units"] += qty
        agg[key]["margin_total"] += (line_total * margin_pct)

    metrics: list[dict] = []
    for idx, ((store_id, metric_date), vals) in enumerate(sorted(agg.items()), start=1):
        revenue = vals["revenue"].quantize(Decimal("0.01"))
        units = vals["units"]
        aov = (revenue / Decimal(max(units, 1))).quantize(Decimal("0.01"))
        margin_rate = (vals["margin_total"] / revenue).quantize(Decimal("0.0001")) if revenue > 0 else Decimal("0.0000")
        # Synthetic sell-through approximation
        sell_through = min(Decimal("0.9999"), Decimal(units) / Decimal(max(120, units + 20))).quantize(Decimal("0.0001"))

        metrics.append(
            {
                "seed_run_id": run_id,
                "store_id": store_id,
                "metric_date": metric_date.isoformat(),
                "revenue": f"{revenue:.2f}",
                "units_sold": units,
                "sell_through": f"{sell_through:.4f}",
                "aov": f"{aov:.2f}",
                "margin_rate": f"{margin_rate:.4f}",
            }
        )

    return metrics


def _write_csv(path: Path, rows: list[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return 0

    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        normalized_rows = []
        for row in rows:
            normalized = {}
            for key, value in row.items():
                if isinstance(value, (dict, list)):
                    normalized[key] = json.dumps(value, separators=(",", ":"), sort_keys=True)
                else:
                    normalized[key] = value
            normalized_rows.append(normalized)
        writer.writerows(normalized_rows)
    return len(rows)


def materialize_synthetic_csvs(
    output_root: Path,
    run_id: str,
    stores: list[dict],
    customers: list[dict],
    products: list[dict],
    orders: list[dict],
    order_items: list[dict],
    store_daily_metrics: list[dict],
    raw_snapshot: dict,
    config: dict,
) -> SyntheticArtifacts:
    output_dir = output_root / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    row_counts = {
        "stores": _write_csv(output_dir / "stores.csv", stores),
        "customers": _write_csv(output_dir / "customers.csv", customers),
        "products": _write_csv(output_dir / "products.csv", products),
        "orders": _write_csv(output_dir / "orders.csv", orders),
        "order_items": _write_csv(output_dir / "order_items.csv", order_items),
        "store_daily_metrics": _write_csv(output_dir / "store_daily_metrics.csv", store_daily_metrics),
    }

    (output_dir / "snapshot.json").write_text(json.dumps(raw_snapshot), encoding="utf-8")
    (output_dir / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "row_counts": row_counts,
                "config": config,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    return SyntheticArtifacts(run_id=run_id, output_dir=output_dir, row_counts=row_counts)


def generate_synthetic_dataset(
    seed: int,
    run_id: str,
    stores: list[dict],
    volumes: GenerationVolumes,
    trailing_months: int,
    output_root: Path,
    raw_snapshot: dict,
    now: datetime | None = None,
) -> SyntheticArtifacts:
    if now is None:
        now = datetime.now(timezone.utc)

    rng = random.Random(seed)

    selected_stores = stores[: volumes.stores] if volumes.stores < len(stores) else list(stores)

    customers = generate_customers(rng, run_id, selected_stores, volumes.customers, now)
    _assert_unique_customers(customers)
    products = generate_products(rng, run_id, selected_stores, volumes.products)
    orders, order_items = generate_orders_and_items(
        rng,
        run_id,
        selected_stores,
        customers,
        products,
        volumes.orders,
        trailing_months,
        now,
    )
    metrics = build_store_daily_metrics(run_id, orders, order_items, products)

    return materialize_synthetic_csvs(
        output_root=output_root,
        run_id=run_id,
        stores=selected_stores,
        customers=customers,
        products=products,
        orders=orders,
        order_items=order_items,
        store_daily_metrics=metrics,
        raw_snapshot=raw_snapshot,
        config={
            "seed": seed,
            "volumes": volumes.__dict__,
            "trailing_months": trailing_months,
        },
    )


def new_run_id() -> str:
    return f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
