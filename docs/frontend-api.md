# Frontend API Guide

This is the retail frontend contract for the product database backend. The
canonical machine-generated OpenAPI schema is available from a running API at
`/openapi.json` and can be exported into the repo with:

```bash
.venv/bin/python scripts/export_openapi.py
```

or:

```bash
make openapi
```

For frontend planning, use `docs/frontend-openapi.yaml` as the curated retail API
surface. It excludes MCP/OpenAI Apps adapter concerns and focuses on catalog,
product detail, recommendations, generated image URLs, and image-job progress.

## Identity Model

- Catalog products use `cat_...` IDs.
- Display variants use `var_...` IDs.
- Stores are inventory context only. Use `store_id` as a filter or inventory
  dimension, not as product identity.
- Product cards are parent catalog products with `default_variant_id`, price
  range, images, attributes, and inventory summary.
- Product detail expands the parent product into `variants[]`, variant galleries,
  sizes, and per-store inventory rows.

## Runtime Base URLs

Local:

```text
http://localhost:8000
```

Production:

```text
https://products-api.quickstark.com
```

Generated image files are served from:

```text
/product-images/{filename}
```

## Retail Frontend Endpoints

| Purpose | Method | Path |
| --- | --- | --- |
| Catalog landing data | `GET` | `/api/catalog` |
| Category list | `GET` | `/api/categories` |
| Category list alias | `GET` | `/api/catalog/categories` |
| Store-scoped category availability | `GET` | `/api/stores/{store_id}/categories` |
| Product list | `GET` | `/api/products` |
| Product list alias | `GET` | `/api/catalog/products` |
| Products in category | `GET` | `/api/categories/{category}/products` |
| Product detail | `GET` | `/api/products/{product_id}` |
| Related products | `GET` | `/api/products/{product_id}/related` |
| Product search | `GET` | `/api/search/products` |
| Product recommendations | `POST` | `/api/recommendations/products` |
| Image analysis | `POST` | `/api/image-analysis` |
| Image recommendations | `POST` | `/api/recommendations/image` |

## Admin/Operations Endpoints Useful During Buildout

These should be treated as operator/admin controls, not unauthenticated public
frontend actions.

| Purpose | Method | Path |
| --- | --- | --- |
| Start background product image generation | `POST` | `/admin/product-images/generate` |
| Poll one image generation job | `GET` | `/admin/product-images/jobs/{job_id}` |
| List recent image generation jobs | `GET` | `/admin/product-images/jobs` |
| Health check | `GET` | `/health` |

## Recommended Frontend Flow

1. Load `/api/catalog?limit=24` for initial category navigation and product cards.
2. Use `/api/categories` to build category navigation.
3. Use `/api/products` or `/api/categories/{category}/products` for collection
   pages with filters and pagination.
4. Use `/api/products/{product_id}` for PDP data. Expect `variants[]`, image
   galleries, sizes, and store inventory here.
5. Use `/api/products/{product_id}/related` for related-product rails.
6. Use `/api/recommendations/products` for AI/rules-assisted rails.
7. Use `/api/recommendations/image` when a shopper uploads an inspiration image.
   The backend validates the image, extracts structured visual cues with OpenAI,
   discards the raw image, and returns product cards.

## Consumer Image Recommendation Uploads

`POST /api/image-analysis` accepts `multipart/form-data` with:

- `image`: required JPEG, PNG, or WebP file.
- `context`: optional text hint from the frontend.

`POST /api/recommendations/image` accepts the same upload fields plus optional
filters:

- `store_id`
- `category`
- `brand`
- `budget_min`
- `budget_max`
- `include_preorder`
- `top_k`

Successful image recommendation responses return:

```json
{
  "analysis": {
    "summary": "Rose silk occasion dress",
    "target_categories": ["womens_apparel"],
    "colors": ["rose"],
    "materials": ["silk"],
    "style_keywords": ["tailored", "occasion"],
    "confidence": 0.91
  },
  "recommendations": [],
  "strategy": "catalog_vector_image"
}
```

The recommendation product shape is the same `RecommendedProduct` catalog-card
shape returned by `/api/recommendations/products`.

## Query Parameters

`GET /api/products` supports the full product filter set:

- `q`
- `category`
- `brand`
- `color`
- `size`
- `availability`
- `store_id`
- `min_price`
- `max_price`
- `include_preorder`
- `in_stock_only`
- `sort`
- `limit`
- `offset`

`GET /api/catalog`, `GET /api/catalog/products`, `GET /api/search/products`, and
`GET /api/categories/{category}/products` expose narrower subsets. Use
`docs/frontend-openapi.yaml` as the source of truth for each endpoint's exact
parameters.

Recommended sorts:

- `relevance`
- `newest`
- `price_asc`
- `price_desc`
- `inventory_desc`

## Product Card Shape

```json
{
  "id": "cat_...",
  "catalog_id": "cat_...",
  "title": "Bottega Veneta Sage Trousers",
  "brand": "Bottega Veneta",
  "category": "womens_apparel",
  "category_label": "Women's Apparel",
  "price": 750.0,
  "price_min": 690.0,
  "price_max": 750.0,
  "default_variant_id": "var_...",
  "image_url": "https://products-api.quickstark.com/product-images/...",
  "images": {
    "thumbnail_url": "https://products-api.quickstark.com/product-images/...-thumb.jpg",
    "primary_url": "https://products-api.quickstark.com/product-images/...-detail-1.jpg",
    "detail_urls": [
      "https://products-api.quickstark.com/product-images/...-detail-1.jpg"
    ]
  },
  "attributes": {
    "color": "Sage",
    "material": "cotton",
    "gender": "women",
    "season": "spring"
  },
  "inventory_summary": {
    "total_units": 12,
    "in_stock_units": 12,
    "preorder_units": 0,
    "store_count": 2,
    "in_stock_store_count": 2,
    "availability": "in_stock"
  }
}
```

## Image Generation Progress

The background image API updates counts when a batch finishes, not per individual
variant. A running job may show `attempted: 0` until it completes.

```bash
curl -s "https://products-api.quickstark.com/admin/product-images/jobs?limit=20" \
  | jq '.jobs[] | {id, category, status, attempted, generated, skipped, failed_count}'
```

The category orchestration script repeats category batches until the API returns
`attempted: 0`, which means no matching variants remain without images.
