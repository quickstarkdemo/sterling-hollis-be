from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Customer, Order, OrderItem, Product, Store, StoreDailyMetric, SyntheticRun
from app.services.daily_synthetic_orders import (
    DAILY_ORDER_PREFIX,
    DailyOrderGenerationOptions,
    generate_daily_synthetic_orders,
    plan_daily_synthetic_orders,
)


@pytest.fixture()
def sessions():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    try:
        yield TestingSessionLocal
    finally:
        engine.dispose()


def _add_source_data(db, *, latest_order_at: datetime = datetime(2026, 3, 20, tzinfo=timezone.utc)):
    db.add(SyntheticRun(id="run_seed", seed=42, status="loaded", started_at=latest_order_at, config={}))
    stores = [
        Store(
            id="1001",
            seed_run_id="run_seed",
            name="Dallas Downtown",
            city="Dallas",
            state="TX",
            postal_code="75201",
            address_line1="1 Main St",
            address_line2=None,
            phone="555-111-1111",
            latitude=Decimal("32.770000"),
            longitude=Decimal("-96.790000"),
            profile_type="texas_core",
            services=["Personal Shopping"],
            raw_source={},
        ),
        Store(
            id="1002",
            seed_run_id="run_seed",
            name="Miami",
            city="Miami",
            state="FL",
            postal_code="33131",
            address_line1="2 Ocean Dr",
            address_line2=None,
            phone="555-222-2222",
            latitude=Decimal("25.760000"),
            longitude=Decimal("-80.190000"),
            profile_type="resort_luxury",
            services=["Styling"],
            raw_source={},
        ),
    ]
    db.add_all(stores)
    db.add_all(
        [
            Customer(
                id="cust_gold",
                seed_run_id="run_seed",
                home_store_id="1001",
                first_name="Avery",
                last_name="Parker",
                email="avery@example-fashion.test",
                phone_e164="+12145550001",
                city="Dallas",
                state="TX",
                joined_at=latest_order_at - timedelta(days=300),
                loyalty_tier="gold",
                sex="female",
                price_sensitivity=Decimal("0.4200"),
                occasion_affinity={"wedding": 0.8},
                style_vector={"womens_apparel": 0.9, "shoes": 0.7},
                size_preferences={"top": "M", "shoe": "8"},
                channel_preference="hybrid",
                pii_token="token-gold",
            ),
            Customer(
                id="cust_standard",
                seed_run_id="run_seed",
                home_store_id="1002",
                first_name="Jordan",
                last_name="Lee",
                email="jordan@example-fashion.test",
                phone_e164="+12145550002",
                city="Miami",
                state="FL",
                joined_at=latest_order_at - timedelta(days=120),
                loyalty_tier="standard",
                sex="male",
                price_sensitivity=Decimal("0.6200"),
                occasion_affinity={"vacation": 0.9},
                style_vector={"mens_apparel": 0.9, "shoes": 0.7},
                size_preferences={"top": "L", "shoe": "10"},
                channel_preference="online",
                pii_token="token-standard",
            ),
        ]
    )
    db.add_all(
        [
            Product(
                id="prod_dress",
                seed_run_id="run_seed",
                store_id="1001",
                title="Valentino Rose Dress",
                description="Event dress",
                link="https://fashion.example/products/prod_dress",
                image_link="https://fashion.example/images/prod_dress.jpg",
                price=Decimal("750.00"),
                availability="in stock",
                brand="Valentino",
                category="womens_apparel",
                color="Rose",
                size="M",
                material="silk",
                gender="women",
                season="spring",
                margin_pct=Decimal("0.6200"),
                inventory_qty=18,
                objective_weight=Decimal("0.9000"),
                metadata_json={},
            ),
            Product(
                id="prod_pump",
                seed_run_id="run_seed",
                store_id="1001",
                title="Jimmy Choo Satin Pump",
                description="Occasion heel",
                link="https://fashion.example/products/prod_pump",
                image_link="https://fashion.example/images/prod_pump.jpg",
                price=Decimal("595.00"),
                availability="in stock",
                brand="Jimmy Choo",
                category="shoes",
                color="Gold",
                size="8",
                material="satin",
                gender="women",
                season="all-season",
                margin_pct=Decimal("0.5400"),
                inventory_qty=30,
                objective_weight=Decimal("0.7000"),
                metadata_json={},
            ),
            Product(
                id="prod_jacket",
                seed_run_id="run_seed",
                store_id="1002",
                title="Tom Ford Midnight Sport Coat",
                description="Tailored jacket",
                link="https://fashion.example/products/prod_jacket",
                image_link="https://fashion.example/images/prod_jacket.jpg",
                price=Decimal("820.00"),
                availability="preorder",
                brand="Tom Ford",
                category="mens_apparel",
                color="Navy",
                size="M",
                material="wool",
                gender="men",
                season="fall",
                margin_pct=Decimal("0.5800"),
                inventory_qty=6,
                objective_weight=Decimal("0.8800"),
                metadata_json={},
            ),
        ]
    )
    for index in range(7):
        ordered_at = latest_order_at - timedelta(days=index)
        order = Order(
            id=f"seed_order_{index}",
            seed_run_id="run_seed",
            customer_id="cust_gold",
            store_id="1001",
            ordered_at=ordered_at,
            status="completed",
            occasion="wedding",
            channel="in_store",
            subtotal=Decimal("750.00"),
            discount_amount=Decimal("0.00"),
            tax_amount=Decimal("61.88"),
            total_amount=Decimal("811.88"),
            returned=False,
        )
        db.add(order)
        db.add(
            OrderItem(
                id=f"seed_item_{index}",
                order_id=order.id,
                product_id="prod_dress",
                quantity=1,
                unit_price=Decimal("750.00"),
                discount_amount=Decimal("0.00"),
                line_total=Decimal("750.00"),
            )
        )
    db.commit()


def test_plan_daily_synthetic_orders_uses_seasonality_and_bounds(sessions):
    with sessions() as db:
        _add_source_data(db)

        december = plan_daily_synthetic_orders(
            db,
            DailyOrderGenerationOptions(
                from_date=date(2026, 12, 5),
                through_date=date(2026, 12, 5),
                base_orders=100,
                min_orders=1,
                max_orders=500,
            ),
        )
        february = plan_daily_synthetic_orders(
            db,
            DailyOrderGenerationOptions(
                from_date=date(2026, 2, 5),
                through_date=date(2026, 2, 5),
                base_orders=100,
                min_orders=1,
                max_orders=500,
            ),
        )
        capped = plan_daily_synthetic_orders(
            db,
            DailyOrderGenerationOptions(
                from_date=date(2026, 12, 5),
                through_date=date(2026, 12, 5),
                base_orders=1000,
                min_orders=25,
                max_orders=220,
            ),
        )

    assert december.planned_orders > february.planned_orders
    assert capped.planned_orders == 220


def test_plan_daily_synthetic_orders_caps_stale_catchup_to_recent_days(sessions):
    with sessions() as db:
        _add_source_data(db, latest_order_at=datetime(2026, 3, 1, tzinfo=timezone.utc))

        plan = plan_daily_synthetic_orders(
            db,
            DailyOrderGenerationOptions(
                through_date=date(2026, 6, 21),
                max_days=14,
                base_orders=10,
                min_orders=1,
                max_orders=100,
            ),
        )

    assert len(plan.target_days) == 14
    assert plan.capped_days > 0
    assert plan.target_days[0].target_date == date(2026, 6, 8)
    assert plan.target_days[-1].target_date == date(2026, 6, 21)


def test_generate_daily_synthetic_orders_is_idempotent_and_refreshes_metrics(sessions):
    options = DailyOrderGenerationOptions(
        from_date=date(2026, 6, 20),
        through_date=date(2026, 6, 20),
        max_days=1,
        base_orders=8,
        min_orders=8,
        max_orders=8,
    )
    with sessions() as db:
        _add_source_data(db)
        customer_count = db.scalar(select(func.count()).select_from(Customer))

        first = generate_daily_synthetic_orders(db, options)
        second = generate_daily_synthetic_orders(db, options)

        daily_orders = db.scalars(select(Order).where(Order.id.like(f"{DAILY_ORDER_PREFIX}_20260620_%"))).all()
        daily_items = db.scalars(select(OrderItem).where(OrderItem.order_id.in_([order.id for order in daily_orders]))).all()
        metrics = db.scalars(select(StoreDailyMetric).where(StoreDailyMetric.seed_run_id == first.run_id)).all()
        run = db.get(SyntheticRun, first.run_id)

        assert first.run_id == "daily_orders_20260620_20260620"
        assert second.run_id == first.run_id
        assert first.inserted_orders == 8
        assert second.inserted_orders == 8
        assert len(daily_orders) == 8
        assert len(daily_items) == second.inserted_items
        assert second.metrics_refreshed == len(metrics)
        assert metrics
        assert db.scalar(select(func.count()).select_from(Customer)) == customer_count
        assert run is not None
        assert run.status == "loaded"
        assert run.config["kind"] == "daily_synthetic_orders"


def test_generate_daily_synthetic_orders_dry_run_does_not_mutate(sessions):
    with sessions() as db:
        _add_source_data(db)
        before_orders = db.scalar(select(func.count()).select_from(Order))

        result = generate_daily_synthetic_orders(
            db,
            DailyOrderGenerationOptions(
                from_date=date(2026, 6, 20),
                through_date=date(2026, 6, 20),
                base_orders=8,
                min_orders=8,
                max_orders=8,
                dry_run=True,
            ),
        )

        assert result.dry_run is True
        assert result.planned_orders == 8
        assert result.inserted_orders == 0
        assert db.scalar(select(func.count()).select_from(Order)) == before_orders


def test_daily_synthetic_orders_cli_outputs_dry_run_json(monkeypatch, capsys, sessions):
    import app.daily_synthetic_orders as cli

    with sessions() as db:
        _add_source_data(db)

    monkeypatch.setattr(cli, "SessionLocal", sessions)
    monkeypatch.setattr(
        "sys.argv",
        [
            "daily_synthetic_orders",
            "--from-date",
            "2026-06-20",
            "--through-date",
            "2026-06-20",
            "--base-orders",
            "8",
            "--min-orders",
            "8",
            "--max-orders",
            "8",
            "--dry-run",
        ],
    )

    cli.main()

    out = capsys.readouterr().out
    assert '"dry_run": true' in out
    assert '"planned_orders": 8' in out
    assert '"inserted_orders": 0' in out
