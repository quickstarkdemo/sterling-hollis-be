from __future__ import annotations

from sqlalchemy import and_, func, or_

IN_STOCK = "in stock"
PREORDER = "preorder"
OUT_OF_STOCK = "out of stock"


def normalize_availability(value: str | None) -> str:
    return str(value or "").strip().lower()


def is_in_stock(availability: str | None, inventory_qty: int | None) -> bool:
    qty = int(inventory_qty or 0)
    return normalize_availability(availability) == IN_STOCK and qty > 0


def is_preorder(availability: str | None) -> bool:
    return normalize_availability(availability) == PREORDER


def is_out_of_stock(availability: str | None, inventory_qty: int | None) -> bool:
    qty = int(inventory_qty or 0)
    return normalize_availability(availability) == OUT_OF_STOCK or qty <= 0


def is_not_in_stock(availability: str | None, inventory_qty: int | None) -> bool:
    qty = int(inventory_qty or 0)
    return normalize_availability(availability) != IN_STOCK or qty <= 0


def is_customer_recommendation_eligible(availability: str | None, inventory_qty: int | None) -> bool:
    # Customer workspace policy: include preorder; exclude explicit out-of-stock and zero/negative inventory rows.
    if is_preorder(availability):
        return True
    return is_in_stock(availability, inventory_qty)


def sql_availability_token(column_availability):
    return func.lower(func.coalesce(column_availability, ""))


def sql_is_in_stock(column_availability, column_inventory_qty):
    token = sql_availability_token(column_availability)
    return and_(token == IN_STOCK, column_inventory_qty > 0)


def sql_is_preorder(column_availability):
    token = sql_availability_token(column_availability)
    return token == PREORDER


def sql_is_out_of_stock(column_availability):
    token = sql_availability_token(column_availability)
    return token == OUT_OF_STOCK


def sql_is_not_in_stock(column_availability, column_inventory_qty):
    token = sql_availability_token(column_availability)
    return or_(token != IN_STOCK, column_inventory_qty <= 0)
