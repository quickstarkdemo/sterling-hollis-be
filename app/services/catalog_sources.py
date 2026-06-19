from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import shutil
import warnings
from uuid import uuid4

from fastapi import HTTPException, UploadFile, status
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.catalog.admin_schemas import (
    DraftMutationRequestV2,
    DraftRevisionResponse,
    ProductMediaDraft,
    product_draft_v2_from_snapshot,
)
from app.catalog.source_schemas import (
    CatalogSourceAssetResponse,
    CatalogSourceBundleListResponse,
    CatalogSourceBundleResponse,
    CatalogSourcePromotionRequest,
    CatalogSourcePromotionResponse,
)
from app.config import Settings
from app.models import (
    CatalogDraftRevision,
    CatalogProduct,
    CatalogSourceAsset,
    CatalogSourceBundle,
    ImageGenerationJob,
)
from app.schemas import IndexJobStatus
from app.services.auth.clerk import AuthenticatedPrincipal
from app.services.catalog_admin import (
    create_draft_from_v2_compatibility,
    draft_revision_version,
)
from app.services.image_analysis import ImageUploadError, validate_image_bytes
from app.services.product_images import _write_thumbnail


_MIME_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catalog source bundle not found.")


def _private_base(settings: Settings) -> Path:
    value = str(settings.catalog_source_output_dir or "").strip()
    if not value:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Catalog source storage is not configured.",
        )
    return Path(value).expanduser().resolve()


def _private_path(settings: Settings, storage_key: str) -> Path:
    base = _private_base(settings)
    candidate = (base / storage_key).resolve()
    if not candidate.is_relative_to(base):
        raise RuntimeError("Catalog source storage key escaped the private storage root.")
    return candidate


def _owner_directory(principal: AuthenticatedPrincipal) -> str:
    value = f"{principal.provider}\0{principal.provider_user_id}".encode()
    return hashlib.sha256(value).hexdigest()[:20]


def _validate_filename(filename: str | None) -> str:
    value = str(filename or "").strip()
    if not value:
        raise ImageUploadError("Uploaded image requires a filename.")
    if len(value) > 255:
        raise ImageUploadError("Uploaded image filename is too long.")
    if "/" in value or "\\" in value or value in {".", ".."}:
        raise ImageUploadError("Uploaded image filename must not contain a path.")
    return value


def _validated_source_image(
    image_bytes: bytes,
    content_type: str | None,
    *,
    settings: Settings,
) -> tuple[str, int, int]:
    try:
        mime_type = validate_image_bytes(
            image_bytes,
            content_type,
            max_bytes=max(1, int(settings.catalog_source_upload_max_bytes)),
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(image_bytes)) as image:
                width, height = image.size
    except Image.DecompressionBombError as exc:
        raise ImageUploadError("Uploaded image exceeds the safe pixel limit.") from exc
    except Image.DecompressionBombWarning as exc:
        raise ImageUploadError("Uploaded image exceeds the safe pixel limit.") from exc

    maximum_dimension = max(1, int(settings.catalog_source_max_dimension))
    maximum_pixels = max(1, int(settings.catalog_source_max_pixels))
    if width > maximum_dimension or height > maximum_dimension:
        raise ImageUploadError(
            f"Image dimensions must not exceed {maximum_dimension} pixels per side."
        )
    if width * height > maximum_pixels:
        raise ImageUploadError(
            f"Image pixel count must not exceed {maximum_pixels}."
        )
    return mime_type, width, height


def _write_private_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_bytes(content)
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)


def _owned_bundle(
    db: Session,
    *,
    bundle_id: str,
    principal: AuthenticatedPrincipal,
    lock: bool = False,
) -> CatalogSourceBundle:
    statement = (
        select(CatalogSourceBundle)
        .options(selectinload(CatalogSourceBundle.assets))
        .where(
            CatalogSourceBundle.id == bundle_id,
            CatalogSourceBundle.owner_provider == principal.provider,
            CatalogSourceBundle.owner_provider_user_id == principal.provider_user_id,
        )
    )
    if lock:
        statement = statement.with_for_update()
    bundle = db.scalar(statement)
    if bundle is None:
        raise _not_found()
    return bundle


def _bundle_asset(bundle: CatalogSourceBundle, asset_id: str) -> CatalogSourceAsset:
    asset = next((item for item in bundle.assets if item.id == asset_id), None)
    if asset is None:
        raise _not_found()
    return asset


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _asset_response(bundle: CatalogSourceBundle, asset: CatalogSourceAsset) -> CatalogSourceAssetResponse:
    return CatalogSourceAssetResponse(
        id=asset.id,
        display_order=asset.display_order,
        original_filename=asset.original_filename,
        content_type=asset.content_type,
        byte_size=asset.byte_size,
        width=asset.width,
        height=asset.height,
        checksum_sha256=asset.checksum_sha256,
        storage_provider=asset.storage_provider,
        status=asset.status,
        promoted_media_id=asset.promoted_media_id,
        preview_url=(
            f"/api/admin/catalog/source-bundles/{bundle.id}/assets/{asset.id}/preview"
        ),
        created_at=_utc(asset.created_at),
    )


def _bundle_response(bundle: CatalogSourceBundle) -> CatalogSourceBundleResponse:
    return CatalogSourceBundleResponse(
        id=bundle.id,
        title=bundle.title,
        catalog_product_id=bundle.catalog_product_id,
        draft_revision_id=bundle.draft_revision_id,
        status=bundle.status,
        assets=[
            _asset_response(bundle, asset)
            for asset in sorted(bundle.assets, key=lambda item: (item.display_order, item.id))
        ],
        created_at=_utc(bundle.created_at),
        updated_at=_utc(bundle.updated_at),
    )


def _validate_associations(
    db: Session,
    *,
    catalog_product_id: str | None,
    draft_revision_id: str | None,
    principal: AuthenticatedPrincipal,
) -> tuple[str | None, str | None]:
    product_id = str(catalog_product_id or "").strip() or None
    draft_id = str(draft_revision_id or "").strip() or None
    if draft_id:
        draft = db.get(CatalogDraftRevision, draft_id)
        if draft is None or draft.created_by != principal.provider_user_id:
            raise HTTPException(status_code=404, detail="Catalog draft revision not found.")
        if product_id and product_id != draft.catalog_product_id:
            raise HTTPException(
                status_code=422,
                detail="catalog_product_id must match the selected draft revision.",
            )
        product_id = draft.catalog_product_id
    elif product_id and db.get(CatalogProduct, product_id) is None:
        raise HTTPException(status_code=404, detail="Catalog product not found.")
    return product_id, draft_id


def create_source_bundle(
    db: Session,
    *,
    files: list[UploadFile],
    title: str,
    catalog_product_id: str | None,
    draft_revision_id: str | None,
    principal: AuthenticatedPrincipal,
    settings: Settings,
) -> CatalogSourceBundleResponse:
    maximum_assets = max(1, int(settings.catalog_source_max_assets_per_bundle))
    if not files:
        raise HTTPException(status_code=422, detail="At least one supplier image is required.")
    if len(files) > maximum_assets:
        raise HTTPException(
            status_code=422,
            detail=f"A source bundle supports at most {maximum_assets} images.",
        )
    normalized_title = " ".join(str(title or "Supplier source bundle").split())
    if not normalized_title or len(normalized_title) > 255:
        raise HTTPException(status_code=422, detail="Source bundle title is invalid.")
    product_id, draft_id = _validate_associations(
        db,
        catalog_product_id=catalog_product_id,
        draft_revision_id=draft_revision_id,
        principal=principal,
    )

    validated: list[tuple[str, bytes, str, int, int]] = []
    limit = max(1, int(settings.catalog_source_upload_max_bytes))
    for upload in files:
        filename = _validate_filename(upload.filename)
        content = upload.file.read(limit + 1)
        mime_type, width, height = _validated_source_image(
            content,
            upload.content_type,
            settings=settings,
        )
        validated.append((filename, content, mime_type, width, height))

    bundle_id = f"source_bundle_{uuid4().hex[:20]}"
    relative_directory = Path(_owner_directory(principal)) / bundle_id
    bundle_directory = _private_path(settings, relative_directory.as_posix())
    bundle = CatalogSourceBundle(
        id=bundle_id,
        owner_provider=principal.provider,
        owner_provider_user_id=principal.provider_user_id,
        title=normalized_title,
        catalog_product_id=product_id,
        draft_revision_id=draft_id,
        status="active",
    )
    db.add(bundle)
    try:
        for display_order, (filename, content, mime_type, width, height) in enumerate(validated):
            asset_id = f"source_asset_{uuid4().hex[:20]}"
            extension = _MIME_EXTENSIONS[mime_type]
            storage_key = (relative_directory / f"{asset_id}.{extension}").as_posix()
            preview_key = (relative_directory / f"{asset_id}-preview.jpg").as_posix()
            storage_path = _private_path(settings, storage_key)
            preview_path = _private_path(settings, preview_key)
            _write_private_file(storage_path, content)
            preview_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _write_thumbnail(
                content,
                preview_path,
                output_format="jpeg",
                size=max(96, min(int(settings.catalog_source_thumbnail_size), 1024)),
            )
            try:
                preview_path.chmod(0o600)
            except OSError:
                pass
            bundle.assets.append(
                CatalogSourceAsset(
                    id=asset_id,
                    display_order=display_order,
                    original_filename=filename,
                    content_type=mime_type,
                    byte_size=len(content),
                    width=width,
                    height=height,
                    checksum_sha256=hashlib.sha256(content).hexdigest(),
                    storage_provider="local_private",
                    storage_key=storage_key,
                    preview_storage_key=preview_key,
                    status="ready",
                )
            )
        db.commit()
    except Exception:
        db.rollback()
        shutil.rmtree(bundle_directory, ignore_errors=True)
        raise
    return _bundle_response(
        _owned_bundle(db, bundle_id=bundle.id, principal=principal)
    )


def list_source_bundles(
    db: Session,
    *,
    principal: AuthenticatedPrincipal,
) -> CatalogSourceBundleListResponse:
    bundles = db.scalars(
        select(CatalogSourceBundle)
        .options(selectinload(CatalogSourceBundle.assets))
        .where(
            CatalogSourceBundle.owner_provider == principal.provider,
            CatalogSourceBundle.owner_provider_user_id == principal.provider_user_id,
        )
        .order_by(CatalogSourceBundle.created_at.desc(), CatalogSourceBundle.id.desc())
    ).all()
    return CatalogSourceBundleListResponse(items=[_bundle_response(bundle) for bundle in bundles])


def get_source_bundle(
    db: Session,
    *,
    bundle_id: str,
    principal: AuthenticatedPrincipal,
) -> CatalogSourceBundleResponse:
    return _bundle_response(
        _owned_bundle(db, bundle_id=bundle_id, principal=principal)
    )


def get_source_preview_path(
    db: Session,
    *,
    bundle_id: str,
    asset_id: str,
    principal: AuthenticatedPrincipal,
    settings: Settings,
) -> Path:
    bundle = _owned_bundle(db, bundle_id=bundle_id, principal=principal)
    asset = _bundle_asset(bundle, asset_id)
    preview_path = _private_path(settings, asset.preview_storage_key)
    if not preview_path.is_file():
        raise HTTPException(status_code=404, detail="Catalog source preview not found.")
    return preview_path


def get_source_asset_path(
    db: Session,
    *,
    bundle_id: str,
    asset_id: str,
    principal: AuthenticatedPrincipal,
    settings: Settings,
) -> Path:
    """Return an authorized private source path for later analysis and media jobs."""
    bundle = _owned_bundle(
        db,
        bundle_id=bundle_id,
        principal=principal,
        lock=True,
    )
    asset = _bundle_asset(bundle, asset_id)
    source_path = _private_path(settings, asset.storage_key)
    if not source_path.is_file():
        raise HTTPException(status_code=404, detail="Catalog source image not found.")
    return source_path


def remove_source_asset(
    db: Session,
    *,
    bundle_id: str,
    asset_id: str,
    principal: AuthenticatedPrincipal,
    settings: Settings,
) -> None:
    bundle = _owned_bundle(db, bundle_id=bundle_id, principal=principal, lock=True)
    asset = _bundle_asset(bundle, asset_id)
    if asset.status == "promoted" or asset.promoted_media_id:
        raise _conflict("Promoted source assets must be retained for media lineage.")
    source_path = _private_path(settings, asset.storage_key)
    preview_path = _private_path(settings, asset.preview_storage_key)
    active_paths = {
        str(source_path),
        str(source_path.resolve()),
        asset.storage_key,
    }
    active_job = db.scalar(
        select(ImageGenerationJob.id).where(
            ImageGenerationJob.status.in_([
                IndexJobStatus.queued.value,
                IndexJobStatus.running.value,
            ]),
            ImageGenerationJob.source_image_path.in_(active_paths),
        )
    )
    if active_job:
        raise _conflict("Source assets used by an active image job cannot be removed.")

    try:
        for path in (source_path, preview_path):
            path.unlink(missing_ok=True)
    except OSError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Catalog source storage could not remove the managed image.",
        ) from exc

    db.delete(asset)
    db.flush()
    remaining = sorted(
        (item for item in bundle.assets if item is not asset),
        key=lambda item: (item.display_order, item.id),
    )
    for display_order, item in enumerate(remaining):
        item.display_order = display_order
    db.commit()


def _revision_response(revision: CatalogDraftRevision) -> DraftRevisionResponse:
    return DraftRevisionResponse(
        id=revision.id,
        product_id=revision.catalog_product_id,
        base_version=revision.base_version,
        status=revision.status,
        moderation_state=revision.moderation_state,
        created_by=revision.created_by,
        created_at=_utc(revision.created_at),
    )


def _write_public_image(content: bytes, destination: Path, mime_type: str) -> bool:
    existed = destination.exists()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with Image.open(BytesIO(content)) as image:
            save_format = {
                "image/jpeg": "JPEG",
                "image/png": "PNG",
                "image/webp": "WEBP",
            }[mime_type]
            if save_format == "JPEG":
                image = image.convert("RGB")
                image.save(temporary, format=save_format, quality=92, optimize=True)
            else:
                image.save(temporary, format=save_format, optimize=True)
        temporary.replace(destination)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return not existed


def _public_media_files(
    *,
    source_path: Path,
    preview_path: Path,
    asset: CatalogSourceAsset,
    media_id: str,
    settings: Settings,
) -> tuple[dict, list[Path]]:
    output_dir = Path(settings.product_image_output_dir).expanduser().resolve()
    extension = _MIME_EXTENSIONS[asset.content_type]
    primary_path = output_dir / f"{media_id}.{extension}"
    thumbnail_path = output_dir / f"{media_id}-thumbnail.jpg"
    source_content = source_path.read_bytes()
    created: list[Path] = []
    try:
        if _write_public_image(source_content, primary_path, asset.content_type):
            created.append(primary_path)
        if _write_public_image(preview_path.read_bytes(), thumbnail_path, "image/jpeg"):
            created.append(thumbnail_path)
    except (OSError, ValueError) as exc:
        for path in created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Product media storage could not create the approved derivative.",
        ) from exc
    url_prefix = f"{settings.public_base_url.rstrip('/')}/{settings.product_image_url_path.strip('/')}"
    return (
        {
            "thumbnail_url": f"{url_prefix}/{thumbnail_path.name}",
            "primary_url": f"{url_prefix}/{primary_path.name}",
            "detail_urls": [f"{url_prefix}/{primary_path.name}"],
            "file_path": str(primary_path),
            "thumbnail_path": str(thumbnail_path),
            "source": "supplier_asset",
            "approval_status": "approved",
        },
        created,
    )


def promote_source_asset(
    db: Session,
    *,
    bundle_id: str,
    asset_id: str,
    request: CatalogSourcePromotionRequest,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
    settings: Settings,
) -> CatalogSourcePromotionResponse:
    bundle = _owned_bundle(db, bundle_id=bundle_id, principal=principal, lock=True)
    asset = _bundle_asset(bundle, asset_id)
    if asset.status == "promoted" and asset.promoted_media_id and asset.promoted_draft_revision_id:
        promoted_revision = db.get(CatalogDraftRevision, asset.promoted_draft_revision_id)
        if promoted_revision is None or promoted_revision.created_by != principal.provider_user_id:
            raise _conflict("The promoted source asset no longer has an owned draft revision.")
        return CatalogSourcePromotionResponse(
            bundle_id=bundle.id,
            media_id=asset.promoted_media_id,
            asset=_asset_response(bundle, asset),
            draft=_revision_response(promoted_revision),
        )

    revision = db.get(CatalogDraftRevision, request.draft_id)
    if revision is None or revision.created_by != principal.provider_user_id:
        raise HTTPException(status_code=404, detail="Catalog draft revision not found.")
    if bundle.catalog_product_id and bundle.catalog_product_id != revision.catalog_product_id:
        raise _conflict("The source bundle belongs to a different catalog product.")
    actual_version = draft_revision_version(db, revision)
    if actual_version != request.expected_draft_version:
        raise _conflict(
            f"Expected catalog draft version {request.expected_draft_version}, but current version is {actual_version}."
        )

    product = product_draft_v2_from_snapshot(revision.snapshot_json)
    media_id = "media_" + hashlib.sha256(
        f"catalog-source-media\0{asset.id}".encode()
    ).hexdigest()[:24]
    if any(item.media_id == media_id for item in product.media):
        raise _conflict("The source asset is already present in this catalog draft.")
    source_path = _private_path(settings, asset.storage_key)
    preview_path = _private_path(settings, asset.preview_storage_key)
    if not source_path.is_file() or not preview_path.is_file():
        raise HTTPException(status_code=404, detail="Catalog source image is unavailable.")
    image_set, newly_created_paths = _public_media_files(
        source_path=source_path,
        preview_path=preview_path,
        asset=asset,
        media_id=media_id,
        settings=settings,
    )
    product.media.append(
        ProductMediaDraft(
            media_id=media_id,
            role="core" if not product.media else "variation",
            intent="manual",
            image_set=image_set,
            approval_status="approved",
            display_order=len(product.media),
            provenance={
                "source": "supplier_asset",
                "source_bundle_id": bundle.id,
                "source_asset_id": asset.id,
            },
        )
    )
    draft_committed = False
    try:
        draft_response, _ = create_draft_from_v2_compatibility(
            db,
            DraftMutationRequestV2(
                expected_version=revision.base_version,
                current_draft_id=revision.id,
                expected_draft_version=request.expected_draft_version,
                moderation_state=revision.moderation_state,
                product=product,
            ),
            idempotency_key=idempotency_key,
            principal=principal,
            path_product_id=revision.catalog_product_id,
        )
        draft_committed = True
        asset.status = "promoted"
        asset.promoted_media_id = media_id
        asset.promoted_draft_revision_id = draft_response.id
        bundle.catalog_product_id = revision.catalog_product_id
        bundle.draft_revision_id = draft_response.id
        db.commit()
    except Exception:
        db.rollback()
        if not draft_committed:
            for path in newly_created_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        raise
    refreshed_bundle = _owned_bundle(db, bundle_id=bundle.id, principal=principal)
    refreshed_asset = _bundle_asset(refreshed_bundle, asset.id)
    return CatalogSourcePromotionResponse(
        bundle_id=bundle.id,
        media_id=media_id,
        asset=_asset_response(refreshed_bundle, refreshed_asset),
        draft=draft_response,
    )
