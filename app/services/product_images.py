from __future__ import annotations

import base64
from io import BytesIO
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import CatalogProduct, Product, ProductVariant, StoreInventory

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


PLACEHOLDER_IMAGE_HOST = "fashion.example/images/"


@dataclass(frozen=True)
class ProductImageGenerationOptions:
    model: str
    size: str
    quality: str
    output_format: str
    output_dir: Path
    public_base_url: str
    url_path: str
    detail_count: int = 3
    thumbnail_size: int = 320
    overwrite: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class ProductImageGenerationResult:
    product_id: str
    variant_id: str | None
    title: str
    status: str
    image_link: str | None = None
    thumbnail_link: str | None = None
    detail_links: list[str] | None = None
    file_path: str | None = None
    thumbnail_path: str | None = None
    detail_paths: list[str] | None = None
    prompt: str | None = None
    error: str | None = None


def product_image_options(
    *,
    model: str | None = None,
    size: str | None = None,
    quality: str | None = None,
    output_format: str | None = None,
    output_dir: str | Path | None = None,
    public_base_url: str | None = None,
    url_path: str | None = None,
    detail_count: int | None = None,
    thumbnail_size: int | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> ProductImageGenerationOptions:
    settings = get_settings()
    return ProductImageGenerationOptions(
        model=model or settings.product_image_model,
        size=size or settings.product_image_size,
        quality=quality or settings.product_image_quality,
        output_format=(output_format or settings.product_image_output_format).strip().lower(),
        output_dir=Path(output_dir or settings.product_image_output_dir),
        public_base_url=(public_base_url or settings.public_base_url).rstrip("/"),
        url_path=(url_path or settings.product_image_url_path).strip() or "/product-images",
        detail_count=max(1, min(int(detail_count or settings.product_image_detail_count), 10)),
        thumbnail_size=max(96, min(int(thumbnail_size or settings.product_image_thumbnail_size), 1024)),
        overwrite=overwrite,
        dry_run=dry_run,
    )


def is_placeholder_image_link(image_link: str | None) -> bool:
    value = str(image_link or "").strip()
    return not value or PLACEHOLDER_IMAGE_HOST in value


def public_product_image_url(product: Product) -> str | None:
    image_link = str(product.image_link or "").strip()
    if not image_link or is_placeholder_image_link(image_link):
        return None
    return image_link


def product_variant_image_set(variant: ProductVariant) -> dict | None:
    raw = variant.image_set if isinstance(variant.image_set, dict) else {}
    if raw:
        thumbnail_url = str(raw.get("thumbnail_url") or "").strip() or None
        primary_url = str(raw.get("primary_url") or "").strip() or None
        detail_urls = [
            str(value).strip()
            for value in raw.get("detail_urls", [])
            if str(value or "").strip()
        ]
        if thumbnail_url or primary_url or detail_urls:
            return {
                "thumbnail_url": thumbnail_url or primary_url or (detail_urls[0] if detail_urls else None),
                "primary_url": primary_url or (detail_urls[0] if detail_urls else thumbnail_url),
                "detail_urls": detail_urls or ([primary_url] if primary_url else []),
            }

    image_link = str(variant.image_link or "").strip()
    if image_link and not is_placeholder_image_link(image_link):
        return {
            "thumbnail_url": image_link,
            "primary_url": image_link,
            "detail_urls": [image_link],
        }
    return None


def product_image_set(product: Product) -> dict | None:
    metadata = product.metadata_json if isinstance(product.metadata_json, dict) else {}
    raw = metadata.get("image_set")
    if isinstance(raw, dict):
        thumbnail_url = str(raw.get("thumbnail_url") or "").strip() or None
        primary_url = str(raw.get("primary_url") or "").strip() or None
        detail_urls = [
            str(value).strip()
            for value in raw.get("detail_urls", [])
            if str(value or "").strip()
        ]
        if thumbnail_url or primary_url or detail_urls:
            return {
                "thumbnail_url": thumbnail_url or primary_url or (detail_urls[0] if detail_urls else None),
                "primary_url": primary_url or (detail_urls[0] if detail_urls else thumbnail_url),
                "detail_urls": detail_urls or ([primary_url] if primary_url else []),
            }

    primary_url = public_product_image_url(product)
    if not primary_url:
        return None
    return {
        "thumbnail_url": primary_url,
        "primary_url": primary_url,
        "detail_urls": [primary_url],
    }


def build_product_image_prompt(product: Product) -> str:
    attributes = [
        ("Category", product.category),
        ("Brand", product.brand),
        ("Color", product.color),
        ("Size", product.size),
        ("Material", product.material),
        ("Gender", product.gender),
        ("Season", product.season),
    ]
    attribute_text = "\n".join(f"- {name}: {value}" for name, value in attributes if value)
    return "\n".join(
        [
            "Create a clean ecommerce product image for a luxury retail catalog.",
            "Show a single product as the hero subject on a neutral studio background.",
            "Use realistic lighting, accurate materials, and a premium editorial product-photography style.",
            "Do not include readable text, logos, watermarks, price tags, hang tags, mannequins, people, or extra props.",
            "Do not copy any real brand logo or protected trade dress; represent only the product type and materials.",
            "",
            f"Product title: {product.title}",
            f"Description: {product.description}",
            attribute_text,
        ]
    ).strip()


def build_variant_image_prompt(product: CatalogProduct, variant: ProductVariant) -> str:
    attributes = [
        ("Category", product.category),
        ("Brand", product.brand),
        ("Color", variant.color),
        ("Material", variant.material),
        ("Gender", variant.gender),
        ("Season", variant.season),
    ]
    attribute_text = "\n".join(f"- {name}: {value}" for name, value in attributes if value)
    return "\n".join(
        [
            "Create a clean ecommerce product image for a luxury retail catalog.",
            "Show a single product variant as the hero subject on a neutral studio background.",
            "Use realistic lighting, accurate materials, and a premium editorial product-photography style.",
            "Do not include readable text, logos, watermarks, price tags, hang tags, mannequins, people, or extra props.",
            "Do not copy any real brand logo or protected trade dress; represent only the product type and materials.",
            "",
            f"Product title: {product.title}",
            f"Description: {product.description}",
            attribute_text,
        ]
    ).strip()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug[:80] or "product"


def product_image_filename(product: Product, output_format: str, *, variant: str = "detail", index: int = 1) -> str:
    extension = "jpg" if output_format == "jpeg" else output_format
    suffix = "thumb" if variant == "thumbnail" else f"detail-{max(1, index)}"
    return f"{product.id}-{_slug(product.title)}-{suffix}.{extension}"


def variant_image_filename(
    product: CatalogProduct,
    variant: ProductVariant,
    output_format: str,
    *,
    image_variant: str = "detail",
    index: int = 1,
) -> str:
    extension = "jpg" if output_format == "jpeg" else output_format
    suffix = "thumb" if image_variant == "thumbnail" else f"detail-{max(1, index)}"
    return f"{variant.id}-{_slug(product.title)}-{suffix}.{extension}"


def product_image_link(
    product: Product,
    options: ProductImageGenerationOptions,
    *,
    variant: str = "detail",
    index: int = 1,
) -> str:
    url_path = "/" + options.url_path.strip("/")
    filename = product_image_filename(product, options.output_format, variant=variant, index=index)
    return f"{options.public_base_url}{url_path}/{filename}"


def variant_image_link(
    product: CatalogProduct,
    variant: ProductVariant,
    options: ProductImageGenerationOptions,
    *,
    image_variant: str = "detail",
    index: int = 1,
) -> str:
    url_path = "/" + options.url_path.strip("/")
    filename = variant_image_filename(product, variant, options.output_format, image_variant=image_variant, index=index)
    return f"{options.public_base_url}{url_path}/{filename}"


def _write_thumbnail(source_bytes: bytes, output_path: Path, *, output_format: str, size: int) -> None:
    try:
        from PIL import Image
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("Pillow is required to generate product thumbnails.") from exc

    with Image.open(BytesIO(source_bytes)) as image:
        image.thumbnail((size, size))
        if output_format == "jpeg":
            image = image.convert("RGB")
            image.save(output_path, format="JPEG", quality=85, optimize=True)
        else:
            image.save(output_path, format=output_format.upper(), optimize=True)


def query_variants_for_image_generation(
    db: Session,
    *,
    run_id: str | None = None,
    store_id: str | None = None,
    product_ids: list[str] | None = None,
    variant_ids: list[str] | None = None,
    category: str | None = None,
    brand: str | None = None,
    limit: int = 20,
) -> list[ProductVariant]:
    query = select(ProductVariant.id).join(CatalogProduct, CatalogProduct.id == ProductVariant.catalog_product_id)
    if run_id:
        query = query.where(ProductVariant.seed_run_id == run_id)
    if store_id:
        query = query.join(StoreInventory, StoreInventory.variant_id == ProductVariant.id).where(StoreInventory.store_id == store_id)
    if product_ids:
        query = query.where(ProductVariant.catalog_product_id.in_(product_ids))
    if variant_ids:
        query = query.where(ProductVariant.id.in_(variant_ids))
    if category:
        query = query.where(CatalogProduct.category == category)
    if brand:
        query = query.where(CatalogProduct.brand == brand)
    variant_ids = db.scalars(query.distinct().order_by(ProductVariant.id.asc()).limit(max(1, min(limit, 500)))).all()
    if not variant_ids:
        return []
    variant_map = {
        variant.id: variant
        for variant in db.scalars(select(ProductVariant).where(ProductVariant.id.in_(variant_ids))).all()
    }
    return [variant_map[variant_id] for variant_id in variant_ids if variant_id in variant_map]


def query_products_for_image_generation(
    db: Session,
    *,
    run_id: str | None = None,
    store_id: str | None = None,
    product_ids: list[str] | None = None,
    category: str | None = None,
    brand: str | None = None,
    limit: int = 20,
) -> list[Product]:
    query = select(Product)
    if run_id:
        query = query.where(Product.seed_run_id == run_id)
    if store_id:
        query = query.where(Product.store_id == store_id)
    if product_ids:
        query = query.where(Product.id.in_(product_ids))
    if category:
        query = query.where(Product.category == category)
    if brand:
        query = query.where(Product.brand == brand)
    return db.scalars(query.order_by(Product.id.asc()).limit(max(1, min(limit, 500)))).all()


class ProductImageGenerator:
    def __init__(self, options: ProductImageGenerationOptions, client=None) -> None:
        self.options = options
        self.client = client
        if self.client is None and not options.dry_run:
            if OpenAI is None:
                raise RuntimeError("The openai package is not available.")
            self.client = OpenAI()

    def generate_for_variant(self, db: Session, variant: ProductVariant) -> ProductImageGenerationResult:
        product = db.get(CatalogProduct, variant.catalog_product_id)
        if not product:
            return ProductImageGenerationResult(
                product_id=variant.catalog_product_id,
                variant_id=variant.id,
                title=variant.id,
                status="failed",
                error="catalog_product_id not found",
            )

        existing_image_set = product_variant_image_set(variant)
        if not self.options.overwrite and existing_image_set:
            return ProductImageGenerationResult(
                product_id=product.id,
                variant_id=variant.id,
                title=product.title,
                status="skipped_existing_image",
                image_link=existing_image_set.get("primary_url"),
                thumbnail_link=existing_image_set.get("thumbnail_url"),
                detail_links=existing_image_set.get("detail_urls"),
            )

        prompt = build_variant_image_prompt(product, variant)
        detail_paths = [
            self.options.output_dir
            / variant_image_filename(product, variant, self.options.output_format, image_variant="detail", index=index)
            for index in range(1, self.options.detail_count + 1)
        ]
        detail_links = [
            variant_image_link(product, variant, self.options, image_variant="detail", index=index)
            for index in range(1, self.options.detail_count + 1)
        ]
        thumbnail_path = self.options.output_dir / variant_image_filename(
            product,
            variant,
            self.options.output_format,
            image_variant="thumbnail",
        )
        thumbnail_link = variant_image_link(product, variant, self.options, image_variant="thumbnail")
        image_link = detail_links[0]

        if self.options.dry_run:
            return ProductImageGenerationResult(
                product_id=product.id,
                variant_id=variant.id,
                title=product.title,
                status="dry_run",
                image_link=image_link,
                thumbnail_link=thumbnail_link,
                detail_links=detail_links,
                file_path=str(detail_paths[0]),
                thumbnail_path=str(thumbnail_path),
                detail_paths=[str(path) for path in detail_paths],
                prompt=prompt,
            )

        try:
            assert self.client is not None
            response = self.client.images.generate(
                model=self.options.model,
                prompt=prompt,
                size=self.options.size,
                quality=self.options.quality,
                output_format=self.options.output_format,
                response_format="b64_json",
                n=self.options.detail_count,
            )
            image_payloads = [item.b64_json for item in response.data if item.b64_json]
            if not image_payloads:
                raise RuntimeError("OpenAI image response did not include b64_json data.")
            self.options.output_dir.mkdir(parents=True, exist_ok=True)
            image_bytes_by_path: list[tuple[Path, bytes]] = []
            for file_path, image_base64 in zip(detail_paths, image_payloads, strict=False):
                image_bytes = base64.b64decode(image_base64)
                file_path.write_bytes(image_bytes)
                image_bytes_by_path.append((file_path, image_bytes))
            if not image_bytes_by_path:
                raise RuntimeError("No image files were written.")
            _write_thumbnail(
                image_bytes_by_path[0][1],
                thumbnail_path,
                output_format=self.options.output_format,
                size=self.options.thumbnail_size,
            )
            written_detail_paths = [path for path, _ in image_bytes_by_path]
            written_detail_links = detail_links[: len(written_detail_paths)]
            image_set = {
                "thumbnail_url": thumbnail_link,
                "primary_url": image_link,
                "detail_urls": written_detail_links,
                "generated_by": self.options.model,
                "size": self.options.size,
                "quality": self.options.quality,
                "output_format": self.options.output_format,
            }
            variant.image_link = image_link
            variant.image_set = image_set
            db.add(variant)
            db.commit()
            return ProductImageGenerationResult(
                product_id=product.id,
                variant_id=variant.id,
                title=product.title,
                status="generated",
                image_link=image_link,
                thumbnail_link=thumbnail_link,
                detail_links=written_detail_links,
                file_path=str(written_detail_paths[0]),
                thumbnail_path=str(thumbnail_path),
                detail_paths=[str(path) for path in written_detail_paths],
                prompt=prompt,
            )
        except Exception as exc:
            db.rollback()
            return ProductImageGenerationResult(
                product_id=product.id,
                variant_id=variant.id,
                title=product.title,
                status="failed",
                image_link=image_link,
                thumbnail_link=thumbnail_link,
                detail_links=detail_links,
                file_path=str(detail_paths[0]),
                thumbnail_path=str(thumbnail_path),
                detail_paths=[str(path) for path in detail_paths],
                prompt=prompt,
                error=str(exc)[:1000],
            )

    def generate_for_product(self, db: Session, product: Product) -> ProductImageGenerationResult:
        if not self.options.overwrite and not is_placeholder_image_link(product.image_link):
            return ProductImageGenerationResult(
                product_id=product.id,
                variant_id=None,
                title=product.title,
                status="skipped_existing_image",
                image_link=product.image_link,
                thumbnail_link=(product_image_set(product) or {}).get("thumbnail_url"),
                detail_links=(product_image_set(product) or {}).get("detail_urls"),
            )

        prompt = build_product_image_prompt(product)
        detail_paths = [
            self.options.output_dir
            / product_image_filename(product, self.options.output_format, variant="detail", index=index)
            for index in range(1, self.options.detail_count + 1)
        ]
        detail_links = [
            product_image_link(product, self.options, variant="detail", index=index)
            for index in range(1, self.options.detail_count + 1)
        ]
        thumbnail_path = self.options.output_dir / product_image_filename(
            product,
            self.options.output_format,
            variant="thumbnail",
        )
        thumbnail_link = product_image_link(product, self.options, variant="thumbnail")
        image_link = detail_links[0]

        if self.options.dry_run:
            return ProductImageGenerationResult(
                product_id=product.id,
                variant_id=None,
                title=product.title,
                status="dry_run",
                image_link=image_link,
                thumbnail_link=thumbnail_link,
                detail_links=detail_links,
                file_path=str(detail_paths[0]),
                thumbnail_path=str(thumbnail_path),
                detail_paths=[str(path) for path in detail_paths],
                prompt=prompt,
            )

        try:
            assert self.client is not None
            response = self.client.images.generate(
                model=self.options.model,
                prompt=prompt,
                size=self.options.size,
                quality=self.options.quality,
                output_format=self.options.output_format,
                response_format="b64_json",
                n=self.options.detail_count,
            )
            image_payloads = [item.b64_json for item in response.data if item.b64_json]
            if not image_payloads:
                raise RuntimeError("OpenAI image response did not include b64_json data.")
            self.options.output_dir.mkdir(parents=True, exist_ok=True)
            image_bytes_by_path: list[tuple[Path, bytes]] = []
            for file_path, image_base64 in zip(detail_paths, image_payloads, strict=False):
                image_bytes = base64.b64decode(image_base64)
                file_path.write_bytes(image_bytes)
                image_bytes_by_path.append((file_path, image_bytes))
            if not image_bytes_by_path:
                raise RuntimeError("No image files were written.")
            _write_thumbnail(
                image_bytes_by_path[0][1],
                thumbnail_path,
                output_format=self.options.output_format,
                size=self.options.thumbnail_size,
            )
            written_detail_paths = [path for path, _ in image_bytes_by_path]
            written_detail_links = detail_links[: len(written_detail_paths)]
            metadata = dict(product.metadata_json or {})
            metadata["image_set"] = {
                "thumbnail_url": thumbnail_link,
                "primary_url": image_link,
                "detail_urls": written_detail_links,
                "generated_by": self.options.model,
                "size": self.options.size,
                "quality": self.options.quality,
                "output_format": self.options.output_format,
            }
            product.image_link = image_link
            product.metadata_json = metadata
            db.add(product)
            db.commit()
            return ProductImageGenerationResult(
                product_id=product.id,
                variant_id=None,
                title=product.title,
                status="generated",
                image_link=image_link,
                thumbnail_link=thumbnail_link,
                detail_links=written_detail_links,
                file_path=str(written_detail_paths[0]),
                thumbnail_path=str(thumbnail_path),
                detail_paths=[str(path) for path in written_detail_paths],
                prompt=prompt,
            )
        except Exception as exc:
            db.rollback()
            return ProductImageGenerationResult(
                product_id=product.id,
                variant_id=None,
                title=product.title,
                status="failed",
                image_link=image_link,
                thumbnail_link=thumbnail_link,
                detail_links=detail_links,
                file_path=str(detail_paths[0]),
                thumbnail_path=str(thumbnail_path),
                detail_paths=[str(path) for path in detail_paths],
                prompt=prompt,
                error=str(exc)[:1000],
            )
