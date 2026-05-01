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
product detail, recommendations, generated image URLs, image-job progress, and
the retail chat surface.

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
https://sterling-hollis-be.quickstark.com
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
| Storefront chat | `POST` | `/api/chat` |

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
8. Use `/api/chat` for open storefront chat: product questions, catalog search,
   related products, store phone/contact information, approved service answers,
   and signed-in order/account/personal recommendation requests.

## Storefront Chat

`POST /api/chat` is the stable frontend chat contract. Authentication is optional:

- Anonymous shoppers can ask product, catalog, related-product, store contact,
  and general customer-service questions.
- Account, order-status, personal-size, personal-style, purchase-history, and
  personalized recommendation requests require a valid `Authorization: Bearer
  <Clerk token>` header.
- The frontend must not send `customer_id`. The backend derives customer identity
  only from the signed-in token and linked customer record.

Request:

```json
{
  "message": "What phone number can I call for this store?",
  "conversation_id": "chat_abc123",
  "context": {
    "page_type": "product",
    "route": "/product/cat_123",
    "product_id": "cat_123",
    "current_product": {
      "id": "cat_123",
      "title": "Ivory Leather Shoulder Bag",
      "category": "handbags",
      "brand": "Example Brand",
      "attributes": {
        "color": "ivory",
        "material": "leather"
      }
    },
    "category": "handbags",
    "store_id": "1001"
  }
}
```

Response:

```json
{
  "conversation_id": "chat_abc123",
  "message": "Dallas Downtown is at 1 Main St, Dallas, TX 75201. You can call 555-111-2222.",
  "identity_status": "anonymous",
  "intent": "general_style",
  "route": "simple_tool",
  "cards": [],
  "actions": [],
  "tool_trace": [
    {"name": "triage", "decision": "store-info wording"},
    {"name": "store_info", "decision": "resolved from backend store context"},
    {"name": "ChatIntakeAgent", "decision": "deterministic_fallback_no_openai; confidence=0.50"},
    {"name": "CustomerServiceAgent", "decision": "selected_tool=store_info"},
    {"name": "auth_gate", "decision": "allowed; public store info"}
  ],
  "evaluator_confidence": 0.5,
  "selected_agent": "CustomerServiceAgent",
  "selected_tool": "store_info",
  "requires_followup": false,
  "clarifying_question": null
}
```

Frontend handling rules:

- Render `message` as the primary assistant text.
- Render `cards[]` with the standard product-card component and `actions[]` as
  CTA buttons. `view_product` actions link to product detail pages.
- If `route` is `blocked`, render `message` plus the first `sign_in` or
  `link_account` action.
- If `requires_followup` is true, render `clarifying_question` as the assistant
  prompt and keep the same `conversation_id` on the next turn.
- Treat `tool_trace`, `evaluator_confidence`, `selected_agent`, and
  `selected_tool` as diagnostics or analytics fields. The frontend should not
  branch critical security behavior on them.

Supported chat intents:

- `catalog_search`
- `complementary_products`
- `product_question`
- `account_question`
- `customer_recommendation`
- `general_style`

Supported selected tools:

- `product_detail`
- `semantic_catalog_search`
- `related_products`
- `customer_recommendations`
- `customer_summary`
- `store_info`
- `service_answer`
- `order_status`
- `chat_response`

Blocked auth example:

```json
{
  "message": "Please sign in before I look up account details or customer-specific recommendations.",
  "identity_status": "anonymous",
  "route": "blocked",
  "intent": "account_question",
  "cards": [],
  "actions": [
    {"type": "sign_in", "label": "Sign in", "href": "/sign-in"}
  ],
  "selected_agent": "OrderAgent",
  "selected_tool": "order_status"
}
```

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
  "image_url": "https://sterling-hollis-be.quickstark.com/product-images/...",
  "images": {
    "thumbnail_url": "https://sterling-hollis-be.quickstark.com/product-images/...-thumb.jpg",
    "primary_url": "https://sterling-hollis-be.quickstark.com/product-images/...-detail-1.jpg",
    "detail_urls": [
      "https://sterling-hollis-be.quickstark.com/product-images/...-detail-1.jpg"
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
curl -s "https://sterling-hollis-be.quickstark.com/admin/product-images/jobs?limit=20" \
  | jq '.jobs[] | {id, category, status, attempted, generated, skipped, failed_count}'
```

The category orchestration script repeats category batches until the API returns
`attempted: 0`, which means no matching variants remain without images.
