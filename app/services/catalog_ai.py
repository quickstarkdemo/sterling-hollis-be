from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import time
from typing import Any, Mapping
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog.admin_schemas import InventoryDraft, ProductDraft, VariantDraft
from app.catalog.ai_schemas import (
    CatalogAICommandRequest,
    CatalogAICommandResult,
    CatalogAIDraftResult,
    CatalogAIProductProposal,
)
from app.catalog.workflow_schemas import WorkflowEventInput
from app.config import Settings
from app.models import (
    CatalogAdminMutation,
    CatalogDraftRevision,
    CatalogProduct,
    CatalogWorkflow,
    Store,
    SyntheticRun,
)
from app.observability.genai_otel import genai_llm_span, set_span_attributes
from app.services.auth.clerk import AuthenticatedPrincipal
from app.services.catalog_normalization import catalog_key_for_values, catalog_product_id_for_key
from app.services.catalog_workflow import append_workflow_event, normalize_usage


CATALOG_AI_INSTRUCTIONS = """You create private Sterling Hollis retail catalog drafts.
Return only the requested structured product proposal. Preserve the current draft unless the
presenter explicitly asks for a change. Use the provided category IDs, realistic luxury-retail
prices, concise customer-safe copy, and useful image art direction. Do not include people,
customer identity, secrets, URLs, medical claims, sexual content, hateful content, instructions
for wrongdoing, or private reasoning. If the instruction is unrelated to a retail product, make
the smallest reasonable catalog proposal consistent with the current draft."""


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


def _draft_version(db: Session, revision: CatalogDraftRevision) -> int:
    return int(
        db.scalar(
            select(func.count(CatalogDraftRevision.id)).where(
                CatalogDraftRevision.catalog_product_id == revision.catalog_product_id,
                CatalogDraftRevision.created_by == revision.created_by,
            )
        )
        or 0
    )


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
    actual_version = _draft_version(db, revision)
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
) -> tuple[str, str, ProductDraft | None]:
    if revision is not None:
        current = ProductDraft.model_validate(revision.snapshot_json)
        store_id = current.variants[0].inventory[0].store_id
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
    command: CatalogAICommandRequest, current: ProductDraft | None
) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"presenter_instruction": command.instruction}
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


def _product_from_proposal(
    proposal: CatalogAIProductProposal,
    *,
    seed_run_id: str,
    store_id: str,
    product_id: str | None,
) -> ProductDraft:
    if product_id is None:
        key = catalog_key_for_values(
            brand=proposal.brand,
            title=proposal.title,
            category=proposal.category,
        )
        product_id = catalog_product_id_for_key(key)
    return ProductDraft(
        product_id=product_id,
        seed_run_id=seed_run_id,
        title=proposal.title,
        description=proposal.description,
        brand=proposal.brand,
        category=proposal.category,
        metadata={
            "source": "catalog_studio_responses",
            "image_direction": proposal.image_direction,
        },
        variants=[
            VariantDraft(
                color=variant.color,
                material=variant.material,
                gender=variant.gender,
                season=variant.season,
                price_min=Decimal(str(variant.price_min)),
                price_max=Decimal(str(variant.price_max)),
                link=None,
                image_link=None,
                image_set={},
                metadata={},
                inventory=[
                    InventoryDraft(
                        store_id=store_id,
                        size=inventory.size,
                        availability=inventory.availability,
                        inventory_qty=inventory.inventory_qty,
                        objective_weight=Decimal(str(inventory.objective_weight)),
                        metadata={},
                    )
                    for inventory in variant.inventory
                ],
            )
            for variant in proposal.variants
        ],
    )


def _store_failure(
    db: Session,
    *,
    mutation: CatalogAdminMutation,
    workflow_id: str,
    principal: AuthenticatedPrincipal,
    settings: Settings,
    error: CatalogAICommandError,
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
            request_payload={"input": {"action": "create_or_refine_draft"}},
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
            request_payload={"input": {"action": "moderate_draft"}},
            response_payload={"status": "blocked", "moderation": moderation},
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
            request_payload={"input": {"action": "create_or_refine_draft"}},
            response_payload={"status": "blocked"},
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
    usage: dict[str, int],
    moderation: dict[str, Any],
    duration_ms: int,
) -> CatalogAICommandResult:
    workflow = _owned_workflow(db, workflow_id=workflow_id, principal=principal, lock=True)
    current_revision, current_version = _validate_command_state(
        db, workflow=workflow, command=command, principal=principal
    )
    seed_run_id, store_id, _ = _server_catalog_context(db, current_revision)
    current_product_id = (
        current_revision.catalog_product_id if current_revision is not None else None
    )
    product = _product_from_proposal(
        proposal,
        seed_run_id=seed_run_id,
        store_id=store_id,
        product_id=current_product_id,
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
                "input": {
                    "action": "refine_draft" if current_revision else "create_draft",
                    "draft_id": command.current_draft_id,
                }
            },
            response_payload={
                "status": "ready",
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
    try:
        _, _, current_product = _server_catalog_context(db, current_revision)
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
                input=_provider_input(command, current_product),
                text_format=CatalogAIProductProposal,
                moderation={"model": settings.catalog_studio_moderation_model},
                max_output_tokens=settings.catalog_studio_responses_max_output_tokens,
                safety_identifier=_safety_identifier(principal),
                store=False,
                timeout=settings.catalog_studio_responses_timeout_seconds,
            )
            set_span_attributes(
                span,
                {
                    "gen_ai.response.id": getattr(response, "id", None),
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
        request_id = getattr(response, "id", None)
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
        ) from exc
    except Exception as exc:
        raise _record_failure(
            db,
            mutation=mutation,
            workflow_id=workflow_id,
            principal=principal,
            settings=settings,
            error=_provider_failure(exc),
        ) from exc
