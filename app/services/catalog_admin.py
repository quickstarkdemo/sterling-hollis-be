from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Callable
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog.admin_schemas import (
    AdminProductResponse,
    DraftMutationRequest,
    DraftRevisionResponse,
    LifecycleMutationResponse,
    ProductDraft,
)
from app.models import (
    CatalogAdminMutation,
    CatalogDraftRevision,
    CatalogProduct,
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
            metadata_json=product.metadata,
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
        row.metadata_json = product.metadata
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
        variant_id = variant.variant_id or product_variant_id_for_key(variant_key)
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


def get_admin_product(db: Session, product_id: str) -> AdminProductResponse | None:
    row = db.get(CatalogProduct, product_id)
    drafts = db.scalars(
        select(CatalogDraftRevision)
        .where(CatalogDraftRevision.catalog_product_id == product_id)
        .order_by(CatalogDraftRevision.created_at.desc())
    ).all()
    if row:
        return AdminProductResponse(
            product_id=row.id,
            lifecycle_status=row.lifecycle_status,  # type: ignore[arg-type]
            version=row.version,
            title=row.title,
            description=row.description,
            brand=row.brand,
            category=row.category,
            metadata=row.metadata_json,
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
        drafts=[_draft_response(draft) for draft in drafts],
    )
