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
- `default_variant_id` and `variants[]` are deprecated compatibility fields.
  New clients must use the canonical product, `media[]`, and `inventory[]`
  fields instead.
- Stores are inventory context only. Use `store_id` as a filter or inventory
  dimension, not as product identity.
- Product cards are canonical products with a product-level price range, images,
  attributes, and inventory summary.
- Product detail adds canonical `media[]` and per-store `inventory[]`. Its single
  `variants[]` entry is a read-only projection for legacy consumers and must not
  be used for writes or new UI state.

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
| List owned API traces | `GET` | `/api/admin/traces` |
| Read one owned API trace | `GET` | `/api/admin/traces/{trace_id}` |
| Catch up on ordered trace events | `GET` | `/api/admin/traces/{trace_id}/events` |
| Ingest a bounded browser trace event | `POST` | `/api/admin/traces/{trace_id}/events` |
| Stream owned trace events | `GET` | `/api/admin/traces/{trace_id}/stream` |
| Download sanitized trace JSON | `GET` | `/api/admin/traces/{trace_id}/export` |
| List canonical brands, stores, categories, and availability choices | `GET` | `/api/admin/catalog/v2/references` |
| Add a canonical brand | `POST` | `/api/admin/catalog/v2/brands` |
| Search products across lifecycle states | `GET` | `/api/admin/catalog/products` |
| Create private product draft | `POST` | `/api/admin/catalog/products/drafts` |
| Start a private revision from a published snapshot | `POST` | `/api/admin/catalog/products/{product_id}/revisions` |
| Revise an existing product privately | `PUT` | `/api/admin/catalog/products/{product_id}/draft` |
| Publish an approved draft | `POST` | `/api/admin/catalog/products/{product_id}/publish` |
| Archive a published product | `POST` | `/api/admin/catalog/products/{product_id}/archive` |
| Inspect product lifecycle and draft history | `GET` | `/api/admin/catalog/products/{product_id}` |
| Upload private supplier source bundle | `POST` | `/api/admin/catalog/source-bundles` |
| List private supplier source bundles | `GET` | `/api/admin/catalog/source-bundles` |
| Read private supplier source bundle | `GET` | `/api/admin/catalog/source-bundles/{bundle_id}` |
| Read bounded supplier image preview | `GET` | `/api/admin/catalog/source-bundles/{bundle_id}/assets/{asset_id}/preview` |
| Remove unattached supplier source | `DELETE` | `/api/admin/catalog/source-bundles/{bundle_id}/assets/{asset_id}` |
| Promote supplier source to approved draft media | `POST` | `/api/admin/catalog/source-bundles/{bundle_id}/assets/{asset_id}/promote` |
| Start sanitized OpenAI catalog workflow | `POST` | `/api/admin/catalog/workflows` |
| Append ordered workflow event | `POST` | `/api/admin/catalog/workflows/{workflow_id}/events` |
| Generate or refine moderated product draft | `POST` | `/api/admin/catalog/workflows/{workflow_id}/draft-commands` |
| Create a short-lived Realtime voice session | `POST` | `/api/admin/catalog/workflows/{workflow_id}/realtime/sessions` |
| Execute an approved Realtime draft tool call | `POST` | `/api/admin/catalog/workflows/{workflow_id}/realtime/tool-calls` |
| Generate or refine draft imagery | `POST` | `/api/admin/catalog/workflows/{workflow_id}/image-commands` |
| Create a product media variation | `POST` | `/api/admin/catalog/workflows/{workflow_id}/media-commands` |
| Set main, reorder, remove, or restore product media | `POST` | `/api/admin/catalog/workflows/{workflow_id}/media-mutations` |
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
4. Use `/api/products/{product_id}` for PDP data. Read product-level facts,
   `media[]`, and `inventory[]`; ignore deprecated `variants[]` and
   `default_variant_id` in new code.
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
- `CATALOG_STUDIO_REALTIME_ENABLED`, `CATALOG_STUDIO_REALTIME_MODEL`, and
  `CATALOG_STUDIO_REALTIME_TRANSCRIPTION_MODEL`
- `CATALOG_STUDIO_REALTIME_CLIENT_SECRET_TTL_SECONDS` and
  `CATALOG_STUDIO_REALTIME_TIMEOUT_SECONDS`
- `CATALOG_STUDIO_REALTIME_SAFETY_IDENTIFIER_SECRET`

Existing demo-observability allowlists and `CLERK_DEMO_CUSTOMER_EMAIL` remain
valid administrator sources for backward compatibility. The response contains
booleans describing whether Responses, Moderation, Image Generation, Realtime,
worker storage, API trace capture, and catalog dependencies are configured. An unavailable
Realtime capability also includes one safe reason: `feature_disabled`,
`missing_api_key`, or `missing_safety_secret`. It does not probe providers or
return configuration values.

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
    "realtime": {"configured": false, "reason": "feature_disabled"},
    "worker_storage": {"configured": true},
    "api_traces": {"configured": false, "reason": "feature_disabled"},
    "catalog": {"configured": true, "authoring_schema_version": 3}
  }
}
```

Missing or invalid Clerk credentials return `401`. Valid non-administrator
credentials return `403`. Successful responses use `Cache-Control: no-store`.

### Production Realtime readiness check

Realtime has three required production values and four optional overrides:

| Setting | Production requirement | Default | Secret |
| --- | --- | --- | --- |
| `OPENAI_API_KEY` | required | none | yes |
| `CATALOG_STUDIO_REALTIME_ENABLED` | must be `true` | `false` | no |
| `CATALOG_STUDIO_REALTIME_SAFETY_IDENTIFIER_SECRET` | required and non-empty | none | yes |
| `CATALOG_STUDIO_REALTIME_MODEL` | optional | `gpt-realtime-2` | no |
| `CATALOG_STUDIO_REALTIME_TRANSCRIPTION_MODEL` | optional | `gpt-4o-mini-transcribe` | no |
| `CATALOG_STUDIO_REALTIME_CLIENT_SECRET_TTL_SECONDS` | optional | `600` | no |
| `CATALOG_STUDIO_REALTIME_TIMEOUT_SECONDS` | optional | `15` | no |

After deploying backend `main`, verify the deployment's Realtime configuration
gate, migration, and health-check steps succeeded. Then request the authenticated
administrator session and require this safe response before requesting browser
media access:

```json
{
  "capabilities": {
    "realtime": {"configured": true}
  }
}
```

Do not infer provider or browser health from that response. The final production
check is to grant microphone access in Catalog Studio, reach `listening`, complete
one instruction, and stop the session. Keep text authoring enabled when the
browser reports permission denial or when session creation, provider exchange,
or WebRTC transport fails; those are distinct operational boundaries from the
three configuration reasons returned by `GET /api/admin/session`.

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

## API Trace Capture

API trace capture is disabled unless `API_TRACE_CAPTURE_ENABLED=true`. The
browser may send W3C `traceparent` and `tracestate` headers. Middleware validates
that context and creates a child request span, but the headers alone never
authorize or persist data. Persistence and trace API access begin only after the
existing Catalog Studio administrator dependency binds the Clerk owner.

Authorized responses expose `traceparent`, `tracestate`, `X-Trace-Id`,
`X-Trace-Span-Id`, and `X-Trace-Capture`. CORS accepts those request headers only
from configured origins. Unknown origins and request headers fail preflight.

Use the owner-scoped API as follows:

1. List recent traces with `GET /api/admin/traces?limit=25`. Follow the opaque
   `next_cursor` when present.
2. Read a projection with `GET /api/admin/traces/{trace_id}` or resume ordered
   events with `GET /api/admin/traces/{trace_id}/events?after_sequence=12`.
3. Append only allowlisted, bounded browser events with `POST` to the events
   route. Stable `event_id` values make retries idempotent.
4. Stream with authenticated `fetch`, not `EventSource`, so the Clerk bearer
   token remains in the `Authorization` header. The server rejects `token`,
   `access_token`, `authorization`, and `bearer` query parameters. Resume with
   `after_sequence`; the stream sends ordered `trace_event` records, keepalive
   comments, and an `expired` event when retained metadata disappears.
5. Download the same versioned, sanitized projection from the `/export` route.
   After payload retention expires, the JSON remains structurally valid and
   carries retention markers instead of prior payload values.

All trace lookup failures return `404` without revealing whether another owner
has the identifier. Disabled capture returns `503`; `/api/admin/session` reports
the safe `api_traces` capability reason `feature_disabled`.

## Catalog Studio Supplier Sources

Supplier handoff images are private authoring evidence, not public product
media. Upload one ordered bundle with `multipart/form-data`:

```http
POST /api/admin/catalog/source-bundles
Authorization: Bearer <Clerk token>
Content-Type: multipart/form-data

title=Fall supplier handoff
draft_revision_id=draft_...
files=<front.jpg>
files=<detail.png>
```

JPEG, PNG, and WebP inputs are validated from their bytes and declared MIME
type before persistence. The backend also enforces per-file byte, dimension,
pixel-count, and per-bundle file-count limits. Client filenames may not contain
paths. Originals are stored below `CATALOG_SOURCE_OUTPUT_DIR`, which is not
mounted as a public static directory. Responses expose stable IDs, checksums,
dimensions, lifecycle state, and an authenticated bounded `preview_url`; they
never expose storage keys or filesystem paths.

Use `GET /api/admin/catalog/source-bundles` to restore the owner's bundles and
`GET /api/admin/catalog/source-bundles/{bundle_id}` for one bundle. Preview
responses use `Cache-Control: private, no-store`. Other administrators receive
`404` for bundle, preview, removal, and promotion requests so ownership is not
disclosed.

Upload and promotion responses use the same private, no-store policy. Preview
responses also set `X-Content-Type-Options: nosniff`.

Delete only unattached assets:

```http
DELETE /api/admin/catalog/source-bundles/{bundle_id}/assets/{asset_id}
Authorization: Bearer <Clerk token>
```

Removal is blocked while an image job uses the source and after promotion, when
the source is retained for lineage. Explicit promotion copies a metadata-stripped
public derivative into managed product-image storage and creates a new versioned
draft with one approved media item. It does not change price or inventory:

```http
POST /api/admin/catalog/source-bundles/{bundle_id}/assets/{asset_id}/promote
Authorization: Bearer <Clerk token>
Idempotency-Key: promote-supplier-front-1
Content-Type: application/json

{
  "draft_id": "draft_...",
  "expected_draft_version": 1
}
```

A bundle remains reusable as its product moves through later draft revisions.
Promoted media IDs and public derivative filenames are opaque and do not embed
the private bundle ID, source asset ID, or supplier filename. Storage failures
return `503`; removal does not delete the database record until both managed
private files have been removed.

Relevant settings and defaults are:

| Setting | Default |
| --- | --- |
| `CATALOG_SOURCE_OUTPUT_DIR` | `data/catalog-sources` |
| `CATALOG_SOURCE_UPLOAD_MAX_BYTES` | `8388608` |
| `CATALOG_SOURCE_MAX_ASSETS_PER_BUNDLE` | `20` |
| `CATALOG_SOURCE_MAX_DIMENSION` | `12000` |
| `CATALOG_SOURCE_MAX_PIXELS` | `40000000` |
| `CATALOG_SOURCE_THUMBNAIL_SIZE` | `320` |

## Catalog Studio Product Lifecycle

### Structured authoring and reviewable suggestions (v3)

The session capability `catalog.authoring_schema_version: 3` enables the v3
authoring contract. V3 preserves the canonical v2 product, media, and inventory
shape while adding benefits, specifications, care instructions, content
details, SEO, private supplier-source references, media alt text, and explicit
readiness inputs. V2 reads remain available as a deterministic projection of a
v3 draft. V1 or v2 replacement writes against a current v3 draft return `409`
so an older client cannot erase v3-only content.

Use these v3 routes for structured authoring:

- `POST /api/admin/catalog/v3/products/drafts` creates a v3 draft.
- `GET /api/admin/catalog/v3/products/{product_id}` returns published and
  private v3 state, including calculated readiness for the current draft.
- `PUT /api/admin/catalog/v3/products/{product_id}/draft` replaces a current v3
  snapshot with published and draft version guards.
- `POST /api/admin/catalog/v3/products/{product_id}/revisions` starts a v3
  revision from the published product.
- `GET .../drafts/{draft_id}/readiness` separates publication blockers from
  non-blocking recommendations.
- `GET .../drafts/{draft_id}/preview` omits private source references,
  readiness inputs, and server-only storage fields.
- `POST /api/admin/catalog/v3/products/{product_id}/publish` applies readiness
  blockers before publication and persists structured copy privately. Supplier
  source references remain draft-only and are not copied into published product
  metadata.

AI output is stored as suggestions, never as an implicit product overwrite.
Create a set with `POST .../suggestion-sets`, list private sets with `GET
.../suggestion-sets`, and apply an idempotent decision with `POST
.../suggestion-sets/{suggestion_set_id}/decisions`. A decision may target one
suggestion, one section, or every remaining suggestion. Section acceptance is
atomic: either the complete resulting product validates and creates one new
draft revision, or no suggestion or draft changes. Each accepted field records
its prior value, evidence asset IDs, certainty, input origin, reviewer, reason,
and resulting draft revision.

`input_origin` is one of `supplier_analysis`, `typed_action`, or `voice`.
Voice therefore remains a first-class authoring input while using the same
review, optimistic concurrency, idempotency, and audit rules as typed actions.
Actual Responses and Realtime generation remain capability-gated; the v3
suggestion review endpoints themselves do not require an OpenAI API key.

### Assisted customer-review moderation

Product reviews are imported only from trusted fixtures or operator tooling;
public submission and provider ingestion are not exposed. Customer display
name, text, rating, and submission time are immutable and stored separately
from merchant moderation state.

Use these protected routes in Catalog Studio:

- `GET /api/admin/catalog/products/{product_id}/reviews` lists original review
  content, versioned moderation state, bounded AI themes, response drafts, and
  the append-only merchant action history.
- `POST .../reviews/{review_id}/assist` runs Responses with structured output
  and moderation. It may stage categories, a theme summary, a suggested action,
  and a response draft, but it never publishes a moderation decision.
- `POST .../reviews/{review_id}/decisions` requires an `Idempotency-Key`, the
  current moderation version, a reason, and one of `approve`, `flag`, `reject`,
  `save_response`, or `publish_response`.

Only approved reviews appear in public product detail responses. Merchant
responses remain private until separately published. Public responses omit AI
analysis, external source IDs, decision reasons, actor IDs, provider metadata,
and unpublished response drafts. Provider failure, policy blocking, and stale
versions leave the prior public state unchanged.

Trusted fixtures can be imported with:

```bash
python scripts/seed_product_reviews.py --product-id <catalog-product-id>
```

### Canonical product-level v2 contract

New merchandising clients use `/api/admin/catalog/v2/products`. The v2 draft
contains product-level price, link, color, material, gender, season, media, and
store inventory. It never returns `variants`, `variant_axes`,
`primary_variant_index`, objective weights, or variant-owned inventory.

Load `GET /api/admin/catalog/v2/references` once per editor session for sorted,
admin-safe brands, named stores, canonical categories, and availability choices.
The response intentionally omits store source metadata and technical seed data.
Create missing brands with `POST /api/admin/catalog/v2/brands` and a fresh
`Idempotency-Key`; normalized case/whitespace collisions return `409` and never
create a duplicate. V2 drafts must send the selected `brand_id` together with
its returned display `brand`, and every inventory `store_id` must exist in the
reference response.

The v2 lifecycle mirrors the existing optimistic, idempotent workflow:

- `GET /api/admin/catalog/v2/products` lists lifecycle state.
- `POST /api/admin/catalog/v2/products/drafts` creates a private product draft.
- `GET /api/admin/catalog/v2/products/{product_id}` returns separate published
  and current product-level snapshots.
- `PUT /api/admin/catalog/v2/products/{product_id}/draft` saves a complete
  private product snapshot with both published and draft version guards.
- `POST /api/admin/catalog/v2/products/{product_id}/revisions` starts from the
  current canonical published state.
- `POST /api/admin/catalog/v2/products/{product_id}/publish` atomically promotes
  product fields, media, and inventory.
- `POST /api/admin/catalog/v2/products/{product_id}/archive` retains history
  while removing the product from public reads.

Inventory identity is `product_id + store_id + normalized optional size`.
Omitting size and sending a blank size are the same identity; duplicate rows,
negative quantities, unknown stores, stale versions, and invalid price ranges
fail without partial writes.

```json
{
  "expected_version": 0,
  "moderation_state": "approved",
  "product": {
    "schema_version": 2,
    "seed_run_id": "run_catalog",
    "title": "Studio Coat",
    "description": "A structured wool coat.",
    "brand_id": "brand_0dfb170c78a23d9c30ff",
    "brand": "Sterling Hollis",
    "category": "womens_apparel",
    "price_min": 250,
    "price_max": 300,
    "link": "https://example.com/studio-coat",
    "color": "Black",
    "material": "wool",
    "gender": "women",
    "season": "fall",
    "media": [],
    "inventory": [{
      "store_id": "1001",
      "size": null,
      "availability": "in stock",
      "inventory_qty": 8
    }]
  }
}
```

The unversioned routes below remain a bounded compatibility adapter for the
currently deployed editor. Their variant-shaped reads and writes translate to
canonical product fields and inventory, while legacy variant tables remain only
as a temporary public/read compatibility projection.

All Catalog Studio product routes require the same Clerk administrator policy
as `/api/admin/session`. Drafts are private: they never appear in catalog lists,
detail, search, related products, or recommendations. Editing an existing
product also leaves its published snapshot visible until publication succeeds.

`GET /api/admin/catalog/products` searches published, archived, and private
draft state with `q`, `lifecycle_status`, `category`, `brand`, `page`, and
`page_size`. Product detail returns separate `published_snapshot` and
`current_draft` objects, including variants, inventory, safe image state, and
the draft version needed for the next edit. Server-only file paths are never
returned.

Every mutation requires an `Idempotency-Key` header and an `expected_version`
in its body. A retry with the same key and identical request replays the first
result. Reusing the key for a different request returns `409`. A stale published
version also returns `409` without changing public state.

The lifecycle is:

1. `POST /api/admin/catalog/products/drafts` creates a new-product draft with
   `expected_version: 0`.
2. `POST /api/admin/catalog/products/{product_id}/revisions` clones an existing
   published or archived snapshot into a private draft. An optional
   `workflow_id` links that draft to an existing administrator-owned workflow
   in the same transaction.
3. `PUT /api/admin/catalog/products/{product_id}/draft` replaces the complete
   private snapshot. Send the detail response's `current_draft.revision.id` as
   `current_draft_id` and `current_draft.draft_version` as
   `expected_draft_version`; stale edits return `409`. Safe detail responses can
   be round-tripped without deleting hidden image-worker state. Explicitly
   clearing `image_link` and `image_set` clears only the private reference.
4. The draft captures product fields, variants, per-store inventory, image
   selection, and `moderation_state`.
5. `POST /api/admin/catalog/products/{product_id}/publish` atomically replaces
   the published product, variant, and inventory projection. Publication is
   rejected unless moderation is `approved` and all referenced stores exist.
6. `POST /api/admin/catalog/products/{product_id}/archive` removes the product
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

The success response contains a canonical `schema_version: 2` private product
draft and the current business timeline. The Responses schema uses a canonical
`brand_id`/`brand` pair, product-level price and attributes, optional initial
inventory, and image intent. It does not generate variant families,
`variant_axes`, or `primary_variant_index`. The model may select only brands and
stores supplied by the backend. If the requested brand is absent, create it
through **Add Brand** and retry; the command returns `unknown_catalog_brand`
without persisting a draft.

The timeline records distinct Moderation and Responses events. The owner-only
`developer=true` workflow projection adds bounded model,
request ID, latency, usage, policy, and structured-output metadata without the
presenter instruction, system instructions, private reasoning, or raw provider
objects. Provider timeouts and invalid structured output leave the prior draft
unchanged and return a safe error whose `detail` object contains `code`,
`message`, and `retryable` fields.

## Realtime Voice Draft Commands

Realtime voice is an alternate input for the same private draft workflow; it is
not a separate catalog persistence path. First create or open a catalog workflow,
then request a short-lived browser credential:

```http
POST /api/admin/catalog/workflows/{workflow_id}/realtime/sessions
Authorization: Bearer <Clerk token>
```

The response is never cacheable and contains an ephemeral `client_secret`, its
Unix `expires_at`, the configured model, the OpenAI WebRTC URL, and exactly one
tool appropriate for the current workflow state: `create_catalog_draft` for an
empty workflow or `refine_catalog_draft` for a workflow with a current draft.
The frontend uses that secret to send its SDP offer directly to OpenAI at the
returned `webrtc_url`; raw microphone audio does not pass through or persist in
the Sterling Hollis backend. The standard `OPENAI_API_KEY` is never returned.

When Realtime emits a function call, relay the allowlisted call through the
authenticated backend:

```http
POST /api/admin/catalog/workflows/{workflow_id}/realtime/tool-calls
Authorization: Bearer <Clerk token>
Idempotency-Key: voice-draft-call-1
Content-Type: application/json
```

```json
{
  "call_id": "call_voice_1",
  "name": "refine_catalog_draft",
  "arguments": {
    "instruction": "Change the coat to ivory and keep the silhouette.",
    "current_draft_id": "draft_...",
    "expected_draft_version": 1
  }
}
```

This endpoint accepts only create/refine draft tools and delegates to the same
moderated, version-checked, idempotent Responses command used by the text UI.
Publication, archive, direct catalog writes, and arbitrary tool names are
rejected. Timeline events store the tool name, draft identifiers, version, and
status but omit the transcript and raw audio. Session creation failures return
the standard safe `{code, message, retryable}` detail object.

Production enables this capability with `CATALOG_STUDIO_REALTIME_ENABLED=true`
and a dedicated random `CATALOG_STUDIO_REALTIME_SAFETY_IDENTIFIER_SECRET`.
The backend HMACs the Clerk subject with that secret before sending the stable,
privacy-preserving `OpenAI-Safety-Identifier`; neither value is returned to the
frontend or written to the workflow timeline.

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

### Product media variations

Product media is independent of sellable variants and inventory. Catalog Studio
projects every distinct published product and legacy variant image into a stable
media item without truncating large galleries. A draft carries one approved
`core` media asset plus ordered `variation` assets for
color treatment, camera angle, scene, scale, people, or a bounded freeform
instruction. Creating media never creates a SKU, price, size, or inventory row.
`parameters` accepts at most eight primitive values; keys are limited to 64
characters and string values to 500 characters. `instruction` is limited to
2,000 characters.

```http
POST /api/admin/catalog/workflows/{workflow_id}/media-commands
Authorization: Bearer <Clerk token>
Idempotency-Key: media-scene-1
Content-Type: application/json

{
  "draft_id": "draft_...",
  "expected_draft_version": 1,
  "source_media_id": "media_core",
  "intent": "scene",
  "parameters": {"scene": "bright living room"}
}
```

The source may be any approved media asset. Edit instructions are moderated
before a job is created. Locally managed images are read from worker storage;
published HTTPS sources are materialized only when their origin is allowlisted,
every resolved and redirected address is public, and content type, byte limit,
redirect count, and timeout checks pass. The worker uses a high-fidelity image
edit, writes the result to a new
draft media asset, and returns its `target_media_id`. Poll and approve the job
through the existing image-job endpoints. Approval accepts
`approval_intent: "add"` or `approval_intent: "replace"`; replacement also
requires `replace_media_id` and preserves source and predecessor lineage.

Gallery-only changes use a new versioned draft revision and the same optimistic
concurrency contract as other edits:

```http
POST /api/admin/catalog/workflows/{workflow_id}/media-mutations
Authorization: Bearer <Clerk token>
Idempotency-Key: media-set-main-1
Content-Type: application/json

{
  "draft_id": "draft_...",
  "expected_draft_version": 2,
  "action": "set_main",
  "media_id": "media_..."
}
```

Use `action: "reorder"` with `ordered_media_ids`; the current main image must
remain first. `remove` rejects the last image, the current main image, and media
used by an active image job. `restore` recovers an approved media item from
earlier owned draft history. Publication requires every retained
media asset to be approved and exposes approved assets in `display_order` on
the public product detail response. Products without product media retain the
existing default-variant image fallback.

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

Public catalog lists, detail, search, recommendations, chat, MCP discovery, and
the deprecated OpenAI product feed all derive price, attributes, inventory, and
managed media from the same canonical product records. Draft, archived,
unapproved, and removed media remain private.

For compatibility, product cards still include `default_variant_id`, and detail
still includes one synthetic `variants[]` entry derived from the same canonical
facts. Both fields are deprecated, read-only, and instrumented on the backend so
remaining legacy reads can be removed safely.

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
