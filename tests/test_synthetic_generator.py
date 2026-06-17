from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from app.services.synthetic_generator import (
    DEMO_CUSTOMER_EMAIL,
    DEMO_CUSTOMER_FIRST_NAME,
    DEMO_CUSTOMER_ID,
    DEMO_CUSTOMER_LAST_NAME,
    DEMO_CUSTOMER_PHONE_E164,
    DEMO_CUSTOMER_SEX,
    GenerationVolumes,
    generate_synthetic_dataset,
)
from app.services.analyst_story import (
    ANALYST_STORE_CATEGORY_V1_FILE,
    ANALYST_STORE_CATEGORY_V1_HEADERS,
)
from app.services.customer_preferences import product_allowed_for_sex, top_style_categories
from app.services.taxonomy import CATEGORY_TAXONOMY


def _sample_stores(run_id: str) -> list[dict]:
    return [
        {
            "id": "1001",
            "seed_run_id": run_id,
            "name": "Dallas - Downtown",
            "city": "Dallas",
            "state": "TX",
            "postal_code": "75201",
            "address_line1": "1 Main St",
            "address_line2": None,
            "phone": "555-111-1111",
            "latitude": 32.77,
            "longitude": -96.79,
            "profile_type": "texas_core",
            "services": ["Personal Shopping", "Alterations"],
            "raw_source": {},
        },
        {
            "id": "1002",
            "seed_run_id": run_id,
            "name": "Miami",
            "city": "Miami",
            "state": "FL",
            "postal_code": "33131",
            "address_line1": "99 Ocean Dr",
            "address_line2": None,
            "phone": "555-222-2222",
            "latitude": 25.76,
            "longitude": -80.19,
            "profile_type": "resort_luxury",
            "services": ["Personal Shopping"],
            "raw_source": {},
        },
    ]


def _read_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as fp:
        return list(csv.DictReader(fp))


def test_generation_is_deterministic(tmp_path: Path):
    run_id = "run_test_1"
    stores = _sample_stores(run_id)
    now = datetime(2026, 3, 13, tzinfo=timezone.utc)
    volumes = GenerationVolumes(stores=2, products=50, customers=80, orders=200)

    first = generate_synthetic_dataset(
        seed=123,
        run_id=run_id,
        stores=stores,
        volumes=volumes,
        trailing_months=12,
        output_root=tmp_path,
        raw_snapshot={"stores": []},
        now=now,
    )

    second = generate_synthetic_dataset(
        seed=123,
        run_id="run_test_2",
        stores=_sample_stores("run_test_2"),
        volumes=volumes,
        trailing_months=12,
        output_root=tmp_path,
        raw_snapshot={"stores": []},
        now=now,
    )

    first_products = _read_csv(first.output_dir / "products.csv")
    second_products = _read_csv(second.output_dir / "products.csv")

    assert len(first_products) == 50
    assert len(second_products) == 50
    assert first_products[0]["title"] == second_products[0]["title"]
    assert first_products[10]["price"] == second_products[10]["price"]


def test_generated_orders_have_no_orphan_items(tmp_path: Path):
    run_id = "run_test_3"
    stores = _sample_stores(run_id)
    volumes = GenerationVolumes(stores=2, products=40, customers=60, orders=140)

    artifacts = generate_synthetic_dataset(
        seed=777,
        run_id=run_id,
        stores=stores,
        volumes=volumes,
        trailing_months=6,
        output_root=tmp_path,
        raw_snapshot={"stores": []},
        now=datetime(2026, 3, 13, tzinfo=timezone.utc),
    )

    orders = _read_csv(artifacts.output_dir / "orders.csv")
    items = _read_csv(artifacts.output_dir / "order_items.csv")

    order_ids = {o["id"] for o in orders}
    assert len(order_ids) == 140
    assert items
    assert all(i["order_id"] in order_ids for i in items)


def test_products_include_required_feed_fields(tmp_path: Path):
    run_id = "run_test_4"
    stores = _sample_stores(run_id)
    volumes = GenerationVolumes(stores=2, products=30, customers=20, orders=40)

    artifacts = generate_synthetic_dataset(
        seed=42,
        run_id=run_id,
        stores=stores,
        volumes=volumes,
        trailing_months=6,
        output_root=tmp_path,
        raw_snapshot={"stores": []},
        now=datetime(2026, 3, 13, tzinfo=timezone.utc),
    )

    products = _read_csv(artifacts.output_dir / "products.csv")
    required_fields = ["id", "title", "description", "link", "image_link", "price", "availability", "brand", "category"]

    assert len(products) == 30
    for product in products:
        for field in required_fields:
            assert product[field]


def test_customers_include_unique_phones_and_demo_customer(tmp_path: Path):
    run_id = "run_test_5"
    stores = _sample_stores(run_id)
    volumes = GenerationVolumes(stores=2, products=30, customers=40, orders=40)

    artifacts = generate_synthetic_dataset(
        seed=42,
        run_id=run_id,
        stores=stores,
        volumes=volumes,
        trailing_months=6,
        output_root=tmp_path,
        raw_snapshot={"stores": []},
        now=datetime(2026, 3, 13, tzinfo=timezone.utc),
    )

    customers = _read_csv(artifacts.output_dir / "customers.csv")
    phones = [customer["phone_e164"] for customer in customers]

    assert len(customers) == 40
    assert len(phones) == len(set(phones))
    assert any(customer["id"] == DEMO_CUSTOMER_ID and customer["phone_e164"] == DEMO_CUSTOMER_PHONE_E164 for customer in customers)
    assert any(
        customer["id"] == DEMO_CUSTOMER_ID
        and customer["email"] == DEMO_CUSTOMER_EMAIL
        and customer["first_name"] == DEMO_CUSTOMER_FIRST_NAME
        and customer["last_name"] == DEMO_CUSTOMER_LAST_NAME
        and customer["sex"] == DEMO_CUSTOMER_SEX
        for customer in customers
    )
    assert sum(1 for customer in customers if customer["phone_e164"] == DEMO_CUSTOMER_PHONE_E164) == 1

    demo_customer = next(customer for customer in customers if customer["id"] == DEMO_CUSTOMER_ID)
    style_vector = json.loads(demo_customer["style_vector"])
    assert style_vector["mens_apparel"] > style_vector["womens_apparel"]
    assert "womens_apparel" not in top_style_categories(style_vector, "male", limit=3)

    sampled = customers[:30]
    for customer in sampled:
        sex = customer.get("sex")
        if sex not in {"male", "female"}:
            continue
        style = json.loads(customer["style_vector"])
        top = top_style_categories(style, sex, limit=3)
        if sex == "male":
            assert "womens_apparel" not in top
        if sex == "female":
            assert "mens_apparel" not in top


def test_product_gender_filtering_respects_customer_sex():
    assert product_allowed_for_sex("men", "mens_apparel", "male")
    assert product_allowed_for_sex("unisex", "home", "male")
    assert not product_allowed_for_sex("women", "womens_apparel", "male")
    assert product_allowed_for_sex("women", "womens_apparel", "female")
    assert not product_allowed_for_sex("men", "mens_apparel", "female")


def test_customer_names_are_broadly_unique(tmp_path: Path):
    run_id = "run_test_6"
    stores = _sample_stores(run_id)
    volumes = GenerationVolumes(stores=2, products=40, customers=600, orders=80)

    artifacts = generate_synthetic_dataset(
        seed=2026,
        run_id=run_id,
        stores=stores,
        volumes=volumes,
        trailing_months=6,
        output_root=tmp_path,
        raw_snapshot={"stores": []},
        now=datetime(2026, 3, 13, tzinfo=timezone.utc),
    )

    customers = _read_csv(artifacts.output_dir / "customers.csv")
    full_names = [f"{row['first_name']} {row['last_name']}" for row in customers]
    unique_ratio = len(set(full_names)) / max(len(full_names), 1)

    first_name_counts: dict[str, int] = {}
    for row in customers:
        first_name = row["first_name"]
        first_name_counts[first_name] = first_name_counts.get(first_name, 0) + 1
    largest_bucket = max(first_name_counts.values())

    assert unique_ratio >= 0.95
    assert largest_bucket / len(customers) < 0.05


def test_products_are_distributed_across_multiple_stores(tmp_path: Path):
    run_id = "run_test_7"
    stores = _sample_stores(run_id)
    volumes = GenerationVolumes(stores=2, products=120, customers=80, orders=120)

    artifacts = generate_synthetic_dataset(
        seed=31415,
        run_id=run_id,
        stores=stores,
        volumes=volumes,
        trailing_months=6,
        output_root=tmp_path,
        raw_snapshot={"stores": []},
        now=datetime(2026, 3, 13, tzinfo=timezone.utc),
    )

    products = _read_csv(artifacts.output_dir / "products.csv")
    title_store_map: dict[str, set[str]] = {}
    for row in products:
        title_store_map.setdefault(row["title"], set()).add(row["store_id"])

    multi_store_titles = [title for title, store_ids in title_store_map.items() if len(store_ids) > 1]
    assert multi_store_titles
    assert len(multi_store_titles) / max(len(title_store_map), 1) >= 0.25


def test_product_families_share_design_identity_across_color_variants(tmp_path: Path):
    run_id = "run_test_product_families"
    stores = _sample_stores(run_id)
    volumes = GenerationVolumes(stores=2, products=180, customers=40, orders=80)

    artifacts = generate_synthetic_dataset(
        seed=9173,
        run_id=run_id,
        stores=stores,
        volumes=volumes,
        trailing_months=6,
        output_root=tmp_path,
        raw_snapshot={"stores": []},
        now=datetime(2026, 3, 13, tzinfo=timezone.utc),
    )

    products = _read_csv(artifacts.output_dir / "products.csv")
    families: dict[str, list[dict]] = {}
    for row in products:
        style_code = json.loads(row["metadata_json"])["style_code"]
        families.setdefault(style_code, []).append(row)

    coherent_families = [
        rows
        for rows in families.values()
        if len({row["color"] for row in rows}) >= 2
        and len({row["store_id"] for row in rows}) >= 2
        and len({row["size"] for row in rows}) >= 2
    ]

    assert len(products) == volumes.products
    assert coherent_families
    assert all(len({row["title"] for row in rows}) == 1 for rows in coherent_families)


def test_orders_show_holiday_seasonality_curve(tmp_path: Path):
    run_id = "run_test_8"
    stores = _sample_stores(run_id)
    volumes = GenerationVolumes(stores=2, products=120, customers=120, orders=2200)

    artifacts = generate_synthetic_dataset(
        seed=20260317,
        run_id=run_id,
        stores=stores,
        volumes=volumes,
        trailing_months=12,
        output_root=tmp_path,
        raw_snapshot={"stores": []},
        now=datetime(2026, 3, 13, tzinfo=timezone.utc),
    )

    orders = _read_csv(artifacts.output_dir / "orders.csv")
    counts_by_month: dict[int, int] = {}
    for order in orders:
        month = int(order["ordered_at"][5:7])
        counts_by_month[month] = counts_by_month.get(month, 0) + 1

    assert counts_by_month[12] > counts_by_month[11]
    assert counts_by_month[11] > counts_by_month[2]
    assert counts_by_month[12] > counts_by_month[1]


def test_analyst_store_category_csv_contract_and_divergence(tmp_path: Path):
    run_id = "run_test_9"
    stores = [
        *_sample_stores(run_id),
        {
            "id": "1003",
            "seed_run_id": run_id,
            "name": "NYC - Flagship",
            "city": "New York",
            "state": "NY",
            "postal_code": "10001",
            "address_line1": "500 5th Ave",
            "address_line2": None,
            "phone": "555-333-3333",
            "latitude": 40.75,
            "longitude": -73.99,
            "profile_type": "flagship_urban",
            "services": ["Personal Shopping"],
            "raw_source": {},
        },
        {
            "id": "1004",
            "seed_run_id": run_id,
            "name": "Houston - Galleria",
            "city": "Houston",
            "state": "TX",
            "postal_code": "77056",
            "address_line1": "8 Market St",
            "address_line2": None,
            "phone": "555-444-4444",
            "latitude": 29.74,
            "longitude": -95.46,
            "profile_type": "suburban_affluent",
            "services": ["Alterations"],
            "raw_source": {},
        },
    ]
    volumes = GenerationVolumes(stores=4, products=520, customers=320, orders=2600)

    artifacts = generate_synthetic_dataset(
        seed=20260320,
        run_id=run_id,
        stores=stores,
        volumes=volumes,
        trailing_months=18,
        output_root=tmp_path,
        raw_snapshot={"stores": []},
        now=datetime(2026, 3, 13, tzinfo=timezone.utc),
    )

    path = artifacts.output_dir / ANALYST_STORE_CATEGORY_V1_FILE
    with path.open("r", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        rows = list(reader)
        assert reader.fieldnames == ANALYST_STORE_CATEGORY_V1_HEADERS

    assert len(rows) == 30

    store_ids = {row["id"] for row in _read_csv(artifacts.output_dir / "stores.csv")}
    categories = set(CATEGORY_TAXONOMY.keys())

    required = [
        "as_of_date",
        "store_id",
        "store_name",
        "state",
        "profile_type",
        "category",
        "category_label",
        "analyst_priority",
        "divergence_flag",
        "analyst_rationale",
    ]
    for row in rows:
        for field in required:
            assert row[field]
        assert row["store_id"] in store_ids
        assert row["category"] in categories
        assert row["category_label"] == CATEGORY_TAXONOMY[row["category"]]["label"]
        assert row["lookback_days"] == "90"
        assert row["prior_lookback_days"] == "90"
        assert row["analyst_priority"] in {"grow", "protect", "deprioritize"}
        assert row["divergence_flag"] in {"aligned", "contrarian"}

        assert float(row["current_revenue"]) >= 0.0
        assert float(row["prior_revenue"]) >= 0.0
        assert float(row["current_units"]) >= 0.0
        assert float(row["prior_units"]) >= 0.0
        assert 0.0 <= float(row["analyst_recommended_discount_pct"]) <= 20.0
        assert -40.0 <= float(row["analyst_floor_space_shift_pct"]) <= 40.0
        assert 0.0 <= float(row["current_margin_rate_pct"]) <= 100.0
        assert 0.0 <= float(row["analyst_confidence"]) <= 1.0

    contrarian_count = sum(1 for row in rows if row["divergence_flag"] == "contrarian")
    ratio = contrarian_count / max(len(rows), 1)
    assert 0.2 <= ratio <= 0.4

    for row in rows[:5]:
        rationale = row["analyst_rationale"].lower()
        priority = row["analyst_priority"]
        if priority == "grow":
            assert "grow" in rationale
        elif priority == "protect":
            assert "protect" in rationale
        else:
            assert "deprioritize" in rationale or "rotate" in rationale or "reallocate" in rationale
        if row["divergence_flag"] == "contrarian":
            assert "contrarian" in rationale
