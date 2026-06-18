from __future__ import annotations

import hashlib


CATALOG_AVAILABILITY_CHOICES = (
    ("in stock", "In stock"),
    ("low stock", "Low stock"),
    ("preorder", "Preorder"),
    ("out of stock", "Out of stock"),
)
CATALOG_AVAILABILITY_VALUES = frozenset(
    value for value, _ in CATALOG_AVAILABILITY_CHOICES
)


def normalized_brand_name(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def display_brand_name(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def catalog_brand_id_for_name(name: object) -> str:
    normalized = normalized_brand_name(name)
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:20]
    return f"brand_{digest}"
