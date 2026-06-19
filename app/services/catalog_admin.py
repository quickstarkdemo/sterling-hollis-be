from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Callable, Literal
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.catalog.admin_schemas import (
    AdminDraftSnapshot,
    AdminDraftSnapshotV2,
    AdminDraftSnapshotV3,
    AdminProductListItem,
    AdminProductListResponse,
    AdminProductResponse,
    AdminProductResponseV2,
    AdminProductResponseV3,
    BrandCreateRequest,
    BrandReference,
    CatalogChoice,
    CatalogReferenceData,
    DraftMutationRequest,
    DraftMutationRequestV2,
    DraftMutationRequestV3,
    DraftRevisionResponse,
    LifecycleMutationResponse,
    ProductDraft,
    ProductDraftV2,
    ProductDraftV3,
    ProductDraftPreviewV3,
    ProductReadinessResponse,
    ReadinessIssue,
    StoreReference,
    StartRevisionRequest,
    product_draft_v1_from_snapshot,
    product_draft_v1_from_v2,
    product_draft_v2_from_snapshot,
    product_draft_v2_from_v1,
    product_draft_v2_from_v3,
    product_draft_v3_from_snapshot,
    product_draft_v3_from_v2,
)
from app.catalog.authoring import (
    authoring_metadata,
    persisted_product_metadata,
    public_product_metadata,
)
from app.catalog.references import (
    CATALOG_AVAILABILITY_CHOICES,
    CATALOG_AVAILABILITY_VALUES,
    catalog_brand_id_for_name,
    display_brand_name,
    normalized_brand_name,
)
from app.models import (
    CatalogAdminMutation,
    CatalogBrand,
    CatalogDraftRevision,
    CatalogSourceBundle,
    CatalogSuggestionSet,
    CatalogProduct,
    CatalogWorkflow,
    ProductMediaAsset,
    ProductInventory,
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
    product_inventory_id_for_values,
    normalized_size,
    store_inventory_id_for_values,
    variant_key_for_values,
)
from app.services.index_jobs import enqueue_index_job
from app.services.taxonomy import CATEGORY_TAXONOMY


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _managed_image_urls(image_set: dict, image_link: str | None = None) -> list[str]:
    """Return authored image URLs in presentation order without thumbnail duplicates."""
    candidates = [image_set.get("primary_url")]
    candidates.extend(image_set.get("detail_urls") or [])
    if not any(candidates):
        candidates.extend([image_set.get("thumbnail_url"), image_link])
    elif image_link:
        candidates.append(image_link)
    urls: list[str] = []
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value and value not in urls:
            urls.append(value)
    return urls


def _legacy_media_id(product_id: str, image_url: str) -> str:
    digest = hashlib.sha256(f"{product_id}\0{image_url}".encode("utf-8")).hexdigest()[:24]
    return f"media_legacy_{digest}"


def _complete_media_payload(
    *,
    product_id: str,
    persisted_assets: list[ProductMediaAsset],
    variants: list[ProductVariant],
) -> list[dict]:
    """Project every published image as one stable, approved media item."""
    payload: list[dict] = []
    represented_urls: dict[str, dict] = {}
    known_ids = {asset.id for asset in persisted_assets}

    for asset in persisted_assets:
        image_set = _safe_image_value(dict(asset.image_set or {}))
        item = {
            "media_id": asset.id,
            "role": asset.role,
            "intent": asset.intent,
            "source_media_id": asset.source_media_id,
            "predecessor_media_id": asset.predecessor_media_id,
            "parameters": dict(asset.parameters or {}),
            "image_set": image_set,
            "approval_status": "approved",
            "display_order": asset.display_order,
            "provenance": dict(asset.provenance or {}),
        }
        payload.append(item)
        primary_urls = _managed_image_urls(image_set)
        if primary_urls:
            represented_urls[primary_urls[0]] = item

    candidates: list[tuple[str, str, str | None]] = []
    for asset in persisted_assets:
        urls = _managed_image_urls(_safe_image_value(dict(asset.image_set or {})))
        candidates.extend((url, "managed_detail", asset.id) for url in urls[1:])
    for variant in variants:
        image_set = _safe_image_value(dict(variant.image_set or {}))
        candidates.extend(
            (url, "legacy_variant", variant.id)
            for url in _managed_image_urls(image_set, variant.image_link)
        )

    for image_url, source_kind, source_id in candidates:
        existing = represented_urls.get(image_url)
        if existing is not None:
            source_ids = existing["provenance"].setdefault("source_ids", [])
            if source_id and source_id not in source_ids:
                source_ids.append(source_id)
            continue
        media_id = _legacy_media_id(product_id, image_url)
        if media_id in known_ids:
            media_id = _legacy_media_id(product_id, f"{source_kind}:{source_id}:{image_url}")
        known_ids.add(media_id)
        item = {
            "media_id": media_id,
            "role": "variation",
            "intent": "manual",
            "source_media_id": None,
            "predecessor_media_id": None,
            "parameters": {},
            "image_set": {
                "thumbnail_url": image_url,
                "primary_url": image_url,
                "detail_urls": [image_url],
            },
            "approval_status": "approved",
            "display_order": len(payload),
            "provenance": {
                "source": source_kind,
                "source_ids": [source_id] if source_id else [],
                "source_url": image_url,
            },
        }
        payload.append(item)
        represented_urls[image_url] = item

    if not payload:
        return []
    core = next((item for item in payload if item["role"] == "core"), payload[0])
    ordered = [core, *(item for item in payload if item is not core)]
    for display_order, item in enumerate(ordered):
        item["role"] = "core" if item is core else "variation"
        item["display_order"] = display_order
    return ordered


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
        if (
            existing.created_by != principal.provider_user_id
            or existing.operation != operation
            or existing.request_hash != fingerprint
        ):
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
            and concurrent.created_by == principal.provider_user_id
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


def list_catalog_references(db: Session) -> CatalogReferenceData:
    brands = db.scalars(
        select(CatalogBrand)
        .where(CatalogBrand.active.is_(True))
        .order_by(func.lower(CatalogBrand.name), CatalogBrand.id)
    ).all()
    stores = db.scalars(
        select(Store).order_by(
            func.lower(Store.name),
            func.lower(Store.city),
            func.lower(Store.state),
            Store.id,
        )
    ).all()
    categories = sorted(
        (
            CatalogChoice(id=category_id, label=str(config["label"]))
            for category_id, config in CATEGORY_TAXONOMY.items()
        ),
        key=lambda item: (item.label.casefold(), item.id),
    )
    return CatalogReferenceData(
        brands=[BrandReference(id=brand.id, name=brand.name) for brand in brands],
        stores=[
            StoreReference(
                id=store.id,
                name=store.name,
                city=store.city,
                state=store.state,
                label=f"{store.name} — {store.city}, {store.state}",
            )
            for store in stores
        ],
        categories=categories,
        availability=[
            CatalogChoice(id=value, label=label)
            for value, label in CATALOG_AVAILABILITY_CHOICES
        ],
    )


def add_catalog_brand(
    db: Session,
    request: BrandCreateRequest,
    *,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
) -> tuple[BrandReference, bool]:
    display_name = display_brand_name(request.name)
    normalized_name = normalized_brand_name(display_name)
    payload = {"name": display_name}

    def action() -> dict:
        existing = db.scalar(
            select(CatalogBrand).where(
                CatalogBrand.normalized_name == normalized_name
            )
        )
        if existing is not None:
            raise _conflict(
                f"Brand {existing.name!r} already exists as a canonical catalog brand."
            )
        brand = CatalogBrand(
            id=catalog_brand_id_for_name(normalized_name),
            name=display_name,
            normalized_name=normalized_name,
            active=True,
        )
        db.add(brand)
        db.flush()
        return BrandReference(id=brand.id, name=brand.name).model_dump(mode="json")

    response, replayed = _idempotent(
        db,
        key=idempotency_key,
        operation="catalog.v2.add-brand",
        payload=payload,
        principal=principal,
        action=action,
    )
    return BrandReference.model_validate(response), replayed


def _canonicalize_v2_references(
    db: Session, product: ProductDraftV2
) -> ProductDraftV2:
    brand = db.get(CatalogBrand, product.brand_id)
    if brand is None or not brand.active:
        raise HTTPException(status_code=422, detail="The selected catalog brand is not active.")
    if normalized_brand_name(product.brand) != brand.normalized_name:
        raise HTTPException(
            status_code=422,
            detail="brand must match the selected canonical brand_id.",
        )
    if product.category not in CATEGORY_TAXONOMY:
        raise HTTPException(status_code=422, detail="The selected catalog category is invalid.")
    invalid_availability = sorted(
        {
            row.availability
            for row in product.inventory
            if row.availability not in CATALOG_AVAILABILITY_VALUES
        }
    )
    if invalid_availability:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported inventory availability: {invalid_availability[0]!r}.",
        )
    store_ids = {row.store_id for row in product.inventory}
    existing_store_ids = set(
        db.scalars(select(Store.id).where(Store.id.in_(store_ids))).all()
    )
    missing_store_ids = sorted(store_ids - existing_store_ids)
    if missing_store_ids:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown catalog store_id: {missing_store_ids[0]!r}.",
        )
    return product.model_copy(update={"brand": brand.name})


def _active_brand_for_name(db: Session, name: str) -> CatalogBrand:
    brand = db.scalar(
        select(CatalogBrand).where(
            CatalogBrand.normalized_name == normalized_brand_name(name),
            CatalogBrand.active.is_(True),
        )
    )
    if brand is None:
        raise _conflict("Add this brand to the canonical catalog registry before publishing.")
    return brand


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


def _reject_older_write_to_v3_product(
    product: CatalogProduct | None,
    *,
    contract: str,
) -> None:
    if product and isinstance(authoring_metadata(product.metadata_json).get("v3"), dict):
        raise _conflict(
            f"This product is owned by the v3 authoring contract and cannot be replaced by a {contract} write."
        )


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


def _owned_draft_for_update(
    db: Session,
    *,
    draft_id: str,
    product_id: str,
    principal: AuthenticatedPrincipal,
) -> CatalogDraftRevision | None:
    return db.scalar(
        select(CatalogDraftRevision)
        .where(
            CatalogDraftRevision.id == draft_id,
            CatalogDraftRevision.catalog_product_id == product_id,
            CatalogDraftRevision.created_by == principal.provider_user_id,
            CatalogDraftRevision.status == "draft",
        )
        .with_for_update()
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


def _safe_preview_value(value):
    if isinstance(value, dict):
        return {
            key: _safe_preview_value(item)
            for key, item in value.items()
            if key not in {"file_path", "storage_key", "preview_storage_key"}
        }
    if isinstance(value, list):
        return [_safe_preview_value(item) for item in value]
    return value


def _safe_product_snapshot(product: ProductDraft) -> ProductDraft:
    payload = product.model_dump(mode="json")
    for asset in payload.get("media", []):
        asset["image_set"] = _safe_image_value(asset.get("image_set") or {})
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
    current_media = {row.media_id: row for row in current.media}
    payload = incoming.model_dump(mode="json")
    for index, asset in enumerate(incoming.media):
        previous_asset = current_media.get(asset.media_id)
        if previous_asset is None:
            continue
        for field in (
            "approval_status",
            "source_media_id",
            "predecessor_media_id",
            "provenance",
        ):
            payload["media"][index][field] = getattr(previous_asset, field)
        if asset.image_set:
            payload["media"][index]["image_set"] = _restore_server_image_fields(
                asset.image_set, previous_asset.image_set
            )
    for index, variant in enumerate(incoming.variants):
        previous = current_variants.get(_variant_identity(variant))
        if previous is None or not variant.image_set:
            continue
        payload["variants"][index]["image_set"] = _restore_server_image_fields(
            variant.image_set, previous.image_set
        )
    return ProductDraft.model_validate(payload)


def _preserve_server_image_fields_v2(
    incoming: ProductDraftV2, current: ProductDraftV2
) -> ProductDraftV2:
    current_media = {row.media_id: row for row in current.media}
    payload = incoming.model_dump(mode="json")
    for index, asset in enumerate(incoming.media):
        previous = current_media.get(asset.media_id)
        if previous is None:
            continue
        for field in (
            "approval_status",
            "source_media_id",
            "predecessor_media_id",
            "provenance",
        ):
            payload["media"][index][field] = getattr(previous, field)
        if asset.image_set:
            payload["media"][index]["image_set"] = _restore_server_image_fields(
                asset.image_set, previous.image_set
            )
    return ProductDraftV2.model_validate(payload)


def _preserve_server_image_fields_v3(
    incoming: ProductDraftV3, current: ProductDraftV3
) -> ProductDraftV3:
    current_media = {row.media_id: row for row in current.media}
    payload = incoming.model_dump(mode="json")
    for index, asset in enumerate(incoming.media):
        previous = current_media.get(asset.media_id)
        if previous is None:
            continue
        for field in (
            "approval_status",
            "source_media_id",
            "predecessor_media_id",
            "provenance",
        ):
            payload["media"][index][field] = getattr(previous, field)
        if asset.image_set:
            payload["media"][index]["image_set"] = _restore_server_image_fields(
                asset.image_set, previous.image_set
            )
    return ProductDraftV3.model_validate(payload)


def _v3_authoring_metadata(product: ProductDraftV3) -> dict:
    return {
        "benefits": list(product.benefits),
        "specifications": [
            specification.model_dump(mode="json")
            for specification in product.specifications
        ],
        "care_instructions": list(product.care_instructions),
        "content_details": list(product.content_details),
        "seo": product.seo.model_dump(mode="json"),
        "readiness_inputs": product.readiness_inputs.model_dump(mode="json"),
        "media_alt_text": {
            asset.media_id: asset.alt_text
            for asset in product.media
            if asset.alt_text
        },
    }


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
    persisted_assets = db.scalars(
        select(ProductMediaAsset)
        .where(ProductMediaAsset.catalog_product_id == row.id)
        .order_by(ProductMediaAsset.display_order.asc(), ProductMediaAsset.id.asc())
    ).all()
    media_payload = _complete_media_payload(
        product_id=row.id,
        persisted_assets=list(persisted_assets),
        variants=list(variants),
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
            "media": media_payload,
            "variants": variant_payload,
        }
    )


def _published_snapshot_v2(db: Session, row: CatalogProduct) -> ProductDraftV2:
    compatibility = _published_snapshot(db, row)
    inventory = db.scalars(
        select(ProductInventory)
        .where(ProductInventory.catalog_product_id == row.id)
        .order_by(
            ProductInventory.store_id.asc(),
            ProductInventory.size_key.asc(),
        )
    ).all()
    return ProductDraftV2(
        product_id=row.id,
        seed_run_id=row.seed_run_id,
        title=row.title,
        description=row.description,
        brand_id=row.brand_id or catalog_brand_id_for_name(row.brand),
        brand=row.brand,
        category=row.category,
        price_min=row.price_min,
        price_max=row.price_max,
        link=row.link,
        color=row.color,
        material=row.material,
        gender=row.gender,
        season=row.season,
        metadata=public_product_metadata(row.metadata_json),
        media=compatibility.media,
        inventory=[
            {
                "store_id": item.store_id,
                "size": item.size,
                "availability": item.availability,
                "inventory_qty": item.inventory_qty,
                "metadata": dict(item.metadata_json or {}),
            }
            for item in inventory
        ],
    )


def _published_snapshot_v3(db: Session, row: CatalogProduct) -> ProductDraftV3:
    base = product_draft_v3_from_v2(_published_snapshot_v2(db, row))
    stored = authoring_metadata(row.metadata_json).get("v3")
    if not isinstance(stored, dict):
        return base
    payload = base.model_dump(mode="json")
    for field in (
        "benefits",
        "specifications",
        "care_instructions",
        "content_details",
        "seo",
        "readiness_inputs",
    ):
        if field in stored:
            payload[field] = stored[field]
    media_alt_text = stored.get("media_alt_text")
    if isinstance(media_alt_text, dict):
        for media in payload.get("media", []):
            value = media_alt_text.get(media["media_id"])
            if isinstance(value, str):
                media["alt_text"] = value
    return ProductDraftV3.model_validate(payload)


def _readiness_response(
    db: Session,
    revision: CatalogDraftRevision,
    product: ProductDraftV3,
) -> ProductReadinessResponse:
    blocking: list[ReadinessIssue] = []
    recommendations: list[ReadinessIssue] = []
    approved_core = any(
        asset.role == "core" and asset.approval_status == "approved"
        for asset in product.media
    )
    if not approved_core:
        blocking.append(
            ReadinessIssue(
                code="missing_approved_media",
                field_path="/media",
                message="An approved core product image is required.",
            )
        )
    if any(asset.approval_status != "approved" for asset in product.media):
        blocking.append(
            ReadinessIssue(
                code="unapproved_media",
                field_path="/media",
                message="Every product image must be approved before publication.",
            )
        )
    if product.price_min <= 0 or product.price_max <= 0:
        blocking.append(
            ReadinessIssue(
                code="missing_price",
                field_path="/price_min",
                message="A positive product price is required.",
            )
        )
    specification_names = {
        specification.name.casefold() for specification in product.specifications
    }
    for required in product.readiness_inputs.required_specifications:
        if required.casefold() not in specification_names:
            blocking.append(
                ReadinessIssue(
                    code="missing_required_specification",
                    field_path="/specifications",
                    message=f"Required specification {required!r} is missing.",
                )
            )
    if revision.moderation_state != "approved":
        blocking.append(
            ReadinessIssue(
                code="moderation_not_approved",
                field_path="/moderation_state",
                message="Catalog moderation must be approved before publication.",
            )
        )
    if not db.get(SyntheticRun, product.seed_run_id):
        blocking.append(
            ReadinessIssue(
                code="missing_seed_run",
                field_path="/seed_run_id",
                message="The product seed run is unavailable.",
            )
        )
    if not product.seo.title:
        recommendations.append(
            ReadinessIssue(
                code="missing_seo_title",
                field_path="/seo/title",
                message="Add a search title before publication.",
            )
        )
    if not product.seo.description:
        recommendations.append(
            ReadinessIssue(
                code="missing_seo_description",
                field_path="/seo/description",
                message="Add a search description before publication.",
            )
        )
    if any(not asset.alt_text for asset in product.media):
        recommendations.append(
            ReadinessIssue(
                code="missing_media_alt_text",
                field_path="/media",
                message="Add alt text for every product image.",
            )
        )
    return ProductReadinessResponse(
        product_id=revision.catalog_product_id,
        draft_id=revision.id,
        draft_version=draft_revision_version(db, revision),
        ready=not blocking,
        blocking_errors=blocking,
        recommendations=recommendations,
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
            product_draft_v1_from_snapshot(revision.snapshot_json)
        ),
    )


def _admin_draft_snapshot_v2(
    db: Session,
    revision: CatalogDraftRevision,
    principal: AuthenticatedPrincipal,
) -> AdminDraftSnapshotV2:
    workflow = _workflow_for_draft(db, revision.id, principal)
    product = product_draft_v2_from_snapshot(revision.snapshot_json)
    payload = product.model_dump(mode="json")
    for asset in payload.get("media", []):
        asset["image_set"] = _safe_image_value(asset.get("image_set") or {})
    return AdminDraftSnapshotV2(
        revision=_draft_response(revision),
        draft_version=draft_revision_version(db, revision),
        workflow_id=workflow.id if workflow else None,
        product=ProductDraftV2.model_validate(payload),
    )


def _admin_draft_snapshot_v3(
    db: Session,
    revision: CatalogDraftRevision,
    principal: AuthenticatedPrincipal,
) -> AdminDraftSnapshotV3:
    workflow = _workflow_for_draft(db, revision.id, principal)
    product = product_draft_v3_from_snapshot(revision.snapshot_json)
    payload = product.model_dump(mode="json")
    for asset in payload.get("media", []):
        asset["image_set"] = _safe_image_value(asset.get("image_set") or {})
    safe_product = ProductDraftV3.model_validate(payload)
    return AdminDraftSnapshotV3(
        revision=_draft_response(revision),
        draft_version=draft_revision_version(db, revision),
        workflow_id=workflow.id if workflow else None,
        product=safe_product,
        readiness=_readiness_response(db, revision, safe_product),
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
        published = _assert_expected_version(db, product_id, request.expected_version)
        _reject_older_write_to_v3_product(published, contract="legacy")
        product = _safe_product_snapshot(
            request.product.model_copy(update={"product_id": product_id})
        )
        latest = _latest_owned_draft(db, product_id, principal)
        if latest and int(latest.snapshot_json.get("schema_version") or 1) >= 3:
            raise _conflict(
                "This draft is owned by the v3 authoring contract and cannot be replaced by an older write."
            )
        if request.current_draft_id:
            current = _owned_draft_for_update(
                db,
                draft_id=request.current_draft_id,
                product_id=product_id,
                principal=principal,
            )
            latest = _latest_owned_draft(db, product_id, principal)
            if current is None or latest is None or latest.id != current.id:
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
                product, product_draft_v1_from_snapshot(current.snapshot_json)
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


def create_draft_v2(
    db: Session,
    request: DraftMutationRequestV2,
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
    canonical_product = _canonicalize_v2_references(db, request.product)
    product_id = path_product_id or catalog_product_id_for_key(
        catalog_key_for_values(
            brand=canonical_product.brand,
            title=canonical_product.title,
            category=canonical_product.category,
        )
    )
    if path_product_id and request.product.product_id not in (None, path_product_id):
        raise HTTPException(status_code=422, detail="Body product_id must match the path product_id.")

    product = canonical_product.model_copy(update={"product_id": product_id})
    payload = request.model_dump(mode="json")
    payload["product"] = product.model_dump(mode="json")
    operation = f"catalog.v2.draft:{product_id}"

    def action() -> dict:
        published = _assert_expected_version(db, product_id, request.expected_version)
        _reject_older_write_to_v3_product(published, contract="v2")
        product_to_save = product
        latest = _latest_owned_draft(db, product_id, principal)
        if latest and int(latest.snapshot_json.get("schema_version") or 1) >= 3:
            raise _conflict(
                "This draft is owned by the v3 authoring contract and cannot be replaced by a v2 write."
            )
        if request.current_draft_id:
            current = _owned_draft_for_update(
                db,
                draft_id=request.current_draft_id,
                product_id=product_id,
                principal=principal,
            )
            latest = _latest_owned_draft(db, product_id, principal)
            if current is None or latest is None or latest.id != current.id:
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
            product_to_save = _preserve_server_image_fields_v2(
                product, product_draft_v2_from_snapshot(current.snapshot_json)
            )
        revision = CatalogDraftRevision(
            id=f"draft_{uuid4().hex[:24]}",
            catalog_product_id=product_id,
            base_version=request.expected_version,
            status="draft",
            moderation_state=request.moderation_state,
            snapshot_json=product_to_save.model_dump(mode="json"),
            created_by=principal.provider_user_id,
        )
        db.add(revision)
        db.flush()
        if request.current_draft_id:
            workflows = db.scalars(
                select(CatalogWorkflow).where(
                    CatalogWorkflow.draft_revision_id == request.current_draft_id,
                    CatalogWorkflow.owner_provider == principal.provider,
                    CatalogWorkflow.owner_provider_user_id == principal.provider_user_id,
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


def _validate_v3_source_references(
    db: Session,
    *,
    product: ProductDraftV3,
    product_id: str,
    principal: AuthenticatedPrincipal,
) -> list[CatalogSourceBundle]:
    bundle_ids = [reference.bundle_id for reference in product.source_references]
    owned_bundles = {
        bundle.id: bundle
        for bundle in db.scalars(
            select(CatalogSourceBundle)
            .options(selectinload(CatalogSourceBundle.assets))
            .where(
                CatalogSourceBundle.id.in_(bundle_ids),
                CatalogSourceBundle.owner_provider == principal.provider,
                CatalogSourceBundle.owner_provider_user_id
                == principal.provider_user_id,
            )
        ).all()
    }
    bundles: list[CatalogSourceBundle] = []
    for reference in product.source_references:
        bundle = owned_bundles.get(reference.bundle_id)
        if bundle is None:
            raise HTTPException(status_code=404, detail="Catalog source bundle not found.")
        if bundle.catalog_product_id and bundle.catalog_product_id != product_id:
            raise _conflict("A source bundle belongs to a different catalog product.")
        bundle_asset_ids = {asset.id for asset in bundle.assets}
        if not set(reference.asset_ids).issubset(bundle_asset_ids):
            raise HTTPException(status_code=404, detail="Catalog source asset not found.")
        bundles.append(bundle)
    return bundles


def _supersede_other_suggestion_sets(
    db: Session,
    *,
    product_id: str,
    current_draft_id: str,
    exclude_set_id: str | None = None,
) -> None:
    rows = db.scalars(
        select(CatalogSuggestionSet)
        .options(selectinload(CatalogSuggestionSet.suggestions))
        .where(
            CatalogSuggestionSet.catalog_product_id == product_id,
            CatalogSuggestionSet.current_draft_revision_id == current_draft_id,
            CatalogSuggestionSet.status.in_(["pending", "partially_reviewed"]),
        )
    ).all()
    for row in rows:
        if row.id == exclude_set_id:
            continue
        row.status = "superseded"
        row.updated_at = datetime.now(timezone.utc)
        for suggestion in row.suggestions:
            if suggestion.status == "pending":
                suggestion.status = "superseded"


def create_draft_v3(
    db: Session,
    request: DraftMutationRequestV3,
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
    canonical_product = _canonicalize_v2_references(db, request.product)
    product_id = path_product_id or catalog_product_id_for_key(
        catalog_key_for_values(
            brand=canonical_product.brand,
            title=canonical_product.title,
            category=canonical_product.category,
        )
    )
    if path_product_id and request.product.product_id not in (None, path_product_id):
        raise HTTPException(
            status_code=422,
            detail="Body product_id must match the path product_id.",
        )
    product = ProductDraftV3.model_validate(
        canonical_product.model_copy(update={"product_id": product_id})
    )
    source_bundles = _validate_v3_source_references(
        db,
        product=product,
        product_id=product_id,
        principal=principal,
    )
    payload = request.model_dump(mode="json")
    payload["product"] = product.model_dump(mode="json")
    operation = f"catalog.v3.draft:{product_id}"

    def action() -> dict:
        _assert_expected_version(db, product_id, request.expected_version)
        product_to_save = product
        current = None
        if request.current_draft_id:
            current = _owned_draft_for_update(
                db,
                draft_id=request.current_draft_id,
                product_id=product_id,
                principal=principal,
            )
            latest = _latest_owned_draft(db, product_id, principal)
            if current is None or latest is None or latest.id != current.id:
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
            product_to_save = _preserve_server_image_fields_v3(
                product,
                product_draft_v3_from_snapshot(current.snapshot_json),
            )
        revision = CatalogDraftRevision(
            id=f"draft_{uuid4().hex[:24]}",
            catalog_product_id=product_id,
            base_version=request.expected_version,
            status="draft",
            moderation_state=request.moderation_state,
            snapshot_json=product_to_save.model_dump(mode="json"),
            created_by=principal.provider_user_id,
        )
        db.add(revision)
        db.flush()
        if current:
            _supersede_other_suggestion_sets(
                db,
                product_id=product_id,
                current_draft_id=current.id,
            )
            workflows = db.scalars(
                select(CatalogWorkflow).where(
                    CatalogWorkflow.draft_revision_id == current.id,
                    CatalogWorkflow.owner_provider == principal.provider,
                    CatalogWorkflow.owner_provider_user_id
                    == principal.provider_user_id,
                )
            ).all()
            for workflow in workflows:
                workflow.draft_revision_id = revision.id
                workflow.updated_at = datetime.now(timezone.utc)
        for bundle in source_bundles:
            bundle.catalog_product_id = product_id
            bundle.draft_revision_id = revision.id
            bundle.updated_at = datetime.now(timezone.utc)
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


def create_draft_from_v2_compatibility(
    db: Session,
    request: DraftMutationRequestV2,
    *,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
    path_product_id: str,
) -> tuple[DraftRevisionResponse, bool]:
    current = db.get(CatalogDraftRevision, request.current_draft_id)
    if (
        current
        and current.created_by == principal.provider_user_id
        and current.snapshot_json.get("schema_version") == 3
    ):
        preserved = product_draft_v3_from_snapshot(current.snapshot_json)
        return create_draft_v3(
            db,
            DraftMutationRequestV3(
                expected_version=request.expected_version,
                current_draft_id=request.current_draft_id,
                expected_draft_version=request.expected_draft_version,
                moderation_state=request.moderation_state,
                product=product_draft_v3_from_v2(
                    request.product,
                    preserved=preserved,
                ),
            ),
            idempotency_key=idempotency_key,
            principal=principal,
            path_product_id=path_product_id,
        )
    return create_draft_v2(
        db,
        request,
        idempotency_key=idempotency_key,
        principal=principal,
        path_product_id=path_product_id,
    )


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
    if product.media:
        core_assets = [asset for asset in product.media if asset.role == "core"]
        if len(core_assets) != 1 or core_assets[0].approval_status != "approved":
            raise _conflict("Product media requires one approved core image before publication.")
        if any(asset.approval_status != "approved" for asset in product.media):
            raise _conflict("Every product media asset requires approval before publication.")
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
    db: Session,
    product_id: str,
    product: ProductDraft,
    version: int,
    *,
    authoring_v3: dict | None = None,
) -> CatalogProduct:
    canonical = product_draft_v2_from_v1(product)
    brand_reference = _active_brand_for_name(db, product.brand)
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
        authoring_v3=authoring_v3,
    )
    row = db.get(CatalogProduct, product_id)
    if row is None:
        row = CatalogProduct(
            id=product_id,
            seed_run_id=product.seed_run_id,
            catalog_key=catalog_key,
            title=product.title,
            description=product.description,
            brand_id=brand_reference.id,
            brand=brand_reference.name,
            category=product.category,
            price_min=canonical.price_min,
            price_max=canonical.price_max,
            link=canonical.link,
            color=canonical.color,
            material=canonical.material,
            gender=canonical.gender,
            season=canonical.season,
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
        row.brand_id = brand_reference.id
        row.brand = brand_reference.name
        row.category = product.category
        row.price_min = canonical.price_min
        row.price_max = canonical.price_max
        row.link = canonical.link
        row.color = canonical.color
        row.material = canonical.material
        row.gender = canonical.gender
        row.season = canonical.season
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
        db.execute(
            delete(ProductMediaAsset).where(
                ProductMediaAsset.catalog_product_id == product_id
            )
        )
        db.execute(
            delete(ProductInventory).where(
                ProductInventory.catalog_product_id == product_id
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
    for asset in product.media:
        if asset.approval_status != "approved":
            continue
        db.add(
            ProductMediaAsset(
                id=asset.media_id,
                catalog_product_id=product_id,
                role=asset.role,
                intent=asset.intent,
                source_media_id=asset.source_media_id,
                predecessor_media_id=asset.predecessor_media_id,
                image_set={
                    key: value
                    for key, value in asset.image_set.items()
                    if key not in {"file_path", "history"}
                },
                parameters=asset.parameters,
                provenance=asset.provenance,
                display_order=asset.display_order,
            )
        )
    for inventory in canonical.inventory:
        display_size, size_key = normalized_size(inventory.size)
        db.add(
            ProductInventory(
                id=product_inventory_id_for_values(
                    product_id=product_id,
                    store_id=inventory.store_id,
                    size_key=size_key,
                ),
                seed_run_id=product.seed_run_id,
                catalog_product_id=product_id,
                store_id=inventory.store_id,
                size=display_size,
                size_key=size_key,
                availability=inventory.availability,
                inventory_qty=inventory.inventory_qty,
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
        revision = _owned_draft_for_update(
            db,
            draft_id=draft_id,
            product_id=product_id,
            principal=principal,
        )
        if revision is None:
            raise HTTPException(status_code=404, detail="Catalog draft revision not found.")
        latest = _latest_owned_draft(db, product_id, principal)
        if latest is None or latest.id != revision.id:
            raise _conflict("The requested catalog draft is no longer current.")
        if revision.base_version != expected_version:
            raise _conflict("Draft base version does not match the expected catalog version.")
        product_v3 = (
            product_draft_v3_from_snapshot(revision.snapshot_json)
            if revision.snapshot_json.get("schema_version") == 3
            else None
        )
        if product_v3:
            readiness = _readiness_response(db, revision, product_v3)
            if readiness.blocking_errors:
                raise _conflict(
                    "Catalog publication is blocked: "
                    + readiness.blocking_errors[0].message
                )
        product = product_draft_v1_from_snapshot(revision.snapshot_json)
        _validate_publishable(db, revision, product)
        row = _apply_snapshot(
            db,
            product_id,
            product,
            expected_version + 1,
            authoring_v3=_v3_authoring_metadata(product_v3) if product_v3 else None,
        )
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
        _reject_older_write_to_v3_product(row, contract="legacy")
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


def start_product_revision_v2(
    db: Session,
    *,
    product_id: str,
    request: StartRevisionRequest,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
) -> tuple[AdminDraftSnapshotV2, bool]:
    payload = request.model_dump(mode="json")
    operation = f"catalog.v2.start-revision:{product_id}"

    def action() -> dict:
        row = _assert_expected_version(db, product_id, request.expected_version)
        if row is None:
            raise HTTPException(status_code=404, detail="Catalog product not found.")
        _reject_older_write_to_v3_product(row, contract="v2")
        current = _latest_owned_draft(db, product_id, principal)
        if current and current.base_version == row.version:
            raise _conflict("This catalog product already has a current private draft.")
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
            snapshot_json=_published_snapshot_v2(db, row).model_dump(mode="json"),
            created_by=principal.provider_user_id,
        )
        db.add(revision)
        db.flush()
        if workflow:
            workflow.draft_revision_id = revision.id
            workflow.published_product_id = product_id
            workflow.updated_at = datetime.now(timezone.utc)
            db.flush()
        return _admin_draft_snapshot_v2(db, revision, principal).model_dump(mode="json")

    response, replayed = _idempotent(
        db,
        key=idempotency_key,
        operation=operation,
        payload=payload,
        principal=principal,
        action=action,
    )
    return AdminDraftSnapshotV2.model_validate(response), replayed


def start_product_revision_v3(
    db: Session,
    *,
    product_id: str,
    request: StartRevisionRequest,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
) -> tuple[AdminDraftSnapshotV3, bool]:
    payload = request.model_dump(mode="json")
    operation = f"catalog.v3.start-revision:{product_id}"

    def action() -> dict:
        row = _assert_expected_version(db, product_id, request.expected_version)
        if row is None:
            raise HTTPException(status_code=404, detail="Catalog product not found.")
        current = _latest_owned_draft(db, product_id, principal)
        if current and current.base_version == row.version:
            raise _conflict("This catalog product already has a current private draft.")
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
            snapshot_json=_published_snapshot_v3(db, row).model_dump(mode="json"),
            created_by=principal.provider_user_id,
        )
        db.add(revision)
        db.flush()
        if workflow:
            workflow.draft_revision_id = revision.id
            workflow.published_product_id = product_id
            workflow.updated_at = datetime.now(timezone.utc)
            db.flush()
        return _admin_draft_snapshot_v3(db, revision, principal).model_dump(mode="json")

    response, replayed = _idempotent(
        db,
        key=idempotency_key,
        operation=operation,
        payload=payload,
        principal=principal,
        action=action,
    )
    return AdminDraftSnapshotV3.model_validate(response), replayed


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
                product_draft_v1_from_snapshot(draft.snapshot_json)
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
        .where(
            CatalogDraftRevision.catalog_product_id == product_id,
            CatalogDraftRevision.created_by == principal.provider_user_id,
        )
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
    snapshot = product_draft_v1_from_snapshot(drafts[0].snapshot_json)
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


def get_admin_product_v2(
    db: Session,
    product_id: str,
    *,
    principal: AuthenticatedPrincipal,
) -> AdminProductResponseV2 | None:
    row = db.get(CatalogProduct, product_id)
    drafts = db.scalars(
        select(CatalogDraftRevision)
        .where(
            CatalogDraftRevision.catalog_product_id == product_id,
            CatalogDraftRevision.created_by == principal.provider_user_id,
        )
        .order_by(CatalogDraftRevision.created_at.desc())
    ).all()
    current = _latest_owned_draft(db, product_id, principal)
    if row and current and current.base_version != row.version:
        current = None
    current_snapshot = (
        _admin_draft_snapshot_v2(db, current, principal) if current else None
    )
    published_snapshot = _published_snapshot_v2(db, row) if row else None
    if row:
        effective = current_snapshot.product if current_snapshot else published_snapshot
        assert effective is not None
        return AdminProductResponseV2(
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
    snapshot = product_draft_v2_from_snapshot(drafts[0].snapshot_json)
    return AdminProductResponseV2(
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


def get_admin_product_v3(
    db: Session,
    product_id: str,
    *,
    principal: AuthenticatedPrincipal,
) -> AdminProductResponseV3 | None:
    row = db.get(CatalogProduct, product_id)
    drafts = db.scalars(
        select(CatalogDraftRevision)
        .where(
            CatalogDraftRevision.catalog_product_id == product_id,
            CatalogDraftRevision.created_by == principal.provider_user_id,
        )
        .order_by(CatalogDraftRevision.created_at.desc())
    ).all()
    current = _latest_owned_draft(db, product_id, principal)
    if row and current and current.base_version != row.version:
        current = None
    current_snapshot = (
        _admin_draft_snapshot_v3(db, current, principal) if current else None
    )
    published_snapshot = _published_snapshot_v3(db, row) if row else None
    if row:
        effective = current_snapshot.product if current_snapshot else published_snapshot
        assert effective is not None
        return AdminProductResponseV3(
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
    snapshot = product_draft_v3_from_snapshot(drafts[0].snapshot_json)
    return AdminProductResponseV3(
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


def _owned_draft(
    db: Session,
    *,
    product_id: str,
    draft_id: str,
    principal: AuthenticatedPrincipal,
) -> CatalogDraftRevision:
    revision = db.scalar(
        select(CatalogDraftRevision).where(
            CatalogDraftRevision.id == draft_id,
            CatalogDraftRevision.catalog_product_id == product_id,
            CatalogDraftRevision.created_by == principal.provider_user_id,
        )
    )
    if revision is None:
        raise HTTPException(status_code=404, detail="Catalog draft revision not found.")
    return revision


def get_product_readiness_v3(
    db: Session,
    *,
    product_id: str,
    draft_id: str,
    principal: AuthenticatedPrincipal,
) -> ProductReadinessResponse:
    revision = _owned_draft(
        db,
        product_id=product_id,
        draft_id=draft_id,
        principal=principal,
    )
    product = product_draft_v3_from_snapshot(revision.snapshot_json)
    return _readiness_response(db, revision, product)


def get_product_preview_v3(
    db: Session,
    *,
    product_id: str,
    draft_id: str,
    principal: AuthenticatedPrincipal,
) -> ProductDraftPreviewV3:
    revision = _owned_draft(
        db,
        product_id=product_id,
        draft_id=draft_id,
        principal=principal,
    )
    product = product_draft_v3_from_snapshot(revision.snapshot_json)
    payload = product.model_dump(
        mode="json",
        exclude={"source_references", "readiness_inputs"},
    )
    payload = _safe_preview_value(payload)
    return ProductDraftPreviewV3(
        product_id=product_id,
        draft_id=draft_id,
        draft_version=draft_revision_version(db, revision),
        preview=payload,
        readiness=_readiness_response(db, revision, product),
    )
