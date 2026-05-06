# sterling-hollis-be

Synthetic fashion data platform with:
- Postgres as source of truth
- Pinecone as vector retrieval layer
- FastAPI for generation, ingestion, indexing, and recommendation APIs
- MCP-first operator workflows for store associates and merchandisers
- ChatGPT Apps SDK-ready widget rendering for high-level operator actions

## What this implements

- Live store seeding from configurable source endpoints
- Deterministic synthetic generators for:
  - `stores`, `customers`, `products`, `orders`, `order_items`, `store_daily_metrics`, `analyst_store_category_v1`
- CSV run artifacts under `data/runs/<run_id>/`
- Admin load and finalize endpoints
- Product embedding/index endpoint (OpenAI + Pinecone when configured, deterministic fallback otherwise)
- Durable async product indexing jobs backed by Postgres and a lightweight worker service
- Customer and merchandising recommendation endpoints
- Human-first MCP tools for store resolution, customer resolution, associate recommendations, and merch workflows
- Customer search by name, email, and synthetic phone number
- Unified customer lookup tool that resolves exact identifiers or returns candidate lists for ambiguous searches
- Dedicated demo customer seeded with a stable real-number lookup target for demos
- Draft-then-send Twilio SMS support using a global test destination number
- Editable SMS drafts, history, and Twilio smoke-test support
- Apps SDK render tool for a minimal customer-search workspace
- External ChatGPT UI bundle served from `/ui-assets` instead of inline widget HTML
- Archived legacy workspace assets under `app/static/chatgpt-ui/archive/legacy-workspaces`
- Automatic fast-path recommendation mode for structured associate requests
- OpenAI-commerce-style product feed endpoint

## Project structure

- `app/models.py`: SQLAlchemy schema
- `app/services/store_source.py`: live store ingestion + normalization
- `app/services/synthetic_generator.py`: deterministic synthetic dataset generation
- `app/services/loader.py`: CSV ingestion into Postgres
- `app/services/indexing.py`: embedding generation + Pinecone upsert
- `app/services/recommendations.py`: customer + merchandising recommenders
- `app/routers/admin_synthetic.py`: admin APIs
- `app/routers/recommendations.py`: recommendation/feed APIs

## Quick start

### 1) Configure env

```bash
cp .env.example .env
```

Set at minimum:
- `DATABASE_URL` or the `PGHOST` / `PGPORT` / `PGDATABASE` / `PGUSER` / `PGPASSWORD` group
- `STORE_SOURCE_INDEX_URL`
- `STORE_SOURCE_DETAIL_URL_TEMPLATE`
- `MCP_ALLOWED_HOSTS` for any non-local MCP hostname
- `MCP_ALLOWED_ORIGINS` if your MCP clients send `Origin`

Optional for vector cloud indexing:
- `OPENAI_API_KEY`
- `PINECONE_API_KEY`
- `PINECONE_INDEX_NAME` if you want a non-default index name
- `PINECONE_CLOUD` / `PINECONE_REGION` if your Pinecone project is not `aws/us-east-1`
- `EMBEDDING_MODEL` / `EMBEDDING_DIMENSION` if you intentionally change embedding models

Optional for Datadog APM, profiling, runtime metrics, DBM propagation, Dynamic Instrumentation, and LLM Observability:
- `DD_TRACE_ENABLED=true`
- `DD_AGENT_HOST` and `DD_TRACE_AGENT_PORT` for the Datadog Agent reachable from the app container
- `DD_ENV`, `DD_SERVICE`, and `DD_VERSION`
- `DD_SITE`, `DD_API_KEY`, and `DD_APP_KEY`
- `DD_AI_GUARD_ENABLED=true` to enable the inline chat AI Guard check
- `DD_AI_GUARD_ENDPOINT` only when overriding the SDK-derived endpoint
- `DD_AI_GUARD_DEMO_FALLBACK_ENABLED=true` only for local/offline demos
- `DD_PROFILING_ENABLED=true`
- `DD_RUNTIME_METRICS_ENABLED=true`
- `DD_LLMOBS_ENABLED=true`
- `STRANDS_OTEL_ENABLED=false` to keep Strands-native OTEL traces disabled unless debugging agent event loops
- `DD_LLMOBS_AGENTLESS_ENABLED=true`
- `DD_LLMOBS_ML_APP=sterling-hollis-be`
- `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`
- `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf`
- `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://otlp.datadoghq.com/v1/traces`

Optional for Twilio-assisted customer communication:
- `TWILIO_ACCOUNT_SID`
- `TWILIO_API_KEY_SID`
- `TWILIO_API_KEY_SECRET`
- `TWILIO_SENDER_NUMBER`
- `TWILIO_TEST_TO_NUMBER`
- `INDEX_WORKER_POLL_SECONDS` if you want a non-default worker poll interval

Optional for Clerk storefront authentication:
- `CLERK_ISSUER`
- `CLERK_JWKS_URL`
- `CLERK_AUTHORIZED_PARTIES`, for example `http://localhost,http://127.0.0.1,https://sterling-hollis-fe.quickstark.com,https://sterling-hollis.quickstark.com`

Deployment-related values:
- `DOCKERHUB_USER`
- `DOCKERHUB_TOKEN`
- `DOCKERHUB_IMAGE` such as `quickstark/sterling-hollis-be`
- `API_PORT` if you do not want to expose the API on `8000`
- `PUBLIC_BASE_URL` for remote MCP/App widget deployment, for example `https://sterling-hollis-be.quickstark.com`

### Environment reference

Runtime / database:
- `DATABASE_URL`
- `PGHOST`
- `PGPORT`
- `PGDATABASE`
- `PGUSER`
- `PGPASSWORD`
- `DATA_DIR`
- `STORE_SOURCE_INDEX_URL`
- `STORE_SOURCE_DETAIL_URL_TEMPLATE`
- `STORE_SOURCE_CACHE_PATH`
- `MCP_ALLOWED_HOSTS`
- `MCP_ALLOWED_ORIGINS`
- `PUBLIC_BASE_URL`
- `CLERK_ISSUER`
- `CLERK_JWKS_URL`
- `CLERK_AUTHORIZED_PARTIES`

Vector / recommendation:
- `OPENAI_API_KEY`
- `PINECONE_API_KEY`
- `PINECONE_INDEX_NAME`
- `PINECONE_CLOUD`
- `PINECONE_REGION`
- `EMBEDDING_MODEL`
- `EMBEDDING_DIMENSION`

Demo observability harness:
- `DEMO_OBSERVABILITY_ENABLED`
- `DEMO_OBSERVABILITY_MODE`
- `DEMO_OBSERVABILITY_LATENCY_SECONDS`
- `DEMO_OBSERVABILITY_TARGET_STORE_ID`

Twilio:
- `TWILIO_ACCOUNT_SID`
- `TWILIO_API_KEY_SID`
- `TWILIO_API_KEY_SECRET`
- `TWILIO_SENDER_NUMBER`
- `TWILIO_TEST_TO_NUMBER`
- `INDEX_WORKER_POLL_SECONDS`

Deployment:
- `DOCKERHUB_USER`
- `DOCKERHUB_TOKEN`
- `DOCKERHUB_IMAGE`
- `API_PORT`

Datadog:
- `DD_TRACE_ENABLED`
- `DD_AGENT_HOST`
- `DD_TRACE_AGENT_PORT`
- `DD_TRACE_AGENT_URL`
- `DD_ENV`
- `DD_SERVICE`
- `DD_VERSION`
- `DD_SITE`
- `DD_API_KEY`
- `DD_APP_KEY`
- `DD_AI_GUARD_ENABLED`
- `DD_AI_GUARD_ENDPOINT`
- `DD_AI_GUARD_DEMO_FALLBACK_ENABLED`
- `DD_PROFILING_ENABLED`
- `DD_PROFILING_TIMELINE_ENABLED`
- `DD_RUNTIME_METRICS_ENABLED`
- `DD_LLMOBS_ENABLED`
- `STRANDS_OTEL_ENABLED`
- `DD_LLMOBS_AGENTLESS_ENABLED`
- `DD_LLMOBS_ML_APP`
- `DD_LOGS_INJECTION`
- `DD_DATA_STREAMS_ENABLED`
- `DD_DBM_PROPAGATION_MODE`
- `DD_DYNAMIC_INSTRUMENTATION_ENABLED`
- `DD_REMOTE_CONFIGURATION_ENABLED`
- `DD_CODE_ORIGIN_FOR_SPANS_ENABLED`
- `DD_SYMBOL_DATABASE_UPLOAD_ENABLED`
- `DD_TRACE_OBFUSCATION_QUERY_EXEC_ENABLED`
- `DD_TRACE_REMOVE_INTEGRATION_SERVICE_NAMES_ENABLED`
- `DD_DOGSTATSD_DISABLE`
- `OTEL_SERVICE_NAME`
- `OTEL_SEMCONV_STABILITY_OPT_IN`
- `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`
- `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`
- `OTEL_EXPORTER_OTLP_TRACES_HEADERS`

Notes:
- `DOCKERHUB_IMAGE` must be lowercase and include the Docker Hub namespace, for example `quickstark/sterling-hollis-be`, not just `sterling-hollis-be`.
- Production can run with only the `PG*` values. The app and entrypoint derive `DATABASE_URL` from them automatically.
- The committed repo does not include a default live store-source URL. Configure the store source locally through env or rely on a cached snapshot file.
- FastMCP enforces host validation on MCP requests. For any remote MCP deployment, add the public hostname to `MCP_ALLOWED_HOSTS`. If your client sends `Origin`, add the matching origin to `MCP_ALLOWED_ORIGINS`.
- `PUBLIC_BASE_URL` should match the externally reachable scheme and host when using remote MCP clients or Apps SDK widgets.
- `CLERK_AUTHORIZED_PARTIES` should list the frontend origins allowed to send Clerk session tokens. Keep local origins and add every production storefront origin, including the deployed FE host.
- Datadog instrumentation is provided by `ddtrace` and starts through `ddtrace-run` when Datadog env is present. For deployment, add the Datadog values above as GitHub Actions secrets. The workflow writes them into `deploy/runtime.env`.
- The demo observability harness is off by default. To create a Datadog demo incident, enable it with `POST /admin/demo/observability`; latency mode adds a slow `demo.inventory_reconciliation` APM span inside `/api/chat`, while error modes mark the reconciliation step degraded without failing chat. Use `POST /admin/demo/observability/trigger-error` only when you intentionally need an unhandled `DemoSupplierFeedSchemaError` 500 for Error Management.
- Datadog LLM Observability uses `DD_LLMOBS_ENABLED=true` for the canonical app-level chat traces. Keep `STRANDS_OTEL_ENABLED=false` by default to avoid duplicate Strands-native OTEL traces; set it to `true` only when debugging Strands event-loop telemetry. When Strands OTEL is enabled and `OTEL_EXPORTER_OTLP_TRACES_HEADERS` is unset, the app derives `dd-api-key=<DD_API_KEY>,dd-otlp-source=llmobs` at startup.
- MCP client LLM Observability uses Datadog's automatic MCP Python SDK instrumentation. Run `make mcp-smoke` with `DD_LLMOBS_ENABLED=true`, `DD_LLMOBS_AGENTLESS_ENABLED=true`, and `DD_API_KEY` to emit MCP client spans.
- `DD_AGENT_HOST` must resolve from inside the Docker containers. The production compose file maps `host.docker.internal` to the Docker host, so that is the default deployment value when a host-level Datadog Agent is listening for APM traffic.
- Keep `DD_TRACE_OBFUSCATION_QUERY_EXEC_ENABLED=true` unless you intentionally want SQL query values to appear in trace metadata.

### 2) Run with Docker Compose

```bash
docker compose up --build
```

API: `http://localhost:8000`

Compose now starts the API in migration-first mode:
- wait for Postgres
- run `alembic upgrade head`
- start FastAPI
- start a separate `index-worker` service that polls durable indexing jobs from Postgres

The local development compose file includes a bundled Postgres container and sets `UVICORN_RELOAD=true`. The production deployment path does not bundle Postgres and runs Uvicorn without reload.

### 2a) Understand vector modes

This project has four vector operating modes:

- `cloud_full`: `OPENAI_API_KEY` and `PINECONE_API_KEY` are both configured. Product vectors are generated by OpenAI and stored in Pinecone. Customer recommendations can use `hybrid_vector_rules`.
- `openai_only`: OpenAI embeddings are available, but Pinecone is not configured. Embeddings can be generated, but retrieval stays local and recommendations do not use Pinecone.
- `pinecone_with_fallback_embeddings`: Pinecone is configured without OpenAI. This stores deterministic fallback vectors in Pinecone. It is operationally valid, but not a useful semantic retrieval setup.
- `local_fallback`: neither provider is configured. Deterministic local vectors are used and recommendations fall back to SQL/rules behavior when no Pinecone retrieval is available.

You can inspect the current runtime mode with:

```bash
curl http://localhost:8000/admin/system/vector-status | jq
```

If you want a live provider check, use:

```bash
curl 'http://localhost:8000/admin/system/vector-status?probe=true' | jq
```

`probe=true` performs real provider calls. The OpenAI probe generates one small embedding request, so it has a small usage cost.

### 3) Generate -> load -> index

```bash
# Generate synthetic CSV assets
curl -X POST http://localhost:8000/admin/synthetic/generate \
  -H 'content-type: application/json' \
  -d '{"seed":20260313}'

# Load generated entities into Postgres
curl -X POST http://localhost:8000/admin/synthetic/load \
  -H 'content-type: application/json' \
  -d '{"run_id":"<RUN_ID>","entities":["stores","customers","products","orders","order_items","store_daily_metrics"]}'

# Index products synchronously (legacy; may take long enough to hit MCP/client timeouts)
curl -X POST http://localhost:8000/admin/synthetic/index-products \
  -H 'content-type: application/json' \
  -d '{"run_id":"<RUN_ID>","batch_size":128}'

# Run report
curl http://localhost:8000/admin/synthetic/runs/<RUN_ID>/report
```

Each generated run now also includes `analyst_store_category_v1.csv`, a 30-row
store+category analyst-mock dataset designed for Google Sheets/ChatGPT
comparison workflows (`current 90d` vs `prior 90d`) with a moderate aligned vs
contrarian recommendation mix.

You can also generate a standalone sample file without running the API:

```bash
python3 scripts/generate_analyst_story_sample.py --output /tmp/analyst_store_category_v1_sample.csv
```

For MCP/operator flows, prefer the durable async indexing path:

1. `fashion_start_index_products`
2. `fashion_get_index_job`
3. `fashion_get_run_report`

This avoids client-side timeouts while the worker continues processing in the background.

### 3a) If you add OpenAI/Pinecone keys after a run already exists

You must re-index products for the existing run. Earlier runs may already have `local_only` embeddings recorded from fallback mode, and those need to be replaced with real OpenAI vectors and Pinecone upserts.

Manual API path:

```bash
curl -X POST http://localhost:8000/admin/synthetic/index-products \
  -H 'content-type: application/json' \
  -d '{"run_id":"<RUN_ID>","batch_size":128}'
```

One-shot Make target:

```bash
make reindex-latest
```

That target:
- waits for API health
- finds the most recent loaded/indexed synthetic run
- re-runs product indexing
- prints the indexing summary and embedding coverage report

Important constraint:
- `EMBEDDING_DIMENSION` must match the Pinecone index dimension.
- The default pairing is `text-embedding-3-small` with dimension `1536`.
- If you change embedding models to a different dimension, use a new `PINECONE_INDEX_NAME` or recreate the old index.

### Postgres and Pinecone

Postgres:
- Postgres is the system of record.
- The schema is managed by Alembic.
- Startup runs `alembic upgrade head`, so an empty database is created automatically on first boot.
- The initial schema migration lives in [alembic/versions/f790a40c397b_initial_schema.py](alembic/versions/f790a40c397b_initial_schema.py).

Key tables:
- `synthetic_runs`
- `stores`
- `customers`
- `products`
- `orders`
- `order_items`
- `product_embeddings`
- `catalog_product_embeddings`
- `store_daily_metrics`
- `synthetic_validation_failures`
- `customer_communications`
- `ui_sessions`
- `twilio_smoke_tests`
- `index_jobs`

If you want SQL instead of running Alembic directly:

```bash
. .venv/bin/activate
alembic upgrade head --sql > schema.sql
```

Pinecone:
- Pinecone is a hosted service, not a container in this repo.
- The app talks to Pinecone through [app/services/pinecone_service.py](app/services/pinecone_service.py).
- The Pinecone index is created on demand by the code when product indexing runs.
- Product indexing writes both store-scoped `product:*` vectors and global
  `catalog:*` vectors in `PINECONE_CATALOG_NAMESPACE` for store-independent
  visual search.
- Rebuilding Postgres from scratch requires reloading data and re-running product indexing so Postgres rows and Pinecone vectors are aligned.

## API highlights

- `GET /api/categories`
- `GET /api/categories/{category}/products`
- `GET /api/products`
- `GET /api/products/{product_id}`
- `GET /api/products/{product_id}/related`
- `GET /api/search/products?q=<query>`
- `POST /api/recommendations/products`
- `POST /api/image-analysis`
- `POST /api/recommendations/image`
- `POST /admin/synthetic/generate`
- `POST /admin/synthetic/load`
- `POST /admin/synthetic/index-products`
- `GET /admin/system/vector-status`
- `GET /admin/synthetic/runs/{run_id}/report`
- `POST /admin/seed/load/{entity}?run_id=<RUN_ID>`
- `POST /admin/seed/run/{seed_run_id}/finalize?status=loaded`
- `POST /recommendations/customer`
- `POST /recommendations/merchandising`
- `GET /feeds/products/openai?store_id=<ID>&limit=2000` deprecated compatibility export
- `POST /mcp` and related Streamable HTTP MCP traffic mounted at `/mcp`

The `/api/*` catalog endpoints are the primary retail frontend contract. They expose
category metadata, product listing/search/detail, related products, and product
recommendations without requiring OpenAI or Pinecone. Product responses split stable
catalog fields from store-scoped inventory fields and are backed by normalized
`catalog_products`, `product_variants`, and `store_inventory` tables. Legacy
`products` rows remain for MCP/operator compatibility during the transition.

Set `ENABLE_MCP_ADAPTER=false` to run the backend without mounting `/mcp`. Set
`ENABLE_OPENAI_APPS_UI=false` to disable `/ui-assets`, ChatGPT sandbox CORS, and
widget session endpoints.

### Frontend API spec

Frontend-facing API documentation lives in:

- `docs/frontend-api.md` for the retail frontend integration guide.
- `docs/frontend-openapi.yaml` for the curated frontend OpenAPI contract.

The running FastAPI service also exposes its canonical generated schema at
`/openapi.json`. To export that generated schema into the repo:

```bash
.venv/bin/python scripts/export_openapi.py
```

or:

```bash
make openapi
```

### Consumer image recommendations

Retail frontends can upload a consumer inspiration image without storing the raw
file in this service:

```bash
curl -X POST http://localhost:8000/api/recommendations/image \
  -F "image=@/path/to/style.png;type=image/png" \
  -F "top_k=8"
```

The API validates JPEG, PNG, or WebP uploads in memory, sends the image to
OpenAI for structured visual attribute extraction, discards the raw bytes, and
returns catalog-card recommendations. `POST /api/image-analysis` exposes the
analysis step alone for frontends that want to preview or reuse extracted cues.

Visual recommendations query the global catalog vector namespace first. Re-run
product indexing after deploying this version so existing catalog rows have
`catalog:*` vectors:

```bash
make reindex-latest
```

### Product image generation

Product variants have an `image_link` and `image_set`. Synthetic legacy products
still contain placeholder `fashion.example` URLs, and the normalization backfill
copies usable image metadata onto `product_variants`.

To generate real product images with OpenAI and update `product_variants.image_link`
and `product_variants.image_set`:

```bash
. .venv/bin/activate
python scripts/generate_product_images.py --category womens_apparel --limit 10
```

The script builds prompts from product title, description, brand, category, color,
material, gender, and season. For each display variant it writes one thumbnail plus
multiple full-size detail images, stores the primary URL in
`product_variants.image_link`, and stores the full gallery in
`product_variants.image_set`. `--store-id` is optional and means "variants stocked
by this store"; it does not create store-specific product images.

The files are written to `PRODUCT_IMAGE_OUTPUT_DIR` (`data/product-images` by
default), public URLs use `PRODUCT_IMAGE_URL_PATH` (`/product-images` by default),
and the FastAPI app serves that directory from the same path.

Changing `PUBLIC_BASE_URL` only affects newly generated image URLs. To rewrite
previously stored image URLs after a hostname change, run:

```bash
python scripts/rewrite_product_image_urls.py --dry-run
python scripts/rewrite_product_image_urls.py
```

The rewrite updates stored product image URLs under `PRODUCT_IMAGE_URL_PATH` from
the old `https://products-api.quickstark.com` base to the current
`PUBLIC_BASE_URL`.

For deployed public URLs to work, run generation inside the deployed API container
or copy generated files into the deployed `/app/data/product-images` volume. Running
the script on a laptop can update DB URLs while leaving the actual image files only
on the laptop filesystem.

Useful dry-run example:

```bash
python scripts/generate_product_images.py --category womens_apparel --limit 3 --dry-run
```

For frontend-triggered or large batch generation, enqueue a background job instead
of waiting on a synchronous request:

```bash
curl -X POST http://localhost:8000/admin/product-images/generate \
  -H "Content-Type: application/json" \
  -d '{"category":"womens_apparel","limit":25,"detail_count":3}'
```

API jobs default to `missing_images_only=true`, so broad category/all-catalog jobs
pick variants that still have placeholder or empty image URLs. Set
`overwrite=true` to regenerate existing galleries.

Poll the job until `status` is `succeeded` or `failed`:

```bash
curl http://localhost:8000/admin/product-images/jobs/imgjob_...
curl http://localhost:8000/admin/product-images/jobs?limit=10
```

The existing `index-worker` service now acts as the background worker for both
indexing and image generation jobs. Keep it running with `OPENAI_API_KEY`
configured. In production, the API and worker both mount
`deploy_products_data:/app/data`, so worker-generated files under
`/app/data/product-images` are served immediately from `/product-images/...`.
The volume intentionally keeps the original Compose-created Docker volume name
so previously generated image files remain attached after the backend container
rename. The deploy workflow also copies any files from temporary rename-era
volumes into `deploy_products_data` before recreating the stack.

To audit whether the deployed container has all files referenced by the database:

```bash
python scripts/audit_product_image_files.py --image-dir /app/data/product-images
```

If the audit reports missing files, recover them by copying the original generated
files into the deployed `/app/data/product-images` volume. Regeneration is not
required when the files still exist on another Docker volume, host directory, or
backup.

To generate across the catalog by category, use the API orchestration script. It
fetches `GET /api/categories`, enqueues one category batch at a time, polls each
job, and repeats a category until the API returns `attempted: 0`:

```bash
python scripts/generate_category_images.py \
  --base-url https://sterling-hollis-be.quickstark.com \
  --batch-size 50 \
  --detail-count 3
```

Preview the category plan without enqueueing jobs:

```bash
python scripts/generate_category_images.py \
  --base-url https://sterling-hollis-be.quickstark.com \
  --plan-only
```

Optional image settings:
- `PRODUCT_IMAGE_MODEL` default `gpt-image-2`
- `PRODUCT_IMAGE_SIZE` default `1024x1024`
- `PRODUCT_IMAGE_QUALITY` default `medium`
- `PRODUCT_IMAGE_OUTPUT_FORMAT` default `jpeg`
- `PRODUCT_IMAGE_DETAIL_COUNT` default `3`
- `PRODUCT_IMAGE_THUMBNAIL_SIZE` default `320`

## MCP highlights

Low-level/admin MCP tools:
- `fashion_generate_synthetic`
- `fashion_load_synthetic`
- `fashion_start_index_products`
- `fashion_get_index_job`
- `fashion_list_index_jobs`
- `fashion_get_run_report`

Operator/customer tools:
- `fashion_lookup_customer`
- `fashion_find_customers`
- `fashion_open_customer_workspace`
- `fashion_resolve_customer`
- `fashion_resolve_store`
- `fashion_store_associate_recommend`
- `fashion_prepare_customer_sms`
- `fashion_update_customer_sms_draft`
- `fashion_send_customer_sms`
- `fashion_prepare_customer_email_draft`
- `fashion_update_customer_email_draft`
- `fashion_get_customer_email_draft`
- `fashion_send_customer_email_draft`
- `fashion_customer_message_history`
- `fashion_twilio_smoke_test`

Render tools:
- `fashion_render_customer_search_workspace`
- `fashion_render_merch_workspace`
- `fashion_render_exec_workspace`

### Customer lookup behavior

Use `fashion_lookup_customer` when the query may be ambiguous.

- exact `email`, `customer_id`, or full `phone_e164` resolves directly
- partial email, name fragments, and phone last-4 return ranked candidates

Use `fashion_resolve_customer` only when you already have an exact identifier and want strict resolution.

### Async indexing behavior

`fashion_index_products` still exists as a legacy synchronous tool, but operator clients may time out before it returns on larger runs.

Preferred pattern:
1. `fashion_start_index_products(run_id=...)`
2. poll `fashion_get_index_job(job_id=...)`
3. confirm final state with `fashion_get_run_report(run_id=...)`

The `index-worker` service executes these jobs independently of the MCP request lifecycle, so client timeouts do not cancel indexing.

## MCP Endpoint

The application now exposes a Streamable HTTP MCP server on the same host and port as the REST API:

```bash
http://localhost:8000/mcp
```

This MCP endpoint is intended to be:
- locally testable with an MCP client or MCP Inspector
- remotely exposable later through Cloudflare for ChatGPT Developer Mode
- transport-compatible with modern MCP clients

### MCP tools

Low-level/admin tools:
- `fashion_vector_status`
- `fashion_latest_run`
- `fashion_generate_synthetic`
- `fashion_load_synthetic`
- `fashion_index_products`
- `fashion_get_run_report`
- `fashion_customer_recommendations`
- `fashion_merchandising_recommendations`
- `fashion_get_product_feed`

Human-first operator tools:
- `fashion_resolve_store`
- `fashion_resolve_customer`
- `fashion_find_customers`
- `fashion_open_customer_workspace`
- `fashion_store_associate_recommend`
- `fashion_prepare_customer_sms`
- `fashion_update_customer_sms_draft`
- `fashion_send_customer_sms`
- `fashion_prepare_customer_email_draft`
- `fashion_update_customer_email_draft`
- `fashion_get_customer_email_draft`
- `fashion_send_customer_email_draft`
- `fashion_customer_message_history`
- `fashion_twilio_smoke_test`
- `fashion_merch_action_recommendations`
- `fashion_merch_diagnostics`
- `fashion_merch_trend_summary`

Apps SDK render tools:
- `fashion_render_customer_search_workspace`
- `fashion_render_merch_workspace`
- `fashion_render_exec_workspace`

### Local MCP smoke test

Once the app is running:

```bash
make mcp-smoke
```

That connects to `http://localhost:8000/mcp`, initializes an MCP session, lists tools, and calls:
- `fashion_vector_status`
- `fashion_latest_run`

When Datadog LLM Observability env is present, the smoke client runs through `ddtrace-run` so Datadog can automatically instrument MCP client calls.

### Calling MCP tools from Python

The MCP tool surface is flattened for agent friendliness. Most tools now take top-level arguments directly.

```python
import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    async with streamable_http_client("http://localhost:8000/mcp") as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            status = await session.call_tool("fashion_vector_status", {"probe": False})
            print(status.structuredContent)

            recs = await session.call_tool(
                "fashion_customer_recommendations",
                {
                    "store_id": "1001",
                    "occasion": "wedding guest dress",
                    "budget_max": 900,
                    "top_k": 3
                },
            )
            print(recs.structuredContent)


asyncio.run(main())
```

Examples:

- `fashion_customer_recommendations(store_id="1001", occasion="wedding guest dress", budget_max=900, top_k=3)`
- `fashion_merchandising_recommendations(store_id="1001", objective="margin", lookback_days=90, top_k=5)`
- `fashion_index_products(run_id="<RUN_ID>", batch_size=128)`
- `fashion_load_synthetic(run_id="<RUN_ID>", entities=["stores","customers","products","orders","order_items","store_daily_metrics"])`

Human-first examples:

- `fashion_resolve_store(store_query="Dallas downtown")`
- `fashion_find_customers(query="avery 1234", limit=10)`
- `fashion_open_customer_workspace(customer_query="Jorgen Nielsen", style_constraints={"constraint_source":"chat_image","target_categories":["mens_apparel"],"exclude_categories":["athleticwear"],"target_genders":["male"],"style_keywords":["tailored","micro-check"]})`
- `fashion_open_customer_workspace(customer_query="Jorgen Nielsen", initial_email_draft_id="msg_123abc", initial_email_subject="Canvas draft subject", initial_email_body="Hi Jorgen, ...")`
- `fashion_resolve_customer(email="avery.parker.1@example-fashion.test")`
- `fashion_resolve_customer(phone_last4="1234")`
- `fashion_store_associate_recommend(store_query="Dallas", customer_email="avery.parker.1@example-fashion.test", occasion="wedding guest dress", budget_max=900, top_k=5)`
- `fashion_store_associate_recommend(store_query="Dallas", customer_email="avery.parker.1@example-fashion.test", occasion="wedding guest dress", budget_max=900, top_k=5, retrieval_mode="auto")`
- `fashion_store_associate_recommend(store_id="1001", customer_id="cust_000001", top_k=6, retrieval_mode="auto", style_constraints={"constraint_source":"chat_image","target_categories":["mens_apparel","shoes"],"target_genders":["male"],"style_keywords":["tailored","minimal"]})`
- `fashion_prepare_customer_sms(store_query="Dallas", customer_email="avery.parker.1@example-fashion.test", occasion="wedding guest dress", budget_max=900, top_k=3)`
- `fashion_prepare_customer_email_draft(store_id="1001", customer_id="cust_000001", selected_product_ids=["prod_000001","prod_000002"], to_email="buyer@example.com", subject="Curated picks from your stylist")`
- `fashion_update_customer_email_draft(message_id="<MESSAGE_ID>", subject="Updated subject", body_text="Updated body copy", to_email="buyer@example.com")`
- `fashion_get_customer_email_draft(message_id="<MESSAGE_ID>")`
- `fashion_send_customer_email_draft(message_id="<MESSAGE_ID>")`
- `fashion_update_customer_sms_draft(message_id="<MESSAGE_ID>", body_text="Updated follow-up copy", selected_product_ids=["prod_000001","prod_000002"])`
- `fashion_send_customer_sms(message_id="<MESSAGE_ID>")`
- `fashion_customer_message_history(customer_email="avery.parker.1@example-fashion.test", status="sent", limit=10)`
- `fashion_twilio_smoke_test(body_text="Smoke test from sterling-hollis-be")`
- `fashion_merch_action_recommendations(store_query="Dallas", question="What should this store feature this week if we care about margin?", top_k=8)`
- `fashion_merch_diagnostics(store_query="Dallas", question="Why are shoes underperforming here?", category="shoes", compare_mode="peer_and_prior_period", lookback_days=90)`
- `fashion_merch_trend_summary(store_query="Dallas", question="Summarize recent store trends for handbags and women’s apparel.", category="handbags", compare_mode="peer_and_prior_period")`
- `fashion_exec_overview(lookback_days=90, objective="revenue", top_k_stores=12)`
- `fashion_exec_event_readiness_radar(lookback_days=56, events=["wedding","holiday_party","workwear"])`
- `fashion_exec_what_if_simulator(lookback_days=90, discount_pct=10, floor_space_shift_pct=5, from_category="womens_apparel", to_category="shoes")`
- `fashion_exec_campaign_autopilot_prepare(to_email="store.manager@example.com", lookback_days=56, top_k=6)`
- `fashion_exec_campaign_autopilot_send(draft_id="<DRAFT_ID>", approved=true)`

Render-tool examples for ChatGPT Apps:

- `fashion_render_customer_search_workspace(query="avery", limit=10)`
- `fashion_open_customer_workspace(customer_query="Jorgen Nielsen", style_constraints={"constraint_source":"chat_image","target_categories":["mens_apparel"],"target_genders":["male"],"style_keywords":["tailored"]})`
- `fashion_render_customer_search_workspace(selected_customer_id="cust_000001", initial_email_draft_id="msg_123abc")`

### Manual local MCP testing with Inspector

You can also use the official MCP Inspector:

```bash
npx -y @modelcontextprotocol/inspector
```

Then connect the inspector UI to:

```bash
http://localhost:8000/mcp
```

### Local editor/client config examples

Codex CLI:

```bash
codex mcp add fashionDb --url http://127.0.0.1:8000/mcp
```

VS Code `.vscode/mcp.json`:

```json
{
  "servers": {
    "fashionDb": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

Cursor `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "fashionDb": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

### Important ChatGPT constraint

The MCP server is transport-compatible with ChatGPT Developer Mode, but ChatGPT itself cannot connect to `localhost`.

For ChatGPT, the same MCP endpoint must be exposed on a remote public URL later, for example through Cloudflare. That is the correct next phase after local validation.

### Twilio communication model

The customer-communication flow is intentionally conservative:

- recommendation tools never send SMS automatically
- `fashion_prepare_customer_sms` creates a persisted draft in `customer_communications`
- `fashion_update_customer_sms_draft` lets an associate edit message copy and selected products before send
- `fashion_send_customer_sms` is the only send action
- `fashion_twilio_smoke_test` validates the live Twilio path with no customer context
- all v1 outbound messages go to `TWILIO_TEST_TO_NUMBER`
- the outbound sender is `TWILIO_SENDER_NUMBER`
- synthetic customer `phone_e164` values are used for search and UI realism only; they are not the live delivery target
- one seeded demo customer is assigned `+12146932322` for reliable demo lookup

### Operator performance notes

- Store resolution and peer-store lookup use a short in-process TTL cache.
- Associate recommendation tools support `retrieval_mode`:
  - `auto`: uses fast SQL/rules for structured requests and semantic retrieval otherwise
  - `fast`: skips embedding/Pinecone and uses SQL/rules only
  - `semantic`: forces embedding + Pinecone when providers are enabled

Current message body content is text-only:
- associate greeting
- recommended product titles and prices
- product links

The synthetic dataset includes `image_link`, but those URLs are placeholders and are not used for outbound messaging.

Image-guided recommendation notes:
- if a user uploads an image in chat, pass extracted cues into `style_constraints` on recommendation tools
- supported fields: `target_categories`, `exclude_categories`, `target_genders`, `style_keywords`, and `constraint_source`
- recommendation responses include `applied_style_constraints`, `constraint_source`, and `constraint_stage` so the workspace can explain what was applied

### Apps SDK widgets

The repo now includes Apps SDK-ready render tools layered on top of the human-first MCP tools:

- customer search workspace
- merchandising workspace
- executive overview workspace

This widget is mounted as an MCP resource and is intended for ChatGPT app usage. It relies on `PUBLIC_BASE_URL` for widget CSP and remote access.

Implementation notes:
- widget HTML is now a thin shell that loads a bundled JS/CSS UI from `/ui-assets`
- widget runtime uses `window.openai.callTool` directly (no custom parent RPC fallback)
- email delivery now supports a draft lifecycle: prepare -> update/get -> send by `message_id`
- workspace uses a draft-first path (`Copy Draft`, `Copy Canvas Prompt`, `Refresh Draft`, `Send Draft Email`) while legacy direct-send email tool remains available for compatibility
- chat-first handoff is supported by workspace hydration fields: `initial_email_draft_id` plus optional `initial_email_subject` and `initial_email_body`
- send authority stays in persisted backend draft state
- widget state persisted with optional `window.openai.setWidgetState` includes `query`, selected customer, filters, selected products, style constraints, and draft fields (`emailTo`, `emailSubject`, `emailBody`, `emailDraftId`)
- legacy multi-workspace UI assets are archived under `app/static/chatgpt-ui/archive/legacy-workspaces`

## Testing

```bash
pip install -e '.[dev]'
pytest
```

## Self-Hosted Deployment

This repo includes a deployment path for a local self-hosted GitHub runner that:
- uploads `.env` values to GitHub Actions secrets
- reads the tracked `VERSION` file and tags the image as `<version>-<short_sha>`
- builds and pushes one API image to Docker Hub
- deploys the API locally on the runner host with Docker Compose
- connects to your external Postgres instance using `PG*` values or `DATABASE_URL`

Tracked deployment assets:
- [scripts/deploy.sh](scripts/deploy.sh)
- [scripts/setup-secrets.sh](scripts/setup-secrets.sh)
- [.github/workflows/deploy-self-hosted.yaml](.github/workflows/deploy-self-hosted.yaml)
- [deploy/docker-compose.prod.yml](deploy/docker-compose.prod.yml)

Production defaults:
- Docker Hub image: `quickstark/sterling-hollis-be`
- API container name: `sterling-hollis-be`
- worker container name: `sterling-hollis-be-index-worker`
- exposed host port: `8000`
- Postgres: external instance, not a bundled container
- transport: REST API and MCP served from the same container on port `8000`

### Deploy flow

1. Put your production values in `.env`.
2. Run:

```bash
./scripts/deploy.sh
```

That script:
- validates required env keys
- lets you update `VERSION`
- uploads GitHub secrets
- prompts for a commit message
- pushes the current branch to trigger the workflow

If there are no tracked changes to commit, the script can dispatch the workflow manually instead.

### Production deployment assets

- Workflow: [.github/workflows/deploy-self-hosted.yaml](.github/workflows/deploy-self-hosted.yaml)
- Runtime compose file: [deploy/docker-compose.prod.yml](deploy/docker-compose.prod.yml)
- Deploy script: [scripts/deploy.sh](scripts/deploy.sh)
- Secret uploader: [scripts/setup-secrets.sh](scripts/setup-secrets.sh)
- Version file: [VERSION](VERSION)

The workflow:
- builds and pushes `latest`
- builds and pushes `<version>-<short_sha>`
- writes `deploy/runtime.env` from GitHub secrets
- runs `docker compose -f deploy/docker-compose.prod.yml up -d`
- health-checks `http://localhost:${API_PORT:-8000}/health`

### Secrets expected by the workflow

Required:
- `PGHOST`
- `PGPORT`
- `PGDATABASE`
- `PGUSER`
- `PGPASSWORD`
- `DOCKERHUB_USER`
- `DOCKERHUB_TOKEN`
- `DOCKERHUB_IMAGE`

Optional:
- `DATABASE_URL`
- `API_PORT`
- `OPENAI_API_KEY`
- `PINECONE_API_KEY`
- `PINECONE_INDEX_NAME`
- `PINECONE_CLOUD`
- `PINECONE_REGION`
- `EMBEDDING_MODEL`
- `EMBEDDING_DIMENSION`
- `PUBLIC_BASE_URL`
- `CLERK_ISSUER`
- `CLERK_JWKS_URL`
- `CLERK_AUTHORIZED_PARTIES`
- `TWILIO_ACCOUNT_SID`
- `TWILIO_API_KEY_SID`
- `TWILIO_API_KEY_SECRET`
- `TWILIO_SENDER_NUMBER`
- `TWILIO_TEST_TO_NUMBER`

Example external Postgres settings:

```env
PGHOST=192.168.1.200
PGPORT=9001
PGDATABASE=products
PGUSER=postgres
PGPASSWORD=your-password
```

Example Docker Hub settings:

```env
DOCKERHUB_USER=quickstark
DOCKERHUB_TOKEN=your-token
DOCKERHUB_IMAGE=quickstark/sterling-hollis-be
API_PORT=8000
```

Example remote MCP/Twilio settings:

```env
PUBLIC_BASE_URL=https://sterling-hollis-be.quickstark.com
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_API_KEY_SID=SKxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_API_KEY_SECRET=your-secret
TWILIO_SENDER_NUMBER=+15555550123
TWILIO_TEST_TO_NUMBER=+15555550999
```

## Make Targets

```bash
make up
make test
make migrate
make e2e
make reindex-latest
make mcp-smoke
make down
```

`make e2e` brings the stack up, waits for health, runs generate/load/index/report, and prints compact JSON summaries for the run and recommendation checks.

`make reindex-latest` is the operational shortcut after you add cloud vector credentials to `.env`.

`make mcp-smoke` verifies the mounted MCP endpoint and prints the discovered tools plus sample structured tool output.

## Migrations (Alembic)

```bash
# Use your target DB URL
export DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/productdb

# Apply migrations
alembic upgrade head

# Create a new migration after model changes
alembic revision --autogenerate -m "describe change"
```

If your database already has tables created from earlier `create_all(...)` startup behavior, stamp it first:

```bash
alembic stamp head
```

The container startup script and `scripts/init_db.py` both handle this automatically by stamping `head` once when they detect an existing schema without `alembic_version`.

## Notes

- For learning and resilience, embedding generation falls back to deterministic local vectors when OpenAI is not configured.
- Pinecone indexing is namespace-scoped per store: `store_<store_id>`.
- Synthetic customer records avoid real PII and include hashed surrogate tokens.
