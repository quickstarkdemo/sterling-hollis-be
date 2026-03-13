from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Customer, Store
from app.schemas import ResolvedCustomer, ResolvedStore


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
}


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.lower().replace("-", " ").replace(",", " ").split())


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


def resolve_customer(session: Session, email: str | None = None, customer_id: str | None = None) -> CustomerMatch:
    customer: Customer | None = None
    match_reason = ""

    if email:
        normalized = email.strip().lower()
        customer = session.scalar(select(Customer).where(Customer.email == normalized))
        match_reason = "email"
    if customer is None and customer_id:
        customer = session.get(Customer, customer_id)
        match_reason = "customer_id"

    if customer is None:
        raise ValueError("Customer was not found. Provide a valid email or customer_id.")

    home_store = session.get(Store, customer.home_store_id)
    resolved = ResolvedCustomer(
        id=customer.id,
        email=customer.email,
        first_name=customer.first_name,
        last_name=customer.last_name,
        home_store_id=customer.home_store_id,
        home_store_name=home_store.name if home_store else customer.home_store_id,
        loyalty_tier=customer.loyalty_tier,
        match_reason=match_reason,
    )
    return CustomerMatch(resolved=resolved)
