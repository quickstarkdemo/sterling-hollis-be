from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.catalog.review_schemas import (
    AdminProductReviewListResponse,
    AdminProductReviewResponse,
    PublicProductReview,
    ReviewActionResponse,
    ReviewAIProposal,
    ReviewAssistRequest,
    ReviewDecisionRequest,
    ReviewModerationResponse,
)
from app.config import Settings
from app.models import (
    CatalogAdminMutation,
    CatalogProduct,
    CatalogProductReview,
    CatalogReviewAction,
    CatalogReviewModeration,
)
from app.services.auth.clerk import AuthenticatedPrincipal
from app.services.catalog_admin import _conflict, _idempotent, _request_hash
from app.services.catalog_ai import (
    CatalogAICommandError,
    _combined_moderation,
    _moderation_result,
    _resolve_client,
    _safety_identifier,
    _usage,
)


REVIEW_ASSIST_INSTRUCTIONS = """You assist a retail merchant with customer-review moderation.
Classify only the supplied review text and rating. Preserve the customer's words and rating exactly.
Return bounded categories, a concise theme summary, a suggested merchant action, and an optional
professional response draft. Never claim to publish, approve, reject, or alter the review."""


class ProductReviewAIService:
    def __init__(self, settings: Settings, client: Any | None = None):
        self.settings = settings
        self.client = client

    def generate(
        self,
        review: CatalogProductReview,
        *,
        principal: AuthenticatedPrincipal,
    ) -> tuple[ReviewAIProposal, dict]:
        provider = _resolve_client(self.settings, self.client)
        try:
            response = provider.responses.parse(
                model=self.settings.catalog_studio_responses_model,
                instructions=REVIEW_ASSIST_INSTRUCTIONS,
                input=[{
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": json.dumps(
                            {"rating": review.rating, "review": review.body},
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    }],
                }],
                text_format=ReviewAIProposal,
                moderation={"model": self.settings.catalog_studio_moderation_model},
                max_output_tokens=self.settings.catalog_studio_responses_max_output_tokens,
                safety_identifier=_safety_identifier(principal),
                store=False,
                timeout=self.settings.catalog_studio_responses_timeout_seconds,
            )
        except CatalogAICommandError:
            raise
        except Exception as exc:
            raise CatalogAICommandError(
                code="review_assist_failed",
                detail="Review assistance is temporarily unavailable.",
                status_code=status.HTTP_502_BAD_GATEWAY,
                retryable=True,
            ) from exc
        if getattr(response, "status", None) != "completed" or not isinstance(
            getattr(response, "output_parsed", None), ReviewAIProposal
        ):
            raise CatalogAICommandError(
                code="invalid_structured_output",
                detail="Review assistance did not return a valid proposal.",
                status_code=status.HTTP_502_BAD_GATEWAY,
                retryable=True,
            )
        moderation = _combined_moderation(
            _moderation_result(getattr(getattr(response, "moderation", None), "input", None), label="input"),
            _moderation_result(getattr(getattr(response, "moderation", None), "output", None), label="output"),
        )
        if moderation["flagged"]:
            raise CatalogAICommandError(
                code="review_assist_blocked",
                detail="Review assistance was blocked by moderation. The review state is unchanged.",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                retryable=False,
            )
        metadata = {
            "request_id": getattr(response, "id", None),
            "model": getattr(response, "model", None) or self.settings.catalog_studio_responses_model,
            "usage": _usage(response),
            "moderation": {
                "decision": moderation["decision"],
                "categories": moderation["categories"],
            },
        }
        return response.output_parsed, metadata


def import_product_review(
    db: Session,
    *,
    product_id: str,
    source: str,
    external_review_id: str,
    author_display_name: str,
    body: str,
    rating: int,
    submitted_at: datetime,
) -> AdminProductReviewResponse:
    if db.get(CatalogProduct, product_id) is None:
        raise HTTPException(status_code=404, detail="Catalog product not found.")
    if rating < 1 or rating > 5:
        raise HTTPException(status_code=422, detail="Review rating must be between 1 and 5.")
    source = source.strip()
    external_review_id = external_review_id.strip()
    author_display_name = author_display_name.strip()
    body = body.strip()
    if not all((source, external_review_id, author_display_name, body)):
        raise HTTPException(
            status_code=422,
            detail="Review source, identifier, author, and body must not be blank.",
        )
    existing = db.scalar(
        select(CatalogProductReview).where(
            CatalogProductReview.source == source,
            CatalogProductReview.external_review_id == external_review_id,
        )
    )
    if existing is not None:
        existing = _review_query(db, product_id=existing.catalog_product_id, review_id=existing.id)
        if (
            existing.catalog_product_id != product_id
            or existing.author_display_name != author_display_name
            or existing.body != body
            or existing.rating != rating
        ):
            raise _conflict("The trusted review identifier already exists with different immutable content.")
        return _review_response(existing)
    review = CatalogProductReview(
        id=f"review_{uuid4().hex}",
        catalog_product_id=product_id,
        source=source,
        external_review_id=external_review_id,
        author_display_name=author_display_name,
        body=body,
        rating=rating,
        submitted_at=submitted_at,
    )
    review.moderation = CatalogReviewModeration(state="pending", version=1)
    db.add(review)
    db.commit()
    return _review_response(_review_query(db, product_id=product_id, review_id=review.id))


def _review_query(
    db: Session,
    *,
    product_id: str,
    review_id: str,
    for_update: bool = False,
) -> CatalogProductReview:
    statement = (
        select(CatalogProductReview)
        .options(
            selectinload(CatalogProductReview.moderation),
            selectinload(CatalogProductReview.actions),
        )
        .where(
            CatalogProductReview.id == review_id,
            CatalogProductReview.catalog_product_id == product_id,
        )
    )
    if for_update:
        statement = statement.execution_options(populate_existing=True).with_for_update()
    review = db.scalar(statement)
    if review is None:
        raise HTTPException(status_code=404, detail="Product review not found.")
    return review


def _review_response(review: CatalogProductReview) -> AdminProductReviewResponse:
    moderation = review.moderation
    return AdminProductReviewResponse(
        id=review.id,
        product_id=review.catalog_product_id,
        source=review.source,
        external_review_id=review.external_review_id,
        author_display_name=review.author_display_name,
        body=review.body,
        rating=review.rating,
        submitted_at=review.submitted_at,
        moderation=ReviewModerationResponse(
            version=moderation.version,
            state=moderation.state,  # type: ignore[arg-type]
            ai_categories=list(moderation.ai_categories_json or []),
            ai_theme_summary=moderation.ai_theme_summary,
            ai_suggested_action=moderation.ai_suggested_action,  # type: ignore[arg-type]
            ai_provider_metadata=dict(moderation.ai_provider_metadata_json or {}),
            response_draft=moderation.response_draft,
            response_published=moderation.response_published,
            response_published_at=moderation.response_published_at,
            decided_by=moderation.decided_by,
            decided_at=moderation.decided_at,
            decision_reason=moderation.decision_reason,
        ),
        actions=[
            ReviewActionResponse(
                id=item.id,
                action=item.action,
                expected_version=item.expected_version,
                resulting_version=item.resulting_version,
                actor_provider_user_id=item.actor_provider_user_id,
                reason=item.reason,
                created_at=item.created_at,
            )
            for item in review.actions
        ],
    )


def list_admin_product_reviews(
    db: Session, *, product_id: str
) -> AdminProductReviewListResponse:
    if db.get(CatalogProduct, product_id) is None:
        raise HTTPException(status_code=404, detail="Catalog product not found.")
    rows = db.scalars(
        select(CatalogProductReview)
        .options(
            selectinload(CatalogProductReview.moderation),
            selectinload(CatalogProductReview.actions),
        )
        .where(CatalogProductReview.catalog_product_id == product_id)
        .order_by(CatalogProductReview.submitted_at.desc(), CatalogProductReview.id)
    ).all()
    return AdminProductReviewListResponse(items=[_review_response(row) for row in rows])


def _record_action(
    review: CatalogProductReview,
    *,
    idempotency_key: str,
    action: str,
    expected_version: int,
    principal: AuthenticatedPrincipal,
    reason: str | None,
) -> None:
    review.actions.append(
        CatalogReviewAction(
            id=f"review_action_{uuid4().hex}",
            idempotency_key=idempotency_key,
            action=action,
            expected_version=expected_version,
            resulting_version=review.moderation.version,
            actor_provider=principal.provider,
            actor_provider_user_id=principal.provider_user_id,
            reason=reason,
        )
    )


def assist_product_review(
    db: Session,
    *,
    product_id: str,
    review_id: str,
    request: ReviewAssistRequest,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
    service: ProductReviewAIService,
) -> AdminProductReviewResponse:
    payload = request.model_dump(mode="json")
    operation = f"catalog.review.assist:{review_id}"
    existing = db.get(CatalogAdminMutation, idempotency_key)
    if existing is not None:
        if (
            existing.created_by != principal.provider_user_id
            or existing.operation != operation
            or existing.request_hash != _request_hash(payload)
        ):
            raise _conflict("Idempotency-Key was already used for a different catalog mutation.")
        return AdminProductReviewResponse.model_validate(existing.response_json)

    current = _review_query(db, product_id=product_id, review_id=review_id)
    if current.moderation.version != request.expected_version:
        raise _conflict("The review moderation state changed; reload before requesting assistance.")
    proposal, provider_metadata = service.generate(current, principal=principal)

    def action() -> dict:
        review = _review_query(db, product_id=product_id, review_id=review_id, for_update=True)
        if review.moderation.version != request.expected_version:
            raise _conflict("The review moderation state changed; reload before requesting assistance.")
        moderation = review.moderation
        moderation.ai_categories_json = list(proposal.categories)
        moderation.ai_theme_summary = proposal.theme_summary
        moderation.ai_suggested_action = proposal.suggested_action
        moderation.ai_provider_metadata_json = provider_metadata
        if proposal.response_draft:
            moderation.response_draft = proposal.response_draft
        moderation.version += 1
        moderation.updated_at = datetime.now(timezone.utc)
        _record_action(
            review,
            idempotency_key=idempotency_key,
            action="assist",
            expected_version=request.expected_version,
            principal=principal,
            reason=None,
        )
        db.flush()
        return _review_response(review).model_dump(mode="json")

    response, _ = _idempotent(
        db,
        key=idempotency_key,
        operation=operation,
        payload=payload,
        principal=principal,
        action=action,
    )
    return AdminProductReviewResponse.model_validate(response)


def decide_product_review(
    db: Session,
    *,
    product_id: str,
    review_id: str,
    request: ReviewDecisionRequest,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
) -> AdminProductReviewResponse:
    payload = request.model_dump(mode="json")

    def action() -> dict:
        review = _review_query(db, product_id=product_id, review_id=review_id, for_update=True)
        moderation = review.moderation
        if moderation.version != request.expected_version:
            raise _conflict("The review moderation state changed; reload before deciding.")
        now = datetime.now(timezone.utc)
        if request.action in {"approve", "flag", "reject"}:
            moderation.state = {
                "approve": "approved",
                "flag": "flagged",
                "reject": "rejected",
            }[request.action]
            moderation.decided_by = principal.provider_user_id
            moderation.decided_at = now
            moderation.decision_reason = request.reason
        elif request.action == "save_response":
            moderation.response_draft = request.response_text
        else:
            if moderation.state != "approved":
                raise HTTPException(
                    status_code=422,
                    detail="A merchant response can be published only for an approved review.",
                )
            response_text = request.response_text or moderation.response_draft
            if not response_text:
                raise HTTPException(status_code=422, detail="No merchant response draft is available.")
            moderation.response_draft = response_text
            moderation.response_published = response_text
            moderation.response_published_at = now
        moderation.version += 1
        moderation.updated_at = now
        _record_action(
            review,
            idempotency_key=idempotency_key,
            action=request.action,
            expected_version=request.expected_version,
            principal=principal,
            reason=request.reason,
        )
        db.flush()
        return _review_response(review).model_dump(mode="json")

    response, _ = _idempotent(
        db,
        key=idempotency_key,
        operation=f"catalog.review.decision:{review_id}",
        payload=payload,
        principal=principal,
        action=action,
    )
    return AdminProductReviewResponse.model_validate(response)


def public_product_reviews(db: Session, *, product_id: str) -> list[PublicProductReview]:
    rows = db.scalars(
        select(CatalogProductReview)
        .join(CatalogReviewModeration)
        .options(selectinload(CatalogProductReview.moderation))
        .where(
            CatalogProductReview.catalog_product_id == product_id,
            CatalogReviewModeration.state == "approved",
        )
        .order_by(CatalogProductReview.submitted_at.desc(), CatalogProductReview.id)
    ).all()
    return [
        PublicProductReview(
            id=row.id,
            author_display_name=row.author_display_name,
            body=row.body,
            rating=row.rating,
            submitted_at=row.submitted_at,
            merchant_response=row.moderation.response_published,
            merchant_responded_at=row.moderation.response_published_at,
        )
        for row in rows
    ]
