from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.catalog.admin_schemas import (
    CatalogFieldSuggestionResponse,
    CatalogSuggestionDecisionRequest,
    CatalogSuggestionDecisionResponse,
    CatalogSuggestionReviewResponse,
    CatalogSuggestionSetCreateRequest,
    CatalogSuggestionSetListResponse,
    CatalogSuggestionSetResponse,
    ProductDraftV3,
    product_draft_v3_from_snapshot,
)
from app.models import (
    CatalogDraftRevision,
    CatalogFieldSuggestion,
    CatalogSourceAsset,
    CatalogSourceBundle,
    CatalogSuggestionReview,
    CatalogSuggestionSet,
    CatalogWorkflow,
)
from app.services.auth.clerk import AuthenticatedPrincipal
from app.services.catalog_admin import (
    _admin_draft_snapshot_v3,
    _idempotent,
    _latest_owned_draft,
    _owned_draft_for_update,
    _supersede_other_suggestion_sets,
    draft_revision_version,
)


_TARGET_SECTIONS = {
    "/title": "identity",
    "/description": "content",
    "/brand_id": "identity",
    "/brand": "identity",
    "/category": "identity",
    "/price_min": "identity",
    "/price_max": "identity",
    "/link": "identity",
    "/color": "identity",
    "/material": "identity",
    "/gender": "identity",
    "/season": "identity",
    "/benefits": "content",
    "/specifications": "content",
    "/care_instructions": "content",
    "/content_details": "content",
    "/seo/title": "seo",
    "/seo/description": "seo",
    "/seo/keywords": "seo",
}


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _section_for_path(path: str) -> str:
    if path in _TARGET_SECTIONS:
        return _TARGET_SECTIONS[path]
    parts = path.split("/")
    if len(parts) == 4 and parts[1] == "media" and parts[2] and parts[3] == "alt_text":
        return "media"
    raise _unprocessable(f"Unsupported suggestion target_path: {path!r}.")


def validate_suggestion_target_path(path: str) -> str:
    """Validate an application-owned suggestion path and return its editor section."""

    return _section_for_path(path)


def _media_index(product_payload: dict[str, Any], media_id: str) -> int:
    for index, media in enumerate(product_payload.get("media", [])):
        if media.get("media_id") == media_id:
            return index
    raise _unprocessable(f"Unknown media target: {media_id!r}.")


def _read_target(product_payload: dict[str, Any], path: str) -> Any:
    section = _section_for_path(path)
    if section == "media":
        _, _, media_id, field = path.split("/")
        return deepcopy(product_payload["media"][_media_index(product_payload, media_id)].get(field))
    value: Any = product_payload
    for part in path.lstrip("/").split("/"):
        if not isinstance(value, dict) or part not in value:
            raise _unprocessable(f"Unknown suggestion target_path: {path!r}.")
        value = value[part]
    return deepcopy(value)


def _write_target(product_payload: dict[str, Any], path: str, value: Any) -> None:
    section = _section_for_path(path)
    if section == "media":
        _, _, media_id, field = path.split("/")
        product_payload["media"][_media_index(product_payload, media_id)][field] = deepcopy(value)
        return
    parts = path.lstrip("/").split("/")
    target: dict[str, Any] = product_payload
    for part in parts[:-1]:
        nested = target.get(part)
        if not isinstance(nested, dict):
            raise _unprocessable(f"Unknown suggestion target_path: {path!r}.")
        target = nested
    if parts[-1] not in target:
        raise _unprocessable(f"Unknown suggestion target_path: {path!r}.")
    target[parts[-1]] = deepcopy(value)


def _validated_product(payload: dict[str, Any]) -> ProductDraftV3:
    try:
        return ProductDraftV3.model_validate(payload)
    except ValidationError as exc:
        raise _unprocessable(f"Suggestion value is invalid for its target: {exc.errors()[0]['msg']}.") from exc


def _owned_set(
    db: Session,
    *,
    suggestion_set_id: str,
    product_id: str,
    principal: AuthenticatedPrincipal,
    for_update: bool = False,
) -> CatalogSuggestionSet:
    query = select(CatalogSuggestionSet).where(
        CatalogSuggestionSet.id == suggestion_set_id,
        CatalogSuggestionSet.catalog_product_id == product_id,
        CatalogSuggestionSet.owner_provider == principal.provider,
        CatalogSuggestionSet.owner_provider_user_id == principal.provider_user_id,
    )
    query = query.options(
        selectinload(CatalogSuggestionSet.suggestions),
        selectinload(CatalogSuggestionSet.reviews),
    )
    if for_update:
        query = query.execution_options(populate_existing=True).with_for_update()
    suggestion_set = db.scalar(query)
    if suggestion_set is None:
        raise HTTPException(status_code=404, detail="Catalog suggestion set not found.")
    return suggestion_set


def _field_response(row: CatalogFieldSuggestion) -> CatalogFieldSuggestionResponse:
    return CatalogFieldSuggestionResponse(
        id=row.id,
        section=row.section,
        target_path=row.target_path,
        proposed_value=deepcopy(row.proposed_value_json),
        baseline_value=deepcopy(row.baseline_value_json),
        prior_value=deepcopy(row.prior_value_json),
        evidence_asset_ids=list(row.evidence_asset_ids_json or []),
        certainty_class=row.certainty_class,  # type: ignore[arg-type]
        input_origin=row.input_origin,  # type: ignore[arg-type]
        status=row.status,  # type: ignore[arg-type]
        reviewed_by=row.reviewed_by,
        reviewed_at=row.reviewed_at,
        review_reason=row.review_reason,
        applied_draft_revision_id=row.applied_draft_revision_id,
        created_at=row.created_at,
    )


def _review_response(row: CatalogSuggestionReview) -> CatalogSuggestionReviewResponse:
    target = dict(row.target_json or {})
    return CatalogSuggestionReviewResponse(
        id=row.id,
        action=row.action,  # type: ignore[arg-type]
        scope=row.scope,  # type: ignore[arg-type]
        suggestion_ids=list(target.get("suggestion_ids") or []),
        section=target.get("section"),
        expected_draft_version=row.expected_draft_version,
        resulting_draft_revision_id=row.resulting_draft_revision_id,
        actor_provider_user_id=row.actor_provider_user_id,
        reason=row.reason,
        created_at=row.created_at,
    )


def _set_response(row: CatalogSuggestionSet) -> CatalogSuggestionSetResponse:
    return CatalogSuggestionSetResponse(
        id=row.id,
        product_id=row.catalog_product_id,
        base_draft_id=row.base_draft_revision_id,
        base_draft_version=row.base_draft_version,
        current_draft_id=row.current_draft_revision_id,
        current_draft_version=row.current_draft_version,
        workflow_id=row.workflow_id,
        status=row.status,  # type: ignore[arg-type]
        suggestions=[_field_response(item) for item in row.suggestions],
        reviews=[_review_response(item) for item in row.reviews],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _validate_evidence(
    db: Session,
    *,
    evidence_asset_ids: set[str],
    product: ProductDraftV3,
    principal: AuthenticatedPrincipal,
) -> None:
    if not evidence_asset_ids:
        return
    referenced_assets = {
        asset_id
        for reference in product.source_references
        for asset_id in reference.asset_ids
    }
    if not evidence_asset_ids.issubset(referenced_assets):
        raise _unprocessable("Suggestion evidence must reference source assets attached to this draft.")
    owned_asset_ids = set(
        db.scalars(
            select(CatalogSourceAsset.id)
            .join(CatalogSourceBundle, CatalogSourceAsset.bundle_id == CatalogSourceBundle.id)
            .where(
                CatalogSourceAsset.id.in_(evidence_asset_ids),
                CatalogSourceBundle.owner_provider == principal.provider,
                CatalogSourceBundle.owner_provider_user_id == principal.provider_user_id,
            )
        ).all()
    )
    if owned_asset_ids != evidence_asset_ids:
        raise HTTPException(status_code=404, detail="Catalog source asset not found.")


def create_suggestion_set(
    db: Session,
    *,
    product_id: str,
    request: CatalogSuggestionSetCreateRequest,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
) -> tuple[CatalogSuggestionSetResponse, bool]:
    payload = request.model_dump(mode="json")
    operation = f"catalog.v3.suggestions.create:{product_id}"

    def action() -> dict:
        current = _latest_owned_draft(db, product_id, principal)
        if current is None or current.id != request.draft_id:
            raise HTTPException(status_code=409, detail="The requested catalog draft is no longer current.")
        current_version = draft_revision_version(db, current)
        if current_version != request.expected_draft_version:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Expected catalog draft version {request.expected_draft_version}, "
                    f"but current version is {current_version}."
                ),
            )
        product = product_draft_v3_from_snapshot(current.snapshot_json)
        product_payload = product.model_dump(mode="json")
        evidence_ids = {
            asset_id
            for suggestion in request.suggestions
            for asset_id in suggestion.evidence_asset_ids
        }
        _validate_evidence(
            db,
            evidence_asset_ids=evidence_ids,
            product=product,
            principal=principal,
        )
        workflow = None
        if request.workflow_id:
            workflow = db.scalar(
                select(CatalogWorkflow).where(
                    CatalogWorkflow.id == request.workflow_id,
                    CatalogWorkflow.owner_provider == principal.provider,
                    CatalogWorkflow.owner_provider_user_id == principal.provider_user_id,
                )
            )
            if workflow is None:
                raise HTTPException(status_code=404, detail="Catalog Studio catalog workflow not found.")
            if workflow.draft_revision_id not in (None, current.id):
                raise HTTPException(status_code=409, detail="Catalog workflow is linked to another draft.")
            if (
                workflow.published_product_id
                and workflow.published_product_id != product_id
            ):
                raise HTTPException(status_code=409, detail="Catalog workflow is linked to another product.")

        suggestion_set = CatalogSuggestionSet(
            id=f"suggestion_set_{uuid4().hex[:24]}",
            owner_provider=principal.provider,
            owner_provider_user_id=principal.provider_user_id,
            catalog_product_id=product_id,
            base_draft_revision_id=current.id,
            base_draft_version=current_version,
            current_draft_revision_id=current.id,
            current_draft_version=current_version,
            workflow_id=workflow.id if workflow else None,
            status="pending",
        )
        db.add(suggestion_set)
        for suggestion in request.suggestions:
            section = _section_for_path(suggestion.target_path)
            baseline = _read_target(product_payload, suggestion.target_path)
            candidate_payload = deepcopy(product_payload)
            _write_target(candidate_payload, suggestion.target_path, suggestion.proposed_value)
            _validated_product(candidate_payload)
            db.add(
                CatalogFieldSuggestion(
                    id=f"suggestion_{uuid4().hex[:24]}",
                    suggestion_set=suggestion_set,
                    section=section,
                    target_path=suggestion.target_path,
                    proposed_value_json=deepcopy(suggestion.proposed_value),
                    baseline_value_json=baseline,
                    evidence_asset_ids_json=list(suggestion.evidence_asset_ids),
                    certainty_class=suggestion.certainty_class,
                    input_origin=suggestion.input_origin,
                    status="pending",
                )
            )
        if workflow:
            workflow.draft_revision_id = current.id
            workflow.updated_at = datetime.now(timezone.utc)
        db.flush()
        return _set_response(suggestion_set).model_dump(mode="json")

    response, replayed = _idempotent(
        db,
        key=idempotency_key,
        operation=operation,
        payload=payload,
        principal=principal,
        action=action,
    )
    return CatalogSuggestionSetResponse.model_validate(response), replayed


def list_suggestion_sets(
    db: Session,
    *,
    product_id: str,
    principal: AuthenticatedPrincipal,
) -> CatalogSuggestionSetListResponse:
    rows = db.scalars(
        select(CatalogSuggestionSet)
        .options(
            selectinload(CatalogSuggestionSet.suggestions),
            selectinload(CatalogSuggestionSet.reviews),
        )
        .where(
            CatalogSuggestionSet.catalog_product_id == product_id,
            CatalogSuggestionSet.owner_provider == principal.provider,
            CatalogSuggestionSet.owner_provider_user_id == principal.provider_user_id,
        )
        .order_by(CatalogSuggestionSet.created_at.desc(), CatalogSuggestionSet.id.desc())
    ).all()
    return CatalogSuggestionSetListResponse(items=[_set_response(row) for row in rows])


def _selected_suggestions(
    suggestion_set: CatalogSuggestionSet,
    request: CatalogSuggestionDecisionRequest,
) -> list[CatalogFieldSuggestion]:
    pending = [item for item in suggestion_set.suggestions if item.status == "pending"]
    if request.scope == "suggestion":
        selected = [item for item in pending if item.id == request.suggestion_id]
    elif request.scope == "section":
        selected = [item for item in pending if item.section == request.section]
    else:
        selected = pending
    if not selected:
        raise HTTPException(status_code=409, detail="No pending suggestions match this decision.")
    return selected


def decide_suggestion_set(
    db: Session,
    *,
    product_id: str,
    suggestion_set_id: str,
    request: CatalogSuggestionDecisionRequest,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
) -> tuple[CatalogSuggestionDecisionResponse, bool]:
    payload = request.model_dump(mode="json")
    operation = f"catalog.v3.suggestions.decide:{suggestion_set_id}"

    def action() -> dict:
        initial_set = _owned_set(
            db,
            suggestion_set_id=suggestion_set_id,
            product_id=product_id,
            principal=principal,
        )
        current = _owned_draft_for_update(
            db,
            draft_id=initial_set.current_draft_revision_id,
            product_id=product_id,
            principal=principal,
        )
        suggestion_set = _owned_set(
            db,
            suggestion_set_id=suggestion_set_id,
            product_id=product_id,
            principal=principal,
            for_update=True,
        )
        if suggestion_set.status == "superseded":
            raise HTTPException(status_code=409, detail="This suggestion set has been superseded.")
        latest = _latest_owned_draft(db, product_id, principal)
        if (
            current is None
            or latest is None
            or latest.id != current.id
            or current.id != suggestion_set.current_draft_revision_id
        ):
            raise HTTPException(status_code=409, detail="The suggestion set no longer targets the current draft.")
        actual_version = draft_revision_version(db, current)
        if (
            actual_version != request.expected_draft_version
            or suggestion_set.current_draft_version != request.expected_draft_version
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Expected catalog draft version {request.expected_draft_version}, "
                    f"but current version is {actual_version}."
                ),
            )
        selected = _selected_suggestions(suggestion_set, request)
        now = datetime.now(timezone.utc)
        resulting_revision = None
        if request.action == "accept":
            product_payload = product_draft_v3_from_snapshot(current.snapshot_json).model_dump(
                mode="json"
            )
            prior_values: dict[str, Any] = {}
            for suggestion in selected:
                prior_values[suggestion.id] = _read_target(product_payload, suggestion.target_path)
                _write_target(
                    product_payload,
                    suggestion.target_path,
                    suggestion.proposed_value_json,
                )
            updated_product = _validated_product(product_payload)
            resulting_revision = CatalogDraftRevision(
                id=f"draft_{uuid4().hex[:24]}",
                catalog_product_id=product_id,
                base_version=current.base_version,
                status="draft",
                moderation_state=current.moderation_state,
                snapshot_json=updated_product.model_dump(mode="json"),
                created_by=principal.provider_user_id,
            )
            db.add(resulting_revision)
            db.flush()
            _supersede_other_suggestion_sets(
                db,
                product_id=product_id,
                current_draft_id=current.id,
                exclude_set_id=suggestion_set.id,
            )
            referenced_bundle_ids = {
                reference.bundle_id for reference in updated_product.source_references
            }
            if referenced_bundle_ids:
                for bundle in db.scalars(
                    select(CatalogSourceBundle).where(
                        CatalogSourceBundle.id.in_(referenced_bundle_ids),
                        CatalogSourceBundle.owner_provider == principal.provider,
                        CatalogSourceBundle.owner_provider_user_id
                        == principal.provider_user_id,
                    )
                ).all():
                    bundle.catalog_product_id = product_id
                    bundle.draft_revision_id = resulting_revision.id
                    bundle.updated_at = now
            for workflow in db.scalars(
                select(CatalogWorkflow).where(
                    CatalogWorkflow.draft_revision_id == current.id,
                    CatalogWorkflow.owner_provider == principal.provider,
                    CatalogWorkflow.owner_provider_user_id == principal.provider_user_id,
                )
            ).all():
                workflow.draft_revision_id = resulting_revision.id
                workflow.updated_at = now
            for suggestion in selected:
                suggestion.prior_value_json = prior_values[suggestion.id]
                suggestion.status = "accepted"
                suggestion.applied_draft_revision_id = resulting_revision.id
            suggestion_set.current_draft_revision_id = resulting_revision.id
            suggestion_set.current_draft_version = actual_version + 1
        else:
            product_payload = product_draft_v3_from_snapshot(
                current.snapshot_json
            ).model_dump(mode="json")
            for suggestion in selected:
                suggestion.prior_value_json = _read_target(
                    product_payload,
                    suggestion.target_path,
                )
                suggestion.status = "rejected" if request.action == "reject" else "superseded"

        for suggestion in selected:
            suggestion.reviewed_by = principal.provider_user_id
            suggestion.reviewed_at = now
            suggestion.review_reason = request.reason
        pending_count = sum(
            1 for suggestion in suggestion_set.suggestions if suggestion.status == "pending"
        )
        if pending_count:
            suggestion_set.status = "partially_reviewed"
        elif request.action == "supersede":
            suggestion_set.status = "superseded"
        else:
            suggestion_set.status = "reviewed"
        suggestion_set.updated_at = now
        review = CatalogSuggestionReview(
            id=f"suggestion_review_{uuid4().hex[:24]}",
            suggestion_set=suggestion_set,
            action=request.action,
            scope=request.scope,
            target_json={
                "suggestion_ids": [suggestion.id for suggestion in selected],
                **({"section": request.section} if request.section else {}),
            },
            expected_draft_version=request.expected_draft_version,
            resulting_draft_revision_id=(
                resulting_revision.id if resulting_revision else None
            ),
            actor_provider=principal.provider,
            actor_provider_user_id=principal.provider_user_id,
            reason=request.reason,
        )
        db.add(review)
        db.flush()
        draft = (
            _admin_draft_snapshot_v3(db, resulting_revision, principal)
            if resulting_revision
            else None
        )
        return CatalogSuggestionDecisionResponse(
            suggestion_set=_set_response(suggestion_set),
            draft=draft,
        ).model_dump(mode="json")

    response, replayed = _idempotent(
        db,
        key=idempotency_key,
        operation=operation,
        payload=payload,
        principal=principal,
        action=action,
    )
    return CatalogSuggestionDecisionResponse.model_validate(response), replayed
