from __future__ import annotations

import csv
import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CatalogDraftRevision,
    CatalogProduct,
    ChatSession,
    CustomerAuthIdentity,
    CustomerCommunication,
    ImageGenerationJob,
    IndexJob,
    MerchStrategyStoreOverride,
    Product,
    ProductVariant,
    SyntheticRun,
)
from app.schemas import ImageGenerationJobRequest
from app.services.image_jobs import enqueue_image_generation_job
from app.services.index_jobs import enqueue_index_job
from app.services.loader import (
    SYNTHETIC_LOAD_ORDER,
    assert_synthetic_tables_empty,
    current_loaded_counts,
    load_entity_csv,
    normalize_loaded_catalog,
    parse_entity_csv,
    read_generated_counts,
    reset_synthetic_tables,
)


@dataclass(frozen=True)
class CatalogCutoverPreflight:
    run_id: str
    current_run_id: str | None
    generated_counts: dict[str, int]
    family_count: int
    variant_color_count: int
    blockers: tuple[str, ...] = ()
    blocking_product_ids: tuple[str, ...] = ()

    @property
    def safe_to_execute(self) -> bool:
        return not self.blockers and not self.blocking_product_ids


@dataclass(frozen=True)
class CatalogCutoverResult:
    run_id: str
    previous_run_id: str | None
    dry_run: bool
    generated_counts: dict[str, int]
    loaded_counts: dict[str, int] = field(default_factory=dict)
    image_job_ids: tuple[str, ...] = ()
    index_job_id: str | None = None
    rollback_command: str | None = None


class CatalogCutoverBlockedError(ValueError):
    def __init__(self, preflight: CatalogCutoverPreflight):
        details = [*preflight.blockers]
        if preflight.blocking_product_ids:
            details.append("blocking product IDs: " + ", ".join(preflight.blocking_product_ids))
        super().__init__("Catalog cutover blocked: " + "; ".join(details))
        self.preflight = preflight


def _csv_row_count(path: Path) -> int:
    if not path.exists():
        return -1
    with path.open("r", newline="", encoding="utf-8") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def _loaded_run_ids(db: Session) -> list[str]:
    return list(db.scalars(select(Product.seed_run_id).distinct().order_by(Product.seed_run_id)).all())


def _authored_product_ids(db: Session) -> list[str]:
    draft_ids = set(db.scalars(select(CatalogDraftRevision.catalog_product_id).distinct()).all())
    for product in db.scalars(select(CatalogProduct)).all():
        metadata = product.metadata_json if isinstance(product.metadata_json, dict) else {}
        if metadata.get("source") != "legacy_products":
            draft_ids.add(product.id)
    return sorted(draft_ids)


def _durable_state_blockers(db: Session) -> list[str]:
    checks = (
        ("customer auth identities", select(func.count()).select_from(CustomerAuthIdentity)),
        ("customer communications", select(func.count()).select_from(CustomerCommunication)),
        (
            "customer-linked chat sessions",
            select(func.count()).select_from(ChatSession).where(ChatSession.customer_id.is_not(None)),
        ),
        ("store strategy overrides", select(func.count()).select_from(MerchStrategyStoreOverride)),
    )
    blockers = []
    for label, statement in checks:
        count = int(db.scalar(statement) or 0)
        if count:
            blockers.append(f"{label} must be exported or explicitly cleared before cutover ({count} rows)")
    return blockers


def _product_family_stats(path: Path) -> tuple[int, int, list[str]]:
    blockers: list[str] = []
    families: dict[str, dict[str, set[str]]] = {}
    if not path.exists():
        return 0, 0, ["products.csv is missing"]

    with path.open("r", newline="", encoding="utf-8") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            try:
                metadata = json.loads(row.get("metadata_json") or "{}")
            except json.JSONDecodeError:
                blockers.append(f"products.csv row {row_number} has invalid metadata_json")
                continue
            style_code = str(metadata.get("style_code") or "").strip()
            if not style_code:
                blockers.append(f"products.csv row {row_number} has no style_code")
                continue
            family = families.setdefault(
                style_code,
                {
                    "title": set(),
                    "brand": set(),
                    "category": set(),
                    "material": set(),
                    "gender": set(),
                    "season": set(),
                    "color": set(),
                },
            )
            for field_name in family:
                family[field_name].add(str(row.get(field_name) or "").strip().casefold())

    stable_fields = ("title", "brand", "category", "material", "gender", "season")
    for style_code, family in sorted(families.items()):
        drifted = [field_name for field_name in stable_fields if len(family[field_name]) > 1]
        if drifted:
            blockers.append(f"{style_code} changes stable fields: {', '.join(drifted)}")
    return len(families), sum(len(family["color"]) for family in families.values()), blockers


def preflight_catalog_cutover(
    db: Session,
    *,
    run_id: str,
    data_dir: Path,
    require_coherent_families: bool = True,
) -> CatalogCutoverPreflight:
    blockers: list[str] = []
    run = db.get(SyntheticRun, run_id)
    if run is None:
        blockers.append(f"synthetic run {run_id!r} does not exist")
    blockers.extend(_durable_state_blockers(db))

    data_dir = data_dir.resolve()
    run_id_safe = bool(run_id) and Path(run_id).name == run_id and run_id not in {".", ".."}
    if not run_id_safe:
        blockers.append("run_id must be one path-safe name")
        run_dir = data_dir / "__invalid_run_id__"
    else:
        run_dir = data_dir / run_id
    try:
        generated_counts = read_generated_counts(data_dir, run_id) if run_id_safe else {}
    except (OSError, json.JSONDecodeError) as exc:
        generated_counts = {}
        blockers.append(f"manifest.json cannot be read: {exc}")
    if not generated_counts:
        blockers.append("manifest.json is missing or has no row counts")

    for entity in SYNTHETIC_LOAD_ORDER:
        path = run_dir / f"{entity}.csv"
        actual = _csv_row_count(path)
        expected = generated_counts.get(entity)
        if actual < 0:
            blockers.append(f"{entity}.csv is missing")
        elif expected is None:
            blockers.append(f"manifest has no {entity} count")
        elif actual != expected:
            blockers.append(f"{entity}.csv has {actual} rows; manifest expects {expected}")
        elif path.exists():
            try:
                parse_entity_csv(path)
            except Exception as exc:
                blockers.append(f"{entity}.csv cannot be parsed: {exc}")

    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("run_id") != run_id:
                blockers.append("manifest run_id does not match the requested run")
        except json.JSONDecodeError:
            blockers.append("manifest.json is not valid JSON")

    family_count, variant_color_count, family_blockers = _product_family_stats(run_dir / "products.csv")
    blockers.extend(family_blockers)
    if (
        require_coherent_families
        and generated_counts.get("products", 0) > 1
        and variant_color_count <= family_count
    ):
        blockers.append("replacement catalog contains no multi-color product family")

    loaded_run_ids = _loaded_run_ids(db)
    current_run_id = loaded_run_ids[0] if len(loaded_run_ids) == 1 else None
    if len(loaded_run_ids) > 1:
        blockers.append("loaded legacy products span multiple synthetic runs")
    if loaded_run_ids:
        active_job_ids = list(
            db.scalars(
                select(ImageGenerationJob.id)
                .where(
                    ImageGenerationJob.run_id.in_(loaded_run_ids),
                    ImageGenerationJob.status.in_(["queued", "running"]),
                )
                .order_by(ImageGenerationJob.id)
            ).all()
        )
        if active_job_ids:
            blockers.append("active legacy image jobs must finish or be cancelled: " + ", ".join(active_job_ids))
        active_index_job_ids = list(
            db.scalars(
                select(IndexJob.id)
                .where(
                    IndexJob.run_id.in_(loaded_run_ids),
                    IndexJob.status.in_(["queued", "running"]),
                )
                .order_by(IndexJob.id)
            ).all()
        )
        if active_index_job_ids:
            blockers.append("active index jobs must finish or be cancelled: " + ", ".join(active_index_job_ids))

    return CatalogCutoverPreflight(
        run_id=run_id,
        current_run_id=current_run_id,
        generated_counts=generated_counts,
        family_count=family_count,
        variant_color_count=variant_color_count,
        blockers=tuple(sorted(set(blockers))),
        blocking_product_ids=tuple(_authored_product_ids(db)),
    )


def _rollback_command(previous_run_id: str | None, data_dir: Path) -> str | None:
    if previous_run_id is None:
        return None
    return (
        "python scripts/cutover_synthetic_catalog.py "
        f"--run-id {shlex.quote(previous_run_id)} "
        f"--data-dir {shlex.quote(str(data_dir))} --execute --allow-legacy-families"
    )


def _enqueue_variant_image_jobs(db: Session, *, run_id: str) -> tuple[str, ...]:
    groups = db.execute(
        select(CatalogProduct.category, CatalogProduct.brand, func.count(ProductVariant.id))
        .join(ProductVariant, ProductVariant.catalog_product_id == CatalogProduct.id)
        .where(CatalogProduct.seed_run_id == run_id)
        .group_by(CatalogProduct.category, CatalogProduct.brand)
        .order_by(CatalogProduct.category, CatalogProduct.brand)
    ).all()
    job_ids = []
    for category, brand, count in groups:
        if int(count) <= 500:
            requests = [
                ImageGenerationJobRequest(
                    run_id=run_id,
                    category=category,
                    brand=brand,
                    limit=max(1, int(count)),
                    overwrite=True,
                    missing_images_only=False,
                )
            ]
        else:
            products = db.execute(
                select(CatalogProduct.id, func.count(ProductVariant.id))
                .join(ProductVariant, ProductVariant.catalog_product_id == CatalogProduct.id)
                .where(
                    CatalogProduct.seed_run_id == run_id,
                    CatalogProduct.category == category,
                    CatalogProduct.brand == brand,
                )
                .group_by(CatalogProduct.id)
                .order_by(CatalogProduct.id)
            ).all()
            requests = [
                ImageGenerationJobRequest(
                    run_id=run_id,
                    product_id=product_id,
                    limit=max(1, int(product_variant_count)),
                    overwrite=True,
                    missing_images_only=False,
                )
                for product_id, product_variant_count in products
            ]
        for request in requests:
            job_ids.append(enqueue_image_generation_job(db, request).id)
    return tuple(job_ids)


def cutover_synthetic_catalog(
    db: Session,
    *,
    run_id: str,
    data_dir: Path,
    execute: bool = False,
    enqueue_images: bool = False,
    enqueue_index: bool = False,
    require_coherent_families: bool = True,
) -> CatalogCutoverResult:
    preflight = preflight_catalog_cutover(
        db,
        run_id=run_id,
        data_dir=data_dir,
        require_coherent_families=require_coherent_families,
    )
    if not preflight.safe_to_execute:
        raise CatalogCutoverBlockedError(preflight)

    rollback_command = _rollback_command(preflight.current_run_id, data_dir)
    if not execute:
        return CatalogCutoverResult(
            run_id=run_id,
            previous_run_id=preflight.current_run_id,
            dry_run=True,
            generated_counts=preflight.generated_counts,
            rollback_command=rollback_command,
        )

    try:
        reset_synthetic_tables(
            db,
            detach_image_job_targets_for_run=preflight.current_run_id,
        )
        assert_synthetic_tables_empty(db)
        for entity in SYNTHETIC_LOAD_ORDER:
            load_entity_csv(db, run_id, data_dir, entity)
        normalize_loaded_catalog(db, run_id)

        loaded_counts = current_loaded_counts(db, run_id)
        mismatches = {
            entity: (preflight.generated_counts.get(entity), loaded_counts.get(entity))
            for entity in SYNTHETIC_LOAD_ORDER
            if preflight.generated_counts.get(entity) != loaded_counts.get(entity)
        }
        normalized_expectations = {
            "catalog_products": preflight.family_count,
            "product_variants": preflight.variant_color_count,
        }
        mismatches.update(
            {
                entity: (expected, loaded_counts.get(entity))
                for entity, expected in normalized_expectations.items()
                if expected != loaded_counts.get(entity)
            }
        )
        if mismatches:
            raise ValueError(f"post-cutover counts do not match preflight: {mismatches}")

        run = db.get(SyntheticRun, run_id)
        if run is not None:
            run.status = "loaded"
            db.add(run)
            db.commit()
    except Exception as exc:
        db.rollback()
        raise RuntimeError(
            f"Catalog cutover failed after execution began: {exc}. "
            f"Rollback with: {rollback_command or 'no previous run is available'}"
        ) from exc

    image_job_ids = _enqueue_variant_image_jobs(db, run_id=run_id) if enqueue_images else ()
    index_job_id = enqueue_index_job(db, run_id).id if enqueue_index else None
    return CatalogCutoverResult(
        run_id=run_id,
        previous_run_id=preflight.current_run_id,
        dry_run=False,
        generated_counts=preflight.generated_counts,
        loaded_counts=loaded_counts,
        image_job_ids=image_job_ids,
        index_job_id=index_job_id,
        rollback_command=rollback_command,
    )
