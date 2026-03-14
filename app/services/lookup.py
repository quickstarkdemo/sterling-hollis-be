from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Customer, Store
from app.schemas import CustomerSearchResponse, CustomerSearchResult, ResolvedCustomer, ResolvedStore


@dataclass
class StoreMatch:
    resolved: ResolvedStore
    alternatives: list[ResolvedStore]


@dataclass
class CustomerMatch:
    resolved: ResolvedCustomer


_STATE_NAMES = {
    "tx": "texas",
    "fl": "florida",
    "ca": "california",
    "ny": "new york",
    "il": "illinois",
    "ma": "massachusetts",
    "nv": "nevada",
    "hi": "hawaii",
    "co": "colorado",
}


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.lower().replace("-", " ").replace(",", " ").split())


def _digits(text: str | None) -> str:
    return re.sub(r"\D", "", text or "")


def _mask_phone(phone_e164: str) -> str:
    digits = _digits(phone_e164)
    if len(digits) < 4:
        return phone_e164
    return f"(***) ***-{digits[-4:]}"


def _resolved_customer(customer: Customer, home_store: Store | None, match_reason: str) -> ResolvedCustomer:
    return ResolvedCustomer(
        id=customer.id,
        email=customer.email,
        phone_e164=customer.phone_e164,
        full_name=f"{customer.first_name} {customer.last_name}",
        first_name=customer.first_name,
        last_name=customer.last_name,
        home_store_id=customer.home_store_id,
        home_store_name=home_store.name if home_store else customer.home_store_id,
        loyalty_tier=customer.loyalty_tier,
        match_reason=match_reason,
    )


def _score_store(store: Store, query: str) -> tuple[float, str]:
    norm_query = _normalize(query)
    if not norm_query:
        return 0.0, "no query"

    if norm_query == store.id.lower():
        return 100.0, "exact store id"

    name = _normalize(store.name)
    city = _normalize(store.city)
    state = _normalize(store.state)
    state_name = _STATE_NAMES.get(state, state)
    joined = f"{name} {city} {state} {state_name}".strip()

    score = 0.0
    reasons: list[str] = []
    if norm_query == name:
        score += 90
        reasons.append("exact store name")
    if norm_query == city:
        score += 85
        reasons.append("exact city")
    if norm_query in {state, state_name}:
        score += 60
        reasons.append("state match")

    tokens = [token for token in norm_query.split() if token]
    if tokens and all(token in joined for token in tokens):
        score += 40 + len(tokens) * 5
        reasons.append("all query tokens matched")
    else:
        for token in tokens:
            if token in name:
                score += 12
            elif token in city:
                score += 10
            elif token in {state, state_name}:
                score += 8

    if city and city in norm_query:
        score += 20
        reasons.append("city mentioned")
    if name and name in norm_query:
        score += 25
        reasons.append("store name mentioned")

    reason = ", ".join(dict.fromkeys(reasons)) if reasons else "best fuzzy match"
    return score, reason


def resolve_store(session: Session, store_query: str | None = None, store_id: str | None = None) -> StoreMatch:
    stores = session.scalars(select(Store).order_by(Store.name)).all()
    if not stores:
        raise ValueError("No stores are loaded.")

    if store_id:
        store = session.get(Store, store_id)
        if not store:
            raise ValueError(f"Store {store_id} was not found.")
        resolved = ResolvedStore(
            id=store.id,
            name=store.name,
            city=store.city,
            state=store.state,
            profile_type=store.profile_type,
            match_reason="exact store id",
            match_score=100.0,
        )
        return StoreMatch(resolved=resolved, alternatives=[])

    if not store_query:
        raise ValueError("Provide store_query or store_id.")

    scored: list[tuple[float, ResolvedStore]] = []
    for store in stores:
        score, reason = _score_store(store, store_query)
        if score <= 0:
            continue
        scored.append(
            (
                score,
                ResolvedStore(
                    id=store.id,
                    name=store.name,
                    city=store.city,
                    state=store.state,
                    profile_type=store.profile_type,
                    match_reason=reason,
                    match_score=round(score, 2),
                ),
            )
        )

    if not scored:
        raise ValueError(f"No store matched '{store_query}'.")

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best = scored[0]
    alternatives = [resolved for score, resolved in scored[1:4] if best_score - score <= 18]
    return StoreMatch(resolved=best, alternatives=alternatives)


def find_customers(session: Session, query: str, limit: int = 10) -> CustomerSearchResponse:
    normalized = _normalize(query)
    digits = _digits(query)
    if not normalized and not digits:
        raise ValueError("Provide a customer query.")

    filters = []
    if normalized:
        like = f"%{normalized}%"
        filters.extend(
            [
                Customer.email.ilike(like),
                Customer.first_name.ilike(like),
                Customer.last_name.ilike(like),
            ]
        )
        if " " in normalized:
            first, _, last = normalized.partition(" ")
            filters.append((Customer.first_name.ilike(f"%{first}%") & Customer.last_name.ilike(f"%{last}%")))
    if digits:
        filters.append(Customer.phone_e164.like(f"%{digits[-10:]}%"))
        if len(digits) >= 4:
            filters.append(Customer.phone_e164.like(f"%{digits[-4:]}"))

    customers = session.scalars(select(Customer).where(or_(*filters)).limit(max(limit * 5, 25))).all()
    if not customers:
        return CustomerSearchResponse(query=query, results=[])

    scored: list[CustomerSearchResult] = []
    for customer in customers:
        home_store = session.get(Store, customer.home_store_id)
        full_name = f"{customer.first_name} {customer.last_name}"
        score = 0.0
        reason = "partial match"

        email = customer.email.lower()
        if normalized and normalized == email:
            score = 100.0
            reason = "exact email"
        elif digits and digits == _digits(customer.phone_e164):
            score = 99.0
            reason = "exact phone"
        elif digits and len(digits) == 4 and customer.phone_e164.endswith(digits):
            score = 88.0
            reason = "phone last 4"
        else:
            if normalized and normalized == full_name.lower():
                score += 95.0
                reason = "exact name"
            elif normalized and normalized in email:
                score += 84.0
                reason = "email contains query"
            else:
                for token in normalized.split():
                    if token in customer.first_name.lower():
                        score += 22.0
                    if token in customer.last_name.lower():
                        score += 24.0
                    if token in email:
                        score += 18.0
                if digits and len(digits) >= 4 and customer.phone_e164.endswith(digits[-4:]):
                    score += 25.0
                    reason = "phone suffix"

        if score <= 0:
            continue

        scored.append(
            CustomerSearchResult(
                **_resolved_customer(customer, home_store, reason).model_dump(),
                masked_phone=_mask_phone(customer.phone_e164),
                match_score=round(score, 2),
            )
        )

    scored.sort(key=lambda item: (item.match_score, item.full_name), reverse=True)
    return CustomerSearchResponse(query=query, results=scored[:limit])


def resolve_customer(
    session: Session,
    email: str | None = None,
    customer_id: str | None = None,
    phone_e164: str | None = None,
    phone_last4: str | None = None,
) -> CustomerMatch:
    customer: Customer | None = None
    match_reason = ""

    if email:
        normalized = email.strip().lower()
        customer = session.scalar(select(Customer).where(Customer.email == normalized))
        match_reason = "email"
    if customer is None and customer_id:
        customer = session.get(Customer, customer_id)
        match_reason = "customer_id"
    if customer is None and phone_e164:
        digits = _digits(phone_e164)
        customer = session.scalar(select(Customer).where(Customer.phone_e164.like(f"%{digits[-10:]}")))
        match_reason = "phone_e164"
    if customer is None and phone_last4:
        digits = _digits(phone_last4)
        matches = session.scalars(select(Customer).where(Customer.phone_e164.like(f"%{digits[-4:]}"))).all()
        if len(matches) > 1:
            raise ValueError(f"Phone last4 '{digits[-4:]}' matched multiple customers. Use fashion_find_customers first.")
        customer = matches[0] if matches else None
        match_reason = "phone_last4"

    if customer is None:
        raise ValueError("Customer was not found. Provide a valid email, customer_id, phone_e164, or phone_last4.")

    home_store = session.get(Store, customer.home_store_id)
    return CustomerMatch(resolved=_resolved_customer(customer, home_store, match_reason))
