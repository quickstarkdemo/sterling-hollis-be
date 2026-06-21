from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import base64
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Mapping
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog.admin_schemas import (
    CatalogFieldSuggestionCreate,
    CatalogSuggestionSetCreateRequest,
    ProductDraftV2,
    ProductDraftV3,
    ProductInventoryDraftV2,
    product_draft_v2_from_snapshot,
    product_draft_v3_from_snapshot,
)
from app.catalog.ai_schemas import (
    CatalogAICommandRequest,
    CatalogAICommandResult,
    CatalogAIDraftResult,
    CatalogAIInventoryProposal,
    CatalogAIProductProposal,
    CatalogAISuggestionCommandRequest,
    CatalogAISuggestionCommandResult,
    CatalogAISuggestionProposal,
)
from app.catalog.references import normalized_brand_name
from app.catalog.workflow_schemas import WorkflowEventInput
from app.api_traces.adapters import (
    new_openai_client_request_id,
    openai_request_ids,
)
from app.config import Settings
from app.models import (
    CatalogAdminMutation,
    CatalogBrand,
    CatalogDraftRevision,
    CatalogProduct,
    CatalogSourceAsset,
    CatalogSourceBundle,
    CatalogWorkflow,
    Store,
    SyntheticRun,
)
from app.observability.genai_otel import genai_llm_span, set_span_attributes
from app.services.auth.clerk import AuthenticatedPrincipal
from app.services.catalog_admin import (
    _latest_owned_draft,
    draft_revision_version,
)
from app.services.catalog_normalization import catalog_key_for_values, catalog_product_id_for_key
from app.services.taxonomy import CATEGORY_TAXONOMY
from app.services.catalog_workflow import append_workflow_event, normalize_usage
from app.services.catalog_suggestions import (
    create_suggestion_set,
    validate_suggestion_target_path,
)


CATALOG_AI_INSTRUCTIONS = """You create private Sterling Hollis retail catalog drafts.
Return only the requested structured product proposal. Preserve the current draft unless the
presenter explicitly asks for a change. Use only the provided canonical brand, store, category,
and availability IDs. Return one product with product-level price, attributes, optional inventory,
concise customer-safe copy, and useful image art direction. Do not create commerce variants. Do not
include people, customer identity, secrets, URLs, medical claims, sexual content, hateful content, instructions
for wrongdoing, or private reasoning. If the instruction is unrelated to a retail product, make
the smallest reasonable catalog proposal consistent with the current draft."""


CATALOG_SUGGESTION_INSTRUCTIONS = """You propose reviewable Sterling Hollis product field changes.
Return only the requested structured suggestion proposal. Use only the requested target paths.
For supplier analysis, cite every directly visible fact with one or more provided source asset IDs.
Classify marketing language and other inferences as derived. Never infer exact dimensions, weight,
identifiers, GTIN or UPC values, compliance, certifications, or safety claims from appearance. Put
unsupported facts in unknown_fields with a concise question. Do not return inventory, lifecycle,
publication, archive, credentials, private reasoning, or customer data. Suggestions are proposals
only and must never be described as already saved or published."""

_SUPPLIER_OBSERVED_PATHS = {"/color", "/material", "/specifications"}
_SUPPLIER_UNSUPPORTED_PATHS = {"/price_min", "/price_max", "/link"}


class CatalogAICommandError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        detail: str,
        status_code: int,
        retryable: bool,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code
        self.retryable = retryable


class CatalogAIService:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self.client = client

    def execute(
        self,
        db: Session,
        *,
        workflow_id: str,
        command: CatalogAICommandRequest,
        idempotency_key: str,
        principal: AuthenticatedPrincipal,
    ) -> CatalogAICommandResult:
        return execute_catalog_ai_command(
            db,
            workflow_id=workflow_id,
            command=command,
            idempotency_key=idempotency_key,
            principal=principal,
            settings=self.settings,
            client=self.client,
        )


class CatalogAISuggestionService:
    """Generate grounded v3 proposals without mutating the canonical draft."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self.client = client

    def execute(
        self,
        db: Session,
        *,
        product_id: str,
        command: CatalogAISuggestionCommandRequest,
        idempotency_key: str,
        principal: AuthenticatedPrincipal,
    ) -> CatalogAISuggestionCommandResult:
        return execute_catalog_ai_suggestion_command(
            db,
            product_id=product_id,
            command=command,
            idempotency_key=idempotency_key,
            principal=principal,
            settings=self.settings,
            client=self.client,
        )


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _command_hash(workflow_id: str, command: CatalogAICommandRequest) -> str:
    payload = json.dumps(
        {"workflow_id": workflow_id, **command.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _mutation_key(
    *, workflow_id: str, idempotency_key: str, principal: AuthenticatedPrincipal
) -> str:
    source = f"{principal.provider}:{principal.provider_user_id}:{workflow_id}:{idempotency_key}"
    return f"ai_{hashlib.sha256(source.encode()).hexdigest()}"


def _safety_identifier(principal: AuthenticatedPrincipal) -> str:
    source = f"{principal.provider}:{principal.provider_user_id}"
    return hashlib.sha256(source.encode()).hexdigest()


def _event_prefix(mutation_key: str) -> str:
    return f"catalog-ai-{mutation_key.removeprefix('ai_')[:32]}"


def _owned_workflow(
    db: Session,
    *,
    workflow_id: str,
    principal: AuthenticatedPrincipal,
    lock: bool,
) -> CatalogWorkflow:
    statement = select(CatalogWorkflow).where(CatalogWorkflow.id == workflow_id)
    if lock:
        statement = statement.with_for_update()
    workflow = db.scalar(statement)
    if (
        workflow is None
        or workflow.owner_provider != principal.provider
        or workflow.owner_provider_user_id != principal.provider_user_id
    ):
        raise HTTPException(status_code=404, detail="Catalog Studio catalog workflow not found.")
    return workflow


def _validate_command_state(
    db: Session,
    *,
    workflow: CatalogWorkflow,
    command: CatalogAICommandRequest,
    principal: AuthenticatedPrincipal,
) -> tuple[CatalogDraftRevision | None, int]:
    if command.current_draft_id is None:
        if workflow.draft_revision_id is not None:
            raise _conflict(
                "This catalog workflow already has a draft; refine its current draft instead."
            )
        return None, 0

    if workflow.draft_revision_id != command.current_draft_id:
        raise _conflict("The requested draft is no longer current for this catalog workflow.")
    revision = db.get(CatalogDraftRevision, command.current_draft_id)
    if revision is None or revision.created_by != principal.provider_user_id:
        raise HTTPException(status_code=404, detail="Catalog draft revision not found.")
    if revision.snapshot_json.get("schema_version") == 3:
        raise _conflict(
            "V3 catalog drafts require reviewable suggestion commands instead of direct AI mutation."
        )
    actual_version = draft_revision_version(db, revision)
    if actual_version != command.expected_draft_version:
        raise _conflict(
            f"Expected AI draft version {command.expected_draft_version}, "
            f"but current version is {actual_version}."
        )
    return revision, actual_version


def _replay_existing_mutation(
    mutation: CatalogAdminMutation,
    *,
    operation: str,
    request_hash: str,
) -> CatalogAICommandResult:
    if mutation.operation != operation or mutation.request_hash != request_hash:
        raise _conflict(
            "Idempotency-Key was already used for a different Catalog Studio AI command."
        )
    payload = dict(mutation.response_json or {})
    state = payload.get("state")
    if state == "completed":
        result = CatalogAICommandResult.model_validate(payload.get("result") or {})
        return result.model_copy(update={"replayed": True})
    if state == "failed":
        raise CatalogAICommandError(
            code=str(payload.get("code") or "catalog_ai_failed"),
            detail=str(payload.get("detail") or "The Catalog Studio AI command failed."),
            status_code=int(payload.get("status_code") or 502),
            retryable=bool(payload.get("retryable")),
        )
    raise _conflict("This Catalog Studio AI command is already processing.")


def _reserve_command(
    db: Session,
    *,
    workflow_id: str,
    command: CatalogAICommandRequest,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
) -> tuple[CatalogAdminMutation, CatalogAICommandResult | None]:
    key = idempotency_key.strip()
    if not key:
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank.")
    mutation_key = _mutation_key(
        workflow_id=workflow_id, idempotency_key=key, principal=principal
    )
    operation = f"catalog.ai:{workflow_id}"
    request_hash = _command_hash(workflow_id, command)
    existing = db.get(CatalogAdminMutation, mutation_key)
    if existing is not None:
        return existing, _replay_existing_mutation(
            existing, operation=operation, request_hash=request_hash
        )

    mutation = CatalogAdminMutation(
        idempotency_key=mutation_key,
        operation=operation,
        request_hash=request_hash,
        response_json={"state": "processing"},
        created_by=principal.provider_user_id,
    )
    db.add(mutation)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        concurrent = db.get(CatalogAdminMutation, mutation_key)
        if concurrent is None:
            raise _conflict("Catalog Studio AI command state changed; retry.")
        return concurrent, _replay_existing_mutation(
            concurrent, operation=operation, request_hash=request_hash
        )
    return mutation, None


def _resolve_client(settings: Settings, client: Any | None) -> Any:
    if client is not None:
        return client
    if not settings.openai_api_key:
        raise CatalogAICommandError(
            code="responses_unavailable",
            detail="The Responses capability is not configured.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            retryable=False,
        )
    try:
        from openai import OpenAI

        return OpenAI(api_key=settings.openai_api_key)
    except Exception as exc:  # pragma: no cover - environment-specific constructor failure
        raise CatalogAICommandError(
            code="responses_unavailable",
            detail="The Responses capability is unavailable.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            retryable=False,
        ) from exc


def _server_catalog_context(
    db: Session, revision: CatalogDraftRevision | None
) -> tuple[str, str, ProductDraftV2 | None]:
    if revision is not None:
        current = product_draft_v2_from_snapshot(revision.snapshot_json)
        store_id = current.inventory[0].store_id
        return current.seed_run_id, store_id, current

    context = db.execute(
        select(SyntheticRun.id, Store.id)
        .join(Store, Store.seed_run_id == SyntheticRun.id)
        .order_by(SyntheticRun.started_at.desc(), SyntheticRun.id, Store.id)
    ).first()
    if context is None:
        raise CatalogAICommandError(
            code="catalog_context_unavailable",
            detail=(
                "Catalog Studio needs a loaded catalog workflow and inventory store "
                "before drafting products."
            ),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            retryable=False,
        )
    seed_run_id, store_id = context
    return seed_run_id, store_id, None


def _provider_input(
    command: CatalogAICommandRequest,
    current: ProductDraftV2 | None,
    references: dict[str, Any],
) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "presenter_instruction": command.instruction,
        "catalog_references": references,
    }
    if current is not None:
        payload["current_draft"] = current.model_dump(mode="json")
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": json.dumps(payload, sort_keys=True, separators=(",", ":")),
                }
            ],
        }
    ]


def _moderation_result(value: Any, *, label: str) -> dict[str, Any]:
    if value is None or getattr(value, "type", None) != "moderation_result":
        raise CatalogAICommandError(
            code="moderation_unavailable",
            detail=f"The {label} moderation check did not complete.",
            status_code=status.HTTP_502_BAD_GATEWAY,
            retryable=True,
        )
    categories = dict(getattr(value, "categories", {}) or {})
    scores = dict(getattr(value, "category_scores", {}) or {})
    return {
        "flagged": bool(getattr(value, "flagged", False)),
        "model": str(getattr(value, "model", "")),
        "categories": {str(key): bool(flagged) for key, flagged in categories.items()},
        "category_scores": {
            str(key): float(score)
            for key, score in scores.items()
            if isinstance(score, int | float) and not isinstance(score, bool)
        },
    }


def _combined_moderation(input_result: dict, output_result: dict) -> dict[str, Any]:
    flagged_categories = sorted(
        {
            str(key)
            for result in (input_result, output_result)
            for key, flagged in dict(result.get("categories") or {}).items()
            if flagged
        }
    )
    scores: dict[str, float] = {}
    for result in (input_result, output_result):
        for key, score in dict(result.get("category_scores") or {}).items():
            scores[str(key)] = max(float(score), scores.get(str(key), 0.0))
    blocked = bool(input_result.get("flagged") or output_result.get("flagged"))
    return {
        "flagged": blocked,
        "decision": "blocked" if blocked else "approved",
        "categories": flagged_categories,
        "category_scores": scores,
        "input": input_result,
        "output": output_result,
    }


def _usage(response: Any) -> dict[str, int]:
    value = getattr(response, "usage", None)
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return normalize_usage(value if isinstance(value, Mapping) else {})


def _catalog_reference_context(db: Session) -> dict[str, Any]:
    brands = db.scalars(
        select(CatalogBrand).where(CatalogBrand.active.is_(True)).order_by(CatalogBrand.name)
    ).all()
    stores = db.scalars(select(Store).order_by(Store.name, Store.id)).all()
    return {
        "brands": [{"id": brand.id, "name": brand.name} for brand in brands],
        "stores": [
            {
                "id": store.id,
                "name": store.name,
                "city": store.city,
                "state": store.state,
            }
            for store in stores
        ],
        "categories": sorted(CATEGORY_TAXONOMY),
        "availability": ["in stock", "low stock", "preorder", "out of stock"],
    }


def _product_from_proposal(
    db: Session,
    proposal: CatalogAIProductProposal,
    *,
    seed_run_id: str,
    store_id: str,
    product_id: str | None,
    current: ProductDraftV2 | None,
) -> ProductDraftV2:
    brand = db.get(CatalogBrand, proposal.brand_id)
    if (
        brand is None
        or not brand.active
        or brand.normalized_name != normalized_brand_name(proposal.brand)
    ):
        raise CatalogAICommandError(
            code="unknown_catalog_brand",
            detail="Select an existing canonical brand or use Add Brand before saving this draft.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            retryable=False,
        )
    inventory = proposal.inventory or (
        [
            CatalogAIInventoryProposal(
                store_id=store_id,
                availability="out of stock",
                inventory_qty=0,
            )
        ]
        if current is None
        else []
    )
    if inventory:
        proposed_store_ids = {row.store_id for row in inventory}
        known_store_ids = set(
            db.scalars(select(Store.id).where(Store.id.in_(proposed_store_ids))).all()
        )
        if proposed_store_ids != known_store_ids:
            raise CatalogAICommandError(
                code="unknown_catalog_store",
                detail="Select an existing catalog store before saving initial inventory.",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                retryable=False,
            )
    if product_id is None:
        key = catalog_key_for_values(
            brand=brand.name,
            title=proposal.title,
            category=proposal.category,
        )
        product_id = catalog_product_id_for_key(key)
    return ProductDraftV2(
        product_id=product_id,
        seed_run_id=seed_run_id,
        title=proposal.title,
        description=proposal.description,
        brand_id=brand.id,
        brand=brand.name,
        category=proposal.category,
        price_min=Decimal(str(proposal.price_min)),
        price_max=Decimal(str(proposal.price_max)),
        link=proposal.link,
        color=proposal.color,
        material=proposal.material,
        gender=proposal.gender,
        season=proposal.season,
        metadata={
            **(current.metadata if current else {}),
            "source": "catalog_studio_responses",
            "image_direction": proposal.image_direction,
            "design_specification": proposal.design_specification.model_dump(mode="json"),
        },
        media=list(current.media) if current else [],
        inventory=(
            [
                ProductInventoryDraftV2(
                    store_id=row.store_id,
                    size=row.size,
                    availability=row.availability,
                    inventory_qty=row.inventory_qty,
                )
                for row in inventory
            ]
            if inventory
            else list(current.inventory if current else [])
        ),
    )


def _store_failure(
    db: Session,
    *,
    mutation: CatalogAdminMutation,
    workflow_id: str,
    principal: AuthenticatedPrincipal,
    settings: Settings,
    error: CatalogAICommandError,
    client_request_id: str | None = None,
) -> None:
    prefix = _event_prefix(mutation.idempotency_key)
    mutation.response_json = {
        "state": "failed",
        "code": error.code,
        "detail": error.detail,
        "status_code": error.status_code,
        "retryable": error.retryable,
    }
    append_workflow_event(
        db,
        workflow_id=workflow_id,
        principal=principal,
        settings=settings,
        commit=False,
        event=WorkflowEventInput(
            client_event_id=f"{prefix}-responses-failed",
            stage="draft",
            capability="responses",
            status="failed",
            business_summary=error.detail,
            error_code=error.code,
            retryable=error.retryable,
            request_payload={
                "client_request_id": client_request_id,
                "input": {"action": "create_or_refine_draft"},
            },
            response_payload={"status": "failed", "error_code": error.code},
        ),
    )
    db.commit()


def _record_failure(
    db: Session,
    *,
    mutation: CatalogAdminMutation,
    workflow_id: str,
    principal: AuthenticatedPrincipal,
    settings: Settings,
    error: CatalogAICommandError,
    client_request_id: str | None = None,
) -> CatalogAICommandError:
    mutation_key = mutation.idempotency_key
    db.rollback()
    persisted_mutation = db.get(CatalogAdminMutation, mutation_key) or mutation
    _store_failure(
        db,
        mutation=persisted_mutation,
        workflow_id=workflow_id,
        principal=principal,
        settings=settings,
        error=error,
        client_request_id=client_request_id,
    )
    return error


def _provider_failure(exc: Exception) -> CatalogAICommandError:
    if isinstance(exc, TimeoutError) or type(exc).__name__ == "APITimeoutError":
        return CatalogAICommandError(
            code="responses_timeout",
            detail="Responses timed out before the draft was ready.",
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            retryable=True,
        )
    if type(exc).__name__ in {"APIConnectionError", "ConnectError"}:
        return CatalogAICommandError(
            code="responses_unavailable",
            detail="Responses is temporarily unavailable.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            retryable=True,
        )
    if type(exc).__name__ in {
        "ValidationError",
        "LengthFinishReasonError",
        "ContentFilterFinishReasonError",
    }:
        return CatalogAICommandError(
            code="invalid_structured_output",
            detail="Responses did not return a valid product draft.",
            status_code=status.HTTP_502_BAD_GATEWAY,
            retryable=True,
        )
    provider_status = getattr(exc, "status_code", None)
    return CatalogAICommandError(
        code="responses_failed",
        detail="Responses could not create the product draft.",
        status_code=status.HTTP_502_BAD_GATEWAY,
        retryable=bool(provider_status == 429 or (provider_status and provider_status >= 500)),
    )


def _store_blocked(
    db: Session,
    *,
    mutation: CatalogAdminMutation,
    workflow_id: str,
    principal: AuthenticatedPrincipal,
    settings: Settings,
    model: str,
    request_id: str | None,
    client_request_id: str,
    response_id: str | None,
    usage: dict[str, int],
    moderation: dict[str, Any],
    duration_ms: int,
) -> CatalogAICommandResult:
    prefix = _event_prefix(mutation.idempotency_key)
    result = CatalogAICommandResult(
        status="blocked",
        message="Moderation stopped this draft before it was saved.",
        retryable=False,
        replayed=False,
    )
    append_workflow_event(
        db,
        workflow_id=workflow_id,
        principal=principal,
        settings=settings,
        commit=False,
        event=WorkflowEventInput(
            client_event_id=f"{prefix}-moderation",
            stage="moderation",
            capability="moderation",
            status="blocked",
            business_summary=result.message,
            model=settings.catalog_studio_moderation_model,
            request_id=request_id,
            duration_ms=duration_ms,
            moderation=moderation,
            request_payload={
                "client_request_id": client_request_id,
                "input": {"action": "moderate_draft"},
            },
            response_payload={
                "status": "blocked",
                "moderation": moderation,
                "response_id": response_id,
                "provider_request_id": request_id,
            },
        ),
    )
    append_workflow_event(
        db,
        workflow_id=workflow_id,
        principal=principal,
        settings=settings,
        commit=False,
        event=WorkflowEventInput(
            client_event_id=f"{prefix}-responses",
            stage="draft",
            capability="responses",
            status="blocked",
            business_summary="No product draft was saved.",
            model=model,
            request_id=request_id,
            duration_ms=duration_ms,
            usage=usage,
            moderation=moderation,
            request_payload={
                "client_request_id": client_request_id,
                "input": {"action": "create_or_refine_draft"},
            },
            response_payload={
                "status": "blocked",
                "response_id": response_id,
                "provider_request_id": request_id,
            },
        ),
    )
    mutation.response_json = {
        "state": "completed",
        "result": result.model_dump(mode="json"),
    }
    db.commit()
    return result


def _store_success(
    db: Session,
    *,
    mutation: CatalogAdminMutation,
    workflow_id: str,
    command: CatalogAICommandRequest,
    principal: AuthenticatedPrincipal,
    settings: Settings,
    proposal: CatalogAIProductProposal,
    model: str,
    request_id: str | None,
    client_request_id: str,
    response_id: str | None,
    usage: dict[str, int],
    moderation: dict[str, Any],
    duration_ms: int,
) -> CatalogAICommandResult:
    workflow = _owned_workflow(db, workflow_id=workflow_id, principal=principal, lock=True)
    current_revision, current_version = _validate_command_state(
        db, workflow=workflow, command=command, principal=principal
    )
    seed_run_id, store_id, current = _server_catalog_context(db, current_revision)
    current_product_id = (
        current_revision.catalog_product_id if current_revision is not None else None
    )
    product = _product_from_proposal(
        db,
        proposal,
        seed_run_id=seed_run_id,
        store_id=store_id,
        product_id=current_product_id,
        current=current,
    )
    if current_revision is None and (
        db.get(CatalogProduct, product.product_id) is not None
        or db.scalar(
            select(CatalogDraftRevision.id).where(
                CatalogDraftRevision.catalog_product_id == product.product_id
            )
        )
        is not None
    ):
        raise _conflict(
            "A catalog product already uses the generated brand, title, and category."
        )
    draft_version = current_version + 1
    revision = CatalogDraftRevision(
        id=f"draft_{uuid4().hex[:24]}",
        catalog_product_id=str(product.product_id),
        base_version=current_revision.base_version if current_revision else 0,
        status="draft",
        moderation_state="approved",
        snapshot_json=product.model_dump(mode="json"),
        created_by=principal.provider_user_id,
    )
    db.add(revision)
    db.flush()
    result = CatalogAICommandResult(
        status="succeeded",
        message=(
            "The product draft was refined and is ready for review."
            if current_revision
            else "The product draft is ready for review."
        ),
        retryable=False,
        replayed=False,
        draft=CatalogAIDraftResult(
            id=revision.id,
            product_id=revision.catalog_product_id,
            draft_version=draft_version,
            base_version=revision.base_version,
            moderation_state="approved",
            image_direction=proposal.image_direction,
            product=product,
        ),
    )
    prefix = _event_prefix(mutation.idempotency_key)
    append_workflow_event(
        db,
        workflow_id=workflow_id,
        principal=principal,
        settings=settings,
        commit=False,
        event=WorkflowEventInput(
            client_event_id=f"{prefix}-moderation",
            stage="moderation",
            capability="moderation",
            status="succeeded",
            business_summary="Presenter input and generated copy passed moderation.",
            model=settings.catalog_studio_moderation_model,
            request_id=request_id,
            duration_ms=duration_ms,
            moderation=moderation,
            draft_id=revision.id,
            request_payload={"input": {"action": "moderate_draft"}},
            response_payload={"status": "approved", "moderation": moderation},
        ),
    )
    append_workflow_event(
        db,
        workflow_id=workflow_id,
        principal=principal,
        settings=settings,
        commit=False,
        event=WorkflowEventInput(
            client_event_id=f"{prefix}-responses",
            stage="draft",
            capability="responses",
            status="succeeded",
            business_summary=result.message,
            model=model,
            request_id=request_id,
            duration_ms=duration_ms,
            usage=usage,
            moderation=moderation,
            draft_id=revision.id,
            request_payload={
                "client_request_id": client_request_id,
                "input": {
                    "action": "refine_draft" if current_revision else "create_draft",
                    "draft_id": command.current_draft_id,
                }
            },
            response_payload={
                "status": "ready",
                "response_id": response_id,
                "provider_request_id": request_id,
                "draft_id": revision.id,
                "draft_version": draft_version,
                "product": product.model_dump(mode="json"),
                "image_direction": proposal.image_direction,
            },
        ),
    )
    mutation.response_json = {
        "state": "completed",
        "result": result.model_dump(mode="json"),
    }
    db.commit()
    return result


def execute_catalog_ai_command(
    db: Session,
    *,
    workflow_id: str,
    command: CatalogAICommandRequest,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
    settings: Settings,
    client: Any | None = None,
) -> CatalogAICommandResult:
    raw_idempotency_key = idempotency_key.strip()
    if not raw_idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank.")
    existing = db.get(
        CatalogAdminMutation,
        _mutation_key(
            workflow_id=workflow_id,
            idempotency_key=raw_idempotency_key,
            principal=principal,
        ),
    )
    if existing is not None:
        return _replay_existing_mutation(
            existing,
            operation=f"catalog.ai:{workflow_id}",
            request_hash=_command_hash(workflow_id, command),
        )
    workflow = _owned_workflow(db, workflow_id=workflow_id, principal=principal, lock=False)
    current_revision, _ = _validate_command_state(
        db, workflow=workflow, command=command, principal=principal
    )
    mutation, replay = _reserve_command(
        db,
        workflow_id=workflow_id,
        command=command,
        idempotency_key=raw_idempotency_key,
        principal=principal,
    )
    if replay is not None:
        return replay

    started = time.monotonic()
    client_request_id = new_openai_client_request_id("responses")
    try:
        _, _, current_product = _server_catalog_context(db, current_revision)
        references = _catalog_reference_context(db)
        provider = _resolve_client(settings, client)
        with genai_llm_span(
            "catalog_studio_draft",
            model=settings.catalog_studio_responses_model,
            provider="openai",
            attributes={
                "app.catalog_studio.workflow_id": workflow_id,
                "app.catalog_studio.action": (
                    "refine_draft" if current_revision else "create_draft"
                ),
                "app.catalog_studio.instruction_length": len(command.instruction),
            },
        ) as span:
            response = provider.responses.parse(
                model=settings.catalog_studio_responses_model,
                instructions=CATALOG_AI_INSTRUCTIONS,
                input=_provider_input(command, current_product, references),
                text_format=CatalogAIProductProposal,
                moderation={"model": settings.catalog_studio_moderation_model},
                max_output_tokens=settings.catalog_studio_responses_max_output_tokens,
                safety_identifier=_safety_identifier(principal),
                store=False,
                timeout=settings.catalog_studio_responses_timeout_seconds,
                extra_headers={"X-Client-Request-Id": client_request_id},
            )
            response_id, provider_request_id = openai_request_ids(response)
            set_span_attributes(
                span,
                {
                    "gen_ai.response.id": response_id,
                    "gen_ai.response.request_id": provider_request_id,
                    "gen_ai.request.client_id": client_request_id,
                    "gen_ai.response.model": getattr(response, "model", None),
                },
            )
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        if getattr(response, "status", None) != "completed":
            raise CatalogAICommandError(
                code="invalid_structured_output",
                detail="Responses did not complete a valid product draft.",
                status_code=status.HTTP_502_BAD_GATEWAY,
                retryable=True,
            )
        input_moderation = _moderation_result(
            getattr(getattr(response, "moderation", None), "input", None),
            label="input",
        )
        output_moderation = _moderation_result(
            getattr(getattr(response, "moderation", None), "output", None),
            label="output",
        )
        moderation = _combined_moderation(input_moderation, output_moderation)
        response_model = str(
            getattr(response, "model", None) or settings.catalog_studio_responses_model
        )
        request_id = provider_request_id or response_id
        usage = _usage(response)
        if moderation["flagged"]:
            return _store_blocked(
                db,
                mutation=mutation,
                workflow_id=workflow_id,
                principal=principal,
                settings=settings,
                model=response_model,
                request_id=request_id,
                client_request_id=client_request_id,
                response_id=response_id,
                usage=usage,
                moderation=moderation,
                duration_ms=duration_ms,
            )
        proposal = getattr(response, "output_parsed", None)
        if not isinstance(proposal, CatalogAIProductProposal):
            raise CatalogAICommandError(
                code="invalid_structured_output",
                detail="Responses did not return a valid product draft.",
                status_code=status.HTTP_502_BAD_GATEWAY,
                retryable=True,
            )
        return _store_success(
            db,
            mutation=mutation,
            workflow_id=workflow_id,
            command=command,
            principal=principal,
            settings=settings,
            proposal=proposal,
            model=response_model,
            request_id=request_id,
            client_request_id=client_request_id,
            response_id=response_id,
            usage=usage,
            moderation=moderation,
            duration_ms=duration_ms,
        )
    except CatalogAICommandError as exc:
        raise _record_failure(
            db,
            mutation=mutation,
            workflow_id=workflow_id,
            principal=principal,
            settings=settings,
            error=exc,
            client_request_id=client_request_id,
        )
    except HTTPException as exc:
        error = CatalogAICommandError(
            code="draft_state_conflict",
            detail=str(exc.detail),
            status_code=exc.status_code,
            retryable=False,
        )
        raise _record_failure(
            db,
            mutation=mutation,
            workflow_id=workflow_id,
            principal=principal,
            settings=settings,
            error=error,
            client_request_id=client_request_id,
        ) from exc
    except Exception as exc:
        raise _record_failure(
            db,
            mutation=mutation,
            workflow_id=workflow_id,
            principal=principal,
            settings=settings,
            error=_provider_failure(exc),
            client_request_id=client_request_id,
        ) from exc


def _suggestion_command_hash(
    product_id: str, command: CatalogAISuggestionCommandRequest
) -> str:
    payload = json.dumps(
        {"product_id": product_id, **command.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _suggestion_mutation_key(
    *,
    product_id: str,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
) -> str:
    source = (
        f"{principal.provider}:{principal.provider_user_id}:"
        f"{product_id}:{idempotency_key}"
    )
    return f"ai_suggestions_{hashlib.sha256(source.encode()).hexdigest()}"


def _replay_suggestion_mutation(
    mutation: CatalogAdminMutation,
    *,
    operation: str,
    request_hash: str,
) -> CatalogAISuggestionCommandResult:
    if mutation.operation != operation or mutation.request_hash != request_hash:
        raise _conflict(
            "Idempotency-Key was already used for a different Catalog Studio suggestion command."
        )
    payload = dict(mutation.response_json or {})
    state = payload.get("state")
    if state == "completed":
        result = CatalogAISuggestionCommandResult.model_validate(payload.get("result") or {})
        return result.model_copy(update={"replayed": True})
    if state == "failed":
        raise CatalogAICommandError(
            code=str(payload.get("code") or "catalog_ai_failed"),
            detail=str(payload.get("detail") or "The suggestion command failed."),
            status_code=int(payload.get("status_code") or 502),
            retryable=bool(payload.get("retryable")),
        )
    raise _conflict("This Catalog Studio suggestion command is already processing.")


def _reserve_suggestion_command(
    db: Session,
    *,
    product_id: str,
    command: CatalogAISuggestionCommandRequest,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
) -> tuple[CatalogAdminMutation, CatalogAISuggestionCommandResult | None]:
    key = idempotency_key.strip()
    if not key:
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank.")
    mutation_key = _suggestion_mutation_key(
        product_id=product_id,
        idempotency_key=key,
        principal=principal,
    )
    operation = f"catalog.ai.suggestions:{product_id}"
    request_hash = _suggestion_command_hash(product_id, command)
    existing = db.get(CatalogAdminMutation, mutation_key)
    if existing is not None:
        return existing, _replay_suggestion_mutation(
            existing,
            operation=operation,
            request_hash=request_hash,
        )
    mutation = CatalogAdminMutation(
        idempotency_key=mutation_key,
        operation=operation,
        request_hash=request_hash,
        response_json={"state": "processing"},
        created_by=principal.provider_user_id,
    )
    db.add(mutation)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        concurrent = db.get(CatalogAdminMutation, mutation_key)
        if concurrent is None:
            raise _conflict("Catalog Studio suggestion state changed; retry.")
        return concurrent, _replay_suggestion_mutation(
            concurrent,
            operation=operation,
            request_hash=request_hash,
        )
    return mutation, None


def _owned_suggestion_context(
    db: Session,
    *,
    product_id: str,
    command: CatalogAISuggestionCommandRequest,
    principal: AuthenticatedPrincipal,
) -> tuple[CatalogDraftRevision, ProductDraftV3]:
    workflow = _owned_workflow(
        db,
        workflow_id=command.workflow_id,
        principal=principal,
        lock=False,
    )
    revision = _latest_owned_draft(db, product_id, principal)
    if revision is None or revision.id != command.draft_id:
        raise _conflict("The requested catalog draft is no longer current.")
    actual_version = draft_revision_version(db, revision)
    if actual_version != command.expected_draft_version:
        raise _conflict(
            f"Expected catalog draft version {command.expected_draft_version}, "
            f"but current version is {actual_version}."
        )
    if workflow.draft_revision_id not in {None, revision.id}:
        raise _conflict("The catalog workflow is linked to another draft.")
    if workflow.published_product_id not in {None, product_id}:
        raise _conflict("The catalog workflow is linked to another product.")
    product = product_draft_v3_from_snapshot(revision.snapshot_json)
    for target_path in command.target_paths:
        validate_suggestion_target_path(target_path)
        if (
            command.input_origin == "supplier_analysis"
            and target_path in _SUPPLIER_UNSUPPORTED_PATHS
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Supplier image analysis cannot propose pricing or product links; "
                    "enter those fields manually."
                ),
            )
    return revision, product


def _owned_source_assets(
    db: Session,
    *,
    source_asset_ids: list[str],
    product: ProductDraftV3,
    principal: AuthenticatedPrincipal,
) -> list[CatalogSourceAsset]:
    if not source_asset_ids:
        return []
    referenced_ids = {
        asset_id
        for reference in product.source_references
        for asset_id in reference.asset_ids
    }
    if not set(source_asset_ids).issubset(referenced_ids):
        raise HTTPException(
            status_code=422,
            detail="AI source assets must be attached to the current catalog draft.",
        )
    rows = db.scalars(
        select(CatalogSourceAsset)
        .join(CatalogSourceBundle, CatalogSourceAsset.bundle_id == CatalogSourceBundle.id)
        .where(
            CatalogSourceAsset.id.in_(source_asset_ids),
            CatalogSourceBundle.owner_provider == principal.provider,
            CatalogSourceBundle.owner_provider_user_id == principal.provider_user_id,
        )
    ).all()
    by_id = {row.id: row for row in rows}
    if set(by_id) != set(source_asset_ids):
        raise HTTPException(status_code=404, detail="Catalog source asset not found.")
    return [by_id[asset_id] for asset_id in source_asset_ids]


_SOURCE_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MAX_SOURCE_TEXT_EXCERPT_CHARS = 4000


def _source_asset_kind(asset: CatalogSourceAsset) -> str:
    return "image" if asset.content_type in _SOURCE_IMAGE_CONTENT_TYPES else "document"


def _source_asset_preview_bytes(settings: Settings, asset: CatalogSourceAsset) -> bytes:
    base = Path(str(settings.catalog_source_output_dir or "")).expanduser().resolve()
    candidate = (base / asset.preview_storage_key).resolve()
    if not candidate.is_relative_to(base):
        raise CatalogAICommandError(
            code="source_asset_unavailable",
            detail="A selected supplier source asset is unavailable.",
            status_code=status.HTTP_409_CONFLICT,
            retryable=False,
        )
    try:
        return candidate.read_bytes()
    except OSError as exc:
        raise CatalogAICommandError(
            code="source_asset_unavailable",
            detail="A selected supplier source asset is unavailable.",
            status_code=status.HTTP_409_CONFLICT,
            retryable=False,
        ) from exc


def _source_asset_text_excerpt(settings: Settings, asset: CatalogSourceAsset) -> str:
    content = _source_asset_preview_bytes(settings, asset)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CatalogAICommandError(
            code="source_asset_unavailable",
            detail="A selected supplier source document is unavailable.",
            status_code=status.HTTP_409_CONFLICT,
            retryable=False,
        ) from exc
    return text.strip()[:_MAX_SOURCE_TEXT_EXCERPT_CHARS]


def _suggestion_current_draft_payload(product: ProductDraftV3) -> dict[str, Any]:
    """Return approved authoring facts without storage or private metadata."""

    return {
        "title": product.title,
        "description": product.description,
        "brand_id": product.brand_id,
        "brand": product.brand,
        "category": product.category,
        "color": product.color,
        "material": product.material,
        "gender": product.gender,
        "season": product.season,
        "benefits": product.benefits,
        "specifications": [item.model_dump(mode="json") for item in product.specifications],
        "care_instructions": product.care_instructions,
        "content_details": product.content_details,
        "seo": product.seo.model_dump(mode="json"),
        "media": [
            {
                "media_id": item.media_id,
                "role": item.role,
                "alt_text": item.alt_text,
            }
            for item in product.media
        ],
    }


def _suggestion_provider_input(
    *,
    command: CatalogAISuggestionCommandRequest,
    product: ProductDraftV3,
    source_assets: list[CatalogSourceAsset],
    settings: Settings,
) -> list[dict[str, Any]]:
    source_manifest = []
    for asset in source_assets:
        manifest_item = {
            "asset_id": asset.id,
            "asset_kind": _source_asset_kind(asset),
            "content_type": asset.content_type,
            "width": asset.width,
            "height": asset.height,
            "checksum_sha256": asset.checksum_sha256,
        }
        if asset.content_type not in _SOURCE_IMAGE_CONTENT_TYPES:
            manifest_item["text_excerpt"] = _source_asset_text_excerpt(settings, asset)
        source_manifest.append(manifest_item)
    payload = {
        "presenter_instruction": command.instruction,
        "input_origin": command.input_origin,
        "allowed_target_paths": command.target_paths,
        "current_draft": _suggestion_current_draft_payload(product),
        "source_assets": source_manifest,
    }
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": json.dumps(payload, sort_keys=True, separators=(",", ":")),
        }
    ]
    for asset in source_assets:
        if asset.content_type not in _SOURCE_IMAGE_CONTENT_TYPES:
            continue
        encoded = base64.b64encode(_source_asset_preview_bytes(settings, asset)).decode(
            "ascii"
        )
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{encoded}",
                "detail": "high",
            }
        )
    return [{"role": "user", "content": content}]


def _validated_suggestion_proposal(
    proposal: CatalogAISuggestionProposal,
    *,
    command: CatalogAISuggestionCommandRequest,
) -> CatalogAISuggestionProposal:
    allowed_paths = set(command.target_paths)
    source_ids = set(command.source_asset_ids)
    for item in proposal.suggestions:
        validate_suggestion_target_path(item.target_path)
        if item.target_path not in allowed_paths:
            raise CatalogAICommandError(
                code="invalid_structured_output",
                detail="Responses proposed a field outside the requested target scope.",
                status_code=status.HTTP_502_BAD_GATEWAY,
                retryable=True,
            )
        if not set(item.evidence_asset_ids).issubset(source_ids):
            raise CatalogAICommandError(
                code="invalid_structured_output",
                detail="Responses cited source evidence outside the authorized source set.",
                status_code=status.HTTP_502_BAD_GATEWAY,
                retryable=True,
            )
        if (
            command.input_origin == "supplier_analysis"
            and item.target_path in _SUPPLIER_OBSERVED_PATHS
            and item.certainty_class != "observed"
        ):
            raise CatalogAICommandError(
                code="invalid_structured_output",
                detail="Responses returned an objective supplier fact without observed evidence.",
                status_code=status.HTTP_502_BAD_GATEWAY,
                retryable=True,
            )
    for item in proposal.unknown_fields:
        if item.target_path not in allowed_paths:
            raise CatalogAICommandError(
                code="invalid_structured_output",
                detail="Responses returned an unknown field outside the requested target scope.",
                status_code=status.HTTP_502_BAD_GATEWAY,
                retryable=True,
            )
    return proposal


def _store_suggestion_failure(
    db: Session,
    *,
    mutation: CatalogAdminMutation,
    command: CatalogAISuggestionCommandRequest,
    principal: AuthenticatedPrincipal,
    settings: Settings,
    error: CatalogAICommandError,
    client_request_id: str | None = None,
) -> None:
    mutation.response_json = {
        "state": "failed",
        "code": error.code,
        "detail": error.detail,
        "status_code": error.status_code,
        "retryable": error.retryable,
    }
    append_workflow_event(
        db,
        workflow_id=command.workflow_id,
        principal=principal,
        settings=settings,
        commit=False,
        event=WorkflowEventInput(
            client_event_id=f"{_event_prefix(mutation.idempotency_key)}-suggestions-failed",
            stage="suggestions",
            capability="responses",
            status="failed",
            business_summary=error.detail,
            error_code=error.code,
            retryable=error.retryable,
            request_payload={
                "client_request_id": client_request_id,
                "input": {
                    "action": "generate_product_suggestions",
                    "draft_id": command.draft_id,
                    "expected_draft_version": command.expected_draft_version,
                    "source_asset_count": len(command.source_asset_ids),
                    "target_paths": command.target_paths,
                }
            },
            response_payload={"status": "failed", "error_code": error.code},
        ),
    )
    db.commit()


def _record_suggestion_failure(
    db: Session,
    *,
    mutation: CatalogAdminMutation,
    command: CatalogAISuggestionCommandRequest,
    principal: AuthenticatedPrincipal,
    settings: Settings,
    error: CatalogAICommandError,
    client_request_id: str | None = None,
) -> CatalogAICommandError:
    mutation_key = mutation.idempotency_key
    db.rollback()
    persisted = db.get(CatalogAdminMutation, mutation_key) or mutation
    _store_suggestion_failure(
        db,
        mutation=persisted,
        command=command,
        principal=principal,
        settings=settings,
        error=error,
        client_request_id=client_request_id,
    )
    return error


def _suggestion_provider_failure(exc: Exception) -> CatalogAICommandError:
    error = _provider_failure(exc)
    detail_by_code = {
        "responses_timeout": "Responses timed out before the suggestions were ready.",
        "responses_unavailable": "Responses is temporarily unavailable for suggestions.",
        "invalid_structured_output": "Responses did not return valid product suggestions.",
        "responses_failed": "Responses could not create product suggestions.",
    }
    return CatalogAICommandError(
        code=error.code,
        detail=detail_by_code.get(error.code, error.detail),
        status_code=error.status_code,
        retryable=error.retryable,
    )


def execute_catalog_ai_suggestion_command(
    db: Session,
    *,
    product_id: str,
    command: CatalogAISuggestionCommandRequest,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
    settings: Settings,
    client: Any | None = None,
) -> CatalogAISuggestionCommandResult:
    mutation_key = _suggestion_mutation_key(
        product_id=product_id,
        idempotency_key=idempotency_key.strip(),
        principal=principal,
    )
    existing = db.get(CatalogAdminMutation, mutation_key)
    if existing is not None:
        return _replay_suggestion_mutation(
            existing,
            operation=f"catalog.ai.suggestions:{product_id}",
            request_hash=_suggestion_command_hash(product_id, command),
        )
    revision, product = _owned_suggestion_context(
        db,
        product_id=product_id,
        command=command,
        principal=principal,
    )
    source_assets = _owned_source_assets(
        db,
        source_asset_ids=command.source_asset_ids,
        product=product,
        principal=principal,
    )
    mutation, replay = _reserve_suggestion_command(
        db,
        product_id=product_id,
        command=command,
        idempotency_key=idempotency_key,
        principal=principal,
    )
    if replay is not None:
        return replay

    started = time.monotonic()
    client_request_id = new_openai_client_request_id("responses")
    try:
        provider = _resolve_client(settings, client)
        with genai_llm_span(
            "catalog_studio_suggestions",
            model=settings.catalog_studio_responses_model,
            provider="openai",
            attributes={
                "app.catalog_studio.workflow_id": command.workflow_id,
                "app.catalog_studio.product_id": product_id,
                "app.catalog_studio.action": command.input_origin,
                "app.catalog_studio.source_asset_count": len(source_assets),
            },
        ) as span:
            response = provider.responses.parse(
                model=settings.catalog_studio_responses_model,
                instructions=CATALOG_SUGGESTION_INSTRUCTIONS,
                input=_suggestion_provider_input(
                    command=command,
                    product=product,
                    source_assets=source_assets,
                    settings=settings,
                ),
                text_format=CatalogAISuggestionProposal,
                moderation={"model": settings.catalog_studio_moderation_model},
                max_output_tokens=settings.catalog_studio_responses_max_output_tokens,
                safety_identifier=_safety_identifier(principal),
                store=False,
                timeout=settings.catalog_studio_responses_timeout_seconds,
                extra_headers={"X-Client-Request-Id": client_request_id},
            )
            response_id, provider_request_id = openai_request_ids(response)
            set_span_attributes(
                span,
                {
                    "gen_ai.response.id": response_id,
                    "gen_ai.response.request_id": provider_request_id,
                    "gen_ai.request.client_id": client_request_id,
                    "gen_ai.response.model": getattr(response, "model", None),
                },
            )
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        if getattr(response, "status", None) != "completed":
            raise CatalogAICommandError(
                code="invalid_structured_output",
                detail="Responses did not complete a valid suggestion proposal.",
                status_code=status.HTTP_502_BAD_GATEWAY,
                retryable=True,
            )
        moderation = _combined_moderation(
            _moderation_result(
                getattr(getattr(response, "moderation", None), "input", None),
                label="input",
            ),
            _moderation_result(
                getattr(getattr(response, "moderation", None), "output", None),
                label="output",
            ),
        )
        response_model = str(
            getattr(response, "model", None) or settings.catalog_studio_responses_model
        )
        request_id = provider_request_id or response_id
        usage = _usage(response)
        if moderation["flagged"]:
            result = CatalogAISuggestionCommandResult(
                status="blocked",
                message="Moderation stopped these suggestions before they were saved.",
            )
            append_workflow_event(
                db,
                workflow_id=command.workflow_id,
                principal=principal,
                settings=settings,
                commit=False,
                event=WorkflowEventInput(
                    client_event_id=f"{_event_prefix(mutation.idempotency_key)}-suggestions-blocked",
                    stage="suggestions",
                    capability="responses",
                    status="blocked",
                    business_summary=result.message,
                    model=response_model,
                    request_id=request_id,
                    duration_ms=duration_ms,
                    usage=usage,
                    moderation=moderation,
                    request_payload={
                        "client_request_id": client_request_id,
                        "input": {"action": "generate_product_suggestions"},
                    },
                    response_payload={
                        "status": "blocked",
                        "response_id": response_id,
                        "provider_request_id": request_id,
                    },
                ),
            )
            mutation.response_json = {
                "state": "completed",
                "result": result.model_dump(mode="json"),
            }
            db.commit()
            return result

        proposal = getattr(response, "output_parsed", None)
        if not isinstance(proposal, CatalogAISuggestionProposal):
            raise CatalogAICommandError(
                code="responses_refused",
                detail="Responses did not return a reviewable suggestion proposal.",
                status_code=status.HTTP_502_BAD_GATEWAY,
                retryable=True,
            )
        proposal = _validated_suggestion_proposal(proposal, command=command)
        suggestion_set = None
        if proposal.suggestions:
            create_request = CatalogSuggestionSetCreateRequest(
                draft_id=revision.id,
                expected_draft_version=command.expected_draft_version,
                workflow_id=command.workflow_id,
                suggestions=[
                    CatalogFieldSuggestionCreate(
                        target_path=item.target_path,
                        proposed_value=item.proposed_value,
                        evidence_asset_ids=item.evidence_asset_ids,
                        certainty_class=item.certainty_class,
                        input_origin=command.input_origin,
                    )
                    for item in proposal.suggestions
                ],
            )
            suggestion_set, _ = create_suggestion_set(
                db,
                product_id=product_id,
                request=create_request,
                idempotency_key=f"{mutation.idempotency_key}:set",
                principal=principal,
            )
        result = CatalogAISuggestionCommandResult(
            status="succeeded",
            message=(
                "Review the generated product suggestions before applying them."
                if suggestion_set
                else "No supported facts were proposed; review the follow-up questions."
            ),
            suggestion_set=suggestion_set,
            follow_up_questions=proposal.unknown_fields,
        )
        append_workflow_event(
            db,
            workflow_id=command.workflow_id,
            principal=principal,
            settings=settings,
            commit=False,
            event=WorkflowEventInput(
                client_event_id=f"{_event_prefix(mutation.idempotency_key)}-suggestions",
                stage="suggestions",
                capability="responses",
                status="succeeded",
                business_summary=result.message,
                model=response_model,
                request_id=request_id,
                duration_ms=duration_ms,
                usage=usage,
                moderation=moderation,
                request_payload={
                    "client_request_id": client_request_id,
                    "input": {
                        "action": "generate_product_suggestions",
                        "draft_id": command.draft_id,
                        "expected_draft_version": command.expected_draft_version,
                        "input_origin": command.input_origin,
                        "source_asset_ids": command.source_asset_ids,
                        "target_paths": command.target_paths,
                    }
                },
                response_payload={
                    "status": "succeeded",
                    "response_id": response_id,
                    "provider_request_id": request_id,
                    "suggestion_set_id": suggestion_set.id if suggestion_set else None,
                    "suggestion_count": len(proposal.suggestions),
                    "unknown_count": len(proposal.unknown_fields),
                },
            ),
        )
        mutation.response_json = {
            "state": "completed",
            "result": result.model_dump(mode="json"),
        }
        db.commit()
        return result
    except CatalogAICommandError as exc:
        raise _record_suggestion_failure(
            db,
            mutation=mutation,
            command=command,
            principal=principal,
            settings=settings,
            error=exc,
            client_request_id=client_request_id,
        )
    except HTTPException as exc:
        provider_output_invalid = exc.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        error = CatalogAICommandError(
            code=(
                "invalid_structured_output"
                if provider_output_invalid
                else "suggestion_state_conflict"
            ),
            detail=str(exc.detail),
            status_code=(
                status.HTTP_502_BAD_GATEWAY
                if provider_output_invalid
                else exc.status_code
            ),
            retryable=provider_output_invalid,
        )
        raise _record_suggestion_failure(
            db,
            mutation=mutation,
            command=command,
            principal=principal,
            settings=settings,
            error=error,
            client_request_id=client_request_id,
        ) from exc
    except Exception as exc:
        raise _record_suggestion_failure(
            db,
            mutation=mutation,
            command=command,
            principal=principal,
            settings=settings,
            error=_suggestion_provider_failure(exc),
            client_request_id=client_request_id,
        ) from exc
