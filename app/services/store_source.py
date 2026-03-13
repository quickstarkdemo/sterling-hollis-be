from __future__ import annotations

import json
from pathlib import Path

import httpx

ALL_STORES_URL = "https://stores.neimanmarcus.com/info/min/allStoresAddr_nm.min.json"
STORE_DETAIL_URL = "https://stores.neimanmarcus.com/info/{store_id}.json"


def infer_profile_type(store: dict) -> str:
    state = (store.get("state") or "").strip().upper()
    city = (store.get("city") or "").strip().lower()
    name = (store.get("name") or "").strip().lower()

    if state == "TX":
        return "texas_core"

    if state in {"FL", "HI", "NV"}:
        return "resort_luxury"

    if any(token in city or token in name for token in ["san francisco", "chicago", "boston", "beverly hills", "new york", "dallas"]):
        return "flagship_urban"

    return "suburban_affluent"


def _flatten_store_map(store_map: dict) -> list[dict]:
    flat: list[dict] = []
    for state_name, stores in store_map.items():
        for s in stores:
            record = dict(s)
            record["state_group"] = state_name
            flat.append(record)
    flat.sort(key=lambda x: x.get("id", ""))
    return flat


def fetch_neiman_store_snapshot(cache_path: Path | None = None, timeout_seconds: int = 30) -> dict:
    if cache_path is None:
        cache_path = Path("data/neiman_stores_snapshot.json")

    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
            all_stores_resp = client.get(ALL_STORES_URL)
            all_stores_resp.raise_for_status()
            store_map = all_stores_resp.json()

            stores = _flatten_store_map(store_map)
            details = {}
            for store in stores:
                sid = str(store["id"])
                detail_resp = client.get(STORE_DETAIL_URL.format(store_id=sid))
                if detail_resp.status_code == 200:
                    details[sid] = detail_resp.json()
                else:
                    details[sid] = {}

        snapshot = {
            "all_stores_url": ALL_STORES_URL,
            "detail_url_template": STORE_DETAIL_URL,
            "store_map": store_map,
            "stores": stores,
            "details": details,
        }

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(snapshot), encoding="utf-8")
        return snapshot
    except Exception:
        if cache_path.exists():
            return json.loads(cache_path.read_text(encoding="utf-8"))
        raise


def normalize_stores(snapshot: dict, seed_run_id: str) -> list[dict]:
    stores: list[dict] = []
    for src in snapshot.get("stores", []):
        sid = str(src.get("id"))
        detail = snapshot.get("details", {}).get(sid, {})
        standard_services = detail.get("standardServices") or []
        service_names = [s.get("name", "").strip() for s in standard_services if s.get("name")]
        phone_numbers = src.get("phoneNumbers") or []

        stores.append(
            {
                "id": sid,
                "seed_run_id": seed_run_id,
                "name": src.get("name", "Unknown").strip(),
                "city": (src.get("city") or "").strip(),
                "state": (src.get("state") or "").strip(),
                "postal_code": (src.get("postalCode") or "").strip(),
                "address_line1": (src.get("addressLine1") or "").strip(),
                "address_line2": (src.get("addressLine2") or "").strip() or None,
                "phone": phone_numbers[0] if phone_numbers else None,
                "latitude": float(src.get("lat")) if src.get("lat") else None,
                "longitude": float(src.get("lng")) if src.get("lng") else None,
                "profile_type": infer_profile_type(src),
                "services": service_names,
                "raw_source": {
                    "store": src,
                    "detail": detail,
                },
            }
        )

    stores.sort(key=lambda s: s["id"])
    return stores
