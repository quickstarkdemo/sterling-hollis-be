from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

from app.catalog.schemas import (
    CatalogCategory,
    CatalogProduct,
    CatalogVariant,
    CategoryListResponse,
    ImageAnalysisAttributes,
    ImageRecommendationResponse,
    ProductDetailResponse,
    ProductFacetGroup,
    ProductFacetValue,
    ProductImages,
    ProductInventory,
    ProductInventorySummary,
    ProductListResponse,
    ProductRecommendationRequest,
    ProductRecommendationResponse,
    ProductSort,
    RecommendedProduct,
)
from app.models import CatalogProduct as CatalogProductModel
from app.models import Product, ProductVariant, StoreInventory
from app.schemas import CustomerRecommendationRequest, RetrievalMode
from app.services.catalog_normalization import (
    catalog_key_for_product,
    catalog_product_id_for_key,
)
from app.services.demo_assets import demo_image_url
from app.services.embeddings import EmbeddingService
from app.services.image_analysis import image_analysis_query_text
from app.services.inventory_status import is_in_stock, is_preorder
from app.services.pinecone_service import PineconeService
from app.services.product_images import product_variant_image_set
from app.services.recommendations import customer_recommendations
from app.services.taxonomy import CATEGORY_TAXONOMY

from app.observability.genai_otel import (
    current_genai_span,
    record_tool_call,
    set_span_attributes,
    trace_genai_tool,
)


@dataclass(frozen=True)
class ProductFilters:
    q: str | None = None
    store_id: str | None = None
    category: str | None = None
    brand: str | None = None
    size: str | None = None
    color: str | None = None
    availability: str | None = None
    min_price: float | None = None
    max_price: float | None = None
    in_stock_only: bool = False
    include_preorder: bool = True
    sort: ProductSort = ProductSort.relevance
    limit: int = 24
    offset: int = 0


def category_label(category: str | None) -> str:
    key = str(category or "").strip()
    cfg = CATEGORY_TAXONOMY.get(key)
    if cfg:
        return str(cfg["label"])
    return key.replace("_", " ").title()


def _stock_state(availability: str, inventory_qty: int) -> str:
    if is_in_stock(availability, inventory_qty):
        return "in_stock"
    if is_preorder(availability):
        return "preorder"
    return "out_of_stock"


def _conditions(filters: ProductFilters, *, include_search: bool = True):
    conditions = [CatalogProductModel.lifecycle_status == "published"]
    if filters.store_id:
        conditions.append(StoreInventory.store_id == filters.store_id)
    if filters.category:
        conditions.append(
            func.lower(CatalogProductModel.category) == filters.category.lower()
        )
    if filters.brand:
        conditions.append(
            func.lower(CatalogProductModel.brand) == filters.brand.lower()
        )
    if filters.size:
        conditions.append(func.lower(StoreInventory.size) == filters.size.lower())
    if filters.color:
        conditions.append(func.lower(ProductVariant.color) == filters.color.lower())
    if filters.availability:
        conditions.append(
            func.lower(StoreInventory.availability) == filters.availability.lower()
        )
    if filters.min_price is not None:
        conditions.append(ProductVariant.price_max >= Decimal(str(filters.min_price)))
    if filters.max_price is not None:
        conditions.append(ProductVariant.price_min <= Decimal(str(filters.max_price)))
    if filters.in_stock_only:
        conditions.append(
            and_(
                func.lower(StoreInventory.availability) == "in stock",
                StoreInventory.inventory_qty > 0,
            )
        )
    elif not filters.include_preorder:
        conditions.append(func.lower(StoreInventory.availability) != "preorder")
    if include_search and filters.q:
        token = f"%{filters.q.strip()}%"
        conditions.append(
            or_(
                CatalogProductModel.title.ilike(token),
                CatalogProductModel.description.ilike(token),
                CatalogProductModel.brand.ilike(token),
                CatalogProductModel.category.ilike(token),
                ProductVariant.color.ilike(token),
                ProductVariant.material.ilike(token),
            )
        )
    return conditions


def _filtered_product_id_query(filters: ProductFilters):
    query = (
        select(
            CatalogProductModel.id.label("product_id"),
            func.min(ProductVariant.price_min).label("price_min"),
            func.max(ProductVariant.price_max).label("price_max"),
            func.coalesce(func.sum(StoreInventory.inventory_qty), 0).label(
                "inventory_units"
            ),
            func.coalesce(func.max(StoreInventory.objective_weight), 0).label(
                "objective_weight"
            ),
        )
        .join(
            ProductVariant, ProductVariant.catalog_product_id == CatalogProductModel.id
        )
        .outerjoin(StoreInventory, StoreInventory.variant_id == ProductVariant.id)
        .where(*_conditions(filters))
        .group_by(CatalogProductModel.id)
    )
    if filters.sort == ProductSort.price_asc:
        query = query.order_by(
            func.min(ProductVariant.price_min).asc(), CatalogProductModel.id.asc()
        )
    elif filters.sort == ProductSort.price_desc:
        query = query.order_by(
            func.max(ProductVariant.price_max).desc(), CatalogProductModel.id.asc()
        )
    elif filters.sort == ProductSort.inventory_desc:
        query = query.order_by(
            func.coalesce(func.sum(StoreInventory.inventory_qty), 0).desc(),
            CatalogProductModel.id.asc(),
        )
    elif filters.sort == ProductSort.newest:
        query = query.order_by(CatalogProductModel.id.desc())
    else:
        query = query.order_by(
            func.coalesce(func.max(StoreInventory.objective_weight), 0).desc(),
            CatalogProductModel.id.asc(),
        )
    return query


def _inventory_rows(
    db: Session, variant_ids: list[str], *, store_id: str | None = None
) -> list[StoreInventory]:
    if not variant_ids:
        return []
    query = select(StoreInventory).where(StoreInventory.variant_id.in_(variant_ids))
    if store_id:
        query = query.where(StoreInventory.store_id == store_id)
    return db.scalars(
        query.order_by(StoreInventory.store_id.asc(), StoreInventory.size.asc())
    ).all()


def _variant_images(
    product: CatalogProductModel, variant: ProductVariant
) -> ProductImages:
    raw_image_set = product_variant_image_set(variant)
    fallback = demo_image_url(product.category, variant.id, variant_hint=product.brand)
    return ProductImages(
        thumbnail_url=(raw_image_set or {}).get("thumbnail_url") or fallback,
        primary_url=(raw_image_set or {}).get("primary_url") or fallback,
        detail_urls=(raw_image_set or {}).get("detail_urls") or [fallback],
    )


def _variant_attributes(variant: ProductVariant) -> dict[str, str]:
    return {
        key: value
        for key, value in {
            "color": variant.color,
            "material": variant.material,
            "gender": variant.gender,
            "season": variant.season,
        }.items()
        if value
    }


def _inventory_api(row: StoreInventory) -> ProductInventory:
    return ProductInventory(
        store_id=row.store_id,
        availability=row.availability,
        stock_state=_stock_state(row.availability, int(row.inventory_qty or 0)),
        inventory_qty=int(row.inventory_qty or 0),
        size=row.size,
    )


def _summary(rows: list[StoreInventory]) -> ProductInventorySummary:
    total_units = sum(int(row.inventory_qty or 0) for row in rows)
    in_stock_rows = [
        row for row in rows if is_in_stock(row.availability, row.inventory_qty)
    ]
    preorder_rows = [row for row in rows if is_preorder(row.availability)]
    in_stock_units = sum(int(row.inventory_qty or 0) for row in in_stock_rows)
    preorder_units = sum(int(row.inventory_qty or 0) for row in preorder_rows)
    store_ids = {row.store_id for row in rows}
    in_stock_store_ids = {row.store_id for row in in_stock_rows}
    if in_stock_units:
        availability = "in_stock"
    elif preorder_units:
        availability = "preorder"
    else:
        availability = "out_of_stock"
    return ProductInventorySummary(
        total_units=total_units,
        in_stock_units=in_stock_units,
        preorder_units=preorder_units,
        store_count=len(store_ids),
        in_stock_store_count=len(in_stock_store_ids),
        availability=availability,
    )


def _product_variants(db: Session, product_id: str) -> list[ProductVariant]:
    return db.scalars(
        select(ProductVariant)
        .where(ProductVariant.catalog_product_id == product_id)
        .order_by(ProductVariant.price_min.asc(), ProductVariant.id.asc())
    ).all()


def product_to_catalog(
    db: Session,
    product: CatalogProductModel,
    *,
    include_variants: bool = False,
    store_id: str | None = None,
) -> CatalogProduct | ProductDetailResponse:
    variants = _product_variants(db, product.id)
    variant_ids = [variant.id for variant in variants]
    inventory_rows = _inventory_rows(db, variant_ids, store_id=store_id)
    inventory_by_variant: dict[str, list[StoreInventory]] = {
        variant_id: [] for variant_id in variant_ids
    }
    for row in inventory_rows:
        inventory_by_variant.setdefault(row.variant_id, []).append(row)

    default_variant = max(
        variants,
        key=lambda variant: (
            sum(
                int(row.inventory_qty or 0)
                for row in inventory_by_variant.get(variant.id, [])
            ),
            -float(variant.price_min),
            variant.id,
        ),
        default=None,
    )
    images = (
        _variant_images(product, default_variant)
        if default_variant
        else ProductImages()
    )
    price_min = min((float(variant.price_min) for variant in variants), default=0.0)
    price_max = max((float(variant.price_max) for variant in variants), default=0.0)
    summary = _summary(inventory_rows)
    base = CatalogProduct(
        id=product.id,
        catalog_id=product.id,
        title=product.title,
        description=product.description,
        brand=product.brand,
        category=product.category,
        category_label=category_label(product.category),
        price=price_min,
        price_min=price_min,
        price_max=price_max,
        default_variant_id=default_variant.id if default_variant else None,
        link=default_variant.link if default_variant else None,
        image_url=images.thumbnail_url,
        images=images,
        attributes=_variant_attributes(default_variant) if default_variant else {},
        inventory_summary=summary,
    )
    if not include_variants:
        return base

    variant_payload = []
    for variant in variants:
        rows = inventory_by_variant.get(variant.id, [])
        variant_images = _variant_images(product, variant)
        variant_payload.append(
            CatalogVariant(
                id=variant.id,
                product_id=product.id,
                price_min=float(variant.price_min),
                price_max=float(variant.price_max),
                link=variant.link,
                image_url=variant_images.thumbnail_url,
                images=variant_images,
                attributes=_variant_attributes(variant),
                sizes=sorted({row.size for row in rows}),
                inventory=[_inventory_api(row) for row in rows],
            )
        )
    return ProductDetailResponse(
        **base.model_dump(),
        metadata=dict(product.metadata_json or {}),
        variants=variant_payload,
    )


def _product_id_for_legacy_id(db: Session, product_id: str) -> str:
    product = db.get(Product, product_id)
    if not product:
        return product_id
    return catalog_product_id_for_key(catalog_key_for_product(product))


def list_categories(
    db: Session, *, store_id: str | None = None
) -> CategoryListResponse:
    conditions = [CatalogProductModel.lifecycle_status == "published"]
    if store_id:
        conditions.append(StoreInventory.store_id == store_id)
    rows = db.execute(
        select(
            CatalogProductModel.category,
            func.count(func.distinct(CatalogProductModel.id)),
            func.min(ProductVariant.price_min),
            func.max(ProductVariant.price_max),
            func.coalesce(func.sum(StoreInventory.inventory_qty), 0),
        )
        .join(
            ProductVariant, ProductVariant.catalog_product_id == CatalogProductModel.id
        )
        .outerjoin(StoreInventory, StoreInventory.variant_id == ProductVariant.id)
        .where(*conditions)
        .group_by(CatalogProductModel.category)
        .order_by(CatalogProductModel.category)
    ).all()
    return CategoryListResponse(
        categories=[
            CatalogCategory(
                id=str(row[0]),
                label=category_label(str(row[0])),
                product_count=int(row[1] or 0),
                min_price=float(row[2]) if row[2] is not None else None,
                max_price=float(row[3]) if row[3] is not None else None,
                available_units=int(row[4] or 0),
            )
            for row in rows
        ]
    )


def _facet_group(
    db: Session, filters: ProductFilters, name: str, column
) -> ProductFacetGroup:
    rows = db.execute(
        select(column, func.count(func.distinct(CatalogProductModel.id)))
        .join(
            ProductVariant, ProductVariant.catalog_product_id == CatalogProductModel.id
        )
        .outerjoin(StoreInventory, StoreInventory.variant_id == ProductVariant.id)
        .where(*_conditions(filters), column.is_not(None))
        .group_by(column)
        .order_by(
            func.count(func.distinct(CatalogProductModel.id)).desc(), column.asc()
        )
        .limit(20)
    ).all()
    return ProductFacetGroup(
        name=name,
        values=[
            ProductFacetValue(value=str(row[0]), count=int(row[1] or 0))
            for row in rows
            if row[0]
        ],
    )


def list_products(
    db: Session, filters: ProductFilters, *, include_facets: bool = True
) -> ProductListResponse:
    product_id_query = _filtered_product_id_query(filters)
    total = (
        db.scalar(select(func.count()).select_from(product_id_query.subquery())) or 0
    )
    product_ids = [
        row.product_id
        for row in db.execute(
            product_id_query.limit(filters.limit).offset(filters.offset)
        ).all()
    ]
    product_map = {
        product.id: product
        for product in db.scalars(
            select(CatalogProductModel).where(CatalogProductModel.id.in_(product_ids))
        ).all()
    }
    products = [
        product_map[product_id]
        for product_id in product_ids
        if product_id in product_map
    ]
    facets = []
    if include_facets:
        facets = [
            _facet_group(db, filters, "brand", CatalogProductModel.brand),
            _facet_group(db, filters, "category", CatalogProductModel.category),
            _facet_group(db, filters, "size", StoreInventory.size),
            _facet_group(db, filters, "color", ProductVariant.color),
        ]
    return ProductListResponse(
        items=[
            product_to_catalog(db, product, store_id=filters.store_id)
            for product in products
        ],
        total=int(total),
        limit=filters.limit,
        offset=filters.offset,
        facets=facets,
    )


def get_product_detail(
    db: Session, product_id: str, *, store_id: str | None = None
) -> ProductDetailResponse | None:
    normalized_id = _product_id_for_legacy_id(db, product_id)
    product = db.get(CatalogProductModel, normalized_id)
    if not product or product.lifecycle_status != "published":
        return None
    return product_to_catalog(db, product, include_variants=True, store_id=store_id)  # type: ignore[return-value]


def related_products(
    db: Session, product_id: str, *, limit: int = 12
) -> ProductListResponse | None:
    normalized_id = _product_id_for_legacy_id(db, product_id)
    product = db.get(CatalogProductModel, normalized_id)
    if not product or product.lifecycle_status != "published":
        return None
    filters = ProductFilters(
        category=product.category,
        brand=product.brand,
        sort=ProductSort.relevance,
        limit=limit + 1,
    )
    rows = list_products(db, filters, include_facets=False).items
    items = [row for row in rows if row.id != product.id][:limit]
    return ProductListResponse(
        items=items, total=len(items), limit=limit, offset=0, facets=[]
    )


def recommend_products(
    db: Session, req: ProductRecommendationRequest
) -> ProductRecommendationResponse:
    if req.store_id and req.customer_id:
        rows, strategy, _, _ = customer_recommendations(
            db,
            CustomerRecommendationRequest(
                store_id=req.store_id,
                customer_id=req.customer_id,
                occasion=req.occasion,
                budget_min=req.budget_min,
                budget_max=req.budget_max,
                top_k=req.top_k,
            ),
            retrieval_mode=RetrievalMode.fast,
        )
        seen: set[str] = set()
        recommendations = []
        for row in rows:
            legacy = db.get(Product, row.product_id)
            if not legacy:
                continue
            product_id = catalog_product_id_for_key(catalog_key_for_product(legacy))
            if product_id in seen:
                continue
            product = db.get(CatalogProductModel, product_id)
            if not product or product.lifecycle_status != "published":
                continue
            seen.add(product_id)
            recommendations.append(
                RecommendedProduct(
                    product=product_to_catalog(db, product, store_id=req.store_id),  # type: ignore[arg-type]
                    score=row.score,
                    reasons=row.reasons,
                    strategy=strategy,
                )
            )
            if len(recommendations) >= req.top_k:
                break
        return ProductRecommendationResponse(
            recommendations=recommendations, strategy=strategy
        )

    filters = ProductFilters(
        store_id=req.store_id,
        category=req.category,
        brand=req.brand,
        min_price=req.budget_min,
        max_price=req.budget_max,
        include_preorder=req.include_preorder,
        sort=ProductSort.relevance,
        limit=req.top_k,
    )
    rows = [
        row
        for row in list_products(db, filters, include_facets=False).items
        if row.inventory_summary.availability == "in_stock"
        or (req.include_preorder and row.inventory_summary.availability == "preorder")
    ][: req.top_k]
    recommendations = [
        RecommendedProduct(
            product=row,
            score=1.0 - (idx * 0.01),
            reasons=["matched product filters", "ranked by catalog inventory signal"],
            strategy="sql_catalog_rules",
        )
        for idx, row in enumerate(rows)
    ]
    return ProductRecommendationResponse(
        recommendations=recommendations, strategy="sql_catalog_rules"
    )


def _catalog_card_matches_request(
    product: CatalogProduct, req: ProductRecommendationRequest
) -> bool:
    if req.category and product.category.lower() != req.category.lower():
        return False
    if req.brand and product.brand.lower() != req.brand.lower():
        return False
    if req.budget_min is not None and product.price_max < req.budget_min:
        return False
    if req.budget_max is not None and product.price_min > req.budget_max:
        return False
    if product.inventory_summary.availability == "in_stock":
        return True
    if req.include_preorder and product.inventory_summary.availability == "preorder":
        return True
    return False


def _image_keyword_score(
    product: CatalogProduct, analysis: ImageAnalysisAttributes
) -> tuple[float, list[str]]:
    score = 0.25
    reasons: list[str] = []
    if analysis.target_categories and product.category in set(
        analysis.target_categories
    ):
        score += 0.35
        reasons.append("matched uploaded image category")

    searchable = " ".join(
        [
            product.title,
            product.description,
            product.brand,
            product.category,
            " ".join(product.attributes.values()),
        ]
    ).lower()
    keyword_hits = []
    for keyword in [
        *analysis.colors,
        *analysis.materials,
        *analysis.patterns,
        *analysis.style_keywords,
        *analysis.occasion_keywords,
    ]:
        token = str(keyword or "").strip().lower()
        if token and token in searchable and token not in keyword_hits:
            keyword_hits.append(token)

    if keyword_hits:
        score += min(0.36, 0.06 * len(keyword_hits))
        reasons.append("matched uploaded image cues: " + ", ".join(keyword_hits[:4]))
    if product.inventory_summary.availability == "in_stock":
        score += 0.08
        reasons.append("currently in stock")
    elif product.inventory_summary.availability == "preorder":
        score += 0.02
        reasons.append("available for preorder")
    return score, reasons or ["matched uploaded image guidance"]


def _recommend_products_from_catalog_ids(
    db: Session,
    product_ids: list[str],
    scores: dict[str, float],
    req: ProductRecommendationRequest,
    analysis: ImageAnalysisAttributes,
) -> list[RecommendedProduct]:
    product_map = {
        product.id: product
        for product in db.scalars(
            select(CatalogProductModel).where(
                CatalogProductModel.id.in_(product_ids),
                CatalogProductModel.lifecycle_status == "published",
            )
        ).all()
    }
    recommendations: list[RecommendedProduct] = []
    seen: set[str] = set()
    for product_id in product_ids:
        if product_id in seen:
            continue
        model = product_map.get(product_id)
        if not model:
            continue
        card = product_to_catalog(db, model, store_id=req.store_id)
        assert isinstance(card, CatalogProduct)
        if not _catalog_card_matches_request(card, req):
            continue
        _, reasons = _image_keyword_score(card, analysis)
        recommendations.append(
            RecommendedProduct(
                product=card,
                score=round(float(scores.get(product_id, 0.0)), 4),
                reasons=["visually similar to uploaded image", *reasons[:2]],
                strategy="catalog_vector_image",
            )
        )
        seen.add(product_id)
        if len(recommendations) >= req.top_k:
            break
    return recommendations


def _image_vector_recommendations(
    db: Session,
    req: ProductRecommendationRequest,
    analysis: ImageAnalysisAttributes,
) -> list[RecommendedProduct]:
    try:
        embedding_service = EmbeddingService()
        pinecone = PineconeService()
        if not (embedding_service.enabled and pinecone.enabled):
            return []

        vector = embedding_service.embed_text(image_analysis_query_text(analysis))
        namespace = pinecone.settings.pinecone_catalog_namespace
        matches = pinecone.query(
            namespace=namespace, vector=vector, top_k=max(req.top_k * 5, 50)
        )
    except Exception:
        return []

    product_ids = [
        str(
            match.get("metadata", {}).get("catalog_product_id")
            or match.get("id", "").replace("catalog:", "")
        )
        for match in matches
    ]
    scores = {
        str(
            match.get("metadata", {}).get("catalog_product_id")
            or match.get("id", "").replace("catalog:", "")
        ): float(match.get("score", 0.0))
        for match in matches
    }
    return _recommend_products_from_catalog_ids(db, product_ids, scores, req, analysis)


def _image_sql_recommendations(
    db: Session,
    req: ProductRecommendationRequest,
    analysis: ImageAnalysisAttributes,
) -> list[RecommendedProduct]:
    filters = ProductFilters(
        store_id=req.store_id,
        category=req.category,
        brand=req.brand,
        min_price=req.budget_min,
        max_price=req.budget_max,
        include_preorder=req.include_preorder,
        sort=ProductSort.relevance,
        limit=max(req.top_k * 8, 50),
    )
    rows = [
        row
        for row in list_products(db, filters, include_facets=False).items
        if _catalog_card_matches_request(row, req)
    ]
    ranked = []
    for row in rows:
        score, reasons = _image_keyword_score(row, analysis)
        ranked.append((score, row, reasons))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [
        RecommendedProduct(
            product=row,
            score=round(score, 4),
            reasons=reasons,
            strategy="sql_catalog_image_rules",
        )
        for score, row, reasons in ranked[: req.top_k]
    ]

@trace_genai_tool(
    "product_recommendation_ranking",
    tool_type="ranking",
    attributes=lambda db, req, analysis: {
        "app.recommendations.top_k": req.top_k,
        "app.filters.store_id": req.store_id,
        "app.filters.category": req.category,
        "app.filters.brand": req.brand,
        "app.filters.budget_min": req.budget_min,
        "app.filters.budget_max": req.budget_max,
        "app.filters.include_preorder": req.include_preorder,
        "app.image_analysis.confidence": analysis.confidence,
        "app.image_analysis.target_categories": analysis.target_categories,
    },
)
def recommend_products_from_image_analysis(
    db: Session,
    req: ProductRecommendationRequest,
    analysis: ImageAnalysisAttributes,
) -> ImageRecommendationResponse:
    span = current_genai_span()

    record_tool_call(
        span,
        arguments={
            "top_k": req.top_k,
            "store_id": req.store_id,
            "category": req.category,
            "brand": req.brand,
            "budget_min": req.budget_min,
            "budget_max": req.budget_max,
            "include_preorder": req.include_preorder,
            "target_categories": analysis.target_categories,
            "style_keywords": analysis.style_keywords[:10],
        },
        description="Rank catalog products using vector results first, then SQL keyword fallback.",
    )

    vector_rows = _image_vector_recommendations(db, req, analysis)

    if vector_rows:
        response = ImageRecommendationResponse(
            analysis=analysis,
            recommendations=vector_rows,
            strategy="catalog_vector_image",
        )
    else:
        response = ImageRecommendationResponse(
            analysis=analysis,
            recommendations=_image_sql_recommendations(db, req, analysis),
            strategy="sql_catalog_image_rules",
        )

    set_span_attributes(
        span,
        {
            "app.recommendations.strategy": response.strategy,
            "app.recommendations.count": len(response.recommendations),
        },
    )

    record_tool_call(
        span,
        result={
            "strategy": response.strategy,
            "recommendation_count": len(response.recommendations),
            "product_ids": [row.product.id for row in response.recommendations],
            "top_recommendations": [
                {
                    "id": row.product.id,
                    "title": row.product.title,
                    "brand": row.product.brand,
                    "category": row.product.category,
                    "score": row.score,
                    "strategy": row.strategy,
                }
                for row in response.recommendations[:10]
            ],
        },
    )

    return response
