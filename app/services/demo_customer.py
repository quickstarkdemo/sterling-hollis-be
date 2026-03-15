from __future__ import annotations

DEMO_CUSTOMER_ID = "cust_demo_000001"
DEMO_CUSTOMER_PHONE_E164 = "+12146932322"
DEMO_CUSTOMER_EMAIL = "djn12313@gmail.com"
DEMO_CUSTOMER_FIRST_NAME = "Jorgen"
DEMO_CUSTOMER_LAST_NAME = "Nielsen"


def demo_customer_full_name() -> str:
    return f"{DEMO_CUSTOMER_FIRST_NAME} {DEMO_CUSTOMER_LAST_NAME}"
