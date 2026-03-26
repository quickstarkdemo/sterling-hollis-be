from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import CustomerCommunication, Product, SupplierProductOffer, TwilioSmokeTest
from app.schemas import (
    CustomerCommunicationDraftResponse,
    CustomerCommunicationHistoryResponse,
    CustomerCommunicationRecord,
    CustomerCommunicationStatus,
    CustomerEmailDraftResponse,
    CustomerEmailSendResponse,
    CustomerCommunicationUpdateResponse,
    CustomerRecommendationRequest,
    CustomerRecommendationResponse,
    RetrievalMode,
    ResolvedCustomer,
    ResolvedStore,
    StyleConstraints,
    TwilioSmokeTestRecord,
    TwilioSmokeTestResponse,
    UiProductCard,
)
from app.services.demo_assets import demo_image_url
from app.services.email_service import SesEmailService
from app.services.executive import apply_execution_tags_for_store
from app.services.lookup import resolve_customer, resolve_store
from app.services.recommendations import customer_recommendations
from app.services.twilio_service import TwilioService


def _record_from_model(record: CustomerCommunication, customer: ResolvedCustomer) -> CustomerCommunicationRecord:
    return CustomerCommunicationRecord(
        id=record.id,
        customer_id=record.customer_id,
        customer_email=customer.email,
        customer_phone_e164=customer.phone_e164,
        store_id=record.store_id,
        channel=record.channel,
        status=CustomerCommunicationStatus(record.status),
        destination_e164=record.destination_e164,
        subject=record.subject,
        body_text=record.body_text,
        product_ids=list(record.product_ids or []),
        twilio_message_sid=record.twilio_message_sid,
        error_message=record.error_message,
        created_at=record.created_at,
        sent_at=record.sent_at,
    )


def _product_summary_lines(session: Session, product_ids: list[str]) -> list[str]:
    if not product_ids:
        return []
    products = session.scalars(select(Product).where(Product.id.in_(product_ids))).all()
    by_id = {product.id: product for product in products}
    lines: list[str] = []
    for product_id in product_ids[:3]:
        product = by_id.get(product_id)
        if not product:
            continue
        lines.append(f"- {product.title} (${float(product.price):.2f})")
        lines.append(f"  {product.link}")
    return lines


def _coming_soon_offer_map(session: Session, product_ids: list[str]) -> dict[str, SupplierProductOffer]:
    targets = {str(value).strip() for value in (product_ids or []) if str(value).strip()}
    if not targets:
        return {}
    today = datetime.now(timezone.utc).date()
    offers = session.scalars(
        select(SupplierProductOffer).where(SupplierProductOffer.status.in_(["potential", "committed", "launched"]))
    ).all()
    mapped: dict[str, SupplierProductOffer] = {}
    for offer in offers:
        metadata = offer.metadata_json if isinstance(offer.metadata_json, dict) else {}
        source_product_id = str(metadata.get("source_product_id") or "").strip()
        if not source_product_id or source_product_id not in targets:
            continue
        if offer.available_on and offer.available_on < today:
            continue
        existing = mapped.get(source_product_id)
        existing_date = existing.available_on if existing and existing.available_on is not None else date.max
        offer_date = offer.available_on if offer.available_on is not None else date.max
        if existing is None or offer_date < existing_date:
            mapped[source_product_id] = offer
    return mapped


def _email_product_sections(
    session: Session,
    product_ids: list[str],
    *,
    max_items: int = 3,
) -> tuple[list[str], list[str]]:
    if not product_ids:
        return [], []
    products = session.scalars(select(Product).where(Product.id.in_(product_ids))).all()
    by_id = {product.id: product for product in products}
    upcoming_offer_by_product_id = _coming_soon_offer_map(session, product_ids)
    available_now_lines: list[str] = []
    coming_soon_lines: list[str] = []
    for product_id in product_ids[:max_items]:
        product = by_id.get(product_id)
        if not product:
            continue
        offer = upcoming_offer_by_product_id.get(product.id)
        availability_token = str(product.availability or "").strip().lower()
        labels: list[str] = []
        if availability_token == "preorder":
            labels.append("preorder")
        if offer is not None:
            offer_status = str(offer.status or "").strip().lower()
            if offer_status:
                labels.append(offer_status)
            if offer.available_on:
                labels.append(f"available {offer.available_on.isoformat()}")
        line = f"- {product.title} (${float(product.price):.2f})"
        if labels:
            line = f"{line} [{'; '.join(labels)}]"
        target_lines = coming_soon_lines if (availability_token == "preorder" or offer is not None) else available_now_lines
        target_lines.append(line)
        target_lines.append(f"  {product.link}")
    return available_now_lines, coming_soon_lines


def _selected_product_cards(session: Session, product_ids: list[str]) -> list[UiProductCard]:
    if not product_ids:
        return []
    products = session.scalars(select(Product).where(Product.id.in_(product_ids))).all()
    by_id = {product.id: product for product in products}
    cards: list[UiProductCard] = []
    for product_id in product_ids:
        product = by_id.get(product_id)
        if not product:
            continue
        cards.append(
            UiProductCard(
                product_id=product.id,
                title=product.title,
                brand=product.brand,
                category=product.category,
                price=float(product.price),
                availability=product.availability,
                link=product.link,
                image_url=demo_image_url(product.category, product.id, variant_hint=product.brand),
            )
        )
    return cards


def build_sms_body(
    session: Session,
    store: ResolvedStore,
    customer: ResolvedCustomer,
    recommendation: CustomerRecommendationResponse,
    selected_product_ids: list[str] | None = None,
) -> str:
    product_ids = selected_product_ids or [product.product_id for product in recommendation.recommendations[:3]]
    lines = [
        f"Hi {customer.first_name}, here are a few picks from {store.name}.",
        *(_product_summary_lines(session, product_ids) or ["- I have a few curated options ready for review."]),
        "Reply if you'd like me to hold or pull together options.",
    ]
    return "\n".join(lines)


def _build_email_subject(store: ResolvedStore, occasion: str | None = None) -> str:
    if occasion:
        return f"{store.name} recommendations for {occasion}"
    return f"{store.name} recommendations"


def build_email_body(
    session: Session,
    store: ResolvedStore,
    customer: ResolvedCustomer,
    selected_product_ids: list[str],
) -> str:
    available_now_lines, coming_soon_lines = _email_product_sections(session, selected_product_ids)
    section_lines: list[str] = []
    if available_now_lines:
        section_lines.extend(["Available now:", *available_now_lines])
    if coming_soon_lines:
        if section_lines:
            section_lines.append("")
        section_lines.extend(
            [
                "Coming Soon / Preorder:",
                *coming_soon_lines,
                "",
                "If you'd like, I can place a preorder request or follow up when these arrive.",
            ]
        )
    if not section_lines:
        section_lines = ["- I have a few curated options ready to review."]
    lines = [
        f"Hi {customer.first_name},",
        "",
        f"I pulled together a few recommendations from {store.name}:",
        *section_lines,
        "",
        "Reply to this email if you'd like me to hold any of these or tailor another set of options.",
    ]
    return "\n".join(lines)


def _resolve_email_context(
    session: Session,
    *,
    store_query: str | None = None,
    store_id: str | None = None,
    customer_email: str | None = None,
    customer_id: str | None = None,
    customer_phone_e164: str | None = None,
    phone_last4: str | None = None,
) -> tuple[ResolvedStore, ResolvedCustomer]:
    resolved_customer = resolve_customer(
        session,
        email=customer_email,
        customer_id=customer_id,
        phone_e164=customer_phone_e164,
        phone_last4=phone_last4,
    ).resolved
    if store_id or store_query:
        resolved_store = resolve_store(session, store_query=store_query, store_id=store_id).resolved
    else:
        resolved_store = resolve_store(session, store_id=resolved_customer.home_store_id).resolved
    return resolved_store, resolved_customer


def _build_email_recommendation_snapshot(
    session: Session,
    *,
    store_id: str,
    customer_id: str,
    occasion: str | None,
    budget_min: float | None,
    budget_max: float | None,
    top_k: int,
    retrieval_mode: RetrievalMode,
    selected_product_ids: list[str] | None,
    style_constraints: StyleConstraints | None = None,
) -> tuple[list[str], str, CustomerRecommendationResponse | None]:
    strategy = "selected_products_only"
    recommendation: CustomerRecommendationResponse | None = None
    resolved_product_ids = list(selected_product_ids or [])
    settings = get_settings()
    if not resolved_product_ids:
        req = CustomerRecommendationRequest(
            store_id=store_id,
            customer_id=customer_id,
            occasion=occasion,
            budget_min=budget_min,
            budget_max=budget_max,
            top_k=top_k,
            style_constraints=style_constraints,
        )
        rows, strategy, applied_constraints, constraint_stage = customer_recommendations(
            session, req, retrieval_mode=retrieval_mode
        )
        strategy_packet_id = None
        strategy_tag_intensity = None
        if settings.associate_priority_tags_enabled:
            strategy_packet_id, strategy_tag_intensity, rows = apply_execution_tags_for_store(
                session,
                store_id=store_id,
                recommendations=rows,
            )
        recommendation = CustomerRecommendationResponse(
            store_id=store_id,
            strategy=strategy,
            recommendations=rows,
            strategy_packet_id=strategy_packet_id,
            strategy_tag_intensity=strategy_tag_intensity,
            applied_style_constraints=applied_constraints,
            constraint_source=applied_constraints.constraint_source if applied_constraints else None,
            constraint_stage=constraint_stage,
        )
        resolved_product_ids = [product.product_id for product in recommendation.recommendations[:3]]
    if not resolved_product_ids:
        raise ValueError("No recommendations available to send.")
    return resolved_product_ids, strategy, recommendation


def _email_draft_response_from_record(
    session: Session,
    *,
    record: CustomerCommunication,
    customer: ResolvedCustomer,
    store: ResolvedStore,
    recommendation: CustomerRecommendationResponse | None = None,
    retrieval_mode: RetrievalMode = RetrievalMode.auto,
) -> CustomerEmailDraftResponse:
    destination_email = (record.destination_e164 or "").strip().lower()
    selected_products = _selected_product_cards(session, list(record.product_ids or []))
    resolved_subject = (record.subject or "").strip()
    if not resolved_subject:
        resolved_subject = _build_email_subject(store)
    return CustomerEmailDraftResponse(
        message=_record_from_model(record, customer),
        store=store,
        customer=customer,
        destination_email=destination_email,
        subject=resolved_subject,
        selected_products=selected_products,
        recommendation=recommendation,
        retrieval_mode=retrieval_mode,
    )


def prepare_customer_sms(
    session: Session,
    store_query: str | None = None,
    store_id: str | None = None,
    customer_email: str | None = None,
    customer_id: str | None = None,
    customer_phone_e164: str | None = None,
    phone_last4: str | None = None,
    occasion: str | None = None,
    budget_min: float | None = None,
    budget_max: float | None = None,
    top_k: int = 5,
    retrieval_mode: RetrievalMode = RetrievalMode.auto,
    selected_product_ids: list[str] | None = None,
) -> CustomerCommunicationDraftResponse:
    resolved_customer = resolve_customer(
        session,
        email=customer_email,
        customer_id=customer_id,
        phone_e164=customer_phone_e164,
        phone_last4=phone_last4,
    ).resolved
    settings = get_settings()
    if store_id or store_query:
        resolved_store = resolve_store(session, store_query=store_query, store_id=store_id).resolved
    else:
        resolved_store = resolve_store(session, store_id=resolved_customer.home_store_id).resolved

    req = CustomerRecommendationRequest(
        store_id=resolved_store.id,
        customer_id=resolved_customer.id,
        occasion=occasion,
        budget_min=budget_min,
        budget_max=budget_max,
        top_k=top_k,
    )
    rows, strategy, applied_constraints, constraint_stage = customer_recommendations(
        session, req, retrieval_mode=retrieval_mode
    )
    strategy_packet_id = None
    strategy_tag_intensity = None
    if settings.associate_priority_tags_enabled:
        strategy_packet_id, strategy_tag_intensity, rows = apply_execution_tags_for_store(
            session,
            store_id=resolved_store.id,
            recommendations=rows,
        )
    recommendation = CustomerRecommendationResponse(
        store_id=resolved_store.id,
        strategy=strategy,
        recommendations=rows,
        strategy_packet_id=strategy_packet_id,
        strategy_tag_intensity=strategy_tag_intensity,
        applied_style_constraints=applied_constraints,
        constraint_source=applied_constraints.constraint_source if applied_constraints else None,
        constraint_stage=constraint_stage,
    )

    selected_product_ids = selected_product_ids or [product.product_id for product in recommendation.recommendations[:3]]
    destination = settings.twilio_test_to_number or ""
    body = build_sms_body(session, resolved_store, resolved_customer, recommendation, selected_product_ids=selected_product_ids)
    record = CustomerCommunication(
        id=f"msg_{uuid.uuid4().hex[:12]}",
        customer_id=resolved_customer.id,
        store_id=resolved_store.id,
        channel="sms",
        status=CustomerCommunicationStatus.draft.value,
        destination_e164=destination,
        body_text=body,
        product_ids=selected_product_ids,
        recommendation_context={
            "occasion": occasion,
            "budget_min": budget_min,
            "budget_max": budget_max,
            "top_k": top_k,
            "strategy": strategy,
            "style_constraints": (
                recommendation.applied_style_constraints.model_dump(mode="json")
                if recommendation.applied_style_constraints
                else None
            ),
            "constraint_source": recommendation.constraint_source,
            "constraint_stage": recommendation.constraint_stage,
        },
    )
    session.add(record)
    session.commit()

    return CustomerCommunicationDraftResponse(
        message=_record_from_model(record, resolved_customer),
        store=resolved_store,
        customer=resolved_customer,
        recommendation=recommendation,
        retrieval_mode=retrieval_mode,
    )


def update_customer_sms_draft(
    session: Session,
    message_id: str,
    body_text: str,
    selected_product_ids: list[str] | None = None,
) -> CustomerCommunicationUpdateResponse:
    record = session.get(CustomerCommunication, message_id)
    if not record:
        raise ValueError(f"Message {message_id} was not found.")
    if record.status != CustomerCommunicationStatus.draft.value:
        raise ValueError("Only draft messages can be edited.")

    customer = resolve_customer(session, customer_id=record.customer_id).resolved
    store = resolve_store(session, store_id=record.store_id).resolved

    record.body_text = body_text.strip()
    if selected_product_ids is not None:
        record.product_ids = selected_product_ids
    session.add(record)
    session.commit()
    return CustomerCommunicationUpdateResponse(message=_record_from_model(record, customer), store=store, customer=customer)


def send_customer_sms(session: Session, message_id: str) -> CustomerCommunicationRecord:
    record = session.get(CustomerCommunication, message_id)
    if not record:
        raise ValueError(f"Message {message_id} was not found.")

    customer = resolve_customer(session, customer_id=record.customer_id).resolved
    twilio = TwilioService()
    try:
        payload = twilio.send_sms(record.body_text, to_number=record.destination_e164)
        record.status = CustomerCommunicationStatus.sent.value
        record.twilio_message_sid = payload.get("sid")
        record.error_message = None
        record.sent_at = datetime.now(timezone.utc)
    except Exception as exc:
        record.status = CustomerCommunicationStatus.failed.value
        record.error_message = str(exc)[:2000]
        session.add(record)
        session.commit()
        return _record_from_model(record, customer)

    session.add(record)
    session.commit()
    return _record_from_model(record, customer)


def prepare_customer_email_draft(
    session: Session,
    message_id: str | None = None,
    store_query: str | None = None,
    store_id: str | None = None,
    customer_email: str | None = None,
    customer_id: str | None = None,
    customer_phone_e164: str | None = None,
    phone_last4: str | None = None,
    occasion: str | None = None,
    budget_min: float | None = None,
    budget_max: float | None = None,
    top_k: int = 6,
    retrieval_mode: RetrievalMode = RetrievalMode.auto,
    selected_product_ids: list[str] | None = None,
    to_email: str | None = None,
    subject: str | None = None,
    style_constraints: StyleConstraints | None = None,
) -> CustomerEmailDraftResponse:
    resolved_store, resolved_customer = _resolve_email_context(
        session,
        store_query=store_query,
        store_id=store_id,
        customer_email=customer_email,
        customer_id=customer_id,
        customer_phone_e164=customer_phone_e164,
        phone_last4=phone_last4,
    )

    resolved_product_ids, strategy, recommendation = _build_email_recommendation_snapshot(
        session,
        store_id=resolved_store.id,
        customer_id=resolved_customer.id,
        occasion=occasion,
        budget_min=budget_min,
        budget_max=budget_max,
        top_k=top_k,
        retrieval_mode=retrieval_mode,
        selected_product_ids=selected_product_ids,
        style_constraints=style_constraints,
    )

    destination_email = (to_email or resolved_customer.email or "").strip().lower()
    if not destination_email:
        raise ValueError("Destination email is required.")

    resolved_subject = (subject or _build_email_subject(resolved_store, occasion=occasion)).strip()
    body_text = build_email_body(session, resolved_store, resolved_customer, selected_product_ids=resolved_product_ids)

    record: CustomerCommunication | None = None
    if message_id:
        record = session.get(CustomerCommunication, message_id)
        if not record:
            raise ValueError(f"Message {message_id} was not found.")
        if record.channel != "email":
            raise ValueError("Only email drafts are supported by this endpoint.")
        if record.status == CustomerCommunicationStatus.sent.value:
            raise ValueError("Sent messages cannot be regenerated. Create a new draft.")

    if record is None:
        record = CustomerCommunication(
            id=message_id or f"msg_{uuid.uuid4().hex[:12]}",
            customer_id=resolved_customer.id,
            store_id=resolved_store.id,
            channel="email",
            status=CustomerCommunicationStatus.draft.value,
            destination_e164=destination_email,
            subject=resolved_subject,
            body_text=body_text,
            product_ids=resolved_product_ids,
            recommendation_context={},
        )
    else:
        record.customer_id = resolved_customer.id
        record.store_id = resolved_store.id
        record.destination_e164 = destination_email
        record.subject = resolved_subject
        record.body_text = body_text
        record.product_ids = resolved_product_ids
        record.error_message = None

    record.recommendation_context = {
        "occasion": occasion,
        "budget_min": budget_min,
        "budget_max": budget_max,
        "top_k": top_k,
        "strategy": strategy,
        "style_constraints": (
            recommendation.applied_style_constraints.model_dump(mode="json")
            if recommendation and recommendation.applied_style_constraints
            else None
        ),
        "constraint_source": recommendation.constraint_source if recommendation else None,
        "constraint_stage": recommendation.constraint_stage if recommendation else None,
        "subject": resolved_subject,
        "retrieval_mode": retrieval_mode.value if isinstance(retrieval_mode, RetrievalMode) else str(retrieval_mode),
    }

    session.add(record)
    session.commit()
    return _email_draft_response_from_record(
        session,
        record=record,
        customer=resolved_customer,
        store=resolved_store,
        recommendation=recommendation,
        retrieval_mode=retrieval_mode,
    )


def update_customer_email_draft(
    session: Session,
    message_id: str,
    subject: str | None = None,
    body_text: str | None = None,
    to_email: str | None = None,
    selected_product_ids: list[str] | None = None,
) -> CustomerEmailDraftResponse:
    record = session.get(CustomerCommunication, message_id)
    if not record:
        raise ValueError(f"Message {message_id} was not found.")
    if record.channel != "email":
        raise ValueError("Only email drafts are supported by this endpoint.")
    if record.status == CustomerCommunicationStatus.sent.value:
        raise ValueError("Sent messages cannot be edited.")

    customer = resolve_customer(session, customer_id=record.customer_id).resolved
    store = resolve_store(session, store_id=record.store_id).resolved
    if subject is not None:
        record.subject = subject.strip() or None
    if body_text is not None:
        record.body_text = body_text.strip()
    if to_email is not None:
        resolved_destination = to_email.strip().lower()
        if not resolved_destination:
            raise ValueError("Destination email is required.")
        record.destination_e164 = resolved_destination
    if selected_product_ids is not None:
        record.product_ids = selected_product_ids
    session.add(record)
    session.commit()
    return _email_draft_response_from_record(session, record=record, customer=customer, store=store)


def get_customer_email_draft(session: Session, message_id: str) -> CustomerEmailDraftResponse:
    record = session.get(CustomerCommunication, message_id)
    if not record:
        raise ValueError(f"Message {message_id} was not found.")
    if record.channel != "email":
        raise ValueError("Only email drafts are supported by this endpoint.")
    customer = resolve_customer(session, customer_id=record.customer_id).resolved
    store = resolve_store(session, store_id=record.store_id).resolved
    return _email_draft_response_from_record(session, record=record, customer=customer, store=store)


def send_customer_email_draft(session: Session, message_id: str) -> CustomerEmailSendResponse:
    record = session.get(CustomerCommunication, message_id)
    if not record:
        raise ValueError(f"Message {message_id} was not found.")
    if record.channel != "email":
        raise ValueError("Only email drafts are supported by this endpoint.")
    if record.status == CustomerCommunicationStatus.sent.value:
        raise ValueError("Draft was already sent.")

    customer = resolve_customer(session, customer_id=record.customer_id).resolved
    store = resolve_store(session, store_id=record.store_id).resolved
    destination_email = (record.destination_e164 or "").strip().lower()
    if not destination_email:
        raise ValueError("Destination email is required.")
    resolved_subject = (record.subject or "").strip() or _build_email_subject(store)
    record.subject = resolved_subject
    if not record.body_text or not record.body_text.strip():
        record.body_text = build_email_body(session, store, customer, selected_product_ids=list(record.product_ids or []))

    email_service = SesEmailService()
    provider_message_id = None
    try:
        payload = email_service.send_email(
            to_email=destination_email,
            subject=resolved_subject,
            text_body=record.body_text,
        )
        provider_message_id = payload.get("message_id")
        record.status = CustomerCommunicationStatus.sent.value
        record.twilio_message_sid = provider_message_id
        record.error_message = None
        record.sent_at = datetime.now(timezone.utc)
    except Exception as exc:
        record.status = CustomerCommunicationStatus.failed.value
        record.error_message = str(exc)[:2000]

    session.add(record)
    session.commit()

    return CustomerEmailSendResponse(
        message=_record_from_model(record, customer),
        store=store,
        customer=customer,
        destination_email=destination_email,
        subject=resolved_subject,
        selected_products=_selected_product_cards(session, list(record.product_ids or [])),
        provider_message_id=provider_message_id,
    )


def send_customer_recommendations_email(
    session: Session,
    store_query: str | None = None,
    store_id: str | None = None,
    customer_email: str | None = None,
    customer_id: str | None = None,
    customer_phone_e164: str | None = None,
    phone_last4: str | None = None,
    occasion: str | None = None,
    budget_min: float | None = None,
    budget_max: float | None = None,
    top_k: int = 6,
    retrieval_mode: RetrievalMode = RetrievalMode.auto,
    selected_product_ids: list[str] | None = None,
    to_email: str | None = None,
    subject: str | None = None,
) -> CustomerEmailSendResponse:
    draft = prepare_customer_email_draft(
        session,
        store_query=store_query,
        store_id=store_id,
        customer_email=customer_email,
        customer_id=customer_id,
        customer_phone_e164=customer_phone_e164,
        phone_last4=phone_last4,
        occasion=occasion,
        budget_min=budget_min,
        budget_max=budget_max,
        top_k=top_k,
        retrieval_mode=retrieval_mode,
        selected_product_ids=selected_product_ids,
        to_email=to_email,
        subject=subject,
    )
    return send_customer_email_draft(session, draft.message.id)


def customer_message_history(
    session: Session,
    customer_email: str | None = None,
    customer_id: str | None = None,
    phone_e164: str | None = None,
    phone_last4: str | None = None,
    limit: int = 20,
    status: CustomerCommunicationStatus | None = None,
) -> CustomerCommunicationHistoryResponse:
    resolved_customer = resolve_customer(
        session,
        email=customer_email,
        customer_id=customer_id,
        phone_e164=phone_e164,
        phone_last4=phone_last4,
    ).resolved
    query = select(CustomerCommunication).where(CustomerCommunication.customer_id == resolved_customer.id)
    if status is not None:
        query = query.where(CustomerCommunication.status == status.value)
    query = query.order_by(desc(CustomerCommunication.created_at)).limit(limit)
    records = session.scalars(query).all()
    return CustomerCommunicationHistoryResponse(
        customer=resolved_customer,
        messages=[_record_from_model(record, resolved_customer) for record in records],
    )


def get_customer_message(session: Session, message_id: str) -> tuple[CustomerCommunicationRecord, ResolvedCustomer, ResolvedStore]:
    record = session.get(CustomerCommunication, message_id)
    if not record:
        raise ValueError(f"Message {message_id} was not found.")

    customer = resolve_customer(session, customer_id=record.customer_id).resolved
    store = resolve_store(session, store_id=record.store_id).resolved
    return _record_from_model(record, customer), customer, store


def get_selected_products_for_message(session: Session, message_id: str) -> list[UiProductCard]:
    record = session.get(CustomerCommunication, message_id)
    if not record:
        raise ValueError(f"Message {message_id} was not found.")
    return _selected_product_cards(session, list(record.product_ids or []))


def twilio_smoke_test(session: Session, body_text: str | None = None) -> TwilioSmokeTestResponse:
    settings = get_settings()
    body = (body_text or "Product DB Twilio smoke test from operator workflow.").strip()
    record = TwilioSmokeTest(
        id=f"twilio_{uuid.uuid4().hex[:12]}",
        destination_e164=settings.twilio_test_to_number or "",
        body_text=body,
        status=CustomerCommunicationStatus.draft.value,
    )
    session.add(record)
    session.commit()

    twilio = TwilioService()
    try:
        payload = twilio.send_sms(body, to_number=record.destination_e164)
        record.status = CustomerCommunicationStatus.sent.value
        record.twilio_message_sid = payload.get("sid")
        record.sent_at = datetime.now(timezone.utc)
        record.error_message = None
    except Exception as exc:
        record.status = CustomerCommunicationStatus.failed.value
        record.error_message = str(exc)[:2000]

    session.add(record)
    session.commit()
    return TwilioSmokeTestResponse(
        result=TwilioSmokeTestRecord(
            id=record.id,
            destination_e164=record.destination_e164,
            body_text=record.body_text,
            status=CustomerCommunicationStatus(record.status),
            twilio_message_sid=record.twilio_message_sid,
            error_message=record.error_message,
            created_at=record.created_at,
            sent_at=record.sent_at,
        )
    )
