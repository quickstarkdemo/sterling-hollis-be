from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Callable, Literal
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog.admin_schemas import (
    AdminDraftSnapshot,
    AdminProductListItem,
    AdminProductListResponse,
    AdminProductResponse,
    DraftMutationRequest,
    DraftRevisionResponse,
    LifecycleMutationResponse,
    ProductDraft,
    StartRevisionRequest,
)
from app.catalog.authoring import (
    authoring_metadata,
    persisted_product_metadata,
    public_product_metadata,
)
from app.models import (
    CatalogAdminMutation,
    CatalogDraftRevision,
    CatalogProduct,
    CatalogWorkflow,
    ProductVariant,
    Store,
    StoreInventory,
    SyntheticRun,
)
from app.services.auth.clerk import AuthenticatedPrincipal
from app.services.catalog_normalization import (
    catalog_key_for_values,
    catalog_product_id_for_key,
    product_variant_id_for_key,
    store_inventory_id_for_values,
    variant_key_for_values,
)
from app.services.index_jobs import enqueue_index_job


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _request_hash(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _idempotent(
    db: Session,
    *,
    key: str,
    operation: str,
    payload: dict,
    principal: AuthenticatedPrincipal,
    action: Callable[[], dict],
) -> tuple[dict, bool]:
    key = key.strip()
    if not key:
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank.")
    fingerprint = _request_hash(payload)
    existing = db.get(CatalogAdminMutation, key)
    if existing:
        if existing.operation != operation or existing.request_hash != fingerprint:
            raise _conflict("Idempotency-Key was already used for a different catalog mutation.")
        return dict(existing.response_json), True

    response = action()
    db.add(
        CatalogAdminMutation(
            idempotency_key=key,
            operation=operation,
            request_hash=fingerprint,
            response_json=response,
            created_by=principal.provider_user_id,
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        concurrent = db.get(CatalogAdminMutation, key)
        if (
            concurrent
            and concurrent.operation == operation
            and concurrent.request_hash == fingerprint
        ):
            return dict(concurrent.response_json), True
        if concurrent:
            raise _conflict(
                "Idempotency-Key was already used for a different catalog mutation."
            ) from exc
        raise _conflict(
            "Catalog state changed while the mutation was being applied; retry with fresh state."
        ) from exc
    return response, False


def _product_id(product: ProductDraft) -> str:
    if product.product_id:
        return product.product_id
    key = catalog_key_for_values(brand=product.brand, title=product.title, category=product.category)
    return catalog_product_id_for_key(key)


def _assert_expected_version(
    db: Session, product_id: str, expected_version: int
) -> CatalogProduct | None:
    product = db.scalar(
        select(CatalogProduct).where(CatalogProduct.id == product_id).with_for_update()
    )
    actual = product.version if product else 0
    if actual != expected_version:
        raise _conflict(
            f"Expected catalog version {expected_version}, but current version is {actual}."
        )
    return product


def _draft_response(revision: CatalogDraftRevision) -> DraftRevisionResponse:
    return DraftRevisionResponse(
        id=revision.id,
        product_id=revision.catalog_product_id,
        base_version=revision.base_version,
        status=revision.status,
        moderation_state=revision.moderation_state,  # type: ignore[arg-type]
        created_by=revision.created_by,
        created_at=revision.created_at,
    )


def draft_revision_version(db: Session, revision: CatalogDraftRevision) -> int:
    """Return the owner-scoped ordinal used for optimistic draft commands."""
    return int(
        db.scalar(
            select(func.count(CatalogDraftRevision.id)).where(
                CatalogDraftRevision.catalog_product_id == revision.catalog_product_id,
                CatalogDraftRevision.created_by == revision.created_by,
            )
        )
        or 0
    )


def _latest_owned_draft(
    db: Session, product_id: str, principal: AuthenticatedPrincipal
) -> CatalogDraftRevision | None:
    return db.scalar(
        select(CatalogDraftRevision)
        .where(
            CatalogDraftRevision.catalog_product_id == product_id,
            CatalogDraftRevision.created_by == principal.provider_user_id,
            CatalogDraftRevision.status == "draft",
        )
        .order_by(
            CatalogDraftRevision.created_at.desc(), CatalogDraftRevision.id.desc()
        )
        .limit(1)
    )


def _workflow_for_draft(
    db: Session, draft_id: str, principal: AuthenticatedPrincipal
) -> CatalogWorkflow | None:
    return db.scalar(
        select(CatalogWorkflow).where(
            CatalogWorkflow.draft_revision_id == draft_id,
            CatalogWorkflow.owner_provider == principal.provider,
            CatalogWorkflow.owner_provider_user_id == principal.provider_user_id,
        )
    )


def _safe_image_value(value):
    if isinstance(value, dict):
        return {
            key: _safe_image_value(item)
            for key, item in value.items()
            if key != "file_path"
        }
    if isinstance(value, list):
        return [_safe_image_value(item) for item in value]
    return value


def _safe_product_snapshot(product: ProductDraft) -> ProductDraft:
    payload = product.model_dump(mode="json")
    for variant in payload["variants"]:
        variant["image_set"] = _safe_image_value(variant.get("image_set") or {})
    return ProductDraft.model_validate(payload)


def _restore_server_image_fields(incoming, current):
    """Restore server-only image fields omitted from a safe API round trip."""
    if isinstance(incoming, dict) and isinstance(current, dict):
        restored = dict(incoming)
        if "file_path" in current and "file_path" not in restored:
            restored["file_path"] = current["file_path"]
        for key, value in list(restored.items()):
            if key in current:
                restored[key] = _restore_server_image_fields(value, current[key])
        return restored
    if isinstance(incoming, list) and isinstance(current, list):
        return [
            _restore_server_image_fields(value, current[index])
            if index < len(current)
            else value
            for index, value in enumerate(incoming)
        ]
    return incoming


def _variant_identity(variant) -> tuple:
    if variant.variant_id:
        return ("id", variant.variant_id)
    return (
        "attributes",
        variant.color,
        variant.material,
        variant.gender,
        variant.season,
    )


def _preserve_server_image_fields(
    incoming: ProductDraft, current: ProductDraft
) -> ProductDraft:
    current_variants = {_variant_identity(row): row for row in current.variants}
    payload = incoming.model_dump(mode="json")
    for index, variant in enumerate(incoming.variants):
        previous = current_variants.get(_variant_identity(variant))
        if previous is None or not variant.image_set:
            continue
        payload["variants"][index]["image_set"] = _restore_server_image_fields(
            variant.image_set, previous.image_set
        )
    return ProductDraft.model_validate(payload)


def _published_snapshot(db: Session, row: CatalogProduct) -> ProductDraft:
    variants = db.scalars(
        select(ProductVariant)
        .where(ProductVariant.catalog_product_id == row.id)
        .order_by(ProductVariant.id.asc())
    ).all()
    inventory_by_variant: dict[str, list[StoreInventory]] = {}
    if variants:
        inventory_rows = db.scalars(
            select(StoreInventory)
            .where(StoreInventory.variant_id.in_([variant.id for variant in variants]))
            .order_by(
                StoreInventory.variant_id.asc(),
                StoreInventory.store_id.asc(),
                StoreInventory.size.asc(),
            )
        ).all()
        for inventory in inventory_rows:
            inventory_by_variant.setdefault(inventory.variant_id, []).append(inventory)
    variant_payload = []
    for variant in variants:
        variant_payload.append(
            {
                "variant_id": variant.id,
                "color": variant.color,
                "material": variant.material,
                "gender": variant.gender,
                "season": variant.season,
                "price_min": variant.price_min,
                "price_max": variant.price_max,
                "link": variant.link,
                "image_link": variant.image_link,
                "image_set": _safe_image_value(dict(variant.image_set or {})),
                "metadata": dict(variant.metadata_json or {}),
                "inventory": [
                    {
                        "store_id": item.store_id,
                        "size": item.size,
                        "availability": item.availability,
                        "inventory_qty": item.inventory_qty,
                        "objective_weight": item.objective_weight,
                        "metadata": dict(item.metadata_json or {}),
                    }
                    for item in inventory_by_variant.get(variant.id, [])
                ],
            }
        )
    stable_context = len(
        {(variant.gender, variant.season) for variant in variants}
    ) <= 1
    inferred_variant_axes = (
        [
            field
            for field in ("color", "material")
            if len({getattr(variant, field) for variant in variants}) > 1
        ]
        if stable_context
        else []
    )
    stored_authoring = authoring_metadata(row.metadata_json)
    stored_variant_axes = stored_authoring.get("variant_axes")
    variant_axes = (
        stored_variant_axes
        if isinstance(stored_variant_axes, list)
        else inferred_variant_axes
    )
    primary_variant_id = stored_authoring.get("primary_variant_id")
    primary_variant_index = next(
        (
            index
            for index, variant in enumerate(variants)
            if variant.id == primary_variant_id
        ),
        0,
    )
    return ProductDraft.model_validate(
        {
            "product_id": row.id,
            "seed_run_id": row.seed_run_id,
            "title": row.title,
            "description": row.description,
            "brand": row.brand,
            "category": row.category,
            "metadata": public_product_metadata(row.metadata_json),
            "design_specification": stored_authoring.get("design_specification"),
            "variant_axes": variant_axes,
            "primary_variant_index": primary_variant_index,
            "variants": variant_payload,
        }
    )


def _admin_draft_snapshot(
    db: Session,
    revision: CatalogDraftRevision,
    principal: AuthenticatedPrincipal,
) -> AdminDraftSnapshot:
    workflow = _workflow_for_draft(db, revision.id, principal)
    return AdminDraftSnapshot(
        revision=_draft_response(revision),
        draft_version=draft_revision_version(db, revision),
        workflow_id=workflow.id if workflow else None,
        product=_safe_product_snapshot(
            ProductDraft.model_validate(revision.snapshot_json)
        ),
    )


def create_draft(
    db: Session,
    request: DraftMutationRequest,
    *,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
    path_product_id: str | None = None,
) -> tuple[DraftRevisionResponse, bool]:
    if path_product_id is None and request.expected_version != 0:
        raise HTTPException(
            status_code=422,
            detail="New catalog product drafts require expected_version 0.",
        )
    if path_product_id is None and request.product.product_id is not None:
        raise HTTPException(
            status_code=422,
            detail="New catalog product IDs are assigned by the server.",
        )
    product_id = path_product_id or _product_id(request.product)
    if path_product_id and request.product.product_id not in (None, path_product_id):
        raise HTTPException(status_code=422, detail="Body product_id must match the path product_id.")

    payload = request.model_dump(mode="json")
    payload["product"]["product_id"] = product_id
    operation = f"catalog.draft:{product_id}"

    def action() -> dict:
        _assert_expected_version(db, product_id, request.expected_version)
        product = _safe_product_snapshot(
            request.product.model_copy(update={"product_id": product_id})
        )
        if request.current_draft_id:
            current = _latest_owned_draft(db, product_id, principal)
            if current is None or current.id != request.current_draft_id:
                raise _conflict("The requested catalog draft is no longer current.")
            actual_draft_version = draft_revision_version(db, current)
            if actual_draft_version != request.expected_draft_version:
                raise _conflict(
                    f"Expected catalog draft version {request.expected_draft_version}, "
                    f"but current version is {actual_draft_version}."
                )
            if current.base_version != request.expected_version:
                raise _conflict(
                    "The current draft no longer targets the expected published version."
                )
            product = _preserve_server_image_fields(
                product, ProductDraft.model_validate(current.snapshot_json)
            )
        payload["product"] = product.model_dump(mode="json")
        revision = CatalogDraftRevision(
            id=f"draft_{uuid4().hex[:24]}",
            catalog_product_id=product_id,
            base_version=request.expected_version,
            status="draft",
            moderation_state=request.moderation_state,
            snapshot_json=payload["product"],
            created_by=principal.provider_user_id,
        )
        db.add(revision)
        db.flush()
        if request.current_draft_id:
            workflows = db.scalars(
                select(CatalogWorkflow).where(
                    CatalogWorkflow.draft_revision_id == request.current_draft_id,
                    CatalogWorkflow.owner_provider == principal.provider,
                    CatalogWorkflow.owner_provider_user_id
                    == principal.provider_user_id,
                )
            ).all()
            for workflow in workflows:
                workflow.draft_revision_id = revision.id
                workflow.updated_at = datetime.now(timezone.utc)
        return _draft_response(revision).model_dump(mode="json")

    response, replayed = _idempotent(
        db,
        key=idempotency_key,
        operation=operation,
        payload=payload,
        principal=principal,
        action=action,
    )
    return DraftRevisionResponse.model_validate(response), replayed


def _validate_publishable(
    db: Session, revision: CatalogDraftRevision, product: ProductDraft
) -> None:
    if revision.status != "draft":
        raise _conflict("Only draft revisions can be published.")
    if revision.moderation_state != "approved":
        raise _conflict("Catalog publication requires an approved moderation state.")
    if not db.get(SyntheticRun, product.seed_run_id):
        raise _conflict(f"Synthetic run {product.seed_run_id!r} does not exist.")
    if any(not variant.image_link and not variant.image_set for variant in product.variants):
        raise _conflict("Every catalog variant requires an image_link or image_set before publication.")
    if any(
        variant.image_set.get("source") == "catalog_studio"
        and variant.image_set.get("approval_status") != "approved"
        for variant in product.variants
    ):
        raise _conflict("Catalog Studio generated images require approval before publication.")
    store_ids = {row.store_id for variant in product.variants for row in variant.inventory}
    existing_store_ids = set(db.scalars(select(Store.id).where(Store.id.in_(store_ids))).all())
    missing = sorted(store_ids - existing_store_ids)
    if missing:
        raise _conflict("Unknown inventory stores: " + ", ".join(missing))


def _variant_id(product: ProductDraft, variant) -> str:
    if variant.variant_id:
        return variant.variant_id
    variant_key = variant_key_for_values(
        brand=product.brand,
        title=product.title,
        category=product.category,
        color=variant.color,
        material=variant.material,
        gender=variant.gender,
        season=variant.season,
    )
    return product_variant_id_for_key(variant_key)


def _apply_snapshot(
    db: Session, product_id: str, product: ProductDraft, version: int
) -> CatalogProduct:
    catalog_key = catalog_key_for_values(
        brand=product.brand, title=product.title, category=product.category
    )
    conflicting_id = db.scalar(
        select(CatalogProduct.id).where(
            CatalogProduct.catalog_key == catalog_key,
            CatalogProduct.id != product_id,
        )
    )
    if conflicting_id:
        raise _conflict("Another catalog product already uses this brand, title, and category.")
    explicit_variant_ids = [variant.variant_id for variant in product.variants if variant.variant_id]
    if explicit_variant_ids:
        conflicting_variant_id = db.scalar(
            select(ProductVariant.id).where(
                ProductVariant.id.in_(explicit_variant_ids),
                ProductVariant.catalog_product_id != product_id,
            )
        )
        if conflicting_variant_id:
            raise _conflict(f"Variant ID {conflicting_variant_id!r} belongs to another catalog product.")

    primary_variant_id = _variant_id(
        product, product.variants[product.primary_variant_index]
    )
    metadata = persisted_product_metadata(
        product.metadata,
        design_specification=(
            product.design_specification.model_dump(mode="json")
            if product.design_specification
            else None
        ),
        variant_axes=list(product.variant_axes),
        primary_variant_id=primary_variant_id,
    )
    row = db.get(CatalogProduct, product_id)
    if row is None:
        row = CatalogProduct(
            id=product_id,
            seed_run_id=product.seed_run_id,
            catalog_key=catalog_key,
            title=product.title,
            description=product.description,
            brand=product.brand,
            category=product.category,
            metadata_json=metadata,
            lifecycle_status="published",
            version=version,
        )
        db.add(row)
        db.flush()
    else:
        row.seed_run_id = product.seed_run_id
        row.catalog_key = catalog_key
        row.title = product.title
        row.description = product.description
        row.brand = product.brand
        row.category = product.category
        row.metadata_json = metadata
        row.lifecycle_status = "published"
        row.version = version
        row.updated_at = datetime.now(timezone.utc)
        existing_variant_ids = db.scalars(
            select(ProductVariant.id).where(ProductVariant.catalog_product_id == product_id)
        ).all()
        if existing_variant_ids:
            db.execute(
                delete(StoreInventory).where(
                    StoreInventory.variant_id.in_(existing_variant_ids)
                )
            )
            db.execute(
                delete(ProductVariant).where(
                    ProductVariant.id.in_(existing_variant_ids)
                )
            )
        db.flush()

    for variant in product.variants:
        variant_key = variant_key_for_values(
            brand=product.brand,
            title=product.title,
            category=product.category,
            color=variant.color,
            material=variant.material,
            gender=variant.gender,
            season=variant.season,
        )
        variant_id = _variant_id(product, variant)
        db.add(
            ProductVariant(
                id=variant_id,
                seed_run_id=product.seed_run_id,
                catalog_product_id=product_id,
                variant_key=variant_key,
                color=variant.color,
                material=variant.material,
                gender=variant.gender,
                season=variant.season,
                price_min=variant.price_min,
                price_max=variant.price_max,
                link=variant.link,
                image_link=variant.image_link,
                image_set={
                    key: value
                    for key, value in variant.image_set.items()
                    if key not in {"file_path", "history"}
                },
                metadata_json=variant.metadata,
            )
        )
        for inventory in variant.inventory:
            db.add(
                StoreInventory(
                    id=store_inventory_id_for_values(
                        store_id=inventory.store_id, variant_id=variant_id, size=inventory.size
                    ),
                    seed_run_id=product.seed_run_id,
                    store_id=inventory.store_id,
                    variant_id=variant_id,
                    size=inventory.size,
                    availability=inventory.availability,
                    inventory_qty=inventory.inventory_qty,
                    objective_weight=inventory.objective_weight,
                    metadata_json=inventory.metadata,
                )
            )
    db.flush()
    return row


def publish_draft(
    db: Session,
    *,
    product_id: str,
    draft_id: str,
    expected_version: int,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
) -> tuple[LifecycleMutationResponse, bool]:
    payload = {"draft_id": draft_id, "expected_version": expected_version}
    operation = f"catalog.publish:{product_id}"

    def action() -> dict:
        _assert_expected_version(db, product_id, expected_version)
        revision = db.get(CatalogDraftRevision, draft_id)
        if not revision or revision.catalog_product_id != product_id:
            raise HTTPException(status_code=404, detail="Catalog draft revision not found.")
        if revision.base_version != expected_version:
            raise _conflict("Draft base version does not match the expected catalog version.")
        product = ProductDraft.model_validate(revision.snapshot_json)
        _validate_publishable(db, revision, product)
        row = _apply_snapshot(db, product_id, product, expected_version + 1)
        enqueue_index_job(db, product.seed_run_id, commit=False, deduplicate=False)
        revision.status = "published"
        revision.published_at = datetime.now(timezone.utc)
        return LifecycleMutationResponse(
            product_id=product_id,
            lifecycle_status="published",
            version=row.version,
        ).model_dump(mode="json")

    response, replayed = _idempotent(
        db,
        key=idempotency_key,
        operation=operation,
        payload=payload,
        principal=principal,
        action=action,
    )
    return LifecycleMutationResponse.model_validate(response), replayed


def archive_product(
    db: Session,
    *,
    product_id: str,
    expected_version: int,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
) -> tuple[LifecycleMutationResponse, bool]:
    payload = {"expected_version": expected_version}
    operation = f"catalog.archive:{product_id}"

    def action() -> dict:
        row = _assert_expected_version(db, product_id, expected_version)
        if row is None:
            raise HTTPException(status_code=404, detail="Catalog product not found.")
        row.lifecycle_status = "archived"
        row.version += 1
        row.updated_at = datetime.now(timezone.utc)
        return LifecycleMutationResponse(
            product_id=product_id,
            lifecycle_status="archived",
            version=row.version,
        ).model_dump(mode="json")

    response, replayed = _idempotent(
        db,
        key=idempotency_key,
        operation=operation,
        payload=payload,
        principal=principal,
        action=action,
    )
    return LifecycleMutationResponse.model_validate(response), replayed


def start_product_revision(
    db: Session,
    *,
    product_id: str,
    request: StartRevisionRequest,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
) -> tuple[AdminDraftSnapshot, bool]:
    payload = request.model_dump(mode="json")
    operation = f"catalog.start-revision:{product_id}"

    def action() -> dict:
        row = _assert_expected_version(db, product_id, request.expected_version)
        if row is None:
            raise HTTPException(status_code=404, detail="Catalog product not found.")
        current = _latest_owned_draft(db, product_id, principal)
        if current and current.base_version == row.version:
            raise _conflict(
                "This catalog product already has a current private draft."
            )
        workflow = None
        if request.workflow_id:
            workflow = db.scalar(
                select(CatalogWorkflow)
                .where(CatalogWorkflow.id == request.workflow_id)
                .with_for_update()
            )
            if (
                workflow is None
                or workflow.owner_provider != principal.provider
                or workflow.owner_provider_user_id != principal.provider_user_id
            ):
                raise HTTPException(
                    status_code=404,
                    detail="Catalog Studio catalog workflow not found.",
                )
            if workflow.draft_revision_id:
                raise _conflict("This catalog workflow already has a current draft.")
            if (
                workflow.published_product_id
                and workflow.published_product_id != product_id
            ):
                raise _conflict(
                    "This catalog workflow is already linked to another product."
                )

        revision = CatalogDraftRevision(
            id=f"draft_{uuid4().hex[:24]}",
            catalog_product_id=product_id,
            base_version=row.version,
            status="draft",
            moderation_state="approved",
            snapshot_json=_published_snapshot(db, row).model_dump(mode="json"),
            created_by=principal.provider_user_id,
        )
        db.add(revision)
        db.flush()
        if workflow:
            workflow.draft_revision_id = revision.id
            workflow.published_product_id = product_id
            workflow.updated_at = datetime.now(timezone.utc)
            db.flush()
        return _admin_draft_snapshot(db, revision, principal).model_dump(mode="json")

    response, replayed = _idempotent(
        db,
        key=idempotency_key,
        operation=operation,
        payload=payload,
        principal=principal,
        action=action,
    )
    return AdminDraftSnapshot.model_validate(response), replayed


def list_admin_products(
    db: Session,
    *,
    principal: AuthenticatedPrincipal,
    q: str | None = None,
    lifecycle_status: Literal["draft", "published", "archived"] | None = None,
    category: str | None = None,
    brand: str | None = None,
    page: int = 1,
    page_size: int = 24,
) -> AdminProductListResponse:
    products = {row.id: row for row in db.scalars(select(CatalogProduct)).all()}
    owned_drafts = db.scalars(
        select(CatalogDraftRevision)
        .where(
            CatalogDraftRevision.created_by == principal.provider_user_id,
            CatalogDraftRevision.status == "draft",
        )
        .order_by(
            CatalogDraftRevision.created_at.desc(), CatalogDraftRevision.id.desc()
        )
    ).all()
    draft_versions = {
        product_id: int(version)
        for product_id, version in db.execute(
            select(
                CatalogDraftRevision.catalog_product_id,
                func.count(CatalogDraftRevision.id),
            )
            .where(CatalogDraftRevision.created_by == principal.provider_user_id)
            .group_by(CatalogDraftRevision.catalog_product_id)
        ).all()
    }
    current_drafts: dict[str, CatalogDraftRevision] = {}
    for revision in owned_drafts:
        published = products.get(revision.catalog_product_id)
        if published and revision.base_version != published.version:
            continue
        current_drafts.setdefault(revision.catalog_product_id, revision)

    product_ids = set(products) | set(current_drafts)
    items: list[AdminProductListItem] = []
    query = (q or "").strip().casefold()
    for product_id in product_ids:
        row = products.get(product_id)
        draft = current_drafts.get(product_id)
        if draft:
            effective = _safe_product_snapshot(
                ProductDraft.model_validate(draft.snapshot_json)
            )
            title = effective.title
            description = effective.description
            effective_brand = effective.brand
            effective_category = effective.category
        elif row:
            title = row.title
            description = row.description
            effective_brand = row.brand
            effective_category = row.category
        else:
            continue
        status_value = row.lifecycle_status if row else "draft"
        if lifecycle_status == "draft" and draft is None:
            continue
        if lifecycle_status in {"published", "archived"} and status_value != lifecycle_status:
            continue
        if category and effective_category.casefold() != category.casefold():
            continue
        if brand and effective_brand.casefold() != brand.casefold():
            continue
        if query and query not in " ".join(
            [
                title,
                description,
                effective_brand,
                effective_category,
            ]
        ).casefold():
            continue
        updated_at = draft.created_at if draft else row.updated_at  # type: ignore[union-attr]
        items.append(
            AdminProductListItem(
                product_id=product_id,
                lifecycle_status=status_value,  # type: ignore[arg-type]
                version=row.version if row else 0,
                title=title,
                brand=effective_brand,
                category=effective_category,
                has_draft=draft is not None,
                current_draft_id=draft.id if draft else None,
                current_draft_version=(
                    draft_versions[product_id] if draft else None
                ),
                updated_at=updated_at,
            )
        )
    items.sort(key=lambda item: (item.updated_at, item.product_id), reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    return AdminProductListResponse(
        items=items[start : start + page_size],
        total=total,
        page=page,
        page_size=page_size,
    )


def get_admin_product(
    db: Session,
    product_id: str,
    *,
    principal: AuthenticatedPrincipal,
) -> AdminProductResponse | None:
    row = db.get(CatalogProduct, product_id)
    drafts = db.scalars(
        select(CatalogDraftRevision)
        .where(CatalogDraftRevision.catalog_product_id == product_id)
        .order_by(CatalogDraftRevision.created_at.desc())
    ).all()
    current = _latest_owned_draft(db, product_id, principal)
    if row and current and current.base_version != row.version:
        current = None
    current_snapshot = (
        _admin_draft_snapshot(db, current, principal) if current else None
    )
    published_snapshot = _published_snapshot(db, row) if row else None
    if row:
        effective = current_snapshot.product if current_snapshot else published_snapshot
        assert effective is not None
        return AdminProductResponse(
            product_id=row.id,
            lifecycle_status=row.lifecycle_status,  # type: ignore[arg-type]
            version=row.version,
            title=effective.title,
            description=effective.description,
            brand=effective.brand,
            category=effective.category,
            metadata=effective.metadata,
            published_snapshot=published_snapshot,
            current_draft=current_snapshot,
            drafts=[_draft_response(draft) for draft in drafts],
        )
    if not drafts:
        return None
    snapshot = ProductDraft.model_validate(drafts[0].snapshot_json)
    return AdminProductResponse(
        product_id=product_id,
        lifecycle_status="draft",
        version=0,
        title=snapshot.title,
        description=snapshot.description,
        brand=snapshot.brand,
        category=snapshot.category,
        metadata=snapshot.metadata,
        current_draft=current_snapshot,
        drafts=[_draft_response(draft) for draft in drafts],
    )
