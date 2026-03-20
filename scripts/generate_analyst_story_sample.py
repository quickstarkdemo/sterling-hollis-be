#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import argparse
import tempfile
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.synthetic_generator import GenerationVolumes, generate_synthetic_dataset


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a 30-row analyst_store_category_v1 sample CSV.")
    parser.add_argument("--output", default="examples/analyst_store_category_v1_sample.csv")
    parser.add_argument("--seed", type=int, default=20260320)
    parser.add_argument("--as-of-date", default="2026-03-13")
    args = parser.parse_args()

    run_id = "run_analyst_sample"
    output_path = Path(args.output)
    as_of = datetime.fromisoformat(args.as_of_date).replace(tzinfo=timezone.utc)

    tmp_root = Path(tempfile.mkdtemp(prefix="analyst_story_sample_"))
    artifacts = generate_synthetic_dataset(
        seed=args.seed,
        run_id=run_id,
        stores=_sample_stores(run_id),
        volumes=GenerationVolumes(stores=4, products=520, customers=320, orders=2600),
        trailing_months=18,
        output_root=tmp_root,
        raw_snapshot={"stores": []},
        now=as_of,
    )

    source_csv = artifacts.output_dir / "analyst_store_category_v1.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_csv, output_path)
    print(output_path)


if __name__ == "__main__":
    main()
