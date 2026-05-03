from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlparse
from uuid import uuid4

import jwt
from fastapi import Depends, HTTPException, Request, status
from jwt import PyJWKClient
from jwt.exceptions import InvalidTokenError, PyJWKClientError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import Customer, CustomerAuthIdentity, Store
from app.schemas import ResolvedCustomer
from app.services.lookup import _resolved_customer


class ClerkAuthError(ValueError):
    """Raised when a Clerk session token is missing required trust signals."""


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    provider: str
    provider_user_id: str
    email: str | None = None
    claims: dict | None = None


@dataclass(frozen=True)
class ChatIdentity:
    status: Literal["anonymous", "authenticated_unlinked", "authenticated_customer"]
    principal: AuthenticatedPrincipal | None = None
    customer: ResolvedCustomer | None = None

    @property
    def customer_id(self) -> str | None:
        return self.customer.id if self.customer else None


def anonymous_identity() -> ChatIdentity:
    return ChatIdentity(status="anonymous")


def _csv_values(raw: str | None) -> set[str]:
    return {item.strip().rstrip("/") for item in (raw or "").split(",") if item.strip()}


def _origin_parts(value: str | None) -> tuple[str, str | None, int | None] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlparse(value.strip())
    if not parsed.scheme or not parsed.hostname:
        return None
    return parsed.scheme.lower(), parsed.hostname.lower(), parsed.port


def _authorized_party_allowed(azp: str | None, authorized_parties: set[str]) -> bool:
    if not authorized_parties:
        return True
    azp_parts = _origin_parts(azp)
    if not azp_parts:
        return False
    azp_scheme, azp_host, azp_port = azp_parts

    for allowed in authorized_parties:
        allowed_parts = _origin_parts(allowed)
        if not allowed_parts:
            continue
        allowed_scheme, allowed_host, allowed_port = allowed_parts
        if allowed_scheme != azp_scheme:
            continue
        if allowed_host.startswith("*.") and azp_host.endswith(allowed_host[1:]):
            return True
        if allowed_host != azp_host:
            continue
        if allowed_port is None or allowed_port == azp_port:
            return True
    return False


def _email_from_claims(claims: dict) -> str | None:
    for key in ("email", "primary_email_address", "email_address"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value.lower()
    return None


def verify_clerk_token(token: str, settings: Settings | None = None) -> AuthenticatedPrincipal:
    settings = settings or get_settings()
    if not settings.clerk_issuer or not settings.clerk_jwks_url:
        raise ClerkAuthError("Clerk JWT verification is not configured.")

    try:
        signing_key = PyJWKClient(settings.clerk_jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=settings.clerk_issuer,
            options={"require": ["exp", "nbf", "iss", "sub"]},
        )
    except (InvalidTokenError, PyJWKClientError, ValueError) as exc:
        raise ClerkAuthError("Invalid Clerk session token.") from exc

    authorized_parties = _csv_values(settings.clerk_authorized_parties)
    azp = claims.get("azp")
    if not _authorized_party_allowed(azp, authorized_parties):
        raise ClerkAuthError("Clerk token authorized party is not allowed.")

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise ClerkAuthError("Clerk token is missing a subject.")

    return AuthenticatedPrincipal(
        provider="clerk",
        provider_user_id=subject,
        email=_email_from_claims(claims),
        claims=claims,
    )


def _resolved_customer_for_id(db: Session, customer_id: str, match_reason: str) -> ResolvedCustomer | None:
    customer = db.get(Customer, customer_id)
    if not customer:
        return None
    store = db.get(Store, customer.home_store_id)
    return _resolved_customer(customer, store, match_reason)


def _create_identity_link(db: Session, principal: AuthenticatedPrincipal, customer: Customer) -> CustomerAuthIdentity:
    now = datetime.now(timezone.utc)
    identity = CustomerAuthIdentity(
        id=f"auth_{uuid4().hex[:24]}",
        provider=principal.provider,
        provider_user_id=principal.provider_user_id,
        customer_id=customer.id,
        email=principal.email,
        created_at=now,
        last_seen_at=now,
    )
    db.add(identity)
    db.flush()
    return identity


def resolve_chat_identity(
    db: Session,
    principal: AuthenticatedPrincipal | None,
    settings: Settings | None = None,
) -> ChatIdentity:
    if principal is None:
        return anonymous_identity()

    identity = db.scalar(
        select(CustomerAuthIdentity).where(
            CustomerAuthIdentity.provider == principal.provider,
            CustomerAuthIdentity.provider_user_id == principal.provider_user_id,
        )
    )
    if identity:
        identity.last_seen_at = datetime.now(timezone.utc)
        if principal.email and identity.email != principal.email:
            identity.email = principal.email
        customer = _resolved_customer_for_id(db, identity.customer_id, "linked Clerk identity")
        if customer:
            return ChatIdentity(status="authenticated_customer", principal=principal, customer=customer)
        return ChatIdentity(status="authenticated_unlinked", principal=principal)

    settings = settings or get_settings()
    customer: Customer | None = None
    if principal.email:
        matches = db.scalars(select(Customer).where(func.lower(Customer.email) == principal.email.lower())).all()
        if len(matches) == 1:
            customer = matches[0]

    demo_email = settings.clerk_demo_customer_email.lower() if settings.clerk_demo_customer_email else None
    if customer is None and settings.clerk_demo_customer_id:
        if not demo_email or (principal.email and principal.email.lower() == demo_email):
            customer = db.get(Customer, settings.clerk_demo_customer_id)

    if customer is None:
        return ChatIdentity(status="authenticated_unlinked", principal=principal)

    _create_identity_link(db, principal, customer)
    resolved = _resolved_customer_for_id(db, customer.id, "matched Clerk email")
    if not resolved:
        return ChatIdentity(status="authenticated_unlinked", principal=principal)
    return ChatIdentity(status="authenticated_customer", principal=principal, customer=resolved)


def bearer_token_from_request(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must be a Bearer token.",
        )
    return token.strip()


def optional_chat_identity(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ChatIdentity:
    token = bearer_token_from_request(request)
    if not token:
        return anonymous_identity()
    try:
        principal = verify_clerk_token(token, settings)
        identity = resolve_chat_identity(db, principal, settings)
        db.commit()
        return identity
    except ClerkAuthError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
