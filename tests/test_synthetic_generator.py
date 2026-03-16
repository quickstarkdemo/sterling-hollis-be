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
from app.services.customer_preferences import product_allowed_for_sex, top_style_categories


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
