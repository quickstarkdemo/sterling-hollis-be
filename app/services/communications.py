from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import CustomerCommunication
from app.schemas import (
    CustomerCommunicationDraftResponse,
    CustomerCommunicationHistoryResponse,
    CustomerCommunicationRecord,
    CustomerCommunicationStatus,
    CustomerRecommendationResponse,
    ResolvedCustomer,
    ResolvedStore,
)
from app.services.lookup import resolve_customer, resolve_store
from app.services.recommendations import customer_recommendations
from app.services.twilio_service import TwilioService
from app.schemas import CustomerRecommendationRequest


def _record_from_model(record: CustomerCommunication, customer_email: str) -> CustomerCommunicationRecord:
    return CustomerCommunicationRecord(
        id=record.id,
        customer_id=record.customer_id,
        customer_email=customer_email,
        store_id=record.store_id,
        channel=record.channel,
        status=CustomerCommunicationStatus(record.status),
        destination_e164=record.destination_e164,
        body_text=record.body_text,
        product_ids=list(record.product_ids or []),
        twilio_message_sid=record.twilio_message_sid,
        error_message=record.error_message,
        created_at=record.created_at,
        sent_at=record.sent_at,
    )


def build_sms_body(store: ResolvedStore, customer: ResolvedCustomer, recommendation: CustomerRecommendationResponse) -> str:
    lines = [
        f"Hi {customer.first_name}, here are a few picks from {store.name}.",
    ]
    for product in recommendation.recommendations[:3]:
        lines.append(f"- {product.title} (${product.price:.2f})")
    lines.append("Reply if you'd like me to hold or pull together options.")
    return "\n".join(lines)


def prepare_customer_sms(
    session: Session,
    store_query: str | None = None,
    store_id: str | None = None,
    customer_email: str | None = None,
    customer_id: str | None = None,
    occasion: str | None = None,
    budget_min: float | None = None,
    budget_max: float | None = None,
    top_k: int = 5,
) -> CustomerCommunicationDraftResponse:
    settings = get_settings()
    resolved_store = resolve_store(session, store_query=store_query, store_id=store_id).resolved
    resolved_customer = resolve_customer(session, email=customer_email, customer_id=customer_id).resolved

    req = CustomerRecommendationRequest(
        store_id=resolved_store.id,
        customer_id=resolved_customer.id,
        occasion=occasion,
        budget_min=budget_min,
        budget_max=budget_max,
        top_k=top_k,
    )
    rows, strategy = customer_recommendations(session, req)
    recommendation = CustomerRecommendationResponse(
        store_id=resolved_store.id,
        strategy=strategy,
        recommendations=rows,
    )

    destination = settings.twilio_test_to_number or ""
    body = build_sms_body(resolved_store, resolved_customer, recommendation)
    record = CustomerCommunication(
        id=f"msg_{uuid.uuid4().hex[:12]}",
        customer_id=resolved_customer.id,
        store_id=resolved_store.id,
        channel="sms",
        status=CustomerCommunicationStatus.draft.value,
        destination_e164=destination,
        body_text=body,
        product_ids=[product.product_id for product in recommendation.recommendations],
        recommendation_context={
            "occasion": occasion,
            "budget_min": budget_min,
            "budget_max": budget_max,
            "top_k": top_k,
            "strategy": strategy,
        },
    )
    session.add(record)
    session.commit()

    return CustomerCommunicationDraftResponse(
        message=_record_from_model(record, resolved_customer.email),
        store=resolved_store,
        customer=resolved_customer,
        recommendation=recommendation,
    )


def send_customer_sms(session: Session, message_id: str) -> CustomerCommunicationRecord:
    record = session.get(CustomerCommunication, message_id)
    if not record:
        raise ValueError(f"Message {message_id} was not found.")

    customer = resolve_customer(session, customer_id=record.customer_id).resolved
    twilio = TwilioService()
    try:
        payload = twilio.send_sms(record.body_text)
        record.status = CustomerCommunicationStatus.sent.value
        record.twilio_message_sid = payload.get("sid")
        record.error_message = None
        record.sent_at = datetime.now(timezone.utc)
    except Exception as exc:
        record.status = CustomerCommunicationStatus.failed.value
        record.error_message = str(exc)[:2000]
        session.add(record)
        session.commit()
        return _record_from_model(record, customer.email)

    session.add(record)
    session.commit()
    return _record_from_model(record, customer.email)


def customer_message_history(
    session: Session,
    customer_email: str | None = None,
    customer_id: str | None = None,
    limit: int = 20,
) -> CustomerCommunicationHistoryResponse:
    resolved_customer = resolve_customer(session, email=customer_email, customer_id=customer_id).resolved
    query = (
        select(CustomerCommunication)
        .where(CustomerCommunication.customer_id == resolved_customer.id)
        .order_by(desc(CustomerCommunication.created_at))
        .limit(limit)
    )
    records = session.scalars(query).all()
    return CustomerCommunicationHistoryResponse(
        customer=resolved_customer,
        messages=[_record_from_model(record, resolved_customer.email) for record in records],
    )


def get_customer_message(session: Session, message_id: str) -> tuple[CustomerCommunicationRecord, ResolvedCustomer, ResolvedStore]:
    record = session.get(CustomerCommunication, message_id)
    if not record:
        raise ValueError(f"Message {message_id} was not found.")

    customer = resolve_customer(session, customer_id=record.customer_id).resolved
    store = resolve_store(session, store_id=record.store_id).resolved
    return _record_from_model(record, customer.email), customer, store
