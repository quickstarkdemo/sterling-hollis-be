# sterling-hollis-be

FastAPI backend for the Sterling Hollis synthetic fashion retail platform.

The service owns the Postgres data model, synthetic retail data generation,
catalog normalization, recommendation APIs, storefront chat, MCP operator tools,
ChatGPT Apps SDK widgets, and the Datadog demo/observability harness.

## What This Implements

- Postgres source of truth managed by Alembic.
- Live or cached store-source ingestion.
- Deterministic synthetic generators for stores, customers, products, orders,
  order items, store daily metrics, supplier product offers, and analyst story
  samples.
- Normalized catalog APIs for retail frontends.
- OpenAI and Pinecone product indexing when configured, with deterministic local
  vector fallback when provider keys are absent.
- Durable background jobs for product indexing and product image generation.
- Product image analysis and image-based recommendations.
- Storefront chat with optional Clerk authentication for account-specific flows.
- MCP tools for admin, associate, merchandising, inventory, and executive
  workflows.
- Apps SDK widget resources served from `/ui-assets`.
- Draft-first SMS and email workflows backed by persisted communication rows.
- Datadog tracing, LLM Observability, AI Guard, runtime metrics, and demo fault
  toggles.

## Project Structure

- `app/main.py`: FastAPI app factory, middleware, mounts, and health route.
- `app/config.py`: environment-backed runtime settings.
- `app/models.py`: SQLAlchemy schema.
- `app/routers/`: REST API routes.
- `app/services/`: generation, loading, indexing, recommendations, chat, image,
  observability, and widget services.
- `app/mcp_server.py`: MCP tools and widget resources.
- `alembic/versions/`: database migrations.
- `scripts/`: local operations, OpenAPI export, deployment, image generation,
  and smoke-test helpers.
- `deploy/`: production Docker Compose runtime assets.
- `docs/`: active integration and operations documentation.

## Documentation

Active docs live under `docs/`:

- [docs/README.md](docs/README.md): documentation index.
- [docs/frontend-api.md](docs/frontend-api.md): retail frontend integration
  guide.
- [docs/frontend-openapi.yaml](docs/frontend-openapi.yaml): curated frontend
  OpenAPI contract.
- [docs/openapi.json](docs/openapi.json): generated FastAPI schema export.
- [docs/chat-flow.excalidraw](docs/chat-flow.excalidraw): storefront chat flow
  diagram.
- [docs/datadog-reference-tables/README.md](docs/datadog-reference-tables/README.md):
  Datadog reference table import notes.

External OpenAI and MCP reference pages are intentionally not vendored in this
repo. Use the official upstream docs for those platforms so local docs do not
drift.

## Quick Start

### 1. Configure Environment

```bash
cp .env.example .env
```

For local Docker Compose, the bundled Postgres service and default local
settings are enough to boot the app. For direct local runs or production, set
`DATABASE_URL` or the `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, and
`PGPASSWORD` group.

Useful optional groups:

- Store source: `STORE_SOURCE_INDEX_URL`,
  `STORE_SOURCE_DETAIL_URL_TEMPLATE`, `STORE_SOURCE_CACHE_PATH`.
- Vector and recommendation providers: `OPENAI_API_KEY`, `PINECONE_API_KEY`,
  `PINECONE_INDEX_NAME`, `PINECONE_CATALOG_NAMESPACE`, `EMBEDDING_MODEL`,
  `EMBEDDING_DIMENSION`.
- Storefront/chat: `CHAT_ORCHESTRATION_MODE`, `CHAT_ORCHESTRATION_MODEL`,
  `CHAT_ORCHESTRATION_MIN_CONFIDENCE`.
- Public frontend/MCP deployment: `PUBLIC_BASE_URL`, `MCP_ALLOWED_HOSTS`,
  `MCP_ALLOWED_ORIGINS`, `CORS_ALLOWED_ORIGINS`,
  `ENABLE_MCP_ADAPTER`, `ENABLE_OPENAI_APPS_UI`.
- Clerk auth: `CLERK_ISSUER`, `CLERK_JWKS_URL`,
  `CLERK_AUTHORIZED_PARTIES`, `CLERK_DEMO_CUSTOMER_ID`,
  `CLERK_DEMO_CUSTOMER_EMAIL`.
- Product images and image recommendations: `IMAGE_ANALYSIS_MODEL`,
  `IMAGE_ANALYSIS_DETAIL`, `IMAGE_UPLOAD_MAX_BYTES`, and the
  `PRODUCT_IMAGE_*` settings.
- Customer communication: Twilio `TWILIO_*` values for SMS and
  `SES_REGION`, `SES_FROM_EMAIL`, `AMAZON_KEY_ID`, `AMAZON_KEY_SECRET` for
  email delivery.
- Datadog/OTEL: `DD_*`, `OTEL_*`, and `STRANDS_OTEL_ENABLED`.
- Feature gates: `EXEC_AUTO_OPTIMIZE_ENABLED`, `STRATEGY_PACKET_ENABLED`,
  `MERCH_STRATEGY_CONTEXT_ENABLED`, `ASSOCIATE_PRIORITY_TAGS_ENABLED`.
- Deployment: `DOCKERHUB_USER`, `DOCKERHUB_TOKEN`, `DOCKERHUB_IMAGE`,
  `API_PORT`.

`.env.example` is the detailed local reference. Keep secret values out of the
repo.

### Catalog Studio configuration

Catalog Studio uses the standard `OPENAI_API_KEY` for provider calls. Realtime
voice is available only when `CATALOG_STUDIO_REALTIME_ENABLED=true` and
`CATALOG_STUDIO_REALTIME_SAFETY_IDENTIFIER_SECRET` is also set to a dedicated
random secret. The safety secret is used only to HMAC the authenticated Clerk
user ID before it is sent as an OpenAI safety identifier.

| Setting | Default | Required when | Secret |
| --- | --- | --- | --- |
| `OPENAI_API_KEY` | empty | Any OpenAI-backed Catalog Studio capability is enabled | yes |
| `CATALOG_STUDIO_CLERK_AUTHORIZED_EMAILS` | empty | Email allowlisting is used | no |
| `CATALOG_STUDIO_CLERK_AUTHORIZED_SUBJECTS` | empty | Clerk subject allowlisting is used | no |
| `CATALOG_STUDIO_ADMIN_CLAIM_PATH` | empty | A custom Clerk admin claim is used | no |
| `CATALOG_STUDIO_ADMIN_CLAIM_VALUE` | `admin` | A custom Clerk admin claim is used | no |
| `CATALOG_STUDIO_RESPONSES_MODEL` | `gpt-5.5` | Responses draft authoring is used | no |
| `CATALOG_STUDIO_MODERATION_MODEL` | `omni-moderation-latest` | Moderated draft authoring is used | no |
| `CATALOG_STUDIO_RESPONSES_TIMEOUT_SECONDS` | `60` | Optional override | no |
| `CATALOG_STUDIO_RESPONSES_MAX_OUTPUT_TOKENS` | `2500` | Optional override | no |
| `CATALOG_STUDIO_REALTIME_ENABLED` | `false` | Set to `true` to enable voice | no |
| `CATALOG_STUDIO_REALTIME_MODEL` | `gpt-realtime-2` | Voice is enabled | no |
| `CATALOG_STUDIO_REALTIME_TRANSCRIPTION_MODEL` | `gpt-4o-mini-transcribe` | Voice is enabled | no |
| `CATALOG_STUDIO_REALTIME_CLIENT_SECRET_TTL_SECONDS` | `600` | Optional voice override | no |
| `CATALOG_STUDIO_REALTIME_TIMEOUT_SECONDS` | `15` | Optional voice override | no |
| `CATALOG_STUDIO_REALTIME_SAFETY_IDENTIFIER_SECRET` | empty | Voice is enabled | yes |
| `CATALOG_STUDIO_SHARED_WORKFLOWS` | `false` | Set to `true` only for shared presenter workflows | no |
| `CATALOG_STUDIO_TRACE_RETENTION_DAYS` | `7` | Optional trace override | no |
| `CATALOG_STUDIO_TRACE_MAX_DEPTH` | `6` | Optional trace override | no |
| `CATALOG_STUDIO_TRACE_MAX_STRING_LENGTH` | `1000` | Optional trace override | no |
| `CATALOG_STUDIO_TRACE_MAX_ARRAY_LENGTH` | `25` | Optional trace override | no |
| `CATALOG_STUDIO_TRACE_MAX_OBJECT_KEYS` | `50` | Optional trace override | no |
| `CATALOG_STUDIO_TRACE_MAX_BYTES` | `16384` | Optional trace override | no |
| `CATALOG_STUDIO_TRACE_REDACTED_KEYS` | empty | Add deployment-specific JSON keys to redact | no |
| `API_TRACE_CAPTURE_ENABLED` | `false` | Set to `true` only for authenticated developer trace capture | no |
| `API_TRACE_PAYLOAD_RETENTION_HOURS` | `24` | Optional generic trace payload retention override | no |
| `API_TRACE_METADATA_RETENTION_DAYS` | `7` | Optional generic trace topology retention override | no |
| `API_TRACE_MAX_DEPTH` | `6` | Optional generic trace redaction override | no |
| `API_TRACE_MAX_STRING_LENGTH` | `1000` | Optional generic trace redaction override | no |
| `API_TRACE_MAX_ARRAY_LENGTH` | `25` | Optional generic trace redaction override | no |
| `API_TRACE_MAX_OBJECT_KEYS` | `50` | Optional generic trace redaction override | no |
| `API_TRACE_MAX_BYTES` | `16384` | Optional generic trace payload override | no |
| `API_TRACE_REDACTED_KEYS` | empty | Add deployment-specific generic trace keys to redact | no |
| `API_TRACE_MAX_SPANS` | `100` | Optional per-trace topology override | no |
| `API_TRACE_MAX_LINKS` | `100` | Optional per-trace topology override | no |
| `API_TRACE_MAX_EVENTS` | `250` | Optional per-trace topology override | no |
| `API_TRACE_MAX_ARTIFACTS` | `50` | Optional per-trace topology override | no |
| `CATALOG_STUDIO_MEDIA_ALLOWED_HOSTS` | empty | Published remote images are used as edit sources | no |
| `CATALOG_STUDIO_MEDIA_FETCH_MAX_BYTES` | `8388608` | Optional remote image safety override | no |
| `CATALOG_STUDIO_MEDIA_FETCH_TIMEOUT_SECONDS` | `15` | Optional remote image safety override | no |
| `CATALOG_STUDIO_MEDIA_FETCH_MAX_REDIRECTS` | `3` | Optional remote image safety override | no |
| `PRODUCT_IMAGE_MODEL` | `gpt-image-2` | Product image generation is used | no |
| `PRODUCT_IMAGE_SIZE` | `1024x1024` | Optional image override | no |
| `PRODUCT_IMAGE_QUALITY` | `medium` | Optional image override | no |
| `PRODUCT_IMAGE_OUTPUT_FORMAT` | `jpeg` | Optional image override | no |
| `PRODUCT_IMAGE_OUTPUT_DIR` | `data/product-images` | Product image generation is used | no |
| `PRODUCT_IMAGE_URL_PATH` | `/product-images` | Product image generation is used | no |
| `PRODUCT_IMAGE_DETAIL_COUNT` | `3` | Optional legacy batch override | no |
| `PRODUCT_IMAGE_THUMBNAIL_SIZE` | `320` | Optional image override | no |
| `PRODUCT_IMAGE_REQUEST_TIMEOUT_SECONDS` | `300` | Optional image override | no |
| `PRODUCT_IMAGE_JOB_STALE_SECONDS` | `900` | Optional worker recovery override | no |
| `IMAGE_JOB_ADMIN_STALE_RECOVERY_SECONDS` | `60` | Optional admin recovery override | no |

`GET /api/admin/session` reports configuration readiness without contacting a
provider or returning values. Realtime reports `feature_disabled`,
`missing_api_key`, or `missing_safety_secret` when it is unavailable. A
configured result means the required settings are present; it does not prove
that the provider or browser media connection is currently healthy.

#### Production Realtime preflight

1. Configure `OPENAI_API_KEY`, set `CATALOG_STUDIO_REALTIME_ENABLED=true`, and
   set `CATALOG_STUDIO_REALTIME_SAFETY_IDENTIFIER_SECRET` to a dedicated random
   secret. The model, transcription model, client-secret TTL, and timeout are
   optional and use the defaults in the table above when omitted.
2. Deploy backend `main`. The production workflow writes the runtime settings,
   rejects an enabled deployment with a blank API key or safety secret, runs
   database migrations, and completes the API health check.
3. With an authorized Clerk session, call `GET /api/admin/session` and require
   `capabilities.realtime.configured` to be `true`. Do not log the bearer token
   or any runtime setting values.
4. In Catalog Studio, start voice, grant microphone access, and confirm the
   control reaches `listening`. Complete one instruction and then stop voice.
   A permission denial, provider/session error, or WebRTC transport failure
   must leave text authoring available and identify that boundary separately.

The deployment check proves that required settings are non-empty, the session
capability proves that the running API loaded them, and the browser check proves
microphone and provider transport health. All three checks are required for an
end-to-end production verification.

Important environment notes:

- `DOCKERHUB_IMAGE` must be lowercase and include a Docker Hub namespace, for
  example `quickstark/sterling-hollis-be`.
- `.env` and `deploy/runtime.env` are local/runtime secret files. They must not
  be committed, shared, or logged.
- The default embedding pairing is `text-embedding-3-small` with dimension
  `1536`. If you change model dimensions, use a new Pinecone index or recreate
  the old one.
- `PUBLIC_BASE_URL` must match the externally reachable scheme and host when
  using remote MCP clients or Apps SDK widgets.
- FastMCP validates request hosts. Add remote MCP hostnames to
  `MCP_ALLOWED_HOSTS`; add matching origins to `MCP_ALLOWED_ORIGINS` when the
  client sends `Origin`.
- `CLERK_AUTHORIZED_PARTIES` should include every frontend origin allowed to
  send Clerk session tokens.
- `DD_AGENT_HOST` must resolve from inside containers. The production compose
  file maps `host.docker.internal` to the Docker host.
- Runtime metrics require `DD_RUNTIME_METRICS_ENABLED=true` and DogStatsD UDP
  ingestion on the Datadog Agent.
- Use separate provider keys per environment. Rotate OpenAI, Pinecone, Datadog,
  Twilio, Docker Hub, Clerk, Postgres, SES, and AWS credentials after exposure
  or personnel changes.

### 2. Run Locally With Docker Compose

```bash
docker compose up --build
```

API: `http://localhost:8000`

Compose starts:

- `postgres`
- `api`
- `index-worker`

The API waits for Postgres, runs `alembic upgrade head`, serves FastAPI, mounts
`/mcp` when `ENABLE_MCP_ADAPTER=true`, and mounts `/ui-assets` when
`ENABLE_OPENAI_APPS_UI=true`. The worker polls durable indexing and product
image jobs from Postgres.

### 3. Generate, Load, and Index Data

```bash
curl -X POST http://localhost:8000/admin/synthetic/generate \
  -H 'content-type: application/json' \
  -d '{"seed":20260313}'
```

```bash
curl -X POST http://localhost:8000/admin/synthetic/load \
  -H 'content-type: application/json' \
  -d '{"run_id":"<RUN_ID>","entities":["stores","customers","products","orders","order_items","store_daily_metrics","supplier_product_offers"]}'
```

For API clients and MCP/operator flows, prefer the durable async indexing path:

1. `fashion_start_index_products`
2. `fashion_get_index_job`
3. `fashion_get_run_report`

The legacy synchronous endpoint is still available:

```bash
curl -X POST http://localhost:8000/admin/synthetic/index-products \
  -H 'content-type: application/json' \
  -d '{"run_id":"<RUN_ID>","batch_size":128}'
```

Run reports are available at:

```bash
curl http://localhost:8000/admin/synthetic/runs/<RUN_ID>/report
```

Each generated run also includes `analyst_store_category_v1.csv`, a 30-row
store/category analyst sample for spreadsheet and ChatGPT comparison workflows.
To create a standalone sample without the API:

```bash
python3 scripts/generate_analyst_story_sample.py --output /tmp/analyst_store_category_v1_sample.csv
```

If OpenAI or Pinecone keys are added after a run already exists, re-index the
run so fallback embeddings are replaced with provider-backed vectors:

```bash
make reindex-latest
```

## Vector Modes

Inspect the active vector mode:

```bash
curl http://localhost:8000/admin/system/vector-status | jq
```

Run a live provider probe:

```bash
curl 'http://localhost:8000/admin/system/vector-status?probe=true' | jq
```

`probe=true` performs real provider calls. The OpenAI probe makes one small
embedding request.

Modes:

- `cloud_full`: OpenAI and Pinecone are both configured.
- `openai_only`: embeddings can be generated, but retrieval remains local.
- `pinecone_with_fallback_embeddings`: Pinecone stores deterministic fallback
  vectors because OpenAI is absent.
- `local_fallback`: neither provider is configured; deterministic local vectors
  and SQL/rules behavior are used.

Product indexing writes store-scoped `product:*` vectors and global `catalog:*`
vectors under `PINECONE_CATALOG_NAMESPACE`.

## API Highlights

Frontend APIs:

- `GET /api/catalog`
- `GET /api/catalog/categories`
- `GET /api/catalog/products`
- `GET /api/stores/{store_id}/categories`
- `GET /api/categories`
- `GET /api/categories/{category}/products`
- `GET /api/products`
- `GET /api/products/{product_id}`
- `GET /api/products/{product_id}/related`
- `GET /api/search/products?q=<query>`
- `POST /api/recommendations/products`
- `POST /api/image-analysis`
- `POST /api/recommendations/image`
- `POST /api/chat`
- `GET/POST /api/demo/observability`
- `POST /api/demo/observability/reset`
- `POST /api/demo/observability/network-outage-log`

Operator/admin APIs:

- `GET /health`
- `POST /admin/synthetic/generate`
- `POST /admin/synthetic/load`
- `POST /admin/synthetic/index-products`
- `GET /admin/synthetic/runs/{run_id}/report`
- `GET /admin/system/vector-status`
- `POST /admin/product-images/generate`
- `GET /admin/product-images/jobs/{job_id}`
- `GET /admin/product-images/jobs`
- `GET/POST /admin/demo/observability`
- `POST /admin/demo/observability/reset`
- `POST /admin/demo/observability/network-outage-log`
- `POST /admin/demo/observability/trigger-error`
- `POST /admin/seed/load/{entity}?run_id=<RUN_ID>`
- `POST /admin/seed/run/{seed_run_id}/finalize?status=loaded`

Compatibility APIs:

- `POST /recommendations/customer`
- `POST /recommendations/merchandising`
- `GET /feeds/products/openai?store_id=<ID>&limit=2000`
- `POST /mcp` and related Streamable HTTP MCP traffic mounted at `/mcp`

The `/api/*` catalog endpoints are the primary retail frontend contract. Product
responses separate stable catalog fields from store-scoped inventory fields and
are backed by normalized `catalog_products`, `product_variants`, and
`store_inventory` tables. Legacy `products` rows remain for MCP/operator
compatibility.

Export the generated FastAPI schema:

```bash
make openapi
```

Use [docs/frontend-api.md](docs/frontend-api.md) and
[docs/frontend-openapi.yaml](docs/frontend-openapi.yaml) for the curated retail
frontend contract.

## Access Posture

The app currently separates routes by intended deployment boundary. Treat this
as the operational contract when exposing the service:

| Surface | Intended callers | Access posture |
| --- | --- | --- |
| `/health` | Load balancers, operators | Public or internal health check. |
| `/api/catalog`, `/api/categories`, `/api/products`, `/api/search/products`, `/api/recommendations/products`, `/api/image-analysis`, `/api/recommendations/image` | Retail frontend | Public shopper API. Provider-backed calls may incur OpenAI/Pinecone cost. |
| `/api/chat` | Retail frontend | Anonymous for public catalog/service questions; Clerk bearer token required for account, order, or personal recommendation requests. |
| `/api/demo/observability/*` | Demo/admin panels | Clerk bearer token plus allowlist via demo auth env vars. |
| `/admin/*` | Operators/admin automation | Not a public shopper API. Keep private, or protect with a reverse proxy, VPN, Cloudflare Access, or another explicit admin identity layer. |
| `/recommendations/*` and `/feeds/products/openai` | Legacy/operator integrations | Compatibility surface. Do not expose as the primary public frontend contract unless wrapped by the same controls as the caller. |
| `/mcp` | Local MCP clients or trusted remote connector | Local-only by default. Remote exposure requires an explicit identity and tool-permission boundary before ChatGPT or other clients can connect. |
| `/ui-assets/*` | Apps SDK widget host | Static widget assets. Pair with the `/mcp` access model and `PUBLIC_BASE_URL`/CSP settings. |

`MCP_ALLOWED_HOSTS` and `MCP_ALLOWED_ORIGINS` are host/origin validation
controls, not user authorization. They do not decide who may invoke tools.

## Storefront Chat

`POST /api/chat` is the stable frontend chat contract. Authentication is
optional:

- Anonymous shoppers can ask product, catalog, related-product, store contact,
  and general customer-service questions.
- Account, order-status, personal-size, personal-style, purchase-history, and
  personalized recommendation requests require a valid `Authorization: Bearer
  <Clerk token>` header.
- The frontend must not send `customer_id`; the backend derives customer
  identity from the signed-in token and linked customer record.

Deterministic routing is the default. Set `CHAT_ORCHESTRATION_MODE` to
`strands_product` only when OpenAI-backed Strands chat orchestration should be
used.

## Product Images

Retail frontends can upload inspiration images without storing the raw file in
this service:

```bash
curl -X POST http://localhost:8000/api/recommendations/image \
  -F "image=@/path/to/style.png;type=image/png" \
  -F "top_k=8"
```

The backend validates JPEG, PNG, or WebP uploads in memory, extracts structured
visual cues with OpenAI when configured, discards the raw bytes, and returns
catalog-card recommendations. `POST /api/image-analysis` exposes just the
analysis step.

Generate product image galleries from existing variant metadata:

```bash
. .venv/bin/activate
python scripts/generate_product_images.py --category womens_apparel --limit 10
```

For frontend-triggered or large batches, enqueue a job:

```bash
curl -X POST http://localhost:8000/admin/product-images/generate \
  -H "Content-Type: application/json" \
  -d '{"category":"womens_apparel","limit":25,"detail_count":3}'
```

Poll jobs:

```bash
curl http://localhost:8000/admin/product-images/jobs/imgjob_...
curl http://localhost:8000/admin/product-images/jobs?limit=10
```

Generate across the catalog by category:

```bash
python scripts/generate_category_images.py \
  --base-url https://sterling-hollis-be.quickstark.com \
  --batch-size 50 \
  --detail-count 3
```

Audit deployed image files against database URLs:

```bash
python scripts/audit_product_image_files.py --image-dir /app/data/product-images
```

Rewrite stored image URLs after a public hostname change:

```bash
python scripts/rewrite_product_image_urls.py --dry-run
python scripts/rewrite_product_image_urls.py
```

The API and worker share the same product-image volume in production, so
worker-generated files under `/app/data/product-images` are served immediately
from `/product-images/...`.

## MCP and Apps SDK Widgets

MCP endpoint:

```text
http://localhost:8000/mcp
```

Run the local MCP smoke test after the app is running:

```bash
make mcp-smoke
```

Representative MCP tool groups:

- Admin: vector status, latest run, synthetic generate/load/index/report.
- Customer and associate: customer lookup, customer search, store resolution,
  associate recommendations, customer value summaries, workspace rendering.
- Communication: SMS draft/update/send, email draft/update/get/send,
  recommendation email compatibility, message history, Twilio smoke test.
- Merchandising: workspaces, diagnostics, trend summaries, action
  recommendations, inventory views, mix recommendations, CSV export, strategy
  overrides.
- Unified workspace: overview, inventory view, action recommendations, product
  mix recommendations, CSV export.
- Executive: overview, event readiness, what-if simulation, campaign autopilot,
  strategy packets, CSV export, auto-optimization.
- Inventory: store checks, product search, store inventory, facets.
- Public catalog discovery: normalized product search and stable-ID detail for
  ChatGPT, with the unified widget rendering product cards.
- Feed compatibility: OpenAI-style export from the normalized published
  catalog.

Use `make mcp-smoke` or an MCP Inspector session to see the current full tool
surface.

ChatGPT cannot connect to `localhost`; remote ChatGPT Developer Mode usage needs
the same `/mcp` endpoint exposed at a public HTTPS URL. Do not expose it as a
bare public endpoint. Put an explicit identity layer in front of it, restrict
which tools each actor can invoke, and separate read-only, operator, admin, and
send-capable tools before using it outside a trusted local network. Set
`PUBLIC_BASE_URL`, `MCP_ALLOWED_HOSTS`, and `MCP_ALLOWED_ORIGINS` for that
deployment.

Apps SDK widget resources are thin HTML shells that load bundled JS/CSS from
`/ui-assets`. The active widget bundles are:

- customer workspace
- merchandising workspace
- executive workspace
- unified executive/merchandising workspace

The unified widget also renders results from `fashion_catalog_search` and
`fashion_catalog_product_detail`. Both tools read the same normalized catalog as
the storefront and `/api/chat`; only `published` products are returned. Drafts,
archives, Catalog Studio authoring metadata, workflow events, and administrative
state are excluded. `fashion_catalog_product_detail` accepts either a stable
catalog ID or a legacy product ID that resolves to the same normalized record.

`fashion_get_product_feed` uses this published normalized source as well, so a
newly published Catalog Studio product appears once under its stable catalog ID
without a duplicate legacy row.

Set `ENABLE_MCP_ADAPTER=false` to run without `/mcp`. Set
`ENABLE_OPENAI_APPS_UI=false` to disable `/ui-assets`, ChatGPT sandbox CORS, and
widget session endpoints.

## Security And Data Boundaries

Widget state should stay minimal. Persist only opaque IDs and non-sensitive UI
flags through `window.openai.setWidgetState` by default. Keep draft bodies,
recipient addresses, customer identity, message history, and authorization
decisions backend-owned. If a future widget needs to persist sensitive content
in host state, document the retention, deletion, and redaction rules before
shipping it.

Third-party data handling:

| Service | Data sent | Boundary |
| --- | --- | --- |
| OpenAI | Embedding text, image-analysis inputs, optional chat orchestration prompts, generated product image prompts | Do not send real PII unless the deployment has approved that data class. Raw shopper upload bytes are discarded after analysis. |
| Pinecone | Product/catalog vectors and metadata needed for retrieval | Keep namespaces environment-specific and avoid storing customer PII in vector metadata. |
| Datadog | APM spans, logs, metrics, LLM Observability traces, demo network logs | Redact secrets and avoid logging raw env values, credentials, or unneeded customer content. |
| Twilio | Draft-approved outbound SMS body and test destination | V1 delivery uses `TWILIO_TEST_TO_NUMBER`; synthetic customer phones are lookup/UI data only. |
| SES/AWS | Draft-approved outbound email body and recipient | Keep send authority in persisted backend draft state. |
| Clerk | JWT issuer/JWKS validation and authorized-party checks | Use Clerk identity only to link authenticated frontend requests to backend customer records. |
| GitHub/Docker Hub | Deployment secrets and container images | Restrict secret access and rotate tokens after exposure or role changes. |

## Customer Communication Model

Outbound communication is draft-first:

- Recommendation tools never send messages automatically.
- `fashion_prepare_customer_sms` creates a persisted SMS draft.
- `fashion_update_customer_sms_draft` lets an associate edit copy and selected
  products before send.
- `fashion_send_customer_sms` is the only SMS send action.
- Email tools follow the same prepare/update/get/send draft lifecycle.
- Twilio v1 sends go to `TWILIO_TEST_TO_NUMBER`.
- Synthetic customer phone numbers are used for search and UI realism, not live
  delivery.

## Datadog Demo Observability

The demo observability harness is off by default. Enable it with:

```bash
curl -X POST http://localhost:8000/admin/demo/observability \
  -H 'content-type: application/json' \
  -d '{"enabled":true,"mode":"latency"}'
```

Modes include latency, error, latency plus error, and network outage. The
network outage mode returns controlled 503s from app-facing API paths while
leaving `/health` and demo reset controls available. Use the
`network-outage-log` endpoints to send the `snmp_trap_logs` payloads to Datadog
Logs HTTP Intake with the backend `DD_API_KEY`.

Datadog LLM Observability uses `DD_LLMOBS_ENABLED=true` for canonical app-level
chat traces. Keep `STRANDS_OTEL_ENABLED=false` unless debugging Strands-native
event-loop telemetry.

## Testing

```bash
pip install -e '.[dev]'
pytest
```

Make targets:

```bash
make install
make test
make up
make logs
make migrate
make e2e
make reindex-latest
make mcp-smoke
make openapi
make down
```

`make e2e` starts the stack, waits for health, runs generate/load/index/report,
and prints compact JSON summaries for data generation and recommendation checks.

## Self-Hosted Deployment

This repo includes a deployment path for a local self-hosted GitHub runner that:

- uploads `.env` values to GitHub Actions secrets
- reads `VERSION` and tags the image as `<version>-<short_sha>`
- builds and pushes one API image to Docker Hub
- deploys the API and index worker locally with Docker Compose
- connects to an external Postgres instance through `PG*` values or
  `DATABASE_URL`

Tracked deployment assets:

- [scripts/deploy.sh](scripts/deploy.sh)
- [scripts/setup-secrets.sh](scripts/setup-secrets.sh)
- [.github/workflows/deploy-self-hosted.yaml](.github/workflows/deploy-self-hosted.yaml)
- [deploy/docker-compose.prod.yml](deploy/docker-compose.prod.yml)
- [VERSION](VERSION)

Production defaults:

- Docker Hub image: `quickstark/sterling-hollis-be`
- API container name: `sterling-hollis-be`
- worker container name: `sterling-hollis-be-index-worker`
- exposed host port: `8000`
- Postgres: external instance, not a bundled container
- transport: REST API and MCP served from the API container on port `8000`

Deploy flow:

```bash
./scripts/deploy.sh
```

The GitHub Actions workflow does not deploy pull-request branches. A merge to
`main` produces the `push` event that builds and deploys through the self-hosted
runner. Manual dispatch is restricted to the `main` ref, and deployment runs
are serialized so two pushes cannot mutate the local Compose stack at once.

Required GitHub secrets:

- `PGHOST`
- `PGPORT`
- `PGDATABASE`
- `PGUSER`
- `PGPASSWORD`
- `DOCKERHUB_USER`
- `DOCKERHUB_TOKEN`
- `DOCKERHUB_IMAGE`

Optional secret groups include `DATABASE_URL`, `API_PORT`, OpenAI/Pinecone,
store source, public MCP/UI, Clerk, Datadog/OTEL, SES/Amazon email, Twilio, and
feature-gate values.

The workflow writes `deploy/runtime.env`, runs:

```bash
docker compose -f deploy/docker-compose.prod.yml up -d
```

and health-checks:

```text
http://localhost:${API_PORT:-8000}/health
```

## Migrations

```bash
export DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/productdb
alembic upgrade head
```

Create a migration after model changes:

```bash
alembic revision --autogenerate -m "describe change"
```

If a database already has tables created before Alembic was introduced, stamp it
first:

```bash
alembic stamp head
```

The container startup script and `scripts/init_db.py` automatically stamp `head`
when they detect an existing schema without `alembic_version`.

## Notes

- The committed repo does not include a default live store-source URL. Configure
  `STORE_SOURCE_INDEX_URL` and `STORE_SOURCE_DETAIL_URL_TEMPLATE`, or point
  `STORE_SOURCE_CACHE_PATH` at a real local cache file before running synthetic
  generation.
- Embedding generation falls back to deterministic local vectors when OpenAI is
  not configured.
- Pinecone indexing is namespace-scoped per store as `store_<store_id>` and uses
  `PINECONE_CATALOG_NAMESPACE` for store-independent catalog vectors.
- Synthetic customer records avoid real PII and include hashed surrogate tokens.
