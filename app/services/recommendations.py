from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.models import Customer, Order, OrderItem, Product
from app.schemas import (
    CustomerRecommendationRequest,
    MerchandisingRecommendationRequest,
    ProductRecommendation,
    RetrievalMode,
)
from app.services.customer_preferences import normalize_customer_sex, top_style_categories
from app.services.embeddings import EmbeddingService
from app.services.demo_assets import demo_image_url
from app.services.pinecone_service import PineconeService
from app.services.taxonomy import OCCASION_TO_CATEGORY


def _safe_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _gender_alignment_score(customer_sex: str | None, product_gender: str | None) -> tuple[float, str | None]:
    if not customer_sex or not product_gender:
        return 0.0, None
    gender = product_gender.strip().lower()
    if customer_sex == "male":
        if gender in {"men", "male", "boys"}:
            return 0.24, "matched male profile"
        if gender == "unisex":
            return 0.08, "matched unisex product"
        if gender in {"women", "female", "girls"}:
            return -0.34, "reduced due to gender mismatch"
    if customer_sex == "female":
        if gender in {"women", "female", "girls"}:
            return 0.24, "matched female profile"
        if gender == "unisex":
            return 0.08, "matched unisex product"
        if gender in {"men", "male", "boys"}:
            return -0.34, "reduced due to gender mismatch"
    if customer_sex == "nonbinary":
        if gender == "unisex":
            return 0.12, "matched nonbinary profile"
    return 0.0, None


def _build_customer_query_context(db: Session, req: CustomerRecommendationRequest) -> str:
    parts = [f"Store: {req.store_id}"]
    if req.occasion:
        parts.append(f"Occasion: {req.occasion}")
    if req.budget_min is not None or req.budget_max is not None:
        parts.append(f"Budget: {req.budget_min or 0}-{req.budget_max or 99999}")

    if req.customer_id:
        customer = db.get(Customer, req.customer_id)
        if customer:
            parts.append(f"Loyalty tier: {customer.loyalty_tier}")
            parts.append(f"Price sensitivity: {customer.price_sensitivity}")
            customer_sex = normalize_customer_sex(customer.sex)
            if customer_sex:
                parts.append(f"Customer sex: {customer_sex}")
            style = customer.style_vector if isinstance(customer.style_vector, dict) else _safe_json(customer.style_vector)
            if style:
                top_style = sorted(style.items(), key=lambda x: x[1], reverse=True)[:3]
                parts.append("Style affinity: " + ", ".join(k for k, _ in top_style))
            occasions = customer.occasion_affinity if isinstance(customer.occasion_affinity, dict) else _safe_json(customer.occasion_affinity)
            if occasions:
                top_occasions = sorted(occasions.items(), key=lambda x: x[1], reverse=True)[:2]
                parts.append("Occasion affinity: " + ", ".join(k for k, _ in top_occasions))
            sizes = customer.size_preferences if isinstance(customer.size_preferences, dict) else _safe_json(customer.size_preferences)
            if sizes:
                parts.append("Size preferences: " + ", ".join(f"{k}={v}" for k, v in sizes.items()))

        recent_categories = db.execute(
            select(Product.category, func.count(OrderItem.id).label("cnt"))
            .join(OrderItem, OrderItem.product_id == Product.id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.customer_id == req.customer_id)
            .group_by(Product.category)
            .order_by(desc("cnt"))
            .limit(5)
        ).all()
        if recent_categories:
            parts.append("Recent categories: " + ", ".join(row[0] for row in recent_categories))

    return "\n".join(parts)


def _rule_rerank(
    product: Product,
    req: CustomerRecommendationRequest,
    base_score: float,
    customer_brand_prefs: set[str],
    preferred_categories: set[str],
    customer_sex: str | None,
) -> tuple[float, list[str]]:
    score = float(base_score)
    reasons: list[str] = []

    if req.occasion and product.category in OCCASION_TO_CATEGORY.get(req.occasion, []):
        score += 0.18
        reasons.append(f"matched {req.occasion} occasion")

    price = float(product.price)
    if req.budget_min is not None and price < req.budget_min:
        score -= 0.12
    if req.budget_max is not None and price > req.budget_max:
        score -= 0.2
    if req.budget_min is not None and req.budget_max is not None and req.budget_min <= price <= req.budget_max:
        score += 0.14
        reasons.append("inside requested budget")

    if product.availability == "in stock":
        score += 0.08
        reasons.append("currently in stock")

    if product.brand in customer_brand_prefs:
        score += 0.11
        reasons.append("aligned with prior brand purchases")

    if preferred_categories and product.category in preferred_categories:
        score += 0.1
        reasons.append("aligned with category preferences")

    sex_delta, sex_reason = _gender_alignment_score(customer_sex, product.gender)
    if sex_delta != 0:
        score += sex_delta
    if sex_reason:
        reasons.append(sex_reason)

    score += float(product.objective_weight) * 0.05
    return score, reasons


def customer_recommendations(
    db: Session, req: CustomerRecommendationRequest, retrieval_mode: RetrievalMode = RetrievalMode.semantic
) -> tuple[list[ProductRecommendation], str]:
    if retrieval_mode == RetrievalMode.auto:
        retrieval_mode = (
            RetrievalMode.fast
            if req.customer_id and (req.occasion or req.budget_min is not None or req.budget_max is not None)
            else RetrievalMode.semantic
        )
    use_semantic = retrieval_mode == RetrievalMode.semantic
    embedding_service = EmbeddingService() if use_semantic else None
    pinecone = PineconeService() if use_semantic else None

    customer_brand_prefs: set[str] = set()
    preferred_categories: set[str] = set()
    customer_sex: str | None = None
    if req.customer_id:
        customer = db.get(Customer, req.customer_id)
        if customer:
            customer_sex = normalize_customer_sex(customer.sex)
            style = customer.style_vector if isinstance(customer.style_vector, dict) else _safe_json(customer.style_vector)
            preferred_categories = set(top_style_categories(style, customer_sex, limit=3))
        brand_rows = db.execute(
            select(Product.brand, func.count(OrderItem.id).label("cnt"))
            .join(OrderItem, OrderItem.product_id == Product.id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.customer_id == req.customer_id)
            .group_by(Product.brand)
            .order_by(desc("cnt"))
            .limit(8)
        ).all()
        customer_brand_prefs = {row[0] for row in brand_rows}

    vector_candidates: list[tuple[str, float]] = []
    if use_semantic and pinecone and pinecone.enabled and embedding_service:
        context_text = _build_customer_query_context(db, req)
        query_vector = embedding_service.embed_text(context_text)
        matches = pinecone.query(
            namespace=f"store_{req.store_id}",
            vector=query_vector,
            top_k=max(req.top_k * 3, 30),
            filters={"store_id": {"$eq": req.store_id}},
        )
        vector_candidates = [(m["metadata"].get("product_id") or m["id"].replace("product:", ""), float(m["score"])) for m in matches]

    strategy = "hybrid_vector_rules" if vector_candidates else "sql_rules_fast_path"

    if vector_candidates:
        ids = [pid for pid, _ in vector_candidates]
        scores = {pid: score for pid, score in vector_candidates}

        products = db.scalars(select(Product).where(Product.id.in_(ids))).all()
        product_map = {p.id: p for p in products}
        ranked = []
        for pid in ids:
            p = product_map.get(pid)
            if not p:
                continue
            score, reasons = _rule_rerank(
                p,
                req,
                scores.get(pid, 0.0),
                customer_brand_prefs,
                preferred_categories,
                customer_sex,
            )
            ranked.append((score, p, reasons))

        # External vector stores can contain ids that are stale relative to the SQL source of truth.
        # If none of the vector hits resolve locally, fall back to SQL/rules instead of returning nothing.
        if ranked:
            ranked.sort(key=lambda x: x[0], reverse=True)
            output: list[ProductRecommendation] = []
            for score, p, reasons in ranked[: req.top_k]:
                output.append(
                    ProductRecommendation(
                        product_id=p.id,
                        title=p.title,
                        brand=p.brand,
                        category=p.category,
                        price=float(p.price),
                        availability=p.availability,
                        link=p.link,
                        image_url=demo_image_url(p.category, p.id, variant_hint=p.brand),
                        score=round(score, 4),
                        reasons=reasons or ["high relevance"],
                    )
                )

            return output, strategy

    # SQL fallback path for local mode or when vector hits cannot be resolved against Postgres.
    strategy = "sql_rules_fast_path"
    query = select(Product).where(Product.store_id == req.store_id, Product.availability != "out of stock")
    if req.occasion and req.occasion in OCCASION_TO_CATEGORY:
        query = query.where(Product.category.in_(OCCASION_TO_CATEGORY[req.occasion]))
    if req.budget_max is not None:
        query = query.where(Product.price <= req.budget_max)
    if req.budget_min is not None:
        query = query.where(Product.price >= req.budget_min)

    products = db.scalars(query.order_by(desc(Product.objective_weight)).limit(req.top_k * 4)).all()
    ranked = []
    for p in products:
        score, reasons = _rule_rerank(
            p,
            req,
            0.4,
            customer_brand_prefs,
            preferred_categories,
            customer_sex,
        )
        ranked.append((score, p, reasons))

    ranked.sort(key=lambda x: x[0], reverse=True)
    output: list[ProductRecommendation] = []
    for score, p, reasons in ranked[: req.top_k]:
        output.append(
            ProductRecommendation(
                product_id=p.id,
                title=p.title,
                brand=p.brand,
                category=p.category,
                price=float(p.price),
                availability=p.availability,
                link=p.link,
                image_url=demo_image_url(p.category, p.id, variant_hint=p.brand),
                score=round(score, 4),
                reasons=reasons or ["high relevance"],
            )
        )

    return output, strategy


def merchandising_recommendations(db: Session, req: MerchandisingRecommendationRequest):
    since = datetime.now(timezone.utc) - timedelta(days=req.lookback_days)

    base = (
        select(
            Product.id,
            Product.title,
            Product.category,
            func.sum(OrderItem.quantity).label("units"),
            func.sum(OrderItem.line_total).label("revenue"),
            func.avg(Product.margin_pct).label("margin"),
            func.avg(Product.objective_weight).label("objective_weight"),
        )
        .join(OrderItem, OrderItem.product_id == Product.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.store_id == req.store_id, Order.ordered_at >= since)
        .group_by(Product.id, Product.title, Product.category)
    )

    rows = db.execute(base).all()

    ranked = []
    for row in rows:
        units = float(row.units or 0)
        revenue = float(row.revenue or 0)
        margin = float(row.margin or 0)
        obj_w = float(row.objective_weight or 0)

        if req.objective.value == "sell_through":
            metric = units * (0.7 + obj_w)
            rationale = "high unit velocity with favorable objective weighting"
        elif req.objective.value == "margin":
            metric = revenue * margin
            rationale = "strong margin-adjusted revenue"
        else:
            metric = revenue
            rationale = "highest revenue contribution"

        ranked.append((metric, row.id, row.title, row.category, rationale))

    ranked.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            "product_id": pid,
            "title": title,
            "category": category,
            "metric_value": round(metric, 4),
            "rationale": rationale,
        }
        for metric, pid, title, category, rationale in ranked[: req.top_k]
    ]
