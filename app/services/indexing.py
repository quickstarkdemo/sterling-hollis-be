from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.models import CatalogProduct, CatalogProductEmbedding, Product, ProductEmbedding
from app.services.embeddings import EmbeddingService
from app.services.pinecone_service import PineconeService


def build_product_embedding_text(product: Product) -> str:
    attrs = [product.category, product.brand, product.color or "", product.material or "", product.gender or "", product.season or ""]
    attrs_text = " | ".join(a for a in attrs if a)
    return f"{product.title}\n{product.description}\n{attrs_text}"


def _unique_variant_values(product: CatalogProduct, attr: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for variant in product.variants:
        value = str(getattr(variant, attr, "") or "").strip()
        key = value.lower()
        if not value or key in seen:
            continue
        values.append(value)
        seen.add(key)
    return values


def build_catalog_product_embedding_text(product: CatalogProduct) -> str:
    parts = [
        product.title,
        product.description,
        f"Brand: {product.brand}",
        f"Category: {product.category}",
    ]
    for label, attr in [
        ("Colors", "color"),
        ("Materials", "material"),
        ("Genders", "gender"),
        ("Seasons", "season"),
    ]:
        values = _unique_variant_values(product, attr)
        if values:
            parts.append(f"{label}: {', '.join(values)}")
    return "\n".join(part for part in parts if part)


def _index_catalog_products_for_run(
    db: Session,
    run_id: str,
    *,
    batch_size: int,
    embedding_service: EmbeddingService,
    pinecone: PineconeService,
) -> dict[str, int]:
    settings = get_settings()
    namespace = settings.pinecone_catalog_namespace

    db.execute(delete(CatalogProductEmbedding).where(CatalogProductEmbedding.seed_run_id == run_id))
    db.commit()

    products = db.scalars(
        select(CatalogProduct)
        .where(CatalogProduct.seed_run_id == run_id)
        .options(selectinload(CatalogProduct.variants))
        .order_by(CatalogProduct.id)
    ).all()

    status_counter: Counter[str] = Counter()
    failed = 0

    for start in range(0, len(products), batch_size):
        batch = products[start : start + batch_size]
        texts = [build_catalog_product_embedding_text(product) for product in batch]
        try:
            vectors = embedding_service.embed_texts(texts)
        except Exception:
            vectors = [embedding_service._deterministic_vector(text) for text in texts]
            status_counter["catalog_embedded_fallback"] += len(batch)

        rows = []
        payloads = []
        for product, vector in zip(batch, vectors, strict=True):
            vector_id = f"catalog:{product.id}"
            payloads.append(
                {
                    "id": vector_id,
                    "values": vector,
                    "metadata": {
                        "catalog_product_id": product.id,
                        "product_id": product.id,
                        "category": product.category,
                        "brand": product.brand,
                    },
                }
            )
            rows.append(
                {
                    "product_id": product.id,
                    "seed_run_id": run_id,
                    "namespace": namespace,
                    "vector_id": vector_id,
                    "embedding_model": embedding_service.model,
                    "status": "pending",
                    "embedded_at": datetime.now(timezone.utc),
                }
            )

        try:
            pinecone.upsert(namespace=namespace, vectors=payloads)
            status = "indexed" if pinecone.enabled else "local_only"
            status_counter[f"catalog_{status}"] += len(payloads)
            for row in rows:
                row["status"] = status
        except Exception as exc:
            failed += len(payloads)
            status_counter["catalog_failed"] += len(payloads)
            for row in rows:
                row["status"] = "failed"
                row["error"] = str(exc)[:1000]

        db.bulk_insert_mappings(CatalogProductEmbedding, rows)
        db.commit()

    return {
        "catalog_attempted": len(products),
        "catalog_indexed": status_counter.get("catalog_indexed", 0) + status_counter.get("catalog_local_only", 0),
        "catalog_failed": failed,
        **dict(status_counter),
    }


def index_products_for_run(db: Session, run_id: str, batch_size: int = 128) -> dict:
    embedding_service = EmbeddingService()
    pinecone = PineconeService()

    db.execute(delete(ProductEmbedding).where(ProductEmbedding.seed_run_id == run_id))
    db.commit()

    products = db.scalars(select(Product).where(Product.seed_run_id == run_id).order_by(Product.id)).all()
    attempted = len(products)

    status_counter: Counter[str] = Counter()
    failed = 0

    for start in range(0, len(products), batch_size):
        batch = products[start : start + batch_size]
        texts = [build_product_embedding_text(p) for p in batch]

        try:
            vectors = embedding_service.embed_texts(texts)
        except Exception:
            vectors = [embedding_service._deterministic_vector(t) for t in texts]
            status_counter["embedded_fallback"] += len(batch)

        # group upserts by namespace for Pinecone
        pinecone_payloads: dict[str, list[dict]] = defaultdict(list)
        embedding_rows = []

        for product, vector in zip(batch, vectors, strict=True):
            namespace = f"store_{product.store_id}"
            vector_id = f"product:{product.id}"
            metadata = {
                "product_id": product.id,
                "store_id": product.store_id,
                "category": product.category,
                "brand": product.brand,
                "availability": product.availability,
                "price": float(product.price),
                "margin_pct": float(product.margin_pct),
            }

            pinecone_payloads[namespace].append({"id": vector_id, "values": vector, "metadata": metadata})
            embedding_rows.append(
                {
                    "product_id": product.id,
                    "seed_run_id": run_id,
                    "store_id": product.store_id,
                    "namespace": namespace,
                    "vector_id": vector_id,
                    "embedding_model": embedding_service.model,
                    "status": "pending",
                    "embedded_at": datetime.now(timezone.utc),
                }
            )

        for namespace, payload in pinecone_payloads.items():
            try:
                pinecone.upsert(namespace=namespace, vectors=payload)
                status = "indexed" if pinecone.enabled else "local_only"
                status_counter[status] += len(payload)
                for row in embedding_rows:
                    if row["namespace"] == namespace:
                        row["status"] = status
            except Exception as exc:
                failed += len(payload)
                status_counter["failed"] += len(payload)
                for row in embedding_rows:
                    if row["namespace"] == namespace:
                        row["status"] = "failed"
                        row["error"] = str(exc)[:1000]

        db.bulk_insert_mappings(ProductEmbedding, embedding_rows)
        db.commit()

    catalog_status = _index_catalog_products_for_run(
        db,
        run_id,
        batch_size=batch_size,
        embedding_service=embedding_service,
        pinecone=pinecone,
    )

    indexed = status_counter.get("indexed", 0) + status_counter.get("local_only", 0)
    status_breakdown = dict(status_counter)
    status_breakdown.update(catalog_status)
    return {
        "attempted": attempted,
        "indexed": indexed,
        "failed": failed,
        "status_breakdown": status_breakdown,
    }
