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
| Catalog Studio administrator session and capabilities | `GET` | `/api/admin/session` |
| Create private product draft | `POST` | `/api/admin/catalog/products/drafts` |
| Revise an existing product privately | `PUT` | `/api/admin/catalog/products/{product_id}/draft` |
| Publish an approved draft | `POST` | `/api/admin/catalog/products/{product_id}/publish` |
| Archive a published product | `POST` | `/api/admin/catalog/products/{product_id}/archive` |
| Inspect product lifecycle and draft history | `GET` | `/api/admin/catalog/products/{product_id}` |
| Start sanitized OpenAI catalog workflow | `POST` | `/api/admin/catalog/workflows` |
| Append ordered workflow event | `POST` | `/api/admin/catalog/workflows/{workflow_id}/events` |
| Generate or refine moderated product draft | `POST` | `/api/admin/catalog/workflows/{workflow_id}/draft-commands` |
| Generate or refine draft imagery | `POST` | `/api/admin/catalog/workflows/{workflow_id}/image-commands` |
| Generate coherent remaining variants | `POST` | `/api/admin/catalog/workflows/{workflow_id}/image-variant-sets` |
| Poll a coordinated variant set | `GET` | `/api/admin/catalog/workflows/{workflow_id}/image-variant-sets/{image_variant_set_id}` |
| Poll one draft image job | `GET` | `/api/admin/catalog/workflows/{workflow_id}/image-jobs/{job_id}` |
| Approve draft imagery | `POST` | `/api/admin/catalog/workflows/{workflow_id}/image-jobs/{job_id}/approve` |
| Read business or developer timeline | `GET` | `/api/admin/catalog/workflows/{workflow_id}` |
| Clerk demo fault state | `GET` | `/api/demo/observability` |
| Clerk demo fault toggle | `POST` | `/api/demo/observability` |
| Clerk demo fault reset | `POST` | `/api/demo/observability/reset` |

## Admin/Operations Endpoints Useful During Buildout

These are legacy operator controls for local scripts and maintenance. Public
deployments set `ENABLE_LEGACY_ADMIN_ROUTES=false`; if a production-like
environment explicitly enables them, the backend applies the same Catalog
Studio administrator policy used by `/api/admin/*`.

| Purpose | Method | Path |
| --- | --- | --- |
| Start background product image generation | `POST` | `/admin/product-images/generate` |
| Poll one image generation job | `GET` | `/admin/product-images/jobs/{job_id}` |
| List recent image generation jobs | `GET` | `/admin/product-images/jobs` |
| Get Datadog demo fault state | `GET` | `/admin/demo/observability` |
| Toggle Datadog demo faults | `POST` | `/admin/demo/observability` |
| Disable Datadog demo faults | `POST` | `/admin/demo/observability/reset` |
| Trigger Datadog demo unhandled error | `POST` | `/admin/demo/observability/trigger-error` |
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
- If `route` is `blocked`, render `message` plus the first `sign_in` action
  when present. Signed-in users whose email does not match a customer profile
  can receive a blocked response with no action.
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

## Catalog Studio Administrator Session

`GET /api/admin/session` is the server-authoritative browser entry point for
Catalog Studio authorization and capability availability. Send the Clerk
session token as `Authorization: Bearer <Clerk token>`. The token is verified
for signature, issuer, expiry, subject, and authorized party before the
administrator policy runs.

Administrators can be configured by normalized email, Clerk subject, or one
custom claim:

- `CATALOG_STUDIO_CLERK_AUTHORIZED_EMAILS`
- `CATALOG_STUDIO_CLERK_AUTHORIZED_SUBJECTS`
- `CATALOG_STUDIO_ADMIN_CLAIM_PATH` and `CATALOG_STUDIO_ADMIN_CLAIM_VALUE`
- `CATALOG_STUDIO_RESPONSES_MODEL` and `CATALOG_STUDIO_MODERATION_MODEL`
- `CATALOG_STUDIO_RESPONSES_TIMEOUT_SECONDS` and
  `CATALOG_STUDIO_RESPONSES_MAX_OUTPUT_TOKENS`

Existing demo-observability allowlists and `CLERK_DEMO_CUSTOMER_EMAIL` remain
valid administrator sources for backward compatibility. The response contains
only booleans describing whether Responses, Moderation, Image Generation,
Realtime, worker storage, and catalog dependencies are configured. It does not
probe providers or return configuration values.

```http
GET /api/admin/session
authorization: Bearer <Clerk token>
```

```json
{
  "authorized": true,
  "capabilities": {
    "responses": {"configured": true},
    "moderation": {"configured": true},
    "image_generation": {"configured": true},
    "realtime": {"configured": false},
    "worker_storage": {"configured": true},
    "catalog": {"configured": true}
  }
}
```

Missing or invalid Clerk credentials return `401`. Valid non-administrator
credentials return `403`. Successful responses use `Cache-Control: no-store`.

### Testing protected APIs in Swagger

The generated FastAPI docs are available at `/docs`. Public endpoints can use
**Try it out** directly. Protected Catalog Studio endpoints declare the
`ClerkBearer` security scheme:

1. Sign in to the Sterling Hollis frontend as an authorized Catalog Studio
   administrator and obtain the active Clerk session JWT.
2. Open `/docs`, select **Authorize**, and paste the raw JWT into the
   `ClerkBearer` value field. Swagger adds the `Bearer` prefix.
3. Call `GET /api/admin/session` first. Continue only after it returns `200` and
   `authorized: true`.
4. Use a fresh `Idempotency-Key` for each catalog mutation. Reuse the same key
   only when intentionally replaying the exact same request.

Never paste the standard OpenAI API key, Clerk secret key, or an authorization
header copied from another user into Swagger.

## Catalog Studio Product Lifecycle

All Catalog Studio product routes require the same Clerk administrator policy
as `/api/admin/session`. Drafts are private: they never appear in catalog lists,
detail, search, related products, or recommendations. Editing an existing
product also leaves its published snapshot visible until publication succeeds.

Every mutation requires an `Idempotency-Key` header and an `expected_version`
in its body. A retry with the same key and identical request replays the first
result. Reusing the key for a different request returns `409`. A stale version
also returns `409` without changing public state.

The lifecycle is:

1. `POST /api/admin/catalog/products/drafts` creates a new-product draft with
   `expected_version: 0`, or `PUT /api/admin/catalog/products/{product_id}/draft`
   stages a revision using the current published version.
2. The draft captures product fields, variants, per-store inventory, image
   selection, and `moderation_state`.
3. `POST /api/admin/catalog/products/{product_id}/publish` atomically replaces
   the published product, variant, and inventory projection. Publication is
   rejected unless moderation is `approved` and all referenced stores exist.
4. `POST /api/admin/catalog/products/{product_id}/archive` removes the product
   from every public read surface while retaining its administrative record and
   revision history.

Example draft mutation:

```http
POST /api/admin/catalog/products/drafts
authorization: Bearer <Clerk token>
idempotency-key: catalog-demo-product-1
content-type: application/json
```

```json
{
  "expected_version": 0,
  "moderation_state": "approved",
  "product": {
    "seed_run_id": "run_catalog",
    "title": "Studio Coat",
    "description": "A structured Catalog Studio product.",
    "brand": "Sterling Hollis",
    "category": "womens_apparel",
    "metadata": {"source": "catalog_studio"},
    "variants": [{
      "color": "Black",
      "material": "wool",
      "price_min": 250,
      "price_max": 250,
      "image_link": "https://cdn.example/studio-coat.jpg",
      "inventory": [{
        "store_id": "1001",
        "size": "M",
        "availability": "in stock",
        "inventory_qty": 8,
        "objective_weight": 0.9
      }]
    }]
  }
}
```

## Sanitized OpenAI Workflow Timeline

`POST /api/admin/catalog/workflows` starts an administrator-owned workflow and
records its first ordered business event. It requires an `Idempotency-Key` so
an exact start retry returns the original workflow. Subsequent stages append through
`POST /api/admin/catalog/workflows/{workflow_id}/events` with a stable
`client_event_id`; an exact retry replays the existing event and conflicting
reuse returns `409`.

`GET /api/admin/catalog/workflows/{workflow_id}` returns the business timeline by
default. Add `?developer=true` to include model, request ID, duration, normalized
usage, moderation summary, error code, and bounded request/response projections.
Developer fields are available only to the workflow owner. If shared catalog workflows are
enabled, other administrators receive only the business projection.

Sanitization happens before persistence. Authorization data, credentials,
system instructions, private reasoning, raw audio, binary image data, customer
identity, configured private keys, oversized strings/arrays/objects, and unknown
fields are redacted, omitted, or replaced with deterministic truncation markers.
After the configured retention period, event payloads are replaced with an
expiry marker while workflow metadata and catalog records remain intact.

## Responses and Moderation Draft Commands

`POST /api/admin/catalog/workflows/{workflow_id}/draft-commands` turns one bounded
presenter instruction into a private, schema-validated product draft. The
backend uses the Responses API with strict structured output and requests input
and output moderation signals in the same provider call. The application blocks
the command when either moderation result is flagged, and no generated copy or
draft is persisted in that case.

Every command requires an `Idempotency-Key`. An exact replay returns the saved
result without another Responses call. To refine a draft, send its current ID
and the `draft_version` returned by the previous command. Refinements create a
new private revision for the same product, so the published catalog remains
unchanged until an administrator explicitly publishes an approved revision.

```http
POST /api/admin/catalog/workflows/{workflow_id}/draft-commands
authorization: Bearer <Clerk token>
idempotency-key: catalog-demo-draft-1
content-type: application/json
```

```json
{
  "instruction": "Create a black wool evening coat with architectural lines.",
  "expected_draft_version": 0
}
```

For a follow-up refinement:

```json
{
  "instruction": "Change the color to ivory and keep the price unchanged.",
  "current_draft_id": "draft_...",
  "expected_draft_version": 1
}
```

The success response contains the validated private draft and the current
business timeline. The timeline records distinct Moderation and Responses
events. The owner-only `developer=true` workflow projection adds bounded model,
request ID, latency, usage, policy, and structured-output metadata without the
presenter instruction, system instructions, private reasoning, or raw provider
objects. Provider timeouts and invalid structured output leave the prior draft
unchanged and return a safe error whose `detail` object contains `code`,
`message`, and `retryable` fields.

## Catalog Studio Image Commands

After a draft command succeeds, enqueue one image for a specific draft variant:

```http
POST /api/admin/catalog/workflows/{workflow_id}/image-commands
Authorization: Bearer <Clerk token>
Idempotency-Key: image-command-1
Content-Type: application/json

{
  "action": "generate",
  "draft_id": "draft_...",
  "expected_draft_version": 1,
  "variant_index": 0
}
```

The existing background image worker processes the job with the configured
Image API model and one medium-quality image by default. Poll the returned job,
then approve a successful result using the same current draft ID and version.
Publication rejects Catalog Studio generated images that remain in `review`.

To refine an image, first approve its current result, then submit `action:
"refine"` with a `refinement_prompt`. Refinement uses the approved image as its
input and preserves its history in the private draft. If the draft changes while
the provider request is running, the worker discards the late result and reports
a retryable `stale_draft` timeline event.

AI-authored product drafts also carry a shared `design_specification`, declared
`variant_axes` (`color` and/or `material`), and `primary_variant_index`. Generate
and approve that primary variant through the single-image command before
requesting the rest of the family:

```http
POST /api/admin/catalog/workflows/{workflow_id}/image-variant-sets
Authorization: Bearer <Clerk token>
Idempotency-Key: image-family-1
Content-Type: application/json

{
  "draft_id": "draft_...",
  "expected_draft_version": 1
}
```

The response includes one `image_variant_set_id`, aggregate status, and the
latest child job for each non-primary variant. Children use Image API edits from
the approved primary and change only the declared axes. Poll the variant-set
URL, approve each successful child with the normal approval endpoint, and wait
for `complete` before publishing. A partial failure reports
`partially_failed`; repeating the command queues only failed children and does
not duplicate queued, successful, or approved siblings.

## Demo Observability Toggle

The Datadog demo fault harness is an operator/demo control. Do not expose it as
a normal shopper-facing setting. Clerk-authenticated demo panels should call
the `/api/demo/observability` endpoints with `Authorization: Bearer <Clerk token>`.
The caller must match `DEMO_OBSERVABILITY_CLERK_AUTHORIZED_EMAILS`,
`DEMO_OBSERVABILITY_CLERK_AUTHORIZED_SUBJECTS`, or `CLERK_DEMO_CUSTOMER_EMAIL`.
Local/admin tooling can use `/admin/demo/observability` only when legacy admin
routes are enabled. Public deployments use the protected `/api` endpoints.

```http
GET /api/demo/observability
```

Response:

```json
{
  "enabled": false,
  "mode": "off",
  "latency_seconds": 8,
  "target_store_id": "1001",
  "incident_id": "demo-atp-supplier-feed-2026-05-06",
  "correlation_key": "sterling-hollis-atp-reconciliation",
  "network_device": "DATACENTER-USER-SW11A",
  "network_site": "dc01",
  "outage_scope": "storefront_api",
  "network_event_count": 3,
  "snmp_trap_log": {
    "ddsource": "snmp-traps",
    "hostname": "datacenter-user-sw11a",
    "topology_role": "parent",
    "correlation_key": "sterling-hollis-network-outage"
  },
  "snmp_trap_logs": [
    {
      "ddsource": "snmp-traps",
      "hostname": "datacenter-user-sw11a",
      "topology_role": "parent",
      "topology_child_device": "store-fulfillment-edge01",
      "correlation_key": "sterling-hollis-network-outage"
    },
    {
      "ddsource": "snmp-traps",
      "hostname": "store-fulfillment-edge01",
      "topology_role": "child",
      "topology_parent_device": "datacenter-user-sw11a",
      "correlation_key": "sterling-hollis-network-outage"
    },
    {
      "ddsource": "snmp-traps",
      "hostname": "gmtek5000",
      "status": "error",
      "topology_role": "dependent_server",
      "topology_parent_device": "store-fulfillment-edge01",
      "error_type": "gmtek5000_network_dependency_error",
      "correlation_key": "sterling-hollis-network-outage"
    }
  ]
}
```

To enable latency in the chat path:

```http
POST /api/demo/observability
authorization: Bearer <Clerk token>
content-type: application/json
```

```json
{
  "enabled": true,
  "mode": "latency",
  "latency_seconds": 8,
  "target_store_id": "1001",
  "network_event_count": 3
}
```

Supported `mode` values are `off`, `latency`, `error`, and
`latency_and_error`, and `network_outage`. Use
`POST /api/demo/observability/reset` to turn it off from the Clerk demo panel.
When enabled for a matching `store_id`, the next `/api/chat` turn emits an
`available_to_promise_reconciliation` `tool_trace` entry. Error modes degrade
that reconciliation step but still return the normal chat response.

For a network outage demo, first call:

```http
POST /admin/demo/observability/network-outage-log
```

Clerk-authenticated demo panels can call the equivalent endpoint:

```http
POST /api/demo/observability/network-outage-log
authorization: Bearer <Clerk token>
```

Those endpoints send the `snmp_trap_logs` payload list to Datadog Logs HTTP Intake
with the backend `DD_API_KEY`, using the same `http-intake.logs.<site>/api/v2/logs`
path as the standalone simulator projects. `network_event_count` controls the
number of emitted log events, capped at 25. The default of 3 emits one parent
switch trap, one downstream child trap, and one `gmtek5000` dependent-server
logical error. Larger counts rotate through those three nodes and interface
down/up states to mimic a flapping network dependency.
After the log send succeeds, enable `network_outage`. While active, app-facing API paths return
`503 Service Unavailable` with the same `incident_id` and `correlation_key`;
`/health`, `/admin/demo/observability/reset`, and
`/api/demo/observability/reset` remain available so the demo is recoverable
from the frontend.

Use `POST /admin/demo/observability/trigger-error` only when the demo needs a
real unhandled backend 500 for Datadog Error Management grouping. Do not call
that endpoint from the shopper chat flow.

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

This is an admin/operator check, not a shopper-facing frontend call. Use it from
local tooling or from an authenticated admin surface protected outside the
retail app.

The background image API updates counts when a batch finishes, not per individual
variant. A running job may show `attempted: 0` until it completes.

```bash
curl -s "http://localhost:8000/admin/product-images/jobs?limit=20" \
  | jq '.jobs[] | {id, category, status, attempted, generated, skipped, failed_count}'
```

The category orchestration script repeats category batches until the API returns
`attempted: 0`, which means no matching variants remain without images.
