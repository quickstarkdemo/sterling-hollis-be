SHELL := /bin/bash

COMPOSE ?= docker compose
API_URL ?= http://localhost:8000
E2E_SEED ?= 20260313

.PHONY: install test up down logs migrate stamp e2e reindex-latest mcp-smoke openapi

install:
	python3 -m venv .venv
	. .venv/bin/activate && pip install -e '.[dev]'

test:
	. .venv/bin/activate && pytest -q

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f api postgres

migrate:
	. .venv/bin/activate && alembic upgrade head

stamp:
	. .venv/bin/activate && alembic stamp head

e2e:
	set -euo pipefail; \
	$(COMPOSE) up -d --build; \
	until curl -fsS $(API_URL)/health >/dev/null; do sleep 2; done; \
	GEN=$$(curl -fsS -X POST $(API_URL)/admin/synthetic/generate -H 'content-type: application/json' -d '{"seed":$(E2E_SEED)}'); \
	RUN_ID=$$(printf '%s' "$$GEN" | jq -r '.run_id'); \
	LOAD=$$(curl -fsS -X POST $(API_URL)/admin/synthetic/load -H 'content-type: application/json' -d "{\"run_id\":\"$$RUN_ID\",\"entities\":[\"stores\",\"customers\",\"products\",\"orders\",\"order_items\",\"store_daily_metrics\",\"supplier_product_offers\"]}"); \
	INDEX=$$(curl -fsS -X POST $(API_URL)/admin/synthetic/index-products -H 'content-type: application/json' -d "{\"run_id\":\"$$RUN_ID\",\"batch_size\":128}"); \
	REPORT=$$(curl -fsS $(API_URL)/admin/synthetic/runs/$$RUN_ID/report); \
	STORE_ID=$$($(COMPOSE) exec -T postgres psql -U postgres -d productdb -Atc "select id from stores where seed_run_id='$$RUN_ID' order by id limit 1;"); \
	CUSTOMER_ID=$$($(COMPOSE) exec -T postgres psql -U postgres -d productdb -Atc "select id from customers where seed_run_id='$$RUN_ID' and home_store_id='$$STORE_ID' order by id limit 1;"); \
	CUST_REC=$$(curl -fsS -X POST $(API_URL)/recommendations/customer -H 'content-type: application/json' -d "{\"store_id\":\"$$STORE_ID\",\"customer_id\":\"$$CUSTOMER_ID\",\"occasion\":\"wedding\",\"budget_max\":900,\"top_k\":5}"); \
	MERCH_REC=$$(curl -fsS -X POST $(API_URL)/recommendations/merchandising -H 'content-type: application/json' -d "{\"store_id\":\"$$STORE_ID\",\"objective\":\"margin\",\"lookback_days\":90,\"top_k\":5}"); \
	echo "$$GEN" | jq '{run_id,row_counts,stores_discovered}'; \
	echo "$$LOAD" | jq '{run_id,loaded_rows}'; \
	echo "$$INDEX" | jq '{run_id,attempted,indexed,failed,status_breakdown}'; \
	echo "$$REPORT" | jq '{run_id,status,loaded_counts,embedding_coverage,validation_failures}'; \
	echo "$$CUST_REC" | jq '{store_id,strategy,recommendations:(.recommendations|map({product_id,title,score})[:3])}'; \
	echo "$$MERCH_REC" | jq '{store_id,objective,recommendations:(.recommendations|map({product_id,title,metric_value})[:3])}'

reindex-latest:
	set -euo pipefail; \
	until curl -fsS $(API_URL)/health >/dev/null; do sleep 2; done; \
	RUN_ID=$$($(COMPOSE) exec -T postgres psql -U postgres -d productdb -Atc "select id from synthetic_runs where status in ('loaded','indexed') order by started_at desc limit 1;"); \
	if [ -z "$$RUN_ID" ]; then echo "No loaded synthetic run found"; exit 1; fi; \
	echo "Re-indexing $$RUN_ID"; \
	curl -fsS -X POST $(API_URL)/admin/synthetic/index-products -H 'content-type: application/json' -d "{\"run_id\":\"$$RUN_ID\",\"batch_size\":128}" | jq '{run_id,attempted,indexed,failed,status_breakdown}'; \
	curl -fsS $(API_URL)/admin/synthetic/runs/$$RUN_ID/report | jq '{run_id,status,embedding_coverage,validation_failures}'

mcp-smoke:
	set -euo pipefail; \
	until curl -fsS $(API_URL)/health >/dev/null; do sleep 2; done; \
	scripts/run_mcp_smoke.sh $(API_URL)/mcp; \
	.venv/bin/python scripts/mcp_bundle_smoke.py $(API_URL)

openapi:
	.venv/bin/python scripts/export_openapi.py
